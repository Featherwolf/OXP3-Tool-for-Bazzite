"""Who the desktop user is, where the plugin keeps its files, and settings.json.

The backend runs as root (plugin.json flag "root"), so HOME is /root: everything about the user comes from
DECKY_USER_HOME. Decky chowns the settings, data and log folders to the user at every load, so files read from
them are untrusted input and files written there must not follow a link the user planted.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import pwd
import secrets
import stat
import threading
from dataclasses import dataclass

from . import userexec

log = logging.getLogger("oxp3deck")


@dataclass
class Ctx:
    user: str
    uid: int
    gid: int
    home: str
    settings_dir: str
    runtime_dir: str
    log_dir: str
    plugin_dir: str
    root: str = "/"

    def sys_path(self, p: str) -> str:
        """An absolute system path (/proc, /sys, /run, /usr, /etc, /var) under `root`, which tests point at a
        temporary folder. Paths in the home, or already under root, are returned unchanged."""
        root = self.root.rstrip("/")
        if not root or not p.startswith("/"):
            return p
        home = self.home.rstrip("/")
        for keep in (home, root):
            if keep and (p == keep or p.startswith(keep + "/")):
                return p
        return root + "/" + p.lstrip("/")


def from_decky() -> Ctx:
    """The context from Decky's variables. Raises RuntimeError when no user owns DECKY_USER_HOME."""
    try:
        import decky  # injected by Decky Loader; tests install a stub
    except ImportError:
        decky = None

    def var(name: str) -> str:
        return str(getattr(decky, name, "") or os.environ.get(name, ""))

    given = var("DECKY_USER_HOME").rstrip("/")
    if not given:
        raise RuntimeError("context: DECKY_USER_HOME is not set")
    home = os.path.realpath(given)   # Bazzite: /home is a link to /var/home
    try:
        pw = pwd.getpwuid(os.stat(home).st_uid)
    except (OSError, KeyError) as e:
        raise RuntimeError(f"context: no user owns {given}: {e}") from e

    def in_home(p: str) -> str:
        # Same spelling as `home`, without resolving links below it (the user controls those).
        p = os.path.normpath(p) if p else p
        if p == given or p.startswith(given + "/"):
            return home + p[len(given):]
        return p

    return Ctx(user=pw.pw_name, uid=pw.pw_uid, gid=pw.pw_gid, home=home,
               settings_dir=in_home(var("DECKY_PLUGIN_SETTINGS_DIR")),
               runtime_dir=in_home(var("DECKY_PLUGIN_RUNTIME_DIR")),
               log_dir=in_home(var("DECKY_PLUGIN_LOG_DIR")),
               plugin_dir=in_home(var("DECKY_PLUGIN_DIR")))


def read_small(path: str, max_bytes: int = 1_000_000, dir_fd: int | None = None) -> bytes | None:
    """A regular file's content, or None when it is missing, too big, not a regular file or a symlink. Opened
    non-blocking so a FIFO planted in a user folder cannot hang the backend. dir_fd: `path` is relative to that
    folder descriptor."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            return None
        chunks = []
        size = 0
        while size <= max_bytes:
            chunk = os.read(fd, 65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            size += len(chunk)
        return None
    except OSError:
        return None
    finally:
        os.close(fd)


def _open_folder(folder: str) -> int:
    """The folder for *at() calls. Its parents are walked with userexec.open_dir (no symlink the user could have
    planted in Decky's user-owned folders), and the folder itself must not be a link at all."""
    parent, base = os.path.split(folder.rstrip("/") or "/")
    if not base:
        return os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    pfd = userexec.open_dir(parent or "/")
    try:
        return os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=pfd)
    finally:
        os.close(pfd)


def write_atomic(path: str, data: bytes, mode: int = 0o644) -> None:
    """Replace `path` with `data` through a new temporary file in the same folder, then os.replace. The folder is
    opened without following a link (_open_folder) and the temporary file is created exclusively; the rename
    replaces a link at `path` instead of writing through it. Raises OSError."""
    folder, name = os.path.split(os.path.abspath(path))
    dfd = _open_folder(folder)
    try:
        tmp = f".{name}.{secrets.token_hex(4)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=dfd)
        try:
            with os.fdopen(fd, "wb") as f:
                os.fchmod(f.fileno(), mode)   # not reduced by the umask
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            raise
    finally:
        os.close(dfd)


class Settings:
    """settings.json: a flat JSON object, kept in memory and written whole on every change. Never raises: set()
    and update() return None, or an error text when the value is not JSON or the file could not be written (the
    value is then kept in memory only)."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict = {}
        raw = read_small(path)
        if raw is not None:
            try:
                data = json.loads(raw.decode("utf-8"))
            except ValueError as e:
                log.warning("settings: %s is not valid JSON, starting empty: %s", path, e)
                data = {}
            self._data = data if isinstance(data, dict) else {}

    def get(self, key: str, default=None):
        with self._lock:
            return copy.deepcopy(self._data.get(key, default))

    def all(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def set(self, key: str, value) -> str | None:
        return self.update({key: value})

    def update(self, values: dict) -> str | None:
        with self._lock:
            new = dict(self._data)
            new.update(values)
            try:
                blob = (json.dumps(new, indent=2, sort_keys=True) + "\n").encode("utf-8")
            except (TypeError, ValueError) as e:
                return f"settings: not JSON: {e}"
            self._data = json.loads(blob)   # a private copy, so callers' objects can change freely
            try:
                write_atomic(self.path, blob)
            except OSError as e:
                log.warning("settings: cannot write %s: %s", self.path, e)
                return f"settings: cannot write {self.path}: {e.strerror or e}"
        return None
