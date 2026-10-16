import contextlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

from oxp3_tool import diagnose, display
from oxp3_tool.cli import main
from oxp3_tool.detect import Context
from oxp3_tool.fixes import BY_ID

from .fakeroot import OXP3_MODES, FakeOxp3

DMC_LOG = ("[    2.101] xe 0000:00:02.0: [drm] Finished loading DMC firmware i915/xe3lpd_dmc.bin (v2.20)\n"
           "[  900.000] xe 0000:00:02.0: [drm] Finished loading DMC firmware i915/xe3lpd_dmc.bin (v2.36)\n")


class ParseTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.f = FakeOxp3(Path(tmp.name))

    def info(self, **kw):
        self.f.display_debugfs(**kw)
        return (self.f.root / "sys/kernel/debug/dri/0000:00:02.0/i915_display_info").read_text()

    def test_crtc_mode(self):
        mode = display.parse_crtc_mode(self.info(refresh=144), "eDP-1")
        self.assertEqual(mode.name, "1920x1200")
        self.assertAlmostEqual(mode.hz, 144.0, places=3)
        self.assertAlmostEqual(display.parse_crtc_mode(self.info(refresh=60), "eDP-1").hz, 60.0, places=3)

    def test_crtc_mode_needs_active_pipe_for_that_connector(self):
        self.assertIsNone(display.parse_crtc_mode(self.info(active=False), "eDP-1"))
        self.assertIsNone(display.parse_crtc_mode(self.info(), "DP-1"))

    def test_fixed_modes(self):
        modes = display.parse_fixed_modes(self.info(), "eDP-1")
        self.assertEqual([round(m.hz) for m in modes], [144, 60])
        self.assertEqual(display.parse_fixed_modes(self.info(), "DP-1"), [])

    def test_mode_regex_matches_kernel_format(self):
        # DRM_MODE_FMT: "%s": %d %d %d %d %d %d %d %d %d %d 0x%x 0x%x
        line = "\tuapi: enable=yes, active=yes, mode=" + OXP3_MODES[144]
        self.assertTrue(display._UAPI_RE.match(line))

    def test_psr_mode(self):
        self.assertEqual(display.parse_psr_mode("Sink support: PSR = yes\nPSR mode: PSR1 enabled\n"), "PSR1 enabled")
        self.assertEqual(display.parse_psr_mode("PSR mode: disabled\n  PSR sink not reliable\n"), "disabled")
        self.assertIsNone(display.parse_psr_mode("Sink support: PSR = no\n"))

    def test_dmc_version(self):
        self.assertEqual(display.parse_dmc_version("DMC initialized: yes\nversion: 2.36\n", ""), (2, 36))
        # The last load in the log wins (the driver reloads it after a GPU reset).
        self.assertEqual(display.parse_dmc_version(None, DMC_LOG), (2, 36))
        self.assertEqual(display.parse_dmc_version("DMC initialized: no\n", DMC_LOG.splitlines()[0]), (2, 20))
        self.assertIsNone(display.parse_dmc_version(None, "nothing"))

    def test_kwin_outputs(self):
        doc = [
            {"name": "outputs", "data": [
                {"edidIdentifier": "GSM 23305 1 2 2023 0", "connectorName": "eDP-1", "vrrPolicy": "Always"},
                {"edidIdentifier": "SDC 17153 0 1 2025 0", "connectorName": "eDP-1",
                 "mode": {"width": 1920, "height": 1200, "refreshRate": 60000}, "vrrPolicy": "Automatic",
                 "highDynamicRange": True},
                {"connectorName": "eDP-2", "mode": {"refreshRate": True}, "vrrPolicy": "bogus"},
                "junk",
            ]},
            {"name": "outputs", "data": [{"connectorName": "eDP-1", "vrrPolicy": "Always"}]},
        ]
        outs = display.parse_kwin_outputs(json.dumps(doc).encode(), "SDC", 0x4301)
        self.assertEqual(len(outs), 2)
        self.assertEqual((outs[0].refresh_hz, outs[0].vrr_policy, outs[0].hdr), (60.0, "Automatic", True))
        self.assertEqual((outs[1].refresh_mhz, outs[1].vrr_policy), (None, None))
        self.assertIn("KWin's default is Never", outs[1].describe())

    def test_kwin_outputs_bad_input(self):
        for data in (b"", b"{", b"\xff\xfe", b"{}", b"[1, 2]", b'[{"name": "outputs", "data": {}}]'):
            self.assertEqual(display.parse_kwin_outputs(data, "SDC", 0x4301), [], data)


class RefreshStateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.f = FakeOxp3(self.root)

    def state(self):
        return Context(self.f.system()).refresh_state

    def status(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["status"], system=self.f.system()), 0)
        return out.getvalue()

    def test_full_picture(self):
        self.f.display_debugfs()
        self.f.kwin_config(vrr="Always")
        st = self.state()
        self.assertEqual(st.connector, "eDP-1")
        self.assertEqual(round(st.mode.hz), 144)
        self.assertEqual(st.psr_mode, "Panel Replay Selective Update enabled")
        self.assertEqual(st.dmc_version, (2, 36))
        self.assertIs(st.dc_balance_param, False)
        self.assertTrue(st.dc_balance_risk)
        self.assertEqual(len(st.kwin), 1)  # the external monitor's entry is ignored
        out = self.status()
        self.assertIn("Refresh:  eDP-1 runs 1920x1200 at 144 Hz now (panel modes: 144 Hz, 60 Hz)", out)
        self.assertIn("display firmware (DMC) v2.36; the kernel predates the variable-refresh fix", out)
        self.assertIn("Desktop Mode setting: 144 Hz, Adaptive Sync Always", out)
        self.assertIn("Adaptive Sync is set to Always.", out)
        self.assertIn("`kscreen-doctor output.eDP-1.vrrpolicy.never`", out)
        self.assertIn("In Game Mode, leave variable refresh (VRR) off", out)
        for line in out.splitlines():
            self.assertLessEqual(len(line), 120, line)

    def test_kernel_with_fix_gives_no_vrr_warning(self):
        self.f.display_debugfs()
        self.f.kwin_config(vrr="Always")
        self.f.write("/sys/module/xe/parameters/enable_dc_balance", "N\n")
        self.assertFalse(self.state().dc_balance_risk)
        out = self.status()
        self.assertIn("the kernel has the variable-refresh fix", out)
        self.assertNotIn("Adaptive Sync is set to Always", out)
        self.assertNotIn("leave variable refresh (VRR) off", out)

    def test_older_firmware_gives_no_vrr_warning(self):
        self.f.display_debugfs(dmc="2.32")
        self.f.kwin_config(vrr="Always")
        self.assertFalse(self.state().dc_balance_risk)
        self.assertNotIn("Adaptive Sync is set to Always", self.status())

    def test_dmc_version_from_kernel_log(self):
        self.f.runner.journal["0"] = DMC_LOG
        self.assertEqual(self.state().dmc_version, (2, 36))

    def test_unknown_firmware_says_so(self):
        out = self.status()
        self.assertIn("display firmware (DMC) version unknown", out)
        self.assertIn("Could not read the display firmware version", out)
        self.assertIn("current mode not checked: no readable i915_display_info", out)

    def test_automatic_policy(self):
        self.f.display_debugfs()
        self.f.kwin_config(vrr="Automatic")
        out = self.status()
        self.assertIn("Adaptive Sync is set to Automatic, so fullscreen games and videos", out)

    def test_desktop_set_to_60(self):
        self.f.display_debugfs(refresh=60)
        self.f.kwin_config(refresh_mhz=60000, vrr="Never")
        out = self.status()
        self.assertIn("eDP-1 runs 1920x1200 at 60 Hz now", out)
        self.assertIn("Desktop Mode is set to 60 Hz.", out)
        self.assertIn("`kscreen-doctor output.eDP-1.mode.1920x1200@144`", out)
        self.assertNotIn("Adaptive Sync is set to", out)

    def test_running_at_60_without_kwin_setting(self):
        self.f.display_debugfs(refresh=60)
        self.assertIn("The panel runs at 60 Hz right now.", self.status())

    def test_screen_off(self):
        self.f.display_debugfs(active=False)
        st = self.state()
        self.assertIsNone(st.mode)
        self.assertIn("no active display pipe drives eDP-1", st.debugfs_why)

    def test_dsb_errors_and_panel_replay_hints(self):
        self.f.display_debugfs()
        self.f.runner.journal["0"] = "[CRTC:151:pipe A] DSB 0 poll error\n" * 3
        ctx = Context(self.f.system())
        self.assertEqual(ctx.refresh_state.dsb_errors, 3)
        self.assertIn("3 'DSB poll error'", BY_ID["xe-dsb-off"].check(ctx).reason)
        self.assertIn("Panel Replay Selective Update", BY_ID["xe-panel-replay-off"].check(ctx).reason)
        out = self.status()
        self.assertIn("the optional xe-dsb-off fix stops them", out)
        self.assertIn("experimental xe-panel-replay-off fix", out)

    def test_kwin_config_symlink_and_size_are_refused(self):
        self.f.write("/etc/secret.json", json.dumps([{"name": "outputs", "data": [
            {"connectorName": "eDP-1", "vrrPolicy": "Always"}]}]))
        self.f.symlink("/var/home/gamer/.config/kwinoutputconfig.json", str(self.root / "etc/secret.json"))
        self.assertEqual(self.state().kwin, [])
        self.f.write("/var/home/other/.config/kwinoutputconfig.json", " " * (display.KWIN_CONFIG_MAX + 1))
        self.assertEqual(self.state().kwin, [])

    def test_home_symlink_is_not_counted_twice(self):
        self.f.kwin_config(vrr="Always")
        self.f.symlink("/home", "var/home")
        self.assertEqual(len(self.state().kwin), 1)

    def test_read_regular(self):
        sys = self.f.system()
        self.f.write("/tmp/a", "x" * 10)
        self.assertEqual(sys.read_regular("/tmp/a", 10), b"x" * 10)
        self.assertIsNone(sys.read_regular("/tmp/a", 9))
        self.assertIsNone(sys.read_regular("/tmp", 100))
        self.assertIsNone(sys.read_regular("/tmp/missing", 100))
        os.symlink("a", self.root / "tmp/b")
        self.assertIsNone(sys.read_regular("/tmp/b", 100))

    def test_diagnose_includes_refresh(self):
        self.f.display_debugfs()
        self.f.kwin_config(vrr="Always")
        bundle, _ = diagnose.collect(Context(self.f.system()), "status")
        hw = bundle.files["hardware.txt"].decode()
        self.assertIn("refresh: eDP-1 runs 1920x1200 at 144 Hz now", hw)
        findings = bundle.files["findings.txt"].decode()
        self.assertIn("Refresh rate:\n- Adaptive Sync is set to Always.", findings)
        self.assertIn("debugfs/dri/0000:00:02.0/i915_dmc_info", bundle.files)

    def test_diagnose_archive_writes(self):
        self.f.display_debugfs()
        archive = Path(self.root.parent, "x.tar.gz")
        self.addCleanup(lambda: archive.exists() and archive.unlink())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["diagnose", "-o", str(archive)], system=self.f.system()), 0)
        with tarfile.open(archive) as tar:
            self.assertIn("oxp3-diagnose/debugfs/dri/0000:00:02.0/eDP-1/i915_psr_status", tar.getnames())


if __name__ == "__main__":
    unittest.main()
