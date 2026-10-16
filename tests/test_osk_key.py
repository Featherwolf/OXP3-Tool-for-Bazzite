import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from oxp3_tool import osk_helper, templates
from oxp3_tool.cli import main
from oxp3_tool.system import CommandResult, System

from .fakeroot import FakeOxp3

IP_OWNER = ":1.42"


def signal_line(action="ui_osk", value=1.0, sender=IP_OWNER, **extra):
    msg = {"type": "signal", "sender": sender, "path": "/org/shadowblip/InputPlumber/devices/target/dbus0",
           "interface": "org.shadowblip.Input.DBusDevice", "member": "InputEvent",
           "payload": {"type": "sd", "data": [action, value]}}
    msg.update(extra)
    return json.dumps(msg) + "\n"


class ParseTest(unittest.TestCase):
    def test_parse_event(self):
        self.assertEqual(osk_helper.parse_event(signal_line()), (IP_OWNER, "ui_osk", 1.0))
        self.assertEqual(osk_helper.parse_event(signal_line(value=0)), (IP_OWNER, "ui_osk", 0.0))
        self.assertEqual(osk_helper.parse_event(signal_line(sender=None)), ("", "ui_osk", 1.0))
        for bad in ("", "not json", "[]", "null", signal_line(member="TouchEvent"),
                    signal_line(interface="org.example"), signal_line(type="method_call"),
                    signal_line(payload=["sd"]), signal_line(payload=None), signal_line(payload={"type": "sd"}),
                    signal_line(payload={"type": "s", "data": ["ui_osk", 1.0]}),
                    signal_line(value=True), signal_line(value="1"), signal_line(action=7)):
            self.assertIsNone(osk_helper.parse_event(bad), bad)


class WatcherTest(unittest.TestCase):
    def setUp(self):
        self.opened = []
        patches = [
            mock.patch.object(osk_helper, "name_owner", return_value=IP_OWNER),
            mock.patch.object(osk_helper, "steam_uids", return_value=[1000]),
            mock.patch.object(osk_helper, "open_keyboard", side_effect=self.opened.append),
            mock.patch.object(osk_helper, "log"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.clock = 100.0
        p = mock.patch.object(osk_helper.time, "monotonic", side_effect=lambda: self.clock)
        p.start()
        self.addCleanup(p.stop)

    def test_press_opens_release_does_not(self):
        w = osk_helper.Watcher()
        self.assertTrue(w.handle(signal_line()))
        self.assertFalse(w.handle(signal_line(value=0.0)))
        self.assertFalse(w.handle(signal_line(action="ui_guide")))
        self.assertEqual(self.opened, [[1000]])

    def test_debounce(self):
        w = osk_helper.Watcher()
        self.assertTrue(w.handle(signal_line()))
        self.clock += 0.1
        self.assertFalse(w.handle(signal_line()))
        self.clock += 0.3
        self.assertTrue(w.handle(signal_line()))
        self.assertEqual(len(self.opened), 2)

    def test_only_inputplumber_may_send_it(self):
        self.assertIn("sender='org.shadowblip.InputPlumber'", osk_helper.MATCH)
        w = osk_helper.Watcher()
        self.assertFalse(w.handle(signal_line(sender=":1.99")))
        self.clock += 1
        self.assertFalse(w.handle(signal_line(sender=None)))
        self.assertEqual(self.opened, [])
        # InputPlumber restarted with a new unique name: looked up again.
        osk_helper.name_owner.return_value = ":1.99"
        self.clock += 1
        self.assertTrue(w.handle(signal_line(sender=":1.99")))
        osk_helper.name_owner.return_value = None
        self.clock += 1
        self.assertFalse(w.handle(signal_line(sender=":1.7")))

    def test_owner_lookups_are_rate_limited(self):
        w = osk_helper.Watcher()
        for _ in range(100):
            self.assertFalse(w.handle(signal_line(sender=":1.66")))
        self.assertEqual(osk_helper.name_owner.call_count, 1)
        self.assertEqual(osk_helper.log.call_count, 1)
        self.clock += osk_helper.OWNER_RECHECK
        self.assertTrue(w.handle(signal_line()))  # the real sender still gets through


class WriteTest(unittest.TestCase):
    """The pipe writer that runs as the Steam user."""

    def write(self, path):
        res = subprocess.run([sys.executable, "-I", "-c", osk_helper._WRITE, str(path), osk_helper.PIPE_LINE],
                             capture_output=True, timeout=10)
        return res.returncode

    def test_line_format(self):
        # steam-runtime-tools' _srt_steam_command_via_pipe: fixed program path, then shell-quoted arguments.
        self.assertEqual(osk_helper.PIPE_LINE, "'~/.steam/root/ubuntu12_32/steam' 'steam://open/keyboard'\n")

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
                if code != osk_helper.NOT_READING:
                    break
            t.join(10)
            self.assertEqual(code, 0)
            self.assertEqual(got, [osk_helper.PIPE_LINE.encode()])

    def test_steam_not_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.write(Path(tmp, "steam.pipe")), osk_helper.NOT_READING)
            fifo = Path(tmp, "steam.pipe")
            os.mkfifo(fifo)
            self.assertEqual(self.write(fifo), osk_helper.NOT_READING)  # no reader: ENXIO, never blocks

    def test_refuses_regular_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "steam.pipe")
            path.write_text("keep")
            self.assertEqual(self.write(path), 1)
            self.assertEqual(path.read_text(), "keep")


class OpenKeyboardTest(unittest.TestCase):
    def run_open(self, uids, codes):
        err = io.StringIO()
        with mock.patch.object(osk_helper, "send_as", side_effect=codes), contextlib.redirect_stderr(err):
            ok = osk_helper.open_keyboard(uids)
        return ok, err.getvalue()

    def test_results(self):
        ok, log = self.run_open([], [])
        self.assertFalse(ok)
        self.assertIn("Steam is not running", log)
        self.assertNotIn(osk_helper.SEND_FAILED, log)
        ok, log = self.run_open([1000, 1001], [1, 0])
        self.assertTrue(ok)
        self.assertIn(osk_helper.SENT + " (uid 1001)", log)
        ok, log = self.run_open([1000], [osk_helper.NOT_READING])
        self.assertFalse(ok)
        self.assertIn(osk_helper.SEND_FAILED + ": Steam is not reading", log)
        ok, log = self.run_open([1000], [1])
        self.assertIn(osk_helper.SEND_FAILED + ": writing", log)

    def test_send_as_drops_privileges(self):
        pw = mock.Mock(pw_dir="/var/home/gamer", pw_gid=1000)
        with mock.patch.object(osk_helper.pwd, "getpwuid", return_value=pw), \
                mock.patch.object(osk_helper.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(osk_helper.send_as(1000), 0)
        argv = run.call_args.args[0]
        kw = run.call_args.kwargs
        self.assertEqual(argv[:3], ["/usr/bin/python3", "-I", "-c"])
        self.assertEqual(argv[4:], ["/var/home/gamer/.steam/steam.pipe", osk_helper.PIPE_LINE])
        self.assertEqual((kw["user"], kw["group"], kw["extra_groups"]), (1000, 1000, []))
        self.assertEqual(kw["env"], {"HOME": "/var/home/gamer", "PATH": "/usr/bin"})
        with mock.patch.object(osk_helper.pwd, "getpwuid", side_effect=KeyError):
            self.assertEqual(osk_helper.send_as(4242), 1)

    def test_steam_uids(self):
        with tempfile.TemporaryDirectory() as tmp:
            for pid, comm in (("10", "steam\n"), ("11", "steamwebhelper\n"), ("12", "steam\n"), ("self", "steam\n")):
                Path(tmp, pid).mkdir()
                Path(tmp, pid, "comm").write_text(comm)
            Path(tmp, "13").mkdir()  # exited between listdir and open
            me = os.getuid()
            self.assertEqual(osk_helper.steam_uids(tmp), [] if me == 0 else [me])
            self.assertEqual(osk_helper.steam_uids(os.path.join(tmp, "missing")), [])


class ScriptTest(unittest.TestCase):
    def test_installed_script_is_standalone(self):
        script = templates.osk_key_helper_script()
        self.assertTrue(script.startswith("#!/usr/bin/python3 -I\n# Installed by oxp3-tool ("))
        self.assertNotIn("oxp3_tool", script.split("\n", 4)[4])
        compile(script, templates.OSK_KEY_HELPER_PATH, "exec")

    def test_unit_can_reach_home(self):
        self.assertIn("ProtectHome=read-only", templates.OSK_KEY_UNIT)
        self.assertNotIn("ProtectHome=yes", templates.OSK_KEY_UNIT)
        self.assertIn(f"ExecStart=/usr/bin/python3 -I {templates.OSK_KEY_HELPER_PATH}", templates.OSK_KEY_UNIT)


class OskFixTest(unittest.TestCase):
    UNIT = templates.OSK_KEY_UNIT_NAME

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.f = FakeOxp3(self.root)
        self.helper = self.root / templates.OSK_KEY_HELPER_PATH.lstrip("/")
        self.unit = self.root / templates.OSK_KEY_UNIT_PATH.lstrip("/")

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
        self.assertEqual(self.helper.read_text(), templates.osk_key_helper_script())
        self.assertEqual(self.helper.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.unit.read_text(), templates.OSK_KEY_UNIT)
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
        self.assertIn(f"remove {templates.OSK_KEY_HELPER_PATH}", out)
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
                # journalctl -r --grep A|B -n 1: the newest line matching either marker.
                self.assertEqual(argv[argv.index("--grep") + 1], f"{osk_helper.SEND_FAILED}|{osk_helper.SENT}")
                self.assertIn("-r", argv)
                return CommandResult(0, log[-1] + "\n") if log else CommandResult(1)
            return real_run(argv, timeout)

        warning = "warning: the last keyboard request could not be handed to Steam this boot"
        log.append(osk_helper.SEND_FAILED + ": Steam is not reading ~/.steam/steam.pipe")
        rc, out, _ = self.cli("status", system=System(self.root, runner))
        self.assertIn(warning, out)
        log.append(osk_helper.SENT + " (uid 1000)")  # a later press worked
        rc, out, _ = self.cli("status", system=System(self.root, runner))
        self.assertNotIn(warning, out)
        for marker in (osk_helper.SENT, osk_helper.SEND_FAILED):
            self.assertRegex(marker, r"^[A-Za-z ]+$")  # plain text inside journalctl's regex
        with mock.patch.object(templates, "osk_key_helper_script", lambda: "#!/usr/bin/python3 -I\n# new\n"):
            rc, out, _ = self.cli("status")
            self.assertIn("installed by an older oxp3-tool; run `sudo oxp3-tool apply buttons`", out)
            rc, out, _ = self.cli("apply", "--dry-run")
            self.assertIn(f"update {templates.OSK_KEY_HELPER_PATH}", out)

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

    def test_helper_update_leaves_inputplumber_alone(self):
        self.assertEqual(self.cli("apply", "--yes", "buttons")[0], 0)
        self.f.runner.calls.clear()
        with mock.patch.object(templates, "osk_key_helper_script", lambda: "#!/usr/bin/python3 -I\n# new\n"):
            rc, out, _ = self.cli("apply", "--dry-run", "buttons")
            self.assertIn("then start the on-screen keyboard helper", out)
            self.assertNotIn("restart InputPlumber", out)
            rc, out, err = self.cli("apply", "--yes", "buttons")
        self.assertEqual(rc, 0, err)
        calls = self.calls()
        self.assertNotIn("systemctl try-restart inputplumber.service", calls)
        self.assertIn(f"systemctl restart {self.UNIT}", calls)
        self.assertNotIn("Reboot (or leave Game Mode", out)
        # A map change still restarts InputPlumber, with the reboot note.
        rc, out, _ = self.cli("apply", "--yes", "buttons", "--home", "quick-access")
        self.assertIn("systemctl try-restart inputplumber.service", self.calls())
        self.assertIn("Reboot (or leave Game Mode", out)

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

    def test_lost_state_still_owns_the_unit(self):
        from oxp3_tool.state import STATE_FILE
        self.assertEqual(self.cli("apply", "--yes", "buttons")[0], 0)
        (self.root / STATE_FILE.lstrip("/")).unlink()
        self.assertEqual(self.cli("apply", "--yes", "buttons")[0], 0)
        self.f.runner.calls.clear()
        self.assertEqual(self.cli("apply", "--yes", "buttons", "--keyboard-key", "keyboard-chord")[0], 0)
        self.assertIn(f"systemctl disable --now {self.UNIT}", self.calls())
        self.assertEqual(self.f.runner.unit_active[self.UNIT], "inactive")

    def test_missing_busctl(self):
        (self.root / "usr/bin/busctl").unlink()
        rc, _, err = self.cli("apply", "--yes", "buttons")
        self.assertNotEqual(rc, 0)
        self.assertIn("keyboard-chord", err)
        self.assertEqual(self.cli("apply", "--yes", "buttons", "--keyboard-key", "keyboard-chord")[0], 0)


if __name__ == "__main__":
    unittest.main()
