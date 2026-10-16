"""Open Steam's on-screen keyboard when a key mapped to it is pressed.

With native Steam Input, the buttons fix maps the keyboard action to the
InputPlumber D-Bus action ``ui_osk`` instead of Steam's Steam+X shortcut,
which does nothing on some setups. InputPlumber then sends no key or button
at all: its D-Bus target emits the signal
org.shadowblip.Input.DBusDevice.InputEvent("ui_osk", 1.0) on press and 0.0 on
release. This helper watches for that signal (``busctl monitor``, as root),
checks that InputPlumber sent it, and hands steam://open/keyboard to the
running Steam client the way Valve's steam-runtime-steam-remote does: one
shell-quoted command line written to ~/.steam/steam.pipe. The write runs as
the Steam user, never as root. HHD opens the keyboard with the same URL.

The buttons fix installs this file as a standalone script and runs it as a
systemd service. It only uses the standard library and busctl.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import signal
import subprocess
import sys
import time
from typing import Optional

ACTION = "ui_osk"
URL = "steam://open/keyboard"
BUS = "org.shadowblip.InputPlumber"
SIGNAL_IFACE = "org.shadowblip.Input.DBusDevice"
MATCH = f"type='signal',interface='{SIGNAL_IFACE}',member='InputEvent'"
BUSCTL = "/usr/bin/busctl"
PYTHON = "/usr/bin/python3"
MIN_INTERVAL = 0.3  # seconds between two opens (key bounce, repeated chords)
RETRY_SECONDS = 2.0
# Valve's writer puts a fixed program path first; Steam ignores it.
PIPE_LINE = f"'~/.steam/root/ubuntu12_32/steam' '{URL}'\n"
# Logged when Steam is running but did not take the request; oxp3-tool status looks for it.
SEND_FAILED = "could not hand the keyboard request to Steam"

# Runs as the Steam user. Exit codes: 0 sent, 3 nobody reads the pipe, 1 other errors.
_WRITE = r"""
import errno, os, stat, sys
try:
    fd = os.open(sys.argv[1], os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOCTTY)
except OSError as e:
    sys.exit(3 if e.errno in (errno.ENOENT, errno.ENXIO) else 1)
try:
    if not stat.S_ISFIFO(os.fstat(fd).st_mode):
        sys.exit(1)
    os.write(fd, sys.argv[2].encode())
except OSError:
    sys.exit(1)
finally:
    os.close(fd)
"""
NOT_READING = 3


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def parse_event(line: str) -> Optional[tuple[str, str, float]]:
    """(sender, action, value) from one `busctl monitor --json=short` line, else None."""
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    if not isinstance(msg, dict) or msg.get("type") != "signal" or msg.get("member") != "InputEvent" \
            or msg.get("interface") != SIGNAL_IFACE:
        return None
    payload = msg.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "sd":
        return None
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != 2:
        return None
    action, value = data
    if not isinstance(action, str) or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    sender = msg.get("sender")
    return (sender if isinstance(sender, str) else ""), action, float(value)


def name_owner() -> Optional[str]:
    """Unique bus name of InputPlumber (it changes when InputPlumber restarts)."""
    try:
        res = subprocess.run([BUSCTL, "--system", "--timeout=2", "call", "org.freedesktop.DBus",
                              "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetNameOwner", "s", BUS],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.fullmatch(r's "(:[0-9.]+)"\s*', res.stdout) if res.returncode == 0 else None
    return m.group(1) if m else None


def steam_uids(proc: str = "/proc") -> list[int]:
    """Owners of processes named steam (the client or its launcher), root excluded."""
    uids: list[int] = []
    try:
        entries = os.listdir(proc)
    except OSError:
        return uids
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(proc, entry, "comm")) as f:
                if f.read().strip() != "steam":
                    continue
            uid = os.stat(os.path.join(proc, entry)).st_uid
        except OSError:
            continue
        if uid != 0 and uid not in uids:
            uids.append(uid)
    return uids


def send_as(uid: int) -> int:
    """Write PIPE_LINE to the user's ~/.steam/steam.pipe as that user; _WRITE's exit code."""
    try:
        pw = pwd.getpwuid(uid)
    except KeyError:
        return 1
    path = os.path.join(pw.pw_dir, ".steam", "steam.pipe")
    try:
        res = subprocess.run([PYTHON, "-I", "-c", _WRITE, path, PIPE_LINE], user=uid, group=pw.pw_gid,
                             extra_groups=[], env={"HOME": pw.pw_dir, "PATH": "/usr/bin"}, cwd="/",
                             stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"cannot write {path}: {e}")
        return 1
    return res.returncode


def open_keyboard(uids: list[int]) -> bool:
    if not uids:
        log("Steam is not running; nothing to open")
        return False
    codes = []
    for uid in uids:
        code = send_as(uid)
        if code == 0:
            log(f"asked Steam (uid {uid}) to open the keyboard")
            return True
        codes.append(code)
    why = "Steam is not reading ~/.steam/steam.pipe" if all(c == NOT_READING for c in codes) \
        else "writing ~/.steam/steam.pipe failed"
    log(f"{SEND_FAILED}: {why}")
    return False


class Watcher:
    def __init__(self) -> None:
        self.owner: Optional[str] = None
        self.last = float("-inf")

    def handle(self, line: str) -> bool:
        """Open the keyboard for a press of ACTION sent by InputPlumber; True when it tried."""
        event = parse_event(line)
        if event is None:
            return False
        sender, action, value = event
        if action != ACTION or value < 0.5:
            return False  # other actions, and releases
        if sender != self.owner:
            self.owner = name_owner()
            if sender != self.owner:
                log(f"ignoring {ACTION} from {sender or 'an unknown sender'}: not {BUS}")
                return False
        now = time.monotonic()
        if now - self.last < MIN_INTERVAL:
            return False
        self.last = now
        open_keyboard(steam_uids())
        return True


def run() -> int:
    def stop(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    watcher = Watcher()
    while True:
        try:
            mon = subprocess.Popen([BUSCTL, "--system", "monitor", "--json=short", f"--match={MATCH}"],
                                   stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, errors="replace",
                                   bufsize=1)
        except OSError as e:
            log(f"cannot start busctl monitor: {e}")
            time.sleep(RETRY_SECONDS)
            continue
        log(f"watching InputPlumber for the {ACTION} action")
        try:
            assert mon.stdout is not None
            for line in mon.stdout:
                watcher.handle(line)
        finally:
            mon.kill()
            mon.wait()
        log(f"busctl monitor stopped ({mon.returncode}); restarting")
        time.sleep(RETRY_SECONDS)


if __name__ == "__main__":
    sys.exit(run())
