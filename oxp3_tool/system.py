"""Host access layer.

Every file path the tool touches goes through :class:`System`, which maps an
absolute path onto ``root`` (``/`` on a real machine, a temporary tree in the
tests). External commands go through a runner so tests can replace them.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


Runner = Callable[[Sequence[str], Optional[int]], CommandResult]


def subprocess_runner(argv: Sequence[str], timeout: Optional[int] = None) -> CommandResult:
    try:
        proc = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return CommandResult(127, "", f"{argv[0]}: command not found")
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"{argv[0]}: timed out after {timeout}s")
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


class SimulatedRunner:
    """Runner for --root: never runs host commands, just records them.

    ``rpm-ostree kargs`` is emulated: the next boot's arguments start as the
    fake root's /proc/cmdline and changes are kept in ``<root>/.oxp3-sim-kargs``.
    Every other command "succeeds" with no output.
    """

    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self.calls: list[list[str]] = []

    def _kargs(self) -> list[str]:
        for name in (".oxp3-sim-kargs", "proc/cmdline"):
            try:
                return (self.root / name).read_text().split()
            except OSError:
                continue
        return []

    def __call__(self, argv: Sequence[str], timeout: Optional[int] = None) -> CommandResult:
        argv = list(argv)
        self.calls.append(argv)
        if argv[:2] == ["rpm-ostree", "kargs"]:
            kargs = self._kargs()
            for opt in argv[2:]:
                key, _, value = opt.partition("=")
                if key == "--append-if-missing" and value not in kargs:
                    kargs.append(value)
                elif key == "--delete-if-present":
                    kargs = [k for k in kargs if k != value]
            if len(argv) > 2:
                (self.root / ".oxp3-sim-kargs").write_text(" ".join(kargs) + "\n")
            return CommandResult(0, " ".join(kargs) + "\n")
        return CommandResult(0, "", "")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Suffix for temporary files written next to their target. InputPlumber
# scans its config directories with `path.extension().unwrap()`, so a
# temporary file must always carry a real extension (a dot-file such as
# ".foo" has none in Rust and would make InputPlumber panic).
TMP_SUFFIX = ".oxp3tmp"


class System:
    def __init__(self, root: str | os.PathLike = "/", runner: Optional[Runner] = None):
        self.root = Path(root)
        self._runner = runner or subprocess_runner

    # -- paths -----------------------------------------------------------

    def path(self, p: str) -> Path:
        if not p.startswith("/"):
            raise ValueError(f"expected an absolute path, got {p!r}")
        return self.root / p.lstrip("/")

    def exists(self, p: str) -> bool:
        return self.path(p).exists()

    def is_dir(self, p: str) -> bool:
        return self.path(p).is_dir()

    def is_file(self, p: str) -> bool:
        return self.path(p).is_file()

    def glob(self, pattern: str) -> list[str]:
        """Glob an absolute pattern; returns absolute host paths, sorted."""
        if not pattern.startswith("/"):
            raise ValueError(f"expected an absolute pattern, got {pattern!r}")
        found = self.root.glob(pattern.lstrip("/"))
        return sorted("/" + str(f.relative_to(self.root)) for f in found)

    def listdir(self, p: str) -> list[str]:
        try:
            return sorted(os.listdir(self.path(p)))
        except OSError:
            return []

    def realpath(self, p: str) -> Optional[str]:
        """Fully resolve ``p``; returns an absolute host path, or None if it does not exist."""
        local = self.path(p)
        if not os.path.lexists(local):
            return None
        resolved = Path(os.path.realpath(local))
        root = self.root.resolve()
        try:
            return "/" + str(resolved.relative_to(root)) if root != Path("/") else str(resolved)
        except ValueError:
            return str(resolved)

    # -- reading ---------------------------------------------------------

    def read_bytes(self, p: str) -> Optional[bytes]:
        try:
            return self.path(p).read_bytes()
        except OSError:
            return None

    def read_text(self, p: str) -> Optional[str]:
        data = self.read_bytes(p)
        if data is None:
            return None
        return data.decode("utf-8", errors="replace")

    def read_attr(self, p: str) -> Optional[str]:
        """Read a sysfs-style attribute, stripped of surrounding whitespace."""
        text = self.read_text(p)
        return None if text is None else text.strip()

    def sha256(self, p: str) -> Optional[str]:
        data = self.read_bytes(p)
        return None if data is None else sha256_bytes(data)

    # -- writing ---------------------------------------------------------

    def makedirs(self, p: str, mode: int = 0o755) -> None:
        self.path(p).mkdir(parents=True, exist_ok=True, mode=mode)

    def write_atomic(self, p: str, data: bytes, mode: int = 0o644) -> None:
        target = self.path(p)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + TMP_SUFFIX)
        try:
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, target)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

    def copy(self, src: str, dst: str) -> None:
        d = self.path(dst)
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.path(src), d)

    def remove(self, p: str) -> bool:
        try:
            self.path(p).unlink()
            return True
        except FileNotFoundError:
            return False

    def rmdir_if_empty(self, p: str) -> None:
        try:
            self.path(p).rmdir()
        except OSError as e:
            if e.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT, errno.ENOTDIR):
                raise

    def prune_empty_dirs(self, p: str) -> None:
        """Remove empty directories under ``p`` (and ``p`` itself if it ends up empty)."""
        top = self.path(p)
        if not top.is_dir():
            return
        for dirpath, _dirnames, _files in sorted(os.walk(top), key=lambda t: len(t[0]), reverse=True):
            try:
                os.rmdir(dirpath)
            except OSError:
                pass

    # -- commands --------------------------------------------------------

    def run(self, argv: Sequence[str], timeout: Optional[int] = 120) -> CommandResult:
        return self._runner(argv, timeout)

    def which(self, name: str, dirs: Sequence[str] = ("/usr/bin", "/usr/sbin", "/bin", "/sbin")) -> Optional[str]:
        for d in dirs:
            candidate = f"{d}/{name}"
            if self.is_file(candidate):
                return candidate
        return None

    @property
    def is_real_root(self) -> bool:
        return self.root.resolve() == Path("/")
