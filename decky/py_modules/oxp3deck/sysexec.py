"""Run a program with a clean environment and stream its output line by line.

Both pipes are read with poll() on the calling thread, so a program that fills one pipe while we wait on the other
cannot deadlock, and on_line sees each line as it arrives (a job shows install.sh's output while it runs).
"""
from __future__ import annotations

import logging
import os
import select
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass

from . import env

log = logging.getLogger("oxp3deck")

KEEP_MAX = 16 << 20     # bytes of each stream kept in Result; more is still streamed to on_line
LINE_MAX = 64 << 10     # a longer line without a newline is passed on in pieces
EXIT_GRACE = 1.0        # seconds to wait for EOF after the program exits (a background child may keep the pipe)


@dataclass
class Result:
    rc: int     # exit code (128 + N when killed by signal N, like a shell); -1: could not start; -2: timed out
    out: str
    err: str


class _Pipe:
    def __init__(self) -> None:
        self.partial = bytearray()
        self.kept = bytearray()

    def feed(self, chunk: bytes) -> list[str]:
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

    def text(self) -> str:
        return self.kept.decode("utf-8", errors="replace")


def _line(raw: bytes) -> str:
    s = bytes(raw).decode("utf-8", errors="replace").rstrip("\r")
    # A progress meter redraws itself with \r: keep what a terminal would show last.
    return s.rsplit("\r", 1)[-1] if "\r" in s else s


def run(argv: list[str], *, timeout: float | None = None, on_line=None, env_extra: dict | None = None,
        cwd: str | None = None, start_new_session: bool = False) -> Result:
    """Run argv (no shell) with stdin from /dev/null and env.clean_env(env_extra). Never raises."""
    argv = [str(a) for a in argv]
    if not argv:
        return Result(-1, "", "run: empty command")
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env.clean_env(env_extra), cwd=cwd, close_fds=True,
                                start_new_session=start_new_session)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return Result(-1, "", f"{argv[0]}: cannot start: {e}")

    callback = [on_line]

    def emit(lines: list[str]) -> None:
        for line in lines:
            if callback[0] is None:
                return
            try:
                callback[0](line)
            except Exception:
                log.exception("run %s: on_line failed; no more lines are passed on", argv[0])
                callback[0] = None

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
    note = f"{argv[0]}: timed out after {timeout:g} s"
    return Result(-2, out, f"{err.rstrip()}\n{note}" if err.strip() else note)


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
