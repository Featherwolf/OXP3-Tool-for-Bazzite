"""Run a program with a clean environment and stream its output line by line.

Both pipes are read with poll() on the calling thread, so a program that fills one pipe while we wait on the other
cannot deadlock, and on_line sees each line as it arrives (a job shows install.sh's output while it runs).

With output_file the program writes to a root-only file instead of a pipe, and the file is followed: a program
that must outlive the plugin process (install.sh while Decky updates the plugin) then has no pipe to lose.
"""
from __future__ import annotations

import logging
import os
import select
import shutil
import signal
import stat
import subprocess
import time
from dataclasses import dataclass

from . import env

log = logging.getLogger("oxp3deck")

KEEP_MAX = 16 << 20     # bytes of each stream kept in Result; more is still streamed to on_line
LINE_MAX = 64 << 10     # a longer line without a newline is passed on in pieces
EXIT_GRACE = 1.0        # seconds to wait for EOF after the program exits (a background child may keep the pipe)
RUN_DIR = "/run/oxp3deck"   # output files, root only (oxp3tool.py's RUN_DIR, the same folder)
FOLLOW_EVERY = 0.02     # seconds between reads of an output file while nothing new is in it: short, since a file
                        # does not slow its writer down, and FILE_CAP is checked only when we read (RS-1)
FILE_MAX = 8 << 20      # an output file read this far is emptied (tmpfs is memory); see _read_file
FILE_CAP = 16 << 20     # an output file this big is emptied unread: the program writes faster than we follow
SKIPPED = "[... output skipped: the program wrote faster than it could be followed ...]"


@dataclass
class Result:
    rc: int     # exit code (128 + N when killed by signal N, like a shell); -1: could not start; -2: timed out
    out: str
    err: str


class _Pipe:
    def __init__(self) -> None:
        self.partial = bytearray()
        self.kept = bytearray()
        self.gapped = False     # nothing came since gap()'s last note

    def feed(self, chunk: bytes) -> list[str]:
        self.gapped = False
        if len(self.kept) < KEEP_MAX:
            self.kept += chunk[:KEEP_MAX - len(self.kept)]
        self.partial += chunk
        lines = []
        while True:
            i = self.partial.find(b"\n")
            if i < 0:
                break
            lines.append(_line(self.partial[:i]))
            del self.partial[:i + 1]
        while len(self.partial) >= LINE_MAX:
            lines.append(_line(self.partial[:LINE_MAX]))
            del self.partial[:LINE_MAX]
        return lines

    def flush(self) -> list[str]:
        rest, self.partial = self.partial, bytearray()
        return [_line(rest)] if rest else []

    def gap(self, note: str) -> list[str]:
        """A part of the stream that is skipped: the unfinished line, then note on a line of its own (kept too); one
        note for gaps with nothing in between."""
        if self.gapped:
            return []
        self.gapped = True
        lines = self.flush()
        raw = ("\n" if lines else "") + note + "\n"
        if len(self.kept) < KEEP_MAX:
            self.kept += raw.encode()[:KEEP_MAX - len(self.kept)]
        return lines + [note]

    def text(self) -> str:
        return self.kept.decode("utf-8", errors="replace")


def _line(raw: bytes) -> str:
    s = bytes(raw).decode("utf-8", errors="replace").rstrip("\r")
    # A progress meter redraws itself with \r: keep what a terminal would show last.
    return s.rsplit("\r", 1)[-1] if "\r" in s else s


def run(argv: list[str], *, timeout: float | None = None, on_line=None, env_extra: dict | None = None,
        cwd: str | None = None, start_new_session: bool = False, output_file: str | None = None,
        on_start=None) -> Result:
    """Run argv (no shell) with stdin from /dev/null and env.clean_env(env_extra). Never raises.

    start_new_session: the program gets its own session and process group, and a timeout kills the whole group.
    output_file: a path in a root-only folder (run_dir) for a new file (O_EXCL, O_NOFOLLOW, 0600) that gets the
    program's stdout and stderr; it is read as it grows and passed to on_line until the program exits, then
    removed. When the plugin process dies first, the program carries on writing to the file (no pipe to lose, no
    SIGPIPE) and the file stays. Result.out then has both streams and Result.err only our own notes.
    on_start(pid): called once the program has started."""
    argv = [str(a) for a in argv]
    if not argv:
        return Result(-1, "", "run: empty command")
    if output_file is not None:
        return _run_to_file(argv, output_file, timeout=timeout, on_line=on_line, env_extra=env_extra, cwd=cwd,
                            start_new_session=start_new_session, on_start=on_start)
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env.clean_env(env_extra), cwd=cwd, close_fds=True,
                                start_new_session=start_new_session)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return Result(-1, "", f"{argv[0]}: cannot start: {e}")
    _started(argv[0], proc, on_start)
    emit = _emitter(argv[0], on_line)
    pipes = {proc.stdout.fileno(): _Pipe(), proc.stderr.fileno(): _Pipe()}
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        timed_out = _pump(proc, pipes, deadline, emit)
    finally:
        proc.stdout.close()
        proc.stderr.close()
    out, err = (p.text() for p in pipes.values())
    if not timed_out:
        try:
            rc = proc.wait(None if deadline is None else max(0.0, deadline - time.monotonic()))
            return Result(rc if rc >= 0 else 128 - rc, out, err)
        except subprocess.TimeoutExpired:
            pass   # closed its output but kept running
    _kill(proc, start_new_session)
    return _timed_out(argv[0], timeout, out, err)


def _timed_out(name: str, timeout, out: str, err: str) -> Result:
    note = f"{name}: timed out after {timeout:g} s"
    return Result(-2, out, f"{err.rstrip()}\n{note}" if err.strip() else note)


def _emitter(name: str, on_line):
    """emit(lines): each line to on_line; after on_line raises once, no more lines are passed on."""
    callback = [on_line]

    def emit(lines: list[str]) -> None:
        for line in lines:
            if callback[0] is None:
                return
            try:
                callback[0](line)
            except Exception:
                log.exception("run %s: on_line failed; no more lines are passed on", name)
                callback[0] = None
    return emit


def _started(name: str, proc: subprocess.Popen, on_start) -> None:
    if on_start is not None:
        try:
            on_start(proc.pid)
        except Exception:
            log.exception("run %s: on_start failed", name)


def _run_to_file(argv: list[str], path: str, *, timeout, on_line, env_extra, cwd, start_new_session,
                 on_start) -> Result:
    """run() with the program's stdout and stderr going to a new file at `path`, followed while it runs."""
    flags = os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        wfd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | flags, 0o600)
    except OSError as e:
        return Result(-1, "", f"{argv[0]}: cannot create {path}: {e.strerror or e}")
    rfd = None
    try:
        try:
            rfd = os.open(path, os.O_RDONLY | flags)   # our own offset; the folder is root's, nobody swaps the file
            # The child gets wfd as stdout and stderr (dup2 drops O_CLOEXEC); we keep it only to empty the file.
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=wfd, stderr=subprocess.STDOUT,
                                    env=env.clean_env(env_extra), cwd=cwd, close_fds=True,
                                    start_new_session=start_new_session)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            return Result(-1, "", f"{argv[0]}: cannot start: {e}")
        _started(argv[0], proc, on_start)
        emit = _emitter(argv[0], on_line)
        pipe = _Pipe()
        deadline = None if timeout is None else time.monotonic() + timeout
        timed_out = _follow(proc, rfd, wfd, pipe, deadline, emit)
        if timed_out:
            _kill(proc, start_new_session)
        grace = time.monotonic() + EXIT_GRACE    # a background child may go on writing
        while _read_file(rfd, wfd, pipe, emit) and time.monotonic() < grace:
            pass                 # what it wrote before it ended
        emit(pipe.flush())
        if timed_out:
            return _timed_out(argv[0], timeout, pipe.text(), "")
        rc = proc.wait()
        return Result(rc if rc >= 0 else 128 - rc, pipe.text(), "")
    finally:
        for fd in (wfd, rfd):
            if fd is not None:
                os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass


def _follow(proc: subprocess.Popen, rfd: int, wfd: int, pipe: _Pipe, deadline: float | None, emit) -> bool:
    """Pass on the output file's new lines until the program exits. Returns True when the deadline passed first."""
    while proc.poll() is None:
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            return True
        if not _read_file(rfd, wfd, pipe, emit):
            # a plain sleep: proc.wait(timeout) would poll in a loop of ever longer sleeps of its own
            time.sleep(max(0.0, FOLLOW_EVERY if deadline is None else min(FOLLOW_EVERY, deadline - now)))
    return False


def _read_file(rfd: int, wfd: int, pipe: _Pipe, emit, chunks: int = 16) -> bool:
    """Pass on what the program added to its output file since the last read (at most `chunks` reads, so the
    caller checks the deadline again). True when there was something.

    A file is not a pipe: nothing slows the program down while on_line is slow, and the file is emptied below only
    once a read reaches its end. One that reaches FILE_CAP all the same is emptied unread (RS-1): a SKIPPED line
    stands for what was in it."""
    got = False
    for _ in range(chunks):
        try:
            if os.fstat(wfd).st_size >= FILE_CAP:
                os.ftruncate(wfd, 0)        # the program appends (O_APPEND): it goes on at the new end
                os.lseek(rfd, 0, os.SEEK_SET)
                emit(pipe.gap(SKIPPED))
                return True
        except OSError:
            pass
        try:
            chunk = os.read(rfd, 65536)
        except InterruptedError:
            continue
        except OSError:
            return got
        if not chunk:
            break
        got = True
        emit(pipe.feed(chunk))
    else:
        return got           # more to come: no emptying in the middle
    if got and os.lseek(rfd, 0, os.SEEK_CUR) >= FILE_MAX:
        # Start the file again: the program appends (O_APPEND), so it goes on at the new end. A line it writes
        # between our last read and this truncate is lost; that needs FILE_MAX of output and the same instant.
        try:
            os.ftruncate(wfd, 0)
            os.lseek(rfd, 0, os.SEEK_SET)
        except OSError:
            pass
    return got


def run_dir(ctx) -> str:
    """ctx.sys_path(RUN_DIR), created 0700: the folder for output files (oxp3tool.py's _run_dir calls this).
    Refused when it is a symlink or someone else's folder (the owner check is against our own uid, which is root in
    the plugin). Raises OSError."""
    path = ctx.sys_path(RUN_DIR)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as e:
        raise OSError(f"{path} is not a folder (a symlink?): {e.strerror or e}") from e
    try:
        st = os.fstat(fd)
        if st.st_uid != os.geteuid():
            raise OSError(f"{path} belongs to uid {st.st_uid}, not to root")
        if stat.S_IMODE(st.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
    return path


def remove_files(ctx, pattern) -> list[str]:
    """Remove the regular files in ctx.sys_path(RUN_DIR) whose whole name matches `pattern` (a compiled regex):
    output files a plugin process that has gone left behind. Through a folder descriptor; nothing when the folder
    is missing, a symlink or not ours. Returns the names removed. Never raises."""
    try:
        dfd = os.open(ctx.sys_path(RUN_DIR), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return []
    removed = []
    try:
        if os.fstat(dfd).st_uid != os.geteuid():
            return []
        for name in os.listdir(dfd):
            if not pattern.fullmatch(name):
                continue
            try:
                if stat.S_ISREG(os.stat(name, dir_fd=dfd, follow_symlinks=False).st_mode):
                    os.unlink(name, dir_fd=dfd)
                    removed.append(name)
            except OSError:
                pass
    except OSError:
        pass
    finally:
        os.close(dfd)
    return removed


def proc_starttime(ctx, pid) -> int | None:
    """Field 22 of ctx.sys_path(/proc/PID/stat): when the process started, in clock ticks after boot. With the pid
    it names one process (a pid is reused, its start time is not). None when there is no such process, it has
    exited (a zombie) or pid is not a positive number."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        with open(ctx.sys_path(f"/proc/{pid}/stat"), "rb") as f:
            text = f.read(4096).decode("ascii", errors="replace")
    except OSError:
        return None
    # pid (comm) state ppid ...: comm may hold spaces and ')', so split after the last ')'
    fields = text[text.rfind(")") + 1:].split()
    if len(fields) < 20 or fields[0] in ("Z", "X"):
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def proc_alive(ctx, pid, starttime) -> bool:
    """Whether the process recorded as (pid, starttime) by proc_starttime still runs."""
    return starttime is not None and proc_starttime(ctx, pid) == starttime


def _pump(proc: subprocess.Popen, pipes: dict, deadline: float | None, emit) -> bool:
    """Read both pipes until EOF. Returns True when the deadline passed first."""
    poller = select.poll()
    for fd in pipes:
        poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
    live = set(pipes)
    exited_at = None
    while live:
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            return True
        if exited_at is None and proc.poll() is not None:
            exited_at = now
        if exited_at is not None and now - exited_at >= EXIT_GRACE:
            break
        wait = 0.5
        if deadline is not None:
            wait = min(wait, deadline - now)
        if exited_at is not None:
            wait = min(wait, EXIT_GRACE - (now - exited_at))
        for fd, _events in poller.poll(max(0.0, wait) * 1000):
            try:
                chunk = os.read(fd, 65536)
            except InterruptedError:
                continue
            except OSError:
                chunk = b""
            if chunk:
                emit(pipes[fd].feed(chunk))
            else:
                poller.unregister(fd)
                live.discard(fd)
    for p in pipes.values():
        emit(p.flush())
    return False


def _kill(proc: subprocess.Popen, group: bool) -> None:
    try:
        if group:
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except OSError:
        pass
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        log.warning("run: pid %d did not exit after SIGKILL", proc.pid)


def which(name: str) -> str | None:
    """The program's path in the system PATH (not Decky's, not the user's)."""
    return shutil.which(name, path=env.SYSTEM_PATH)
