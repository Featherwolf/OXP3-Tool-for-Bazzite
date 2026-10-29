"""`oxp3-tool kernel`: boot another installed kernel once, without a keyboard, on CachyOS with Limine.

CachyOS writes `remember_last_entry: yes` into limine.conf: Limine boots whatever booted last, so a kernel that
booted once (and then hung, or left the screen dark) boots again at every power-on, and only a key press in
Limine's menu gets back. `kernel try` makes the kernel running now the one every power-on boots
(`remember_last_entry: no`, and `default_entry` pointed at it when it is not the default already), then asks Limine
for one boot of the other kernel (LoaderEntryOneShot, through bootctl). `kernel keep` puts both lines back, so the
kernel running then is the one Limine keeps booting.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import signal
import time
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

from . import limine_menu as lm
from .detect import CACHYOS, Context
from .kargs import PACMAN_LOCK, LimineBackend

Out = Callable[[str], None]
BOOT_LOCK = "/run/lock/boot-partition.lock"  # limine-common-functions' BOOT_PARTITION_LOCK


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
        self._file_sums: dict[str, Optional[str]] = {}
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
        """Every file the entry loads is on the ESP, and matches the checksum limine.conf gives it (Limine stops
        with an error otherwise)."""
        if not entry.files or not ("path" in entry.options or "kernel_path" in entry.options):
            return False
        for value in entry.files:
            m = re.fullmatch(r"boot\(\):(/[^#]+)(?:#(.*))?", value)
            if not m:
                return False
            if m.group(1) not in self._file_sums:
                data = self.sys.read_bytes(self.esp + m.group(1))
                self._file_sums[m.group(1)] = None if data is None else lm.b2sum(data)
            digest = self._file_sums[m.group(1)]
            if digest is None or (m.group(2) is not None and m.group(2).lower() != digest):
                return False
        return True

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

    lock_timeout = 60.0

    @contextlib.contextmanager
    def boot_lock(self) -> Iterator[None]:
        """The lock CachyOS's Limine tools (limine-entry-tool, limine-mkinitcpio, limine-snapper-sync) hold while
        they read and rewrite limine.conf."""
        path = self.sys.path(BOOT_LOCK)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, 0o644)
        try:
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise KernelError("another tool (limine-mkinitcpio, limine-snapper-sync, ...) is changing "
                                          "the boot partition; try again in a minute") from None
                    time.sleep(0.5)
            yield
        finally:
            os.close(fd)

    def commit(self, new: Optional[bytes], oneshot: Optional[str], out: Out) -> None:
        """Replace limine.conf with ``new`` (enrolling its checksum where that is set up; any mismatch puts the old
        file back), then set the one-shot entry ("" clears it). Holds the boot-partition lock, and keeps Ctrl+C and
        a closed terminal from stopping it half way; re-reads limine.conf at the end."""
        notes: list[str] = []
        try:
            with self.boot_lock(), _shielded():
                if self.sys.exists(PACMAN_LOCK):
                    raise KernelError("pacman is running (a kernel update rewrites limine.conf); try again when it "
                                      "is done")
                if self.sys.read_bytes(self.conf) != self.data:
                    raise KernelError(f"{self.conf} changed while this ran; run the command again")
                if oneshot == "":  # keep: never remember_last_entry: yes on disk while a one-shot is pending
                    self.set_oneshot("")
                if new is not None and new != self.data:
                    self._replace(new, notes)
                if oneshot:  # try: only once remember_last_entry: no is in place
                    self.set_oneshot(oneshot)
                data = self.sys.read_bytes(self.conf) or b""
                self.data, self.menu = data, lm.parse(data.decode("utf-8"))
        finally:
            for note in notes:  # printed outside the shield: a closed terminal must not stop the work above
                out(note)

    def _replace(self, new: bytes, notes: list[str]) -> None:
        old = self.data
        had_checksum = bool(lm.enrolled_hashes(self.sys, self.esp))
        try:
            self.sys.write_atomic(self.conf, new, mode=None)
            problem = self._settle(new, enroll=True)
        except OSError as e:
            problem = f"writing it failed ({e.strerror or e})"
        if problem is None:
            return
        if self.sys.read_bytes(self.conf) == old:  # the write failed before its rename: nothing was replaced
            notes.append(f"error: {problem}")
        else:
            notes.append(f"error: {problem}; putting the previous {self.conf} back")
            try:
                self.sys.write_atomic(self.conf, old, mode=None)
            except OSError as e:
                raise KernelError(f"DO NOT REBOOT: {self.conf} could not be put back ({e.strerror or e})") from e
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

    def checksum_problem(self) -> Optional[str]:
        """Limine would halt at power-on: limine.conf does not match the checksum enrolled in its binary."""
        current = lm.b2sum(self.data)
        if any(h != current for h in lm.enrolled_hashes(self.sys, self.esp).values()):
            return ("DO NOT REBOOT: limine.conf does not match the checksum enrolled in Limine's EFI binary, so Limine "
                    "would stop at power-on; run `sudo limine-enroll-config` and check that it ends without errors")
        return None

    def set_oneshot(self, value: str) -> None:
        if not self.sys.which("bootctl"):
            raise KernelError("bootctl (systemd) is missing")
        res = self.sys.run(["bootctl", "set-oneshot", value], timeout=60)
        if not res.ok or (self.oneshot or "") != value:
            raise KernelError(f"`bootctl set-oneshot {value}` failed: {(res.stderr or res.stdout).strip()[-300:]}")


@contextlib.contextmanager
def _shielded() -> Iterator[None]:
    """Ctrl+C, a closed terminal and SIGTERM ignored, also by the commands run meanwhile (limine-enroll-config),
    so limine.conf and its enrolled checksum are never left disagreeing."""
    old = {}
    try:
        for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
            old[sig] = signal.signal(sig, signal.SIG_IGN)
    except ValueError:  # not the main thread
        pass
    try:
        yield
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


# -- the command ----------------------------------------------------------------------


def _notes(lk: LimineKernels, kernels: list[Kernel]) -> list[str]:
    """Warnings about the state `kernel try` left, or any state that would stop Limine at its menu."""
    menu = lk.menu
    notes = []
    problem = lk.checksum_problem()
    if problem:
        notes.append(problem)
    default = menu.default()
    if default is None:
        notes.append("warning: limine.conf's default_entry names no bootable entry, so Limine would wait in its menu "
                     "whenever it does not boot a one-shot or remembered entry")
    elif lm.marked_original(menu, "default_entry") is not None:
        name = lk.name(default, kernels)
        notes.append(f"Limine's default entry is {name} since `oxp3-tool kernel try` (a failed boot comes back to "
                     f"it). Do not remove {name} before `sudo oxp3-tool kernel keep` on the kernel to keep, or "
                     "Limine waits in its menu at power-on")
    if lm.marked_original(menu) is not None and menu.remembers():
        notes.append("warning: limine.conf has remember_last_entry: yes again under `oxp3-tool kernel try`'s marker, "
                     "so a failed boot no longer comes back to Limine's default entry; `sudo oxp3-tool kernel keep` "
                     "clears the marker")
    elif lm.marked_original(menu) is not None:
        notes.append(f"Limine boots its default entry, {lk.name(default, kernels)}, at every power-on "
                     "(`oxp3-tool kernel try` turned remember_last_entry off; `sudo oxp3-tool kernel keep`, on the "
                     "kernel to keep, puts it back)")
    return notes


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
            where = f"entry {menu.path(k.entry)}, its files are missing or changed on the ESP"
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
    for note in _notes(lk, kernels):
        out(note)
    if default is not None and lk.owner(default, kernels) is None:
        out(f"note: Limine's default entry, '{menu.path(default)}', is no installed kernel's entry.")


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


def _oneshot_name(lk: LimineKernels, pending: str, kernels: list[Kernel]) -> str:
    entry = lk.menu.by_bli_id_or_path(pending)
    return lk.name(entry, kernels) if entry is not None else f"'{pending}' (no such entry now; Limine would ignore it)"


def _global_line(lk: LimineKernels, key: str) -> str:
    menu = lk.menu
    at = menu.value_line(key)
    if at is None:
        raise KernelError(f"{lk.conf} has no global {key} line; this tool does not add one")
    return menu.lines[at]


def _try(lk: LimineKernels, kernels: list[Kernel], package: str, assume_yes: bool, dry_run: bool, out: Out,
         confirm: Callable[[str, bool], bool]) -> int:
    menu = lk.menu
    target = next((k for k in kernels if k.package == package), None)
    names = ", ".join(k.package for k in kernels)
    if target is None:
        raise KernelError(f"{package} is not an installed kernel package (installed: {names})")
    if target.entry is None or not lk.files_ok(target.entry):
        raise KernelError(f"{package} has no usable Limine entry in {lk.conf} (or its files on the ESP are missing "
                          "or changed); run `sudo limine-update`")
    running = lk.running(kernels)
    if target is running:
        raise KernelError(f"{package} is the kernel running now" +
                          ("; `sudo oxp3-tool kernel keep` keeps it" if lm.marked_original(menu) is not None else ""))
    if running is None or running.entry is None or not lk.files_ok(running.entry):
        nxt, why = menu.boots(lk.oneshot, lk.last_booted)
        what = ("is no longer installed (pacman updated or removed it)" if running is None else
                "has no usable Limine entry of its own")
        raise KernelError(f"the kernel running now ({lk.ctx.kernel_release or '?'}) {what}, so a failed boot could "
                          f"not come back to it. The next boot is {lk.name(nxt, kernels)}" +
                          (", and then every power-on, because it booted last and remember_last_entry is yes"
                           if why == "remembered" else ""))
    problem = lk.edit_problem()
    if problem:
        raise KernelError(problem)
    if not menu.autoboots():
        raise KernelError(f"{lk.conf} has `timeout: no`: after a failed boot Limine would wait in its menu for a key")

    # A failed boot must come back to the kernel running now: Limine's default entry, with remember_last_entry off.
    text = lk.data.decode("utf-8")
    plan = []
    if menu.remembers():
        line = _global_line(lk, "remember_last_entry")
        text = lm.set_global(text, "remember_last_entry", "remember_last_entry: no", marker=lm.marker_for(line))
        plan.append(f"change `{line}` to `remember_last_entry: no`: every power-on then boots Limine's default entry")
    if lk.owner(menu.default(), kernels) is not running:
        line = _global_line(lk, "default_entry")
        original = lm.marked_original(menu, "default_entry") or line
        if lk.owner(lm.parse(lm.set_global(text, "default_entry", original)).default(), kernels) is running:
            text = lm.set_global(text, "default_entry", original, marker=None)  # back to what it was
        else:
            text = lm.set_global(text, "default_entry", f"default_entry: {menu.path(running.entry)}",
                                 marker=lm.marker_for(original))
        plan.append(f"change `{line}` to `{lm.parse(text).lines[lm.parse(text).value_line('default_entry')]}`, so "
                    f"the default entry is {running.package}, the kernel running now")
    if plan:
        plan[-1] += f" (in {lk.conf}" + (", then enroll its checksum)" if lk.backend.enroll_enabled else ")")
    oneshot_id = menu.bli_id(target.entry)
    plan.append(f"ask Limine for one boot of {package} (`bootctl set-oneshot {oneshot_id}`)")
    out("This will:")
    for line in plan:
        out(f"  - {line}")
    if lm.marked_original(lm.parse(text), "default_entry") is not None:
        out(f"note: until `sudo oxp3-tool kernel keep`, do not remove {running.package}: Limine's default entry "
            "would then name nothing, and Limine would wait in its menu at power-on.")
    if dry_run:
        return 0
    if not confirm("Go ahead?", assume_yes):
        out("Nothing changed.")
        return 1
    lk.commit(text.encode("utf-8"), oneshot_id, out)
    nxt, _ = lk.menu.boots(lk.oneshot, lk.last_booted)
    if lk.owner(nxt, kernels) is not target or lk.menu.remembers() or \
            lk.owner(lk.menu.default(), kernels) is not running:
        lk.set_oneshot("")
        raise KernelError(f"limine.conf is not as expected after the change (another tool rewrote it?), so the one "
                          f"boot of {package} was cancelled; `sudo oxp3-tool kernel` shows the state")
    out(f"Ready. Reboot when you like (`systemctl reboot`): Limine boots {package} once.")
    out(f"If it hangs or the screen stays dark, hold the power button for about 10 seconds until the device is off, "
        f"then turn it on: Limine boots {running.package} after {lk.menu.timeout_text()}. Every reboot or power-on "
        f"after that one boot goes back to {running.package} too.")
    out(f"On {package}, `uname -r` shows {target.release}. To keep booting it: `sudo oxp3-tool kernel keep`.")
    return 0


def _keep(lk: LimineKernels, kernels: list[Kernel], assume_yes: bool, dry_run: bool, out: Out,
          confirm: Callable[[str, bool], bool]) -> int:
    menu = lk.menu
    originals = {key: lm.marked_original(menu, key) for key in ("remember_last_entry", "default_entry")}
    running = lk.running(kernels)
    pending = lk.oneshot
    if not any(originals.values()) and not pending:
        if menu.remembers():
            out("Nothing to change: Limine boots the entry that booted last (remember_last_entry: yes).")
        else:
            out(f"Nothing to change: remember_last_entry is off in {lk.conf} (not by this tool), so Limine boots its "
                f"default entry, {lk.name(menu.default(), kernels)}.")
        return 0
    text = lk.data.decode("utf-8")
    plan = []
    if pending:
        plan.append(f"cancel the one boot of {_oneshot_name(lk, pending, kernels)} asked for "
                    "(`bootctl set-oneshot ''`)")
    for key, original in originals.items():
        if original is not None:
            text = lm.set_global(text, key, original, marker=None)
            plan.append(f"put `{original}` back in {lk.conf}")
    if any(originals.values()) and lk.backend.enroll_enabled:
        plan[-1] += " and enroll its checksum"
    if any(originals.values()):
        problem = lk.edit_problem()
        if problem:
            raise KernelError(problem)
    after = lm.parse(text)
    nxt, _ = after.boots(None, lk.last_booted)
    out("This will:")
    for line in plan:
        out(f"  - {line}")
    if nxt is None:
        raise KernelError(f"after that Limine would boot nothing at power-on (its default entry names no bootable "
                          f"entry); nothing changed")
    out(f"Then every power-on boots {lk.name(nxt, kernels, after)}.")
    if running is not None and lk.owner(nxt, kernels) is not running:
        out(f"note: that is not {running.package}, the kernel running now (Limine has not recorded booting it).")
    if dry_run:
        return 0
    if not confirm("Go ahead?", assume_yes):
        out("Nothing changed.")
        return 1
    lk.commit(text.encode("utf-8") if any(originals.values()) else None, "" if pending else None, out)
    out("Done.")
    return 0


def status_lines(ctx: Context) -> list[str]:
    """For `oxp3-tool status`: a pending `kernel try`, what it changed in limine.conf, a self-built kernel."""
    try:
        lk = LimineKernels(ctx)
    except KernelError:
        return []
    kernels = lk.kernels()
    menu = lk.menu
    out = []
    pending = lk.oneshot
    if pending and menu.by_bli_id_or_path(pending) is None:
        out.append(f"a one-shot boot of '{pending}' is set but names no entry in limine.conf now; Limine ignores it")
    elif pending:
        once, _ = menu.boots(pending, None)
        after, _ = menu.boots(None, menu.path(once) if once is not None and menu.remembers() else None)
        out.append(f"Limine boots {lk.name(once, kernels)} once at the next boot (`oxp3-tool kernel try`), then "
                   f"{lk.name(after, kernels)}")
    out += _notes(lk, kernels)
    running = lk.running(kernels)
    if running is not None and running.package.endswith("-vsw"):
        stock = running.package[:-len("-vsw")]
        out.append(f"{running.package} is a self-built kernel (tools/cachyos-vswing-kernel.sh): `pacman -Syu` does "
                   f"not update it. Once {stock} has the white-dots fix (Linux 7.4): `sudo oxp3-tool kernel try "
                   f"{stock}`, reboot, `sudo oxp3-tool kernel keep` on it, then `sudo pacman -R {running.package} "
                   f"{running.package}-headers`")
    return out
