"""Kernel arguments of the next boot, and how this tool changes them, per OS and boot loader.

Bazzite changes them with ``rpm-ostree kargs`` in a staged deployment. CachyOS
keeps them in boot-loader configuration that its own generator turns into boot
entries, so there the tool writes one drop-in file of its own that the
generator reads, regenerates the entries and checks them:

- Limine (CachyOS's default): /etc/limine-entry-tool.d/90-oxp3-tool.conf, read
  by limine-entry-tool; ``limine-mkinitcpio`` rebuilds the initramfs and entry
  of every kernel. ``limine-entry-tool --get-cmdline`` shows the result before
  anything on the EFI system partition changes.
- systemd-boot through sdboot-manage: /etc/sdboot-manage.conf.d/90-oxp3-tool.conf,
  then ``sdboot-manage gen``.
- GRUB: /etc/default/grub.d/90-oxp3-tool.cfg, then ``grub-mkconfig``.

Everything else (rEFInd, unified kernel images, a hand-made setup, more than one
boot loader that could be in use) gets exact steps to do by hand instead of a
guess. Kernel updates regenerate the entries from the same configuration, so
the arguments stay.
"""

from __future__ import annotations

import contextlib
import re
import signal
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Iterator, Optional

from .templates import OWN_MARKER, PROJECT_URL

if TYPE_CHECKING:
    from .detect import Context

# Only arguments of this shape are ever written (module.param=value).
KARG_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+=[A-Za-z0-9_.,:-]+$")
PACMAN_LOCK = "/var/lib/pacman/db.lck"
# Present while a boot-entry rebuild runs and after one that did not finish; /run empties at boot.
REBUILD_MARKER = "/run/oxp3-tool-boot-entries-unfinished"
EFIVARS = "/sys/firmware/efi/efivars"
# Set by systemd-boot and GRUB (boot loader interface); Limine's value is not confirmed.
LOADER_INFO = f"{EFIVARS}/LoaderInfo-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f"
SECURE_BOOT = f"{EFIVARS}/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"
ESP_CANDIDATES = ("/efi", "/boot", "/boot/efi", "/limine")  # limine-entry-tool's search order
# Followed by 128 hex digits in Limine's EFI binary: all zeros unless `limine enroll-config` enrolled a checksum.
LIMINE_B2SUM_SIGNATURE = b"++CONFIG_B2SUM_SIGNATURE++"

HARDWARE_SETUP_UNIT = "bazzite-hardware-setup.service"
KARGS_UPDATE_WARNING = (
    "Reboot before installing a system update: an update installed first is built from the "
    "current kernel arguments and drops this change."
)
DROPIN_HEADER = (
    f"# {OWN_MARKER}{PROJECT_URL}).\n"
    "# Kernel arguments for oxp3-tool's fixes. Do not edit: `sudo oxp3-tool apply` rewrites this file\n"
    "# and `sudo oxp3-tool revert` removes it.\n"
)

Printer = Callable[[str], None]


class KargError(Exception):
    pass


@dataclass
class KargResult:
    ok: bool = True
    error: str = ""
    changed: bool = False


def tokens(cmdline: str) -> list[str]:
    """Command-line words in order, without repeats, the way the boot-entry tools write them."""
    out: list[str] = []
    for tok in re.findall(r'''(?:[^\s"']+|"[^"]*"|'[^']*')+''', cmdline):
        if tok.startswith("initrd=") or tok.startswith("BOOT_IMAGE="):
            continue
        if tok not in out:
            out.append(tok)
    return out


def karg_key(karg: str) -> str:
    return karg.split("=", 1)[0]


class KargBackend:
    """How the next boot's kernel arguments are read and changed."""

    name = ""
    how = ""  # in "add kernel argument X (<how>, reboot needed)"
    update_warning: Optional[str] = None
    unreadable = "could not read the next boot's kernel arguments"

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.sys = ctx.sys

    def refusal(self) -> Optional[str]:
        """Why this tool cannot change kernel arguments here (with steps to do it by hand), or None."""
        return None

    def short_refusal(self, karg: str) -> Optional[str]:
        """Why a fix that sets ``karg`` is not applicable here, with what to do instead."""
        return self.refusal()

    def next_boot(self) -> Optional[list[str]]:
        """Arguments every kernel entry of the next boot has (None: unknown, e.g. not root)."""
        return None

    def owned(self) -> list[str]:
        """Arguments in this tool's own configuration file (file-based boot loaders)."""
        return []

    def left_in_entries(self) -> Optional[list[str]]:
        """Arguments some next-boot entry has that the user's own configuration does not give it: what a
        removal may still have to remove (None: the same as next_boot())."""
        return None

    def booted_user_args(self) -> list[str]:
        """Arguments the user's own boot configuration gives the running kernel (not this tool's)."""
        return []

    def busy(self) -> Optional[str]:
        return None

    def notes(self) -> list[str]:
        return []

    def unfinished(self) -> Optional[str]:
        """The command to finish a boot-entry rebuild that did not finish since this boot, or None."""
        return None

    def change(self, add: list[str], remove: list[str], keep: list[str], out: Printer) -> KargResult:
        """Make the next boot have ``keep`` (every argument this tool owns afterwards); ``add`` and
        ``remove`` are what changes, for tools that take differences."""
        raise NotImplementedError

    def invalidate(self) -> None:
        pass

    def missing_text(self) -> str:
        """Why an argument this tool set can be missing from the next boot."""
        return ""

    def remove_hint(self, karg: str) -> str:
        return f"remove {karg} from the kernel command line"

    def recovery_hint(self) -> str:
        return ""

    def describe(self) -> str:
        """One status line about where kernel arguments live (empty for Bazzite)."""
        return ""

    def manual(self, args: list[str]) -> str:
        return (f"add {' '.join(args)} to the kernel command line in your boot loader's configuration "
                "and rebuild its boot entries")


class RpmOstreeBackend(KargBackend):
    name = "rpm-ostree"
    how = "rpm-ostree kargs"
    update_warning = KARGS_UPDATE_WARNING
    unreadable = "could not read the current kernel arguments with `rpm-ostree kargs`"

    def refusal(self) -> Optional[str]:
        if not self.ctx.has_rpm_ostree:
            return "rpm-ostree is not available, so kernel arguments cannot be changed"
        return None

    def short_refusal(self, karg: str) -> Optional[str]:
        return "rpm-ostree is not available" if not self.ctx.has_rpm_ostree else None

    def next_boot(self) -> Optional[list[str]]:
        if not self.ctx.has_rpm_ostree:
            return None
        res = self.sys.run(["rpm-ostree", "kargs"], timeout=120)
        if not res.ok:
            return None
        return res.stdout.split()

    def busy(self) -> Optional[str]:
        if self.ctx.rpm_ostree_busy:
            return "an rpm-ostree transaction is running (an update?); try again when it is done"
        if self.ctx.unit_state(HARDWARE_SETUP_UNIT) == "activating":
            return f"{HARDWARE_SETUP_UNIT} is still running; try again in a minute"
        return None

    def notes(self) -> list[str]:
        if self.ctx.has_staged_deployment:
            return ["A staged update is waiting for a reboot; the kernel arguments are changed in it."]
        return []

    def change(self, add: list[str], remove: list[str], keep: list[str], out: Printer) -> KargResult:
        result = KargResult()
        for flag, args in (("--append-if-missing", add), ("--delete-if-present", remove)):
            if not args:
                continue
            argv = ["rpm-ostree", "kargs"] + [f"{flag}={k}" for k in args]
            out(f"  running: {' '.join(argv)}")
            res = self.sys.run(argv, timeout=600)
            if not res.ok:
                return KargResult(False, f"rpm-ostree kargs failed ({res.returncode}): {res.stderr.strip()[:500]}",
                                  result.changed)
            result.changed = True
        return result

    def missing_text(self) -> str:
        return "a system update installed before rebooting can drop it"

    def remove_hint(self, karg: str) -> str:
        return f"`sudo rpm-ostree kargs --delete={karg}`"

    def recovery_hint(self) -> str:
        return ("run `ujust configure-grub show` so the boot menu can start the previous version if the screen "
                "stays dark (you need a keyboard there), or keep SSH ready")


class ManualBackend(KargBackend):
    """A boot setup this tool does not change: the user adds the arguments by hand."""

    name = "manual"
    how = "by hand"

    def __init__(self, ctx: Context, why: str, steps: str = "") -> None:
        super().__init__(ctx)
        self.why = why
        self.steps = steps

    def refusal(self) -> Optional[str]:
        return f"{self.why}; {self.steps or 'add the kernel arguments by hand'}"

    def short_refusal(self, karg: str) -> Optional[str]:
        return f"{self.why}; set {karg} by hand: {self.manual([karg])}"

    def next_boot(self) -> Optional[list[str]]:
        return None

    def change(self, add: list[str], remove: list[str], keep: list[str], out: Printer) -> KargResult:
        return KargResult(False, self.refusal() or "")

    def describe(self) -> str:
        return f"Boot:     {self.why}; kernel-argument fixes have to be done by hand"

    def manual(self, args: list[str]) -> str:
        if self.steps:
            return self.steps.replace("the arguments", " ".join(args) or "the arguments")
        return super().manual(args)


class DropinBackend(KargBackend):
    """A boot loader whose generator reads a drop-in directory this tool writes one file into."""

    dropin = ""
    regen: list[str] = []
    regen_timeout = 300
    regen_what = ""
    # Generated entries keep root= (or zfs=) from the configuration, so a change must not drop it.
    root_in_config = True
    kernel_keys = False  # whether the configuration has per-kernel command lines

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self._entries: Optional[dict[str, list[str]]] = None
        self._entries_read = False

    # -- per boot loader ---------------------------------------------------

    def render_lines(self, args: list[str], kernels: list[str]) -> list[str]:
        raise NotImplementedError

    def effective(self) -> Optional[dict[str, list[str]]]:
        """Kernel name -> the arguments the generator would write now, from the configuration."""
        raise NotImplementedError

    def read_entries(self) -> Optional[dict[str, list[str]]]:
        """Kernel name -> the arguments of its generated boot entry (fallback entries excluded)."""
        raise NotImplementedError

    def regen_failed(self, code: int) -> tuple[bool, str]:
        """(restore the previous drop-in, error text) for a failed regeneration."""
        return True, f"`{' '.join(self.regen)}` failed ({code})"

    # -- shared ------------------------------------------------------------

    def render(self, args: list[str], kernels: list[str] = ()) -> Optional[bytes]:
        if not args:
            return None
        lines = self.render_lines(args, list(kernels))
        return (DROPIN_HEADER + "\n".join(lines) + "\n").encode()

    def owned(self) -> list[str]:
        text = self.sys.read_text(self.dropin) or ""
        if OWN_MARKER not in text:
            return []
        out: list[str] = []
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            for m in re.finditer(r'"([^"]*)"', line):
                for tok in re.sub(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?", " ", m.group(1)).split():
                    if KARG_RE.match(tok) and tok not in out:
                        out.append(tok)
        return out

    def entries(self) -> Optional[dict[str, list[str]]]:
        if not self._entries_read:
            self._entries = self.read_entries()
            self._entries_read = True
        return self._entries

    def next_boot(self) -> Optional[list[str]]:
        entries = self.entries()
        if not entries:
            return None
        lists = list(entries.values())
        return [t for t in lists[0] if all(t in other for other in lists[1:])]

    def user_args(self) -> Optional[dict[str, list[str]]]:
        """Entry name -> the arguments the user's own configuration (without this tool's drop-in) gives it."""
        return None

    def booted_user_args(self) -> list[str]:
        """Arguments the user's own configuration gives the kernel that is running."""
        return []

    def left_in_entries(self) -> Optional[list[str]]:
        entries = self.entries()
        if not entries:
            return None
        user = self.user_args() or {}
        return list(dict.fromkeys(t for name, args in entries.items() for t in args if t not in user.get(name, [])))

    def invalidate(self) -> None:
        self._entries = None
        self._entries_read = False

    def busy(self) -> Optional[str]:
        if self.sys.exists(PACMAN_LOCK):
            return (f"{PACMAN_LOCK} exists: a pacman update is running, or one was interrupted (for example by a "
                    "flat battery). If it is running, try again when it is done. If no package manager is running, "
                    f"remove {PACMAN_LOCK}, finish the update with `sudo pacman -Syu`, and then try again")
        return None

    @property
    def finish_command(self) -> str:
        """What the user runs to finish a rebuild this tool could not finish."""
        return " ".join(self.regen)

    def notes(self) -> list[str]:
        if self.ctx.secure_boot:
            return ["Secure Boot is on: if rebuilding the boot entries reports an error, do not reboot until "
                    f"`sudo {self.finish_command}` succeeds."]
        return []

    def unfinished(self) -> Optional[str]:
        text = self.sys.read_text(REBUILD_MARKER)
        if text is None:
            return None
        return text.strip() or self.finish_command

    def _mark(self, command: Optional[str]) -> None:
        if command is None:
            self.sys.remove(REBUILD_MARKER)
        else:
            self.sys.write_atomic(REBUILD_MARKER, (command + "\n").encode(), mode=0o644)

    def base_problem(self, args: list[str]) -> Optional[str]:
        """Why the configuration without this tool's drop-in cannot take one (checked before writing)."""
        return None

    def missing_text(self) -> str:
        return "the boot entries were rebuilt without it"

    def _write(self, content: Optional[bytes]) -> None:
        if content is None:
            self.sys.remove(self.dropin)
        else:
            self.sys.write_atomic(self.dropin, content, mode=0o644)

    def _check(self, before: dict[str, list[str]], after: Optional[dict[str, list[str]]],
               ours: set[str], keep: list[str]) -> list[str]:
        """Problems with ``after``: kernels that miss an argument, or anything else that changed."""
        if after is None:
            return ["the boot loader's tool could not show the resulting command line"]
        problems = []
        for kernel, was in before.items():
            now = after.get(kernel)
            if now is None:
                problems.append(f"{kernel}: no command line")
                continue
            missing = [k for k in keep if k not in now]
            if missing:
                problems.append(f"{kernel}: {' '.join(missing)} would not be in its command line")
            others_before = [t for t in was if t not in ours]
            others_after = [t for t in now if t not in ours]
            if sorted(others_before) != sorted(others_after):
                problems.append(f"{kernel}: the rest of its command line would change "
                                f"({' '.join(others_before) or '(empty)'} -> {' '.join(others_after) or '(empty)'})")
            if self.root_in_config and any(t.startswith(("root=", "zfs=")) for t in was) \
                    and not any(t.startswith(("root=", "zfs=")) for t in now):
                problems.append(f"{kernel}: root= would be lost")
            for k in keep:
                clash = [t for t in now if t != k and t.split("=", 1)[0] == karg_key(k)]
                if clash:
                    problems.append(f"{kernel}: {' '.join(clash)} is set elsewhere and conflicts with {k}")
        return problems

    def change(self, add: list[str], remove: list[str], keep: list[str], out: Printer) -> KargResult:
        bad = [k for k in keep if not KARG_RE.match(k)]
        if bad:
            return KargResult(False, f"refusing unexpected kernel arguments: {' '.join(bad)}")
        prev = self.sys.read_bytes(self.dropin)
        if prev is not None and OWN_MARKER not in prev[:512].decode("utf-8", "replace"):
            return KargResult(False, f"refusing to replace {self.dropin}, which oxp3-tool did not write")
        busy = self.busy()
        if busy:
            return KargResult(False, busy)
        problem = self.base_problem(keep) if keep else None
        if problem:
            return KargResult(False, problem)
        ours = set(self.owned()) | set(keep) | set(remove)
        content = self.render(keep)
        # Checked even when the drop-in already has this content: a killed run may have left it unchecked.
        try:
            with _signals_interrupt():
                before, after, problems = self._probe(prev, content, ours, keep)
        except BaseException:
            self._restore(prev)
            raise
        if before is None:
            self._restore(prev)
            return KargResult(False, f"could not read the current kernel command line; {self.manual(keep)}")
        if problems:
            # Arguments being added now never stay behind unchecked, also when a killed run wrote them.
            self._restore(_without(prev, add))
            what = self.manual(keep) if keep else self.manual_remove()
            return KargResult(False, f"{self.name}'s configuration does not take the change as expected "
                                     f"({'; '.join(problems)}); nothing was changed. {what}")
        effective = after or before
        if self.sys.read_bytes(self.dropin) == prev and self.unfinished() is None \
                and self._entries_match(keep, remove, effective):
            return KargResult(True, "", False)
        busy = self.busy()
        if busy:
            self._restore(prev)
            return KargResult(False, busy)
        out(f"  running: {' '.join(self.regen)} ({self.regen_what})")
        self._mark(self.unfinished() or " ".join(self.regen))  # stays if this run is killed
        res = self.sys.run(self.regen, timeout=self.regen_timeout)
        self.invalidate()
        if not res.ok:
            restore, error = self.regen_failed(res.returncode)
            detail = (res.stderr or res.stdout).strip()[-400:]
            finished = False
            if restore:
                self._restore(prev)
                again = self.sys.run(self.regen, timeout=self.regen_timeout)
                self.invalidate()
                finished = again.ok
                if not again.ok:
                    error += (f"; restoring the previous configuration and rebuilding again failed too: do not "
                              f"reboot until `sudo {' '.join(self.regen)}` succeeds")
            self._mark(None if finished else (" ".join(self.regen) if restore else self.finish_command))
            return KargResult(False, f"{error}: {detail}" if detail else error, not restore)
        self._mark(None)
        effective = self.effective() or effective
        if not self._entries_match(keep, remove, effective):
            entries = self.entries()
            if entries is None:
                return KargResult(False, f"`{' '.join(self.regen)}` finished, but the boot entries could not be "
                                         "read back to check them", True)
            gone = [r for r in remove if not any(r in v for v in effective.values())]
            bad = [name for name, args in entries.items()
                   if not all(k in args for k in keep) or any(r in args for r in gone)]
            return KargResult(False, f"`{' '.join(self.regen)}` finished, but these boot entries lack the change: "
                                     f"{', '.join(bad) or 'unknown'}; run `sudo {' '.join(self.regen)}` again and "
                                     "check its output", True)
        return KargResult(True, "", True)

    def _probe(self, prev: Optional[bytes], content: Optional[bytes], ours: set[str], keep: list[str]
               ) -> tuple[Optional[dict[str, list[str]]], Optional[dict[str, list[str]]], list[str]]:
        """(command lines without this tool's drop-in, with ``content``, what is wrong with the latter).

        The baseline is the user's own configuration, so a drop-in that has taken over a command line
        (root= included) cannot pass as the way things were. Leaves ``content`` (or a per-kernel retry) written.
        """
        if prev is not None:
            self._write(None)
        before = self.effective()
        if not before:
            return None, None, []
        self._write(content)
        after = self.effective()
        problems = self._check(before, after, ours, keep)
        misses = [k for k, v in (after or {}).items() if not all(a in v for a in keep)]
        retry = self.own_key_kernels(misses) if problems and self.kernel_keys else []
        if retry:
            # A kernel with its own command-line key ignores the default one: name it too.
            self._write(self.render(keep, retry))
            again = self.effective()
            if not self._check(before, again, ours, keep):
                return before, again, []
        return before, after, problems

    def own_key_kernels(self, kernels: list[str]) -> list[str]:
        """Those of ``kernels`` whose command line is set by a key of their own elsewhere."""
        return []

    def _restore(self, prev: Optional[bytes]) -> None:
        if prev is None:
            self.sys.remove(self.dropin)
        else:
            self.sys.write_atomic(self.dropin, prev, mode=0o644)

    def manual_remove(self) -> str:
        return (f"To undo it by hand, delete {self.dropin}, then run `sudo {' '.join(self.regen)}` and check that "
                "it ends without errors.")

    def _entries_match(self, keep: list[str], remove: list[str], effective: dict[str, list[str]]) -> bool:
        entries = self.entries()
        if not entries:
            return False
        wanted_gone = [r for r in remove if not any(r in v for v in effective.values())]
        return all(all(k in args for k in keep) and not any(r in args for r in wanted_gone)
                   for args in entries.values())

    def describe(self) -> str:
        return f"Boot:     {self.name} ({self.dropin}, then `{' '.join(self.regen)}`)"

    def manual(self, args: list[str]) -> str:
        return (f"To set it by hand, add {' '.join(args) or 'the arguments'} to the kernel command line in "
                f"{self.manual_where}, then run `sudo {' '.join(self.regen)}` and reboot.")

    manual_where = ""


@contextlib.contextmanager
def _signals_interrupt() -> Iterator[None]:
    """Turn SIGHUP and SIGTERM into KeyboardInterrupt (a closed terminal or SSH session), so cleanup runs."""
    def interrupt(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    old = {}
    try:
        for sig in (signal.SIGHUP, signal.SIGTERM):
            old[sig] = signal.signal(sig, interrupt)
    except ValueError:  # not the main thread
        pass
    try:
        yield
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


def _without(content: Optional[bytes], args: list[str]) -> Optional[bytes]:
    """A drop-in without ``args``: lines left with no argument go, and an empty drop-in is no drop-in."""
    if content is None or not args:
        return content
    lines, left = [], False
    for line in content.decode("utf-8", "replace").splitlines():
        if line.lstrip().startswith("#") or '"' not in line:
            lines.append(line)
            continue
        head, _, rest = line.partition('"')
        value, _, tail = rest.rpartition('"')
        kept = [t for t in value.split() if t not in args]
        if any(KARG_RE.match(t) for t in kept):
            lead = value[:len(value) - len(value.lstrip())]  # sdboot-manage's += " ..." needs its space
            lines.append(f'{head}"{lead}{" ".join(kept)}"{tail}')
            left = True
    return ("\n".join(lines) + "\n").encode() if left else None


def _strip_quotes(value: str) -> str:
    value = value.strip()
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] == '"' else value


class LimineBackend(DropinBackend):
    name = "Limine"
    dropin = "/etc/limine-entry-tool.d/90-oxp3-tool.conf"
    regen = ["limine-mkinitcpio"]
    regen_timeout = 900
    regen_what = "rebuilds every kernel's initramfs and Limine boot entry; this takes a minute or two"
    how = "Limine drop-in, then limine-mkinitcpio"
    kernel_keys = True

    def render_lines(self, args: list[str], kernels: list[str]) -> list[str]:
        joined = " ".join(args)
        return [f'KERNEL_CMDLINE[default]+="{joined}"'] + [f'KERNEL_CMDLINE[{k}]+="{joined}"' for k in kernels]

    def config_files(self) -> list[str]:
        """limine-entry-tool's configuration files, in the order it reads them (later ones win)."""
        return self.sys.glob("/usr/share/limine-entry-tool.d/*.conf") + ["/etc/limine-entry-tool.conf"] + \
            self.sys.glob("/etc/limine-entry-tool.d/*.conf") + ["/etc/default/limine"]

    def config_value(self, key: str) -> Optional[str]:
        """The last plain ``KEY=value`` of limine-entry-tool's configuration files."""
        value = None
        for path in self.config_files():
            for line in (self.sys.read_text(path) or "").splitlines():
                line = line.strip()
                if line.startswith(f"{key}="):
                    value = _strip_quotes(line.split("=", 1)[1])
        return value

    @staticmethod
    def cmdline_lines(text: str) -> Iterator[tuple[str, bool, str]]:
        """(key, appends, value) of each KERNEL_CMDLINE line, the way limine-entry-tool's ConfigReader reads it."""
        for line in text.splitlines():
            m = re.match(r"\s*KERNEL_CMDLINE(?:\[([^\]]*)\])?\s*(\+?)=(.*)$", line)
            if m and not line.lstrip().startswith("#"):
                yield _strip_quotes(m.group(1) or "") or "default", bool(m.group(2)), _strip_quotes(m.group(3))

    def cmdlines(self) -> dict[str, str]:
        """KERNEL_CMDLINE[key] values the way limine-entry-tool reads them, leaving out this tool's drop-in."""
        out: dict[str, str] = {}
        for path in self.config_files():
            if path == self.dropin:
                continue
            for key, append, value in self.cmdline_lines(self.sys.read_text(path) or ""):
                out[key] = f"{value} {out[key]}" if append and key in out else value
        return out

    def default_file(self) -> str:
        """The configuration file with the user's KERNEL_CMDLINE[default] line (the last one that has it)."""
        found = "/etc/default/limine"
        for path in self.config_files():
            if path != self.dropin and any(k == "default" for k, _, _ in self.cmdline_lines(
                    self.sys.read_text(path) or "")):
                found = path
        return found

    def dropin_problem(self) -> Optional[str]:
        """Which command lines this tool's drop-in has become all of: the next rebuild loses root= there."""
        text = self.sys.read_text(self.dropin)
        if not text or OWN_MARKER not in text:
            return None
        keys = self.cmdlines()
        whole = [k for k, _, _ in self.cmdline_lines(text) if not keys.get(k, "").strip()]
        if not whole:
            return None
        what = "every kernel" if "default" in whole else ", ".join(dict.fromkeys(whole))
        return (f"no other file sets {' or '.join(f'KERNEL_CMDLINE[{k}]' for k in dict.fromkeys(whole))}, so "
                f"{self.dropin} is now the whole command line of {what}: the next limine-mkinitcpio run or kernel "
                "update writes boot entries without root=")

    def base_problem(self, args: list[str]) -> Optional[str]:
        if not self.cmdlines().get("default", "").strip():
            return f"a drop-in would replace Limine's whole command line here, root= included. {self.manual(args)}"
        return None

    def own_key_kernels(self, kernels: list[str]) -> list[str]:
        keys = self.cmdlines()
        return [k for k in kernels if keys.get(k, "").strip()]

    def _user_cmdline(self, name: str, keys: dict[str, str]) -> list[str]:
        value = keys.get(name, "")
        if not value.strip() and "fallback" in name:
            value = keys.get("fallback", "")
        if not value.strip():
            value = keys.get("default", "")
        if value.strip():
            return tokens(value)
        etc = (self.sys.read_text("/etc/kernel/cmdline") or "").replace("\n", " ").strip()
        return tokens(etc) if etc else list(self.ctx.active_kargs)

    def user_args(self) -> Optional[dict[str, list[str]]]:
        entries = self.entries()
        if not entries:
            return None
        keys = self.cmdlines()
        return {name: self._user_cmdline(name, keys) for name in entries}

    def booted_user_args(self) -> list[str]:
        release = self.ctx.kernel_release or ""
        if not re.fullmatch(r"[A-Za-z0-9._+-]+", release):
            return []
        res = self.sys.run(["pacman", "-Qqo", f"/usr/lib/modules/{release}/modules.builtin"], timeout=60)
        name = res.stdout.strip() if res.ok else ""
        return self._user_cmdline(name, self.cmdlines()) if re.fullmatch(r"[A-Za-z0-9@._+-]+", name) else []

    @property
    def esp(self) -> Optional[str]:
        configured = self.config_value("ESP_PATH")
        return configured or self.ctx.vfat_mount(ESP_CANDIDATES)

    def kernels(self) -> list[str]:
        builtin = self.sys.glob("/usr/lib/modules/*/modules.builtin")
        if not builtin:
            return []
        res = self.sys.run(["pacman", "-Qqo", *builtin], timeout=60)
        names = []
        for line in res.stdout.splitlines():
            name = line.strip()
            if re.fullmatch(r"[A-Za-z0-9@._+-]+", name) and name not in names:
                names.append(name)
        return names

    def refusal(self) -> Optional[str]:
        for tool in ("limine-entry-tool", "limine-mkinitcpio"):
            if not self.sys.which(tool):
                return (f"Limine without {tool} (CachyOS installs it with limine-mkinitcpio-hook) is set up in a "
                        f"way this tool does not change; {self.manual([])}")
        if (self.config_value("ENABLE_UKI") or "").lower() == "yes":
            return ("Limine boots unified kernel images here (ENABLE_UKI=yes), which this tool does not change; "
                    + self.manual([]))
        return None

    def short_refusal(self, karg: str) -> Optional[str]:
        refusal = self.refusal()
        if refusal:
            return refusal.replace(self.manual([]), self.manual([karg]))
        return self.base_problem([karg])

    def effective(self) -> Optional[dict[str, list[str]]]:
        out = {}
        for kernel in self.kernels():
            res = self.sys.run(["limine-entry-tool", "--get-cmdline", kernel], timeout=60)
            lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
            if not res.ok or not lines:
                return None
            out[kernel] = tokens(lines[-1])
        return out or None

    def read_entries(self) -> Optional[dict[str, list[str]]]:
        esp = self.esp
        machine_id = (self.sys.read_attr("/etc/machine-id") or "").strip()
        if not esp or not re.fullmatch(r"[0-9a-f]{32}", machine_id):
            return None
        text = self.sys.read_text(f"{esp}/limine.conf")
        if text is None:
            return None
        return parse_limine_conf(text, machine_id) or None

    @property
    def finish_command(self) -> str:
        return "limine-update"

    def regen_failed(self, code: int) -> tuple[bool, str]:
        if code == 3:
            # A post hook (enrolling the config checksum, signing) failed after the entries were written.
            return False, ("Do not reboot yet: limine-mkinitcpio could not finish (exit 3). Run "
                           "`sudo limine-update` and check that it ends without errors")
        return True, f"limine-mkinitcpio failed ({code}); the previous configuration was put back"

    @property
    def editor_off(self) -> bool:
        """Limine's entry editor is off after the next rebuild: a config checksum is enrolled in its EFI binary,
        limine-mkinitcpio will enroll one, or limine.conf turns the editor off.

        Secure Boot alone does not: Limine treats Secure Boot as inactive without an enrolled checksum.
        """
        enroll = ""
        for line in (self.sys.read_text("/etc/default/limine") or "").splitlines():
            if line.strip().startswith("ENABLE_ENROLL_LIMINE_CONFIG="):  # read from this file only, as it is
                enroll = _strip_quotes(line.split("=", 1)[1]).lower()
        if enroll == "yes":
            return True
        esp = self.esp
        for path in (f"{esp}/EFI/limine/limine_x64.efi", f"{esp}/EFI/BOOT/BOOTX64.EFI") if esp else ():
            data = self.sys.read_bytes(path)
            at = data.find(LIMINE_B2SUM_SIGNATURE) if data else -1
            if at >= 0 and data[at + len(LIMINE_B2SUM_SIGNATURE):at + len(LIMINE_B2SUM_SIGNATURE) + 128].strip(b"0"):
                return True
        conf = (self.sys.read_text(f"{esp}/limine.conf") if esp else None) or ""
        return re.search(r"(?im)^\s*editor_enabled\s*:\s*no\b", conf) is not None

    def recovery_hint(self) -> str:
        if self.editor_off:
            return ("Limine's entry editor is off here (its config checksum is enrolled, or limine.conf turns it "
                    "off), so have SSH or a CachyOS USB stick ready")
        return ("the Limine menu shows at each boot (timeout in limine.conf); with a USB keyboard, pick the entry, "
                "press E and delete the argument for one boot, or keep SSH ready")

    def remove_hint(self, karg: str) -> str:
        return (f"remove {karg} from the KERNEL_CMDLINE lines in {self.default_file()} and "
                "/etc/limine-entry-tool.d/, then run `sudo limine-mkinitcpio`")

    def manual(self, args: list[str]) -> str:
        what = " ".join(args) or "the arguments"
        own = self.own_key_kernels(self.kernels())
        also = (f" Also add {what} inside the quotes of {' and '.join(f'KERNEL_CMDLINE[{k}]' for k in own)}: a "
                "kernel with a command line of its own ignores KERNEL_CMDLINE[default].") if own else ""
        if self.cmdlines().get("default", "").strip():
            return (f"To set it by hand, add {what} to the kernel command line in {self.default_file()} (inside the "
                    f"quotes of KERNEL_CMDLINE[default]), then run `sudo limine-mkinitcpio` and reboot.{also}")
        first = ""
        if self.sys.exists(self.dropin):
            first = (f"First delete {self.dropin} (reverting this tool's kernel-argument fixes does it): no other "
                     "file sets KERNEL_CMDLINE[default], so it is now Limine's whole command line, and the next "
                     "limine-mkinitcpio run or kernel update writes boot entries without root=. ")
        return (f"{first}This system has no other KERNEL_CMDLINE[default] line, so Limine takes its command line "
                "from /etc/kernel/cmdline (or /proc/cmdline when that file is missing or empty). To set it by hand, "
                f"add {what} to /etc/kernel/cmdline (if it does not exist, create it with the words of /proc/cmdline "
                f"except BOOT_IMAGE= and initrd=, plus {what}), then run `sudo limine-mkinitcpio` and reboot. Do not "
                "add a KERNEL_CMDLINE[default] line holding only the new argument: it replaces the whole command "
                f"line, root= included.{also}")


def parse_limine_conf(text: str, machine_id: str) -> dict[str, list[str]]:
    """Kernel name -> command line of the entries limine-entry-tool writes (path boot():/<machine-id>/<kernel>/vmlinuz).

    Fallback entries (named <kernel>-fallback) and snapshot entries never match their own name in the path.
    """
    blocks: list[tuple[str, dict[str, str]]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("/"):
            blocks.append((line.lstrip("/").lstrip("+").strip(), {}))
        elif blocks and ":" in line and not line.startswith("#"):
            key, _, value = line.partition(":")
            blocks[-1][1].setdefault(key.strip().lower(), value.strip())
    out: dict[str, list[str]] = {}
    for name, opts in blocks:
        path = opts.get("path") or opts.get("kernel_path") or ""
        if re.fullmatch(rf"boot\(\):/{re.escape(machine_id)}/{re.escape(name)}/vmlinuz(#[0-9a-f]*)?", path):
            out[name] = tokens(opts.get("cmdline", opts.get("kernel_cmdline", "")))
    return out


# $1: a drop-in to leave out ("" for none).
SDBOOT_EVAL = (
    '. /etc/sdboot-manage.conf; '
    'for f in /usr/lib/sdboot-manage.conf.d/*.conf /etc/sdboot-manage.conf.d/*.conf; do '
    '[ "$f" = "$1" ] && continue; [ -f "$f" ] && . "$f"; done; '
    'printf "%s\\n%s\\n" "$LINUX_OPTIONS" "$NO_AUTOGEN"'
)


class SdbootBackend(DropinBackend):
    name = "systemd-boot"
    dropin = "/etc/sdboot-manage.conf.d/90-oxp3-tool.conf"
    regen = ["sdboot-manage", "gen"]
    regen_timeout = 120
    regen_what = "rewrites the systemd-boot entries"
    how = "sdboot-manage drop-in, then sdboot-manage gen"
    manual_where = "LINUX_OPTIONS in /etc/sdboot-manage.conf"
    root_in_config = False  # sdboot-manage adds root= itself

    def render_lines(self, args: list[str], kernels: list[str]) -> list[str]:
        return [f'LINUX_OPTIONS+=" {" ".join(args)}"']

    def _eval(self, skip: str = "") -> Optional[tuple[str, str]]:
        res = self.sys.run(["bash", "--noprofile", "--norc", "-c", SDBOOT_EVAL, "bash", skip], timeout=60)
        if not res.ok:
            return None
        lines = res.stdout.split("\n")
        return (lines[0], lines[1] if len(lines) > 1 else "")

    def refusal(self) -> Optional[str]:
        script = self.sys.read_text("/usr/bin/sdboot-manage") or ""
        if "/etc/sdboot-manage.conf.d/" not in script:
            return f"this sdboot-manage does not read /etc/sdboot-manage.conf.d; {self.manual([])}"
        return None

    def short_refusal(self, karg: str) -> Optional[str]:
        refusal = self.refusal()
        return refusal.replace(self.manual([]), self.manual([karg])) if refusal else None

    def busy(self) -> Optional[str]:
        busy = super().busy()
        if busy:
            return busy
        values = self._eval()
        if values and values[1].strip() == "yes":
            return ("sdboot-manage is set to NO_AUTOGEN=yes, so its boot entries are kept by hand; "
                    + self.manual([]))
        return None

    def effective(self) -> Optional[dict[str, list[str]]]:
        values = self._eval()
        entries = self.entries()
        if values is None or not entries:
            return None
        return {name: tokens(values[0]) for name in entries}

    def booted_user_args(self) -> list[str]:
        values = self._eval(self.dropin)
        return tokens(values[0]) if values else []

    def user_args(self) -> Optional[dict[str, list[str]]]:
        entries = self.entries()
        if not entries:
            return None
        mine = self.booted_user_args()
        return {name: mine for name in entries}

    def read_entries(self) -> Optional[dict[str, list[str]]]:
        res = self.sys.run(["bootctl", "--print-esp-path"], timeout=30)
        esp = res.stdout.strip() if res.ok else ""
        if not esp.startswith("/"):
            return None
        out = {}
        for path in sorted(self.sys.glob(f"{esp}/loader/entries/*.conf")):
            name = path.rsplit("/", 1)[1][:-len(".conf")]
            if name.endswith("-fallback"):
                continue
            text = self.sys.read_text(path)
            if text is None:
                return None
            fields = {}
            for line in text.splitlines():
                key, _, value = line.strip().partition(" ")
                fields.setdefault(key, value.strip())
            if not fields.get("linux", "").startswith("/vmlinuz-"):
                continue  # an entry sdboot-manage did not write
            out[name] = tokens(fields.get("options", ""))
        return out or None

    def regen_failed(self, code: int) -> tuple[bool, str]:
        return True, f"sdboot-manage gen failed ({code}); the previous configuration was put back"

    def recovery_hint(self) -> str:
        return ("reboot with `sudo systemctl reboot --boot-loader-menu=30`; in the menu, with a USB keyboard, press "
                "e on the entry and delete the argument for one boot, or keep SSH ready")

    def remove_hint(self, karg: str) -> str:
        return f"remove {karg} from LINUX_OPTIONS in /etc/sdboot-manage.conf, then run `sudo sdboot-manage gen`"


# $1: a drop-in to leave out ("" for none).
GRUB_EVAL = (
    '. /etc/default/grub; '
    'for f in /etc/default/grub.d/*.cfg; do [ "$f" = "$1" ] && continue; [ -f "$f" ] && . "$f"; done; '
    'printf "%s %s\\n" "$GRUB_CMDLINE_LINUX" "$GRUB_CMDLINE_LINUX_DEFAULT"'
)


class GrubBackend(DropinBackend):
    name = "GRUB"
    dropin = "/etc/default/grub.d/90-oxp3-tool.cfg"
    regen = ["grub-mkconfig", "-o", "/boot/grub/grub.cfg"]
    regen_what = "rewrites the GRUB menu"
    how = "GRUB drop-in, then grub-mkconfig"
    manual_where = "GRUB_CMDLINE_LINUX_DEFAULT in /etc/default/grub"
    root_in_config = False  # grub-mkconfig adds root= itself

    def render_lines(self, args: list[str], kernels: list[str]) -> list[str]:
        return [f'GRUB_CMDLINE_LINUX_DEFAULT="${{GRUB_CMDLINE_LINUX_DEFAULT}} {" ".join(args)}"']

    def refusal(self) -> Optional[str]:
        script = self.sys.read_text("/usr/bin/grub-mkconfig") or ""
        if "default/grub.d/" not in script:
            return f"this grub-mkconfig does not read /etc/default/grub.d; {self.manual([])}"
        return None

    def short_refusal(self, karg: str) -> Optional[str]:
        refusal = self.refusal()
        return refusal.replace(self.manual([]), self.manual([karg])) if refusal else None

    def _eval(self, skip: str = "") -> Optional[list[str]]:
        res = self.sys.run(["sh", "-c", GRUB_EVAL, "sh", skip], timeout=60)
        return tokens(res.stdout.strip()) if res.ok else None

    def effective(self) -> Optional[dict[str, list[str]]]:
        args = self._eval()
        entries = self.entries()
        if args is None or not entries:
            return None
        return {name: args for name in entries}

    def booted_user_args(self) -> list[str]:
        return self._eval(self.dropin) or []

    def user_args(self) -> Optional[dict[str, list[str]]]:
        entries = self.entries()
        if not entries:
            return None
        mine = self.booted_user_args()
        return {name: mine for name in entries}

    def read_entries(self) -> Optional[dict[str, list[str]]]:
        text = self.sys.read_text("/boot/grub/grub.cfg")
        if text is None:
            return None
        out: dict[str, list[str]] = {}
        inside = False
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("### BEGIN /etc/grub.d/10_linux"):
                inside = True
            elif s.startswith("### END /etc/grub.d/10_linux"):
                inside = False
            elif inside and s.startswith("linux"):
                parts = s.split()
                if len(parts) < 2:
                    continue
                args = tokens(" ".join(parts[2:]))
                if "single" in args:
                    continue  # recovery entries
                out.setdefault(parts[1], args)
        return out or None

    def recovery_hint(self) -> str:
        return ("with a USB keyboard, press e on the entry in the GRUB menu and delete the argument for one boot, "
                "or keep SSH ready")

    def remove_hint(self, karg: str) -> str:
        return f"remove {karg} from /etc/default/grub, then run `sudo grub-mkconfig -o /boot/grub/grub.cfg`"


REFIND_STEPS = ("add the arguments inside the quotes after \"Boot with standard options\" on the first line of "
                "/boot/refind_linux.conf (keep root=...); rEFInd reads it at the next boot")


def loader_info(ctx: Context) -> Optional[str]:
    """The boot loader named in the LoaderInfo EFI variable (boot loader interface), lower-cased."""
    data = ctx.sys.read_bytes(LOADER_INFO)
    if not data or len(data) <= 4:
        return None
    text = data[4:].decode("utf-16-le", "replace").rstrip("\x00").strip().lower()
    for name in ("systemd-boot", "grub", "limine", "refind"):
        if text.startswith(name):
            return name
    return "other"


def detect_cachyos_backend(ctx: Context) -> KargBackend:
    """Pick the boot loader this CachyOS install uses, or a manual backend when it is not clear."""
    sys = ctx.sys
    complete = {
        "limine": sys.exists("/usr/bin/limine-entry-tool"),
        "systemd-boot": all(sys.exists(p) for p in ("/usr/bin/sdboot-manage", "/etc/sdboot-manage.conf")),
        "grub": all(sys.exists(p) for p in ("/usr/bin/grub-mkconfig", "/etc/default/grub")),
    }
    partial = {
        "limine": any(sys.exists(p) for p in ("/usr/bin/limine-entry-tool", "/etc/default/limine",
                                              "/usr/bin/limine-mkinitcpio", "/usr/bin/limine")),
        "systemd-boot": any(sys.exists(p) for p in ("/usr/bin/sdboot-manage", "/etc/sdboot-manage.conf")),
        "grub": any(sys.exists(p) for p in ("/usr/bin/grub-mkconfig", "/etc/default/grub", "/boot/grub/grub.cfg")),
        "refind": any(sys.exists(p) for p in ("/boot/refind_linux.conf", "/usr/bin/refind-install")),
    }
    backends = {"limine": LimineBackend, "systemd-boot": SdbootBackend, "grub": GrubBackend}
    booted = loader_info(ctx)
    if booted == "refind":
        return ManualBackend(ctx, "rEFInd boots this system", REFIND_STEPS)
    if booted in backends:
        if complete[booted]:
            return backends[booted](ctx)
        if booted == "systemd-boot":
            return ManualBackend(ctx, "systemd-boot boots this system without sdboot-manage",
                                 "add the arguments to /etc/kernel/cmdline and rebuild the boot entries the way "
                                 "your setup does")
        return ManualBackend(ctx, f"{booted} boots this system, but its configuration tools are missing")
    found = [name for name, there in partial.items() if there]
    if len(found) == 1 and found[0] in backends and complete[found[0]]:
        return backends[found[0]](ctx)
    if found == ["refind"]:
        return ManualBackend(ctx, "rEFInd boots this system", REFIND_STEPS)
    if not found:
        return ManualBackend(ctx, "no supported boot loader configuration was found")
    if len(found) == 1:
        return ManualBackend(ctx, f"{found[0]} is set up, but its configuration tools are missing")
    return ManualBackend(ctx, f"more than one boot loader is set up ({', '.join(found)}), so it is not clear "
                              "which one boots this system")
