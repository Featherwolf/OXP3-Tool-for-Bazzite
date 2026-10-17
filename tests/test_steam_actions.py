import contextlib
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from oxp3_tool import steam_actions, steam_session, templates
from oxp3_tool.cli import main
from oxp3_tool.system import CommandResult, System

from .fakeroot import FakeOxp3

IP_OWNER = ":1.42"
KEYBOARD_LINE = "'~/.steam/root/ubuntu12_32/steam' 'steam://open/keyboard'\n"


def signal_line(action="ui_osk", value=1.0, sender=IP_OWNER, **extra):
    msg = {"type": "signal", "sender": sender, "path": "/org/shadowblip/InputPlumber/devices/target/dbus0",
           "interface": "org.shadowblip.Input.DBusDevice", "member": "InputEvent",
           "payload": {"type": "sd", "data": [action, value]}}
    msg.update(extra)
    return json.dumps(msg) + "\n"


class ParseTest(unittest.TestCase):
    def test_parse_event(self):
        self.assertEqual(steam_actions.parse_event(signal_line()), (IP_OWNER, "ui_osk", 1.0))
        self.assertEqual(steam_actions.parse_event(signal_line(value=0)), (IP_OWNER, "ui_osk", 0.0))
        self.assertEqual(steam_actions.parse_event(signal_line(sender=None)), ("", "ui_osk", 1.0))
        for bad in ("", "not json", "[]", "null", signal_line(member="TouchEvent"),
                    signal_line(interface="org.example"), signal_line(type="method_call"),
                    signal_line(payload=["sd"]), signal_line(payload=None), signal_line(payload={"type": "sd"}),
                    signal_line(payload={"type": "s", "data": ["ui_osk", 1.0]}),
                    signal_line(value=True), signal_line(value="1"), signal_line(action=7)):
            self.assertIsNone(steam_actions.parse_event(bad), bad)


class FakeActions:
    def __init__(self):
        self.done = []

    def perform(self, action):
        self.done.append(action)
        return True


class WatcherTest(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(steam_actions, "name_owner", return_value=IP_OWNER),
            mock.patch.object(steam_actions, "log"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.clock = 100.0
        p = mock.patch.object(steam_actions.time, "monotonic", side_effect=lambda: self.clock)
        p.start()
        self.addCleanup(p.stop)
        self.actions = FakeActions()

    def watcher(self):
        return steam_actions.Watcher(self.actions)

    def test_press_acts_release_does_not(self):
        w = self.watcher()
        self.assertTrue(w.handle(signal_line()))
        self.assertFalse(w.handle(signal_line(value=0.0)))
        self.assertFalse(w.handle(signal_line(action="ui_guide")))
        self.assertTrue(w.handle(signal_line(action="oxp3_home")))
        self.assertTrue(w.handle(signal_line(action="oxp3_fps_limit")))
        self.assertEqual(self.actions.done, ["keyboard", "home", "fps-limit"])

    def test_debounce_per_action(self):
        w = self.watcher()
        self.assertTrue(w.handle(signal_line()))
        self.clock += 0.1
        self.assertFalse(w.handle(signal_line()))
        self.assertTrue(w.handle(signal_line(action="oxp3_home")))  # another action is not held back
        self.clock += 0.3
        self.assertTrue(w.handle(signal_line()))
        self.assertEqual(self.actions.done, ["keyboard", "home", "keyboard"])

    def test_only_inputplumber_may_send_it(self):
        self.assertIn("sender='org.shadowblip.InputPlumber'", steam_actions.MATCH)
        w = self.watcher()
        self.assertFalse(w.handle(signal_line(sender=":1.99")))
        self.clock += 1
        self.assertFalse(w.handle(signal_line(sender=None)))
        self.assertEqual(self.actions.done, [])
        # InputPlumber restarted with a new unique name: looked up again.
        steam_actions.name_owner.return_value = ":1.99"
        self.clock += 1
        self.assertTrue(w.handle(signal_line(sender=":1.99")))
        steam_actions.name_owner.return_value = None
        self.clock += 1
        self.assertFalse(w.handle(signal_line(sender=":1.7")))

    def test_owner_lookups_are_rate_limited(self):
        w = self.watcher()
        for _ in range(100):
            self.assertFalse(w.handle(signal_line(sender=":1.66")))
        self.assertEqual(steam_actions.name_owner.call_count, 1)
        self.assertEqual(steam_actions.log.call_count, 1)
        self.clock += steam_actions.OWNER_RECHECK
        self.assertTrue(w.handle(signal_line()))  # the real sender still gets through


class WriteTest(unittest.TestCase):
    """The pipe writer that runs as the Steam user."""

    def write(self, path):
        res = subprocess.run([sys.executable, "-I", "-c", steam_session._WRITE, str(path), KEYBOARD_LINE],
                             capture_output=True, timeout=10)
        return res.returncode

    def test_line_format(self):
        # steam-runtime-tools' _srt_steam_command_via_pipe: fixed program path, then shell-quoted arguments.
        self.assertEqual(steam_session.pipe_line(steam_session.URLS["keyboard"]), KEYBOARD_LINE)
        self.assertEqual(steam_session.pipe_line(steam_session.URLS["home"]),
                         "'~/.steam/root/ubuntu12_32/steam' 'steam://open/library'\n")

    def test_writes_to_a_reading_steam(self):
        with tempfile.TemporaryDirectory() as tmp:
            fifo = Path(tmp, "steam.pipe")
            os.mkfifo(fifo)
            got = []

            def reader():
                with open(fifo, "rb") as f:
                    got.append(f.read())

            t = threading.Thread(target=reader)
            t.start()
            # The writer gets ENXIO until the reader has the FIFO open.
            for _ in range(200):
                code = self.write(fifo)
                if code != steam_session.NOT_READING:
                    break
            t.join(10)
            self.assertEqual(code, 0)
            self.assertEqual(got, [KEYBOARD_LINE.encode()])

    def test_steam_not_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.write(Path(tmp, "steam.pipe")), steam_session.NOT_READING)
            fifo = Path(tmp, "steam.pipe")
            os.mkfifo(fifo)
            self.assertEqual(self.write(fifo), steam_session.NOT_READING)  # no reader: ENXIO, never blocks

    def test_refuses_regular_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "steam.pipe")
            path.write_text("keep")
            self.assertEqual(self.write(path), 1)
            self.assertEqual(path.read_text(), "keep")


class OpenUrlTest(unittest.TestCase):
    def run_open(self, uids, codes, action="keyboard"):
        err = io.StringIO()
        with mock.patch.object(steam_session, "steam_uids", return_value=uids), \
                mock.patch.object(steam_session, "send_as", side_effect=codes) as send, \
                contextlib.redirect_stderr(err):
            ok = steam_session.open_url(action)
        return ok, err.getvalue(), send

    def test_results(self):
        ok, log, _ = self.run_open([], [])
        self.assertFalse(ok)
        self.assertIn("failed: Steam is not running; cannot open the keyboard", log)
        ok, log, send = self.run_open([1000, 1001], [1, 0])
        self.assertTrue(ok)
        self.assertIn("ok: asked Steam to open the keyboard (uid 1001)", log)
        self.assertEqual(send.call_args.args, (1001, "steam://open/keyboard"))
        ok, log, _ = self.run_open([1000], [steam_session.NOT_READING])
        self.assertFalse(ok)
        self.assertIn("failed: could not hand the request to Steam: Steam is not reading", log)
        ok, log, _ = self.run_open([1000], [1])
        self.assertIn("failed: could not hand the request to Steam: writing", log)
        ok, log, send = self.run_open([1000], [0], action="home")
        self.assertIn("ok: asked Steam to open the Home screen (uid 1000)", log)
        self.assertEqual(send.call_args.args, (1000, "steam://open/library"))

    def test_send_as_drops_privileges(self):
        pw = mock.Mock(pw_dir="/var/home/gamer", pw_gid=1000)
        with mock.patch.object(steam_session.pwd, "getpwuid", return_value=pw), \
                mock.patch.object(steam_session.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(steam_session.send_as(1000, "steam://open/keyboard"), 0)
        argv = run.call_args.args[0]
        kw = run.call_args.kwargs
        self.assertEqual(argv[:3], ["/usr/bin/python3", "-I", "-c"])
        self.assertEqual(argv[4:], ["/var/home/gamer/.steam/steam.pipe", KEYBOARD_LINE])
        self.assertEqual((kw["user"], kw["group"], kw["extra_groups"]), (1000, 1000, []))
        self.assertEqual(kw["env"], {"HOME": "/var/home/gamer", "PATH": "/usr/bin"})
        with mock.patch.object(steam_session.pwd, "getpwuid", side_effect=KeyError):
            self.assertEqual(steam_session.send_as(4242, "steam://open/keyboard"), 1)

    def test_steam_uids(self):
        with tempfile.TemporaryDirectory() as tmp:
            for pid, comm in (("10", "steam\n"), ("11", "steamwebhelper\n"), ("12", "steam\n"), ("self", "steam\n")):
                Path(tmp, pid).mkdir()
                Path(tmp, pid, "comm").write_text(comm)
            Path(tmp, "13").mkdir()  # exited between listdir and open
            me = os.getuid()
            self.assertEqual(steam_session.steam_uids(tmp), [] if me == 0 else [me])
            self.assertEqual(steam_session.steam_uids(os.path.join(tmp, "missing")), [])


class FakeProc:
    """A /proc with one Steam client in a Game Mode session and gamescope's limiter file."""

    def __init__(self, tmp, limiter=0, display="gamescope-0", uid=1000):
        self.proc = Path(tmp, "proc")
        pid = self.proc / "4321"
        pid.mkdir(parents=True)
        (pid / "comm").write_text("steam\n")
        env = {"HOME": "/var/home/gamer", "GAMESCOPE_WAYLAND_DISPLAY": display,
               "GAMESCOPE_LIMITER_FILE": "/tmp/gamescope-limiter.AbCd1234", "XDG_RUNTIME_DIR": "/run/user/1000"}
        (pid / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")
        self.limiter = pid / "root/tmp/gamescope-limiter.AbCd1234"
        self.limiter.parent.mkdir(parents=True)
        self.set(limiter)
        self.uid = uid
        stat_uid = os.stat(pid).st_uid
        self.patches = [
            mock.patch.object(steam_session.os, "stat", side_effect=self.fake_stat(stat_uid)),
            mock.patch.object(steam_session.os, "fstat", side_effect=self.fake_fstat()),
        ]

    def set(self, value):
        self.limiter.write_bytes(struct.pack("=I", value))

    def fake_stat(self, real_uid):
        real = os.stat

        def fake(path, *a, **kw):
            st = real(path, *a, **kw)
            if str(path).startswith(str(self.proc)):
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, self.uid, st.st_gid,
                                       st.st_size, int(st.st_atime), int(st.st_mtime), int(st.st_ctime)))
            return st
        return fake

    def fake_fstat(self):
        real = os.fstat

        def fake(fd):
            st = real(fd)
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, self.uid, st.st_gid,
                                   st.st_size, int(st.st_atime), int(st.st_mtime), int(st.st_ctime)))
        return fake

    def __enter__(self):
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()


class FrameLimitTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.runs = []

    def toggle(self, fake, fps=72, stderr=b"", rc=0, effect=True, limit=None):
        limit = limit or steam_session.FrameLimit()

        def run(argv, **kw):
            self.runs.append((argv, kw))
            if effect and not rc and not stderr:
                fake.set(1 if argv[-1] != "0" else 0)
            return subprocess.CompletedProcess(argv, rc, b"", stderr)

        pw = mock.Mock(pw_dir="/var/home/gamer", pw_gid=1000)
        err = io.StringIO()
        with mock.patch.object(steam_session.pwd, "getpwuid", return_value=pw), \
                mock.patch.object(steam_session.subprocess, "run", side_effect=run), contextlib.redirect_stderr(err):
            ok = limit.toggle(fps, str(fake.proc))
        return ok, err.getvalue()

    def test_toggles_from_gamescope_state(self):
        with FakeProc(self.tmp) as fake:
            limit = steam_session.FrameLimit()
            ok, log = self.toggle(fake, limit=limit)
            self.assertTrue(ok)
            self.assertIn("ok: frame limit 72 fps", log)
            argv, kw = self.runs[-1]
            self.assertEqual(argv, ["/usr/bin/gamescopectl", "debug_set_fps_limit", "72"])
            self.assertEqual((kw["user"], kw["extra_groups"]), (1000, []))
            self.assertEqual(kw["env"]["XDG_RUNTIME_DIR"], "/run/user/1000")
            self.assertEqual(kw["env"]["GAMESCOPE_WAYLAND_DISPLAY"], "gamescope-0")
            ok, log = self.toggle(fake, limit=limit)
            self.assertIn("ok: frame limit off", log)
            self.assertEqual(self.runs[-1][0][-1], "0")
            # A limit Steam set (Quick Access, or one at the panel's full rate that changes
            # nothing) is replaced, so the press always shows a limit; the next press clears it.
            fake.set(1)
            ok, log = self.toggle(fake, fps=48, limit=limit)
            self.assertEqual(self.runs[-1][0][-1], "48")
            self.assertIn("ok: frame limit 48 fps (replaced a limit that was already on)", log)

    def test_first_press_sets_even_when_steam_has_a_limit(self):
        with FakeProc(self.tmp, limiter=1) as fake:
            limit = steam_session.FrameLimit()
            self.toggle(fake, limit=limit)
            self.assertEqual(self.runs[-1][0][-1], "72")
            self.toggle(fake, limit=limit)
            self.assertEqual(self.runs[-1][0][-1], "0")
            # Steam turned our limit off meanwhile: the press sets it again instead of clearing nothing.
            self.toggle(fake, limit=limit)
            fake.set(0)
            self.toggle(fake, limit=limit)
            self.assertEqual(self.runs[-1][0][-1], "72")

    def test_a_new_game_mode_session_starts_without_our_limit(self):
        with FakeProc(self.tmp, limiter=0) as fake:
            limit = steam_session.FrameLimit()
            self.toggle(fake, limit=limit)
            self.assertEqual(self.runs[-1][0][-1], "72")
            # Desktop Mode and back: a new limiter file, and Steam's no-op limit is on.
            environ = fake.proc / "4321" / "environ"
            environ.write_bytes(environ.read_bytes().replace(b"AbCd1234", b"New54321"))
            new = fake.proc / "4321" / "root/tmp/gamescope-limiter.New54321"
            new.write_bytes(struct.pack("=I", 1))
            fake.limiter = new
            self.toggle(fake, limit=limit)
            self.assertEqual(self.runs[-1][0][-1], "72")  # sets, instead of turning the old one off

    def test_failures(self):
        with FakeProc(self.tmp) as fake:
            ok, log = self.toggle(fake, stderr=b"Command not found.\n")
            self.assertFalse(ok)
            self.assertIn("failed: could not change the frame limit: gamescope: Command not found.", log)
            ok, log = self.toggle(fake, rc=1, stderr=b"Failed to connect\n")
            self.assertIn("failed: could not change the frame limit: gamescopectl exited with 1", log)
            ok, log = self.toggle(fake, effect=False)
            self.assertIn("failed: gamescope did not set the frame limit", log)

    def test_not_in_game_mode(self):
        with FakeProc(self.tmp, display="") as fake:
            ok, log = self.toggle(fake)
        self.assertFalse(ok)
        # A failure, so status shows it; it names the Steam processes it saw.
        self.assertIn("failed: Steam is not running in Game Mode; no frame limit to change "
                      "(Steam processes: pid 4321 uid 1000 ? (no gamescope display))", log)
        self.assertEqual(self.runs, [])

    def test_unreadable_state_falls_back_to_what_was_sent(self):
        with FakeProc(self.tmp) as fake:
            fake.limiter.unlink()
            limit = steam_session.FrameLimit()
            ok, log = self.toggle(fake, effect=False, limit=limit)
            self.assertEqual(self.runs[-1][0][-1], "72")
            ok, log = self.toggle(fake, effect=False, limit=limit)
            self.assertEqual(self.runs[-1][0][-1], "0")

    def test_limiter_file_checks(self):
        with FakeProc(self.tmp, uid=4242) as fake:  # not owned by the Steam user
            env = steam_session.read_environ(str(fake.proc), 4321)
            self.assertIsNone(steam_session.limiter_active(str(fake.proc), 4321, 1000, env))
            self.assertFalse(steam_session.limiter_active(str(fake.proc), 4321, 4242, env))
            for path in ("/tmp/other", "tmp/gamescope-limiter.x", "/tmp/../etc/gamescope-limiter.x"):
                self.assertIsNone(steam_session.limiter_active(str(fake.proc), 4321, 4242,
                                                               {"GAMESCOPE_LIMITER_FILE": path}))


class RobustnessTest(unittest.TestCase):
    def test_process_names_that_are_not_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            for pid, comm in (("10", b"GameApp\xe3\x83\x89\xe3\x83\xa9\xe3\x82\n"), ("11", b"steam\n")):
                Path(tmp, pid).mkdir()
                Path(tmp, pid, "comm").write_bytes(comm)
            me = os.getuid()
            self.assertEqual(steam_session.steam_uids(tmp), [] if me == 0 else [me])

    def test_an_action_error_is_logged_not_raised(self):
        err = io.StringIO()
        with mock.patch.object(steam_session, "open_url", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stderr(err):
            self.assertFalse(steam_session.Actions().perform("home"))
        self.assertIn("failed: home: RuntimeError: boom", err.getvalue())


class KeyConfigTest(unittest.TestCase):
    def test_reads_and_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "keys.json")
            self.assertEqual(steam_session.key_config(path), {})
            Path(path).write_text(templates.keys_config_json(dict(templates.DEFAULT_BUTTON_ACTIONS, xbox="home"),
                                                             "native", 48))
            config = steam_session.key_config(path)
            self.assertEqual((config["xbox"], config["session"], steam_session.fps_limit(config)),
                             ("home", "native", 48))
            for bad in ("[]", "not json", json.dumps({"fps_limit": 500}), json.dumps({"fps_limit": True})):
                Path(path).write_text(bad)
                self.assertEqual(steam_session.fps_limit(steam_session.key_config(path)),
                                 steam_session.DEFAULT_FPS_LIMIT)
            Path(path).write_text(" " * (steam_session.KEYS_CONFIG_MAX + 1))
            self.assertEqual(steam_session.key_config(path), {})


class ScriptTest(unittest.TestCase):
    def test_installed_scripts_are_standalone(self):
        for script, path in ((templates.steam_actions_helper_script(), templates.STEAM_ACTIONS_HELPER_PATH),
                             (templates.xbox_key_helper_script(), templates.XBOX_KEY_HELPER_PATH)):
            self.assertTrue(script.startswith("#!/usr/bin/python3 -I\n# Installed by oxp3-tool ("))
            body = script.split("\n", 3)[3]
            self.assertNotIn("oxp3_tool", body)
            self.assertEqual(body.count("from __future__"), 1)
            self.assertTrue(body.startswith("from __future__ import annotations\n"))
            compile(script, path, "exec")

    def test_installed_scripts_run(self):
        """The bundles define everything they use: import them and look at the entry points."""
        for script, names in ((templates.steam_actions_helper_script(), ("Watcher", "Actions", "run", "MATCH")),
                              (templates.xbox_key_helper_script(), ("Sender", "Actions", "run", "find_hidraw"))):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, "helper.py")
                path.write_text(script)
                code = ("import importlib.util, sys; spec = importlib.util.spec_from_file_location('h', sys.argv[1]); "
                        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
                        f"print(all(hasattr(m, n) for n in {names!r}))")
                res = subprocess.run([sys.executable, "-I", "-c", code, str(path)], capture_output=True, text=True,
                                     timeout=30)
                self.assertEqual(res.stdout.strip(), "True", res.stderr)

    def test_units_can_reach_the_session(self):
        for unit, path in ((templates.STEAM_ACTIONS_UNIT, templates.STEAM_ACTIONS_HELPER_PATH),
                           (templates.XBOX_KEY_UNIT, templates.XBOX_KEY_HELPER_PATH)):
            self.assertIn("ProtectHome=read-only", unit)
            self.assertNotIn("ProtectHome=yes", unit)
            self.assertIn(f"ExecStart=/usr/bin/python3 -I {path}", unit)


class SteamActionsFixTest(unittest.TestCase):
    UNIT = templates.STEAM_ACTIONS_UNIT_NAME

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.f = FakeOxp3(self.root)
        self.helper = self.root / templates.STEAM_ACTIONS_HELPER_PATH.lstrip("/")
        self.unit = self.root / templates.STEAM_ACTIONS_UNIT_PATH.lstrip("/")

    def cli(self, *argv, system=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(list(argv), system=system or self.f.system())
        return rc, out.getvalue(), err.getvalue()

    def calls(self):
        return [" ".join(c) for c in self.f.runner.calls]

    def test_apply_installs_and_starts_helper(self):
        rc, out, err = self.cli("apply", "--yes", "buttons")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.helper.read_text(), templates.steam_actions_helper_script())
        self.assertEqual(self.helper.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.unit.read_text(), templates.STEAM_ACTIONS_UNIT)
        calls = self.calls()
        self.assertIn(f"systemctl enable {self.UNIT}", calls)
        self.assertLess(calls.index("systemctl daemon-reload"), calls.index(f"systemctl restart {self.UNIT}"))
        self.assertLess(calls.index("systemctl try-restart inputplumber.service"),
                        calls.index(f"systemctl restart {self.UNIT}"))
        rc, out, _ = self.cli("status")
        self.assertIn(f"{self.UNIT} is active", out)

    def test_switching_to_the_chord_drops_the_helper(self):
        self.assertEqual(self.cli("apply", "--yes", "buttons")[0], 0)
        rc, out, _ = self.cli("apply", "--dry-run", "buttons", "--keyboard-key", "keyboard-chord")
        self.assertIn(f"stop and disable {self.UNIT}", out)
        self.assertIn(f"remove {templates.STEAM_ACTIONS_HELPER_PATH}", out)
        self.f.runner.calls.clear()
        rc, out, err = self.cli("apply", "--yes", "buttons", "--keyboard-key", "keyboard-chord")
        self.assertEqual(rc, 0, err)
        calls = self.calls()
        self.assertIn(f"systemctl disable --now {self.UNIT}", calls)
        self.assertIn("systemctl daemon-reload", calls)
        self.assertNotIn(f"systemctl restart {self.UNIT}", calls)
        self.assertFalse(self.helper.exists())
        self.assertFalse(self.unit.exists())
        self.assertEqual(self.f.runner.unit_active[self.UNIT], "inactive")
        rc, out, _ = self.cli("status")
        self.assertNotIn(self.UNIT, out)
        # And back.
        self.assertEqual(self.cli("apply", "--yes", "buttons", "--keyboard-key", "keyboard")[0], 0)
        self.assertTrue(self.unit.exists())
        self.assertEqual(self.f.runner.unit_enabled[self.UNIT], "enabled")

    def test_ogui_overlay_needs_no_helper(self):
        self.f.ogui_overlay()
        self.assertEqual(self.cli("apply", "--yes", "buttons")[0], 0)
        self.assertFalse(self.unit.exists())
        self.assertNotIn(f"systemctl enable {self.UNIT}", self.calls())

    def test_revert_stops_helper(self):
        self.assertEqual(self.cli("apply", "--yes", "buttons")[0], 0)
        self.f.runner.calls.clear()
        rc, out, err = self.cli("revert", "--yes", "buttons")
        self.assertEqual(rc, 0, err)
        calls = self.calls()
        self.assertIn(f"systemctl disable {self.UNIT}", calls)
        self.assertIn(f"systemctl stop {self.UNIT}", calls)
        self.assertFalse(self.helper.exists())
        self.assertFalse((self.root / "usr/local/libexec/oxp3-tool").exists())
        self.assertEqual(self.f.runner.unit_active[self.UNIT], "inactive")
        self.assertNotIn("warning", out)

    def test_revert_without_helper_does_not_stop_it(self):
        self.assertEqual(self.cli("apply", "--yes", "buttons", "--keyboard-key", "keyboard-chord")[0], 0)
        self.f.runner.calls.clear()
        self.assertEqual(self.cli("revert", "--yes", "buttons")[0], 0)
        self.assertNotIn(f"systemctl stop {self.UNIT}", self.calls())

    def test_status_reports_failures_and_stale_helper(self):
        self.assertEqual(self.cli("apply", "--yes", "buttons")[0], 0)
        real_run = self.f.runner.__call__
        log = []

        def runner(argv, timeout=None):
            if argv[0] == "journalctl" and self.UNIT in argv:
                # journalctl -r --grep ... -n 1: the newest outcome line.
                self.assertEqual(argv[argv.index("--grep") + 1], steam_session.OUTCOME_GREP)
                self.assertIn("-r", argv)
                return CommandResult(0, log[-1] + "\n") if log else CommandResult(1)
            return real_run(argv, timeout)

        warning = "warning: the helper's last action failed (could not hand the request to Steam"
        log.append("failed: could not hand the request to Steam: Steam is not reading ~/.steam/steam.pipe")
        rc, out, _ = self.cli("status", system=System(self.root, runner))
        self.assertIn(warning, out)
        log.append("ok: asked Steam to open the keyboard (uid 1000)")  # a later press worked
        rc, out, _ = self.cli("status", system=System(self.root, runner))
        self.assertNotIn(warning, out)
        with mock.patch.object(templates, "steam_actions_helper_script", lambda: "#!/usr/bin/python3 -I\n# new\n"):
            rc, out, _ = self.cli("status")
            self.assertIn("installed by an older oxp3-tool; run `sudo oxp3-tool apply buttons`", out)
            rc, out, _ = self.cli("apply", "--dry-run")
            self.assertIn(f"update {templates.STEAM_ACTIONS_HELPER_PATH}", out)

    def test_no_restart_twice_changes_nothing(self):
        self.assertEqual(self.cli("apply", "--yes", "buttons", "--no-restart")[0], 0)
        self.f.runner.calls.clear()
        rc, out, _ = self.cli("apply", "--yes", "buttons", "--no-restart")
        self.assertIn("already applied, nothing to change", out)
        self.assertNotIn("finish the InputPlumber restart", out)
        changing = ("systemctl enable", "systemctl disable", "systemctl restart", "systemctl try-restart",
                    "systemctl stop", "systemctl daemon-reload")
        self.assertEqual([c for c in self.calls() if c.startswith(changing)], [])
        # A later apply without --no-restart finishes the skipped restart.
        rc, out, _ = self.cli("apply", "--yes", "buttons")
        self.assertIn("finish the InputPlumber restart skipped earlier", out)
        self.assertIn("systemctl try-restart inputplumber.service", self.calls())

    def test_failed_apply_keeps_the_restart_pending(self):
        self.f.runner.fail.add(f"systemctl enable {self.UNIT}")
        rc, _, _ = self.cli("apply", "--yes", "buttons")
        self.assertNotEqual(rc, 0)
        self.assertNotIn("systemctl try-restart inputplumber.service", self.calls())
        self.f.runner.fail.clear()
        rc, out, err = self.cli("apply", "--yes", "buttons")
        self.assertEqual(rc, 0, err)
        self.assertIn("systemctl try-restart inputplumber.service", self.calls())
        rc, out, _ = self.cli("status")
        self.assertNotIn("InputPlumber has not been restarted", out)

    def test_failed_no_restart_apply_keeps_the_restart_pending(self):
        self.f.runner.fail.add(f"systemctl enable {self.UNIT}")
        self.assertNotEqual(self.cli("apply", "--yes", "buttons", "--no-restart")[0], 0)  # map already written
        self.f.runner.fail.clear()
        rc, out, _ = self.cli("apply", "--yes", "buttons", "--no-restart")
        self.assertEqual(rc, 0)
        self.assertIn("InputPlumber was not restarted", out)
        rc, out, _ = self.cli("status")
        self.assertIn("InputPlumber has not been restarted", out)
        self.f.runner.calls.clear()
        self.assertEqual(self.cli("apply", "--yes", "buttons")[0], 0)
        self.assertIn("systemctl try-restart inputplumber.service", self.calls())
        rc, out, _ = self.cli("status")
        self.assertNotIn("InputPlumber has not been restarted", out)

    def test_failure_before_the_map_write_is_not_a_pending_restart(self):
        self.assertEqual(self.cli("apply", "--yes", "buttons")[0], 0)
        self.f.runner.fail.add(f"systemctl disable --now {self.UNIT}")
        self.assertNotEqual(self.cli("apply", "--yes", "buttons", "--keyboard-key", "keyboard-chord")[0], 0)
        self.f.runner.fail.clear()
        rc, out, _ = self.cli("status")
        self.assertNotIn("InputPlumber has not been restarted", out)
        self.f.runner.calls.clear()
        rc, out, _ = self.cli("apply", "--yes", "buttons")
        self.assertNotIn("systemctl try-restart inputplumber.service", self.calls())

    def test_failed_map_write_is_not_a_pending_restart(self):
        from oxp3_tool.fixes import IP_ETC_CAPABILITY_MAPS
        self.assertEqual(self.cli("apply", "--yes", "buttons")[0], 0)
        real_write = System.write_atomic

        def write_atomic(sys_, path, content, mode=0o644):
            if path.startswith(IP_ETC_CAPABILITY_MAPS):
                raise OSError(28, "No space left on device")
            return real_write(sys_, path, content, mode)

        with mock.patch.object(System, "write_atomic", write_atomic):
            self.assertNotEqual(self.cli("apply", "--yes", "buttons", "--home", "quick-access")[0], 0)
        rc, out, _ = self.cli("status")
        self.assertNotIn("InputPlumber has not been restarted", out)

    def test_no_restart_helper_update_is_not_a_pending_restart(self):
        self.assertEqual(self.cli("apply", "--yes", "buttons")[0], 0)
        with mock.patch.object(templates, "steam_actions_helper_script", lambda: "#!/usr/bin/python3 -I\n# new\n"):
            rc, out, _ = self.cli("apply", "--yes", "buttons", "--no-restart")
            self.assertNotIn("InputPlumber was not restarted", out)
            rc, out, _ = self.cli("status")
            self.assertNotIn("InputPlumber has not been restarted", out)
            self.f.runner.calls.clear()
            rc, out, _ = self.cli("apply", "--yes", "buttons")
        self.assertIn("already applied, nothing to change", out)
        self.assertNotIn("systemctl try-restart inputplumber.service", self.calls())

    def test_helper_update_leaves_inputplumber_alone(self):
        self.assertEqual(self.cli("apply", "--yes", "buttons")[0], 0)
        self.f.runner.calls.clear()
        with mock.patch.object(templates, "steam_actions_helper_script", lambda: "#!/usr/bin/python3 -I\n# new\n"):
            rc, out, _ = self.cli("apply", "--dry-run", "buttons")
            self.assertIn("then start the Steam actions helper", out)
            self.assertNotIn("restart InputPlumber", out)
            rc, out, err = self.cli("apply", "--yes", "buttons")
        self.assertEqual(rc, 0, err)
        calls = self.calls()
        self.assertNotIn("systemctl try-restart inputplumber.service", calls)
        self.assertIn(f"systemctl restart {self.UNIT}", calls)
        self.assertNotIn("Reboot afterwards", out)
        # A map change still restarts InputPlumber, with the reboot note.
        rc, out, _ = self.cli("apply", "--yes", "buttons", "--home", "quick-access")
        self.assertIn("systemctl try-restart inputplumber.service", self.calls())
        self.assertIn("Reboot afterwards", out)

    def test_no_restart_keeps_dropped_helper_running(self):
        self.assertEqual(self.cli("apply", "--yes", "buttons")[0], 0)
        rc, out, _ = self.cli("apply", "--dry-run", "buttons", "--no-restart", "--keyboard-key", "keyboard-chord")
        self.assertIn(f"disable {self.UNIT} (it keeps running until the next boot)", out)
        self.f.runner.calls.clear()
        rc, out, err = self.cli("apply", "--yes", "buttons", "--no-restart", "--keyboard-key", "keyboard-chord")
        self.assertEqual(rc, 0, err)
        calls = self.calls()
        self.assertIn(f"systemctl disable {self.UNIT}", calls)
        self.assertNotIn(f"systemctl disable --now {self.UNIT}", calls)
        self.assertEqual(self.f.runner.unit_active[self.UNIT], "active")
        self.assertEqual(self.f.runner.unit_enabled[self.UNIT], "disabled")

    def test_shared_directory_is_removed_by_the_last_revert(self):
        from .test_xbox_key import IP_LOG
        for order in (["buttons", "xbox-key"], ["xbox-key", "buttons"]):
            with self.subTest(order=order):
                self.f.mcu_hidraw()
                self.f.runner.ip_journal = IP_LOG
                self.assertEqual(self.cli("apply", "--yes", "buttons", "xbox-key")[0], 0)
                for fix in order:
                    self.assertEqual(self.cli("revert", "--yes", fix)[0], 0)
                self.assertFalse((self.root / "usr/local/libexec/oxp3-tool").exists())
        self.assertEqual(self.cli("apply", "--yes", "buttons", "xbox-key")[0], 0)
        self.assertEqual(self.cli("revert", "--yes", "--all")[0], 0)
        self.assertFalse((self.root / "usr/local/libexec/oxp3-tool").exists())

    def test_shared_directory_with_state_from_the_first_version(self):
        import json
        from oxp3_tool.state import STATE_FILE
        from .test_xbox_key import IP_LOG
        self.f.mcu_hidraw()
        self.f.runner.ip_journal = IP_LOG
        self.assertEqual(self.cli("apply", "--yes", "buttons", "xbox-key")[0], 0)
        path = self.root / STATE_FILE.lstrip("/")
        data = json.loads(path.read_text())
        data["fixes"]["xbox-key"]["dirs_created"] = []  # recorded only by the fix that created it
        path.write_text(json.dumps(data))
        self.assertEqual(self.cli("revert", "--yes", "buttons")[0], 0)
        self.assertTrue((self.root / "usr/local/libexec/oxp3-tool").exists())
        self.assertEqual(self.cli("revert", "--yes", "xbox-key")[0], 0)
        self.assertFalse((self.root / "usr/local/libexec/oxp3-tool").exists())

    def test_shared_directory_after_the_helper_was_dropped(self):
        import json
        from oxp3_tool.state import STATE_FILE
        from .test_xbox_key import IP_LOG
        self.f.mcu_hidraw()
        self.f.runner.ip_journal = IP_LOG
        self.assertEqual(self.cli("apply", "--yes", "xbox-key", "buttons")[0], 0)
        path = self.root / STATE_FILE.lstrip("/")
        data = json.loads(path.read_text())
        data["fixes"]["buttons"]["dirs_created"] = []  # state from the first version
        path.write_text(json.dumps(data))
        self.assertEqual(self.cli("revert", "--yes", "xbox-key")[0], 0)
        self.assertEqual(self.cli("apply", "--yes", "buttons", "--keyboard-key", "keyboard-chord")[0], 0)
        self.assertEqual(self.cli("revert", "--yes", "buttons")[0], 0)
        self.assertFalse((self.root / "usr/local/libexec/oxp3-tool").exists())

    def test_lost_state_still_owns_the_unit(self):
        from oxp3_tool.state import STATE_FILE
        self.assertEqual(self.cli("apply", "--yes", "buttons")[0], 0)
        (self.root / STATE_FILE.lstrip("/")).unlink()
        self.assertEqual(self.cli("apply", "--yes", "buttons")[0], 0)
        self.f.runner.calls.clear()
        self.assertEqual(self.cli("apply", "--yes", "buttons", "--keyboard-key", "keyboard-chord")[0], 0)
        self.assertIn(f"systemctl disable --now {self.UNIT}", self.calls())
        self.assertEqual(self.f.runner.unit_active[self.UNIT], "inactive")

    def keys(self):
        config = json.loads((self.root / templates.KEYS_CONFIG_PATH.lstrip("/")).read_text())
        self.assertTrue(config.pop("_comment").startswith("Installed by oxp3-tool ("))
        return config

    def test_home_and_fps_limit_choices(self):
        rc, out, err = self.cli("apply", "--yes", "buttons", "--home", "home", "--xbox-key", "fps-limit",
                                "--fps-limit", "48")
        self.assertEqual(rc, 0, err)
        cap_map = (self.root / "etc/inputplumber/capability_maps.d/onexplayer_type9.yaml").read_text()
        self.assertIn("      dbus: oxp3_home\n", cap_map)
        self.assertIn("      dbus: oxp3_fps_limit\n", cap_map)
        self.assertEqual(self.keys(), {"xbox": "fps-limit", "session": "steam", "fps_limit": 48})
        self.assertTrue(self.helper.exists())
        self.assertIn("No key opens the Steam menu now", out)
        rc, status, _ = self.cli("status")
        self.assertIn("warning: No key opens the Steam menu now", status)
        # Remembered, like the actions; --defaults resets both.
        self.assertEqual(self.cli("apply", "--yes", "buttons")[0], 0)
        self.assertEqual(self.keys()["fps_limit"], 48)
        self.assertEqual(self.cli("apply", "--yes", "buttons", "--defaults")[0], 0)
        self.assertEqual(self.keys(), {"xbox": "guide", "session": "steam", "fps_limit": 72})
        rc, status, _ = self.cli("status")
        self.assertNotIn("No key opens the Steam menu", status)

    def test_home_key_with_the_xbox_helper_loses_holds(self):
        from .test_xbox_key import IP_LOG
        self.f.mcu_hidraw()
        self.f.runner.ip_journal = IP_LOG
        # Both fixes in one run: the warning knows about the helper this run installs.
        rc, out, _ = self.cli("apply", "--yes", "buttons", "xbox-key", "--home", "home")
        self.assertIn("Only the Xbox key opens the Steam menu, and it can only tap", out)
        self.assertNotIn("from an older oxp3-tool", out)
        rc, status, _ = self.cli("status")
        self.assertIn("warning: Only the Xbox key opens the Steam menu", status)
        rc, out, _ = self.cli("apply", "--yes", "buttons", "--home", "guide", "--xbox-key", "fps-limit")
        self.assertNotIn("can only tap", out)
        self.assertNotIn("from an older oxp3-tool", out)
        self.assertEqual(self.keys()["xbox"], "fps-limit")
        # A helper from before it read keys.json is pointed out, for any action but the Steam button.
        unit = self.root / templates.XBOX_KEY_UNIT_PATH.lstrip("/")
        unit.write_text(templates.XBOX_KEY_UNIT.replace("ProtectHome=read-only", "ProtectHome=yes"))
        for action in ("home", "screenshot"):
            rc, out, _ = self.cli("apply", "--yes", "buttons", "--xbox-key", action)
            self.assertIn("run `sudo oxp3-tool apply xbox-key` to update it", out)
        rc, out, _ = self.cli("apply", "--yes", "buttons", "--xbox-key", "guide")
        self.assertNotIn("apply xbox-key", out)

    def test_xbox_key_choice_on_a_helper_unit_leaves_inputplumber_alone(self):
        from .test_xbox_key import IP_LOG
        self.f.mcu_hidraw()
        self.f.runner.ip_journal = IP_LOG
        self.assertEqual(self.cli("apply", "--yes", "xbox-key")[0], 0)
        self.assertEqual(self.cli("apply", "--yes", "buttons")[0], 0)
        cap_map = self.root / "etc/inputplumber/capability_maps.d/onexplayer_type9.yaml"
        self.f.runner.calls.clear()
        rc, out, err = self.cli("apply", "--yes", "buttons", "--xbox-key", "fps-limit")
        self.assertEqual(rc, 0, err)
        # InputPlumber cannot read this Xbox key, so the map has no entry for it; keys.json changed.
        self.assertNotIn("name: Xbox", cap_map.read_text())
        self.assertIn("oxp3-tool-xbox-key.service handles it", cap_map.read_text())
        self.assertEqual(self.keys()["xbox"], "fps-limit")
        self.assertNotIn("systemctl try-restart inputplumber.service", self.calls())
        self.assertNotIn("Reboot afterwards", out)

    def test_undecoded_xbox_key_does_not_count_as_the_steam_menu(self):
        from .test_xbox_key import IP_LOG
        self.f.mcu_hidraw()
        self.f.runner.ip_journal = IP_LOG  # InputPlumber cannot read the Xbox key, and no helper does
        rc, out, _ = self.cli("apply", "--yes", "buttons", "--home", "home")
        self.assertIn("No key opens the Steam menu now", out)

    def test_ogui_menu_on_the_xbox_helper_under_the_overlay(self):
        from .test_xbox_key import IP_LOG
        self.f.mcu_hidraw()
        self.f.runner.ip_journal = IP_LOG
        self.f.ogui_overlay()
        rc, out, _ = self.cli("apply", "--yes", "buttons", "xbox-key", "--xbox-key", "ogui-menu")
        self.assertIn("cannot open the OpenGamepadUI menu", out)

    def test_previous_release_state_is_flagged(self):
        from oxp3_tool.state import STATE_FILE
        self.assertEqual(self.cli("apply", "--yes", "buttons")[0], 0)
        path = self.root / STATE_FILE.lstrip("/")
        state = json.loads(path.read_text())
        state["fixes"]["buttons"]["files"] = [r for r in state["fixes"]["buttons"]["files"]
                                              if r["path"] != templates.KEYS_CONFIG_PATH]
        del state["fixes"]["buttons"]["options"]["actions"]["xbox"]  # the first release had no Xbox entry
        path.write_text(json.dumps(state))
        rc, status, _ = self.cli("status")
        self.assertIn("installed by an older oxp3-tool; run `sudo oxp3-tool apply buttons` to update it", status)
        self.assertNotIn("No key opens the Steam menu", status)  # a missing entry counts as its default

    def test_keys_config_is_ours_after_a_lost_state(self):
        from oxp3_tool.state import STATE_FILE
        self.assertEqual(self.cli("apply", "--yes", "buttons", "--xbox-key", "fps-limit")[0], 0)
        (self.root / STATE_FILE.lstrip("/")).unlink()
        rc, out, _ = self.cli("apply", "--yes", "buttons")
        self.assertNotIn(f"backed up {templates.KEYS_CONFIG_PATH}", out)
        self.assertEqual(self.cli("revert", "--yes", "buttons")[0], 0)
        self.assertFalse((self.root / templates.KEYS_CONFIG_PATH.lstrip("/")).exists())

    def test_default_options_are_not_rewritten(self):
        self.assertEqual(self.cli("apply", "--yes", "buttons")[0], 0)
        from oxp3_tool.state import STATE_FILE
        state = json.loads((self.root / STATE_FILE.lstrip("/")).read_text())
        self.assertNotIn("fps_limit", state["fixes"]["buttons"]["options"])
        rc, out, _ = self.cli("apply", "--dry-run", "buttons")
        self.assertIn("already applied, nothing to change", out)

    def test_revert_removes_the_keys_config(self):
        self.assertEqual(self.cli("apply", "--yes", "buttons")[0], 0)
        self.assertTrue((self.root / templates.KEYS_CONFIG_PATH.lstrip("/")).exists())
        self.assertEqual(self.cli("revert", "--yes", "buttons")[0], 0)
        self.assertFalse((self.root / "etc/oxp3-tool").exists())

    def test_upgrade_from_the_keyboard_only_helper(self):
        """State written when the helper was oxp3-tool-osk-key.service: the old unit is stopped and replaced."""
        from oxp3_tool.state import STATE_FILE
        old_helper, old_unit = "/usr/local/libexec/oxp3-tool/osk-key-helper", "oxp3-tool-osk-key.service"
        self.assertEqual(self.cli("apply", "--yes", "buttons")[0], 0)
        path = self.root / STATE_FILE.lstrip("/")
        text = path.read_text().replace(templates.STEAM_ACTIONS_HELPER_PATH, old_helper)
        path.write_text(text.replace(self.UNIT, old_unit))
        cap_map = self.root / "etc/inputplumber/capability_maps.d/onexplayer_type9.yaml"
        text = cap_map.read_text().replace(
            "# dbus: targets go to oxp3-tool-steam-actions.service, which carries them out in Steam.",
            "# dbus: ui_osk goes to oxp3-tool-osk-key.service, which asks Steam to open its keyboard.")
        cap_map.write_text(text)
        state = json.loads(path.read_text())
        for rec in state["fixes"]["buttons"]["files"]:
            if rec["path"].endswith("onexplayer_type9.yaml"):
                rec["sha256"] = self.f.system().sha256(rec["path"])
        path.write_text(json.dumps(state))
        self.helper.rename(self.root / old_helper.lstrip("/"))
        self.unit.rename(self.root / f"etc/systemd/system/{old_unit}")
        state = json.loads(path.read_text())
        state["fixes"]["buttons"]["files"] = [r for r in state["fixes"]["buttons"]["files"]
                                              if r["path"] != templates.KEYS_CONFIG_PATH]
        path.write_text(json.dumps(state))
        (self.root / templates.KEYS_CONFIG_PATH.lstrip("/")).unlink()
        self.f.runner.unit_enabled = {old_unit: "enabled"}
        self.f.runner.unit_active[old_unit] = "active"
        self.dry_run_log = self.cli("apply", "--dry-run", "buttons")[1]
        self.f.runner.calls.clear()
        rc, out, err = self.cli("apply", "--yes", "buttons")
        self.assertEqual(rc, 0, err)
        calls = self.calls()
        self.assertLess(calls.index(f"systemctl disable --now {old_unit}"), calls.index("systemctl daemon-reload"))
        self.assertIn(f"systemctl enable {self.UNIT}", calls)
        self.assertFalse((self.root / old_helper.lstrip("/")).exists())
        self.assertFalse((self.root / f"etc/systemd/system/{old_unit}").exists())
        self.assertTrue(self.helper.exists())
        self.assertEqual(self.f.runner.unit_active[old_unit], "inactive")
        # Only a comment in the map changed, so InputPlumber is left alone.
        self.assertIn(f"update {'/etc/inputplumber/capability_maps.d/onexplayer_type9.yaml'}", self.dry_run_log)
        self.assertNotIn("systemctl try-restart inputplumber.service", calls)
        self.assertNotIn("Reboot afterwards", out)
        rc, status, _ = self.cli("status")
        self.assertNotIn("InputPlumber has not been restarted", status)

    def test_unit_not_enabled_is_pointed_out(self):
        self.f.ogui_overlay(disabled_for="gamer")  # override from before Bazzite enabled its unit
        rc, out, _ = self.cli("apply", "--yes", "buttons")
        self.assertNotIn("bazzite-native-steam-input.service\n", "\n".join(self.calls()))
        self.assertIn("is not enabled for gamer", out)
        self.assertIn("Reboot afterwards", out)

    def test_inputplumber_restart_restores_the_deck_controller(self):
        self.f.ogui_overlay(disabled_for="gamer")  # Bazzite's native Steam Input setup for user gamer
        self.f.write("/var/home/gamer/.config/systemd/user/default.target.wants/bazzite-native-steam-input.service", "")
        restore = "systemctl --user -M gamer@ --no-block restart bazzite-native-steam-input.service"
        rc, out, err = self.cli("apply", "--yes", "buttons")
        self.assertEqual(rc, 0, err)
        calls = self.calls()
        self.assertLess(calls.index("systemctl try-restart inputplumber.service"), calls.index(restore))
        self.assertIn("If the buttons act like an Xbox controller in Game Mode afterwards, reboot", out)
        self.assertNotIn("Reboot afterwards", out)
        # Not when InputPlumber is left alone, and again after the revert's restart.
        self.f.runner.calls.clear()
        self.assertEqual(self.cli("apply", "--yes", "buttons", "--fps-limit", "48")[0], 0)
        self.assertNotIn(restore, self.calls())
        self.f.runner.calls.clear()
        self.assertEqual(self.cli("revert", "--yes", "buttons")[0], 0)
        calls = self.calls()
        self.assertLess(calls.index("systemctl try-restart inputplumber.service"), calls.index(restore))

    def test_revert_stops_the_previous_releases_helper(self):
        self.assertEqual(self.cli("apply", "--yes", "buttons")[0], 0)
        old_unit = templates.LEGACY_STEAM_ACTIONS_UNITS[0]
        self.f.runner.unit_active[old_unit] = "active"
        self.f.runner.calls.clear()
        self.assertEqual(self.cli("revert", "--yes", "buttons")[0], 0)
        self.assertIn(f"systemctl stop {old_unit}", self.calls())
        self.assertIn("systemctl daemon-reload", self.calls())

    def test_diagnose_records_steam_and_decky(self):
        from oxp3_tool import diagnose
        from oxp3_tool.detect import Context
        pkg = "/var/home/gamer/.local/share/Steam/package"
        self.f.write(f"{pkg}/beta", "steamdeck_publicbeta\n")
        self.f.write(f"{pkg}/steam_client_ubuntu12.manifest", '"ubuntu12"\n{\n\t"version"\t\t"1791918634"\n}\n')
        self.f.write("/var/home/gamer/.local/share/Steam/logs/bootstrap_log.txt", "line 1\nUpdate complete\n")
        self.f.symlink("/home", "var/home")  # as on Bazzite: listed once
        self.f.write("/etc/secret", "root only\n")
        self.f.symlink("/var/home/gamer/.local/share/Steam/package/steam_client_linux.manifest", "/etc/secret")
        bundle, _ = diagnose.collect(Context(self.f.system()), "status")
        steam = bundle.files["steam-client.txt"].decode()
        self.assertEqual(steam.count("beta=steamdeck_publicbeta"), 1)
        self.assertNotIn("root only", steam)  # never through a symlink
        self.assertIn("beta=steamdeck_publicbeta", steam)
        self.assertIn("steam_client_ubuntu12.manifest: version 1791918634", steam)
        self.assertIn("    Update complete", steam)
        self.assertIn("commands/journal-decky-loader-this-boot.txt", bundle.files)

    def test_missing_busctl(self):
        (self.root / "usr/bin/busctl").unlink()
        rc, _, err = self.cli("apply", "--yes", "buttons")
        self.assertNotEqual(rc, 0)
        self.assertIn("choose button actions", err)
        self.assertEqual(self.cli("apply", "--yes", "buttons", "--keyboard-key", "keyboard-chord")[0], 0)


if __name__ == "__main__":
    unittest.main()
