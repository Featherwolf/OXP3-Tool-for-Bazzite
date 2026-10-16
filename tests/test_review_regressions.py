"""Regression tests for problems found in review."""

import contextlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

from oxp3_tool import diagnose
from oxp3_tool.cli import main
from oxp3_tool.detect import Context
from oxp3_tool.engine import Engine
from oxp3_tool.fixes import BY_ID
from oxp3_tool.state import STATE_FILE, State
from oxp3_tool.system import CommandResult

from .fakeroot import FakeOxp3

CAP_MAP = "/etc/inputplumber/capability_maps.d/onexplayer_type9.yaml"
HWDB = "/etc/udev/hwdb.d/61-oxp3-tool-volume-keys.hwdb"
LUA = "/etc/gamescope/scripts/90-oxp3-tool-oled.lua"
USER_MAP = "version: 1\nid: oxp9\n# the user's own map\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name, "root")

    def fake(self, **kw):
        self.f = FakeOxp3(self.root, **kw)
        return self.f

    def engine(self):
        ctx = Context(self.f.system())
        return Engine(ctx, State(self.f.system()), out=lambda _: None), ctx

    def apply(self, ids, **opts):
        eng, _ = self.engine()
        return eng.apply(eng.plan([BY_ID[i] for i in ids], opts))

    def revert(self, ids):
        eng, _ = self.engine()
        return eng.revert(ids)

    def read(self, path):
        p = self.root / path.lstrip("/")
        return p.read_text() if p.exists() else None

    def state(self):
        text = self.read(STATE_FILE)
        return json.loads(text)["fixes"] if text else {}

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(list(argv), system=self.f.system())
        return rc, out.getvalue(), err.getvalue()


class BackupTest(Base):
    def test_lost_state_does_not_clobber_original_backup(self):
        self.fake()
        self.f.write(CAP_MAP, USER_MAP)
        self.apply(["buttons"])
        os.unlink(self.root / STATE_FILE.lstrip("/"))  # state moved aside
        self.apply(["buttons"])
        self.revert(["buttons"])
        self.assertEqual(self.read(CAP_MAP), USER_MAP)

    def test_lost_state_own_file_is_removed_on_revert(self):
        self.fake()
        self.apply(["oled-profile"])
        os.unlink(self.root / STATE_FILE.lstrip("/"))
        self.apply(["oled-profile"])
        self.assertIsNone(self.state()["oled-profile"]["files"][0]["backup"])
        self.revert(["oled-profile"])
        self.assertIsNone(self.read(LUA))

    def test_interrupt_after_write_ahead_is_revertible(self):
        self.fake()
        eng, _ = self.engine()
        plans = eng.plan([BY_ID["oled-profile"]], {})
        real_write = eng.sys.write_atomic

        def write_then_interrupt(*a, **kw):
            real_write(*a, **kw)
            raise KeyboardInterrupt

        eng.sys.write_atomic = write_then_interrupt
        with self.assertRaises(KeyboardInterrupt):
            eng.apply(plans)
        self.assertIn("oled-profile", self.state())
        rc, out, _ = self.cli("status")
        self.assertIn("[incomplete  ] oled-profile", out)
        self.revert(["oled-profile"])
        self.assertIsNone(self.read(LUA))
        self.assertEqual(self.state(), {})

    def test_failed_unit_enable_leaves_no_phantom_entry(self):
        self.fake()
        self.f.runner.fail.add("systemctl enable")
        out = self.apply(["inputplumber-suspend"])
        self.assertFalse(out.ok)
        self.assertEqual(self.state(), {})

    def test_created_dirs_are_removed_on_revert(self):
        self.fake()
        self.f.mkdir("/etc/udev/hwdb.d")
        self.apply(["oled-profile", "buttons", "volume-keys"])
        self.revert(["oled-profile", "buttons", "volume-keys"])
        self.assertFalse((self.root / "etc/gamescope").exists())
        self.assertFalse((self.root / "etc/inputplumber").exists())
        self.assertTrue((self.root / "etc/udev/hwdb.d").exists())


class KargTest(Base):
    def test_karg_added_by_interrupted_run_is_owned(self):
        self.fake()
        real = self.f.runner

        def timeout_after_commit(argv, timeout=None):
            res = real(argv, timeout)
            if argv[:2] == ["rpm-ostree", "kargs"] and len(argv) > 2:
                return CommandResult(124, "", "timed out")
            return res

        system = self.f.system()
        system._runner = timeout_after_commit
        ctx = Context(system)
        eng = Engine(ctx, State(system), out=lambda _: None)
        out = eng.apply(eng.plan([BY_ID["nvme-resume"]], {}))
        self.assertFalse(out.ok)
        self.assertEqual(self.state()["nvme-resume"]["kargs"], ["nvme.noacpi=1"])
        self.revert(["nvme-resume"])
        self.assertNotIn("nvme.noacpi=1", self.f.runner.kargs)

    def test_failed_karg_add_records_nothing(self):
        self.fake()
        self.f.runner.fail.add("rpm-ostree kargs --append")
        self.assertFalse(self.apply(["nvme-resume"]).ok)
        self.assertEqual(self.state(), {})

    def test_revert_undone_by_update_is_still_tracked(self):
        self.fake()
        self.apply(["nvme-resume"])
        self.f.boot()
        self.revert(["nvme-resume"])
        self.assertIn("nvme-resume", self.state())  # still active until reboot
        self.f.update_before_reboot()  # bootc restages from the booted kargs
        rc, out, _ = self.cli("status")
        self.assertIn("came back with a system update", out)
        self.revert(["nvme-resume"])
        self.assertNotIn("nvme.noacpi=1", self.f.runner.kargs)
        self.f.boot()
        self.cli("status")
        rc, out, _ = self.cli("revert", "--all", "--yes")
        self.assertIn("Nothing applied", out)

    def test_reapply_during_pending_revert_keeps_ownership(self):
        self.fake()
        self.apply(["nvme-resume"])
        self.f.boot()
        self.revert(["nvme-resume"])
        self.apply(["nvme-resume"])
        self.assertEqual(self.state()["nvme-resume"]["kargs"], ["nvme.noacpi=1"])
        self.assertNotIn("kargs_already_set", self.state()["nvme-resume"]["notes"])

    def test_karg_dropped_by_update_is_reported_and_reapplied(self):
        self.fake()
        self.apply(["nvme-resume"])
        self.f.update_before_reboot()
        self.f.boot()
        rc, out, _ = self.cli("status")
        self.assertIn("nvme.noacpi=1 is not set", out)
        self.assertIn("apply nvme-resume` again", out)
        self.apply(["nvme-resume"])
        self.assertIn("nvme.noacpi=1", self.f.runner.kargs)

    def test_update_warning(self):
        self.fake()
        out = self.apply(["nvme-resume"])
        self.assertTrue(any("Reboot before installing a system update" in m for m in out.messages))


class OptionsTest(Base):
    def test_button_choice_survives_reapply(self):
        self.fake()
        self.apply(["buttons"], home="quick-access", keyboard_key="screenshot")
        self.apply(["buttons"])
        actions = self.state()["buttons"]["options"]["actions"]
        self.assertEqual(actions["home"], "quick-access")
        self.assertEqual(actions["keyboard"], "screenshot")
        self.apply(["buttons"], defaults=True)
        self.assertEqual(self.state()["buttons"]["options"]["actions"]["home"], "guide")

    def test_min_refresh_survives_reapply(self):
        self.fake()
        self.apply(["oled-profile"], min_refresh=40)
        self.apply(["oled-profile"])
        self.assertEqual(self.state()["oled-profile"]["options"]["min_refresh"], 40)
        self.assertIn("[40] =", self.read(LUA))

    def test_unchanged_plan_says_so(self):
        self.fake()
        self.apply(["buttons", "volume-keys"])
        rc, out, _ = self.cli("apply", "--dry-run", "buttons", "volume-keys")
        self.assertIn("already applied, nothing to change", out)
        self.assertNotIn("then restart InputPlumber", out)
        rc, out, _ = self.cli("apply", "--yes", "buttons")
        self.assertIn("Nothing to change", out)

    def test_no_restart_is_finished_later(self):
        self.fake()
        self.apply(["buttons"], restart=False)
        self.f.runner.calls.clear()
        self.apply(["buttons"])
        self.assertIn(["systemctl", "try-restart", "inputplumber.service"], self.f.runner.calls)


class FixChecksTest(Base):
    def test_volume_keys_steps_aside_for_hhd(self):
        self.fake()
        self.f.runner.hhd_units = ["hhd@gamer.service"]
        _, ctx = self.engine()
        self.assertFalse(BY_ID["volume-keys"].check(ctx).applicable)
        self.fake()
        self.f.write("/usr/bin/hhd", "")
        _, ctx = self.engine()
        check = BY_ID["volume-keys"].check(ctx)
        self.assertTrue(check.applicable)
        self.assertFalse(check.recommended)

    def test_volume_keys_reload_order_and_verification(self):
        self.fake()
        out = self.apply(["buttons", "volume-keys"])
        order = [c[0:3] for c in self.f.runner.calls if c[0] in ("systemd-hwdb", "udevadm", "systemctl")
                 and c[1] in ("update", "control", "trigger", "settle", "try-restart")]
        self.assertEqual([c[1] for c in order], ["update", "control", "trigger", "settle", "try-restart"])
        # The fake kernel never updates force_release, so the tool says a reboot is needed.
        self.assertTrue(any("reboot to finish volume-keys" in m for m in out.messages))

    def test_oled_profile_not_recommended_without_game_mode(self):
        self.fake(image="bazzite", inputplumber=False, game_mode=False)
        _, ctx = self.engine()
        check = BY_ID["oled-profile"].check(ctx)
        self.assertTrue(check.applicable)
        self.assertFalse(check.recommended)

    def test_changed_stock_map_is_not_recommended(self):
        self.fake()
        self.f.write("/usr/share/inputplumber/capability_maps/onexplayer_type9.yaml",
                     "version: 1\nkind: CapabilityMap\nname: x\nid: oxp9\nmapping: []\nfiltered_events: []\n")
        _, ctx = self.engine()
        check = BY_ID["buttons"].check(ctx)
        self.assertTrue(check.applicable)
        self.assertFalse(check.recommended)

    def test_comment_changes_in_stock_map_are_ignored(self):
        self.fake()
        path = self.root / "usr/share/inputplumber/capability_maps/onexplayer_type9.yaml"
        path.write_text("# new comment\n" + path.read_text().replace("id: oxp9", "id: oxp9   "))
        _, ctx = self.engine()
        self.assertTrue(BY_ID["buttons"].check(ctx).recommended)

    def test_stock_map_change_after_apply_is_reported(self):
        self.fake()
        self.apply(["buttons"])
        self.f.write("/usr/share/inputplumber/capability_maps/onexplayer_type9.yaml",
                     "version: 1\nkind: CapabilityMap\nname: x\nid: oxp9\nmapping: []\nfiltered_events: []\n")
        rc, out, _ = self.cli("status")
        self.assertIn("still replaces it completely", out)
        self.assertNotIn("refreshes", out)


class CliTest(Base):
    def test_status_labels(self):
        self.fake(gm7=False)
        rc, out, _ = self.cli("status")
        self.assertIn("[optional    ] nvme-resume", out)
        self.fake(os_id="cachyos")
        rc, out, _ = self.cli("status")
        self.assertIn("NOT Bazzite", out)
        self.assertIn("only changes anything on a ONEXPLAYER 3 running Bazzite", out)

    def test_corrupt_state_keeps_read_only_commands_working(self):
        self.fake()
        self.f.write(STATE_FILE, "{bad")
        rc, out, _ = self.cli("status")
        self.assertEqual(rc, 0)
        self.assertIn("is corrupt", out)
        self.assertIn("[unknown     ] buttons", out)
        self.assertEqual(self.cli("list")[0], 0)
        archive = Path(self.tmp.name, "d.tar.gz")
        self.assertEqual(self.cli("diagnose", "-o", str(archive))[0], 0)
        with tarfile.open(archive) as tar:
            self.assertIn("oxp3-diagnose/files/var/lib/oxp3-tool/state.json", tar.getnames())
        self.assertEqual(self.cli("apply", "--yes")[0], 1)
        self.assertEqual(self.cli("revert", "--all", "--yes")[0], 1)


class DiagnoseTest(Base):
    def test_redacts_serials_and_macs(self):
        self.fake()
        self.f.write("/proc/bus/input/devices", "I: Bus=0003\nU: Uniq=ABCDEF123456\nP: Phys=aa:bb:cc:dd:ee:ff\n")
        real = self.f.runner

        def runner(argv, timeout=None):
            if argv[:2] == ["journalctl", "-k"]:
                return CommandResult(0, "usb 3-5: SerialNumber: 0123456789AB\n"
                                        "wlan0: authenticate with 11:22:33:44:55:66 (local address=aa:bb:cc:dd:ee:ff)\n"
                                        "nvme nvme0: Disabling device after reset failure: -19\n")
            return real(argv, timeout)

        system = self.f.system()
        system._runner = runner
        bundle, findings = diagnose.collect(Context(system), "status")
        blob = b"".join(bundle.files.values()).decode()
        for secret in ("0123456789AB", "ABCDEF123456", "aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"):
            self.assertNotIn(secret, blob)
        self.assertIn("mac-1", blob)
        self.assertTrue(any("Disabling device after reset failure" in f for f in findings))

    def test_never_writes_through_a_symlink(self):
        self.fake()
        target = Path(self.tmp.name, "victim")
        target.write_text("precious")
        link = Path(self.tmp.name, "out.tar.gz")
        link.symlink_to(target)
        rc, _, err = self.cli("diagnose", "-o", str(link))
        self.assertEqual(rc, 1)
        self.assertEqual(target.read_text(), "precious")


if __name__ == "__main__":
    unittest.main()
