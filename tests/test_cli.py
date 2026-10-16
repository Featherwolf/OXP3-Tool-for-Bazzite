import contextlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from oxp3_tool.cli import main

from .fakeroot import FakeOxp3


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name, "root")
        self.f = FakeOxp3(self.root)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(list(argv), system=self.f.system())
        return rc, out.getvalue(), err.getvalue()

    def test_status(self):
        rc, out, _ = self.run_cli()
        self.assertEqual(rc, 0)
        self.assertIn("ONE-NETBOOK ONEXPLAYER 3 (BIOS 5.09) - supported", out)
        self.assertIn("InputPlumber 0.82.0 (active)", out)
        self.assertIn("card1-eDP-1: SDC 0x4301 (AMS881KB01-0)", out)
        self.assertIn("[recommended ] buttons", out)
        self.assertIn("[experimental] gyro", out)

    def test_list(self):
        rc, out, _ = self.run_cli("list")
        self.assertEqual(rc, 0)
        self.assertIn("nvme-resume", out)
        self.assertIn("quick-access", out)

    def test_dry_run_changes_nothing(self):
        rc, out, _ = self.run_cli("apply", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("create /etc/inputplumber/capability_maps.d/onexplayer_type9.yaml", out)
        self.assertIn("add kernel argument nvme.noacpi=1", out)
        self.assertFalse((self.root / "etc/inputplumber").exists())

    def test_apply_needs_confirmation_when_not_interactive(self):
        rc, _, err = self.run_cli("apply")
        self.assertEqual(rc, 1)
        self.assertIn("--yes", err)

    def test_apply_status_revert_cycle(self):
        rc, out, err = self.run_cli("apply", "--yes", "--home", "quick-access")
        self.assertEqual(rc, 0, err)
        self.assertIn("Reboot", out)
        rc, out, _ = self.run_cli("status")
        self.assertIn("[applied     ] buttons", out)
        self.assertIn("home -> quick-access", out)
        self.assertIn("nvme.noacpi=1 is set for the next boot", out)
        self.f.boot()
        rc, out, _ = self.run_cli("status")
        self.assertIn("nvme.noacpi=1 is active", out)
        rc, out, err = self.run_cli("revert", "--all", "--yes")
        self.assertEqual(rc, 0, err)
        rc, out, _ = self.run_cli("status")
        self.assertIn("[reverting   ] nvme-resume", out)
        self.assertIn("nvme.noacpi=1 is removed from the next boot (reboot to finish)", out)
        self.f.boot()
        rc, out, _ = self.run_cli("revert", "--all", "--yes")
        self.assertIn("Nothing applied", out)
        self.assertFalse((self.root / "var/lib/oxp3-tool").exists())

    def test_unknown_fix(self):
        rc, _, err = self.run_cli("apply", "--dry-run", "nope")
        self.assertEqual(rc, 1)
        self.assertIn("unknown fix", err)

    def test_revert_requires_target(self):
        rc, _, _ = self.run_cli("revert")
        self.assertEqual(rc, 2)

    def test_not_oxp3(self):
        self.f.write("/sys/class/dmi/id/product_name", "ONEXPLAYER X1 A\n")
        rc, out, _ = self.run_cli("status")
        self.assertIn("NOT a ONEXPLAYER 3", out)
        rc, _, err = self.run_cli("apply", "--dry-run", "volume-keys")
        self.assertEqual(rc, 1)
        self.assertIn("not a ONEXPLAYER 3", err)

    def test_diagnose(self):
        archive = Path(self.tmp.name, "diag.tar.gz")
        rc, out, _ = self.run_cli("diagnose", "-o", str(archive))
        self.assertEqual(rc, 0)
        with tarfile.open(archive) as tar:
            names = tar.getnames()
        self.assertIn("oxp3-diagnose/status.txt", names)
        self.assertIn("oxp3-diagnose/hardware.txt", names)
        self.assertIn("oxp3-diagnose/files/proc/cmdline", names)
        self.assertIn("oxp3-diagnose/commands/journal-kernel-previous-boot.txt", names)
        self.assertFalse([n for n in names if "serial" in n or "uuid" in n])


if __name__ == "__main__":
    unittest.main()
