"""`oxp3-tool kernel`: booting another kernel once on CachyOS with Limine, and getting back without a keyboard."""

import contextlib
import io
import re
import tempfile
import unittest
from pathlib import Path

from oxp3_tool import limine_menu as lm
from oxp3_tool.cli import main

from .fakeroot import LAST_BOOTED_VAR, LIMINE_EFI, ONESHOT_VAR, FakeCachyOS, FakeOxp3

STOCK, VSW = "linux-cachyos-deckify", "linux-cachyos-deckify-vsw"
KERNELS = {STOCK: "7.2.11-1-cachyos-deckify", VSW: "7.2.11-1-cachyos-deckify-vsw"}
CONF = "/boot/limine.conf"
ENROLL = 'ESP_PATH="/boot"\nKERNEL_CMDLINE[default]+="quiet rw root=UUID=abcd"\nENABLE_ENROLL_LIMINE_CONFIG=yes\n'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name, "root")

    def fake(self, **kw):
        kw.setdefault("kernels", dict(KERNELS))
        self.f = FakeCachyOS(self.root, **kw)
        self.assertEqual(self.f.power_on(), STOCK)  # the installed system has booted once
        return self.f

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(list(argv), system=self.f.system())
        return rc, out.getvalue(), err.getvalue()

    def conf(self):
        return (self.root / CONF.lstrip("/")).read_text()

    def running(self):
        return (self.root / "proc/sys/kernel/osrelease").read_text().strip()

    def install_vsw_later(self):
        """`pacman -U` of the second kernel: its hook adds the entry, keeping limine.conf's other lines."""
        self.f.runner.kernels[VSW] = KERNELS[VSW]
        self.f.write(f"/usr/lib/modules/{KERNELS[VSW]}/modules.builtin", "")
        self.f.regenerate()

    def remove_vsw(self):
        del self.f.runner.kernels[VSW]
        (self.root / f"usr/lib/modules/{KERNELS[VSW]}/modules.builtin").unlink()
        self.f.regenerate()


class TrapTest(Base):
    def test_without_the_tool_a_hung_kernel_boots_again_at_every_power_on(self):
        """Why the command exists: CachyOS's remember_last_entry makes any boot of a kernel stick."""
        self.fake()
        self.assertIn("remember_last_entry: yes", self.conf())
        self.f.runner(["bootctl", "set-oneshot", "CachyOS.linux-cachyos-deckify-vsw"])
        self.f.hangs.add(VSW)
        self.assertEqual(self.f.power_on(), VSW)
        self.assertEqual(self.f.power_on(), VSW)  # the power button does not get out of it


class TryTest(Base):
    def test_listing(self):
        self.fake()
        rc, out, err = self.cli("kernel")
        self.assertEqual(rc, 0, err)
        self.assertIn("* linux-cachyos-deckify      7.2.11-1-cachyos-deckify  (entry CachyOS/linux-cachyos-deckify)"
                      "  [running, Limine's default entry]", out)
        self.assertIn("  linux-cachyos-deckify-vsw  7.2.11-1-cachyos-deckify-vsw  "
                      "(entry CachyOS/linux-cachyos-deckify-vsw)\n", out)
        self.assertIn("Next boot: linux-cachyos-deckify (it booted last, and limine.conf has remember_last_entry: "
                      "yes).", out)

    def test_try_then_a_hang_then_power_cycle_boots_the_stock_kernel(self):
        self.fake()
        rc, out, err = self.cli("kernel", "try", VSW, "-y")
        self.assertEqual(rc, 0, err + out)
        self.assertIn("change `remember_last_entry: yes` to `remember_last_entry: no`", out)
        self.assertIn("`bootctl set-oneshot CachyOS.linux-cachyos-deckify-vsw`", out)
        self.assertIn("Ready. Reboot when you like", out)
        self.assertIn("Limine boots linux-cachyos-deckify after 5 seconds", out)
        self.assertIn('# oxp3-tool kernel try: was "remember_last_entry: yes"; `sudo oxp3-tool kernel keep` puts it '
                      'back\nremember_last_entry: no\n', self.conf())
        rc, out, _ = self.cli("kernel")
        self.assertIn("Next boot: linux-cachyos-deckify-vsw (once, asked for with `kernel try`).", out)
        self.assertIn("The power-on after that: linux-cachyos-deckify.", out)
        self.f.hangs.add(VSW)
        self.assertEqual(self.f.power_on(), VSW)
        self.assertEqual(self.f.power_on(), STOCK)
        self.assertEqual(self.running(), KERNELS[STOCK])
        self.assertEqual(self.f.power_on(), STOCK)

    def test_try_keep_and_remove(self):
        self.fake()
        self.assertEqual(self.cli("kernel", "try", VSW, "-y")[0], 0)
        self.assertEqual(self.f.power_on(), VSW)
        self.assertEqual(self.running(), KERNELS[VSW])
        rc, out, err = self.cli("kernel", "keep", "-y")
        self.assertEqual(rc, 0, err + out)
        self.assertIn("put `remember_last_entry: yes` back", out)
        self.assertIn("Then every power-on boots linux-cachyos-deckify-vsw.", out)
        self.assertNotIn("oxp3-tool", self.conf())
        self.assertIn("\nremember_last_entry: yes\n", self.conf())
        self.assertEqual(self.f.power_on(), VSW)
        self.assertEqual(self.f.power_on(), VSW)
        # Removing the package removes its entry; the remembered path then names nothing: the default boots.
        self.remove_vsw()
        self.assertEqual(self.f.power_on(), STOCK)

    def test_status_lines(self):
        self.fake()
        self.assertNotIn("Limine boots", self.cli("status")[1])
        self.cli("kernel", "try", VSW, "-y")
        out = " ".join(self.cli("status")[1].split())
        self.assertIn("Limine boots linux-cachyos-deckify-vsw once at the next boot (`oxp3-tool kernel try`), then "
                      "linux-cachyos-deckify", out)
        self.assertIn("Limine boots its default entry, linux-cachyos-deckify, at every power-on", out)
        self.f.power_on()
        out = " ".join(self.cli("status")[1].split())
        self.assertNotIn("once at the next boot", out)
        self.assertIn("linux-cachyos-deckify-vsw is a self-built kernel", out)

    def test_back_to_the_stock_kernel_from_a_kept_one(self):
        self.fake()
        self.cli("kernel", "try", VSW, "-y")
        self.f.power_on()
        self.cli("kernel", "keep", "-y")
        # Back to the stock kernel (Limine's default entry, perhaps a version never booted here): a failed boot must
        # come back to the kernel running now, so the default entry points at it until `keep`.
        rc, out, err = self.cli("kernel", "try", STOCK, "-y")
        self.assertEqual(rc, 0, err + out)
        self.assertIn("change `default_entry: 2` to `default_entry: CachyOS/linux-cachyos-deckify-vsw`", out)
        self.assertIn("do not remove linux-cachyos-deckify-vsw", out)
        self.assertIn("Limine boots linux-cachyos-deckify-vsw after 5 seconds", out)
        self.f.hangs.add(STOCK)
        self.assertEqual(self.f.power_on(), STOCK)
        self.assertEqual(self.f.power_on(), VSW)  # the way back
        self.f.hangs.clear()
        self.assertEqual(self.cli("kernel", "try", STOCK, "-y")[0], 0)
        self.assertEqual(self.f.power_on(), STOCK)
        rc, out, err = self.cli("kernel", "keep", "-y")
        self.assertEqual(rc, 0, err + out)
        self.assertIn("put `default_entry: 2` back", out)
        self.assertNotIn("oxp3-tool", self.conf())
        self.assertIn("default_entry: 2\nremember_last_entry: yes\n", self.conf())
        self.assertEqual(self.f.power_on(), STOCK)
        self.remove_vsw()
        self.assertEqual(self.f.power_on(), STOCK)

    def test_keep_before_rebooting_cancels_the_try(self):
        self.fake()
        self.cli("kernel", "try", VSW, "-y")
        rc, out, err = self.cli("kernel", "keep", "-y")
        self.assertEqual(rc, 0, err + out)
        self.assertIn("cancel the one boot of linux-cachyos-deckify-vsw", out)
        self.assertFalse((self.root / ONESHOT_VAR.lstrip("/")).exists())
        self.assertEqual(self.f.power_on(), STOCK)

    def test_kernel_update_keeps_the_change(self):
        self.fake(kernels={STOCK: KERNELS[STOCK]})
        self.install_vsw_later()
        self.assertEqual(self.cli("kernel", "try", VSW, "-y")[0], 0)
        self.f.regenerate()  # a kernel update's hook rewrites limine.conf
        self.assertIn("remember_last_entry: no", self.conf())
        self.f.hangs.add(VSW)
        self.assertEqual(self.f.power_on(), VSW)
        self.assertEqual(self.f.power_on(), STOCK)

    def test_try_twice_changes_the_file_once(self):
        self.fake()
        self.cli("kernel", "try", VSW, "-y")
        conf = self.conf()
        rc, out, _ = self.cli("kernel", "try", VSW, "-y")
        self.assertEqual(rc, 0)
        self.assertNotIn("remember_last_entry: no` in", out)
        self.assertEqual(self.conf(), conf)

    def test_dry_run_and_no_answer_change_nothing(self):
        self.fake()
        conf = self.conf()
        rc, out, _ = self.cli("kernel", "try", VSW, "-n")
        self.assertEqual(rc, 0)
        self.assertIn("This will:", out)
        self.assertEqual(self.conf(), conf)
        self.assertFalse((self.root / ONESHOT_VAR.lstrip("/")).exists())
        rc, _, err = self.cli("kernel", "try", VSW)  # stdin is no terminal: no confirmation
        self.assertEqual(rc, 1)
        self.assertEqual(self.conf(), conf)


class DiagnoseTest(Base):
    def test_findings_name_their_boot_and_kernel(self):
        import tarfile
        self.fake()
        self.f.runner.journal["-1"] = ("Linux version 7.2.11-1-cachyos-deckify (linux-cachyos-deckify@cachyos) ...\n"
                                       "xe 0000:00:02.0: [drm] Port A asks to use VBT vswing/preemph tables\n")
        self.f.runner.journal["0"] = "Linux version 7.2.11-1-cachyos-deckify-vsw (user@host) ...\n"
        archive = Path(self.tmp.name, "diag.tar.gz")
        rc, out, err = self.cli("diagnose", "-o", str(archive))
        self.assertEqual(rc, 0, err)
        with tarfile.open(archive) as tar:
            findings = tar.extractfile("oxp3-diagnose/findings.txt").read().decode()
            kernels = tar.extractfile("oxp3-diagnose/kernels.txt").read().decode()
        self.assertIn("Next boot: linux-cachyos-deckify", kernels)
        self.assertIn("/asks to use VBT vswing/preemph tables/  (previous boot, 7.2.11-1-cachyos-deckify) The firmware "
                      "asks for its own eDP signal levels, which a kernel without the fix ignores", findings)
        self.assertNotIn("this boot", findings)


class RefusalTest(Base):
    def refused(self, *argv):
        conf = self.conf()
        rc, out, err = self.cli("kernel", *argv)
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.conf(), conf)
        self.assertFalse((self.root / ONESHOT_VAR.lstrip("/")).exists())
        return err

    def test_running_or_unknown_kernel(self):
        self.fake()
        self.assertIn("is the kernel running now", self.refused("try", STOCK, "-y"))
        self.assertIn("is not an installed kernel package (installed: linux-cachyos-deckify, "
                      "linux-cachyos-deckify-vsw)", self.refused("try", "linux-cachyos-rc", "-y"))

    def test_running_kernel_without_its_own_entry(self):
        self.fake()
        self.f.write("/proc/sys/kernel/osrelease", "7.2.11-1-cachyos-deckify-snapshot\n")
        self.assertIn("has no usable Limine entry of its own", self.refused("try", VSW, "-y"))

    def test_changed_kernel_files(self):
        self.fake()
        self.f.write(f"/boot/0123456789abcdef0123456789abcdef/{VSW}/vmlinuz", "other")
        self.assertIn("files on the ESP are missing or changed", self.refused("try", VSW, "-y"))

    def test_crlf(self):
        self.fake()
        self.f.write(CONF, self.conf().replace("\n", "\r\n"))
        self.assertIn("Windows (CRLF) line endings", self.refused("try", VSW, "-y"))

    def test_default_naming_nothing_is_replaced(self):
        """A default_entry Limine cannot boot (a directory, a removed kernel): the trial's fallback replaces it."""
        self.fake()
        for value in ("1", "CachyOS/linux-cachyos-lts", "9"):
            self.f.write(CONF, re.sub(r"default_entry: .*", f"default_entry: {value}", self.conf(), count=1))
            rc, out, err = self.cli("kernel", "try", VSW, "-n")
            self.assertEqual(rc, 0, err)
            self.assertIn(f"change `default_entry: {value}` to `default_entry: CachyOS/linux-cachyos-deckify`", out)

    def test_default_is_the_kernel_being_tried(self):
        self.fake()
        self.f.write(CONF, self.conf().replace("default_entry: 2", "default_entry: CachyOS/linux-cachyos-deckify-vsw"))
        rc, out, err = self.cli("kernel", "try", VSW, "-y")
        self.assertEqual(rc, 0, err)
        self.assertIn("change `default_entry: CachyOS/linux-cachyos-deckify-vsw` to `default_entry: "
                      "CachyOS/linux-cachyos-deckify`", out)  # to the running kernel
        self.f.hangs.add(VSW)
        self.assertEqual(self.f.power_on(), VSW)
        self.assertEqual(self.f.power_on(), STOCK)

    def test_timeout_no(self):
        self.fake()
        self.f.write(CONF, "timeout: no\n" + self.conf())
        self.assertIn("timeout: no", self.refused("try", VSW, "-y"))

    def test_earlier_limine_conf(self):
        self.fake()
        self.f.write("/boot/EFI/limine/limine.conf", "timeout: 1\n")
        self.assertIn("Limine reads /boot/EFI/limine/limine.conf before", self.refused("try", VSW, "-y"))

    def test_not_booted_by_limine(self):
        self.fake()
        (self.root / "sys/firmware/efi/efivars/LoaderInfo-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f").unlink()
        self.assertIn("did not start from Limine", self.refused("try", VSW, "-y"))

    def test_pacman_running(self):
        self.fake()
        self.f.pacman_lock()
        self.assertIn("pacman is running", self.refused("try", VSW, "-y"))

    def test_enrolled_checksum_without_enrolling_set_up(self):
        self.fake()
        self.f.write(LIMINE_EFI, b"MZ" + lm.B2SUM_SIGNATURE + lm.b2sum(self.conf().encode()).encode() + b"\0")
        self.assertIn("cannot enroll a changed limine.conf", self.refused("try", VSW, "-y"))

    def test_not_cachyos(self):
        self.f = FakeOxp3(self.root)
        self.f.build()
        rc, _, err = self.cli("kernel")
        self.assertEqual(rc, 1)
        self.assertIn("is for CachyOS booting with Limine", err)


class ConcurrencyTest(Base):
    def test_waits_for_the_boot_partition_lock(self):
        import fcntl
        import os
        from oxp3_tool import kernel_cmd
        self.fake()
        conf = self.conf()
        path = self.root / "run/lock/boot-partition.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)  # limine-snapper-sync is rewriting limine.conf
        self.addCleanup(os.close, fd)
        old = kernel_cmd.LimineKernels.lock_timeout
        kernel_cmd.LimineKernels.lock_timeout = 0.2
        self.addCleanup(setattr, kernel_cmd.LimineKernels, "lock_timeout", old)
        rc, out, err = self.cli("kernel", "try", VSW, "-y")
        self.assertEqual(rc, 1)
        self.assertIn("is changing the boot partition", err)
        self.assertEqual(self.conf(), conf)
        self.assertFalse((self.root / ONESHOT_VAR.lstrip("/")).exists())

    def test_rewritten_while_asking(self):
        from oxp3_tool import cli
        self.fake()

        def confirm(prompt, yes):
            self.f.regenerate()  # a kernel update finished meanwhile
            self.f.write(CONF, self.conf() + "\n")
            return True
        old = cli._confirm
        cli._confirm = confirm
        self.addCleanup(setattr, cli, "_confirm", old)
        rc, out, err = self.cli("kernel", "try", VSW)
        self.assertEqual(rc, 1)
        self.assertIn("changed while this ran", err)
        self.assertNotIn("oxp3-tool", self.conf())

    def test_a_stale_save_after_the_change_cancels_the_one_shot(self):
        self.fake()
        conf = self.conf()
        self.f.runner.after_oneshot = lambda: self.f.write(CONF, conf)  # another tool saves the old tree
        rc, out, err = self.cli("kernel", "try", VSW, "-y")
        self.assertEqual(rc, 1)
        self.assertIn("the one boot of linux-cachyos-deckify-vsw was cancelled", err)
        self.assertFalse((self.root / ONESHOT_VAR.lstrip("/")).exists())
        self.assertEqual(self.f.power_on(), STOCK)

    def test_ctrl_c_and_hangup_during_enrolling_do_not_stop_it(self):
        import signal
        for signum in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
            self.tmp.cleanup()
            self.fake(limine_default=ENROLL)
            self.f.regenerate()
            self.f.runner.kill["limine-enroll-config"] = (0, signum)
            rc, out, err = self.cli("kernel", "try", VSW, "-y")
            self.assertEqual(rc, 0, err + out)
            self.f.hangs.add(VSW)
            self.assertEqual(self.f.power_on(), VSW)
            self.assertEqual(self.f.power_on(), STOCK)  # checksum fine

    def test_mismatched_checksum_is_shown(self):
        self.fake(limine_default=ENROLL)
        self.f.regenerate()
        self.f.write(CONF, self.conf() + "# edited\n")
        rc, out, _ = self.cli("kernel")
        self.assertIn("DO NOT REBOOT: limine.conf does not match the checksum enrolled", out)
        self.assertIn("DO NOT REBOOT", self.cli("status")[1])


class RootTest(Base):
    def test_needs_root_before_the_lock(self):
        from unittest import mock
        from oxp3_tool.system import System
        self.fake()
        with mock.patch.object(System, "is_real_root", new_callable=mock.PropertyMock, return_value=True), \
                mock.patch("os.geteuid", return_value=1000):
            for argv in (("kernel", "keep"), ("kernel", "try", VSW, "-y")):
                rc, out, err = self.cli(*argv)
                self.assertEqual(rc, 1)
                self.assertIn("This needs root. Run it with sudo.", err)


class EnrolledTest(Base):
    def test_try_and_keep_enroll_the_new_checksum(self):
        self.fake(limine_default=ENROLL)
        self.f.regenerate()
        self.assertEqual(self.f.power_on(), STOCK)
        self.assertEqual(lm.enrolled_hashes(self.f.system(), "/boot"),
                         {"EFI/limine/limine_x64.efi": lm.b2sum(self.conf().encode())})
        rc, out, err = self.cli("kernel", "try", VSW, "-y")
        self.assertEqual(rc, 0, err + out)
        self.assertIn("then enroll its checksum", out)
        self.f.hangs.add(VSW)
        self.assertEqual(self.f.power_on(), VSW)
        self.assertEqual(self.f.power_on(), STOCK)  # no checksum mismatch
        self.f.hangs.clear()
        self.cli("kernel", "try", VSW, "-y")
        self.f.power_on()
        self.assertEqual(self.cli("kernel", "keep", "-y")[0], 0)
        self.assertEqual(self.f.power_on(), VSW)

    def test_failed_enrolling_puts_the_old_file_back(self):
        self.fake(limine_default=ENROLL)
        self.f.regenerate()
        conf = self.conf()
        self.f.runner.enroll_exit = 1
        rc, out, err = self.cli("kernel", "try", VSW, "-y")
        self.assertEqual(rc, 1)
        self.assertIn("limine-enroll-config failed (1)", out)
        self.assertIn("putting the previous /boot/limine.conf back", out)
        self.assertIn("/boot/limine.conf was not changed", err)
        self.assertEqual(self.conf(), conf)
        self.f.runner.enroll_exit = 0
        self.assertEqual(self.f.power_on(), STOCK)


    def test_failed_enrolling_that_dropped_the_checksum(self):
        self.fake(limine_default=ENROLL)
        self.f.regenerate()
        conf = self.conf()
        self.f.runner.enroll_exit, self.f.runner.enroll_strips = 1, True
        rc, out, err = self.cli("kernel", "try", VSW, "-y")
        self.assertEqual(rc, 1)
        self.assertIn("has no checksum of it enrolled now", err)
        self.assertNotIn("DO NOT REBOOT", err)
        self.assertEqual(self.conf(), conf)
        self.assertEqual(self.f.power_on(), STOCK)
        self.f.runner.enroll_exit = 0
        self.assertEqual(self.cli("kernel", "try", VSW, "-y")[0], 0)  # enrolls again


class MenuTest(unittest.TestCase):
    CONF = ("timeout: 3\n default_entry: 3 \nRemember_Last_Entry: Yes\n"
            "/+CachyOS\n  //linux-cachyos-deckify\n    comment: kernel-id=linux-cachyos-deckify\n"
            "    protocol: linux\n    path: boot():/m/linux-cachyos-deckify/vmlinuz#00\n"
            "  //Renamed\n    comment: kernel-id=linux-cachyos-lts \n    path: boot():/m/linux-cachyos-lts/vmlinuz\n"
            "  //Snapshots\n    ///1 2026-10-27\n      path: boot():/m/x/vmlinuz\n"
            "/Other systems\n  //Windows/Boot\n    protocol: efi\n  //Windows/Boot\n    protocol: efi\n"
            "  //Old BIOS\n    protocol: bios\n")

    def test_read_like_limine(self):
        menu = lm.parse(self.CONF)
        self.assertEqual(menu.value("default_entry"), "3")
        self.assertTrue(menu.remembers())
        self.assertEqual(menu.timeout_text(), "3 seconds")
        lts = menu.kernel_entry("linux-cachyos-lts")
        self.assertEqual(menu.path(lts), "CachyOS/Renamed")
        self.assertIs(menu.default(), lts)
        self.assertEqual(menu.by_index(3).name, "Snapshots")  # a collapsed directory hides its entries
        self.assertEqual(menu.by_index(4).name, "Other systems")
        wins = [e for e in menu.walk() if e.name == "Windows/Boot"]
        self.assertEqual([menu.path(e) for e in wins], ["Other systems/Windows\\/Boot", "Other systems/Windows\\/Boot#1"])
        self.assertEqual([menu.bli_id(e) for e in wins], ["Other-systems.Windows-.Boot", "Other-systems.Windows-.Boot-1"])
        self.assertIs(menu.by_path("Other systems/Windows\\/Boot#1"), wins[1])
        self.assertIs(menu.by_bli_id_or_path("Other-systems.Windows-.Boot-1"), wins[1])
        self.assertIsNone(menu.by_path("Other systems/Old BIOS"))  # hidden on UEFI
        self.assertEqual(menu.boots(None, "CachyOS/linux-cachyos-deckify")[1], "remembered")
        self.assertEqual(menu.boots("CachyOS.Renamed", "CachyOS/linux-cachyos-deckify"), (lts, "one-shot"))
        self.assertEqual(menu.boots(None, "CachyOS/gone"), (lts, "default"))

    def test_defaults_that_stop_at_the_menu(self):
        for value in ("1", "CachyOS", "CachyOS/missing", "9"):
            menu = lm.parse(self.CONF.replace("default_entry: 3", f"default_entry: {value}"))
            self.assertIsNone(menu.default(), value)  # 9: past the end names nothing
        flat = lm.parse("default_entry: 5\n/a\n  path: boot():/a\n/b\n  path: boot():/b\n")
        self.assertIsNone(flat.default())  # not the first entry, as an older model had it
        self.assertEqual(flat.by_index(1).name, "b")

    def test_globals_end_at_the_first_entry(self):
        menu = lm.parse("timeout: 3\n/+CachyOS\n  //k\n    path: boot():/k\ndefault_entry: 2\nremember_last_entry: yes\n")
        self.assertIsNone(menu.value("default_entry"))
        self.assertFalse(menu.remembers())
        self.assertIsNone(menu.default())  # index 0: the directory
        self.assertEqual(lm.parse("/k\n  path: boot():/k\ndefault_entry: 1\n").value("default_entry"), "1")

    def test_arch_conditions(self):
        def hidden(cond):
            return lm.parse(f"/k\n  {cond}\n  path: boot():/k\n").top[0].skipped()
        for cond in ("if_arch: x86_64", "if_arch: amd64", "if_arch: x86-64,aarch64", "if_loader_arch: ia-32",
                     "if_fw_type: bios", "protocol: bios"):
            self.assertTrue(hidden(cond), cond)
        for cond in ("if_arch: X86-64 aarch64", "if_loader_arch: x86-64", "if_fw_type: UEFI", "protocol: linux"):
            self.assertFalse(hidden(cond), cond)

    def test_paths_round_trip(self):
        menu = lm.parse("/A\\\n  //x\n/snap #3\n  path: boot():/a\n/snap #3\n  path: boot():/b\n/linux é\n  path: boot():/c\n")
        for e in menu.walk():
            self.assertIs(menu.by_path(menu.path(e)), e, menu.path(e))
        self.assertEqual(menu.bli_id(menu.top[3]), "linux---")  # one '-' per UTF-8 byte, as Limine
        long = lm.parse("/" + "a" * 300 + "\n  path: boot():/a\n")
        self.assertEqual(len(menu.bli_id(long.top[0]) if False else long.bli_id(long.top[0])), 232)

    def test_refused_files(self):
        with self.assertRaises(lm.MenuError):
            lm.parse("timeout: 3\r\n/k\n")
        with self.assertRaises(lm.MenuError):
            lm.parse("${OS}=CachyOS\n/+${OS}\n")

    def test_a_directory_named_by_the_one_shot_stops_at_the_menu(self):
        menu = lm.parse(self.CONF)
        self.assertEqual(menu.boots("CachyOS", None), (None, "the one-shot entry is a directory"))

    def test_set_global_and_marker(self):
        text = lm.set_global(self.CONF, "remember_last_entry", "remember_last_entry: no",
                             marker=lm.marker_for("Remember_Last_Entry: Yes"))
        menu = lm.parse(text)
        self.assertFalse(menu.remembers())
        self.assertEqual(lm.marked_original(menu), "Remember_Last_Entry: Yes")
        back = lm.set_global(text, "remember_last_entry", "Remember_Last_Entry: Yes", marker=None)
        self.assertEqual(back, self.CONF)
        with self.assertRaises(lm.MenuError):
            lm.set_global(self.CONF, "editor_enabled", "editor_enabled: no")

    def test_colliding_ids_get_a_number(self):
        menu = lm.parse("/+A b\n  //x\n/A-b\n  //x\n")
        self.assertEqual([menu.bli_id(e) for e in menu.walk() if not e.is_dir], ["A-b.x", "A-b.x-2"])

    def test_parentless_child(self):
        with self.assertRaises(lm.MenuError):
            lm.parse("/+A\n///x\n")


if __name__ == "__main__":
    unittest.main()
