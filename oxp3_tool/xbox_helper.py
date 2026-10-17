"""Give the ONEXPLAYER 3 Xbox key a job when InputPlumber cannot read it.

On some OXP3 units the controller MCU reports the Xbox key only on its vendor
HID interface, as button 0x25 (frame ``b2 3f 01 01 1f 80 25 ...``), and
InputPlumber 0.82 cannot decode that id, so the key does nothing. This helper
reads the same vendor interface (read-only: it never writes to the controller)
and, on each press, does what the buttons fix chose for the Xbox key
(/etc/oxp3-tool/keys.json, read on every press; the Steam button without it):
- a button tap or chord through InputPlumber's ONEXPLAYER 3 composite device
  over D-Bus (SendButtonChord: press, then release 80 ms later). SendEvent,
  which could hold a button, panics inside InputPlumber 0.82
  (blocking_send on a tokio worker), so holds are not possible this way;
- or a Steam action as the Steam user (steam_session.py): the on-screen
  keyboard, the Home screen, the frame limit toggle.

The xbox-key fix installs this file, bundled with steam_session.py, as a
standalone script and runs it as a systemd service. It only uses the standard
library, busctl and gamescopectl.
"""

from __future__ import annotations

import glob
import os
import re
import signal
import subprocess
import time
from typing import Optional

from .steam_session import KEYS_CONFIG, URLS, Actions, key_config, log

VENDOR = "1A86"
PRODUCT = "FE00"
INTERFACE = 2
BUTTON = 0x25
DEVICE_NAME = "ONEXPLAYER 3"
BUS = "org.shadowblip.InputPlumber"
IFACE = "org.shadowblip.Input.CompositeDevice"
BUSCTL = "/usr/bin/busctl"
PACKET_SIZE = 64
RETRY_SECONDS = 2.0
MIN_INTERVAL = 0.3  # seconds between two Steam actions (the frame limit toggles)
SEND_FAILED = "could not send the button to InputPlumber"
OGUI_SESSION = "ogui"
# Buttons InputPlumber's Steam Deck target gets for each action. They skip
# InputPlumber's profile, so the keyboard chord is spelled out as Steam+X; the
# target itself turns Screenshot into Steam+R1.
CHORDS = {
    "guide": ["Guide"],
    "quick-access": ["QuickAccess"],
    "screenshot": ["Screenshot"],
    "keyboard-chord": ["Guide", "North"],
    "ogui-menu": ["QuickAccess2"],
}
STEAM_ACTIONS = set(URLS) | {"fps-limit"}

_COMPOSITE_RE = re.compile(r"^/org/shadowblip/InputPlumber/CompositeDevice\d+$")


def chord_for(action: str, session: object) -> list[str]:
    if action == "ogui-menu" and session != OGUI_SESSION:
        return ["Screenshot"]  # what the OGUI menu key does with native Steam Input
    return CHORDS.get(action, CHORDS["guide"])


def find_hidraw(root: str = "/") -> Optional[str]:
    """The /dev node of the controller's vendor interface (USB interface 2 of 1a86:fe00)."""
    for cls in sorted(glob.glob(os.path.join(root, "sys/class/hidraw/hidraw*"))):
        hid = os.path.realpath(os.path.join(cls, "device"))
        if f":{VENDOR}:{PRODUCT}." not in os.path.basename(hid).upper():
            continue
        try:
            with open(os.path.join(os.path.dirname(hid), "bInterfaceNumber")) as f:
                if int(f.read().strip(), 16) != INTERFACE:
                    continue
        except (OSError, ValueError):
            continue
        name = os.path.basename(cls)
        # InputPlumber moves the nodes of devices it manages here.
        for node in (f"dev/{name}", f"dev/inputplumber/sources/{name}"):
            path = os.path.join(root, node)
            if os.path.exists(path):
                return path
    return None


def parse_report(data: bytes) -> Optional[bool]:
    """True/False for a press/release of BUTTON, None for any other report.

    Mirrors InputPlumber's oxp_hid checks: a 64-byte frame with 0x3F at bytes
    1 and 62, command 0xB2, button id at byte 6, state at byte 12 (1 = pressed).
    """
    if len(data) != PACKET_SIZE or data[0] != 0xB2 or data[1] != 0x3F or data[62] != 0x3F:
        return None
    if data[6] != BUTTON:
        return None
    return data[12] == 1


def busctl(*args: str) -> Optional[str]:
    try:
        res = subprocess.run([BUSCTL, "--system", "--timeout=2", *args], capture_output=True, text=True,
                             timeout=5)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"busctl {args[0]} failed: {e}")
        return None
    if res.returncode != 0:
        log(f"busctl {args[0]} failed: {res.stderr.strip()[:200]}")
        return None
    return res.stdout


def find_composite() -> Optional[str]:
    """D-Bus path of InputPlumber's ONEXPLAYER 3 composite device."""
    tree = busctl("--list", "tree", BUS)
    if tree is None:
        return None
    for path in tree.split():
        if not _COMPOSITE_RE.match(path):
            continue
        name = busctl("get-property", BUS, path, IFACE, "Name")
        if name is not None and name.strip() == f's "{DEVICE_NAME}"':
            return path
    return None


class Sender:
    def __init__(self, actions: Optional[Actions] = None, config_path: str = KEYS_CONFIG) -> None:
        self.path: Optional[str] = None
        self.pressed = False  # the key's state from the frames
        self.config_path = config_path
        self.actions = actions or Actions(config_path=config_path)
        self.last = float("-inf")

    def send(self, pressed: bool) -> None:
        was, self.pressed = self.pressed, pressed
        if not pressed or was:
            return  # a tap already includes the release
        config = key_config(self.config_path)
        action = config.get("xbox")
        if not isinstance(action, str):
            action = "guide"
        if action in STEAM_ACTIONS:
            now = time.monotonic()
            if now - self.last >= MIN_INTERVAL:
                self.last = now
                self.actions.perform(action)
            return
        self.send_chord(chord_for(action, config.get("session")))

    def send_chord(self, buttons: list[str]) -> None:
        events = [f"Gamepad:Button:{b}" for b in buttons]
        for _ in range(2):
            if self.path is None:
                self.path = find_composite()
                if self.path is None:
                    break
            if busctl("call", BUS, self.path, IFACE, "SendButtonChord", "as", str(len(events)), *events) is not None:
                log(f"ok: sent {'+'.join(buttons)}")
                return
            self.path = None  # InputPlumber restarted: look the device up again
        log(f"failed: {SEND_FAILED}")


def run() -> int:
    sender = Sender()

    def stop(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    announced = None
    try:
        while True:
            node = find_hidraw()
            if node is None:
                if announced != "missing":
                    log("waiting for the controller's vendor interface (1a86:fe00, interface 2)")
                    announced = "missing"
                time.sleep(RETRY_SECONDS)
                continue
            try:
                fd = os.open(node, os.O_RDONLY | os.O_CLOEXEC)
            except OSError as e:
                log(f"cannot open {node}: {e}")
                time.sleep(RETRY_SECONDS)
                continue
            if announced != node:
                log(f"reading {node} for the Xbox key (button 0x{BUTTON:02x})")
                announced = node
            try:
                while True:
                    state = parse_report(os.read(fd, PACKET_SIZE))
                    if state is not None and state != sender.pressed:
                        sender.send(state)
            except OSError as e:
                log(f"{node}: {e}; reopening")
            finally:
                os.close(fd)
            sender.pressed = False
            time.sleep(RETRY_SECONDS)
    finally:
        sender.pressed = False


if __name__ == "__main__":
    raise SystemExit(run())
