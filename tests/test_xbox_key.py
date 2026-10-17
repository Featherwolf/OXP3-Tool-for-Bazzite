import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from oxp3_tool import templates, xbox_helper
from oxp3_tool.cli import main
from oxp3_tool.detect import Context
from oxp3_tool.fixes import BY_ID

from .fakeroot import FakeOxp3

ROOT = Path(__file__).resolve().parent.parent
NO_CONFIG = "/nonexistent/oxp3-tool/keys.json"

# Frames from a real OXP3 (InputPlumber's log): the Xbox key as vendor button 0x25.
XBOX_PRESS = bytes.fromhex("b23f01011f802502016f00000100000000") + bytes(45) + b"\x3f\xb2"
XBOX_RELEASE = bytes.fromhex("b23f01011f802502016f00000200000000") + bytes(45) + b"\x3f\xb2"
HOME_PRESS = bytes.fromhex("b23f01011f802402020500000100000000") + bytes(45) + b"\x3f\xb2"
IP_LOG = ("[2026-10-16T17:48:31Z WARN  inputplumber::drivers::oxp_hid::driver] OXP HID: failed to parse B2 report: "
          "InvalidValue: [b2 3f 01 01 1f 80 25 02 01 6f 00 00 01 00 00 00]\n")


class HelperTest(unittest.TestCase):
    def test_parse_report(self):
        self.assertEqual(len(XBOX_PRESS), 64)
        self.assertIs(xbox_helper.parse_report(XBOX_PRESS), True)
        self.assertIs(xbox_helper.parse_report(XBOX_RELEASE), False)
        self.assertIsNone(xbox_helper.parse_report(HOME_PRESS))
        self.assertIsNone(xbox_helper.parse_report(XBOX_PRESS[:63]))
        self.assertIsNone(xbox_helper.parse_report(b"\xb8" + XBOX_PRESS[1:]))
        self.assertIsNone(xbox_helper.parse_report(XBOX_PRESS[:62] + b"\x00\xb2"))

    def test_find_hidraw(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = FakeOxp3(Path(tmp))
            self.assertIsNone(xbox_helper.find_hidraw(tmp))
            f.mcu_hidraw(moved=True)
            self.assertEqual(xbox_helper.find_hidraw(tmp), os.path.join(tmp, "dev/inputplumber/sources/hidraw2"))
            self.assertEqual(Context(f.system()).oxp_vendor_hidraw, "/dev/inputplumber/sources/hidraw2")
        with tempfile.TemporaryDirectory() as tmp:
            FakeOxp3(Path(tmp)).mcu_hidraw(moved=False)
            self.assertEqual(xbox_helper.find_hidraw(tmp), os.path.join(tmp, "dev/hidraw2"))

    def test_sender_looks_up_device_again_after_a_failure(self):
        calls = []
        replies = {"tree": ["/org/shadowblip/InputPlumber/CompositeDevice0\n"
                            "/org/shadowblip/InputPlumber/CompositeDevice1\n"]}

        def fake_busctl(*args):
            calls.append(args)
            if args[0] == "--list":
                return replies["tree"].pop(0) if replies["tree"] else None
            if args[0] == "get-property":
                return 's "ONEXPLAYER 3"\n' if args[2].endswith("1") else 's "Keyboard"\n'
            if args[0] == "call":
                return "" if len([c for c in calls if c[0] == "call"]) > 1 else None
            return None

        replies["tree"].append("/org/shadowblip/InputPlumber/CompositeDevice1\n")
        with mock.patch.object(xbox_helper, "busctl", fake_busctl), contextlib.redirect_stderr(io.StringIO()):
            sender = xbox_helper.Sender(config_path=NO_CONFIG)
            sender.send(True)
        sends = [c for c in calls if c[0] == "call"]
        self.assertEqual(len(sends), 2)  # first failed (InputPlumber restarted), looked up again, sent
        # SendEvent panics inside InputPlumber 0.82; SendButtonChord gives a tap.
        self.assertEqual(sends[-1][2:], ("/org/shadowblip/InputPlumber/CompositeDevice1",
                                         xbox_helper.IFACE, "SendButtonChord", "as", "1", "Gamepad:Button:Guide"))
        self.assertTrue(sender.pressed)

    def test_one_tap_per_press(self):
        calls = []

        def fake_busctl(*args):
            calls.append(args)
            if args[0] == "--list":
                return "/org/shadowblip/InputPlumber/CompositeDevice0\n"
            if args[0] == "get-property":
                return 's "ONEXPLAYER 3"\n'
            return ""

        with mock.patch.object(xbox_helper, "busctl", fake_busctl), contextlib.redirect_stderr(io.StringIO()):
            sender = xbox_helper.Sender(config_path=NO_CONFIG)
            for state in (True, True, False, False, True, False):
                sender.send(state)
        self.assertEqual(len([c for c in calls if c[0] == "call"]), 2)
        self.assertEqual(len([c for c in calls if c[0] == "--list"]), 1)  # the device path is kept

    def test_failure_is_logged_with_the_marker_status_reads(self):
        stderr = io.StringIO()
        with mock.patch.object(xbox_helper, "busctl", lambda *a: None), contextlib.redirect_stderr(stderr):
            sender = xbox_helper.Sender(config_path=NO_CONFIG)
            sender.send(True)
        self.assertIn(f"failed: {xbox_helper.SEND_FAILED}", stderr.getvalue())
        self.assertTrue(sender.pressed)  # tracked from the frames, so the release is not mistaken for a press

    def test_installed_script_is_standalone(self):
        script = templates.xbox_key_helper_script()
        self.assertTrue(script.startswith("#!/usr/bin/python3 -I\n# Installed by oxp3-tool ("))
        self.assertNotIn("oxp3_tool", script.split("\n", 3)[3])
        compile(script, templates.XBOX_KEY_HELPER_PATH, "exec")

    def chords(self, config, presses=1, clock=None):
        """SendButtonChord calls for ``presses`` presses with the buttons fix's ``config``."""
        calls = []

        def fake_busctl(*args):
            calls.append(args)
            if args[0] == "--list":
                return "/org/shadowblip/InputPlumber/CompositeDevice0\n"
            if args[0] == "get-property":
                return 's "ONEXPLAYER 3"\n'
            return ""

        actions = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "keys.json")
            if config is not None:
                Path(path).write_text(json.dumps(config))
            with mock.patch.object(xbox_helper, "busctl", fake_busctl), contextlib.redirect_stderr(io.StringIO()):
                sender = xbox_helper.Sender(actions, config_path=path)
                for _ in range(presses):
                    sender.send(True)
                    sender.send(False)
        return [c[4:] for c in calls if c[0] == "call"], actions

    def test_actions_from_the_buttons_fix(self):
        self.assertEqual(self.chords(None)[0], [("SendButtonChord", "as", "1", "Gamepad:Button:Guide")])
        self.assertEqual(self.chords({"xbox": "screenshot"})[0],
                         [("SendButtonChord", "as", "1", "Gamepad:Button:Screenshot")])
        self.assertEqual(self.chords({"xbox": "keyboard-chord"})[0],
                         [("SendButtonChord", "as", "2", "Gamepad:Button:Guide", "Gamepad:Button:North")])
        self.assertEqual(self.chords({"xbox": "ogui-menu", "session": "native"})[0],
                         [("SendButtonChord", "as", "1", "Gamepad:Button:Screenshot")])
        # A chord skips InputPlumber's intercept, so the OGUI menu cannot be opened this way.
        self.assertEqual(self.chords({"xbox": "ogui-menu", "session": "ogui"})[0], [])
        self.assertEqual(self.chords({"xbox": "no-such-action"})[0],
                         [("SendButtonChord", "as", "1", "Gamepad:Button:Guide")])
        for action in ("home", "keyboard", "fps-limit"):
            calls, actions = self.chords({"xbox": action})
            self.assertEqual(calls, [])
            actions.perform.assert_called_once_with(action)

    def test_steam_actions_are_debounced(self):
        with mock.patch.object(xbox_helper.time, "monotonic", return_value=50.0):
            calls, actions = self.chords({"xbox": "fps-limit"}, presses=3)
        self.assertEqual(actions.perform.call_count, 1)

    def test_script_comes_out_of_the_zipapp(self):
        with tempfile.TemporaryDirectory() as tmp:
            zipapp = Path(tmp, "oxp3-tool")
            subprocess.run([sys.executable, "-I", str(ROOT / "tools/build_zipapp.py"), "-o", str(zipapp)],
                           check=True, capture_output=True)
            code = ("import sys; sys.path.insert(0, sys.argv[1]); from oxp3_tool import templates; "
                    "print(templates.xbox_key_helper_script() == open(sys.argv[2]).read())")
            expected = Path(tmp, "expected")
            expected.write_text(templates.xbox_key_helper_script())
            res = subprocess.run([sys.executable, "-I", "-c", code, str(zipapp), str(expected)],
                                 capture_output=True, text=True, check=True)
            self.assertEqual(res.stdout.strip(), "True")


class XboxKeyFixTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.f = FakeOxp3(self.root)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(list(argv), system=self.f.system())
        return rc, out.getvalue(), err.getvalue()

    def test_check(self):
        fix = BY_ID["xbox-key"]
        self.assertFalse(fix.check(Context(self.f.system())).applicable)
        self.f.mcu_hidraw()
        check = fix.check(Context(self.f.system()))
        self.assertTrue(check.applicable)
        self.assertFalse(check.recommended)
        self.f.runner.ip_journal = IP_LOG
        check = fix.check(Context(self.f.system()))
        self.assertTrue(check.recommended)
        self.assertIn("button 0x25", check.reason)
        self.f.runner.hhd_units = ["hhd@gamer.service"]
        self.assertFalse(fix.check(Context(self.f.system())).applicable)

    def test_apply_and_revert(self):
        self.f.mcu_hidraw()
        self.f.runner.ip_journal = IP_LOG
        rc, out, err = self.cli("apply", "--yes", "xbox-key")
        self.assertEqual(rc, 0, err)
        helper = self.root / templates.XBOX_KEY_HELPER_PATH.lstrip("/")
        unit = self.root / templates.XBOX_KEY_UNIT_PATH.lstrip("/")
        self.assertEqual(helper.read_text(), templates.xbox_key_helper_script())
        self.assertEqual(helper.stat().st_mode & 0o777, 0o755)
        self.assertIn(f"ExecStart=/usr/bin/python3 -I {templates.XBOX_KEY_HELPER_PATH}", unit.read_text())
        calls = [" ".join(c) for c in self.f.runner.calls]
        self.assertIn(f"systemctl enable {templates.XBOX_KEY_UNIT_NAME}", calls)
        self.assertLess(calls.index("systemctl daemon-reload"),
                        calls.index(f"systemctl restart {templates.XBOX_KEY_UNIT_NAME}"))
        rc, out, _ = self.cli("status")
        self.assertIn("[applied     ] xbox-key", out)
        self.assertIn(f"{templates.XBOX_KEY_UNIT_NAME} is active", out)

        self.f.runner.calls.clear()
        rc, out, err = self.cli("revert", "--yes", "xbox-key")
        self.assertEqual(rc, 0, err)
        calls = [" ".join(c) for c in self.f.runner.calls]
        self.assertIn(f"systemctl disable {templates.XBOX_KEY_UNIT_NAME}", calls)
        self.assertIn(f"systemctl stop {templates.XBOX_KEY_UNIT_NAME}", calls)
        self.assertFalse(helper.exists())
        self.assertFalse(unit.exists())
        self.assertFalse((self.root / "usr/local/libexec/oxp3-tool").exists())
        self.assertEqual(self.f.runner.unit_enabled[templates.XBOX_KEY_UNIT_NAME], "disabled")

    def test_status_reports_delivery_failures_and_stale_helper(self):
        self.f.mcu_hidraw()
        self.f.runner.ip_journal = IP_LOG
        self.assertEqual(self.cli("apply", "--yes", "xbox-key")[0], 0)
        real_run = self.f.runner.__call__

        def runner(argv, timeout=None):
            if argv[0] == "journalctl" and templates.XBOX_KEY_UNIT_NAME in argv:
                from oxp3_tool.system import CommandResult
                return CommandResult(0, f"failed: {xbox_helper.SEND_FAILED}\n")
            return real_run(argv, timeout)

        from oxp3_tool.system import System
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["status"], system=System(self.root, runner))
        self.assertIn("the last Xbox key press failed (could not send the button to InputPlumber)", out.getvalue())
        # An older tool's helper: same recorded sha as on disk, different from the current template.
        with mock.patch.object(templates, "xbox_key_helper_script", lambda: "#!/usr/bin/python3 -I\n# new\n"):
            rc, out, _ = self.cli("status")
            self.assertIn("installed by an older oxp3-tool", out)
            rc, out, _ = self.cli("apply", "--dry-run")
            self.assertIn(f"update {templates.XBOX_KEY_HELPER_PATH}", out)

    def test_recommended_apply_includes_it_when_needed(self):
        self.f.mcu_hidraw()
        rc, out, _ = self.cli("apply", "--dry-run")
        self.assertNotIn(templates.XBOX_KEY_HELPER_PATH, out)
        self.f.runner.ip_journal = IP_LOG
        rc, out, _ = self.cli("apply", "--dry-run")
        self.assertIn(templates.XBOX_KEY_HELPER_PATH, out)


class ButtonsAdviceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.f = FakeOxp3(Path(tmp.name))

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = main(list(argv), system=self.f.system())
        return rc, out.getvalue()

    def test_ogui_keyboard_key_warning(self):
        self.f.ogui_overlay()
        self.cli("apply", "--yes", "buttons")
        _, out = self.cli("status")
        self.assertIn("OpenGamepadUI issue #549", out)

    def test_steam_screen_reader_warning(self):
        self.cli("apply", "--yes", "buttons")
        vdf = "/var/home/jordan/.local/share/Steam/config/config.vdf"
        self.f.write(vdf, '"InstallConfigStore"\n{\n\t"ScreenReaderEnabled"\t\t"0"\n}\n')
        _, out = self.cli("status")
        self.assertNotIn("screen reader", out)
        self.f.write(vdf, '"InstallConfigStore"\n{\n\t"ScreenReaderEnabled"\t\t"1"\n}\n')
        _, out = self.cli("status")
        self.assertIn("warning: Steam's screen reader is on for jordan", out)

    def test_native_apply_says_reboot(self):
        rc, out = self.cli("apply", "--yes", "buttons")
        self.assertEqual(rc, 0)
        self.assertIn("Bazzite selects the Steam Deck controller", out)
        _, out = self.cli("status")
        self.assertNotIn("OpenGamepadUI issue #549", out)


if __name__ == "__main__":
    unittest.main()
