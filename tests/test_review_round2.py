"""Regression tests for problems found in the second review round."""

import json
import os
import unittest
from pathlib import Path

from oxp3_tool.cli import LOCK_FILE, acquire_lock
from oxp3_tool.detect import Context
from oxp3_tool.engine import EngineError, reconcile
from oxp3_tool.state import STATE_FILE, State

from .test_review_regressions import CAP_MAP, USER_MAP, Base

MODIFIED = "/var/lib/oxp3-tool/modified"


class KargOwnershipTest(Base):
    def test_failed_reapply_during_revert_keeps_tracking(self):
        self.fake()
        self.apply(["nvme-resume"])
        self.f.boot()
        self.revert(["nvme-resume"])
        self.f.runner.fail.add("rpm-ostree kargs --append")
        self.assertFalse(self.apply(["nvme-resume"]).ok)
        self.f.runner.fail.clear()
        self.f.update_before_reboot()
        self.cli("revert", "--yes", "nvme-resume")
        self.assertNotIn("nvme.noacpi=1", self.f.runner.kargs)

    def test_already_set_karg_recorded_alongside_new_one(self):
        self.fake(kargs=["rhgb", "nvme.noacpi=1"])
        self.assertTrue(self.apply(["nvme-resume", "xe-dsb-off"]).ok)
        st = self.state()
        self.assertIn("nvme-resume", st)
        self.assertTrue(st["nvme-resume"]["applied_at"])
        self.assertEqual(st["xe-dsb-off"]["kargs"], ["xe.enable_dsb=0"])

    def test_batch_revert_failure_only_unreverts_owners(self):
        self.fake()
        self.apply(["nvme-resume", "xe-dsb-off"])
        self.f.boot()
        self.f.runner.kargs.remove("xe.enable_dsb=0")  # dropped from the next boot by something else
        self.f.runner.fail.add("rpm-ostree kargs --delete")
        self.assertFalse(self.revert(["nvme-resume", "xe-dsb-off"]).ok)
        st = self.state()
        self.assertTrue(st["xe-dsb-off"].get("reverted"))
        self.assertNotIn("reverted", st["nvme-resume"])
        self.assertEqual(st["nvme-resume"]["kargs"], ["nvme.noacpi=1"])

    def test_revert_refused_while_busy_changes_nothing(self):
        self.fake()
        self.apply(["nvme-resume", "oled-profile"])
        self.f.boot()
        self.f.runner.transaction = ["upgrade"]
        eng, _ = self.engine()
        with self.assertRaises(EngineError):
            eng.revert(["oled-profile", "nvme-resume"])
        self.assertIsNotNone(self.read("/etc/gamescope/scripts/90-oxp3-tool-oled.lua"))
        self.assertNotIn("reverted", self.state()["nvme-resume"])

    def test_reconcile_waits_for_running_transaction(self):
        self.fake()
        system = self.f.system()
        state = State(system)
        entry = state.entry("nvme-resume")
        entry["kargs_pending_add"] = ["nvme.noacpi=1"]
        state.save()
        self.f.runner.transaction = ["kargs"]
        self.assertFalse(reconcile(Context(system), state))
        self.assertEqual(state.get("nvme-resume")["kargs_pending_add"], ["nvme.noacpi=1"])
        self.f.runner.transaction = None
        self.f.runner.kargs.append("nvme.noacpi=1")  # the transaction committed
        reconcile(Context(system), state)
        self.assertEqual(state.get("nvme-resume")["kargs"], ["nvme.noacpi=1"])

    def test_readme_uninstall_sequence_leaves_no_state(self):
        self.fake()
        self.cli("apply", "--yes")
        self.f.boot()
        self.cli("revert", "--all", "--yes")
        self.f.boot()
        self.cli("status")  # as root, status settles the finished revert
        self.assertFalse((self.root / STATE_FILE.lstrip("/")).exists())


class FileSafetyTest(Base):
    def test_interrupted_revert_never_loses_the_original(self):
        self.fake()
        self.f.write(CAP_MAP, USER_MAP)
        self.apply(["buttons"])
        eng, _ = self.engine()
        calls = {"n": 0}

        def out(line):
            if "restored" in line and calls["n"] == 0:
                calls["n"] += 1
                raise BrokenPipeError("reader went away")

        eng.out = out
        eng.revert(["buttons"])
        self.revert(["buttons"])
        self.assertEqual(self.read(CAP_MAP), USER_MAP)
        self.assertEqual(self.state(), {})

    def test_reapply_keeps_copy_of_edited_file(self):
        self.fake()
        self.apply(["buttons"])
        self.f.write(CAP_MAP, "edited by hand\n")
        out = self.apply(["buttons"], home="quick-access")
        self.assertTrue(any("a copy was kept" in m for m in out.messages))
        self.assertEqual(self.read(f"{MODIFIED}/buttons{CAP_MAP}"), "edited by hand\n")

    def test_edited_copies_are_not_overwritten(self):
        self.fake()
        for text in ("first edit\n", "second edit\n"):
            self.apply(["buttons"])
            self.f.write(CAP_MAP, text)
            self.revert(["buttons"])
        self.assertEqual(self.read(f"{MODIFIED}/buttons{CAP_MAP}"), "first edit\n")
        self.assertEqual(self.read(f"{MODIFIED}/buttons{CAP_MAP}.1"), "second edit\n")


class UnitTest(Base):
    def test_disabled_unit_is_reported_and_reenabled(self):
        self.fake()
        self.apply(["inputplumber-suspend"])
        self.f.runner.unit_enabled["inputplumber-suspend.service"] = "disabled"
        rc, out, _ = self.cli("status")
        self.assertIn("[damaged     ] inputplumber-suspend", out)
        self.apply(["inputplumber-suspend"])
        self.assertEqual(self.f.runner.unit_enabled["inputplumber-suspend.service"], "enabled")

    def test_revert_when_unit_file_is_gone(self):
        self.fake()
        self.apply(["inputplumber-suspend"])
        os.unlink(self.root / "usr/lib/systemd/system/inputplumber-suspend.service")
        self.f.runner.unit_enabled["inputplumber-suspend.service"] = "disabled"
        self.f.runner.fail.add("systemctl disable")
        self.assertTrue(self.revert(["inputplumber-suspend"]).ok)
        self.assertEqual(self.state(), {})


class MiscTest(Base):
    def test_restart_reminder_cleared_by_reboot(self):
        self.fake()
        self.f.write("/proc/sys/kernel/random/boot_id", "boot-1\n")
        self.apply(["buttons"], restart=False)
        rc, out, _ = self.cli("status")
        self.assertIn("InputPlumber has not been restarted", out)
        self.f.write("/proc/sys/kernel/random/boot_id", "boot-2\n")
        rc, out, _ = self.cli("status")
        self.assertNotIn("InputPlumber has not been restarted", out)
        rc, out, _ = self.cli("apply", "--dry-run", "buttons")
        self.assertIn("already applied", out)

    def test_diagnose_reports_unwritable_path(self):
        self.fake()
        rc, _, err = self.cli("diagnose", "-o", str(Path(self.tmp.name, "missing-dir", "x.tar.gz")))
        self.assertEqual(rc, 1)
        self.assertIn("cannot write", err)

    def test_concurrent_runs_are_refused(self):
        self.fake()
        held = acquire_lock(self.f.system())
        self.assertIsNotNone(held)
        try:
            rc, _, err = self.cli("apply", "--yes", "volume-keys")
            self.assertEqual(rc, 1)
            self.assertIn("another oxp3-tool run", err)
            self.assertEqual(self.cli("status")[0], 0)  # read-only commands still work
        finally:
            os.close(held)
        self.assertTrue((self.root / LOCK_FILE.lstrip("/")).exists())
        self.assertEqual(self.cli("apply", "--yes", "volume-keys")[0], 0)

    def test_state_file_has_no_private_keys(self):
        self.fake()
        self.f.write(CAP_MAP, USER_MAP)
        self.apply(["buttons", "oled-profile"])
        for entry in json.loads(self.read(STATE_FILE))["fixes"].values():
            self.assertFalse([k for k in entry if k.startswith("_")])


if __name__ == "__main__":
    unittest.main()
