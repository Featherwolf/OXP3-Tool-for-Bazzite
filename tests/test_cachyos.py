"""CachyOS: platform detection, kernel arguments through each boot loader, and the CachyOS-specific fix paths."""

import contextlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from oxp3_tool import kargs
from oxp3_tool.cli import main
from oxp3_tool.detect import CACHYOS, NATIVE_STEAM, Context
from oxp3_tool.state import STATE_FILE

from .fakeroot import CLI_INSTALLER_LIMINE, MACHINE_ID, FakeCachyOS, FakeOxp3

LIMINE_DROPIN = "/etc/limine-entry-tool.d/90-oxp3-tool.conf"
SDBOOT_DROPIN = "/etc/sdboot-manage.conf.d/90-oxp3-tool.conf"
GRUB_DROPIN = "/etc/default/grub.d/90-oxp3-tool.cfg"
NVME = "nvme.noacpi=1"
DSB = "xe.enable_dsb=0"


class Base(unittest.TestCase):
    bootloader = "limine"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name, "root")

    def fake(self, **kw):
        kw.setdefault("bootloader", self.bootloader)
        self.f = FakeCachyOS(self.root, **kw)
        return self.f

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(list(argv), system=self.f.system())
        return rc, out.getvalue(), err.getvalue()

    def read(self, path):
        p = self.root / path.lstrip("/")
        return p.read_text() if p.exists() else None

    def state(self):
        text = self.read(STATE_FILE)
        return json.loads(text)["fixes"] if text else {}

    def kernel_entries(self):
        """Entries of the regular kernels (no fallback, recovery or snapshot entries)."""
        return {k: v for k, v in self.f.entries().items()
                if not k.endswith("-fallback") and "recovery" not in k and k != "Snapshots"}


class PlatformTest(Base):
    def test_detected_as_cachyos(self):
        self.fake()
        ctx = Context(self.f.system())
        self.assertEqual(ctx.platform, CACHYOS)
        self.assertTrue(ctx.os_ok and ctx.cachyos_handheld and ctx.has_game_mode)
        self.assertEqual(ctx.game_mode_input, NATIVE_STEAM)
        self.assertEqual(ctx.inputplumber.version, "0.83.1")
        self.assertIsInstance(ctx.karg_backend, kargs.LimineBackend)
        rc, out, _ = self.cli("status")
        self.assertIn("OS:       CachyOS, Handheld Edition, kernel 7.2.11-1-cachyos-deckify\n", out)
        self.assertIn("Boot:     Limine (/etc/limine-entry-tool.d/90-oxp3-tool.conf, then `limine-mkinitcpio`)", out)
        self.assertNotIn("NOT Bazzite", out)
        self.assertNotIn("rpm", out)

    def test_arch_with_cachyos_repositories(self):
        self.fake()
        self.f.write("/etc/os-release", 'NAME="Arch Linux"\nID=arch\n')
        self.assertEqual(Context(self.f.system()).platform, CACHYOS)
        self.f.write("/etc/pacman.conf", "[core]\nInclude = x\n")
        ctx = Context(self.f.system())
        self.assertIsNone(ctx.platform)
        self.assertFalse(ctx.os_ok)
        rc, _, err = self.cli("apply", "--yes", "nvme-resume")
        self.assertNotEqual(rc, 0)
        self.assertIn("not Bazzite or CachyOS", err)

    def test_desktop_install_without_game_mode(self):
        self.fake(handheld=False)
        ctx = Context(self.f.system())
        self.assertFalse(ctx.has_game_mode)
        self.assertIsNone(ctx.game_mode_input)
        rc, out, _ = self.cli("status")
        self.assertIn("OS:       CachyOS, kernel", out)
        self.assertIn("CachyOS's Handheld Edition provides one", out)

    def test_inputplumber_missing(self):
        self.fake(inputplumber=False)
        rc, out, _ = self.cli("status")
        self.assertIn("`sudo pacman -S inputplumber`, then reboot", out)

    def test_lts_kernel_has_no_dc_balance_bug(self):
        self.fake(kernels={"linux-cachyos-lts": "6.18.55-2-cachyos-lts"})
        self.f.display_debugfs()
        ctx = Context(self.f.system())
        self.assertIs(ctx.refresh_state.dc_balance_risk, False)
        rc, out, _ = self.cli("status")
        self.assertIn("predates VRR DC balance (7.0)", out)
        self.assertNotIn("leave variable refresh", out)


class LimineTest(Base):
    def test_apply_status_revert(self):
        self.fake()
        rc, out, err = self.cli("apply", "--yes", "nvme-resume")
        self.assertEqual(rc, 0, err)
        dropin = self.read(LIMINE_DROPIN)
        self.assertIn("Installed by oxp3-tool (", dropin)
        self.assertIn('KERNEL_CMDLINE[default]+="nvme.noacpi=1"\n', dropin)
        self.assertEqual([c for c in self.f.runner.calls if c[0] == "limine-mkinitcpio"], [["limine-mkinitcpio"]])
        for name, args in self.kernel_entries().items():
            self.assertIn(NVME, args, name)
            self.assertIn("root=UUID=abcd", args, name)
        self.assertIn("Reboot to finish", out)
        self.assertNotIn("system update", out)
        self.assertEqual(self.state()["nvme-resume"]["kargs"], [NVME])
        rc, out, _ = self.cli("status")
        self.assertIn("nvme.noacpi=1 is set for the next boot (reboot to activate)", out)
        # Idempotent: nothing to rebuild the second time.
        self.f.runner.calls.clear()
        rc, out, _ = self.cli("apply", "--yes", "nvme-resume")
        self.assertEqual(rc, 0)
        self.assertNotIn(["limine-mkinitcpio"], self.f.runner.calls)
        # A kernel update rebuilds the entries from the configuration and keeps it.
        self.f.regenerate()
        self.f.boot()
        rc, out, _ = self.cli("status")
        self.assertIn("nvme.noacpi=1 is active", out)
        rc, out, err = self.cli("revert", "--yes", "nvme-resume")
        self.assertEqual(rc, 0, err)
        self.assertIsNone(self.read(LIMINE_DROPIN))
        for args in self.kernel_entries().values():
            self.assertNotIn(NVME, args)
        rc, out, _ = self.cli("status")
        self.assertIn("nvme.noacpi=1 is removed from the next boot (reboot to finish)", out)
        self.f.boot()
        self.cli("status")
        self.assertEqual(self.state(), {})

    def test_several_fixes_share_one_rebuild(self):
        self.fake()
        rc, _, err = self.cli("apply", "--yes", "nvme-resume", "xe-dsb-off")
        self.assertEqual(rc, 0, err)
        self.assertIn(f'KERNEL_CMDLINE[default]+="{NVME} {DSB}"', self.read(LIMINE_DROPIN))
        self.assertEqual(self.f.runner.calls.count(["limine-mkinitcpio"]), 1)
        rc, _, err = self.cli("revert", "--yes", "xe-dsb-off")
        self.assertEqual(rc, 0, err)
        self.assertIn(f'KERNEL_CMDLINE[default]+="{NVME}"', self.read(LIMINE_DROPIN))
        for args in self.kernel_entries().values():
            self.assertIn(NVME, args)
            self.assertNotIn(DSB, args)

    def test_argument_already_set_by_the_installer(self):
        self.fake(limine_default='ESP_PATH="/boot"\nKERNEL_CMDLINE[default]+="quiet rw root=UUID=abcd nvme.noacpi=1"\n')
        rc, _, err = self.cli("apply", "--yes", "nvme-resume")
        self.assertEqual(rc, 0, err)
        self.assertIsNone(self.read(LIMINE_DROPIN))
        self.assertNotIn(["limine-mkinitcpio"], self.f.runner.calls)
        self.assertEqual(self.state()["nvme-resume"]["notes"]["kargs_already_set"], [NVME])
        self.cli("revert", "--yes", "nvme-resume")
        for args in self.kernel_entries().values():
            self.assertIn(NVME, args)  # not ours to remove

    def test_plain_assignment_in_default_limine_is_refused(self):
        # cachyos-cli-installer writes KERNEL_CMDLINE[default]="..." (plain =), which drops every drop-in.
        self.fake(limine_default=CLI_INSTALLER_LIMINE)
        rc, _, err = self.cli("apply", "--yes", "nvme-resume")
        self.assertNotEqual(rc, 0)
        self.assertIn("would not be in its command line", err)
        self.assertIn("/etc/default/limine (inside the quotes of KERNEL_CMDLINE[default])", err)
        self.assertIsNone(self.read(LIMINE_DROPIN))
        self.assertNotIn(["limine-mkinitcpio"], self.f.runner.calls)
        self.assertEqual(self.state(), {})

    def test_no_default_command_line_is_refused(self):
        # Without any KERNEL_CMDLINE[default], ours would become the whole command line and lose root=.
        self.fake(limine_default='ESP_PATH="/boot"\n')
        self.f.write("/etc/kernel/cmdline", "quiet rw root=UUID=abcd\n")
        self.f.regenerate()
        rc, _, err = self.cli("apply", "--yes", "nvme-resume")
        self.assertNotEqual(rc, 0)
        self.assertIn("root= would be lost", err)
        self.assertIsNone(self.read(LIMINE_DROPIN))
        self.assertNotIn(["limine-mkinitcpio"], self.f.runner.calls)

    def test_kernel_with_its_own_command_line(self):
        self.fake(limine_default=('ESP_PATH="/boot"\nKERNEL_CMDLINE[default]+="quiet rw root=UUID=abcd"\n'
                                  'KERNEL_CMDLINE[linux-cachyos-lts]+="quiet rw root=UUID=abcd lts=1"\n'))
        rc, _, err = self.cli("apply", "--yes", "nvme-resume")
        self.assertEqual(rc, 0, err)
        self.assertIn('KERNEL_CMDLINE[linux-cachyos-lts]+="nvme.noacpi=1"', self.read(LIMINE_DROPIN))
        entries = self.kernel_entries()
        self.assertIn(NVME, entries["linux-cachyos-lts"])
        self.assertIn("lts=1", entries["linux-cachyos-lts"])
        self.assertIn(NVME, entries["linux-cachyos-deckify"])

    def test_conflicting_value_is_refused(self):
        self.fake(limine_default=('ESP_PATH="/boot"\n'
                                  'KERNEL_CMDLINE[default]+="quiet rw root=UUID=abcd xe.enable_dsb=1"\n'))
        rc, _, err = self.cli("apply", "--yes", "xe-dsb-off")
        self.assertNotEqual(rc, 0)
        self.assertIn("xe.enable_dsb=1 is set elsewhere and conflicts with xe.enable_dsb=0", err)
        self.assertIsNone(self.read(LIMINE_DROPIN))

    def test_pacman_running(self):
        self.fake()
        self.f.pacman_lock()
        rc, _, err = self.cli("apply", "--yes", "nvme-resume")
        self.assertNotEqual(rc, 0)
        self.assertIn("a pacman transaction is running", err)
        self.assertIsNone(self.read(LIMINE_DROPIN))

    def test_foreign_file_at_the_dropin_path(self):
        self.fake()
        self.f.write(LIMINE_DROPIN, 'KERNEL_CMDLINE[default]+="mine=1"\n')
        rc, _, err = self.cli("apply", "--yes", "nvme-resume")
        self.assertNotEqual(rc, 0)
        self.assertIn("which oxp3-tool did not write", err)
        self.assertEqual(self.read(LIMINE_DROPIN), 'KERNEL_CMDLINE[default]+="mine=1"\n')

    def test_failed_rebuild_puts_the_configuration_back(self):
        self.fake()
        self.f.runner.limine_exit = 1
        rc, _, err = self.cli("apply", "--yes", "nvme-resume")
        self.assertNotEqual(rc, 0)
        self.assertIn("limine-mkinitcpio failed (1); the previous configuration was put back", err)
        self.assertIsNone(self.read(LIMINE_DROPIN))
        self.assertNotIn(NVME, self.state().get("nvme-resume", {}).get("kargs", []))

    def test_hook_failure_after_the_entries_says_do_not_reboot(self):
        self.fake(secure_boot=True)
        self.f.runner.limine_exit = 3
        rc, out, err = self.cli("apply", "--yes", "nvme-resume")
        self.assertNotEqual(rc, 0)
        self.assertIn("Do not reboot yet", err)
        self.assertIn("Secure Boot is on", out)
        self.assertIsNotNone(self.read(LIMINE_DROPIN))
        self.f.runner.limine_exit = 0
        rc, _, err = self.cli("apply", "--yes", "nvme-resume")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.state()["nvme-resume"]["kargs"], [NVME])

    def test_kernel_left_out_of_the_rebuild_is_reported(self):
        self.fake()
        self.f.runner.limine_skip = {"linux-cachyos-lts"}
        rc, _, err = self.cli("apply", "--yes", "nvme-resume")
        self.assertNotEqual(rc, 0)
        self.assertIn("these boot entries lack the change: linux-cachyos-lts", err)
        # The drop-in stays: the next kernel update or a second apply finishes the job.
        self.f.runner.limine_skip = set()
        rc, _, err = self.cli("apply", "--yes", "nvme-resume")
        self.assertEqual(rc, 0, err)
        for args in self.kernel_entries().values():
            self.assertIn(NVME, args)

    def test_unified_kernel_images_are_left_to_the_user(self):
        self.fake(limine_default=('ESP_PATH="/boot"\nENABLE_UKI=yes\n'
                                  'KERNEL_CMDLINE[default]+="quiet rw root=UUID=abcd"\n'))
        rc, out, _ = self.cli("status")
        self.assertIn("[n/a         ] nvme-resume", out)
        self.assertIn("unified kernel images", out)
        self.assertIn("To set it by hand, add nvme.noacpi=1", out)

    def test_parse_limine_conf(self):
        conf = ("timeout: 5\n/+CachyOS\n  //linux-cachyos\n    comment: kernel-id=linux-cachyos\n"
                f"    path: boot():/{MACHINE_ID}/linux-cachyos/vmlinuz#0a\n    cmdline: quiet rw root=UUID=x a=1\n"
                f"  //linux-cachyos-fallback\n    path: boot():/{MACHINE_ID}/linux-cachyos/vmlinuz#0a\n"
                "    cmdline: quiet rw root=UUID=x\n  //Snapshots\n    ///1\n"
                f"      path: boot():/{MACHINE_ID}/limine_history/vmlinuz#00\n      cmdline: old=1\n")
        self.assertEqual(kargs.parse_limine_conf(conf, MACHINE_ID),
                         {"linux-cachyos": ["quiet", "rw", "root=UUID=x", "a=1"]})


class SdbootTest(Base):
    bootloader = "systemd-boot"

    def test_apply_and_revert(self):
        self.fake()
        rc, _, err = self.cli("apply", "--yes", "nvme-resume")
        self.assertEqual(rc, 0, err)
        self.assertIn('LINUX_OPTIONS+=" nvme.noacpi=1"', self.read(SDBOOT_DROPIN))
        self.assertEqual(self.f.runner.calls.count(["sdboot-manage", "gen"]), 1)
        entries = self.f.entries()
        for kernel in ("linux-cachyos-deckify", "linux-cachyos-lts"):
            self.assertIn(NVME, entries[kernel])
            self.assertNotIn(NVME, entries[f"{kernel}-fallback"])  # the fallback stays a way back
        rc, _, err = self.cli("revert", "--yes", "nvme-resume")
        self.assertEqual(rc, 0, err)
        self.assertIsNone(self.read(SDBOOT_DROPIN))
        self.assertNotIn(NVME, self.f.entries()["linux-cachyos-deckify"])

    def test_failed_generation_restores_and_rebuilds(self):
        self.fake()
        self.f.runner.gen_fail = 1
        rc, _, err = self.cli("apply", "--yes", "nvme-resume")
        self.assertNotEqual(rc, 0)
        self.assertIn("sdboot-manage gen failed (1); the previous configuration was put back", err)
        self.assertIsNone(self.read(SDBOOT_DROPIN))
        self.assertEqual(self.f.runner.calls.count(["sdboot-manage", "gen"]), 2)
        self.assertNotIn(NVME, self.f.entries()["linux-cachyos-deckify"])

    def test_hand_kept_entries_are_refused(self):
        self.fake()
        self.f.write("/etc/sdboot-manage.conf.d/50-mine.conf", 'NO_AUTOGEN="yes"\n')
        rc, _, err = self.cli("apply", "--yes", "nvme-resume")
        self.assertNotEqual(rc, 0)
        self.assertIn("NO_AUTOGEN=yes", err)
        self.assertIsNone(self.read(SDBOOT_DROPIN))


class GrubTest(Base):
    bootloader = "grub"

    def test_apply_and_revert(self):
        self.fake()
        rc, _, err = self.cli("apply", "--yes", "xe-dsb-off")
        self.assertEqual(rc, 0, err)
        self.assertIn('GRUB_CMDLINE_LINUX_DEFAULT="${GRUB_CMDLINE_LINUX_DEFAULT} xe.enable_dsb=0"',
                      self.read(GRUB_DROPIN))
        entries = self.f.entries()
        self.assertIn(DSB, entries["/vmlinuz-linux-cachyos-deckify"])
        self.assertIn("loglevel=3", entries["/vmlinuz-linux-cachyos-deckify"])
        self.assertNotIn(DSB, entries["/vmlinuz-linux-cachyos-deckify (recovery)"])
        rc, _, err = self.cli("revert", "--yes", "xe-dsb-off")
        self.assertEqual(rc, 0, err)
        self.assertIsNone(self.read(GRUB_DROPIN))
        self.assertNotIn(DSB, self.f.entries()["/vmlinuz-linux-cachyos-deckify"])


class ManualBootTest(Base):
    def test_refind_is_left_to_the_user(self):
        self.fake(bootloader="refind")
        rc, out, _ = self.cli("status")
        self.assertIn("Boot:     rEFInd boots this system; kernel-argument fixes have to be done by hand", out)
        self.assertIn("add nvme.noacpi=1 inside the quotes after \"Boot with standard options\"", out)
        rc, _, err = self.cli("apply", "--yes", "nvme-resume")
        self.assertNotEqual(rc, 0)
        self.assertIn("rEFInd boots this system", err)

    def test_two_boot_loaders_without_firmware_evidence(self):
        self.fake(loader_info=None)
        self.f.write("/etc/default/grub", "GRUB_CMDLINE_LINUX_DEFAULT='quiet'\n")
        self.f.write("/usr/bin/grub-mkconfig", "")
        backend = Context(self.f.system()).karg_backend
        self.assertIsInstance(backend, kargs.ManualBackend)
        self.assertIn("more than one boot loader is set up (limine, grub)", backend.refusal())

    def test_single_boot_loader_without_firmware_evidence(self):
        self.fake(loader_info=None)
        self.assertIsInstance(Context(self.f.system()).karg_backend, kargs.LimineBackend)

    def test_firmware_names_a_loader_whose_tools_are_missing(self):
        self.fake(bootloader="limine", loader_info="systemd-boot 258")
        backend = Context(self.f.system()).karg_backend
        self.assertIsInstance(backend, kargs.ManualBackend)
        self.assertIn("/etc/kernel/cmdline", backend.refusal())


class CachyFixesTest(Base):
    def test_buttons_without_steamos_manager(self):
        self.fake()
        rc, out, err = self.cli("apply", "--yes", "buttons")
        self.assertEqual(rc, 0, err)
        calls = [" ".join(c) for c in self.f.runner.calls]
        self.assertFalse([c for c in calls if "bazzite-native-steam-input" in c or "ujust" in c])
        self.assertIn("systemctl try-restart inputplumber.service", calls)
        self.assertIn("steamos-manager is not running", out)
        self.assertIn("`sudo pacman -S steamos-manager`", out)
        rc, out, err = self.cli("revert", "--yes", "buttons")
        self.assertEqual(rc, 0, err)
        self.assertFalse([c for c in self.f.runner.calls if "bazzite-native-steam-input" in " ".join(c)])

    def test_buttons_with_steamos_manager(self):
        self.fake(steamos_manager=True)
        rc, out, err = self.cli("apply", "--yes", "buttons")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("steamos-manager is not running", out)
        self.assertIn("steamos-manager selects the Steam Deck controller again", out)
        rc, out, _ = self.cli("status")
        self.assertIn("steamos-manager selects the Steam Deck controller", out)

    def test_volume_keys_rebuild_arch_hwdb(self):
        self.fake()
        rc, _, err = self.cli("apply", "--yes", "volume-keys")
        self.assertEqual(rc, 0, err)
        self.assertIn(["systemd-hwdb", "--usr", "update"], self.f.runner.calls)
        self.assertNotIn(["systemd-hwdb", "update"], self.f.runner.calls)
        self.f.write("/etc/udev/hwdb.bin", b"")
        self.f.runner.calls.clear()
        rc, _, err = self.cli("revert", "--yes", "volume-keys")
        self.assertEqual(rc, 0, err)
        self.assertIn(["systemd-hwdb", "--usr", "update"], self.f.runner.calls)
        self.assertIn(["systemd-hwdb", "update"], self.f.runner.calls)

    def test_recommended_set_matches_bazzite(self):
        self.fake()
        rc, out, err = self.cli("apply", "--yes")
        self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(self.state()), ["buttons", "nvme-resume", "oled-profile", "volume-keys"])
        self.assertEqual(self.f.runner.calls.count(["limine-mkinitcpio"]), 1)

    def test_hdr_backlight_plan_says_how_to_recover(self):
        self.fake()
        ctx = Context(self.f.system())
        from oxp3_tool.fixes import BY_ID
        plan = BY_ID["xe-hdr-backlight"].plan(ctx, {}, {})
        self.assertTrue(any("Limine menu" in m for m in plan.messages))

    def test_diagnose_collects_cachyos_files(self):
        self.fake()
        archive = Path(self.tmp.name, "diag.tar.gz")
        rc, _, err = self.cli("diagnose", "-o", str(archive))
        self.assertEqual(rc, 0, err)
        with tarfile.open(archive) as tar:
            names = tar.getnames()
            packages = tar.extractfile("oxp3-diagnose/commands/packages.txt").read().decode()
            limine = tar.extractfile("oxp3-diagnose/files/boot/limine.conf").read().decode()
        self.assertIn("$ pacman -Q", packages)
        self.assertFalse([n for n in names if "rpm-ostree" in n])
        self.assertIn("oxp3-diagnose/files/etc/default/limine", names)
        self.assertIn("oxp3-diagnose/commands/limine-cmdline-linux-cachyos-deckify.txt", names)
        self.assertNotIn(MACHINE_ID, limine)
        self.assertIn("<machine-id>", limine)


class BazziteUnchangedTest(unittest.TestCase):
    def test_bazzite_keeps_rpm_ostree(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = FakeOxp3(Path(tmp))
            ctx = Context(f.system())
            self.assertIsInstance(ctx.karg_backend, kargs.RpmOstreeBackend)
            self.assertEqual(ctx.karg_backend.how, "rpm-ostree kargs")


if __name__ == "__main__":
    unittest.main()
