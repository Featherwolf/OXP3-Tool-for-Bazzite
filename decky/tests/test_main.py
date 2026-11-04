# Adapted from ClawDeck (MIT, Copyright (c) 2026 IHulkSmashI and ClawDeck contributors).
"""main.py loaded the way Decky loads it (a stub `decky` module, then exec of the file), with fake feature modules:
every call answers a dict with ok, jobs start one at a time, settings are whitelisted, and the frontend's
src/api.ts names exactly the backend's methods."""
import asyncio
import importlib.util
import inspect
import json
import logging
import os
import re
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from oxp3deck import sysexec
from tests import stub_decky

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(PLUGIN, "main.py")
API_TS = os.path.join(PLUGIN, "src", "api.ts")

# One plausible call per public method; test_every_method_is_covered keeps it complete.
CALLS = {
    "get_overview": (), "oxp3_status": (), "oxp3_catalog": (), "oxp3_plan": (["buttons"], {"home": "guide"}),
    "oxp3_apply": (["buttons"], {}), "oxp3_revert": (["buttons"],), "oxp3_kernel": (),
    "oxp3_kernel_try": ("linux-cachyos-deckify-vsw",), "oxp3_kernel_keep": (), "oxp3_diagnose": (),
    "oxp3_update_check": (), "oxp3_install": ("bundled",), "xess_state": (), "xess_check_update": (),
    "xess_install": (), "xess_uninstall": (), "xess_set_driver": ("26.3",), "xess_shim_system_protons": (),
    "xess_remove_folder": ("/home/jordan/xess-old",), "game_info": ("1091500",), "job_status": (None,),
    "job_list": (), "reboot": (), "get_setting": ("ui_tab",), "set_setting": ("ui_tab", "fixes"),
}


def quiet(test):
    """The errors main.py logs on purpose here would fill the test output."""
    logger = logging.getLogger("oxp3deck")
    level = logger.level
    logger.setLevel(logging.CRITICAL)
    test.addCleanup(logger.setLevel, level)


def load_main(tmpdir):
    stub_decky.install(tmpdir)
    saved = dict(os.environ)
    try:
        spec = importlib.util.spec_from_file_location("oxp3deck_main_under_test", MAIN)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)   # main.py scrubs Decky's loader variables at import
    return mod


class Raising:
    """A feature module whose every function raises."""

    def __getattr__(self, name):
        def boom(*args, **kwargs):
            raise RuntimeError(f"boom in {name}")
        return boom


def call(plugin, name, *args):
    return asyncio.run(getattr(plugin, name)(*args))


def wait_idle(plugin, timeout=10):
    end = time.monotonic() + timeout
    while plugin.jobs.running() is not None and time.monotonic() < end:
        time.sleep(0.01)
    return plugin.jobs.running() is None


def public_methods(cls):
    return sorted(n for n, v in vars(cls).items() if not n.startswith("_") and inspect.iscoroutinefunction(v))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(stub_decky.uninstall)
        quiet(self)
        self.m = load_main(self.tmp.name)
        self.p = self.m.Plugin()
        self.assertIsNone(self.p._broken)
        self.p.ctx = stub_decky.make_ctx(self.tmp.name)   # uid 1000 and a fake root for /etc, /sys, /proc
        self.addCleanup(wait_idle, self.p)
        for name in ("oxp3tool", "xess", "games"):   # nothing real runs (runuser, oxp3-tool) unless a test says so
            setattr(self.m, name, Raising())

    def fake(self, name, **fns):
        mod = types.SimpleNamespace(**fns)
        setattr(self.m, name, mod)
        return mod


class AlwaysAnswersTest(Base):
    def test_every_method_is_covered(self):
        self.assertEqual(public_methods(self.m.Plugin), sorted(CALLS))

    def test_raising_modules_still_give_a_dict(self):
        for name, args in CALLS.items():
            res = call(self.p, name, *args)
            self.assertTrue(wait_idle(self.p))
            if name == "get_setting":
                self.assertIsNone(res)
                continue
            self.assertIsInstance(res, dict, name)
            self.assertIn("ok", res, name)
            json.dumps(res)
            if name == "get_overview":   # every part falls back to its default
                self.assertTrue(res["ok"])
                self.assertEqual(res["xess"]["line"], "XeSS state unknown: boom in summary")
                self.assertFalse(res["oxp3"]["installed"])
                continue
            if res.get("job"):           # no precheck: the job itself failed
                job = call(self.p, "job_status", res["job"]["id"])["job"]
                self.assertEqual(job["state"], "failed", name)
                self.assertIn("RuntimeError: boom in", job["error"])
                continue
            if name in ("job_status", "job_list", "set_setting"):
                self.assertTrue(res["ok"], name)
                continue
            self.assertFalse(res["ok"], name)
            self.assertTrue(res["error"].startswith(name + ": "), (name, res["error"]))

    def test_a_module_that_did_not_load(self):
        self.m.games = None
        self.m._BROKEN["games"] = "SyntaxError: invalid syntax (games.py, line 3)"
        res = call(self.p, "game_info", "1091500")
        self.assertEqual(res, {"ok": False, "error": "game_info: the games module did not load: SyntaxError: "
                                                     "invalid syntax (games.py, line 3)"})

    def test_bad_results_become_errors(self):
        self.fake("oxp3tool", status=lambda ctx: ["not", "a", "dict"])
        self.assertEqual(call(self.p, "oxp3_status"), {"ok": False, "error": "oxp3_status: returned list, not a dict"})
        self.fake("xess", state=lambda ctx, settings: {"ok": 1, "when": object()})
        res = call(self.p, "xess_state")
        self.assertIs(res["ok"], True)
        self.assertIsNone(res["error"])
        self.assertTrue(res["when"].startswith("<object object"))

    def test_work_runs_off_the_event_loop(self):
        seen = []
        self.fake("oxp3tool", status=lambda ctx: seen.append(threading.current_thread()) or {"ok": True,
                                                                                              "status": {}})
        call(self.p, "oxp3_status")
        self.assertIsNot(seen[0], threading.main_thread())


class BrokenStartTest(unittest.TestCase):
    def test_every_call_says_why(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(stub_decky.uninstall)
        quiet(self)
        m = load_main(tmp.name)
        m.decky.DECKY_USER_HOME = ""
        p = m.Plugin()
        self.assertIn("DECKY_USER_HOME is not set", p._broken)
        for name, args in CALLS.items():
            res = call(p, name, *args)
            if name == "get_setting":
                self.assertIsNone(res)
                continue
            self.assertFalse(res["ok"], name)
            self.assertIn("the plugin could not start: RuntimeError: context: DECKY_USER_HOME is not set",
                          res["error"])
        asyncio.run(p._main())       # logs and returns
        asyncio.run(p._unload())
        asyncio.run(p._uninstall())


class JobsTest(Base):
    def test_start_one_at_a_time_and_status(self):
        go = threading.Event()
        seen = {}

        def apply(ctx, job, fixes, opts):
            seen["args"] = (fixes, opts)
            job.say("applying")
            go.wait(10)
            return {"ok": True, "error": None, "outcome": {"reboot_needed": True}}

        self.fake("oxp3tool", apply_args=lambda ctx, fixes, opts: (["apply", *fixes], None), apply=apply,
                  check_fixes=lambda ctx, fixes, what: (fixes, None), rebuild_unfinished=lambda ctx: None,
                  installed=lambda ctx: dict(self.m.NO_OXP3))
        res = call(self.p, "oxp3_apply", ["buttons", "volume-keys"], {"home": "guide"})
        self.assertTrue(res["ok"], res)
        job = res["job"]
        self.assertEqual((job["kind"], job["title"], job["state"]), ("oxp3-apply", "Apply buttons, volume-keys",
                                                                     "running"))
        again = call(self.p, "oxp3_revert", ["buttons"])
        self.assertEqual(again, {"ok": False, "error": "another task is running: Apply buttons, volume-keys",
                                 "job": None})
        self.assertEqual(call(self.p, "get_overview")["job"]["id"], job["id"])
        self.assertIn("is running", call(self.p, "reboot")["error"])
        go.set()
        self.assertTrue(wait_idle(self.p))
        done = call(self.p, "job_status", job["id"])
        self.assertEqual((done["ok"], done["job"]["state"], done["job"]["log"]), (True, "done", ["applying"]))
        self.assertEqual(seen["args"], (["buttons", "volume-keys"], {"home": "guide"}))
        self.assertEqual(call(self.p, "job_status", None)["job"]["id"], job["id"])   # the newest
        self.assertEqual([j["id"] for j in call(self.p, "job_list")["jobs"]], [job["id"]])
        self.assertEqual(call(self.p, "job_status", "nope"), {"ok": False, "error": "job_status: no task nope",
                                                              "job": None})
        self.assertTrue(call(self.p, "get_overview")["reboot_pending"])   # the outcome asked for a reboot

    def test_a_revert_that_needs_a_reboot_is_noted(self):
        self.fake("oxp3tool", check_fixes=lambda ctx, fixes, what: (fixes, None), rebuild_unfinished=lambda ctx: None,
                  installed=lambda ctx: dict(self.m.NO_OXP3),
                  revert=lambda ctx, job, fixes: {"ok": True, "error": None, "outcome": {"reboot_needed": True}})
        self.assertFalse(call(self.p, "get_overview")["reboot_pending"])
        self.assertTrue(call(self.p, "oxp3_revert", ["nvme-resume"])["ok"])
        self.assertTrue(wait_idle(self.p))
        self.assertTrue(call(self.p, "get_overview")["reboot_pending"])

    def test_a_refused_precheck_starts_nothing(self):
        self.fake("oxp3tool", apply_args=lambda ctx, fixes, opts: (None, "apply: unknown fix nope"),
                  check_kernel_package=lambda pkg: "kernel try: '-x' is not a package name")
        self.assertEqual(call(self.p, "oxp3_apply", ["nope"], {}),
                         {"ok": False, "error": "apply: unknown fix nope", "job": None})
        self.assertFalse(call(self.p, "oxp3_kernel_try", "-x")["ok"])
        res = call(self.p, "oxp3_install", "somewhere")
        self.assertEqual(res["error"], "oxp3_install: source must be 'bundled' or 'online', not 'somewhere'")
        self.assertEqual(call(self.p, "job_list")["jobs"], [])

    def test_xess_calls_wait_for_an_xess_job(self):
        go = threading.Event()
        self.fake("xess", install=lambda ctx, settings, job: {"ok": go.wait(10)},
                  set_driver=lambda ctx, settings, prefix: {"ok": True, "drivers": []})
        self.assertTrue(call(self.p, "xess_install")["ok"])
        res = call(self.p, "xess_set_driver", "26.3")
        self.assertEqual(res["error"], "xess_set_driver: Install xess-xmx-linux is running; switch the driver when "
                                       "it has ended")
        go.set()
        self.assertTrue(wait_idle(self.p))
        self.assertTrue(call(self.p, "xess_set_driver", "26.3")["ok"])


class LifecycleTest(Base):
    def test_main_shims_the_system_protons_again(self):
        shimmed = threading.Event()

        def shim(ctx, settings, job):
            job.say("shimmed")
            shimmed.set()
            return {"ok": True, "error": None, "done": ["proton-cachyos-slr"], "failed": []}

        self.fake("xess", system_protons_need_shim=lambda ctx, settings: True, shim_system_protons=shim)
        asyncio.run(self.p._main())
        self.assertTrue(shimmed.wait(10))
        self.assertTrue(wait_idle(self.p))
        jobs = call(self.p, "job_list")["jobs"]
        self.assertEqual([(j["kind"], j["state"]) for j in jobs], [("xess-reshim", "done")])

    def test_main_carries_on_after_an_error(self):
        self.fake("xess", system_protons_need_shim=lambda ctx, settings: False)
        asyncio.run(self.p._main())
        self.m.xess = Raising()
        asyncio.run(self.p._main())
        self.assertEqual(call(self.p, "job_list")["jobs"], [])

    def test_unload_and_uninstall_change_nothing(self):
        with mock.patch.object(sysexec, "run", side_effect=AssertionError("ran a program")):
            self.assertIsNone(asyncio.run(self.p._unload()))
            self.assertIsNone(asyncio.run(self.p._uninstall()))


class OverviewTest(Base):
    def write(self, path, text):
        p = self.p.ctx.sys_path(path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def setUp(self):
        super().setUp()
        self.inst = {"installed": True, "path": "/usr/local/bin/oxp3-tool", "version": "0.4.0", "json_ok": True,
                     "min_version": "0.4.0", "bundled_version": "0.4.0"}
        self.marker = None
        self.fake("oxp3tool", installed=lambda ctx: self.inst, rebuild_unfinished=lambda ctx: self.marker,
                  status=lambda ctx: {"ok": True, "error": None, "status": {"reboot_pending": self.pending}})
        self.summary = {"installed": True, "version": "20261031", "status": "ok", "line": "ok 2026-11-01",
                        "live": True, "relogin_needed": False}
        self.fake("xess", summary=lambda ctx, settings: self.summary)
        self.pending = False

    def test_fields(self):
        self.write("/etc/os-release", 'NAME="CachyOS Linux"\nID=cachyos\nID_LIKE=arch\n')
        self.write("/sys/class/dmi/id/sys_vendor", "ONE-NETBOOK\n")
        self.write("/sys/class/dmi/id/product_name", "ONEXPLAYER 3\n")
        self.marker = "limine-mkinitcpio"
        res = call(self.p, "get_overview")
        self.assertEqual(res, {"ok": True, "error": None, "plugin_version": "0.1.0", "user": "jordan",
                               "home": self.p.ctx.home, "platform": "cachyos",
                               "device": {"vendor": "ONE-NETBOOK", "product": "ONEXPLAYER 3", "supported": True},
                               "oxp3": self.inst, "xess": self.summary, "job": None, "reboot_pending": False,
                               "do_not_reboot": "limine-mkinitcpio"})

    def test_platforms_and_devices(self):
        for text, want in (("ID=bazzite\nID_LIKE=\"fedora\"\n", "bazzite"), ("ID=custom\nID_LIKE=\"bazzite fedora\"\n",
                                                                            "bazzite"),
                           ("ID=fedora\n", None), ("ID=arch\n", None)):
            self.write("/etc/os-release", text)
            self.assertEqual(call(self.p, "get_overview")["platform"], want, text)
        self.write("/etc/pacman.conf", "[options]\n\n[cachyos-v3]\nInclude = x\n")
        self.assertEqual(call(self.p, "get_overview")["platform"], "cachyos")
        self.write("/sys/class/dmi/id/sys_vendor", "MSI\n")
        self.write("/sys/class/dmi/id/product_name", "Claw 8 AI+ A2VM\n")
        self.assertEqual(call(self.p, "get_overview")["device"],
                         {"vendor": "MSI", "product": "Claw 8 AI+ A2VM", "supported": False})

    def test_reboot_pending_comes_from_the_last_status_of_this_boot(self):
        self.write("/proc/sys/kernel/random/boot_id", "boot-1\n")
        self.pending = True
        self.assertTrue(call(self.p, "oxp3_status")["ok"])
        self.assertTrue(call(self.p, "get_overview")["reboot_pending"])
        self.pending = False
        call(self.p, "oxp3_status")
        self.assertFalse(call(self.p, "get_overview")["reboot_pending"])
        self.pending = True
        call(self.p, "oxp3_status")
        self.p._boot = None
        self.write("/proc/sys/kernel/random/boot_id", "boot-2\n")   # rebooted since
        self.assertFalse(call(self.p, "get_overview")["reboot_pending"])


class RebootTest(Base):
    def setUp(self):
        super().setUp()
        self.marker = None
        self.fake("oxp3tool", rebuild_unfinished=lambda ctx: self.marker)
        self.runs = []

        def run(argv, **kw):
            self.runs.append((argv, kw))
            return sysexec.Result(0, "", "")

        for name, value in (("run", run), ("which", lambda name: "/usr/bin/" + name)):
            p = mock.patch.object(sysexec, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_reboot(self):
        self.assertEqual(call(self.p, "reboot"), {"ok": True, "error": None})
        argv, kw = self.runs[0]
        self.assertEqual(argv, ["/usr/bin/systemctl", "reboot"])
        self.assertTrue(kw["start_new_session"])

    def test_not_while_a_rebuild_is_unfinished(self):
        self.marker = "limine-mkinitcpio"
        res = call(self.p, "reboot")
        self.assertFalse(res["ok"])
        self.assertIn("do not reboot", res["error"])
        self.assertIn("`sudo limine-mkinitcpio`", res["error"])
        self.assertEqual(self.runs, [])

    def test_systemctl_failure(self):
        failed = sysexec.Result(1, "", "Call to Reboot failed: Access denied\n")
        with mock.patch.object(sysexec, "run", lambda argv, **kw: failed):
            res = call(self.p, "reboot")
        self.assertEqual(res["error"], "reboot: systemctl reboot: Call to Reboot failed: Access denied")


class SettingsTest(Base):
    def test_whitelist(self):
        for key, value in (("xess_driver", "26.3"), ("xess_driver", None), ("xess_shim_system", True),
                           ("ui_tab", "fixes"), ("ui_open", {"kernel": False, "list": [1, 2]})):
            self.assertEqual(call(self.p, "set_setting", key, value), {"ok": True, "error": None}, key)
            self.assertEqual(call(self.p, "get_setting", key), value)
        for key, value, why in (("xess_package", "/tmp/x", "not a setting the frontend may use"),
                                ("ui_", 1, "not a setting"), ("ui_a-b", 1, "not a setting"), (7, 1, "not a setting"),
                                ("xess_shim_system", "yes", "xess_shim_system cannot be str"),
                                ("xess_driver", "../../x", "not a driver version"),
                                ("ui_big", "x" * 5000, "more than 4096 bytes"),
                                ("ui_nan", float("nan"), "not a JSON value"),
                                ("ui_obj", object(), "not a JSON value")):
            res = call(self.p, "set_setting", key, value)
            self.assertFalse(res["ok"], key)
            self.assertIn(why, res["error"])
        self.assertIsNone(call(self.p, "get_setting", "xess_package"))
        with open(os.path.join(self.p.ctx.settings_dir, "settings.json")) as f:
            self.assertEqual(json.load(f)["ui_tab"], "fixes")   # Decky's copy of the plugin settings folder

    def test_ui_keys_are_limited(self):
        for i in range(self.m.MAX_UI_KEYS):
            self.assertTrue(call(self.p, "set_setting", f"ui_k{i}", i)["ok"])
        self.assertIn("more than", call(self.p, "set_setting", "ui_one_more", 1)["error"])
        self.assertTrue(call(self.p, "set_setting", "ui_k0", "changed")["ok"])   # an existing one may change


class WiringTest(unittest.TestCase):
    def test_api_ts_and_main_name_the_same_methods(self):
        if not os.path.isfile(API_TS):
            self.skipTest("src/api.ts does not exist yet (the frontend is written separately)")
        with open(API_TS) as f:
            names = re.findall(r'callable<.*?>\(\s*"([A-Za-z0-9_]+)"\s*\)', f.read())
        with open(MAIN) as f:
            main = f.read()
        self.assertTrue(names)
        for name in names:
            self.assertIn(f"async def {name}(self", main, f"api.ts calls {name}, main.py has no such method")
        public = re.findall(r"^    async def ([a-z][A-Za-z0-9_]*)\(self", main, re.M)
        self.assertEqual(sorted(public), sorted(set(names)), "every public backend method is in api.ts")
        self.assertEqual(len(names), len(set(names)))

    def test_main_calls_the_real_modules_with_their_signatures(self):
        """Every `_mod("name").fn(...)` (and `x.fn(...)` after `x = _mod("xess")`) in main.py names a function the
        real module has, with as many arguments as it takes: the other tests use fake modules."""
        import importlib
        with open(MAIN) as f:
            main = f.read()
        calls = re.findall(r'_mod\("(\w+)"\)\.(\w+)\(([^()]*)\)', main)
        calls += [("xess", fn, args) for fn, args in re.findall(r"\bx\.(\w+)\(([^()]*)\)", main)]
        self.assertGreater(len(calls), 25)
        for modname, fn, args in calls:
            mod = importlib.import_module("oxp3deck." + modname)
            self.assertTrue(hasattr(mod, fn), f"main.py calls {modname}.{fn}, which does not exist")
            n = len([a for a in args.split(",") if a.strip()])
            try:
                inspect.signature(getattr(mod, fn)).bind(*([None] * n))
            except TypeError as e:
                self.fail(f"main.py calls {modname}.{fn} with {n} arguments: {e}")


if __name__ == "__main__":
    unittest.main()
