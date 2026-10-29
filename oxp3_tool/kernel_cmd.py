"""`oxp3-tool kernel`: boot another installed kernel once, without a keyboard, on CachyOS with Limine.

CachyOS writes `remember_last_entry: yes` into limine.conf: Limine boots whatever booted last, so a kernel that
booted once (and then hung, or left the screen dark) boots again at every power-on, and only a key press in
Limine's menu gets back. `kernel try` sets `remember_last_entry: no` first (Limine then boots its default entry at
every power-on) and asks Limine for one boot of the other kernel (LoaderEntryOneShot, through bootctl).
`kernel keep` puts the line back, so the kernel running then is the one Limine keeps booting.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Callable, Optional

from . import limine_menu as lm
from .detect import CACHYOS, Context
from .kargs import PACMAN_LOCK, LimineBackend

Out = Callable[[str], None]


class KernelError(Exception):
    pass


@dataclass
class Kernel:
    package: str
    release: str
    entry: Optional[lm.Entry]


class LimineKernels:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.sys = ctx.sys
        backend = ctx.karg_backend if ctx.os_family == CACHYOS else None
        if not isinstance(backend, LimineBackend):
            raise KernelError("`oxp3-tool kernel` is for CachyOS booting with Limine (and limine-entry-tool)")
        self.backend = backend
        self.esp = backend.esp
        if not self.esp:
            raise KernelError("the EFI system partition with limine.conf is not mounted")
        self.conf = f"{self.esp}/limine.conf"
        data = self.sys.read_bytes(self.conf)
        if data is None:
            raise KernelError(f"cannot read {self.conf}" +
                              (" (run it with sudo)" if self.sys.is_real_root and os.geteuid() != 0 else ""))
        self.data = data
        try:
            self.menu = lm.parse(data.decode("utf-8"))
        except (UnicodeDecodeError, lm.MenuError) as e:
            raise KernelError(f"{self.conf} has something this tool does not read like Limine does: {e}") from e

    # -- what is installed and what boots ---------------------------------------------

    def kernels(self) -> list[Kernel]:
        out = []
        for path in self.sys.glob("/usr/lib/modules/*/modules.builtin"):
            release = path.split("/")[4]
            res = self.sys.run(["pacman", "-Qqo", path], timeout=60)
            name = res.stdout.strip() if res.ok else ""
            if re.fullmatch(r"[A-Za-z0-9@._+-]+", name):
                out.append(Kernel(name, release, self.menu.kernel_entry(name)))
        return sorted(out, key=lambda k: k.package)

    def running(self, kernels: list[Kernel]) -> Optional[Kernel]:
        release = self.ctx.kernel_release
        return next((k for k in kernels if k.release == release), None)

    @staticmethod
    def owner(entry: Optional[lm.Entry], kernels: list[Kernel]) -> Optional[Kernel]:
        """The kernel package an entry boots (limine-entry-tool's entry for it, in any parse of limine.conf)."""
        if entry is None or entry.is_dir:
            return None
        return next((k for k in kernels if (entry.kernel_id or entry.name) == k.package), None)

    @property
    def oneshot(self) -> Optional[str]:
        return lm.read_efivar(self.sys, lm.ONESHOT_VAR, utf16=True)

    @property
    def last_booted(self) -> Optional[str]:
        return lm.read_efivar(self.sys, lm.LAST_BOOTED_VAR, utf16=False)

    def files_ok(self, entry: lm.Entry) -> bool:
        """The kernel (and initramfs) the entry loads are on the ESP."""
        for key in ("path", "kernel_path", "module_path"):
            value = entry.options.get(key)
            if value is None:
                continue
            m = re.fullmatch(r"boot\(\):(/[^#]+)(#[0-9a-fA-F]*)?", value)
            if not m or not self.sys.is_file(self.esp + m.group(1)):
                return False
        return "path" in entry.options or "kernel_path" in entry.options

    def name(self, entry: Optional[lm.Entry], kernels: list[Kernel], menu: Optional[lm.Menu] = None) -> str:
        if entry is None:
            return "nothing (Limine waits in its menu)"
        k = self.owner(entry, kernels)
        return k.package if k else f"'{(menu or self.menu).path(entry)}'"

    # -- changing limine.conf ---------------------------------------------------------

    def edit_problem(self) -> Optional[str]:
        """Why limine.conf must not be changed now, if it must not."""
        for rel in lm.EARLIER_CONFIGS:
            if self.sys.is_file(f"{self.esp}/{rel}"):
                return f"Limine reads {self.esp}/{rel} before {self.conf}, so a change to the latter would do nothing"
        info = lm.read_efivar(self.sys, f"{lm.EFIVARS}/LoaderInfo-{lm.BLI_GUID}", utf16=True) or ""
        if not info.startswith("Limine"):
            return "this boot did not start from Limine (its UEFI boot loader variables are missing)"
        if self.backend.unfinished() is not None:
            return "a boot-entry rebuild did not finish (see `sudo oxp3-tool status`)"
        if self.sys.exists(PACMAN_LOCK):
            return "pacman is running (a kernel update rewrites limine.conf); try again when it is done"
        hashes = lm.enrolled_hashes(self.sys, self.esp)
        if hashes and not self.backend.enroll_enabled:
            return ("Limine's EFI binary has limine.conf's checksum enrolled, but ENABLE_ENROLL_LIMINE_CONFIG is "
                    "not yes in /etc/default/limine, so this tool cannot enroll a changed limine.conf")
        current = lm.b2sum(self.data)
        if any(h != current for h in hashes.values()):
            return ("the checksum enrolled in Limine's EFI binary does not match limine.conf now; run "
                    "`sudo limine-update`, check that it ends without errors, and try again")
        if self.backend.enroll_enabled and not self.sys.which("limine-enroll-config"):
            return "limine-enroll-config is missing (it enrolls limine.conf's checksum after a change)"
        return None

    def write(self, new: bytes, out: Out) -> None:
        """Replace limine.conf, enroll its checksum where that is set up, and check what Limine will verify;
        any failure puts the old file back."""
        if self.sys.read_bytes(self.conf) != self.data:
            raise KernelError(f"{self.conf} changed while this ran; run the command again")
        old = self.data
        had_checksum = bool(lm.enrolled_hashes(self.sys, self.esp))
        self.sys.write_atomic(self.conf, new, mode=None)
        problem = self._settle(new, enroll=True)
        if problem is None:
            self.data = new
            self.menu = lm.parse(new.decode("utf-8"))
            return
        out(f"error: {problem}; putting the previous {self.conf} back")
        self.sys.write_atomic(self.conf, old, mode=None)
        # A failed limine-enroll-config may have left the binary as it was (its checksum still fits the old file)
        # or put back the binary without any checksum; enroll again only when that is needed.
        hashes = lm.enrolled_hashes(self.sys, self.esp)
        again = self._settle(old, enroll=any(h != lm.b2sum(old) for h in hashes.values()) or
                             (had_checksum and not hashes))
        if again is not None:
            if self.sys.read_bytes(self.conf) == old and not lm.enrolled_hashes(self.sys, self.esp):
                raise KernelError(f"the previous {self.conf} is back, but Limine's EFI binary has no checksum of it "
                                  f"enrolled now ({again}): Limine boots, without that check. Run "
                                  "`sudo limine-enroll-config` and check that it ends without errors")
            raise KernelError(f"DO NOT REBOOT: the previous {self.conf} is back, but {again}. Run "
                              "`sudo limine-enroll-config` and check that it ends without errors")
        raise KernelError(f"{self.conf} was not changed")

    def _settle(self, data: bytes, enroll: bool) -> Optional[str]:
        """None when limine.conf holds ``data`` and every checksum enrolled in Limine's binary is that file's."""
        if self.sys.read_bytes(self.conf) != data:
            return f"{self.conf} does not read back as written"
        if enroll and self.backend.enroll_enabled:
            res = self.sys.run(["limine-enroll-config"], timeout=180)
            if not res.ok:
                return f"limine-enroll-config failed ({res.returncode}): {(res.stderr or res.stdout).strip()[-300:]}"
        want = lm.b2sum(data)
        for rel, value in lm.enrolled_hashes(self.sys, self.esp).items():
            if value != want:
                return f"{self.esp}/{rel} has a checksum enrolled that does not match"
        return None

    def set_oneshot(self, value: str) -> None:
        if not self.sys.which("bootctl"):
            raise KernelError("bootctl (systemd) is missing")
        res = self.sys.run(["bootctl", "set-oneshot", value], timeout=60)
        if not res.ok or (self.oneshot or "") != value:
            raise KernelError(f"`bootctl set-oneshot {value}` failed: {(res.stderr or res.stdout).strip()[-300:]}")


# -- the command ----------------------------------------------------------------------


def _describe(lk: LimineKernels, kernels: list[Kernel], out: Out) -> None:
    menu = lk.menu
    running = lk.running(kernels)
    default = menu.default()
    width = max([len(k.package) for k in kernels] + [10])
    out(f"Kernels ({lk.conf}):")
    for k in kernels:
        tags = []
        if k is running:
            tags.append("running")
        if k.entry is not None and k.entry is default:
            tags.append("Limine's default entry")
        if k.entry is None:
            where = "no Limine entry"
        elif not lk.files_ok(k.entry):
            where = f"entry {menu.path(k.entry)}, its files are missing on the ESP"
        else:
            where = f"entry {menu.path(k.entry)}"
        out(f"  {'*' if k is running else ' '} {k.package:<{width}}  {k.release}  ({where})"
            + (f"  [{', '.join(tags)}]" if tags else ""))
    nxt, why = menu.boots(lk.oneshot, lk.last_booted)
    reason = {"one-shot": "once, asked for with `kernel try`",
              "remembered": "it booted last, and limine.conf has remember_last_entry: yes",
              "default": "Limine's default entry" + ("" if menu.remembers() else
                                                     " (remember_last_entry is off)"),
              }.get(why, why)
    out(f"Next boot: {lk.name(nxt, kernels)} ({reason}).")
    if why == "one-shot":
        after, _ = menu.boots(None, None if not menu.remembers() else menu.path(nxt))
        out(f"The power-on after that: {lk.name(after, kernels)}.")
    if lm.marked_original(menu) is not None:
        out("remember_last_entry is off since `oxp3-tool kernel try`; `sudo oxp3-tool kernel keep` (on the kernel "
            "to keep) puts it back.")
    if default is not None and lk.owner(default, kernels) is None:
        out(f"note: Limine's default entry, '{menu.path(default)}', is no installed kernel's entry.")
    if default is None:
        out("note: limine.conf's default_entry names no bootable entry: after a failed boot Limine waits in its menu.")


def cmd_kernel(ctx: Context, action: Optional[str], package: Optional[str], assume_yes: bool,
               dry_run: bool, out: Out, confirm: Callable[[str, bool], bool]) -> int:
    lk = LimineKernels(ctx)
    kernels = lk.kernels()
    if action is None:
        _describe(lk, kernels, out)
        return 0
    if action == "try":
        return _try(lk, kernels, package or "", assume_yes, dry_run, out, confirm)
    return _keep(lk, kernels, assume_yes, dry_run, out, confirm)


def _try(lk: LimineKernels, kernels: list[Kernel], package: str, assume_yes: bool, dry_run: bool, out: Out,
         confirm: Callable[[str, bool], bool]) -> int:
    menu = lk.menu
    target = next((k for k in kernels if k.package == package), None)
    names = ", ".join(k.package for k in kernels)
    if target is None:
        raise KernelError(f"{package} is not an installed kernel package (installed: {names})")
    if target.entry is None or not lk.files_ok(target.entry):
        raise KernelError(f"{package} has no usable Limine entry in {lk.conf}; run `sudo limine-update`")
    running = lk.running(kernels)
    if target is running:
        raise KernelError(f"{package} is the kernel running now" +
                          ("; `sudo oxp3-tool kernel keep` keeps it" if lm.marked_original(menu) is not None else ""))
    problem = lk.edit_problem()
    if problem:
        raise KernelError(problem)
    if not menu.autoboots():
        raise KernelError(f"{lk.conf} has `timeout: no`: after a failed boot Limine would wait in its menu for a key")
    fallback = menu.default()
    owner = lk.owner(fallback, kernels)
    if fallback is None or owner is None or not lk.files_ok(fallback):
        raise KernelError(f"Limine's default entry ({menu.value('default_entry') or 'none set'}) is not an installed "
                          "kernel's entry: after a failed boot there would be nothing to fall back to. This tool "
                          "does not change default_entry")
    original = None
    at = menu.value_line("remember_last_entry")
    if menu.remembers():
        if at is None or at >= menu.first_entry_line:
            raise KernelError(f"remember_last_entry in {lk.conf} is not a global line; this tool does not change it")
        original = menu.lines[at]

    plan = []
    if original is not None:
        plan.append(f"change `{original}` to `remember_last_entry: no` in {lk.conf}"
                    + (" and enroll its checksum" if lk.backend.enroll_enabled else "")
                    + f": every power-on then boots Limine's default entry, {owner.package}")
    oneshot = target is not owner
    pending = lk.oneshot
    if oneshot:
        plan.append(f"ask Limine for one boot of {package} (`bootctl set-oneshot {menu.bli_id(target.entry)}`)")
    elif pending:
        plan.append(f"cancel the one boot of {lk.name(menu.by_bli_id_or_path(pending), kernels)} asked for "
                    "(`bootctl set-oneshot ''`)")
    out("This will:")
    for line in plan or [f"nothing: the next boot is already {package}"]:
        out(f"  - {line}")
    if not oneshot:
        out(f"{package} is Limine's default entry: with remember_last_entry off, every power-on boots it.")
    if owner is not running and oneshot:
        out(f"note: a failed boot falls back to {owner.package} (Limine's default entry), not to "
            f"{running.package if running else 'the kernel running now'}.")
    if dry_run:
        return 0
    if plan and not confirm("Go ahead?", assume_yes):
        out("Nothing changed.")
        return 1
    if original is not None:
        new = lm.set_global(lk.data.decode("utf-8"), "remember_last_entry", "remember_last_entry: no",
                            marker=lm.marker_for(original))
        lk.write(new.encode("utf-8"), out)
    if oneshot:
        lk.set_oneshot(menu.bli_id(target.entry))
    elif pending:
        lk.set_oneshot("")
    nxt, _ = lk.menu.boots(lk.oneshot, lk.last_booted)
    if lk.owner(nxt, kernels) is not target:
        raise KernelError(f"the next boot would be {lk.name(nxt, kernels)}, not {package}; nothing else changed "
                          "(`sudo oxp3-tool kernel` shows the state)")
    out(f"Ready. Reboot when you like (`systemctl reboot`): Limine boots {package} once.")
    if oneshot:
        out(f"If it hangs or the screen stays dark, hold the power button for about 10 seconds until the device is "
            f"off, then turn it on: Limine boots {owner.package} after {menu.timeout_text()}. Every reboot or "
            f"power-on after that one boot goes back to {owner.package} too.")
        out(f"On {package}, `uname -r` shows {target.release}. To keep booting it: `sudo oxp3-tool kernel keep`.")
    return 0


def _keep(lk: LimineKernels, kernels: list[Kernel], assume_yes: bool, dry_run: bool, out: Out,
          confirm: Callable[[str, bool], bool]) -> int:
    menu = lk.menu
    original = lm.marked_original(menu)
    running = lk.running(kernels)
    pending = lk.oneshot
    if original is None and not pending:
        if menu.remembers():
            out("Nothing to change: Limine boots the entry that booted last (remember_last_entry: yes).")
        else:
            default = menu.default()
            out(f"Nothing to change: remember_last_entry is off in {lk.conf} (not by this tool), so Limine boots its "
                f"default entry, {lk.name(default, kernels)}. This tool does not change default_entry.")
        return 0
    if original is not None:
        problem = lk.edit_problem()
        if problem:
            raise KernelError(problem)
    plan = []
    if pending:
        plan.append(f"cancel the one boot of {lk.name(menu.by_bli_id_or_path(pending), kernels)} asked for "
                    "(`bootctl set-oneshot ''`)")
    after = menu
    if original is not None:
        plan.append(f"put `{original}` back in {lk.conf}" +
                    (" and enroll its checksum" if lk.backend.enroll_enabled else ""))
        after = lm.parse(lm.set_global(lk.data.decode("utf-8"), "remember_last_entry", original, marker=None))
    nxt, _ = after.boots(None, lk.last_booted)
    out("This will:")
    for line in plan:
        out(f"  - {line}")
    out(f"Then every power-on boots {lk.name(nxt, kernels, after)}.")
    if running is not None and lk.owner(nxt, kernels) is not running:
        out(f"note: that is not {running.package}, the kernel running now (Limine has not recorded booting it).")
    if dry_run:
        return 0
    if not confirm("Go ahead?", assume_yes):
        out("Nothing changed.")
        return 1
    if pending:
        lk.set_oneshot("")
    if original is not None:
        lk.write(lm.set_global(lk.data.decode("utf-8"), "remember_last_entry", original, marker=None).encode("utf-8"),
                 out)
    out("Done.")
    return 0


def status_lines(ctx: Context) -> list[str]:
    """For `oxp3-tool status`: a pending `kernel try`, Limine left booting its default entry, a self-built kernel."""
    try:
        lk = LimineKernels(ctx)
    except KernelError:
        return []
    kernels = lk.kernels()
    menu = lk.menu
    out = []
    pending = lk.oneshot
    if pending:
        once, _ = menu.boots(pending, None)
        after, _ = menu.boots(None, menu.path(once) if once is not None and menu.remembers() else None)
        out.append(f"Limine boots {lk.name(once, kernels)} once at the next boot (`oxp3-tool kernel try`), then "
                   f"{lk.name(after, kernels)}")
    if lm.marked_original(menu) is not None:
        out.append(f"Limine boots its default entry, {lk.name(menu.default(), kernels)}, at every power-on "
                   "(`oxp3-tool kernel try` turned remember_last_entry off; `sudo oxp3-tool kernel keep`, on the "
                   "kernel to keep, puts it back)")
    running = lk.running(kernels)
    if running is not None and running.package.endswith("-vsw"):
        out.append(f"{running.package} is a self-built kernel (tools/cachyos-vswing-kernel.sh): `pacman -Syu` does "
                   "not update it. Once the stock kernel has the white-dots fix (Linux 7.4), go back with `sudo "
                   "oxp3-tool kernel try <stock kernel>`, reboot, then `sudo pacman -R "
                   f"{running.package} {running.package}-headers`")
    return out
