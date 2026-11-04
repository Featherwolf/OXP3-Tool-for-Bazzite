import os
import unittest
from unittest import mock

from oxp3deck import env

DECKY_ENV = {
    "LD_LIBRARY_PATH": "/tmp/_MEIabc", "LD_LIBRARY_PATH_ORIG": "/opt/lib",
    "LD_PRELOAD": "/tmp/_MEIabc/libx.so",
    "PYTHONHOME": "/tmp/_MEIabc", "PYTHONPATH": "/tmp/_MEIabc/lib", "PYTHONPATH_ORIG": "",
    "_MEIPASS2": "/tmp/_MEIabc", "_PYI_APPLICATION_HOME_DIR": "/tmp/_MEIabc", "_PYI_PARENT_PROCESS_LEVEL": "1",
    "HOME": "/root", "DECKY_USER": "jordan",
}


class ScrubTest(unittest.TestCase):
    def test_restores_orig_and_drops_the_rest(self):
        with mock.patch.dict(os.environ, DECKY_ENV, clear=True):
            env.scrub_process_env()
            self.assertEqual(dict(os.environ), {"LD_LIBRARY_PATH": "/opt/lib", "LD_LIBRARY_PATH_ORIG": "/opt/lib",
                                                "PYTHONPATH_ORIG": "", "HOME": "/root", "DECKY_USER": "jordan"})

    def test_idempotent(self):
        with mock.patch.dict(os.environ, DECKY_ENV, clear=True):
            env.scrub_process_env()
            first = dict(os.environ)
            env.scrub_process_env()
            self.assertEqual(dict(os.environ), first)


class CleanEnvTest(unittest.TestCase):
    def test_drops_loader_variables_even_restored_ones(self):
        with mock.patch.dict(os.environ, DECKY_ENV, clear=True):
            env.scrub_process_env()
            e = env.clean_env()
        self.assertNotIn("LD_LIBRARY_PATH", e)
        for k in e:
            self.assertFalse(k.startswith(("LD_", "PYTHON", "_MEI", "_PYI_")), k)
        self.assertEqual(e["HOME"], "/root")

    def test_path_default_and_extra(self):
        with mock.patch.dict(os.environ, {"HOME": "/root"}, clear=True):
            e = env.clean_env({"LANG": "C", "N": 3})
            self.assertEqual(e["PATH"], env.SYSTEM_PATH)
            self.assertEqual(e["LANG"], "C")
            self.assertEqual(e["N"], "3")
            self.assertNotIn("LANG", os.environ)    # a copy
        with mock.patch.dict(os.environ, {"PATH": "/bin"}, clear=True):
            self.assertEqual(env.clean_env()["PATH"], "/bin")
            self.assertEqual(env.clean_env({"PATH": "/x"})["PATH"], "/x")

    def test_does_not_touch_os_environ(self):
        with mock.patch.dict(os.environ, DECKY_ENV, clear=True):
            env.clean_env()
            self.assertEqual(dict(os.environ), DECKY_ENV)


if __name__ == "__main__":
    unittest.main()
