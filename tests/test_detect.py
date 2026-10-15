import tempfile
import unittest
from pathlib import Path

from oxp3_tool.detect import Context, parse_edid, parse_ranges

from .fakeroot import OXP3_DMI, FakeOxp3, make_edid


class DetectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def fake(self, **kw):
        return FakeOxp3(Path(self.tmp.name), **kw)

    def test_detects_oxp3_on_bazzite(self):
        fake = self.fake()
        ctx = Context(fake.system())
        self.assertTrue(ctx.is_oxp3)
        self.assertTrue(ctx.is_bazzite)
        self.assertEqual(ctx.image_name, "bazzite-deck")
        self.assertEqual(ctx.kernel_release, "7.2.9-ogc3.1.fc44.x86_64")

    def test_other_handheld_is_not_oxp3(self):
        dmi = dict(OXP3_DMI, product_name="ONEXPLAYER X1 A")
        ctx = Context(self.fake(dmi=dmi).system())
        self.assertFalse(ctx.is_oxp3)
        self.assertFalse(ctx.device_ok)
        self.assertTrue(Context(self.fake(dmi=dmi).system(), force=True).device_ok)

    def test_vendor_must_match_too(self):
        ctx = Context(self.fake(dmi=dict(OXP3_DMI, sys_vendor="AOKZOE")).system())
        self.assertFalse(ctx.is_oxp3)

    def test_panel_and_storage(self):
        ctx = Context(self.fake().system())
        name, edid = ctx.internal_panel
        self.assertEqual(name, "card1-eDP-1")
        self.assertEqual((edid.vendor, edid.product, edid.name), ("SDC", 0x4301, "AMS881KB01-0"))
        self.assertTrue(ctx.panel_is_known)
        self.assertTrue(ctx.has_gm7)
        self.assertFalse(Context(self.fake(gm7=False).system()).has_gm7)

    def test_unknown_panel(self):
        ctx = Context(self.fake(edid=make_edid("BOE", 0x0BCA, None)).system())
        self.assertFalse(ctx.panel_is_known)

    def test_inputplumber(self):
        ctx = Context(self.fake().system())
        ip = ctx.inputplumber
        self.assertTrue(ip.installed)
        self.assertEqual(ip.version, "0.82.0")
        self.assertEqual(ip.active, "active")
        self.assertEqual(ip.oxp3_config, "/usr/share/inputplumber/devices/50-onexplayer_3.yaml")
        self.assertEqual(ip.map_id, "oxp9")
        self.assertEqual(ip.upstream_map, "/usr/share/inputplumber/capability_maps/onexplayer_type9.yaml")
        self.assertEqual(ip.effective_map, ip.upstream_map)
        self.assertEqual(ip.problems, [])

    def test_inputplumber_etc_override_wins(self):
        fake = self.fake()
        fake.write("/etc/inputplumber/capability_maps.d/onexplayer_type9.yaml", "version: 1\nid: oxp9\n")
        ip = Context(fake.system()).inputplumber
        self.assertEqual(ip.effective_map, "/etc/inputplumber/capability_maps.d/onexplayer_type9.yaml")
        self.assertEqual(ip.upstream_map, "/usr/share/inputplumber/capability_maps/onexplayer_type9.yaml")

    def test_inputplumber_problems(self):
        fake = self.fake()
        fake.mkdir("/usr/local/share/inputplumber")
        fake.write("/etc/inputplumber/capability_maps.d/README", "x")
        ip = Context(fake.system()).inputplumber
        self.assertEqual(ip.base_path, "/usr/local/share/inputplumber")
        self.assertIsNone(ip.oxp3_config)
        self.assertEqual(len(ip.problems), 2)

    def test_hhd(self):
        fake = self.fake()
        fake.runner.hhd_units = ["hhd@bazzite.service"]
        hhd = Context(fake.system()).hhd
        self.assertTrue(hhd.running)
        self.assertEqual(hhd.active_units, ["hhd@bazzite.service"])
        self.assertFalse(Context(self.fake().system()).hhd.installed)

    def test_atkbd_and_imu(self):
        ctx = Context(self.fake(force_release="174,176,369-370").system())
        self.assertEqual(ctx.atkbd_force_release, {174, 176, 369, 370})
        self.assertEqual(ctx.imu_driver, "bmi160_i2c")
        self.assertTrue(ctx.imu_present)
        self.assertTrue(ctx.module_has_alias("bmi270_i2c", "10EC5280"))

    def test_kargs(self):
        fake = self.fake(kargs=["rhgb", "nvme.noacpi=1"])
        ctx = Context(fake.system())
        self.assertTrue(ctx.karg_active("nvme.noacpi=1"))
        self.assertTrue(ctx.karg_pending("nvme.noacpi=1"))
        self.assertFalse(ctx.karg_pending("nvme.noacpi"))
        self.assertFalse(ctx.rpm_ostree_busy)
        self.assertFalse(ctx.has_staged_deployment)
        fake.runner.staged = True
        fake.runner.transaction = ["upgrade"]
        ctx.invalidate_kargs()
        self.assertTrue(ctx.has_staged_deployment)
        self.assertTrue(ctx.rpm_ostree_busy)


class ParseTest(unittest.TestCase):
    def test_parse_ranges(self):
        self.assertEqual(parse_ranges("174,176,369-370\n"), {174, 176, 369, 370})
        self.assertEqual(parse_ranges(""), set())
        self.assertEqual(parse_ranges("x,3"), {3})

    def test_parse_edid(self):
        info = parse_edid(make_edid("SDC", 0x4301, "AMS881KB01-0"))
        self.assertEqual(info.vendor, "SDC")
        self.assertEqual(info.product, 0x4301)
        self.assertEqual(info.name, "AMS881KB01-0")
        self.assertIsNone(parse_edid(b"garbage"))
        self.assertIsNone(parse_edid(b"\x00" * 128))


if __name__ == "__main__":
    unittest.main()
