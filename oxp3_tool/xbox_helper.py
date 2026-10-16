"""Turn the ONEXPLAYER 3 Xbox key into the Steam button for InputPlumber.

On some OXP3 units the controller MCU reports the Xbox key only on its vendor
HID interface, as button 0x25 (frame ``b2 3f 01 01 1f 80 25 ...``), and
InputPlumber 0.82 cannot decode that id, so the key does nothing. This helper
reads the same vendor interface (read-only: it never writes to the controller)
and sends Gamepad:Button:Guide press and release events to InputPlumber's
ONEXPLAYER 3 composite device over D-Bus (SendEvent).

The xbox-key fix installs this file as a standalone script and runs it as a
systemd service. It only uses the standard library and busctl.
"""

from __future__ import annotations

import glob
import os
import re
import signal
import subprocess
import sys
import time
from typing import Optional

VENDOR = "1A86"
PRODUCT = "FE00"
INTERFACE = 2
BUTTON = 0x25
EVENT = "Gamepad:Button:Guide"
DEVICE_NAME = "ONEXPLAYER 3"
BUS = "org.shadowblip.InputPlumber"
IFACE = "org.shadowblip.Input.CompositeDevice"
BUSCTL = "/usr/bin/busctl"
PACKET_SIZE = 64
RETRY_SECONDS = 2.0

_COMPOSITE_RE = re.compile(r"^/org/shadowblip/InputPlumber/CompositeDevice\d+$")


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


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
        res = subprocess.run([BUSCTL, "--system", *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"busctl failed: {e}")
        return None
    return res.stdout if res.returncode == 0 else None


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
    def __init__(self) -> None:
        self.path: Optional[str] = None
        self.pressed = False

    def send(self, pressed: bool) -> None:
        for _ in range(2):
            if self.path is None:
                self.path = find_composite()
                if self.path is None:
                    log("InputPlumber has no ONEXPLAYER 3 device right now")
                    return
            value = "true" if pressed else "false"
            if busctl("call", BUS, self.path, IFACE, "SendEvent", "sv", EVENT, "b", value) is not None:
                self.pressed = pressed
                return
            self.path = None  # InputPlumber restarted: look the device up again


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
                log(f"reading {node}; the Xbox key (button 0x{BUTTON:02x}) sends {EVENT}")
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
            if sender.pressed:
                sender.send(False)
            time.sleep(RETRY_SECONDS)
    finally:
        if sender.pressed:
            sender.send(False)


if __name__ == "__main__":
    sys.exit(run())
