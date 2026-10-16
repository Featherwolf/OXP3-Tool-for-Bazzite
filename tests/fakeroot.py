"""A fake ONEXPLAYER 3 running Bazzite, as a directory tree plus a fake command runner.

Values come from real OXP3 reports (DMI from InputPlumber issue #693, panel and
SSD IDs from community testing). InputPlumber files are minimal stand-ins with
the same structure as the 0.82.0 originals.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Optional, Sequence

from oxp3_tool.system import CommandResult, System

OXP3_DMI = {
    "sys_vendor": "ONE-NETBOOK",
    "product_name": "ONEXPLAYER 3",
    "product_version": "Default string",
    "board_vendor": "ONE-NETBOOK",
    "board_name": "ONEXPLAYER 3",
    "board_version": "1002-B",
    "bios_vendor": "American Megatrends International, LLC.",
    "bios_version": "5.09",
    "bios_date": "08/10/2026",
}

# Real OXP3 DMI modalias (spaces dropped by the kernel).
OXP3_MODALIAS = (
    "dmi:bvnAmericanMegatrendsInternational,LLC.:bvr5.09:bd08/10/2026:br5.39:efr0.13:"
    "svnONE-NETBOOK:pnONEXPLAYER3:pvrDefaultstring:rvnONE-NETBOOK:rnONEXPLAYER3:rvr1002-B:"
    "cvnDefaultstring:ct3:cvrDefaultstring:skuDefaultstring:"
)

IP_DEVICE_YAML = """\
version: 1
kind: CompositeDevice
name: ONEXPLAYER 3
matches:
  - dmi_data:
      product_name: ONEXPLAYER 3
      sys_vendor: ONE-NETBOOK
source_devices:
  - group: gamepad
    evdev:
      name: Microsoft X-Box 360 pad
options:
  auto_manage: true
target_devices:
  - xbox-elite
capability_map_id: oxp9
"""

IP_MAP_YAML = """\
version: 1
kind: CapabilityMap
name: OneXPlayer Type 9
id: oxp9
mapping:
  - name: ONEX
    source_events:
      - keyboard: KeyLeftCtrl
      - keyboard: KeyLeftAlt
      - keyboard: KeyLeftMeta
    target_event:
      gamepad:
        button: QuickAccess
  - name: Keyboard
    source_events:
      - keyboard: KeyLeftCtrl
      - keyboard: KeyLeftMeta
      - keyboard: KeyO
    target_event:
      gamepad:
        button: QuickAccess2
filtered_events: []
"""


# The panel's two EDID modes (fixed 380.16 MHz clock, htotal 2000), as xe prints them.
OXP3_MODES = {
    144: '"1920x1200": 144 380160 1920 1936 1952 2000 1200 1256 1260 1320 0x48 0xa',
    60: '"1920x1200": 60 380160 1920 1936 1952 2000 1200 3104 3108 3168 0x40 0xa',
}


def make_edid(vendor: str = "SDC", product: int = 0x4301, name: Optional[str] = "AMS881KB01-0") -> bytes:
    edid = bytearray(128)
    edid[0:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    raw = 0
    for ch in vendor:
        raw = (raw << 5) | (ord(ch) - ord("A") + 1)
    edid[8] = raw >> 8
    edid[9] = raw & 0xFF
    edid[10] = product & 0xFF
    edid[11] = product >> 8
    if name:
        desc = bytearray(18)
        desc[3] = 0xFC
        text = name.encode()[:13]
        desc[5:5 + len(text)] = text
        if len(text) < 13:
            desc[5 + len(text)] = 0x0A
        edid[72:90] = desc
    return bytes(edid)


class FakeRunner:
    """Fake rpm-ostree / systemctl / rpm / modinfo / udev tools."""

    def __init__(self, kargs: Optional[list[str]] = None):
        self.kargs = list(kargs or ["BOOT_IMAGE=(hd0,gpt2)/vmlinuz", "rhgb", "quiet", "root=UUID=x", "rw"])
        self.calls: list[list[str]] = []
        self.unit_active: dict[str, str] = {"inputplumber.service": "active"}
        self.unit_enabled: dict[str, str] = {}
        self.transaction: Optional[list] = None
        self.staged = False
        self.fail: set[str] = set()  # command prefixes (joined) that should fail
        self.hhd_units: list[str] = []
        self.packages = {"inputplumber": "0.82.0"}
        self.journal: dict[str, str] = {}  # boot ("0", "-1") -> kernel log
        self.bmi270_aliases = "i2c:bmi270\nacpi*:BMI0260:*\nacpi*:10EC5280:*\n"

    def _failing(self, argv: Sequence[str]) -> bool:
        joined = " ".join(argv)
        return any(joined.startswith(f) for f in self.fail)

    def __call__(self, argv: Sequence[str], timeout: Optional[int] = None) -> CommandResult:
        argv = list(argv)
        self.calls.append(argv)
        if self._failing(argv):
            return CommandResult(1, "", f"simulated failure of {shlex.join(argv)}")
        cmd = argv[0]
        if cmd == "rpm-ostree":
            return self._rpm_ostree(argv[1:])
        if cmd == "systemctl":
            return self._systemctl(argv[1:])
        if cmd == "rpm":
            name = argv[-1]
            if name in self.packages:
                return CommandResult(0, self.packages[name])
            return CommandResult(1, f"package {name} is not installed\n")
        if cmd == "journalctl" and "-k" in argv:
            boot = argv[argv.index("-b") + 1]
            return CommandResult(0, self.journal.get(boot, ""))
        if cmd == "modinfo":
            if argv[-1] == "bmi270_i2c":
                return CommandResult(0, self.bmi270_aliases)
            return CommandResult(1, "", "modinfo: ERROR: Module not found.")
        if cmd in ("systemd-hwdb", "udevadm", "restorecon"):
            return CommandResult(0)
        return CommandResult(0, f"(fake output of {shlex.join(argv)})\n")

    def _rpm_ostree(self, args: list[str]) -> CommandResult:
        if args and args[0] == "status":
            if "--json" in args:
                deployments = [{"booted": not self.staged}] + ([{"booted": True}] if self.staged else [])
                return CommandResult(0, json.dumps({"deployments": deployments, "transaction": self.transaction}))
            return CommandResult(0, "State: idle\n")
        if args and args[0] == "kargs":
            if len(args) == 1:
                return CommandResult(0, " ".join(self.kargs) + "\n")
            for a in args[1:]:
                key, _, value = a.partition("=")
                if key == "--append-if-missing":
                    if value not in self.kargs:
                        self.kargs.append(value)
                elif key == "--delete-if-present":
                    while value in self.kargs:
                        self.kargs.remove(value)
                else:
                    return CommandResult(1, "", f"unsupported fake option {a}")
            return CommandResult(0, "Kernel arguments updated.\nRun \"systemctl reboot\" to start a reboot\n")
        return CommandResult(1, "", "unsupported fake rpm-ostree command")

    def _systemctl(self, args: list[str]) -> CommandResult:
        verb = args[0]
        if verb == "is-active":
            state = self.unit_active.get(args[1], "inactive")
            return CommandResult(0 if state == "active" else 3, state + "\n")
        if verb == "is-enabled":
            state = self.unit_enabled.get(args[1], "disabled")
            return CommandResult(0 if state == "enabled" else 1, state + "\n")
        if verb == "enable":
            self.unit_enabled[args[1]] = "enabled"
            return CommandResult(0)
        if verb == "disable":
            self.unit_enabled[args[1]] = "disabled"
            return CommandResult(0)
        if verb == "list-units":
            lines = [f"{u} loaded active running Handheld Daemon" for u in self.hhd_units]
            return CommandResult(0, "\n".join(lines) + ("\n" if lines else ""))
        return CommandResult(0)


class FakeOxp3:
    def __init__(self, root: Path, **kw):
        self.root = Path(root)
        self.runner = FakeRunner(kw.pop("kargs", None))
        self.build(**kw)

    def write(self, path: str, content: str | bytes) -> None:
        p = self.root / path.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            content = content.encode()
        p.write_bytes(content)

    def symlink(self, path: str, target: str) -> None:
        p = self.root / path.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.is_symlink() or p.exists():
            p.unlink()
        os.symlink(target, p)

    def mkdir(self, path: str) -> None:
        (self.root / path.lstrip("/")).mkdir(parents=True, exist_ok=True)

    def build(self, dmi: Optional[dict] = None, os_id: str = "bazzite", image: str = "bazzite-deck",
              inputplumber: bool = True, gamescope: bool = True, gm7: bool = True,
              edid: Optional[bytes] = None, force_release: str = "369-370",
              imu_driver: Optional[str] = "bmi160_i2c", iio: Sequence[str] = (),
              game_mode: bool = True) -> None:
        for k, v in (dmi or OXP3_DMI).items():
            self.write(f"/sys/class/dmi/id/{k}", v + "\n")
        self.write("/etc/os-release", f'NAME="Bazzite"\nID={os_id}\nPRETTY_NAME="Bazzite 44 (FROM Fedora Kinoite)"\n')
        self.write("/usr/share/ublue-os/image-info.json", json.dumps({"image-name": image}))
        self.write("/proc/sys/kernel/osrelease", "7.2.9-ogc3.1.fc44.x86_64\n")
        self.write("/proc/cmdline", " ".join(self.runner.kargs) + "\n")
        self.write("/sys/power/mem_sleep", "[s2idle]\n")
        for tool in ("rpm-ostree", "systemctl", "systemd-hwdb", "udevadm", "rpm"):
            self.write(f"/usr/bin/{tool}", "")
        if inputplumber:
            self.write("/usr/bin/inputplumber", "")
            self.write("/usr/share/inputplumber/devices/50-onexplayer_3.yaml", IP_DEVICE_YAML)
            self.write("/usr/share/inputplumber/capability_maps/onexplayer_type9.yaml", IP_MAP_YAML)
            self.write("/usr/lib/systemd/system/inputplumber.service", "[Service]\n")
            self.write("/usr/lib/systemd/system/inputplumber-suspend.service", "[Service]\n")
        if gamescope:
            self.write("/usr/bin/gamescope", "")
        if gamescope and game_mode:
            self.write("/usr/bin/gamescope-session-plus", "")
        self.write("/sys/class/drm/card1-eDP-1/edid", make_edid() if edid is None else edid)
        self.write("/sys/class/drm/card1-eDP-1/status", "connected\n")
        self.mkdir("/sys/module/xe")
        if gm7:
            self.write("/sys/class/nvme/nvme0/model", "Predator SSD GM7 1TB\n")
            self.write("/sys/class/nvme/nvme0/device/vendor", "0x1dee\n")
            self.write("/sys/class/nvme/nvme0/device/device", "0x1602\n")
        else:
            self.write("/sys/class/nvme/nvme0/model", "Samsung SSD 990 PRO 2TB\n")
            self.write("/sys/class/nvme/nvme0/device/vendor", "0x144d\n")
            self.write("/sys/class/nvme/nvme0/device/device", "0xa80c\n")
        self.write("/sys/class/nvme/nvme0/firmware_rev", "BM345CVN\n")
        # Built-in keyboard on the i8042 controller.
        self.mkdir("/sys/bus/serio/drivers/atkbd")
        self.write("/sys/devices/platform/i8042/serio0/force_release", force_release + "\n")
        self.symlink("/sys/devices/platform/i8042/serio0/driver", "../../../../bus/serio/drivers/atkbd")
        self.symlink("/sys/bus/serio/devices/serio0", "../../../devices/platform/i8042/serio0")
        # IMU.
        self.mkdir("/sys/devices/pci0000:00/i2c-10EC5280:00")
        self.symlink("/sys/bus/i2c/devices/i2c-10EC5280:00", "../../../devices/pci0000:00/i2c-10EC5280:00")
        if imu_driver:
            self.mkdir(f"/sys/bus/i2c/drivers/{imu_driver}")
            self.symlink("/sys/devices/pci0000:00/i2c-10EC5280:00/driver", f"../../../bus/i2c/drivers/{imu_driver}")
        for i, name in enumerate(iio):
            self.write(f"/sys/bus/iio/devices/iio:device{i}/name", name + "\n")

    def system(self) -> System:
        return System(self.root, self.runner)

    def boot(self) -> None:
        """Simulate a reboot: the pending kernel arguments become active."""
        self.write("/proc/cmdline", " ".join(self.runner.kargs) + "\n")

    def panel_dpcd(self, tcon: bytes = b"\x01\x13\x00\x00", edp_rev: int = 0x03) -> None:
        """Expose the eDP AUX channel with Intel HDR backlight caps at 0x340 and eDP rev at 0x700."""
        regs = bytearray(0x800)
        regs[0x300:0x303] = b"\x00\xaa\x01"  # Intel source OUI, written by the kernel
        regs[0x340:0x344] = tcon
        regs[0x700] = edp_rev
        self.write("/dev/drm_dp_aux0", bytes(regs))
        self.mkdir("/sys/class/drm/card1-eDP-1/drm_dp_aux0")
        self.write("/sys/class/drm/card1-eDP-1/dpms", "On\n")

    def display_debugfs(self, refresh: int = 144, psr: str = "Panel Replay Selective Update enabled",
                        dmc: Optional[str] = "2.36", active: bool = True) -> None:
        """xe debugfs for the eDP panel (root only on a real system)."""
        d = "/sys/kernel/debug/dri/0000:00:02.0"
        crtc_mode = OXP3_MODES[refresh] if active else '"": 0 0 0 0 0 0 0 0 0 0 0x0 0x0'
        yes = "yes" if active else "no"
        lines = [
            "CRTC info", "---------",
            "[CRTC:151:pipe A]:",
            f"\tuapi: enable={yes}, active={yes}, mode={crtc_mode}",
            f"\thw: enable={yes}, active={yes}",
            f"\tadjusted_mode={crtc_mode}",
            f"\tpipe__mode={crtc_mode}",
            "\tpipe src=1920x1200+0+0, dither=no, bpp=30",
        ]
        if active:
            lines += ["\t[ENCODER:250:DDI A/PHY A]: connectors:", "\t\t[CONNECTOR:251:eDP-1]"]
        lines += [
            "\tunderrun reporting: cpu=yes pch=yes",
            "[CRTC:200:pipe B]:",
            '\tuapi: enable=no, active=no, mode="": 0 0 0 0 0 0 0 0 0 0 0x0 0x0',
            "",
            "Connector info", "--------------",
            "[CONNECTOR:251:eDP-1]: status: connected",
            "\tphysical dimensions: 160x100mm",
            "\tmax bpc: 10",
            "\tfixed modes:",
            f"\t\t{OXP3_MODES[144]}",
            f"\t\t{OXP3_MODES[60]}",
            "\tmodes:",
            f"\t\t{OXP3_MODES[144]}",
            f"\t\t{OXP3_MODES[60]}",
            "[CONNECTOR:260:DP-1]: status: disconnected",
        ]
        self.write(f"{d}/i915_display_info", "\n".join(lines) + "\n")
        self.write(f"{d}/eDP-1/i915_psr_status",
                   "Sink support: PSR = yes [0x03], Panel Replay = yes, Panel Replay Selective Update = yes, "
                   "Panel Replay DSC support = not supported\n"
                   f"PSR mode: {psr}\n")
        info = "DMC initialized: yes\nfw loaded: yes\npath: i915/xe3lpd_dmc.bin\n"
        if dmc:
            info += f"version: {dmc}\n"
        self.write(f"{d}/i915_dmc_info", info)
        self.symlink("/sys/kernel/debug/dri/0", "0000:00:02.0")

    def kwin_config(self, user: str = "gamer", refresh_mhz: Optional[int] = 144000,
                    vrr: Optional[str] = None, hdr: Optional[bool] = None) -> None:
        """KWin's saved output settings (~/.config/kwinoutputconfig.json)."""
        out: dict = {"edidIdentifier": "SDC 17153 0 1 2025 0", "connectorName": "eDP-1"}
        if refresh_mhz:
            out["mode"] = {"width": 1920, "height": 1200, "refreshRate": refresh_mhz}
        if vrr:
            out["vrrPolicy"] = vrr
        if hdr is not None:
            out["highDynamicRange"] = hdr
        other = {"edidIdentifier": "GSM 23305 1 2 2023 0", "connectorName": "DP-1",
                 "mode": {"width": 2560, "height": 1440, "refreshRate": 60000}, "vrrPolicy": "Always"}
        doc = [{"name": "outputs", "data": [other, out]},
               {"name": "setups", "data": []}]
        self.write(f"/var/home/{user}/.config/kwinoutputconfig.json", json.dumps(doc))

    def ogui_overlay(self, disabled_for: Optional[str] = None) -> None:
        """Bazzite deck Game Mode: Steam behind the OpenGamepadUI overlay, optionally
        switched off for one user with `ujust configure-opengamepadui disable`."""
        self.write("/usr/share/gamescope-session-plus/sessions.d/ogui-steam",
                   '/usr/libexec/hwsupport/non-valve-handheld-hardware && CLIENTCMD="opengamepadui '
                   '--accessibility disabled --overlay-mode --steam-input --steamos-manager --skip-update-pack '
                   '-- $CLIENTCMD"\n')
        if disabled_for:
            self.write(f"/var/home/{disabled_for}/.config/gamescope-session-plus/sessions.d/ogui-steam",
                       "source steam session\n")

    def update_before_reboot(self) -> None:
        """Simulate `bootc upgrade` staging an update built from the booted kernel arguments."""
        self.runner.kargs = (self.root / "proc/cmdline").read_text().split()
