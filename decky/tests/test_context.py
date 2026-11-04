import contextlib
import json
import os
import pwd
import tempfile
import threading
import time
import unittest

from oxp3deck import context
from oxp3deck.context import Ctx, Settings
from tests import stub_decky


class SysPathTest(unittest.TestCase):
    def ctx(self, root):
        return Ctx("jordan", 1000, 1000, "/home/jordan", "/s", "/r", "/l", "/p", root)

    def test_real_root_unchanged(self):
        self.assertEqual(self.ctx("/").sys_path("/sys/class/dmi/id/product_name"), "/sys/class/dmi/id/product_name")

    def test_fake_root(self):
        c = self.ctx("/tmp/fake/")
        self.assertEqual(c.sys_path("/etc/os-release"), "/tmp/fake/etc/os-release")
        self.assertEqual(c.sys_path("/run/oxp3-tool.lock"), "/tmp/fake/run/oxp3-tool.lock")

    def test_home_and_rerooted_paths_unchanged(self):
        c = self.ctx("/tmp/fake")
        self.assertEqual(c.sys_path("/home/jordan/.config/x"), "/home/jordan/.config/x")
        self.assertEqual(c.sys_path("/tmp/fake/etc/x"), "/tmp/fake/etc/x")
        self.assertEqual(c.sys_path("/home/jordanx"), "/tmp/fake/home/jordanx")


class FromDeckyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(stub_decky.uninstall)

    def test_from_stub(self):
        mod = stub_decky.install(self.tmp.name)
        c = context.from_decky()
        me = pwd.getpwuid(os.getuid())
        self.assertEqual((c.user, c.uid, c.gid), (me.pw_name, me.pw_uid, me.pw_gid))   # owner of the home
        self.assertEqual(c.home, os.path.realpath(mod.DECKY_USER_HOME))
        self.assertEqual(c.settings_dir, os.path.realpath(mod.DECKY_PLUGIN_SETTINGS_DIR))
        self.assertEqual(c.runtime_dir, os.path.realpath(mod.DECKY_PLUGIN_RUNTIME_DIR))
        self.assertEqual(c.log_dir, os.path.realpath(mod.DECKY_PLUGIN_LOG_DIR))
        self.assertEqual(c.plugin_dir, os.path.realpath(mod.DECKY_PLUGIN_DIR))
        self.assertEqual(c.root, "/")

    def test_linked_home_like_bazzite(self):
        real = os.path.realpath(self.tmp.name)
        os.makedirs(os.path.join(real, "var", "home"))
        os.symlink("var/home", os.path.join(real, "home"))      # /home -> var/home
        mod = stub_decky.install(real)                           # creates home/jordan through the link
        c = context.from_decky()
        self.assertEqual(c.home, os.path.join(real, "var", "home", "jordan"))
        self.assertEqual(c.settings_dir, os.path.join(c.home, "homebrew", "settings", "OXP3Deck"))
        self.assertTrue(mod.DECKY_USER_HOME.startswith(os.path.join(real, "home")))

    def test_missing_home(self):
        mod = stub_decky.install(self.tmp.name)
        mod.DECKY_USER_HOME = os.path.join(self.tmp.name, "nobody")
        with self.assertRaises(RuntimeError):
            context.from_decky()


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "settings.json")

    def test_roundtrip_and_persistence(self):
        s = Settings(self.path)
        self.assertIsNone(s.get("x"))
        self.assertEqual(s.get("x", 5), 5)
        self.assertIsNone(s.set("driver", "26.3"))
        self.assertIsNone(s.update({"a": [1, 2], "b": {"c": True}}))
        self.assertEqual(Settings(self.path).all(), {"driver": "26.3", "a": [1, 2], "b": {"c": True}})
        with open(self.path) as f:
            self.assertEqual(json.load(f)["driver"], "26.3")
        self.assertEqual(os.listdir(self.tmp.name), ["settings.json"])      # no temp files left

    def test_values_are_copies(self):
        s = Settings(self.path)
        v = {"k": [1]}
        s.set("v", v)
        v["k"].append(2)
        s.get("v")["k"].append(3)
        self.assertEqual(s.get("v"), {"k": [1]})

    def test_not_json(self):
        s = Settings(self.path)
        s.set("ok", 1)
        err = s.set("bad", {1, 2})
        self.assertIn("not JSON", err)
        self.assertEqual(s.all(), {"ok": 1})

    def test_corrupt_or_odd_file_starts_empty(self):
        for content in ("{not json", "[1, 2]", ""):
            with open(self.path, "w") as f:
                f.write(content)
            with self.assertLogs("oxp3deck", "WARNING") if content != "[1, 2]" else contextlib.nullcontext():
                self.assertEqual(Settings(self.path).all(), {})

    def test_fifo_does_not_hang(self):
        os.mkfifo(self.path)
        t = time.monotonic()
        self.assertEqual(Settings(self.path).all(), {})
        self.assertLess(time.monotonic() - t, 2)

    def test_link_at_path_is_replaced_not_followed(self):
        outside = os.path.join(self.tmp.name, "outside")
        with open(outside, "w") as f:
            f.write("keep")
        os.symlink(outside, self.path)
        s = Settings(self.path)
        self.assertEqual(s.all(), {})          # links are not read either
        self.assertIsNone(s.set("a", 1))
        self.assertFalse(os.path.islink(self.path))
        with open(outside) as f:
            self.assertEqual(f.read(), "keep")

    def test_linked_folder_refused(self):
        real = os.path.join(self.tmp.name, "real")
        os.mkdir(real)
        os.symlink(real, os.path.join(self.tmp.name, "link"))
        s = Settings(os.path.join(self.tmp.name, "link", "settings.json"))
        with self.assertLogs("oxp3deck", "WARNING"):
            err = s.set("a", 1)
        self.assertIn("cannot write", err)
        self.assertEqual(s.get("a"), 1)        # kept in memory
        self.assertEqual(os.listdir(real), [])

    def test_user_owned_link_above_the_folder_refused(self):
        # Decky gives ~/homebrew/settings/<dir> to the user: a link planted above it must not redirect root
        target = os.path.join(self.tmp.name, "etc")
        os.makedirs(os.path.join(target, "OXP3Deck"))
        link = os.path.join(self.tmp.name, "settings")
        os.symlink(target, link)
        if os.geteuid() == 0:
            os.lchown(link, 1000, 1000)     # as the user would have made it
        s = Settings(os.path.join(link, "OXP3Deck", "settings.json"))
        with self.assertLogs("oxp3deck", "WARNING"):
            self.assertIn("cannot write", s.set("a", 1))
        self.assertEqual(os.listdir(os.path.join(target, "OXP3Deck")), [])

    def test_threads(self):
        s = Settings(self.path)

        def worker(n):
            for i in range(20):
                s.set(f"k{n}", i)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(Settings(self.path).all(), {f"k{n}": 19 for n in range(5)})


class WriteAtomicTest(unittest.TestCase):
    def test_mode_and_content(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "f.json")
            context.write_atomic(p, b"1", 0o600)
            context.write_atomic(p, b"2", 0o640)
            with open(p, "rb") as f:
                self.assertEqual(f.read(), b"2")
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o640)
            self.assertEqual(os.listdir(d), ["f.json"])

    def test_read_small_limits(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "f")
            with open(p, "wb") as f:
                f.write(b"x" * 100)
            self.assertEqual(context.read_small(p), b"x" * 100)
            self.assertIsNone(context.read_small(p, 99))
            self.assertIsNone(context.read_small(d))
            self.assertIsNone(context.read_small(os.path.join(d, "missing")))


if __name__ == "__main__":
    unittest.main()
