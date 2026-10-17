"""Turn InputPlumber D-Bus actions into Steam actions (on-screen keyboard, Home screen, frame limit).

The buttons fix maps keys to InputPlumber D-Bus actions instead of gamepad
buttons when Steam itself cannot do what the key should (Steam+X does nothing
on some setups, and no button opens the Home screen or toggles the frame
limit). InputPlumber then sends no key or button at all: its D-Bus target
emits org.shadowblip.Input.DBusDevice.InputEvent(action, 1.0) on press and
0.0 on release. This helper watches for that signal (``busctl monitor``, as
root), checks that InputPlumber sent it, and performs the action as the Steam
user (steam_session.py).

The buttons fix installs this file, bundled with steam_session.py, as a
standalone script and runs it as a systemd service. It only uses the standard
library, busctl and gamescopectl.
"""

from __future__ import annotations

import json
import re
import signal
import subprocess
import time
from typing import Optional

from .steam_session import DBUS_ACTIONS, Actions, log

BUS = "org.shadowblip.InputPlumber"
SIGNAL_IFACE = "org.shadowblip.Input.DBusDevice"
# sender= makes the bus drop look-alike signals from anyone else (dbus-daemon and
# dbus-broker both match a well-known sender name against its current owner).
MATCH = f"type='signal',sender='{BUS}',interface='{SIGNAL_IFACE}',member='InputEvent'"
BUSCTL = "/usr/bin/busctl"
MIN_INTERVAL = 0.3  # seconds between two runs of one action (key bounce, repeated chords)
RETRY_SECONDS = 2.0
OWNER_RECHECK = 1.0  # seconds between two lookups of InputPlumber's bus name

ACTION_FOR = {name: action for action, name in DBUS_ACTIONS.items()}


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


class Watcher:
    def __init__(self, actions: Optional[Actions] = None) -> None:
        self.actions = actions or Actions()
        self.owner: Optional[str] = None
        self.looked_up = float("-inf")
        self.last: dict[str, float] = {}

    def handle(self, line: str) -> bool:
        """Perform the action for a press InputPlumber sent; True when it tried."""
        event = parse_event(line)
        if event is None:
            return False
        sender, name, value = event
        action = ACTION_FOR.get(name)
        if action is None or value < 0.5:
            return False  # other actions, and releases
        now = time.monotonic()
        if sender != self.owner:
            # InputPlumber restarted (new unique name), or a look-alike sender:
            # look the owner up again, at most once per OWNER_RECHECK.
            if now - self.looked_up < OWNER_RECHECK:
                return False
            self.looked_up = now
            self.owner = name_owner()
            if sender != self.owner:
                log(f"ignoring {name} from {sender or 'an unknown sender'}: not {BUS}")
                return False
        if now - self.last.get(action, float("-inf")) < MIN_INTERVAL:
            return False
        self.last[action] = now
        self.actions.perform(action)
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
        log("watching InputPlumber for " + ", ".join(sorted(ACTION_FOR)))
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
    raise SystemExit(run())
