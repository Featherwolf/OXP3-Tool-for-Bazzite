"""Limine's boot menu as Limine 12.9 (UEFI) reads limine.conf: its entries, the default entry, the remembered
entry and the one-shot entry, and the UEFI variables behind the last two.

What Limine boots at power-on (common/menu.c): the one-shot entry (LoaderEntryOneShot, erased when read), else
with `remember_last_entry: yes` the entry that booted last (LimineLastBootedEntry, which Limine writes before
every boot), else `default_entry`. A default naming a directory or nothing stops the countdown at the menu.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

from .system import System

EFIVARS = "/sys/firmware/efi/efivars"
LIMINE_GUID = "513ee0d0-6e43-cb05-b272-f146a2fcb88a"
BLI_GUID = "4a67b082-0a4c-41cf-b6c7-440b29bb8c4f"
LAST_BOOTED_VAR = f"{EFIVARS}/LimineLastBootedEntry-{LIMINE_GUID}"
ONESHOT_VAR = f"{EFIVARS}/LoaderEntryOneShot-{BLI_GUID}"
SELECTED_VAR = f"{EFIVARS}/LoaderEntrySelected-{BLI_GUID}"
# In the EFI binary: this marker, then the enrolled BLAKE2b of limine.conf in hex (all zeros: none enrolled).
B2SUM_SIGNATURE = b"++CONFIG_B2SUM_SIGNATURE++"
# limine.conf locations Limine tries before the ESP's /limine.conf (config.c; the first is the EFI app's folder).
EARLIER_CONFIGS = ("EFI/limine/limine.conf", "EFI/BOOT/limine.conf", "boot/limine/limine.conf", "boot/limine.conf",
                   "limine/limine.conf")
EFI_BINARIES = ("EFI/limine/limine_x64.efi", "EFI/BOOT/BOOTX64.EFI")


class MenuError(Exception):
    """limine.conf has something this module does not read the way Limine does."""


@dataclass(eq=False)
class Entry:
    name: str
    depth: int
    expanded: bool
    line: int  # index of the entry's line in the file
    parent: Optional["Entry"] = None
    children: list["Entry"] = field(default_factory=list)
    options: dict[str, str] = field(default_factory=dict)  # first value of each (lower-case) key in its own body

    @property
    def is_dir(self) -> bool:
        return bool(self.children)

    @property
    def kernel_id(self) -> str:
        return self.options.get("kernel-id", "")

    def skipped(self) -> bool:
        """Limine hides it on x86-64 UEFI (should_skip_entry)."""
        if self.is_dir:
            return False
        if self.options.get("protocol", "") in ("bios", "bios_chainload"):
            return True
        fw = self.options.get("if_fw_type")
        if fw is not None and fw.lower() != "uefi":
            return True
        arch = self.options.get("if_arch")
        return arch is not None and not re.search(r"(^|[\s,])(x86[-_]64|x86_64|amd64)($|[\s,])", arch.lower())


def _escape(name: str) -> str:
    return re.sub(r"([\\/#])", r"\\\1", name)


@dataclass
class Menu:
    text: str
    lines: list[str]  # as Limine sees them: CRs dropped, spaces and tabs stripped at both ends
    top: list[Entry]

    def value(self, key: str) -> Optional[str]:
        """A global option as Limine's config_get_value(NULL, 0, key) reads it: the first line of the whole file
        that starts with `key:` (any case)."""
        at = self.value_line(key)
        return None if at is None else self.lines[at][len(key) + 1:].lstrip(" \t")

    def value_line(self, key: str) -> Optional[int]:
        k = key.lower() + ":"
        for i, line in enumerate(self.lines):
            if line[:len(k)].lower() == k:
                return i
        return None

    @property
    def first_entry_line(self) -> int:
        return self.top[0].line if self.top else len(self.lines)

    def walk(self, nodes: Optional[list[Entry]] = None):
        for e in self.top if nodes is None else nodes:
            yield e
            yield from self.walk(e.children)

    def siblings(self, e: Entry) -> list[Entry]:
        return e.parent.children if e.parent else self.top

    def path(self, e: Entry) -> str:
        dup = sum(1 for s in self.siblings(e)[:self.siblings(e).index(e)] if not s.skipped() and s.name == e.name)
        own = _escape(e.name) + (f"#{dup}" if dup else "")
        return f"{self.path(e.parent)}/{own}" if e.parent else own

    def bli_id(self, e: Entry) -> str:
        base = self._bli_base(e)
        dups = 0
        for other in self.walk():
            if other is e:
                break
            if not other.is_dir and not other.skipped() and self._bli_base(other) == base:
                dups += 1
        return f"{base}-{dups + 1}" if dups else base

    def _bli_base(self, e: Entry) -> str:
        out = "".join(c if re.fullmatch(r"[0-9A-Za-z+_.@-]", c) else "." if c == "/" else "-" for c in self.path(e))
        return out if out not in ("", ".", "..") else out + "-"

    def by_path(self, path: str) -> Optional[Entry]:
        """find_entry_by_path: name components split at unescaped '/', with an optional #N for the Nth duplicate."""
        nodes, found = self.top, None
        for comp in _split_path(path):
            if comp is None:
                return None
            name, dup = comp
            found, seen = None, 0
            for e in nodes:
                if e.skipped() or e.name != name:
                    continue
                if seen == dup:
                    found = e
                    break
                seen += 1
            if found is None:
                return None
            nodes = found.children
        return found

    def by_bli_id_or_path(self, value: str) -> Optional[Entry]:
        for e in self.walk():
            if not e.is_dir and not e.skipped() and self.bli_id(e) == value:
                return e
        return self.by_path(value)

    def by_index(self, index: int) -> Optional[Entry]:
        """The entry at a 0-based position among the visible ones (expanded directories show their entries)."""
        n = 0

        def visit(nodes: list[Entry]) -> Optional[Entry]:
            nonlocal n
            for e in nodes:
                if e.skipped():
                    continue
                if n == index:
                    return e
                n += 1
                if e.is_dir and e.expanded:
                    hit = visit(e.children)
                    if hit is not None:
                        return hit
            return None
        return visit(self.top)

    def default(self) -> Optional[Entry]:
        """The entry `default_entry` selects, None when Limine stops at its menu instead (a directory, or a value
        naming nothing; `1` without a default_entry line, which on CachyOS is the OS directory)."""
        value = self.value("default_entry")
        if value is None:
            value = "1"  # LoaderEntryDefault is read only without the option; CachyOS always writes it
        if re.fullmatch(r"[0-9]+", value):
            e = self.by_index(max(int(value) - 1, 0)) or self.by_index(0)
        else:
            e = self.by_path(value)
        return e if e is not None and not e.is_dir else None

    def remembers(self) -> bool:
        return (self.value("remember_last_entry") or "").lower() == "yes"

    def autoboots(self) -> bool:
        """The countdown runs (`timeout: no` waits for a key, also when the default entry is valid)."""
        return self.value("timeout") != "no"

    def timeout_text(self) -> str:
        value = self.value("timeout")
        if value is None:
            return "5 seconds"
        m = re.match(r"[0-9]+(\.[0-9]+)?", value)
        seconds = float(m.group(0)) if m else 0.0
        return "at once" if seconds == 0 else f"{seconds:g} second{'s' if seconds != 1 else ''}"

    def kernel_entry(self, package: str) -> Optional[Entry]:
        """The first entry limine-entry-tool maintains for a kernel package: its `comment: kernel-id=`, or a
        name equal to the package for an entry without one."""
        for e in self.walk():
            if e.is_dir or e.skipped():
                continue
            if e.kernel_id == package or (not e.kernel_id and e.name == package):
                return e
        return None

    def boots(self, oneshot: Optional[str], last_booted: Optional[str]) -> tuple[Optional[Entry], str]:
        """(entry, why) for the next power-on; entry None means Limine waits in its menu."""
        if oneshot:
            e = self.by_bli_id_or_path(oneshot)
            if e is not None and not e.is_dir:
                return e, "one-shot"
        if self.remembers() and last_booted:
            e = self.by_path(last_booted)
            if e is not None and not e.is_dir:
                return e, "remembered"
        e = self.default()
        if e is None:
            return None, "no valid default entry"
        return (e, "default") if self.autoboots() else (None, "timeout: no")


def _split_path(path: str) -> list[Optional[tuple[str, int]]]:
    comps: list[Optional[tuple[str, int]]] = []
    for raw in re.split(r"(?<!\\)/", path):  # Limine reads a path component up to an unescaped '/'
        m = re.fullmatch(r"(.*?)#([0-9]+)", raw)
        name, dup = (m.group(1), int(m.group(2))) if m else (raw, 0)
        comps.append((re.sub(r"\\(.)", r"\1", name), dup))
    return comps


def parse(text: str) -> Menu:
    lines = [ln.replace("\r", "").strip(" \t") for ln in text.split("\n")]
    top: list[Entry] = []
    stack: list[Entry] = []
    current: Optional[Entry] = None
    for i, line in enumerate(lines):
        if line.startswith("/"):
            depth = len(line) - len(line.lstrip("/"))
            rest = line[depth:]
            expanded = rest.startswith("+")
            name = (rest[1:] if expanded else rest).lstrip(" ")
            while stack and stack[-1].depth >= depth:
                stack.pop()
            if depth != (stack[-1].depth + 1 if stack else 1):
                raise MenuError(f"line {i + 1}: an entry two levels below its parent ({line!r})")
            e = Entry(name=name, depth=depth, expanded=expanded, line=i, parent=stack[-1] if stack else None)
            (stack[-1].children if stack else top).append(e)
            stack.append(e)
            current = e
        elif current is not None and ":" in line and not line.startswith("#"):
            key, _, value = line.partition(":")
            key = key.strip().lower()
            if key == "comment":
                m = re.search(r"kernel-id=([A-Za-z0-9@._+-]+)", value, re.I)
                if m:
                    current.options.setdefault("kernel-id", m.group(1))
            current.options.setdefault(key, value.strip(" \t"))
    return Menu(text=text, lines=lines, top=top)


def set_global(text: str, key: str, new_line: str, marker: Optional[str] = None) -> str:
    """Replace the line Limine reads `key` from (it must be a global one, above the first entry) with
    ``new_line``; ``marker`` goes on its own line above it (None removes a marker line found there)."""
    menu = parse(text)
    at = menu.value_line(key)
    if at is None or at >= menu.first_entry_line:
        raise MenuError(f"limine.conf has no global {key} line")
    raw = text.split("\n")
    raw[at] = new_line
    if marker is not None:
        raw.insert(at, marker)
    elif at > 0 and raw[at - 1].strip().startswith(MARKER_PREFIX):
        del raw[at - 1]
    return "\n".join(raw)


MARKER_PREFIX = "# oxp3-tool kernel try:"


def marker_for(original: str) -> str:
    return f'{MARKER_PREFIX} was "{original}"; `sudo oxp3-tool kernel keep` puts it back'


def marked_original(menu: Menu) -> Optional[str]:
    """The remember_last_entry line `kernel try` replaced, from its marker above the current line."""
    at = menu.value_line("remember_last_entry")
    if at is None or at == 0 or at >= menu.first_entry_line:
        return None
    m = re.fullmatch(re.escape(MARKER_PREFIX) + r' was "(.*)"; .*', menu.lines[at - 1])
    return m.group(1) if m else None


def read_efivar(sys: System, path: str, utf16: bool) -> Optional[str]:
    data = sys.read_bytes(path)
    if data is None or len(data) < 4:
        return None
    body = data[4:]  # efivarfs: 4 bytes of attributes, then the value
    try:
        text = body.decode("utf-16-le") if utf16 else body.decode("ascii")
    except UnicodeDecodeError:
        return None
    return text.split("\0", 1)[0] or None


def enrolled_hashes(sys: System, esp: str) -> dict[str, str]:
    """EFI binary -> the limine.conf checksum enrolled in it (binaries without one are left out)."""
    out = {}
    for rel in EFI_BINARIES:
        data = sys.read_bytes(f"{esp}/{rel}")
        at = data.find(B2SUM_SIGNATURE) if data else -1
        if at < 0:
            continue
        value = data[at + len(B2SUM_SIGNATURE):at + len(B2SUM_SIGNATURE) + 128].decode("ascii", "replace").lower()
        if value.strip("0"):
            out[rel] = value
    return out


def b2sum(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=64).hexdigest()
