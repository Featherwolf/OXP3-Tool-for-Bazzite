import json
import tempfile
import unittest
from pathlib import Path

from oxp3_tool import templates
from oxp3_tool.detect import Context
from oxp3_tool.engine import Engine, EngineError
from oxp3_tool.fixes import ALL_FIXES, BY_ID
from oxp3_tool.state import STATE_FILE, State, StateError

from .fakeroot import OXP3_DMI, FakeOxp3

CAP_MAP = "/etc/inputplumber/capability_maps.d/onexplayer_type9.yaml"
HWDB = "/etc/udev/hwdb.d/61-oxp3-tool-volume-keys.hwdb"
LUA = "/etc/gamescope/scripts/90-oxp3-tool-oled.lua"


def recommended(ctx):
    return [f for f in ALL_FIXES if f.check(ctx).applicable and f.check(ctx).recommended]


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def fake(self, **kw):
        self.f = FakeOxp3(self.root, **kw)
        return self.f

    def engine(self, force=False):
        ctx = Context(self.f.system(), force=force)
        return Engine(ctx, State(self.f.system()), out=lambda _: None), ctx

    def read(self, path):
        p = self.root / path.lstrip("/")
        return p.read_text() if p.exists() else None

    def state(self):
        text = self.read(STATE_FILE)
        return json.loads(text) if text else None

    def apply(self, ids=None, **opts):
        eng, ctx = self.engine()
        fixes = [BY_ID[i] for i in ids] if ids else recommended(ctx)
        return eng.apply(eng.plan(fixes, opts))

    def revert(self, ids):
        eng, _ = self.engine()
        return eng.revert(ids)

    # -- apply ---------------------------------------------------------------

    def test_recommended_set(self):
        self.fake()
        _, ctx = self.engine()
        self.assertEqual([f.id for f in recommended(ctx)], ["buttons", "volume-keys", "nvme-resume", "oled-profile"])
        self.fake(gm7=False)
        _, ctx = self.engine()
        self.assertNotIn("nvme-resume", [f.id for f in recommended(ctx)])

    def test_apply_recommended(self):
        self.fake()
        out = self.apply()
        self.assertTrue(out.ok, out.errors)
        self.assertTrue(out.changed)
        self.assertTrue(out.reboot_needed)
        self.assertIn("button: Guide", self.read(CAP_MAP))
        self.assertEqual(self.read(HWDB), templates.VOLUME_HWDB)
        self.assertIn("oxp3_tool_sdc_oled", self.read(LUA))
        self.assertIn("nvme.noacpi=1", self.f.runner.kargs)
        st = self.state()["fixes"]
        self.assertEqual(set(st), {"buttons", "volume-keys", "nvme-resume", "oled-profile"})
        self.assertEqual(st["nvme-resume"]["kargs"], ["nvme.noacpi=1"])
        self.assertEqual(st["buttons"]["options"]["actions"]["home"], "guide")
        calls = [" ".join(c) for c in self.f.runner.calls]
        self.assertIn("rpm-ostree kargs --append-if-missing=nvme.noacpi=1", calls)
        self.assertIn("systemctl try-restart inputplumber.service", calls)
        self.assertIn("systemd-hwdb update", calls)
        self.assertIn("udevadm trigger --action=change --parent-match=/sys/devices/platform/i8042/serio0", calls)
        # No temporary files left behind (InputPlumber would choke on them).
        self.assertEqual(sorted(p.name for p in (self.root / "etc/inputplumber/capability_maps.d").iterdir()),
                         ["onexplayer_type9.yaml"])

    def test_apply_is_idempotent(self):
        self.fake()
        self.apply()
        first = self.state()
        self.f.runner.calls.clear()
        out = self.apply()
        self.assertTrue(out.ok)
        self.assertFalse(out.changed)
        self.assertEqual(self.state(), first)
        calls = [" ".join(c) for c in self.f.runner.calls]
        self.assertFalse([c for c in calls if c.startswith("rpm-ostree kargs --")])
        self.assertNotIn("systemctl try-restart inputplumber.service", calls)

    def test_reapply_with_new_options_updates(self):
        self.fake()
        self.apply(["buttons"])
        out = self.apply(["buttons"], home="quick-access", keyboard_key="screenshot")
        self.assertTrue(out.changed)
        text = self.read(CAP_MAP)
        self.assertIn("button: QuickAccess2", text)
        self.assertEqual(self.state()["fixes"]["buttons"]["files"][0]["backup"], None)

    def test_existing_file_is_backed_up_and_restored(self):
        self.fake()
        self.f.write(CAP_MAP, "version: 1\nid: oxp9\n# mine\n")
        self.apply(["buttons"])
        backup = self.state()["fixes"]["buttons"]["files"][0]["backup"]
        self.assertEqual(self.read(backup), "version: 1\nid: oxp9\n# mine\n")
        self.revert(["buttons"])
        self.assertEqual(self.read(CAP_MAP), "version: 1\nid: oxp9\n# mine\n")
        self.assertIsNone(self.read(backup))
        self.assertIsNone(self.state())

    def test_preexisting_karg_is_not_removed(self):
        self.fake(kargs=["rhgb", "nvme.noacpi=1"])
        out = self.apply(["nvme-resume"])
        self.assertTrue(out.ok)
        entry = self.state()["fixes"]["nvme-resume"]
        self.assertEqual(entry["kargs"], [])
        self.assertEqual(entry["notes"]["kargs_already_set"], ["nvme.noacpi=1"])
        self.revert(["nvme-resume"])
        self.assertIn("nvme.noacpi=1", self.f.runner.kargs)
        self.assertIsNone(self.state())

    def test_kargs_are_batched(self):
        self.fake()
        out = self.apply(["nvme-resume", "xe-dsb-off", "xe-panel-replay-off"])
        self.assertTrue(out.ok)
        kcalls = [c for c in self.f.runner.calls if c[:2] == ["rpm-ostree", "kargs"] and len(c) > 2]
        self.assertEqual(len(kcalls), 1)
        self.assertEqual(len(kcalls[0]), 5)

    def test_kargs_failure_records_nothing(self):
        self.fake()
        self.f.runner.fail.add("rpm-ostree kargs --append")
        out = self.apply(["nvme-resume"])
        self.assertFalse(out.ok)
        self.assertIsNone(self.state())

    def test_busy_rpm_ostree_refuses_before_changing_anything(self):
        self.fake()
        self.f.runner.transaction = ["upgrade"]
        eng, ctx = self.engine()
        plans = eng.plan(recommended(ctx), {})
        with self.assertRaises(EngineError):
            eng.apply(plans)
        self.assertIsNone(self.read(CAP_MAP))
        self.assertIsNone(self.state())

    def test_not_oxp3_refuses(self):
        self.fake(dmi=dict(OXP3_DMI, product_name="ONEXPLAYER X1 A"))
        eng, _ = self.engine()
        with self.assertRaises(EngineError):
            eng.plan([BY_ID["volume-keys"]], {})

    def test_not_bazzite_refuses(self):
        self.fake(os_id="fedora")
        eng, _ = self.engine()
        with self.assertRaises(EngineError):
            eng.plan([BY_ID["volume-keys"]], {})

    def test_nvme_needs_gm7_or_flag(self):
        self.fake(gm7=False)
        eng, _ = self.engine()
        with self.assertRaises(EngineError):
            eng.plan([BY_ID["nvme-resume"]], {})
        self.assertTrue(self.apply(["nvme-resume"], any_ssd=True).ok)

    def test_panel_replay_refused_with_psr_off(self):
        self.fake(kargs=["rhgb", "xe.enable_psr=0"])
        eng, _ = self.engine()
        with self.assertRaises(EngineError) as cm:
            eng.plan([BY_ID["xe-panel-replay-off"]], {})
        self.assertIn("blanks", str(cm.exception))

    def test_buttons_not_applicable(self):
        self.fake(inputplumber=False)
        self.assertFalse(BY_ID["buttons"].check(self.engine()[1]).applicable)
        self.fake()
        self.f.runner.hhd_units = ["hhd@gamer.service"]
        check = BY_ID["buttons"].check(self.engine()[1])
        self.assertFalse(check.applicable)
        self.assertIn("Handheld Daemon", check.reason)

    def test_buttons_refuses_unknown_map_id(self):
        self.fake()
        self.f.write("/usr/share/inputplumber/devices/50-onexplayer_3.yaml",
                     "product_name: ONEXPLAYER 3\ncapability_map_id: oxp10\n")
        check = BY_ID["buttons"].check(self.engine()[1])
        self.assertFalse(check.applicable)

    def test_oled_profile_needs_known_panel(self):
        from .fakeroot import make_edid
        self.fake(edid=make_edid("BOE", 0x1234, None))
        self.assertFalse(BY_ID["oled-profile"].check(self.engine()[1]).applicable)

    def test_gyro(self):
        self.fake()
        self.assertTrue(BY_ID["gyro"].check(self.engine()[1]).applicable)
        self.fake(iio=["bmi260"])
        self.assertFalse(BY_ID["gyro"].check(self.engine()[1]).applicable)
        self.fake()
        self.f.runner.bmi270_aliases = "acpi*:BMI0260:*\n"
        self.assertFalse(BY_ID["gyro"].check(self.engine()[1]).applicable)

    def test_unit_enable_and_revert(self):
        self.fake()
        self.apply(["inputplumber-suspend"])
        self.assertEqual(self.f.runner.unit_enabled["inputplumber-suspend.service"], "enabled")
        self.revert(["inputplumber-suspend"])
        self.assertEqual(self.f.runner.unit_enabled["inputplumber-suspend.service"], "disabled")

    def test_unit_already_enabled_is_left_alone(self):
        self.fake()
        self.f.runner.unit_enabled["inputplumber-suspend.service"] = "enabled"
        self.apply(["inputplumber-suspend"])
        self.revert(["inputplumber-suspend"])
        self.assertEqual(self.f.runner.unit_enabled["inputplumber-suspend.service"], "enabled")

    # -- revert --------------------------------------------------------------

    def test_revert_all(self):
        self.fake()
        self.apply()
        out = self.revert([f.id for f in ALL_FIXES])
        self.assertTrue(out.ok, out.errors)
        for path in (CAP_MAP, HWDB, LUA):
            self.assertIsNone(self.read(path), path)
        self.assertNotIn("nvme.noacpi=1", self.f.runner.kargs)
        self.assertIsNone(self.state())
        self.assertFalse((self.root / "var/lib/oxp3-tool/backups").exists())

    def test_revert_keeps_copy_of_edited_file(self):
        self.fake()
        self.apply(["volume-keys"])
        self.f.write(HWDB, "edited\n")
        out = self.revert(["volume-keys"])
        self.assertIsNone(self.read(HWDB))
        self.assertEqual(self.read("/var/lib/oxp3-tool/modified/volume-keys" + HWDB), "edited\n")
        self.assertTrue(any("edited" in m for m in out.messages))

    def test_revert_kargs_failure_keeps_record(self):
        self.fake()
        self.apply(["nvme-resume"])
        self.f.runner.fail.add("rpm-ostree kargs --delete")
        out = self.revert(["nvme-resume"])
        self.assertFalse(out.ok)
        self.assertEqual(self.state()["fixes"]["nvme-resume"]["kargs"], ["nvme.noacpi=1"])

    def test_corrupt_state_is_an_error(self):
        self.fake()
        self.f.write(STATE_FILE, "{not json")
        with self.assertRaises(StateError):
            State(self.f.system())


if __name__ == "__main__":
    unittest.main()
