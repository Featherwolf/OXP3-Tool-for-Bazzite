# Adapted from ClawDeck (MIT, Copyright (c) 2026 IHulkSmashI and ClawDeck contributors).
"""Work done for the desktop user by the root backend.

xess-xmx-linux's scripts write ~/.config and enable systemd --user units, so they must run as the user, never as
root. They get `env -i` and a minimal environment: the 20261031 install.sh runs a bare `systemctl --user
import-environment`, which would copy anything else (Decky's variables) into the user's session. runuser starts
the program directly, so the user's login shell (fish) is not involved.

Root writes in the user's folders never go through a symlink the user could have planted.
"""
from __future__ import annotations

import errno
import os
import re
import stat

from . import sysexec

USER_PATH = "/usr/local/bin:/usr/bin:/bin"
_VAR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def user_env(ctx) -> list[str]:
    """The VAR=VALUE list the user's programs start with."""
    rt = f"/run/user/{ctx.uid}"
    return [f"HOME={ctx.home}", f"USER={ctx.user}", f"LOGNAME={ctx.user}", f"PATH={USER_PATH}",
            f"XDG_RUNTIME_DIR={rt}", f"DBUS_SESSION_BUS_ADDRESS=unix:path={rt}/bus"]


def run(ctx, argv, *, timeout=None, on_line=None, env_extra=None, cwd=None) -> sysexec.Result:
    """Run argv as the desktop user: runuser -u USER -- /usr/bin/env -i <user_env> [extra] argv. The working
    folder defaults to the user's home. Refuses to run as root."""
    if ctx.uid == 0 or ctx.user in ("", "root"):
        return sysexec.Result(-1, "", f"userexec: the desktop user is {ctx.user or 'unknown'}; not running as root")
    argv = [str(a) for a in argv]
    if not argv or "=" in argv[0]:
        return sysexec.Result(-1, "", f"userexec: bad command {argv[:1]}")   # env would take it for VAR=VALUE
    extra = []
    for k, v in (env_extra or {}).items():
        if not _VAR.match(str(k)):
            return sysexec.Result(-1, "", f"userexec: bad variable name {k!r}")
        extra.append(f"{k}={v}")
    runuser = sysexec.which("runuser") or "runuser"
    full = [runuser, "-u", ctx.user, "--", "/usr/bin/env", "-i", *user_env(ctx), *extra, *argv]
    return sysexec.run(full, timeout=timeout, on_line=on_line, cwd=cwd or ctx.home)


def open_dir(path: str) -> int:
    """Open a folder for *at() calls without following a symlink, except one that root owns in a folder root owns
    (/home -> /var/home on Bazzite): the user can neither create nor move those. Raises OSError."""
    if not path.startswith("/"):
        raise OSError(errno.EINVAL, "not an absolute path", path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    parts = [p for p in path.split("/") if p not in ("", ".")]
    hops = 0
    try:
        while parts:
            name = parts.pop(0)
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                hops += 1
                if st.st_uid != 0 or os.fstat(fd).st_uid != 0:
                    raise PermissionError(errno.EPERM, "refusing to follow a symlink root does not own", path)
                if hops > 40:
                    raise OSError(errno.ELOOP, "too many symlinks", path)
                target = os.readlink(name, dir_fd=fd)
                if target.startswith("/"):
                    os.close(fd)
                    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
                parts = [p for p in target.split("/") if p not in ("", ".")] + parts
                continue
            nfd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


def write_new(ctx, path: str, data: bytes, mode: int = 0o644) -> None:
    """Create `path` afresh with `data`, owned by the user. Whatever is at the path (a file or a planted symlink)
    is removed first and the file is created exclusively; the folders above it are opened with open_dir. The
    parent must exist (user_mkdirs). Raises OSError."""
    folder, name = os.path.split(os.path.abspath(path))
    if not name:
        raise IsADirectoryError(errno.EISDIR, "no file name", path)
    dfd = open_dir(folder)
    try:
        try:
            os.unlink(name, dir_fd=dfd)
        except FileNotFoundError:
            pass
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=dfd)
        with os.fdopen(fd, "wb") as f:
            if os.geteuid() == 0:
                os.fchown(f.fileno(), ctx.uid, ctx.gid)
            os.fchmod(f.fileno(), mode)
            f.write(data)
    finally:
        os.close(dfd)


def chown_tree(ctx, path: str) -> None:
    """Give a file, or a folder and everything in it, to the user. Symlinks are changed themselves (lchown) and
    never followed; os.fwalk works on folder descriptors, so a folder swapped for a link mid-walk is not entered.
    Does nothing when the backend is not root."""
    if os.geteuid() != 0:
        return
    os.chown(path, ctx.uid, ctx.gid, follow_symlinks=False)
    if os.path.islink(path) or not os.path.isdir(path):
        return
    for _top, dirs, files, dfd in os.fwalk(path, follow_symlinks=False):
        for name in dirs + files:
            try:
                os.chown(name, ctx.uid, ctx.gid, dir_fd=dfd, follow_symlinks=False)
            except FileNotFoundError:
                pass


def user_mkdirs(ctx, path: str) -> None:
    """mkdir -p as the user, so root never creates folders in the user's home. Refuses paths outside the home.
    Raises OSError."""
    real = os.path.realpath(path)    # a policy check only: mkdir runs with the user's own rights
    home = os.path.realpath(ctx.home)
    if not (real == home or real.startswith(home.rstrip("/") + "/")):
        raise PermissionError(errno.EPERM, f"outside the home of {ctx.user}", path)
    if os.path.isdir(path):
        return
    r = run(ctx, ["mkdir", "-p", "--", path], timeout=30)
    if r.rc != 0:
        raise OSError(errno.EIO, f"mkdir as {ctx.user} failed: {(r.err or r.out).strip()[-300:]}", path)
