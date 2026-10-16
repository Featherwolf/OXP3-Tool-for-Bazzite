"""Tests for the behaviour reported on a real OXP3: face keys, black screen after
waking (and no further sleep), and HDR blanking the panel."""

import unittest

from oxp3_tool import templates
from oxp3_tool.fixes import BY_ID

from .test_review_regressions import CAP_MAP, Base

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

SIMPLE_SUSPEND = "[    1.900] nvme 0000:01:00.0: platform quirk: setting simple suspend\n"
RESET_FAILURE = "[  812.300] nvme nvme0: Disabling device after reset failure: -19\n"
SUSPEND_NO_EXIT = "[  700.000] PM: suspend entry (s2idle)\n[  700.100] Filesystems sync: 0.010 seconds\n"


class NvmeEvidenceTest(Base):
    def test_reapply_after_fix_hid_the_evidence(self):
        self.fake(gm7=False)
        self.f.runner.journal["0"] = SIMPLE_SUSPEND
        self.assertTrue(self.apply(["nvme-resume"]).ok)
        self.f.boot()
        self.f.runner.journal["0"] = ""  # nvme.noacpi=1 suppresses the message
        self.assertTrue(self.check().recommended)
        rc, _, err = self.cli("apply", "--yes", "nvme-resume", "buttons")
        self.assertEqual(rc, 0, err)
        self.revert(["nvme-resume"])
        self.f.boot()
        rc, _, err = self.cli("apply", "--yes", "nvme-resume")
        self.assertEqual(rc, 1)  # reverted and the evidence is gone: needs --any-ssd again

    def check(self):
        return BY_ID["nvme-resume"].check(self.engine()[1])

    def test_simple_suspend_on_any_ssd_is_recommended(self):
        self.fake(gm7=False)
        self.f.runner.journal["0"] = SIMPLE_SUSPEND
        check = self.check()
        self.assertTrue(check.recommended)
        self.assertIn("simple suspend", check.reason)
        self.assertTrue(self.apply(["nvme-resume"]).ok)
        self.assertIn("nvme.noacpi=1", self.f.runner.kargs)

    def test_reset_failure_in_previous_boot_is_recommended(self):
        self.fake(gm7=False)
        self.f.runner.journal["-1"] = RESET_FAILURE
        self.assertTrue(self.check().recommended)

    def test_unfinished_resume_is_reported_not_recommended(self):
        self.fake(gm7=False)
        self.f.runner.journal["-1"] = SUSPEND_NO_EXIT
        self.assertFalse(self.check().recommended)
        rc, out, _ = self.cli("status")
        self.assertIn("saved log ends during a suspend", out)

    def test_finished_resume_is_not_flagged(self):
        self.fake(gm7=False)
        self.f.runner.journal["-1"] = SUSPEND_NO_EXIT + "[  760.000] PM: suspend exit\n"
        rc, out, _ = self.cli("status")
        self.assertNotIn("ends during a suspend", out)

    def test_no_evidence_needs_flag(self):
        self.fake(gm7=False)
        self.assertFalse(self.check().recommended)
        rc, _, err = self.cli("apply", "--dry-run", "nvme-resume")
        self.assertIn("--any-ssd", err)


class HdrBacklightTest(Base):
    def check(self):
        return BY_ID["xe-hdr-backlight"].check(self.engine()[1])

    def test_offered_when_panel_has_intel_interface(self):
        self.fake()
        self.f.panel_dpcd()
        check = self.check()
        self.assertTrue(check.applicable)
        self.assertFalse(check.recommended)
        self.assertIn("Intel HDR backlight interface v1, PQ decode, SDR brightness over PWM", check.reason)
        self.assertTrue(self.apply(["xe-hdr-backlight"]).ok)
        self.assertIn("xe.enable_dpcd_backlight=3", self.f.runner.kargs)
        rc, out, _ = self.cli("status")
        self.assertIn("Panel:    eDP 1.4, Intel HDR backlight interface v1", out)

    def test_not_offered_without_interface(self):
        self.fake()
        self.f.panel_dpcd(tcon=b"\x00\x00\x00\x00")
        self.assertFalse(self.check().applicable)

    def test_not_offered_on_edp15(self):
        self.fake()
        self.f.panel_dpcd(edp_rev=0x06)
        self.assertFalse(self.check().applicable)

    def test_unreadable_panel_is_refused(self):
        self.fake()
        check = self.check()
        self.assertFalse(check.applicable)
        self.assertIn("could not check the panel", check.reason)

    def test_panel_off_is_unknown_not_negative(self):
        self.fake()
        self.f.panel_dpcd()
        self.f.write("/sys/class/drm/card1-eDP-1/dpms", "Off\n")
        check = self.check()
        self.assertFalse(check.applicable)
        self.assertIn("panel is off", check.reason)

    def test_missing_source_oui_is_unknown(self):
        self.fake()
        self.f.panel_dpcd(tcon=b"\x00\x00\x00\x00")
        regs = bytearray((self.root / "dev/drm_dp_aux0").read_bytes())
        regs[0x300:0x303] = b"\x00\x00\x00"
        (self.root / "dev/drm_dp_aux0").write_bytes(bytes(regs))
        check = self.check()
        self.assertIn("source OUI", check.reason)

    def test_sdr_aux_bit_is_reported(self):
        self.fake()
        self.f.panel_dpcd(tcon=b"\x01\x13\x01\x00")
        self.assertIn("SDR brightness over AUX", self.check().reason)

    def test_conflicting_karg(self):
        self.fake(kargs=["rhgb", "xe.enable_dpcd_backlight=1"])
        self.f.panel_dpcd()
        self.assertFalse(self.check().applicable)

    def test_diagnose_reports_dpcd(self):
        self.fake()
        self.f.panel_dpcd()
        from oxp3_tool import diagnose
        from oxp3_tool.detect import Context
        bundle, _ = diagnose.collect(Context(self.f.system()), "status")
        self.assertIn(b"eDP DPCD: /dev/drm_dp_aux0: eDP 1.4, Intel HDR", bundle.files["hardware.txt"])


@unittest.skipIf(yaml is None, "PyYAML not installed")
class GameModeSessionTest(Base):
    def maps(self):
        doc = yaml.safe_load(self.read(CAP_MAP))
        return {m["name"]: m["target_event"]["gamepad"]["button"] for m in doc["mapping"]}

    def test_ogui_overlay_sends_keyboard_as_lefttop(self):
        self.fake()
        self.f.ogui_overlay()
        out = self.apply(["buttons"])
        self.assertEqual(self.maps(), {"ONEX": "QuickAccess", "Keyboard": "LeftTop", "Home": "Guide"})
        self.assertTrue(any("OpenGamepadUI overlay" in m for m in out.messages))
        rc, status, _ = self.cli("status")
        self.assertIn("Game Mode: Steam behind the OpenGamepadUI overlay", status)

    def test_ogui_home_keyboard_needs_mapping(self):
        self.fake()
        self.f.ogui_overlay()
        self.apply(["buttons"], home="keyboard", xbox_key="ogui-menu")
        self.assertEqual(self.maps()["Home"], "LeftTop")
        self.assertEqual(self.maps()["Xbox"], "QuickAccess2")

    def test_native_steam_input(self):
        self.fake()
        self.f.ogui_overlay(disabled_for="gamer")
        self.apply(["buttons"])
        self.assertEqual(self.maps(), {"ONEX": "QuickAccess", "Keyboard": "Keyboard", "Home": "Guide"})
        rc, status, _ = self.cli("status")
        self.assertIn("Game Mode: native Steam Input", status)

    def test_session_switch_is_reported_and_reapplied(self):
        self.fake()
        self.f.ogui_overlay()
        self.apply(["buttons"])
        self.f.write("/var/home/gamer/.config/gamescope-session-plus/sessions.d/ogui-steam", "x\n")
        rc, status, _ = self.cli("status")
        self.assertIn("written for a different Game Mode input", status)
        self.apply(["buttons"])
        self.assertEqual(self.maps()["Keyboard"], "Keyboard")


class OlderStateTest(Base):
    def test_map_from_older_version_is_flagged_under_ogui(self):
        self.fake()
        self.f.ogui_overlay()
        self.apply(["buttons"])
        import json
        from oxp3_tool.state import STATE_FILE
        data = json.loads(self.read(STATE_FILE))
        del data["fixes"]["buttons"]["notes"]["game_mode_input"]  # written before session detection
        self.f.write(STATE_FILE, json.dumps(data))
        rc, out, _ = self.cli("status")
        self.assertIn("written for a different Game Mode input", out)

    def test_diagnose_explains_unchecked_panel(self):
        self.fake()
        self.f.panel_dpcd()
        self.f.write("/sys/class/drm/card1-eDP-1/dpms", "Off\n")
        from oxp3_tool import diagnose
        from oxp3_tool.detect import Context
        bundle, _ = diagnose.collect(Context(self.f.system()), "status")
        self.assertIn(b"eDP DPCD: not checked: the panel is off", bundle.files["hardware.txt"])


@unittest.skipIf(yaml is None, "PyYAML not installed")
class XboxKeyTest(Base):
    def render(self, **actions):
        merged = dict(templates.DEFAULT_BUTTON_ACTIONS, **actions)
        doc = yaml.safe_load(templates.capability_map_yaml("oxp9", merged, None, None))
        return {m["name"]: m for m in doc["mapping"]}

    def test_default_leaves_xbox_alone(self):
        self.assertNotIn("Xbox", self.render())

    def test_xbox_remap(self):
        maps = self.render(xbox="quick-access")
        self.assertEqual(maps["Xbox"]["source_events"], [{"gamepad": {"button": "Guide"}}])
        self.assertEqual(maps["Xbox"]["target_event"], {"gamepad": {"button": "QuickAccess"}})
        self.assertEqual(maps["Home"]["target_event"], {"gamepad": {"button": "Guide"}})

    def test_cli_option(self):
        self.fake()
        rc, _, err = self.cli("apply", "--yes", "buttons", "--xbox-key", "keyboard")
        self.assertEqual(rc, 0, err)
        self.assertIn("name: Xbox", self.read(CAP_MAP))
        self.assertEqual(self.state()["buttons"]["options"]["actions"]["xbox"], "keyboard")

    def test_old_state_without_xbox_key(self):
        self.fake()
        self.apply(["buttons"])
        entry = self.state()["buttons"]
        self.assertEqual(entry["options"]["actions"]["xbox"], "guide")


if __name__ == "__main__":
    unittest.main()
