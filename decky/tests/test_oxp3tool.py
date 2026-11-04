"""oxp3tool.py against a fake oxp3-tool: a small Python file in a fake root that prints fixture JSON
(tests/fixtures/oxp3, real output of this repository's CLI; tests/make_fixtures.py regenerates them)."""
import hashlib
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock

from oxp3deck import context, downloads, oxp3tool, sysexec
from tests import stub_decky

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "oxp3")
ROOT = os.geteuid() == 0
VSW = "linux-cachyos-deckify-vsw"

# The fake: --version prints VERSION; anything else is recorded in fake-calls.jsonl and answered from
# fake-answers.json next to it, keyed by the command ("status", "kernel try", "apply --dry-run", ...).
FAKE_TOOL = r'''#!/usr/bin/python3 -I
import json, os, sys, time
args = sys.argv[1:]
if args == ["--version"]:
    print("@VERSION@")
    sys.exit(0)
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "fake-calls.jsonl"), "a") as f:
    f.write(json.dumps(args) + "\n")
with open(os.path.join(here, "fake-answers.json")) as f:
    answers = json.load(f)
key = args[0]
if key == "kernel" and len(args) > 1 and args[1] in ("try", "keep"):
    key += " " + args[1]
if "--dry-run" in args:
    key += " --dry-run"
a = answers.get(key) or answers.get("*") or {"stderr": "no answer for " + key, "rc": 9}
time.sleep(a.get("sleep", 0))
if "write" in a:
    with open(args[args.index("-o") + 1], "wb") as f:
        f.write(a["write"].encode())
sys.stdout.write(a.get("stdout", ""))
sys.stderr.write(a.get("stderr", ""))
sys.exit(a.get("rc", 0))
'''


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return f.read()


def fake_tool(version="0.4.1"):
    return FAKE_TOOL.replace("@VERSION@", version if " " in version or not version[:1].isdigit()
                             else "oxp3-tool " + version).encode()


class FakeJob:
    def __init__(self):
        self.log = []
        self.lock = threading.Lock()

    def say(self, line):
        with self.lock:
            self.log.extend(str(line).splitlines() or [""])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = stub_decky.make_ctx(self.tmp.name)
        self.bindir = self.ctx.sys_path("/usr/local/bin")
        os.makedirs(self.bindir)
        self.settings = context.Settings(os.path.join(self.ctx.settings_dir, "settings.json"))
        for name, value in (("PYTHON", sys.executable), ("HEARTBEAT", 30.0), ("READ_WAIT", 5.0)):
            p = mock.patch.object(oxp3tool, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(sysexec, "which", lambda name: None)   # no restorecon unless a test adds one
        p.start()
        self.addCleanup(p.stop)
        oxp3tool._versions.clear()
        oxp3tool._catalog.clear()
        self.answers = {"list": {"stdout": fixture("list.json")}}
        self.install_tool()

    def install_tool(self, version="0.4.1", path=None):
        path = path or os.path.join(self.bindir, "oxp3-tool")
        with open(path, "wb") as f:
            f.write(fake_tool(version))
        os.chmod(path, 0o755)
        oxp3tool._versions.clear()
        return path

    def answer(self, key, stdout="", stderr="", rc=0, **kw):
        self.answers[key] = {"stdout": stdout, "stderr": stderr, "rc": rc, **kw}

    def save_answers(self):
        with open(os.path.join(self.bindir, "fake-answers.json"), "w") as f:
            json.dump(self.answers, f)

    def calls(self):
        try:
            with open(os.path.join(self.bindir, "fake-calls.jsonl")) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def run_cmd(self, fn, *args):
        self.save_answers()
        return fn(self.ctx, *args)


class FindAndVersionTest(Base):
    def test_installed(self):
        inst = oxp3tool.installed(self.ctx)
        self.assertEqual(inst, {"installed": True, "path": os.path.join(self.bindir, "oxp3-tool"), "version": "0.4.1",
                                "json_ok": True, "min_version": "0.4.1", "bundled_version": None})

    def test_version_is_cached_until_the_file_changes(self):
        real = sysexec.run
        seen = []
        with mock.patch.object(sysexec, "run", lambda argv, **kw: seen.append(argv) or real(argv, **kw)):
            oxp3tool.installed(self.ctx)
            oxp3tool.installed(self.ctx)
            self.assertEqual(len(seen), 1)
            path = os.path.join(self.bindir, "oxp3-tool")
            with open(path, "wb") as f:           # an update: same size, new mtime
                f.write(fake_tool("0.4.2"))
            os.utime(path, ns=(1, 1))
            self.assertEqual(oxp3tool.installed(self.ctx)["version"], "0.4.2")
            self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], [sys.executable, "-I", path, "--version"])

    def test_too_old_and_bundled(self):
        self.install_tool("0.3.9")
        bundled = os.path.join(self.ctx.plugin_dir, "bin", "oxp3-tool")
        os.makedirs(os.path.dirname(bundled))
        self.install_tool("0.4.1", bundled)
        os.chmod(bundled, 0o644)   # the zip's copy needs no x bit: it runs through python3
        inst = oxp3tool.installed(self.ctx)
        self.assertEqual((inst["version"], inst["json_ok"], inst["bundled_version"]), ("0.3.9", False, "0.4.1"))
        r = self.run_cmd(oxp3tool.status)
        self.assertFalse(r["ok"])
        self.assertIn("needs 0.4.1 or newer", r["error"])
        self.assertEqual(self.calls(), [])

    def test_the_early_040_build_is_too_old_and_the_bundled_copy_is_offered(self):
        # OXP-1: commit 7fbcb93's 0.4.0 has --json but none of button_keys, min_refresh.default, fix pending,
        # reboot_pending; the plugin must not run it, and must offer its own copy although the online check (in that
        # window) also said 0.4.0.
        self.install_tool("0.4.0")
        bundled = os.path.join(self.ctx.plugin_dir, "bin", "oxp3-tool")
        os.makedirs(os.path.dirname(bundled))
        self.install_tool("0.4.1", bundled)
        inst = oxp3tool.installed(self.ctx)
        self.assertEqual((inst["version"], inst["json_ok"], inst["min_version"], inst["bundled_version"]),
                         ("0.4.0", False, "0.4.1", "0.4.1"))
        for fn in (oxp3tool.status, oxp3tool.catalog):
            r = self.run_cmd(fn)
            self.assertFalse(r["ok"])
            self.assertIn("version 0.4.0 is installed; OXP3 Deck needs 0.4.1 or newer", r["error"])
        self.assertEqual(self.calls(), [])

        def get_text(url, timeout=30, max_bytes=2_000_000):
            return json.dumps([{"sha": "c" * 40}]) if url == oxp3tool.COMMITS_API else '__version__ = "0.4.0"\n'

        with mock.patch.object(downloads, "get_text", get_text):
            r = oxp3tool.update_check(self.ctx, self.settings)
        self.assertEqual((r["ok"], r["installed"], r["bundled"], r["online"], r["update"]),
                         (True, "0.4.0", "0.4.1", "0.4.0", "bundled"))

    def test_find_order_and_trust(self):
        local = os.path.join(self.bindir, "oxp3-tool")
        usr = self.ctx.sys_path("/usr/bin/oxp3-tool")
        os.makedirs(os.path.dirname(usr))
        self.install_tool("0.4.0", usr)
        self.assertEqual(oxp3tool.find(self.ctx), local)
        os.chmod(local, 0o644)                    # not executable
        self.assertEqual(oxp3tool.find(self.ctx), usr)
        os.chmod(usr, 0o757)                      # writable by anyone: root does not run it
        self.assertIsNone(oxp3tool.find(self.ctx))
        self.assertFalse(oxp3tool.installed(self.ctx)["installed"])
        r = self.run_cmd(oxp3tool.status)
        self.assertIn("not installed", r["error"])


class RunTest(Base):
    def test_status_catalog_kernel_keep_the_cli_objects(self):
        self.answer("status", fixture("status-cachyos.json"))
        self.answer("kernel", fixture("kernel.json"))
        r = self.run_cmd(oxp3tool.status)
        self.assertEqual(r, {"ok": True, "error": None,
                             "status": json.loads(fixture("status-cachyos.json"))["status"]})
        cat = self.run_cmd(oxp3tool.catalog)
        self.assertEqual(cat["catalog"], json.loads(fixture("list.json"))["catalog"])
        self.assertTrue(cat["ok"])
        k = self.run_cmd(oxp3tool.kernel)
        self.assertEqual(k["kernel"]["next_boot"], {"package": "linux-cachyos-deckify", "then": None,
                                                    "why": "remembered"})
        self.assertEqual(self.calls(), [["status", "--json"], ["list", "--json"], ["kernel", "--json"]])
        oxp3tool.catalog(self.ctx)                # cached while the binary stays the same
        self.assertEqual(len(self.calls()), 3)

    def test_kernel_elsewhere_is_oxp3_tools_error(self):
        self.answer("kernel", fixture("kernel-bazzite.json"), rc=1)
        k = self.run_cmd(oxp3tool.kernel)
        why = "oxp3-tool kernel: `oxp3-tool kernel` is for CachyOS booting with Limine (and limine-entry-tool)"
        self.assertEqual(k, {"ok": False, "kernel": None, "error": why})

    def test_no_json(self):
        self.answer("status", "not json\n", "Traceback (most recent call last):\n  File x\nValueError: boom\n", rc=1)
        r = self.run_cmd(oxp3tool.status)
        self.assertFalse(r["ok"])
        self.assertIsNone(r["status"])
        self.assertTrue(r["error"].startswith("oxp3-tool status exited 1: "), r["error"])
        self.assertTrue(r["error"].endswith("ValueError: boom"), r["error"])
        self.answer("status", "", "", rc=3)
        self.assertEqual(self.run_cmd(oxp3tool.status)["error"], "oxp3-tool status exited 3: no output")

    def test_last_json_line_and_schema(self):
        one_line = json.dumps(json.loads(fixture("status-bazzite.json")))
        self.answer("status", "a stray line\n" + one_line + "\n")
        self.assertTrue(self.run_cmd(oxp3tool.status)["ok"])
        obj = json.loads(one_line)
        obj["schema"] = 2
        self.answer("status", json.dumps(obj))
        r = self.run_cmd(oxp3tool.status)
        self.assertFalse(r["ok"])
        self.assertIn("schema 2", r["error"])

    def test_usage_error_and_unknown_fix_messages(self):
        self.answer("apply", fixture("usage-error.json"), rc=2)
        r = self.run_cmd(oxp3tool.apply, FakeJob(), ["buttons"], {})
        self.assertEqual(r["rc"], 2)
        self.assertIn("invalid choice: 'nope'", r["error"])
        self.answer("apply", fixture("apply-unknown-fix.json"), rc=1)
        r = self.run_cmd(oxp3tool.apply, FakeJob(), ["buttons"], {})
        self.assertEqual(r["error"], "oxp3-tool apply: unknown fix: no-such-fix (see `oxp3-tool list`)")

    def test_output_goes_to_files_in_a_private_folder(self):
        seen = []
        real = sysexec.run

        def spy(argv, **kw):
            seen.append((argv, kw))
            return real(argv, **kw)

        self.answer("status", fixture("status-cachyos.json"))
        with mock.patch.object(sysexec, "run", spy):
            self.assertTrue(self.run_cmd(oxp3tool.status)["ok"])
        argv, kw = seen[-1]
        self.assertEqual(argv[:2], [oxp3tool.SHELL, "-c"])
        run_dir = self.ctx.sys_path("/run/oxp3deck")
        self.assertTrue(argv[4].startswith(run_dir + "/status-") and argv[4].endswith(".out"))
        self.assertEqual(argv[6:], [sys.executable, "-I", os.path.join(self.bindir, "oxp3-tool"), "status", "--json"])
        self.assertTrue(kw["start_new_session"])
        self.assertIsNone(kw.get("timeout"))      # never killed
        self.assertEqual(kw["env_extra"], {"SUDO_UID": "1000", "SUDO_GID": "1000"})
        self.assertEqual(stat.S_IMODE(os.lstat(run_dir).st_mode), 0o700)
        self.assertEqual(os.listdir(run_dir), [])   # read and removed

    def test_run_folder_must_be_a_real_private_folder(self):
        run_dir = self.ctx.sys_path("/run/oxp3deck")
        os.makedirs(os.path.dirname(run_dir))
        elsewhere = os.path.join(self.tmp.name, "elsewhere")
        os.mkdir(elsewhere)
        os.symlink(elsewhere, run_dir)
        self.answer("status", fixture("status-cachyos.json"))
        r = self.run_cmd(oxp3tool.status)
        self.assertFalse(r["ok"])
        self.assertIn("not a folder", r["error"])
        self.assertEqual((self.calls(), os.listdir(elsewhere)), ([], []))
        os.unlink(run_dir)
        os.mkdir(run_dir, 0o755)
        self.assertTrue(self.run_cmd(oxp3tool.status)["ok"])
        self.assertEqual(stat.S_IMODE(os.lstat(run_dir).st_mode), 0o700)

    @unittest.skipUnless(ROOT, "needs root to give the folder to someone else")
    def test_run_folder_of_another_user_is_refused(self):
        run_dir = self.ctx.sys_path("/run/oxp3deck")
        os.makedirs(run_dir, 0o700)
        os.chown(run_dir, 1000, 1000)
        r = self.run_cmd(oxp3tool.status)
        self.assertIn("belongs to uid 1000", r["error"])
        self.assertEqual(self.calls(), [])

    def test_job_gets_heartbeats_and_the_log(self):
        self.answer("apply", fixture("apply-nvme.json"), sleep=0.4)
        job = FakeJob()
        with mock.patch.object(oxp3tool, "HEARTBEAT", 0.1):
            r = self.run_cmd(oxp3tool.apply, job, ["nvme-resume"], {"any_ssd": True})
        self.assertTrue(r["ok"])
        self.assertTrue(r["outcome"]["reboot_needed"])
        self.assertEqual(job.log[0], "oxp3-tool apply nvme-resume --any-ssd --yes")
        self.assertTrue(any("is still running" in line for line in job.log), job.log)
        self.assertEqual(job.log[-2:], ["Reboot to finish (kernel arguments change at boot).", "Done."])

    def test_reads_give_way_to_a_jobs_run(self):
        self.answer("status", fixture("status-cachyos.json"))
        self.save_answers()
        with oxp3tool.LOCK:
            oxp3tool._holder.update(cmd="apply", job=True)
            try:
                r = oxp3tool.status(self.ctx)
            finally:
                oxp3tool._holder.update(cmd=None, job=False)
            self.assertIn("busy: oxp3-tool apply is running", r["error"])
            with mock.patch.object(oxp3tool, "READ_WAIT", 0.05):
                self.assertIn("busy", oxp3tool.status(self.ctx)["error"])
        self.assertEqual(self.calls(), [])


class ArgvTest(Base):
    def setUp(self):
        super().setUp()
        self.answer("apply", fixture("apply-nvme.json"))
        self.answer("apply --dry-run", fixture("apply-dry-run.json"))
        self.answer("revert", fixture("revert-nvme.json"))
        self.answer("kernel", fixture("kernel.json"))
        self.answer("kernel try", fixture("kernel-try.json"))
        self.answer("kernel keep", fixture("kernel-keep.json"))

    def test_apply_flags_then_yes_then_json(self):
        r = self.run_cmd(oxp3tool.apply, FakeJob(), ["buttons", "volume-keys", "buttons"],
                         {"home": "guide", "keyboard_key": "keyboard", "onex_key": "quick-access",
                          "xbox_key": "fps-limit", "fps_limit": 60, "min_refresh": 40.0, "any_ssd": False,
                          "no_restart": True})
        self.assertTrue(r["ok"])
        self.assertEqual(self.calls()[-1], ["apply", "buttons", "volume-keys", "--home", "guide", "--keyboard-key",
                                            "keyboard", "--onex-key", "quick-access", "--xbox-key", "fps-limit",
                                            "--fps-limit", "60", "--min-refresh", "40", "--no-restart", "--yes",
                                            "--json"])
        self.assertEqual(set(r), {"ok", "error", "rc", "version", "plan", "outcome"})

    def test_plan(self):
        r = self.run_cmd(oxp3tool.plan, ["buttons", "volume-keys"], {"home": "guide", "fps_limit": 60})
        want = json.loads(fixture("apply-dry-run.json"))
        self.assertEqual(r, {"ok": True, "error": None, "plan": want["plan"],
                             "log": [e["text"] for e in want["log"]]})
        self.assertEqual(self.calls()[-1], ["apply", "buttons", "volume-keys", "--home", "guide", "--fps-limit", "60",
                                            "--dry-run", "--json"])

    def test_revert_and_kernel(self):
        self.assertTrue(self.run_cmd(oxp3tool.revert, FakeJob(), ["nvme-resume"])["ok"])
        r = self.run_cmd(oxp3tool.kernel_try, FakeJob(), VSW)
        self.assertTrue(r["ok"])
        self.assertTrue(r["kernel"]["trying"])
        self.assertEqual(r["action"], "try")
        self.assertTrue(self.run_cmd(oxp3tool.kernel_keep, FakeJob())["ok"])
        self.assertEqual(self.calls()[1:], [["revert", "nvme-resume", "--yes", "--json"], ["kernel", "--json"],
                                            ["kernel", "try", VSW, "--yes", "--json"],
                                            ["kernel", "keep", "--yes", "--json"]])

    def test_diagnose_ends_in_the_home_as_the_users_file(self):
        self.answer("diagnose", fixture("diagnose.json"), write="ARCHIVE")
        job = FakeJob()
        r = self.run_cmd(oxp3tool.diagnose, job)
        self.assertTrue(r["ok"], r)
        argv = self.calls()[-1]
        run_dir = self.ctx.sys_path("/run/oxp3deck")
        self.assertEqual((argv[:2], argv[3]), (["diagnose", "-o"], "--json"))
        self.assertTrue(argv[2].startswith(run_dir + "/oxp3-diagnose-"))
        self.assertEqual(os.path.dirname(r["path"]), self.ctx.home)
        self.assertRegex(os.path.basename(r["path"]), r"^oxp3-diagnose-\d{8}-\d{6}\.tar\.gz$")
        with open(r["path"]) as f:
            self.assertEqual(f.read(), "ARCHIVE")
        st = os.lstat(r["path"])
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        if ROOT:
            self.assertEqual((st.st_uid, st.st_gid), (1000, 1000))
        self.assertEqual((os.listdir(run_dir), r["findings"]), ([], []))
        self.assertEqual(job.log[-1], f"Saved {r['path']}")
        self.assertEqual(oxp3tool._free_name(self.ctx.home, os.path.basename(r["path"])[:-7], ".tar.gz"),
                         r["path"][:-7] + "-2.tar.gz")


class ValidationTest(Base):
    def setUp(self):
        super().setUp()
        self.answer("*", fixture("apply-nvme.json"))
        self.answer("kernel", fixture("kernel.json"))

    def refused(self, fn, *args):
        r = self.run_cmd(fn, *args)
        self.assertFalse(r["ok"])
        self.assertFalse([c for c in self.calls() if c[0] not in ("list", "kernel") or c[1:2] in (["try"],)],
                         "nothing may run")
        return r["error"]

    def test_fixes(self):
        self.assertIn("name at least one fix", self.refused(oxp3tool.apply, FakeJob(), [], {}))
        self.assertIn("name at least one fix", self.refused(oxp3tool.plan, "buttons", {}))
        self.assertIn("unknown fix nope", self.refused(oxp3tool.apply, FakeJob(), ["buttons", "nope"], {}))
        self.assertIn("lowercase", self.refused(oxp3tool.revert, FakeJob(), ["--all"]))
        self.assertIn("lowercase", self.refused(oxp3tool.revert, FakeJob(), [7]))

    def test_options(self):
        for opts, why in (({"home": "nope"}, "home must be one of fps-limit, guide"),
                          ({"xbox_key": 3}, "xbox_key must be one of"),
                          ({"fps_limit": 5}, "fps_limit must be 10 to 144, not 5"),
                          ({"fps_limit": 145}, "fps_limit must be 10 to 144"),
                          ({"fps_limit": True}, "fps_limit must be a whole number"),
                          ({"fps_limit": "60"}, "fps_limit must be a whole number"),
                          ({"min_refresh": 49}, "min_refresh must be 30 to 48, not 49"),
                          ({"min_refresh": 40.5}, "min_refresh must be a whole number"),
                          ({"any_ssd": "yes"}, "any_ssd must be true or false"),
                          ({"defaults": True}, "unknown option defaults"),
                          ("--yes", "options must be an object")):
            self.assertIn(why, self.refused(oxp3tool.apply, FakeJob(), ["buttons"], opts), opts)

    def test_catalog_flag_names_must_match_the_parser(self):
        cat = json.loads(fixture("list.json"))
        cat["catalog"]["button_keys"]["home"]["flag"] = "--home-key"
        self.answer("list", json.dumps(cat))
        self.assertIn("'--home-key', not '--home'", self.refused(oxp3tool.apply, FakeJob(), ["buttons"],
                                                                 {"home": "guide"}))

    def test_kernel_package(self):
        for pkg in ("-rf", "a b", "", None, "x;y", "a" * 200):
            self.assertIn("not a package name", self.refused(oxp3tool.kernel_try, FakeJob(), pkg))
        self.assertIn("linux-zen is not an installed kernel (installed: linux-cachyos-deckify, "
                      f"{VSW})", self.refused(oxp3tool.kernel_try, FakeJob(), "linux-zen"))
        self.assertEqual(self.calls(), [["kernel", "--json"]])


class InstallTest(Base):
    def setUp(self):
        super().setUp()
        self.dest = os.path.join(self.bindir, "oxp3-tool")
        self.install_tool("0.4.0")   # the early 0.4.0 build (too old, OXP-1) that the bundled 0.4.1 replaces
        self.bundled = os.path.join(self.ctx.plugin_dir, "bin", "oxp3-tool")
        os.makedirs(os.path.dirname(self.bundled))
        self.put_bundled("0.4.1")

    def put_bundled(self, version, digest=None):
        data = fake_tool(version)
        with open(self.bundled, "wb") as f:
            f.write(data)
        os.chmod(self.bundled, 0o644)
        with open(self.bundled + ".sha256", "w") as f:
            f.write(f"{digest or hashlib.sha256(data).hexdigest()}  oxp3-tool\n")
        oxp3tool._versions.clear()
        return data

    def read_dest(self):
        with open(self.dest, "rb") as f:
            return f.read()

    def install(self, source="bundled"):
        job = FakeJob()
        return oxp3tool.install(self.ctx, self.settings, job, source), job

    def test_bundled(self):
        old_lib = self.ctx.sys_path("/usr/local/lib/oxp3-tool")
        os.makedirs(old_lib)
        open(os.path.join(old_lib, "oxp3-tool"), "w").close()
        hook = os.path.join(self.tmp.name, "restorecon")
        with open(hook, "w") as f:
            f.write(f'#!/bin/sh\necho "$@" > {self.tmp.name}/restorecon.args\n')
        os.chmod(hook, 0o755)
        self.assertEqual(oxp3tool.installed(self.ctx)["version"], "0.4.0")
        with mock.patch.object(sysexec, "which", lambda name: hook if name == "restorecon" else None):
            r, job = self.install()
        self.assertEqual(r, {"ok": True, "error": None, "version": "0.4.1", "path": self.dest, "source": "bundled",
                             "commit": None})
        self.assertEqual(self.read_dest(), fake_tool("0.4.1"))
        self.assertEqual(stat.S_IMODE(os.stat(self.dest).st_mode), 0o755)
        self.assertEqual(sorted(os.listdir(self.bindir)), ["oxp3-tool"])
        self.assertFalse(os.path.lexists(old_lib))
        self.assertEqual(os.listdir(self.ctx.sys_path("/run/oxp3deck")), [])
        with open(os.path.join(self.tmp.name, "restorecon.args")) as f:
            self.assertEqual(f.read().strip(), self.dest)
        self.assertEqual(oxp3tool.installed(self.ctx)["version"], "0.4.1")
        self.assertEqual(job.log[-1], f"Installed oxp3-tool 0.4.1 to {self.dest}")

    def test_checksum_mismatch_is_refused(self):
        self.put_bundled("0.4.1", digest="0" * 64)
        r, _ = self.install()
        self.assertFalse(r["ok"])
        self.assertIn("bin/oxp3-tool.sha256 says " + "0" * 64, r["error"])
        self.assertEqual(self.read_dest(), fake_tool("0.4.0"))

    def test_version_gate(self):
        self.install_tool("0.3.0")   # differs from every refused copy below, so a write would show
        for version, why in (("0.3.9", "it is oxp3-tool 0.3.9; OXP3 Deck needs 0.4.1 or newer"),
                             ("0.4.0", "it is oxp3-tool 0.4.0; OXP3 Deck needs 0.4.1 or newer"),   # OXP-1
                             ("hello world", "the file is not oxp3-tool: hello world")):
            self.put_bundled(version)
            r, _ = self.install()
            self.assertEqual(r["error"], "install: " + why)
            self.assertEqual(self.read_dest(), fake_tool("0.3.0"))
        r, _ = self.install("elsewhere")
        self.assertIn("source must be 'bundled' or 'online'", r["error"])

    def test_a_planted_link_is_replaced_not_followed(self):
        victim = os.path.join(self.tmp.name, "victim")
        with open(victim, "w") as f:
            f.write("keep me")
        os.symlink(victim, os.path.join(self.bindir, ".oxp3-tool.new"))
        os.unlink(self.dest)
        os.symlink(victim, self.dest)   # an old install's link
        r, _ = self.install()
        self.assertTrue(r["ok"], r)
        with open(victim) as f:
            self.assertEqual(f.read(), "keep me")
        self.assertFalse(os.path.islink(self.dest))
        self.assertEqual(self.read_dest(), fake_tool("0.4.1"))

    def test_a_failed_write_leaves_the_old_binary(self):
        real = os.replace

        def broken(src, dst, **kw):
            if str(dst) == "oxp3-tool":
                raise OSError(28, "No space left on device")
            return real(src, dst, **kw)

        with mock.patch.object(oxp3tool.os, "replace", broken):
            r, _ = self.install()
        self.assertIn("No space left on device", r["error"])
        self.assertEqual(self.read_dest(), fake_tool("0.4.0"))
        self.assertEqual(sorted(os.listdir(self.bindir)), ["oxp3-tool"])

    def online(self, data, commit="a" * 40, digest=None, api_down=False):
        urls = []
        digest = digest or hashlib.sha256(data).hexdigest()

        def get_text(url, timeout=30, max_bytes=2_000_000):
            urls.append(url)
            if url == oxp3tool.COMMITS_API:
                if api_down:
                    raise downloads.DownloadError("HTTP 403 (GitHub's rate limit? try again in an hour)")
                return json.dumps([{"sha": commit}])
            if url.endswith("/dist/oxp3-tool.sha256"):
                return f"{digest}  oxp3-tool\n"
            if url.endswith("/oxp3_tool/__init__.py"):
                return '"""oxp3-tool"""\n\n__version__ = "0.5.0"\n'
            raise downloads.DownloadError(f"{url}: HTTP 404 Not Found")

        def download(url, dest, *, expected_sha256=None, max_bytes, on_progress=None):
            urls.append(url)
            got = hashlib.sha256(data).hexdigest()
            if expected_sha256 and got != expected_sha256:
                raise downloads.DownloadError(f"download oxp3-tool: sha256 is {got}, expected {expected_sha256}")
            with open(dest, "wb") as f:
                f.write(data)
            return got

        for name, fn in (("get_text", get_text), ("download", download)):
            p = mock.patch.object(downloads, name, fn)
            p.start()
            self.addCleanup(p.stop)
        return urls

    def test_online_pins_both_files_to_one_commit(self):
        urls = self.online(fake_tool("0.5.0"))
        r, _ = self.install("online")
        self.assertTrue(r["ok"], r)
        base = "https://raw.githubusercontent.com/Featherwolf/OXP3-Tool-for-Bazzite/" + "a" * 40 + "/dist/"
        self.assertEqual(urls, [oxp3tool.COMMITS_API, base + "oxp3-tool.sha256", base + "oxp3-tool"])
        self.assertEqual((r["version"], r["commit"]), ("0.5.0", "a" * 40))
        self.assertEqual(self.settings.get(oxp3tool.ONLINE_KEY)["version"], "0.5.0")

    def test_online_without_the_api_uses_head_and_checks_the_sha(self):
        urls = self.online(fake_tool("0.5.0"), api_down=True, digest="1" * 64)
        r, _ = self.install("online")
        self.assertIn("sha256 is", r["error"])
        self.assertTrue(urls[1].startswith("https://raw.githubusercontent.com/Featherwolf/OXP3-Tool-for-Bazzite/HEAD/"))
        self.assertEqual(self.read_dest(), fake_tool("0.4.0"))


class UpdateCheckTest(Base):
    def check(self, installed, bundled, online="0.4.0", fail=False):
        inst = {"installed": installed is not None, "path": None, "version": installed, "json_ok": True,
                "min_version": "0.4.1", "bundled_version": bundled}
        fetched = []

        def get_text(url, timeout=30, max_bytes=2_000_000):
            fetched.append(url)
            if fail:
                raise downloads.DownloadError(f"{url}: timed out")
            if url == oxp3tool.COMMITS_API:
                return json.dumps([{"sha": "b" * 40}])
            return f'__version__ = "{online}"\n'

        with mock.patch.object(oxp3tool, "installed", lambda ctx: inst), \
                mock.patch.object(downloads, "get_text", get_text):
            return oxp3tool.update_check(self.ctx, self.settings), fetched

    def test_which_update(self):
        for installed, bundled, online, want in (("0.4.0", "0.4.1", "0.5.0", "bundled"),
                                                 (None, "0.4.0", "0.5.0", "bundled"),
                                                 ("0.4.1", "0.4.1", "0.5.0", "online"),
                                                 (None, None, "0.4.0", "online"),
                                                 ("0.5.0", "0.4.1", "0.5.0", None),
                                                 ("0.10.0", "0.9.0", "0.9.9", None)):
            self.settings.set(oxp3tool.ONLINE_KEY, None)
            r, _ = self.check(installed, bundled, online)
            self.assertEqual(r, {"ok": True, "error": None, "installed": installed, "bundled": bundled,
                                 "online": online, "online_commit": "b" * 40, "update": want}, (installed, bundled))

    def test_cached_for_an_hour(self):
        _, fetched = self.check("0.4.0", None, "0.4.2")
        self.assertEqual(len(fetched), 2)
        self.assertTrue(fetched[1].endswith("/" + "b" * 40 + "/oxp3_tool/__init__.py"))
        r, fetched = self.check("0.4.0", None, "9.9.9")
        self.assertEqual((fetched, r["online"], r["update"]), ([], "0.4.2", "online"))
        mark = self.settings.get(oxp3tool.ONLINE_KEY)
        mark["checked_at"] -= 3601
        self.settings.set(oxp3tool.ONLINE_KEY, mark)
        r, fetched = self.check("0.4.0", None, "0.4.3")
        self.assertEqual((len(fetched), r["online"]), (2, "0.4.3"))

    def test_offline(self):
        r, _ = self.check("0.4.0", None, fail=True)
        self.assertFalse(r["ok"])
        self.assertIn("online check:", r["error"])
        self.assertEqual((r["online"], r["update"]), (None, None))
        r, _ = self.check("0.4.0", "0.4.1", fail=True)   # the plugin's copy needs no network
        self.assertEqual((r["ok"], r["error"], r["update"]), (True, None, "bundled"))


class MarkerTest(Base):
    def test_rebuild_unfinished(self):
        marker = self.ctx.sys_path(oxp3tool.REBUILD_MARKER)
        self.assertIsNone(oxp3tool.rebuild_unfinished(self.ctx))
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w") as f:
            f.write("limine-mkinitcpio\n")
        self.assertEqual(oxp3tool.rebuild_unfinished(self.ctx), "limine-mkinitcpio")
        with open(marker, "w") as f:
            f.write("\n")
        self.assertEqual(oxp3tool.rebuild_unfinished(self.ctx), "limine-update")
        sd = self.ctx.sys_path("/usr/bin/sdboot-manage")
        os.makedirs(os.path.dirname(sd), exist_ok=True)
        open(sd, "w").close()
        self.assertEqual(oxp3tool.rebuild_unfinished(self.ctx), "sdboot-manage gen")

    def test_tool_running(self):
        """RT-2: oxp3-tool's own lock (cli.py acquire_lock: LOCK_EX for the whole run) says whether it runs."""
        import fcntl
        lock = self.ctx.sys_path(oxp3tool.TOOL_LOCK)
        self.assertFalse(oxp3tool.tool_running(self.ctx))           # no lock file: nothing ever ran
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        self.addCleanup(os.close, fd)
        self.assertFalse(oxp3tool.tool_running(self.ctx))
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)               # a run holds it
        self.assertTrue(oxp3tool.tool_running(self.ctx))
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.assertFalse(oxp3tool.tool_running(self.ctx))
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)               # the probe left no lock behind
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.rename(lock, lock + ".real")
        os.symlink(lock + ".real", lock)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertFalse(oxp3tool.tool_running(self.ctx))           # not through a link


if __name__ == "__main__":
    unittest.main()
