import contextlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from oxp3_tool import diagnose, display
from oxp3_tool.cli import main
from oxp3_tool.detect import Context
from oxp3_tool.engine import Engine
from oxp3_tool.fixes import BY_ID
from oxp3_tool.state import State

from .fakeroot import OXP3_MODES, FakeOxp3, make_edid

DMC_LOG = ("[    2.101] xe 0000:00:02.0: [drm] Finished loading DMC firmware i915/xe3lpd_dmc.bin (v2.20)\n"
           "[  900.000] xe 0000:00:02.0: [drm] Finished loading DMC firmware i915/xe3lpd_dmc.bin (v2.36)\n")
DEBUGFS = "/sys/kernel/debug/dri/0000:00:02.0"


class ParseTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.f = FakeOxp3(Path(tmp.name))

    def info(self, **kw):
        self.f.display_debugfs(**kw)
        return (self.f.root / DEBUGFS.lstrip("/") / "i915_display_info").read_text()

    def test_crtc_mode(self):
        mode = display.parse_crtc_mode(self.info(refresh=144), "eDP-1")
        self.assertEqual(mode.size, "1920x1200")
        self.assertAlmostEqual(mode.hz, 144.0, places=3)
        self.assertEqual(mode.describe(), "1920x1200 at 144 Hz")
        self.assertAlmostEqual(display.parse_crtc_mode(self.info(refresh=60), "eDP-1").hz, 60.0, places=3)

    def test_crtc_mode_needs_active_pipe_for_that_connector(self):
        self.assertIsNone(display.parse_crtc_mode(self.info(active=False), "eDP-1"))
        self.assertIsNone(display.parse_crtc_mode(self.info(), "DP-1"))

    def test_mode_name_from_userspace_is_never_printed(self):
        text = self.info().replace('"1920x1200": 144', '"\x1b[2J\x07evil": 144', 1)
        mode = display.parse_crtc_mode(text, "eDP-1")
        self.assertEqual(mode.describe(), "1920x1200 at 144 Hz")

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

    def test_dmc_info(self):
        self.assertEqual(display.parse_dmc_info("DMC initialized: yes\nfw loaded: yes\nversion: 2.36\n"),
                         (True, (2, 36)))
        self.assertEqual(display.parse_dmc_info("DMC initialized: yes\nfw loaded: no\npath: x\n"), (False, None))
        self.assertEqual(display.parse_dmc_info(""), (None, None))

    def test_dmc_version_from_log(self):
        # The last load wins (the driver reloads it after a GPU reset).
        self.assertEqual(display.dmc_version_from_log(DMC_LOG), (2, 36))
        self.assertEqual(display.dmc_version_from_log(DMC_LOG.splitlines()[0]), (2, 20))
        other = "[ 3.0] xe 0000:03:00.0: [drm] Finished loading DMC firmware i915/bmg_dmc.bin (v2.6)\n"
        self.assertEqual(display.dmc_version_from_log(DMC_LOG + other, "0000:00:02.0"), (2, 36))
        self.assertIsNone(display.dmc_version_from_log("nothing"))

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
        outs = display.parse_kwin_outputs(json.dumps(doc).encode(), "SDC", 0x4301, "gamer")
        # The EDID-identified entry wins over connector-only ones, as in KWin.
        self.assertEqual(len(outs), 1)
        self.assertEqual((outs[0].refresh_hz, outs[0].vrr_policy, outs[0].hdr, outs[0].user),
                         (60.0, "Automatic", True, "gamer"))

    def test_kwin_68_mode_format(self):
        doc = [{"name": "outputs", "data": [
            {"edidIdentifier": "SDC 17153 0 1 2025 0", "connectorName": "eDP-1",
             "mode": {"flags": 1, "cvt": {"clock": 380160, "hdisplay": 1920, "htotal": 2000, "vdisplay": 1200,
                                          "vtotal": 3168, "vscan": 0, "flags": 10}}}]}]
        outs = display.parse_kwin_outputs(json.dumps(doc).encode(), "SDC", 0x4301)
        self.assertEqual(round(outs[0].refresh_hz), 60)
        doc[0]["data"][0]["mode"] = {"basic": {"width": 1920, "height": 1200, "refreshRate": 143999}}
        outs = display.parse_kwin_outputs(json.dumps(doc).encode(), "SDC", 0x4301)
        self.assertEqual(outs[0].refresh_mhz, 143999)

    def test_kwin_connector_only_entry(self):
        doc = [{"name": "outputs", "data": [{"connectorName": "eDP-1"}, {"connectorName": "eDP-1"}]}]
        outs = display.parse_kwin_outputs(json.dumps(doc).encode(), "SDC", 0x4301)
        self.assertEqual(len(outs), 1)  # exact duplicates count once
        self.assertEqual((outs[0].refresh_mhz, outs[0].vrr_policy), (None, None))
        self.assertIn("KWin's default is Never", outs[0].describe())

    def test_kwin_outputs_bad_input(self):
        for data in (b"", b"{", b"\xff\xfe", b"{}", b"[1, 2]", b'[{"name": "outputs", "data": {}}]',
                     b"[" * 200000):
            self.assertEqual(display.parse_kwin_outputs(data, "SDC", 0x4301), [], data[:20])
        huge = json.dumps([{"name": "outputs", "data": [
            {"connectorName": "eDP-1", "mode": {"refreshRate": 10 ** 400}}]}]).encode()
        outs = display.parse_kwin_outputs(huge, "SDC", 0x4301)
        self.assertIsNone(outs[0].refresh_mhz)
        outs[0].describe()


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
        self.assertEqual((st.dmc_loaded, st.dmc_version), (True, (2, 36)))
        self.assertIs(st.dc_balance_param, False)
        self.assertTrue(st.dc_balance_risk)
        self.assertEqual(len(st.kwin), 1)  # the external monitor's entry is ignored
        out = self.status()
        self.assertIn("Refresh:  eDP-1 display mode 1920x1200 at 144 Hz (panel modes: 144 Hz, 60 Hz)", out)
        self.assertIn("with variable refresh (VRR) on the screen can refresh slower; the kernel shows no live rate",
                      out)
        self.assertNotIn("144 Hz now", out)
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

    def test_dc_balance_turned_back_on(self):
        self.f.display_debugfs()
        self.f.kwin_config(vrr="Always")
        self.f.write("/sys/module/xe/parameters/enable_dc_balance", "Y\n")
        self.assertTrue(self.state().dc_balance_risk)
        out = self.status()
        self.assertIn("xe.enable_dc_balance=1 turns the variable-refresh bug back on", out)
        self.assertIn("rpm-ostree kargs --delete=xe.enable_dc_balance`", out)

    def test_dc_balance_on_from_cmdline_without_root(self):
        self.f.display_debugfs()
        self.f.mkdir("/sys/module/xe/parameters/enable_dc_balance")  # exists but unreadable as a file
        self.f.runner.kargs.append("xe.enable_dc_balance=1")
        self.f.boot()
        self.assertTrue(self.state().dc_balance_on)

    def test_older_firmware_gives_no_vrr_warning(self):
        self.f.display_debugfs(dmc="2.32")
        self.f.kwin_config(vrr="Always")
        self.assertFalse(self.state().dc_balance_risk)
        self.assertNotIn("Adaptive Sync is set to Always", self.status())

    def test_firmware_not_loaded(self):
        self.f.display_debugfs(dmc=None)
        self.f.runner.journal["0"] = DMC_LOG  # an older load must not count
        st = self.state()
        self.assertEqual((st.dmc_loaded, st.dmc_version), (False, None))
        self.assertFalse(st.dc_balance_risk)
        out = self.status()
        self.assertIn("display firmware (DMC) not loaded", out)
        self.assertIn("The display firmware (DMC) did not load", out)
        self.assertNotIn("run with sudo", out)
        self.assertNotIn("leave variable refresh", out)

    def test_dmc_version_from_kernel_log(self):
        self.f.runner.journal["0"] = DMC_LOG
        self.assertEqual(self.state().dmc_version, (2, 36))

    def test_unknown_firmware_says_so(self):
        out = self.status()
        self.assertIn("display firmware (DMC) version unknown", out)
        self.assertIn("Could not read the display firmware version.", out)
        self.assertNotIn("run with sudo", out)  # the fake root reads like root
        self.assertIn("display mode not checked: no readable i915_display_info", out)

    def test_automatic_policy(self):
        self.f.display_debugfs()
        self.f.kwin_config(vrr="Automatic")
        out = self.status()
        self.assertIn("Adaptive Sync is set to Automatic, so fullscreen games and", out)

    def test_never_policy_still_explains_game_mode(self):
        self.f.display_debugfs()
        self.f.kwin_config(vrr="Never")
        out = self.status()
        self.assertNotIn("Adaptive Sync is set to", out)
        self.assertIn("far below 144 Hz", out)
        self.assertNotIn("the same bug", out)

    def test_no_game_mode_line_without_game_mode(self):
        self.f = FakeOxp3(self.root / "desktop", game_mode=False)
        self.f.display_debugfs()
        self.assertNotIn("In Game Mode", self.status())

    def test_desktop_set_to_60(self):
        self.f.display_debugfs(refresh=60)
        self.f.kwin_config(refresh_mhz=60000, vrr="Never")
        out = self.status()
        self.assertIn("eDP-1 display mode 1920x1200 at 60 Hz", out)
        self.assertIn("Desktop Mode is set to 60 Hz.", out)
        self.assertIn("`kscreen-doctor output.eDP-1.mode.1920x1200@144`", out)
        self.assertNotIn("Adaptive Sync is set to", out)

    def test_mode_at_60_without_kwin_setting(self):
        self.f.display_debugfs(refresh=60)
        self.assertIn("The display mode is 60 Hz.", self.status())

    def test_screen_off(self):
        self.f.display_debugfs(active=False)
        st = self.state()
        self.assertIsNone(st.mode)
        self.assertIn("no active display pipe drives eDP-1", st.debugfs_why)

    def test_other_panel_gets_no_refresh_section(self):
        self.f = FakeOxp3(self.root, edid=make_edid("BOE", 0x0BE5, None))
        self.f.display_debugfs(refresh=60)
        out = self.status()
        self.assertNotIn("Refresh:", out)
        self.assertNotIn("Refresh rate", out)

    def test_dsb_errors_hint_is_status_only(self):
        self.f.display_debugfs()
        self.f.runner.journal["0"] = "[CRTC:151:pipe A] DSB 0 poll error\n" * 3
        ctx = Context(self.f.system())
        self.assertEqual(ctx.refresh_state.dsb_errors, 3)
        self.assertIn("3 'DSB poll error'", BY_ID["xe-dsb-off"].note(ctx)[0])
        self.assertIn("Panel Replay Selective Update", BY_ID["xe-panel-replay-off"].note(ctx)[0])
        out = self.status()
        self.assertIn("the optional xe-dsb-off fix stops them", out)
        # Planning an apply reads no kernel log.
        self.f.runner.calls.clear()
        ctx = Context(self.f.system())
        Engine(ctx, State(self.f.system()), out=lambda _: None).plan([BY_ID["xe-dsb-off"]], {})
        self.assertFalse([c for c in self.f.runner.calls if c[0] == "journalctl"])

    def test_ignored_vbt_drive_levels_are_explained(self):
        self.f.display_debugfs()
        self.assertNotIn("VBT vswing", self.status())
        self.f.runner.journal["0"] = ("xe 0000:00:02.0: [drm] Port A asks to use VBT vswing/preemph tables\n"
                                      "WARNING: drivers/gpu/drm/i915/display/intel_bios.c:2813 at ...\n")
        ctx = Context(self.f.system())
        self.assertEqual(ctx.refresh_state.vbt_vswing_port, "A")
        out = " ".join(self.status().split())
        self.assertIn("'Port A asks to use VBT vswing/preemph tables'", out)
        self.assertIn("is part of Linux 7.4, first in 7.4-rc1", out)
        self.assertIn("No kernel argument or oxp3-tool fix works around it", out)

    def test_dsb_hint_after_apply_says_reboot(self):
        self.f.display_debugfs()
        self.f.runner.journal["0"] = "[CRTC:151:pipe A] DSB 0 poll error\n"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["apply", "--yes", "xe-dsb-off"], system=self.f.system()), 0)
        self.assertIn("xe-dsb-off is set for the next boot (reboot to activate it)", " ".join(self.status().split()))

    def test_panel_replay_hint_needs_errors(self):
        self.f.display_debugfs()
        self.assertNotIn("xe-panel-replay-off fix", self.status())
        self.f.runner.journal["0"] = "xe 0000:00:02.0: [drm] Selective fetch area calculation failed in pipe A\n"
        self.assertIn("experimental xe-panel-replay-off fix", self.status())
        self.f.runner.kargs.append("xe.enable_psr=0")
        self.assertNotIn("xe-panel-replay-off fix", self.status())

    def test_kwin_config_symlink_and_size_are_refused(self):
        self.f.write("/etc/secret.json", json.dumps([{"name": "outputs", "data": [
            {"connectorName": "eDP-1", "vrrPolicy": "Always"}]}]))
        self.f.symlink("/var/home/gamer/.config/kwinoutputconfig.json", str(self.root / "etc/secret.json"))
        self.assertEqual(self.state().kwin, [])
        self.f.write("/var/home/other/.config/kwinoutputconfig.json", " " * (display.KWIN_CONFIG_MAX + 1))
        self.assertEqual(self.state().kwin, [])

    def test_hostile_kwin_config_does_not_crash(self):
        self.f.display_debugfs()
        self.f.kwin_config(vrr="Always")
        self.f.write("/var/home/mallory/.config/kwinoutputconfig.json", "[" * 100000)
        self.f.write("/var/home/eve/.config/kwinoutputconfig.json", json.dumps(
            [{"name": "outputs", "data": [{"connectorName": "eDP-1", "mode": {"refreshRate": 10 ** 400}}]}]))
        self.assertIn("Adaptive Sync Always", self.status())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["apply", "--dry-run"], system=self.f.system()), 0)

    def test_home_symlink_is_not_counted_twice(self):
        self.f.kwin_config(vrr="Always")
        self.f.symlink("/home", "var/home")
        self.assertEqual(len(self.state().kwin), 1)

    def test_invoking_users_settings_win(self):
        self.f.kwin_config("gamer", refresh_mhz=144000, vrr="Never")
        self.f.kwin_config("guest", refresh_mhz=60000, vrr="Always")
        me = os.getuid()
        os.chown(self.root / "var/home/guest", me + 1, -1) if os.geteuid() == 0 else None
        if os.geteuid() != 0:
            self.skipTest("needs root to give the second home another owner")
        with mock.patch.dict(os.environ, {"SUDO_UID": str(me)}):
            st = self.state()
        self.assertEqual([k.user for k in st.kwin], ["gamer"])
        with mock.patch.dict(os.environ, {"SUDO_UID": str(me + 2)}):
            st = self.state()
        self.assertEqual(sorted(k.user for k in st.kwin), ["gamer", "guest"])
        self.assertIn("Desktop Mode (guest) is set to 60 Hz.", st.advice()[0])

    def test_other_users_settings_are_labelled(self):
        if os.geteuid() != 0:
            self.skipTest("needs root to give the home another owner")
        self.f.kwin_config("guest", refresh_mhz=60000, vrr="Never")
        os.chown(self.root / "var/home/guest", os.getuid() + 1, -1)
        with mock.patch.dict(os.environ, {"SUDO_UID": str(os.getuid())}):
            st = self.state()
        self.assertFalse(st.kwin_is_mine)
        self.assertIn("Desktop Mode (guest) is set to 60 Hz.", st.advice()[0])

    def test_saved_hdr_is_flagged(self):
        self.f.display_debugfs()
        self.f.kwin_config(vrr="Never", hdr=True)
        out = " ".join(self.status().split())
        self.assertIn("Desktop Mode setting: 144 Hz, Adaptive Sync Never, HDR on", out)
        self.assertIn("Desktop Mode has HDR switched on for the built-in screen", out)
        self.assertIn("`kscreen-doctor output.eDP-1.hdr.disable output.eDP-1.wcg.disable`", out)

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
        self.assertIn("refresh: eDP-1 display mode 1920x1200 at 144 Hz", hw)
        findings = bundle.files["findings.txt"].decode()
        self.assertIn("Refresh rate:\n- Adaptive Sync is set to Always.", findings)
        self.assertIn("debugfs/dri/0000:00:02.0/i915_dmc_info", bundle.files)

    def test_diagnose_archive_writes(self):
        self.f.display_debugfs()
        archive = Path(self.root.parent, f"{self.root.name}.tar.gz")
        self.addCleanup(lambda: archive.exists() and archive.unlink())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["diagnose", "-o", str(archive)], system=self.f.system()), 0)
        with tarfile.open(archive) as tar:
            self.assertIn("oxp3-diagnose/debugfs/dri/0000:00:02.0/eDP-1/i915_psr_status", tar.getnames())


if __name__ == "__main__":
    unittest.main()
