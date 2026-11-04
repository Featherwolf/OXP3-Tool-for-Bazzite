import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from oxp3deck import sysexec
from oxp3deck.context import Ctx
from tests import stub_decky

PY = sys.executable    # the test runner's Python; the plugin itself never uses sys.executable


def py(code: str) -> list[str]:
    return [PY, "-I", "-c", code]


class RunTest(unittest.TestCase):
    def test_output_and_rc(self):
        r = sysexec.run(py("import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"), timeout=20)
        self.assertEqual((r.rc, r.out, r.err), (3, "out\n", "err\n"))

    def test_lines_stream_in_order_on_the_calling_thread(self):
        seen = []
        me = threading.get_ident()

        def on_line(line):
            seen.append((line, threading.get_ident(), time.monotonic()))

        code = ("import sys, time\n"
                "for i in range(50): print(i)\n"
                "print('E1', file=sys.stderr)\n"
                "sys.stdout.flush(); sys.stderr.flush(); time.sleep(1)\n"
                "print('last'); print('partial', end='')")
        r = sysexec.run(py(code), timeout=20, on_line=on_line)
        self.assertEqual(r.rc, 0)
        lines = [s for s, _, _ in seen]
        self.assertEqual([x for x in lines if x not in ("E1", "last", "partial")], [str(i) for i in range(50)])
        self.assertIn("E1", lines)
        self.assertEqual(lines[-2:], ["last", "partial"])
        self.assertTrue(all(t == me for _, t, _ in seen))
        times = dict((s, t) for s, _, t in seen)
        self.assertGreater(times["last"] - times["0"], 0.5)    # streamed, not delivered at the end

    def test_full_pipes_do_not_deadlock(self):
        code = "import sys; sys.stderr.write('e' * (1 << 20)); sys.stdout.write('o' * (1 << 20))"
        n = []
        r = sysexec.run(py(code), timeout=30, on_line=n.append)
        self.assertEqual(r.rc, 0)
        self.assertEqual((len(r.out), len(r.err)), (1 << 20, 1 << 20))
        self.assertEqual(sum(len(x) for x in n), 2 << 20)        # long lines arrive in pieces
        self.assertTrue(all(len(x) <= sysexec.LINE_MAX for x in n))

    def test_progress_meter_keeps_last_redraw(self):
        seen = []
        sysexec.run(py("print('10%\\r50%\\r100%\\r'); print('crlf\\r')"), timeout=20, on_line=seen.append)
        self.assertEqual(seen, ["100%", "crlf"])

    def test_killed_by_signal_is_not_a_special_code(self):
        r = sysexec.run(["sh", "-c", "kill -INT $$"], timeout=10)
        self.assertEqual(r.rc, 130)

    def test_timeout(self):
        t = time.monotonic()
        r = sysexec.run(["sleep", "10"], timeout=0.5)
        self.assertEqual(r.rc, -2)
        self.assertIn("timed out", r.err)
        self.assertLess(time.monotonic() - t, 5)

    def test_timeout_kills_the_group(self):
        t = time.monotonic()
        r = sysexec.run(["sh", "-c", "sleep 10 & sleep 10"], timeout=0.5, start_new_session=True)
        self.assertEqual(r.rc, -2)
        self.assertLess(time.monotonic() - t, 5)

    def test_background_child_keeping_the_pipe(self):
        t = time.monotonic()
        r = sysexec.run(["sh", "-c", "sleep 4 & echo hi"], timeout=20)
        self.assertEqual((r.rc, r.out), (0, "hi\n"))
        self.assertLess(time.monotonic() - t, 3.5)

    def test_cannot_start(self):
        r = sysexec.run(["/nonexistent/program"])
        self.assertEqual(r.rc, -1)
        self.assertIn("cannot start", r.err)
        self.assertEqual(sysexec.run([]).rc, -1)
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(sysexec.run(["true"], cwd=os.path.join(d, "missing")).rc, -1)

    def test_stdin_is_devnull(self):
        r = sysexec.run(["cat"], timeout=5)
        self.assertEqual((r.rc, r.out), (0, ""))

    def test_clean_environment(self):
        leak = {"LD_LIBRARY_PATH": "/tmp/_MEIx", "LD_PRELOAD_ORIG": "x", "PYTHONPATH": "/tmp/_MEIx",
                "PYTHONHOME": "/tmp/_MEIx", "_MEIPASS2": "/tmp/_MEIx", "_PYI_APPLICATION_HOME_DIR": "/tmp/_MEIx"}
        with mock.patch.dict(os.environ, leak):
            r = sysexec.run(["env"], env_extra={"OXP3_TEST": "yes"}, timeout=10)
        names = {line.split("=", 1)[0] for line in r.out.splitlines()}
        self.assertTrue(names.isdisjoint(leak), names & set(leak))
        self.assertIn("OXP3_TEST=yes", r.out.splitlines())

    def test_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            r = sysexec.run(["pwd"], cwd=d, timeout=10)
            self.assertEqual(r.out.strip(), os.path.realpath(d))

    def test_failing_callback_does_not_break_the_run(self):
        def boom(_line):
            raise ValueError("boom")

        with self.assertLogs("oxp3deck", "ERROR"):
            r = sysexec.run(py("print(1); print(2)"), timeout=20, on_line=boom)
        self.assertEqual((r.rc, r.out), (0, "1\n2\n"))

    def test_bad_utf8(self):
        r = sysexec.run(py("import sys; sys.stdout.buffer.write(b'a\\xffb\\n')"), timeout=20)
        self.assertEqual(r.out, "a�b\n")


class OutputFileTest(unittest.TestCase):
    """output_file: the program writes to a root-only file that is followed, not to a pipe (RT-1)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "user-x.log")

    def test_followed_while_it_runs_then_removed(self):
        seen, started = [], []

        def on_start(pid):
            st = os.stat(self.path)
            started.append((pid, stat.S_IMODE(st.st_mode), os.readlink(f"/proc/{pid}/fd/1"),
                            os.readlink(f"/proc/{pid}/fd/2")))

        code = ("import sys, time\n"
                "print('first', flush=True); print('E1', file=sys.stderr, flush=True); time.sleep(1)\n"
                "print('last'); print('partial', end=''); sys.exit(5)")
        r = sysexec.run(py(code), timeout=20, output_file=self.path, on_start=on_start,
                        on_line=lambda line: seen.append((line, time.monotonic())))
        self.assertEqual((r.rc, r.out, r.err), (5, "first\nE1\nlast\npartial", ""))
        self.assertEqual([x for x, _ in seen], ["first", "E1", "last", "partial"])
        self.assertGreater(seen[2][1] - seen[0][1], 0.5)        # streamed, not delivered at the end
        pid, mode, out, err = started[0]
        self.assertEqual((mode, out, err), (0o600, self.path, self.path))   # no pipe to lose
        self.assertFalse(os.path.exists(self.path))

    def test_an_existing_file_or_link_is_not_used(self):
        outside = os.path.join(self.tmp.name, "outside")
        with open(outside, "w") as f:
            f.write("keep")
        for planted in ("file", "link"):
            with self.subTest(planted=planted):
                if planted == "file":
                    with open(self.path, "w") as f:
                        f.write("old")
                else:
                    os.symlink(outside, self.path)
                r = sysexec.run(["echo", "hi"], output_file=self.path, timeout=10)
                self.assertEqual(r.rc, -1)
                self.assertIn("cannot create", r.err)
                self.assertTrue(os.path.lexists(self.path))     # not ours: left alone
                os.unlink(self.path)
        with open(outside) as f:
            self.assertEqual(f.read(), "keep")

    def test_cannot_start(self):
        r = sysexec.run(["/nonexistent/program"], output_file=self.path)
        self.assertEqual(r.rc, -1)
        self.assertIn("cannot start", r.err)
        self.assertFalse(os.path.exists(self.path))

    def test_timeout_kills_the_group(self):
        t = time.monotonic()
        pidfile = os.path.join(self.tmp.name, "pid")
        r = sysexec.run(["sh", "-c", f"echo started; sleep 30 & echo $! > {pidfile}; sleep 31"], timeout=0.5,
                        output_file=self.path, start_new_session=True)
        self.assertEqual((r.rc, r.out), (-2, "started\n"))
        self.assertIn("timed out after 0.5 s", r.err)
        self.assertLess(time.monotonic() - t, 5)
        self.assertFalse(os.path.exists(self.path))
        with open(pidfile) as f:
            pid = int(f.read())
        end = time.monotonic() + 5
        while sysexec.proc_starttime(MY_ROOT, pid) is not None and time.monotonic() < end:
            time.sleep(0.05)
        self.assertIsNone(sysexec.proc_starttime(MY_ROOT, pid))

    def test_a_long_output_does_not_fill_the_file(self):
        sizes = []

        def on_line(line):
            if os.path.exists(self.path):
                sizes.append(os.path.getsize(self.path))

        code = ("import sys, time\n"
                "for burst in range(4):\n"
                "    sys.stdout.write(''.join(f'{burst} {i:036d}\\n' for i in range(50))); sys.stdout.flush()\n"
                "    time.sleep(0.6)\n")
        with mock.patch.object(sysexec, "FILE_MAX", 1000):
            seen = []
            r = sysexec.run(py(code), timeout=30, output_file=self.path,
                            on_line=lambda line: on_line(line) or seen.append(line))
        self.assertEqual(r.rc, 0)
        self.assertEqual(len(seen), 200)          # every line still arrives
        self.assertEqual(len(r.out), 200 * 39)
        self.assertLessEqual(max(sizes), 2 * 50 * 39)   # emptied after each burst (one burst is 1950 bytes)

    def test_a_writer_faster_than_on_line_is_capped(self):
        """RS-1: the file never reaches its end for us, so it is never emptied there; FILE_CAP bounds it anyway
        (before, `yes` grew it to 3 GB of tmpfs in 3 s)."""
        sizes, seen = [], []

        def on_line(line):
            if os.path.exists(self.path):
                sizes.append(os.path.getsize(self.path))
            seen.append(line)
            time.sleep(0.001)            # job.say logs every line: much slower than the program

        code = ("import sys, time\n"
                "line = 'z' * 3999 + '\\n'; end = time.monotonic() + 0.5\n"
                "while time.monotonic() < end:\n"
                "    sys.stdout.write(line * 4); sys.stdout.flush(); time.sleep(0.001)\n")
        with mock.patch.object(sysexec, "FILE_MAX", 64 << 10), mock.patch.object(sysexec, "FILE_CAP", 256 << 10):
            r = sysexec.run(py(code), timeout=60, output_file=self.path, on_line=on_line)
        self.assertEqual(r.rc, 0)
        self.assertIn(sysexec.SKIPPED, seen)
        self.assertLess(max(sizes), 2 << 20)       # without the cap: about 7 MB
        self.assertIn(sysexec.SKIPPED + "\n", r.out)

    def test_capped_file_is_emptied_unread(self):
        with open(self.path, "x"):
            pass
        wfd = os.open(self.path, os.O_WRONLY | os.O_APPEND)
        rfd = os.open(self.path, os.O_RDONLY)
        self.addCleanup(os.close, wfd)
        self.addCleanup(os.close, rfd)
        pipe, seen = sysexec._Pipe(), []
        with mock.patch.object(sysexec, "FILE_CAP", 1000):
            os.write(wfd, b"first\nunfinished")
            self.assertTrue(sysexec._read_file(rfd, wfd, pipe, seen.extend, chunks=1))
            os.write(wfd, b" line\n" + b"y\n" * 1000)
            self.assertTrue(sysexec._read_file(rfd, wfd, pipe, seen.extend))
            self.assertEqual(os.fstat(wfd).st_size, 0)
            os.write(wfd, b"after\n")
            self.assertTrue(sysexec._read_file(rfd, wfd, pipe, seen.extend))
        self.assertEqual(seen, ["first", "unfinished", sysexec.SKIPPED, "after"])
        self.assertEqual(pipe.text(), f"first\nunfinished\n{sysexec.SKIPPED}\nafter\n")

    def test_a_child_that_writes_on_after_the_exit(self):
        """The rest of the file is read for EXIT_GRACE at most: a background child that keeps writing cannot keep
        the job waiting (here: a file that never stops growing)."""
        t = time.monotonic()
        with mock.patch.object(sysexec, "EXIT_GRACE", 0.3), mock.patch.object(sysexec, "_read_file",
                                                                                 lambda *a, **k: True):
            r = sysexec.run(["true"], timeout=10, output_file=self.path)
        self.assertEqual(r.rc, 0)
        self.assertLess(time.monotonic() - t, 3)

    def test_failing_on_start(self):
        def boom(pid):
            raise ValueError("boom")

        with self.assertLogs("oxp3deck", "ERROR"):
            r = sysexec.run(["echo", "hi"], timeout=10, on_start=boom)
        self.assertEqual((r.rc, r.out), (0, "hi\n"))
        pids = []
        r = sysexec.run(["echo", "hi"], timeout=10, output_file=self.path, on_start=pids.append)
        self.assertEqual((r.rc, r.out, len(pids)), (0, "hi\n", 1))


MY_ROOT = Ctx(user="jordan", uid=1000, gid=1000, home="/nonexistent", settings_dir="/s", runtime_dir="/r",
              log_dir="/l", plugin_dir="/p", root="/")


class RunDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.c = stub_decky.make_ctx(self.tmp.name)
        self.folder = self.c.sys_path("/run/oxp3deck")

    def test_created_root_only(self):
        self.assertEqual(sysexec.run_dir(self.c), self.folder)
        os.chmod(self.folder, 0o755)
        sysexec.run_dir(self.c)
        self.assertEqual(stat.S_IMODE(os.stat(self.folder).st_mode), 0o700)

    def test_link_or_someone_elses_folder_refused(self):
        os.makedirs(os.path.dirname(self.folder))
        os.symlink(self.tmp.name, self.folder)
        with self.assertRaises(OSError):
            sysexec.run_dir(self.c)
        self.assertEqual(sysexec.remove_files(self.c, re.compile(r".*")), [])
        os.unlink(self.folder)
        if os.geteuid() == 0:
            os.mkdir(self.folder)
            os.chown(self.folder, 1000, 1000)
            with self.assertRaises(OSError):
                sysexec.run_dir(self.c)
            self.assertEqual(sysexec.remove_files(self.c, re.compile(r".*")), [])

    def test_remove_files(self):
        self.assertEqual(sysexec.remove_files(self.c, re.compile(r".*")), [])    # no folder yet
        sysexec.run_dir(self.c)
        outside = os.path.join(self.tmp.name, "outside.out")
        open(outside, "w").close()
        for name in ("apply-1.out", "apply-1.err", "keep.json"):
            open(os.path.join(self.folder, name), "w").close()
        os.symlink(outside, os.path.join(self.folder, "link.out"))
        os.mkdir(os.path.join(self.folder, "dir.out"))
        removed = sysexec.remove_files(self.c, re.compile(r"[a-z0-9-]+\.(?:out|err)"))
        self.assertEqual(sorted(removed), ["apply-1.err", "apply-1.out"])
        self.assertEqual(sorted(os.listdir(self.folder)), ["dir.out", "keep.json", "link.out"])
        self.assertTrue(os.path.exists(outside))


class ProcTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.c = stub_decky.make_ctx(self.tmp.name)

    def stat_file(self, pid, comm, state, start):
        d = self.c.sys_path(f"/proc/{pid}")
        os.makedirs(d, exist_ok=True)
        rest = [state, "1", "1234", "1234"] + ["0"] * 15 + [str(start), "0", "0"]
        with open(os.path.join(d, "stat"), "w") as f:
            f.write(f"{pid} ({comm}) {' '.join(rest)}\n")

    def test_fake_proc(self):
        self.stat_file(4242, "run user) S 1 (x", "S", 98765)     # comm may hold ") " and spaces
        self.assertEqual(sysexec.proc_starttime(self.c, 4242), 98765)
        self.assertTrue(sysexec.proc_alive(self.c, 4242, 98765))
        self.assertFalse(sysexec.proc_alive(self.c, 4242, 98764))     # the pid was reused
        self.assertFalse(sysexec.proc_alive(self.c, 4242, None))
        self.stat_file(4243, "runuser", "Z", 5)
        self.assertIsNone(sysexec.proc_starttime(self.c, 4243))       # exited, not reaped yet
        for pid in (4244, 0, -1, "4242", True, None, 4242.0):
            self.assertIsNone(sysexec.proc_starttime(self.c, pid), pid)
            self.assertFalse(sysexec.proc_alive(self.c, pid, 98765), pid)

    def test_real_proc(self):
        me = sysexec.proc_starttime(MY_ROOT, os.getpid())
        self.assertIsInstance(me, int)
        self.assertTrue(sysexec.proc_alive(MY_ROOT, os.getpid(), me))
        child = subprocess.Popen(["sleep", "10"])
        start = sysexec.proc_starttime(MY_ROOT, child.pid)
        self.assertTrue(sysexec.proc_alive(MY_ROOT, child.pid, start))
        child.kill()
        child.wait()
        self.assertFalse(sysexec.proc_alive(MY_ROOT, child.pid, start))


class WhichTest(unittest.TestCase):
    def test_which(self):
        sh = sysexec.which("sh")
        self.assertTrue(sh and os.path.isabs(sh))
        self.assertIsNone(sysexec.which("no-such-program-oxp3deck"))


if __name__ == "__main__":
    unittest.main()
