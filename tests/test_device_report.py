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
        self.assertIn("logged a suspend and nothing after it", out)

    def test_finished_resume_is_not_flagged(self):
        self.fake(gm7=False)
        self.f.runner.journal["-1"] = SUSPEND_NO_EXIT + "[  760.000] PM: suspend exit\n"
        rc, out, _ = self.cli("status")
        self.assertNotIn("nothing after it", out)

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
        self.assertIn("Intel HDR backlight interface v1, PQ decode", check.reason)
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

    def test_unreadable_panel_is_still_available(self):
        self.fake()
        check = self.check()
        self.assertTrue(check.applicable)
        self.assertIn("could not read", check.reason)

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
