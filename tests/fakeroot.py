"""A fake ONEXPLAYER 3 running Bazzite, as a directory tree plus a fake command runner.

Values come from real OXP3 reports (DMI from InputPlumber issue #693, panel and
SSD IDs from community testing). InputPlumber files are minimal stand-ins with
the same structure as the 0.82.0 originals.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
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
        self.ip_journal = ""  # this boot's inputplumber.service messages
        self.session_journal = ""  # this boot's other messages (gamescope)
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
        if cmd == "journalctl" and "--grep" in argv and "-u" not in argv:
            pattern = argv[argv.index("--grep") + 1]
            lines = [ln for ln in self.session_journal.splitlines() if pattern in ln]
            if "-r" in argv:
                lines.reverse()
            if "-n" in argv:
                lines = lines[:int(argv[argv.index("-n") + 1])]
            return CommandResult(0 if lines else 1, "".join(ln + "\n" for ln in lines))
        if cmd == "journalctl" and "inputplumber.service" in argv:
            pattern = argv[argv.index("--grep") + 1] if "--grep" in argv else ""
            lines = [ln for ln in self.ip_journal.splitlines() if pattern in ln]
            return CommandResult(0 if lines else 1, "".join(ln + "\n" for ln in lines))
        if cmd == "journalctl":
            return CommandResult(1)  # other units: nothing logged
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
            unit = args[-1]
            self.unit_enabled[unit] = "disabled"
            if "--now" in args:
                self.unit_active[unit] = "inactive"
            return CommandResult(0)
        if verb in ("start", "restart"):
            self.unit_active[args[1]] = "active"
            return CommandResult(0)
        if verb == "stop":
            self.unit_active[args[1]] = "inactive"
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
        for tool in ("rpm-ostree", "systemctl", "systemd-hwdb", "udevadm", "rpm", "busctl"):
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
        info = f"DMC initialized: yes\nfw loaded: {'yes' if dmc else 'no'}\npath: i915/xe3lpd_dmc.bin\n"
        if dmc:
            info += f"version: {dmc}\n"
        self.write(f"{d}/i915_dmc_info", info)
        self.symlink("/sys/kernel/debug/dri/0", "0000:00:02.0")

    def mcu_hidraw(self, moved: bool = True) -> None:
        """The controller MCU's three USB interfaces with hidraw nodes; interface 2 is the vendor one.

        InputPlumber moves the nodes of devices it manages to /dev/inputplumber/sources.
        """
        usb = "/sys/devices/pci0000:00/0000:00:14.0/usb1/1-5"
        for n in range(3):
            intf = f"{usb}/1-5:1.{n}"
            hid = f"{intf}/0003:1A86:FE00.000{8 + n}"
            self.write(f"{intf}/bInterfaceNumber", f"0{n}\n")
            self.mkdir(f"{hid}/hidraw/hidraw{n}")
            self.symlink(f"{hid}/hidraw/hidraw{n}/device", f"../../../0003:1A86:FE00.000{8 + n}")
            self.symlink(f"/sys/class/hidraw/hidraw{n}", f"../../..{hid}/hidraw/hidraw{n}")
            self.write(f"/dev/inputplumber/sources/hidraw{n}" if moved and n == 2 else f"/dev/hidraw{n}", b"")
        self.write("/usr/bin/busctl", "")

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


# -- CachyOS ---------------------------------------------------------------------

MACHINE_ID = "0123456789abcdef0123456789abcdef"
EFIVARS = "/sys/firmware/efi/efivars"
ONESHOT_VAR = f"{EFIVARS}/LoaderEntryOneShot-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f"
LAST_BOOTED_VAR = f"{EFIVARS}/LimineLastBootedEntry-513ee0d0-6e43-cb05-b272-f146a2fcb88a"
SELECTED_VAR = f"{EFIVARS}/LoaderEntrySelected-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f"
LIMINE_EFI = "/boot/EFI/limine/limine_x64.efi"
B2SUM_SIGNATURE = b"++CONFIG_B2SUM_SIGNATURE++"
CACHY_KERNELS = {"linux-cachyos-deckify": "7.2.11-1-cachyos-deckify", "linux-cachyos-lts": "6.18.55-2-cachyos-lts"}
CALAMARES_LIMINE = ('ESP_PATH="/boot"\n'
                    'KERNEL_CMDLINE[default]+="quiet nowatchdog splash rw rootflags=subvol=/@ root=UUID=abcd"\n')
CLI_INSTALLER_LIMINE = 'ESP_PATH="/boot"\nKERNEL_CMDLINE[default]="quiet rw root=UUID=abcd"\n'
SDBOOT_MANAGE_CONF = 'LINUX_OPTIONS="nowatchdog quiet splash"\nREMOVE_EXISTING="no"\nOVERWRITE_EXISTING="yes"\n'
DEFAULT_GRUB = ("GRUB_DEFAULT='0'\nGRUB_TIMEOUT='5'\nGRUB_CMDLINE_LINUX_DEFAULT='nowatchdog nvme_load=YES "
                "zswap.enabled=0 splash loglevel=3'\nGRUB_CMDLINE_LINUX=\"\"\n")


def _dquoted(value: str) -> str:
    """removeSurrounding(value.trim(), "\""), as limine-entry-tool's ConfigReader does (double quotes only)."""
    value = value.strip()
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] == '"' else value


def _quoted(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


class FakeCachyRunner(FakeRunner):
    """The CachyOS tools: pacman, limine-entry-tool/limine-mkinitcpio, sdboot-manage, grub-mkconfig.

    The boot-entry generators follow their real counterparts (limine-entry-tool's ConfigReader,
    sdboot-manage, grub-mkconfig's grub.d loop), written independently of oxp3_tool.kargs.
    """

    def __init__(self, root: Path, kernels: dict[str, str]):
        super().__init__(["initrd=\\initramfs.img", "quiet", "rw", "root=UUID=abcd"])
        self.root = root
        self.kernels = dict(kernels)
        self.packages = {"inputplumber": "0.83.1-1.1"}
        self.limine_exit = 0
        self.limine_skip: set[str] = set()
        self.gen_fail = 0  # how many sdboot-manage/grub-mkconfig runs fail next
        self.regenerations = 0
        self.enroll_exit = 0
        self.after_oneshot = None  # called after `bootctl set-oneshot` (another tool's write in the same moment)
        self.enroll_strips = False  # a failing limine-enroll-config leaves the binary without a checksum
        # Command -> how many of its runs pass before one gets Ctrl+C (before it writes anything).
        self.interrupt: dict[str, int] = {}
        # Command -> (runs that pass, signal this process then gets): a closed terminal or a stopped service.
        self.kill: dict[str, tuple[int, int]] = {}

    def read(self, path: str) -> Optional[str]:
        p = self.root / path.lstrip("/")
        return p.read_text() if p.is_file() else None

    def write(self, path: str, text: str) -> None:
        p = self.root / path.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def files(self, pattern: str) -> list[str]:
        return sorted("/" + str(p.relative_to(self.root)) for p in self.root.glob(pattern.lstrip("/")))

    def __call__(self, argv: Sequence[str], timeout: Optional[int] = None) -> CommandResult:
        argv = list(argv)
        cmd = argv[0]
        if cmd not in ("pacman", "limine-entry-tool", "limine-mkinitcpio", "bash", "sh", "sdboot-manage",
                       "grub-mkconfig", "bootctl", "limine-enroll-config"):
            return super().__call__(argv, timeout)
        self.calls.append(argv)
        if cmd in self.interrupt:
            self.interrupt[cmd] -= 1
            if self.interrupt[cmd] < 0:
                del self.interrupt[cmd]
                raise KeyboardInterrupt
        if cmd in self.kill:
            passes, signum = self.kill[cmd]
            if passes:
                self.kill[cmd] = (passes - 1, signum)
            else:
                del self.kill[cmd]
                os.kill(os.getpid(), signum)
        if self._failing(argv):
            return CommandResult(1, "", f"simulated failure of {shlex.join(argv)}")
        if cmd == "pacman" and argv[1] == "-Q":
            found = [f"{n} {self.packages[n]}" for n in argv[2:] if n in self.packages]
            return CommandResult(0 if len(found) == len(argv) - 2 else 1, "".join(f + "\n" for f in found))
        if cmd == "pacman" and argv[1] == "-Qqo":
            owners = []
            for path in argv[2:]:
                for name, version in self.kernels.items():
                    if path == f"/usr/lib/modules/{version}/modules.builtin":
                        owners.append(name)
            return CommandResult(0, "".join(o + "\n" for o in owners))
        if cmd == "limine-entry-tool" and argv[1:2] == ["--get-cmdline"]:
            return CommandResult(0, "some log line\n" + self.limine_cmdline(argv[2]) + "\n")
        if cmd == "limine-mkinitcpio":
            return self.limine_regen()
        script, skip = self.shell_script(argv)
        if cmd == "bash" and "/etc/sdboot-manage.conf" in script:
            values = self.shell_vars(["/etc/sdboot-manage.conf", *self.files("/usr/lib/sdboot-manage.conf.d/*.conf"),
                                      *self.files("/etc/sdboot-manage.conf.d/*.conf")], skip)
            return CommandResult(0, f"{values.get('LINUX_OPTIONS', '')}\n{values.get('NO_AUTOGEN', '')}\n")
        if cmd == "sdboot-manage" and argv[1:] == ["gen"]:
            return self.sdboot_gen()
        if cmd == "bootctl" and "--print-esp-path" in argv:
            return CommandResult(0, "/boot\n")
        if cmd == "bootctl" and argv[1:2] == ["set-oneshot"]:
            var = self.root / ONESHOT_VAR.lstrip("/")
            if argv[2]:
                var.parent.mkdir(parents=True, exist_ok=True)
                var.write_bytes(b"\x07\x00\x00\x00" + (argv[2] + "\0").encode("utf-16-le"))
            else:
                var.unlink(missing_ok=True)
            if self.after_oneshot:
                self.after_oneshot()
            return CommandResult(0)
        if cmd == "limine-enroll-config":
            self.enroll()
            return CommandResult(self.enroll_exit, "", "simulated failure" if self.enroll_exit else "")
        if cmd == "sh" and "/etc/default/grub" in script:
            values = self.shell_vars(["/etc/default/grub", *self.files("/etc/default/grub.d/*.cfg")], skip)
            return CommandResult(0, f"{values.get('GRUB_CMDLINE_LINUX', '')} "
                                    f"{values.get('GRUB_CMDLINE_LINUX_DEFAULT', '')}\n")
        if cmd == "grub-mkconfig":
            return self.grub_gen(argv[argv.index("-o") + 1])
        return CommandResult(0)

    # -- limine-entry-tool ---------------------------------------------------

    def limine_map(self) -> dict[str, str]:
        files = self.files("/usr/share/limine-entry-tool.d/*.conf") + ["/etc/limine-entry-tool.conf"] + \
            self.files("/etc/limine-entry-tool.d/*.conf") + ["/etc/default/limine"]
        cmdlines: dict[str, str] = {}
        for path in files:
            for line in (self.read(path) or "").splitlines():
                line = line.strip()
                if not line.startswith("KERNEL_CMDLINE"):
                    continue
                append = "+=" in line
                key, value = line.split("+=" if append else "=", 1)
                name = _dquoted(key[len("KERNEL_CMDLINE"):].strip("[] ")) or "default"
                value = _dquoted(value)
                if append and name in cmdlines:
                    cmdlines[name] = value + " " + cmdlines[name]  # limine-entry-tool prepends
                else:
                    cmdlines[name] = value
        return cmdlines

    def limine_cmdline(self, kernel: str) -> str:
        cmdlines = self.limine_map()
        if not cmdlines.get("default", "").strip():
            cmdlines["default"] = (self.read("/etc/kernel/cmdline") or "").strip() or \
                (self.read("/proc/cmdline") or "").strip()
        value = cmdlines.get(kernel, "")
        if not value.strip() and "fallback" in kernel:
            value = cmdlines.get("fallback", "")
        return value if value.strip() else cmdlines["default"]

    def limine_regen(self) -> CommandResult:
        self.regenerations += 1
        if self.limine_exit in (1, 2):
            return CommandResult(self.limine_exit, "", "simulated limine-mkinitcpio failure")
        old = self.read("/boot/limine.conf") or ""
        # limine-entry-tool keeps every line above the first entry as it is.
        head = old.split("\n/", 1)[0].split("\n") if "\n/" in old else \
            ["timeout: 5", "default_entry: 2", "remember_last_entry: yes", "", f"comment: machine-id={MACHINE_ID}"]
        # As limine-entry-tool: entries are found by their kernel-id comment and keep the name they have.
        old_blocks = {m.group(2): (m.group(1), m.group(0).rstrip("\n"))
                      for m in re.finditer(r"  //([^\n]+)\n(?:    (?!comment: kernel-id=).*\n)*"
                                           r"    comment: kernel-id=([^\n]+)\n(?:    .*\n)*", old)}
        lines = head + ["/+CachyOS", f"    comment: machine-id={MACHINE_ID}"]
        for kernel, version in self.kernels.items():
            for name in (kernel, f"{kernel}-fallback"):
                if kernel in self.limine_skip:
                    # Keep whatever the old file had for this kernel (process_kernel failed, exit 0).
                    if name in old_blocks:
                        lines += old_blocks[name][1].split("\n")
                    continue
                title = old_blocks.get(name, (name, ""))[0]
                initramfs = "initramfs-fallback" if name.endswith("-fallback") else "initramfs"
                for f in ("vmlinuz", initramfs):  # copied to the ESP
                    self.write(f"/boot/{MACHINE_ID}/{kernel}/{f}", "")
                empty = hashlib.blake2b(b"", digest_size=64).hexdigest()  # what limine-entry-tool appends: b2sum
                lines += [f"  //{title}", f"    comment: Kernel version: {version}", f"    comment: kernel-id={name}",
                          "    protocol: linux",
                          f"    module_path: boot():/{MACHINE_ID}/{kernel}/{initramfs}#{empty}",
                          f"    path: boot():/{MACHINE_ID}/{kernel}/vmlinuz#{empty}",
                          f"    cmdline: {self.limine_cmdline(name)}"]
        lines += ["  //Snapshots", "    ///1 2026-10-27", f"      path: boot():/{MACHINE_ID}/limine_history/vmlinuz#00",
                  "      cmdline: quiet rw root=UUID=abcd snapshot=1"]
        self.write("/boot/limine.conf", "\n".join(lines) + "\n")
        self.enroll()
        return CommandResult(self.limine_exit, "==> Building image\n", "")

    def enroll(self) -> None:
        """limine-enroll-config: with ENABLE_ENROLL_LIMINE_CONFIG=yes, limine.conf's BLAKE2b into the EFI binary."""
        if self.enroll_exit and self.enroll_strips:  # limine-enroll-config put back the binary without one
            p = self.root / LIMINE_EFI.lstrip("/")
            if p.is_file():
                p.write_bytes(b"MZ\0" + B2SUM_SIGNATURE + b"0" * 128 + b"\0")
        if self.enroll_exit or not re.search(r"^ENABLE_ENROLL_LIMINE_CONFIG=[\"']?yes", self.read("/etc/default/limine")
                                             or "", re.M):
            return
        digest = hashlib.blake2b((self.root / "boot/limine.conf").read_bytes(), digest_size=64).hexdigest()
        p = self.root / LIMINE_EFI.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"MZ\0" + B2SUM_SIGNATURE + digest.encode() + b"\0")

    # -- sdboot-manage / grub-mkconfig ----------------------------------------------

    @staticmethod
    def shell_script(argv: list[str]) -> tuple[str, str]:
        """(script, $1) of `bash|sh [...] -c SCRIPT NAME ARG`."""
        if "-c" not in argv:
            return "", ""
        i = argv.index("-c")
        return argv[i + 1] if len(argv) > i + 1 else "", argv[i + 3] if len(argv) > i + 3 else ""

    def shell_vars(self, files: list[str], skip: str = "") -> dict[str, str]:
        values: dict[str, str] = {}
        for path in files:
            if skip and path == skip:
                continue
            for line in (self.read(path) or "").splitlines():
                m = re.match(r"^\s*([A-Z_]+)(\+?)=(.*)$", line)
                if not m:
                    continue
                name, plus, value = m.group(1), m.group(2), _quoted(m.group(3))
                value = value.replace("${" + name + "}", values.get(name, "")).replace("$" + name, values.get(name, ""))
                values[name] = values.get(name, "") + value if plus else value
        return values

    def sdboot_gen(self) -> CommandResult:
        self.regenerations += 1
        if self.gen_fail:
            self.gen_fail -= 1
            return CommandResult(1, "", "Error: There are no boot loader entries after entry generation")
        values = self.shell_vars(["/etc/sdboot-manage.conf", *self.files("/usr/lib/sdboot-manage.conf.d/*.conf"),
                                  *self.files("/etc/sdboot-manage.conf.d/*.conf")])
        for kernel in self.kernels:
            self.write(f"/boot/loader/entries/{kernel}.conf",
                       f"title {kernel}\noptions root=UUID=abcd rw {values.get('LINUX_OPTIONS', '')}\n"
                       f"linux /vmlinuz-{kernel}\ninitrd /initramfs-{kernel}.img\n")
            fallback = values.get("LINUX_FALLBACK_OPTIONS", "")
            self.write(f"/boot/loader/entries/{kernel}-fallback.conf",
                       f"title {kernel} (Fallback)\noptions root=UUID=abcd rw {fallback}\n"
                       f"linux /vmlinuz-{kernel}\ninitrd /initramfs-{kernel}-fallback.img\n")
        return CommandResult(0)

    def grub_gen(self, output: str) -> CommandResult:
        self.regenerations += 1
        if self.gen_fail:
            self.gen_fail -= 1
            return CommandResult(1, "", "grub-mkconfig: syntax error")
        values = self.shell_vars(["/etc/default/grub", *self.files("/etc/default/grub.d/*.cfg")])
        args = f"{values.get('GRUB_CMDLINE_LINUX', '')} {values.get('GRUB_CMDLINE_LINUX_DEFAULT', '')}".strip()
        lines = ["### BEGIN /etc/grub.d/00_header ###", "set timeout=5", "### END /etc/grub.d/00_header ###",
                 "### BEGIN /etc/grub.d/10_linux ###"]
        for kernel in self.kernels:
            lines += [f"menuentry 'CachyOS ({kernel})' {{", f"\tlinux\t/vmlinuz-{kernel} root=UUID=abcd rw  {args}",
                      f"\tinitrd\t/initramfs-{kernel}.img", "}",
                      f"menuentry 'CachyOS ({kernel}, recovery)' {{",
                      f"\tlinux\t/vmlinuz-{kernel} root=UUID=abcd rw single", "}"]
        lines.append("### END /etc/grub.d/10_linux ###")
        self.write(output, "\n".join(lines) + "\n")
        return CommandResult(0)


class FakeCachyOS(FakeOxp3):
    """A ONEXPLAYER 3 running CachyOS (Handheld Edition by default), with one boot loader set up."""

    def __init__(self, root: Path, bootloader: str = "limine", kernels: Optional[dict[str, str]] = None,
                 limine_default: str = CALAMARES_LIMINE, loader_info: Optional[str] = "default",
                 handheld: bool = True, inputplumber: bool = True, steamos_manager: bool = False,
                 secure_boot: bool = False, gm7: bool = True):
        self.root = Path(root)
        self.runner = FakeCachyRunner(self.root, kernels or CACHY_KERNELS)
        self.bootloader = bootloader
        self.hangs: set[str] = set()
        self.build(inputplumber=inputplumber, gm7=gm7)
        self.cachyos(bootloader, limine_default, loader_info, handheld, inputplumber, steamos_manager, secure_boot)

    def cachyos(self, bootloader, limine_default, loader_info, handheld, inputplumber, steamos_manager,
                secure_boot) -> None:
        for path in ("/usr/share/ublue-os/image-info.json", "/usr/bin/rpm-ostree", "/usr/bin/rpm",
                     "/usr/bin/gamescope-session-plus"):
            (self.root / path.lstrip("/")).unlink(missing_ok=True)
        self.write("/etc/os-release", 'NAME="CachyOS Linux"\nPRETTY_NAME="CachyOS"\nID=cachyos\nID_LIKE=arch\n')
        self.write("/etc/pacman.conf", "[options]\nArchitecture = auto\n\n[cachyos-v3]\nInclude = x\n")
        self.write("/etc/machine-id", MACHINE_ID + "\n")
        self.write("/proc/sys/kernel/osrelease", next(iter(self.runner.kernels.values())) + "\n")
        self.write("/proc/self/mounts", "/dev/nvme0n1p2 / btrfs rw 0 0\n/dev/nvme0n1p1 /boot vfat rw 0 0\n")
        for tool in ("pacman", "bootctl", "bash", "sh"):
            self.write(f"/usr/bin/{tool}", "")
        for version in self.runner.kernels.values():
            self.write(f"/usr/lib/modules/{version}/modules.builtin", "")
        if not inputplumber:
            self.runner.packages.pop("inputplumber", None)
        if handheld:
            self.mkdir("/usr/share/cachyos-handheld")
            self.write("/usr/lib/steamos/gamescope-session", "#!/bin/bash\n")
            self.write("/usr/lib/systemd/user/gamescope-session.service", "[Service]\n")
            self.write("/usr/share/wayland-sessions/gamescope-session.desktop", "[Desktop Entry]\n")
        if steamos_manager:
            self.runner.unit_active["steamos-manager.service"] = "active"
        if secure_boot:
            self.write("/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c",
                       b"\x06\x00\x00\x00\x01")
        names = {"limine": "Limine 12.9.4", "systemd-boot": "systemd-boot 258.1-1", "grub": "GRUB 2.16",
                 "refind": "rEFInd 0.14.2"}
        info = names.get(bootloader) if loader_info == "default" else loader_info
        if info:
            self.write("/sys/firmware/efi/efivars/LoaderInfo-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f",
                       b"\x06\x00\x00\x00" + (info + "\0").encode("utf-16-le"))
        if bootloader == "limine":
            for tool in ("limine", "limine-entry-tool", "limine-mkinitcpio", "limine-enroll-config"):
                self.write(f"/usr/bin/{tool}", "")
            self.write("/etc/default/limine", limine_default)
            self.write("/etc/limine-entry-tool.conf", "### defaults\n#KERNEL_CMDLINE[default]=\n")
            self.mkdir("/etc/limine-entry-tool.d")
        elif bootloader == "systemd-boot":
            self.write("/usr/bin/sdboot-manage",
                       "for file in /usr/lib/sdboot-manage.conf.d/*.conf /etc/sdboot-manage.conf.d/*.conf; do\n")
            self.write("/etc/sdboot-manage.conf", SDBOOT_MANAGE_CONF)
            self.mkdir("/etc/sdboot-manage.conf.d")
            self.mkdir("/boot/loader/entries")
        elif bootloader == "grub":
            self.write("/usr/bin/grub-mkconfig", "for dropin in ${sysconfdir}/default/grub.d/*.cfg ; do\n")
            self.write("/etc/default/grub", DEFAULT_GRUB)
        elif bootloader == "refind":
            self.write("/boot/refind_linux.conf",
                       '"Boot with standard options"  "quiet rw root=UUID=abcd"\n')
        self.regenerate()
        self.boot()

    def regenerate(self) -> None:
        """What a kernel update's pacman hook does: rebuild the boot entries from the configuration."""
        if self.bootloader == "limine":
            self.runner.limine_regen()
        elif self.bootloader == "systemd-boot":
            self.runner.sdboot_gen()
        elif self.bootloader == "grub":
            self.runner.grub_gen("/boot/grub/grub.cfg")
        self.runner.regenerations = 0

    def entries(self) -> dict[str, list[str]]:
        """Boot entry name -> its kernel arguments, as the fake generators wrote them."""
        out: dict[str, list[str]] = {}
        if self.bootloader == "limine":
            name = None  # the entry's kernel-id when it has one (a renamed entry keeps it), else its title
            for line in (self.runner.read("/boot/limine.conf") or "").splitlines():
                if line.startswith("  //"):
                    name = line.strip()[2:]
                elif name and line.strip().startswith("comment: kernel-id="):
                    name = line.split("=", 1)[1].strip()
                elif name and line.strip().startswith("cmdline:"):
                    out[name] = line.split(":", 1)[1].split()
        elif self.bootloader == "systemd-boot":
            for path in self.runner.files("/boot/loader/entries/*.conf"):
                for line in (self.runner.read(path) or "").splitlines():
                    if line.startswith("options "):
                        out[path.rsplit("/", 1)[1][:-5]] = line.split()[1:]
        elif self.bootloader == "grub":
            for line in (self.runner.read("/boot/grub/grub.cfg") or "").splitlines():
                parts = line.split()
                if parts[:1] == ["linux"]:
                    out[parts[1] + (" (recovery)" if "single" in parts else "")] = parts[2:]
        return out

    def boot(self, kernel: Optional[str] = None) -> None:
        """Reboot into a kernel's entry (the first kernel's by default)."""
        entries = self.entries()
        kernel = kernel or next(iter(self.runner.kernels))
        args = entries.get(kernel) or entries.get(f"/vmlinuz-{kernel}") or self.runner.kargs
        self.write("/proc/cmdline", " ".join(args) + "\n")
        self.write("/proc/sys/kernel/osrelease", self.runner.kernels[kernel] + "\n")
        shutil.rmtree(self.root / "run", ignore_errors=True)  # a tmpfs

    def power_on(self) -> Optional[str]:
        """Limine 12.9 at power-on, for the limine.conf layout the fake writes: the one-shot entry (erased once
        read), else with remember_last_entry: yes the entry that booted last, else default_entry (an index among
        the visible entries, or a path). Boots it and returns its kernel-id; None when Limine waits in its menu.
        A kernel in ``self.hangs`` boots (Limine records it) but never reaches userspace."""
        conf = (self.root / "boot/limine.conf").read_bytes()
        efi = self.root / LIMINE_EFI.lstrip("/")
        if efi.is_file():
            data = efi.read_bytes()
            at = data.find(B2SUM_SIGNATURE)
            enrolled = data[at + len(B2SUM_SIGNATURE):at + len(B2SUM_SIGNATURE) + 128].decode() if at >= 0 else ""
            if enrolled.strip("0") and enrolled != hashlib.blake2b(conf, digest_size=64).hexdigest():
                raise RuntimeError("Limine: !!! CHECKSUM MISMATCH FOR CONFIG FILE !!!")
        lines = [ln.strip() for ln in conf.decode().splitlines()]

        def option(key: str) -> Optional[str]:
            for ln in lines:
                if ln.lower().startswith(key + ":"):
                    return ln.split(":", 1)[1].strip()
            return None
        visible: list[tuple[str, Optional[str]]] = []  # (path, kernel-id; None for a directory)
        top = sub = None
        for i, ln in enumerate(lines):
            if ln.startswith("///"):
                continue  # in a collapsed directory (Snapshots)
            if ln.startswith("//"):
                sub = ln[2:].lstrip("+").strip()
                is_dir = any(x.startswith("///") for x in lines[i + 1:i + 2])
                kid = next((x.split("=", 1)[1].strip() for x in lines[i + 1:i + 6] if x.startswith("comment: kernel-id=")),
                           sub)
                visible.append((f"{top}/{sub}", None if is_dir else kid))
            elif ln.startswith("/"):
                top = ln[1:].lstrip("+").strip()
                visible.append((top, None))

        def find(value: Optional[str]) -> Optional[tuple[str, Optional[str]]]:
            return next((v for v in visible if value in (v[0], v[0].replace("/", "."))), None) if value else None
        var = self.root / ONESHOT_VAR.lstrip("/")
        chosen = None
        if var.is_file():
            chosen = find(var.read_bytes()[4:].decode("utf-16-le").rstrip("\0"))
            var.unlink()
        last = self.root / LAST_BOOTED_VAR.lstrip("/")
        if chosen is None and (option("remember_last_entry") or "").lower() == "yes" and last.is_file():
            chosen = find(last.read_bytes()[4:].decode().rstrip("\0"))
        if chosen is None:
            default = option("default_entry") or "1"
            if default.isdigit():
                n = max(int(default) - 1, 0)
                chosen = visible[n] if n < len(visible) else visible[0]
            else:
                chosen = find(default)
            if chosen is None or chosen[1] is None or option("timeout") == "no":
                return None
        path, kid = chosen
        self.write(LAST_BOOTED_VAR, b"\x07\x00\x00\x00" + path.encode() + b"\0")
        self.write(SELECTED_VAR, b"\x06\x00\x00\x00" + (path.replace("/", ".") + "\0").encode("utf-16-le"))
        if kid in self.hangs:
            return kid
        self.boot(kid)
        return kid

    def pacman_lock(self, on: bool = True) -> None:
        if on:
            self.write("/var/lib/pacman/db.lck", "")
        else:
            (self.root / "var/lib/pacman/db.lck").unlink(missing_ok=True)
