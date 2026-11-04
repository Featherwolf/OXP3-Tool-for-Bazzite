import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from oxp3deck import sysexec

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


class WhichTest(unittest.TestCase):
    def test_which(self):
        sh = sysexec.which("sh")
        self.assertTrue(sh and os.path.isabs(sh))
        self.assertIsNone(sysexec.which("no-such-program-oxp3deck"))


if __name__ == "__main__":
    unittest.main()
