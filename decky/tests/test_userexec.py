import os
import stat
import tempfile
import unittest
from unittest import mock

from oxp3deck import sysexec, userexec
from oxp3deck.context import Ctx
from tests import stub_decky

ROOT = os.geteuid() == 0


def ctx(home="/home/jordan", **kw):
    fields = dict(user="jordan", uid=1000, gid=1000, home=home, settings_dir="/s", runtime_dir="/r", log_dir="/l",
                  plugin_dir="/p")
    fields.update(kw)
    return Ctx(**fields)


USER_ENV = ["HOME=/home/jordan", "USER=jordan", "LOGNAME=jordan", "PATH=/usr/local/bin:/usr/bin:/bin",
            "XDG_RUNTIME_DIR=/run/user/1000", "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus"]


class FakeRun:
    def __init__(self, rc=0, out="", err=""):
        self.calls = []
        self.result = sysexec.Result(rc, out, err)

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return self.result


class RunTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRun(0, "fine\n")
        for name, value in (("run", self.fake), ("which", lambda n: "/usr/bin/" + n)):
            p = mock.patch.object(sysexec, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_exact_argv(self):
        cb = [].append
        r = userexec.run(ctx(), ["bash", "/home/jordan/Documents/xess-xmx-linux-20261031/install.sh", "--yes"],
                         timeout=900, on_line=cb)
        self.assertEqual(r.out, "fine\n")
        argv, kw = self.fake.calls[0]
        self.assertEqual(argv, ["/usr/bin/runuser", "-u", "jordan", "--", "/usr/bin/env", "-i", *USER_ENV,
                                "bash", "/home/jordan/Documents/xess-xmx-linux-20261031/install.sh", "--yes"])
        self.assertEqual(kw, {"timeout": 900, "on_line": cb, "cwd": "/home/jordan"})

    def test_extra_variables_after_the_minimal_ones(self):
        userexec.run(ctx(), ["bash", "x.sh"], env_extra={"MESA": "26.3", "ODD": "a b=c"}, cwd="/tmp")
        argv, kw = self.fake.calls[0]
        self.assertEqual(argv[6:], [*USER_ENV, "MESA=26.3", "ODD=a b=c", "bash", "x.sh"])
        self.assertEqual(kw["cwd"], "/tmp")
        self.assertNotIn("env_extra", kw)     # nothing reaches runuser's own environment

    def test_refusals(self):
        for c, argv, extra in ((ctx(uid=0, user="root"), ["bash"], None),
                               (ctx(user=""), ["bash"], None),
                               (ctx(), ["A=1"], None),
                               (ctx(), [], None),
                               (ctx(), ["bash"], {"BAD-NAME": "1"}),
                               (ctx(), ["bash"], {"X\nY": "1"})):
            r = userexec.run(c, argv, env_extra=extra)
            self.assertEqual(r.rc, -1, (c.user, argv, extra))
            self.assertIn("userexec:", r.err)
        self.assertEqual(self.fake.calls, [])


class NoLeakTest(unittest.TestCase):
    """The real sysexec.run, with a runuser stand-in that only drops its options: the user's program sees exactly
    the minimal environment, none of Decky's or PyInstaller's variables."""

    def test_user_program_environment(self):
        if not os.access("/usr/bin/env", os.X_OK):
            self.skipTest("no /usr/bin/env")
        with tempfile.TemporaryDirectory() as d:
            runuser = os.path.join(d, "runuser")
            with open(runuser, "w") as f:
                f.write('#!/bin/sh\nwhile [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n')
            os.chmod(runuser, 0o755)
            leak = {"LD_LIBRARY_PATH": "/tmp/_MEIx", "PYTHONPATH": "/tmp/_MEIx", "_PYI_ARCHIVE_FILE": "x",
                    "DECKY_USER": "jordan", "DECKY_PLUGIN_DIR": "/x", "HOME": "/root", "USER": "root"}
            with mock.patch.dict(os.environ, leak), mock.patch.object(sysexec, "which", lambda n: runuser):
                r = userexec.run(ctx(home=d), ["env"], env_extra={"MESA": "26.1"})
        self.assertEqual(r.rc, 0, r.err)
        got = sorted(r.out.splitlines())
        want = sorted([f"HOME={d}", *USER_ENV[1:], "MESA=26.1"])
        self.assertEqual(got, want)


class WriteNewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.c = stub_decky.make_ctx(self.tmp.name)
        self.dir = os.path.join(self.c.home, ".config")
        os.makedirs(self.dir)

    def test_creates_with_mode_and_owner(self):
        p = os.path.join(self.dir, "a.conf")
        userexec.write_new(self.c, p, b"one", 0o600)
        userexec.write_new(self.c, p, b"two", 0o640)
        with open(p, "rb") as f:
            self.assertEqual(f.read(), b"two")
        st = os.lstat(p)
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o640)
        if ROOT:
            self.assertEqual((st.st_uid, st.st_gid), (1000, 1000))

    def test_planted_link_at_the_path_is_replaced(self):
        outside = os.path.join(self.tmp.name, "outside")
        with open(outside, "w") as f:
            f.write("keep")
        p = os.path.join(self.dir, "a.conf")
        os.symlink(outside, p)
        userexec.write_new(self.c, p, b"new")
        self.assertFalse(os.path.islink(p))
        with open(outside) as f:
            self.assertEqual(f.read(), "keep")

    def test_user_owned_link_in_the_folders_is_refused(self):
        target = os.path.join(self.tmp.name, "etc")
        os.mkdir(target)
        link = os.path.join(self.c.home, ".local")
        os.symlink(target, link)
        if ROOT:
            os.lchown(link, 1000, 1000)     # as the user would have made it
        with self.assertRaises(PermissionError):
            userexec.write_new(self.c, os.path.join(link, "x"), b"x")
        self.assertEqual(os.listdir(target), [])

    @unittest.skipUnless(ROOT, "needs a root-owned symlink in a root-owned folder")
    def test_root_owned_link_is_followed(self):
        real = os.path.join(self.tmp.name, "var-home")
        os.mkdir(real)
        os.symlink("var-home", os.path.join(self.tmp.name, "home2"))   # like /home -> var/home
        userexec.write_new(self.c, os.path.join(self.tmp.name, "home2", "f"), b"x")
        self.assertTrue(os.path.isfile(os.path.join(real, "f")))

    def test_missing_parent_and_directory(self):
        with self.assertRaises(FileNotFoundError):
            userexec.write_new(self.c, os.path.join(self.dir, "no", "x"), b"x")
        os.mkdir(os.path.join(self.dir, "d"))
        with self.assertRaises(OSError):
            userexec.write_new(self.c, os.path.join(self.dir, "d"), b"x")
        with self.assertRaises(OSError):
            userexec.open_dir("relative")


@unittest.skipUnless(ROOT, "chown needs root")
class ChownTreeTest(unittest.TestCase):
    def test_tree_and_links(self):
        with tempfile.TemporaryDirectory() as d:
            c = stub_decky.make_ctx(d, uid=4242, gid=4243)
            top = os.path.join(c.home, "pkg")
            os.makedirs(os.path.join(top, "lib", "mesa"))
            with open(os.path.join(top, "lib", "mesa", "x.so"), "w"):
                pass
            outside = os.path.join(d, "outside")
            with open(outside, "w"):
                pass
            os.symlink(outside, os.path.join(top, "lib", "link"))
            os.symlink(d, os.path.join(top, "dirlink"))
            userexec.chown_tree(c, top)
            for base, dirs, files in os.walk(top):
                for n in [base] + [os.path.join(base, x) for x in dirs + files]:
                    st = os.lstat(n)
                    self.assertEqual((st.st_uid, st.st_gid), (4242, 4243), n)
            self.assertEqual(os.lstat(outside).st_uid, 0)
            self.assertEqual(os.lstat(d).st_uid, 0)

    def test_single_file_and_link(self):
        with tempfile.TemporaryDirectory() as d:
            c = stub_decky.make_ctx(d, uid=4242, gid=4242)
            f = os.path.join(d, "f")
            with open(f, "w"):
                pass
            link = os.path.join(d, "l")
            os.symlink(f, link)
            userexec.chown_tree(c, link)
            self.assertEqual((os.lstat(link).st_uid, os.lstat(f).st_uid), (4242, 0))
            userexec.chown_tree(c, f)
            self.assertEqual(os.lstat(f).st_uid, 4242)


class UserMkdirsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.c = stub_decky.make_ctx(self.tmp.name)
        self.fake = FakeRun()
        for name, value in (("run", self.fake), ("which", lambda n: "/usr/bin/" + n)):
            p = mock.patch.object(sysexec, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_as_the_user(self):
        p = os.path.join(self.c.home, ".local", "share", "oxp3deck")
        userexec.user_mkdirs(self.c, p)
        argv, kw = self.fake.calls[0]
        self.assertEqual(argv[:6], ["/usr/bin/runuser", "-u", "jordan", "--", "/usr/bin/env", "-i"])
        self.assertEqual(argv[-4:], ["mkdir", "-p", "--", p])

    def test_existing_folder_needs_no_call(self):
        userexec.user_mkdirs(self.c, self.c.home)
        self.assertEqual(self.fake.calls, [])

    def test_outside_home_refused(self):
        for p in ("/etc/x", os.path.join(self.tmp.name, "root", "x"), self.c.home + "x/y",
                  os.path.join(self.c.home, "..", "..", "x")):
            with self.assertRaises(PermissionError, msg=p):
                userexec.user_mkdirs(self.c, p)
        os.symlink("/etc", os.path.join(self.c.home, "etc"))
        with self.assertRaises(PermissionError):
            userexec.user_mkdirs(self.c, os.path.join(self.c.home, "etc", "x"))
        self.assertEqual(self.fake.calls, [])

    def test_failure_raises(self):
        self.fake.result = sysexec.Result(1, "", "mkdir: cannot create directory: Permission denied\n")
        with self.assertRaises(OSError) as cm:
            userexec.user_mkdirs(self.c, os.path.join(self.c.home, "new"))
        self.assertIn("Permission denied", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
