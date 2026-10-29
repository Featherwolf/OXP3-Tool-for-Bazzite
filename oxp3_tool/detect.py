"""Read-only detection of the device, the OS image and the software stack.

Everything is feature-detected at runtime. Nothing is gated on kernel, BIOS or
package version strings: Bazzite and CachyOS move their kernels almost daily,
and the OXP3 has shipped with several BIOS versions and GPU PCI IDs.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Optional

from .display import (
    DC_BALANCE_PARAM,
    DEBUGFS_DRI,
    DSB_OFF_KARG,
    KWIN_CONFIG,
    KWIN_CONFIG_MAX,
    PANEL_REPLAY_OFF_KARG,
    KwinOutput,
    RefreshState,
    count_dsb_errors,
    count_psr_errors,
    dmc_version_from_log,
    driver_start_logged,
    parse_crtc_mode,
    parse_dmc_info,
    parse_fixed_modes,
    parse_kwin_outputs,
    parse_psr_mode,
    safe_name,
    vbt_vswing_port,
    vbt_vswing_info,
)
from .system import System

if TYPE_CHECKING:
    from .kargs import KargBackend

OXP3_SYS_VENDOR = "ONE-NETBOOK"
OXP3_PRODUCT_NAME = "ONEXPLAYER 3"

# Panel fitted to the OXP3 (Samsung AMS881KB01-0, also used by the Legion Go 2).
OXP3_PANEL_VENDOR = "SDC"
OXP3_PANEL_PRODUCT = 0x4301

# Acer Predator GM7 SSD (Biwin/Maxio controller) that fails to resume from
# s2idle through the ACPI StorageD3Enable path on the OXP3.
GM7_PCI_ID = ("0x1dee", "0x1602")

# ACPI ID the firmware gives the BMI260 IMU.
IMU_I2C_DEVICE = "i2c-10EC5280:00"

DMI_FIELDS = (
    "sys_vendor",
    "product_name",
    "product_version",
    "board_vendor",
    "board_name",
    "board_version",
    "bios_vendor",
    "bios_version",
    "bios_date",
)

IP_FALLBACK_BASE = "/usr/share/inputplumber"
IP_XDG_DATA_DIRS = ("/usr/local/share", "/usr/share")
IP_ETC_DEVICES = "/etc/inputplumber/devices.d"
IP_ETC_CAPABILITY_MAPS = "/etc/inputplumber/capability_maps.d"

_PRODUCT_RE = re.compile(r"""^\s*product_name:\s*["']?ONEXPLAYER 3["']?\s*(#.*)?$""", re.M)
_MAP_ID_RE = re.compile(r"""^capability_map_id:\s*["']?([A-Za-z0-9_.-]+)["']?\s*(#.*)?$""", re.M)
_ID_RE = re.compile(r"""^id:\s*["']?([A-Za-z0-9_.-]+)["']?\s*(#.*)?$""", re.M)
STEAM_CONFIG_VDF = ".local/share/Steam/config/config.vdf"
STEAM_CONFIG_VDF_MAX = 16 * 1024 * 1024
_SCREEN_READER_RE = re.compile(rb'"ScreenReaderEnabled"\s+"1"')
# gamescope DRM backend: "Got known display: <key> (<pretty name>)"
_KNOWN_DISPLAY_RE = re.compile(r"Got known display: ([A-Za-z0-9_.-]+) \(")
POWERBUTTOND = "/usr/lib/hwsupport/steamos-powerbuttond"  # passes power-button presses to Steam in Game Mode
# Functions the 'Vswing / Pre-emphasis Override' series (Linux 7.4) adds to the display code (intel_bios.c).
_VSPEO_SYMBOL_RE = re.compile(rb"\s(?:intel_bios_get_c10_vspeo|intel_bios_get_c20_vspeo|"
                              rb"intel_bios_encoder_requests_vspeo)\s")
# InputPlumber 0.82 oxp_hid: "failed to parse B2 report: <err>: [b2 3f 01 01 1f 80 25 ...]" (byte 6 = button id)
_UNDECODED_RE = re.compile(r"failed to parse B2 report: .*?\[b2 3f(?: [0-9a-f]{2}){4} ([0-9a-f]{2})")


def parse_kargs(text: str) -> list[str]:
    return text.split()


def parse_ranges(text: str) -> set[int]:
    """Parse a kernel bitmap list such as ``174,176,369-370``."""
    out: set[int] = set()
    for part in text.replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            try:
                out.update(range(int(lo), int(hi) + 1))
            except ValueError:
                continue
        else:
            try:
                out.add(int(part))
            except ValueError:
                continue
    return out


@dataclass
class EdidInfo:
    vendor: str
    product: int
    name: Optional[str]


def parse_edid(data: bytes) -> Optional[EdidInfo]:
    if len(data) < 128 or data[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return None
    raw = (data[8] << 8) | data[9]
    vendor = "".join(chr(((raw >> shift) & 0x1F) + ord("A") - 1) for shift in (10, 5, 0))
    product = data[10] | (data[11] << 8)
    name = None
    for off in (54, 72, 90, 108):
        block = data[off:off + 18]
        if len(block) == 18 and block[0:3] == b"\x00\x00\x00" and block[3] == 0xFC:
            name = block[5:18].split(b"\x0a")[0].decode("ascii", errors="replace").strip()
            break
    return EdidInfo(vendor=vendor, product=product, name=name)


# Intel's eDP HDR backlight ("TCON") capability registers, as used by the
# kernel's intel_dp_aux_backlight.c.
DPCD_INTEL_HDR_TCON_CAP = 0x340
DPCD_EDP_REV = 0x700
TCON_BRIGHTNESS_NITS_CAP = 0x10
TCON_2084_DECODE_CAP = 0x01
TCON_SDR_BRIGHTNESS_AUX_CAP = 0x01  # in the third capability byte
EDP_14B = 0x05
# Intel's source OUI, which the kernel writes to the panel; the Intel registers
# read as zero without it (e.g. while the panel is powered off).
DPCD_SOURCE_OUI = 0x300
INTEL_SOURCE_OUI = b"\x00\xaa\x01"

# Game Mode on Bazzite's deck images runs Steam behind the OpenGamepadUI
# overlay; `ujust configure-opengamepadui disable` creates the per-user file
# below, which switches back to native Steam Input.
OGUI_SESSION = "/usr/share/gamescope-session-plus/sessions.d/ogui-steam"
BAZZITE_GAME_MODE = ("/usr/bin/gamescope-session-plus", "/usr/share/gamescope-session-plus/gamescope-session-plus")
# CachyOS's Handheld Edition runs Valve's SteamOS session (gamescope-session-cachyos), with
# native Steam Input: OpenGamepadUI is not packaged there.
CACHYOS_GAME_MODE = "/usr/lib/steamos/gamescope-session"
CACHYOS_GAME_MODE_UNITS = ("/usr/lib/systemd/user/gamescope-session.service",
                           "/usr/share/wayland-sessions/gamescope-session.desktop")
CACHYOS_HANDHELD = "/usr/share/cachyos-handheld"
BAZZITE = "bazzite"
CACHYOS = "cachyos"
OS_NAMES = {BAZZITE: "Bazzite", CACHYOS: "CachyOS"}
OGUI_USER_OVERRIDE = ".config/gamescope-session-plus/sessions.d/ogui-steam"
OGUI = "ogui"
NATIVE_STEAM = "steam"


@dataclass
class EdpCaps:
    aux_device: str
    edp_rev: int
    tcon: bytes

    @property
    def intel_hdr_backlight(self) -> bool:
        """The panel offers Intel's HDR backlight interface with nits-based brightness."""
        return self.tcon[0] >= 1 and bool(self.tcon[1] & TCON_BRIGHTNESS_NITS_CAP)

    @property
    def kernel_can_use_intel_hdr(self) -> bool:
        # The kernel only tries the Intel interface on eDP 1.4b and older.
        return self.intel_hdr_backlight and self.edp_rev <= EDP_14B

    @property
    def sdr_brightness_aux(self) -> bool:
        """With the Intel interface, SDR brightness also goes over AUX (else the SoC PWM)."""
        return bool(self.tcon[2] & TCON_SDR_BRIGHTNESS_AUX_CAP)

    def describe(self) -> str:
        revs = {0: "1.1", 1: "1.2", 2: "1.3", 3: "1.4", 4: "1.4a", 5: "1.4b", 6: "1.5"}
        rev = revs.get(self.edp_rev, hex(self.edp_rev))
        if self.intel_hdr_backlight:
            pq = ", PQ decode" if self.tcon[1] & TCON_2084_DECODE_CAP else ""
            sdr = "AUX" if self.sdr_brightness_aux else "PWM"
            hdr = f"Intel HDR backlight interface v{self.tcon[0]}{pq}, SDR brightness over {sdr}"
        else:
            hdr = "no Intel HDR backlight interface"
        return f"eDP {rev}, {hdr}"


@dataclass
class ResumeEvidence:
    nvme_reset_failure: bool = False  # this boot or the previous one
    unfinished_resume: bool = False  # previous boot: suspend logged, nothing after it
    simple_suspend: bool = False  # firmware asked for ACPI simple suspend on an NVMe drive

    def lines(self) -> list[str]:
        out = []
        if self.nvme_reset_failure:
            out.append("the kernel log shows the SSD failing to come back from sleep "
                       "('Disabling device after reset failure')")
        if self.unfinished_resume:
            out.append("the previous boot's saved log ends during a suspend: the device was powered off "
                       "while asleep or soon after waking, hung while waking, or the SSD dropped out")
        if self.simple_suspend:
            out.append("the firmware puts the SSD slot into ACPI 'simple suspend', the path the Predator GM7 "
                       "fails to wake from")
        return out


def _unfinished_resume(log: str) -> bool:
    entry = log.rfind("PM: suspend entry")
    return entry != -1 and log.find("PM: suspend exit", entry) == -1


@dataclass
class NvmeDrive:
    name: str
    vendor: Optional[str]
    device: Optional[str]
    model: Optional[str]
    firmware: Optional[str]

    @property
    def is_gm7(self) -> bool:
        return (self.vendor or "").lower() == GM7_PCI_ID[0] and (self.device or "").lower() == GM7_PCI_ID[1]


@dataclass
class InputPlumberInfo:
    installed: bool
    version: Optional[str] = None
    active: Optional[str] = None
    base_path: str = IP_FALLBACK_BASE
    oxp3_config: Optional[str] = None
    map_id: Optional[str] = None
    upstream_map: Optional[str] = None
    effective_map: Optional[str] = None
    problems: list[str] = field(default_factory=list)


@dataclass
class HhdInfo:
    binaries: list[str] = field(default_factory=list)
    active_units: list[str] = field(default_factory=list)

    @property
    def installed(self) -> bool:
        return bool(self.binaries or self.active_units)

    @property
    def running(self) -> bool:
        return bool(self.active_units)


class Context:
    """Lazily evaluated facts about the machine."""

    def __init__(self, system: System, force: bool = False):
        self.sys = system
        self.force = force

    # -- device ----------------------------------------------------------

    @cached_property
    def dmi(self) -> dict[str, Optional[str]]:
        return {f: self.sys.read_attr(f"/sys/class/dmi/id/{f}") for f in DMI_FIELDS}

    @cached_property
    def is_oxp3(self) -> bool:
        return (
            self.dmi.get("sys_vendor") == OXP3_SYS_VENDOR
            and self.dmi.get("product_name") == OXP3_PRODUCT_NAME
        )

    @property
    def device_ok(self) -> bool:
        return self.is_oxp3 or self.force

    # -- OS --------------------------------------------------------------

    @cached_property
    def os_release(self) -> dict[str, str]:
        out: dict[str, str] = {}
        text = self.sys.read_text("/etc/os-release") or self.sys.read_text("/usr/lib/os-release") or ""
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if not sep or not key or key.startswith("#"):
                continue
            out[key.strip()] = value.strip().strip('"').strip("'")
        return out

    @cached_property
    def platform(self) -> Optional[str]:
        """BAZZITE or CACHYOS, the systems this tool supports; None for anything else."""
        os_id = self.os_release.get("ID")
        if os_id == BAZZITE:
            return BAZZITE
        if os_id == CACHYOS:
            return CACHYOS
        if os_id == "arch" and re.search(r"^\[cachyos", self.sys.read_text("/etc/pacman.conf") or "", re.M):
            return CACHYOS  # CachyOS's repositories on a system whose os-release still says Arch
        return None

    @cached_property
    def is_bazzite(self) -> bool:
        return self.platform == BAZZITE

    @cached_property
    def is_cachyos(self) -> bool:
        return self.platform == CACHYOS

    @property
    def os_ok(self) -> bool:
        return self.platform is not None or self.force

    @cached_property
    def os_family(self) -> str:
        """The supported system whose tools this machine has: the platform, or a guess under --force."""
        if self.platform:
            return self.platform
        return CACHYOS if self.sys.exists("/usr/bin/pacman") and not self.has_rpm_ostree else BAZZITE

    @cached_property
    def steamos_manager(self) -> bool:
        """steamos-manager runs (CachyOS): it switches InputPlumber's controllers to the Steam Deck one."""
        return self.unit_state("steamos-manager.service") == "active"

    @cached_property
    def cachyos_handheld(self) -> bool:
        return self.is_cachyos and self.sys.is_dir(CACHYOS_HANDHELD)

    def package_version(self, name: str) -> Optional[str]:
        """Installed version of a package (without release or epoch), from rpm or pacman."""
        if self.os_family == CACHYOS:
            res = self.sys.run(["pacman", "-Q", name], timeout=30)
            parts = res.stdout.split()
            if not res.ok or len(parts) < 2:
                return None
            version = parts[-1].split(":", 1)[-1]
            return version.rsplit("-", 1)[0] if "-" in version else version
        res = self.sys.run(["rpm", "-q", "--qf", "%{VERSION}", name], timeout=30)
        return res.stdout.strip() if res.ok and res.stdout.strip() else None

    @cached_property
    def secure_boot(self) -> bool:
        from .kargs import SECURE_BOOT

        data = self.sys.read_bytes(SECURE_BOOT)
        return bool(data) and len(data) >= 5 and data[-1] == 1

    def vfat_mount(self, candidates: tuple[str, ...]) -> Optional[str]:
        """The first of ``candidates`` that is a vfat mount (an EFI system partition)."""
        mounts = {}
        for line in (self.sys.read_text("/proc/self/mounts") or "").splitlines():
            fields = line.split()
            if len(fields) >= 3:
                mounts[fields[1].replace("\\040", " ")] = fields[2]
        return next((c for c in candidates if mounts.get(c) == "vfat"), None)

    @cached_property
    def image_info(self) -> dict:
        text = self.sys.read_text("/usr/share/ublue-os/image-info.json")
        if not text:
            return {}
        try:
            data = json.loads(text)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    @cached_property
    def image_name(self) -> Optional[str]:
        name = self.image_info.get("image-name")
        return name if isinstance(name, str) else None

    @cached_property
    def boot_id(self) -> Optional[str]:
        return self.sys.read_attr("/proc/sys/kernel/random/boot_id")

    @cached_property
    def kernel_release(self) -> Optional[str]:
        return self.sys.read_attr("/proc/sys/kernel/osrelease")

    # -- kernel arguments ------------------------------------------------

    @cached_property
    def active_kargs(self) -> list[str]:
        return parse_kargs(self.sys.read_text("/proc/cmdline") or "")

    @cached_property
    def has_rpm_ostree(self) -> bool:
        return self.sys.which("rpm-ostree") is not None

    @cached_property
    def karg_backend(self) -> KargBackend:
        """How kernel arguments are read and changed here: rpm-ostree, or CachyOS's boot loader."""
        from .kargs import RpmOstreeBackend, detect_cachyos_backend

        if self.os_family == CACHYOS:
            return detect_cachyos_backend(self)
        return RpmOstreeBackend(self)

    @cached_property
    def pending_kargs(self) -> Optional[list[str]]:
        """Kernel arguments of the next boot (the staged deployment, or the regenerated boot entries)."""
        return self.karg_backend.next_boot()

    @cached_property
    def kargs_left(self) -> Optional[list[str]]:
        """Arguments some next-boot entry has beyond the user's own configuration (CachyOS: one kernel's entry
        may lag behind a rebuild); on Bazzite the next deployment's arguments."""
        found = self.karg_backend.left_in_entries()
        return self.pending_kargs if found is None else found

    @cached_property
    def owned_kargs(self) -> list[str]:
        """Arguments in this tool's own boot-loader drop-in (CachyOS)."""
        return self.karg_backend.owned()

    @cached_property
    def rpm_ostree_status(self) -> Optional[dict]:
        if not self.has_rpm_ostree:
            return None
        res = self.sys.run(["rpm-ostree", "status", "--json"], timeout=120)
        if not res.ok:
            return None
        try:
            data = json.loads(res.stdout)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    @property
    def rpm_ostree_busy(self) -> bool:
        status = self.rpm_ostree_status
        return bool(status and status.get("transaction"))

    @property
    def has_staged_deployment(self) -> bool:
        status = self.rpm_ostree_status or {}
        deployments = status.get("deployments") or []
        return bool(deployments) and isinstance(deployments[0], dict) and not deployments[0].get("booted", False)

    def invalidate_kargs(self) -> None:
        """Forget cached kernel-argument facts after changing them."""
        for name in ("pending_kargs", "kargs_left", "rpm_ostree_status", "owned_kargs"):
            self.__dict__.pop(name, None)
        if "karg_backend" in self.__dict__:
            self.karg_backend.invalidate()

    def karg_pending(self, karg: str) -> bool:
        pending = self.pending_kargs
        return pending is not None and karg in pending

    def karg_active(self, karg: str) -> bool:
        return karg in self.active_kargs

    def karg_left(self, karg: str) -> bool:
        """Whether a removal still has something to remove: a next-boot entry, or this tool's drop-in."""
        return karg in (self.kargs_left or []) or karg in self.owned_kargs

    def karg_user_set(self, karg: str) -> bool:
        """Whether the user's own boot configuration gives the running kernel this argument."""
        return karg in self.karg_backend.booted_user_args()

    def reboot_advice(self, what: str) -> str:
        """"(reboot to <what>)", unless a boot-entry rebuild has not finished: then not before it has."""
        finish = self.karg_backend.unfinished()
        if finish:
            return f"(do not reboot before `sudo {finish}` has ended without errors; then reboot to {what})"
        return f"(reboot to {what})"

    # -- systemd ---------------------------------------------------------

    def unit_state(self, unit: str) -> Optional[str]:
        res = self.sys.run(["systemctl", "is-active", unit], timeout=30)
        state = res.stdout.strip()
        return state or None

    def unit_enabled(self, unit: str) -> Optional[str]:
        res = self.sys.run(["systemctl", "is-enabled", unit], timeout=30)
        state = res.stdout.strip()
        return state or None

    def unit_file_exists(self, unit: str) -> bool:
        return any(
            self.sys.exists(f"{d}/{unit}")
            for d in ("/etc/systemd/system", "/usr/lib/systemd/system", "/usr/local/lib/systemd/system")
        )

    # -- InputPlumber ----------------------------------------------------

    @cached_property
    def inputplumber(self) -> InputPlumberInfo:
        installed = self.sys.which("inputplumber") is not None
        info = InputPlumberInfo(installed=installed)
        if not installed:
            return info

        info.version = self.package_version("inputplumber")
        info.active = self.unit_state("inputplumber.service")

        # InputPlumber takes the FIRST existing XDG data dir as its base path.
        for d in IP_XDG_DATA_DIRS:
            if self.sys.is_dir(f"{d}/inputplumber"):
                info.base_path = f"{d}/inputplumber"
                break
        if info.base_path != IP_FALLBACK_BASE:
            info.problems.append(
                f"{info.base_path} exists, so InputPlumber ignores {IP_FALLBACK_BASE} "
                "(all stock device configs). Remove that directory unless you created it on purpose."
            )

        device_dirs = [IP_ETC_DEVICES, f"{info.base_path}/devices"]
        for path in _sorted_multidir(self.sys, device_dirs):
            text = self.sys.read_text(path) or ""
            if _PRODUCT_RE.search(text):
                info.oxp3_config = path
                m = _MAP_ID_RE.search(text)
                info.map_id = m.group(1) if m else None
                break

        if info.map_id:
            map_dirs = [f"{info.base_path}/capability_maps", IP_ETC_CAPABILITY_MAPS]
            for path in _sorted_multidir(self.sys, map_dirs):
                m = _ID_RE.search(self.sys.read_text(path) or "")
                if m and m.group(1) == info.map_id:
                    # A HashMap keyed by id: the last file loaded wins.
                    info.effective_map = path
                    if path.startswith(info.base_path + "/"):
                        info.upstream_map = path

        for d in (IP_ETC_CAPABILITY_MAPS, f"{info.base_path}/capability_maps"):
            for name in self.sys.listdir(d):
                # Rust's Path::extension() is None for "foo" and ".foo".
                if "." not in name.lstrip("."):
                    info.problems.append(
                        f"{d}/{name} has no file extension; InputPlumber crashes "
                        "while loading capability maps. Move it elsewhere."
                    )
        return info

    # -- Handheld Daemon -------------------------------------------------

    @cached_property
    def hhd(self) -> HhdInfo:
        info = HhdInfo()
        candidates = ["/usr/bin/hhd", "/usr/local/bin/hhd"]
        for home_root in ("/home", "/var/home"):
            candidates += self.sys.glob(f"{home_root}/*/.local/bin/hhd")
        info.binaries = sorted({p for p in candidates if self.sys.exists(p)})
        res = self.sys.run(
            ["systemctl", "list-units", "--all", "--plain", "--no-legend", "--no-pager", "--type=service", "hhd*"],
            timeout=30,
        )
        for line in res.stdout.splitlines():
            cols = line.split()
            if len(cols) >= 3 and cols[0].startswith("hhd") and cols[2] in ("active", "activating", "reloading"):
                info.active_units.append(cols[0])
        return info

    # -- display ---------------------------------------------------------

    @cached_property
    def gamescope_installed(self) -> bool:
        return self.sys.which("gamescope") is not None

    @cached_property
    def logind_power_key(self) -> Optional[str]:
        """HandlePowerKey= as logind reads it: logind.conf, then the drop-ins in file-name order (a name in /etc
        hides the same name in /run and /usr), the last setting winning."""
        files = [p for p in ("/etc/systemd/logind.conf", "/usr/lib/systemd/logind.conf") if self.sys.exists(p)][:1]
        dropins: dict[str, str] = {}
        for d in ("/usr/lib/systemd/logind.conf.d", "/usr/local/lib/systemd/logind.conf.d", "/run/systemd/logind.conf.d",
                  "/etc/systemd/logind.conf.d"):  # lowest priority first
            for path in self.sys.glob(f"{d}/*.conf"):
                dropins[path.rsplit("/", 1)[1]] = path
        value = None
        for path in files + [dropins[name] for name in sorted(dropins)]:
            section = ""
            for line in (self.sys.read_text(path) or "").splitlines():
                line = line.strip()
                if line.startswith("["):
                    section = line
                elif section == "[Login]" and line.startswith("HandlePowerKey="):
                    value = line.split("=", 1)[1].strip()
        return value

    @property
    def power_key_unhandled(self) -> bool:
        """Game Mode on CachyOS: logind ignores the power key and nothing passes it to Steam (CachyOS's hardware
        detection installs steamos-powerbuttond only on the handhelds it knows)."""
        return (self.os_family == CACHYOS and self.has_game_mode and self.logind_power_key == "ignore"
                and not self.sys.exists(POWERBUTTOND) and not self.hhd.running)

    @cached_property
    def has_game_mode(self) -> bool:
        """A gamescope-session (Game Mode) exists; desktop images only run gamescope nested."""
        if self.os_family == CACHYOS:
            return self.sys.exists(CACHYOS_GAME_MODE) or all(self.sys.exists(p) for p in CACHYOS_GAME_MODE_UNITS)
        return any(self.sys.exists(p) for p in BAZZITE_GAME_MODE)

    @cached_property
    def internal_panel(self) -> Optional[tuple[str, Optional[EdidInfo]]]:
        """(connector, parsed EDID) of the first eDP connector found."""
        for conn in self.sys.glob("/sys/class/drm/card*-eDP-*"):
            data = self.sys.read_bytes(f"{conn}/edid")
            name = conn.rsplit("/", 1)[-1]
            return name, parse_edid(data) if data else None
        return None

    @property
    def panel_is_known(self) -> Optional[bool]:
        """True/False when the eDP EDID could be read, None when unknown."""
        panel = self.internal_panel
        if not panel or not panel[1]:
            return None
        edid = panel[1]
        return edid.vendor == OXP3_PANEL_VENDOR and edid.product == OXP3_PANEL_PRODUCT

    @cached_property
    def xe_loaded(self) -> bool:
        return self.sys.is_dir("/sys/module/xe")

    # -- storage ---------------------------------------------------------

    @cached_property
    def nvme_drives(self) -> list[NvmeDrive]:
        drives = []
        for d in self.sys.glob("/sys/class/nvme/nvme*"):
            name = d.rsplit("/", 1)[-1]
            if not re.fullmatch(r"nvme\d+", name):
                continue
            drives.append(
                NvmeDrive(
                    name=name,
                    vendor=self.sys.read_attr(f"{d}/device/vendor"),
                    device=self.sys.read_attr(f"{d}/device/device"),
                    model=self.sys.read_attr(f"{d}/model"),
                    firmware=self.sys.read_attr(f"{d}/firmware_rev"),
                )
            )
        return drives

    @property
    def has_gm7(self) -> bool:
        return any(d.is_gm7 for d in self.nvme_drives)

    # -- kernel logs -----------------------------------------------------

    def kernel_log(self, boot: int) -> str:
        """Kernel messages of a boot (0 = this one, -1 = the previous one); empty if unavailable."""
        cache = self.__dict__.setdefault("_kernel_logs", {})
        if boot not in cache:
            res = self.sys.run(["journalctl", "-k", "-b", str(boot), "--no-pager", "-o", "short-monotonic"],
                               timeout=120)
            cache[boot] = res.stdout if res.ok else ""
        return cache[boot]

    @cached_property
    def resume_evidence(self) -> ResumeEvidence:
        now, prev = self.kernel_log(0), self.kernel_log(-1)
        return ResumeEvidence(
            nvme_reset_failure="Disabling device after reset failure" in now + prev,
            unfinished_resume=_unfinished_resume(prev),
            simple_suspend="platform quirk: setting simple suspend" in now,
        )

    @cached_property
    def edp_probe(self) -> tuple[Optional[EdpCaps], str]:
        """Read-only DPCD probe of the internal panel: (caps, why not) - needs root and a lit panel.

        The Intel registers only read correctly while the panel is powered and
        carries Intel's source OUI, so anything else is reported as unknown,
        never as "no interface".
        """
        conns = self.sys.glob("/sys/class/drm/card*-eDP-*")
        if not conns:
            return None, "no internal eDP panel found"
        for conn in conns:
            if (self.sys.read_attr(f"{conn}/dpms") or "On") != "On":
                return None, "the panel is off; check again while the screen is on"
            for aux in self.sys.glob(f"{conn}/drm_dp_aux*"):
                dev = "/dev/" + aux.rsplit("/", 1)[-1]
                if not self.sys.exists(dev) or not os.access(self.sys.path(dev), os.R_OK):
                    return None, "cannot read the panel's DPCD (needs root)"
                oui = self.sys.read_at(dev, DPCD_SOURCE_OUI, 3)
                tcon = self.sys.read_at(dev, DPCD_INTEL_HDR_TCON_CAP, 4)
                rev = self.sys.read_at(dev, DPCD_EDP_REV, 1)
                if oui is None or tcon is None or rev is None:
                    return None, f"reading the panel's DPCD through {dev} failed"
                if oui != INTEL_SOURCE_OUI:
                    return None, (f"the panel does not carry Intel's source OUI right now (reads {oui.hex()}); "
                                  "check again later")
                return EdpCaps(dev, rev[0], tcon), ""
        return None, "no DP AUX device for the eDP panel (/dev/drm_dp_aux*)"

    @property
    def edp_caps(self) -> Optional[EdpCaps]:
        return self.edp_probe[0]

    @cached_property
    def refresh_state(self) -> RefreshState:
        """Read-only refresh-rate picture: display mode, PSR, display firmware, KWin settings."""
        st = RefreshState()
        st.needs_root = self.sys.is_real_root and os.geteuid() != 0
        m = re.match(r"(\d+)\.(\d+)", self.kernel_release or "")
        st.kernel_version = (int(m.group(1)), int(m.group(2))) if m else None
        if self.os_family == CACHYOS:
            st.remove_karg_hint = self.karg_backend.remove_hint("xe.enable_dc_balance=1")
            st.kernel_update_hint = "until you update to kernel 7.2.10 or later (`sudo pacman -Syu`, then reboot)"
            st.vswing_hint = ("on CachyOS, linux-cachyos-rc has it from 7.4-rc1 on (`pacman -Si linux-cachyos-rc`; it "
                              "installs next to your kernel, and `sudo oxp3-tool kernel try linux-cachyos-rc` boots it "
                              "once), or build linux-cachyos-deckify-vsw with tools/cachyos-vswing-kernel.sh (README)")
        st.game_mode = self.has_game_mode
        panel = self.internal_panel
        st.connector = re.sub(r"^card\d+-", "", panel[0]) if panel else None
        debugfs_read = False
        vbt_ports: list[str] = []
        vbt_version, vbt_tables = 0, False
        if st.connector:
            for info in self.sys.glob(f"{DEBUGFS_DRI}/*/i915_display_info"):
                text = self.sys.read_text(info)
                if not text or f":{st.connector}]" not in text:
                    continue
                debugfs_read = True
                d = info.rsplit("/", 1)[0]
                st.mode = parse_crtc_mode(text, st.connector)
                st.fixed_modes = parse_fixed_modes(text, st.connector)
                psr = self.sys.read_text(f"{d}/{st.connector}/i915_psr_status")
                if psr is None:
                    psr = self.sys.read_text(f"{d}/i915_edp_psr_status")
                st.psr_mode = parse_psr_mode(psr) if psr else None
                st.dmc_loaded, st.dmc_version = parse_dmc_info(self.sys.read_text(f"{d}/i915_dmc_info") or "")
                vbt_ports, vbt_version, vbt_tables = vbt_vswing_info(self.sys.read_bytes(f"{d}/i915_vbt") or b"")
                if st.mode is None:
                    st.debugfs_why = f"no active display pipe drives {st.connector} (screen off?)"
                break
            else:
                if st.needs_root:
                    st.debugfs_why = "needs root"
                else:
                    st.debugfs_why = f"no readable i915_display_info for {st.connector} under {DEBUGFS_DRI}"
        if st.dmc_version is None and not (debugfs_read and st.dmc_loaded is not None):
            device = None
            if panel:
                pci = self.sys.realpath(f"/sys/class/drm/{panel[0].split('-', 1)[0]}/device")
                device = pci.rsplit("/", 1)[-1] if pci else None
            st.dmc_version = dmc_version_from_log(self.kernel_log(0), device)
        if self.xe_loaded:
            st.dc_balance_param = self.sys.exists(DC_BALANCE_PARAM)
            # 0400, so only root sees the value (the default is off); without root
            # fall back to the kernel command line, where a bare bool parameter means on.
            value = self.sys.read_attr(DC_BALANCE_PARAM)
            if value is None:
                for k in self.active_kargs:
                    key, _, v = k.partition("=")
                    if key.replace("-", "_") == "xe.enable_dc_balance":
                        value = v or "Y"
            value = (value or "").strip()
            st.dc_balance_on = value[:1] in ("Y", "y", "1", "T", "t") or value.lower() == "on"
        log = self.kernel_log(0)
        st.dsb_errors = count_dsb_errors(log)
        st.psr_errors = count_psr_errors(log)
        st.vbt_vswing_port = vbt_vswing_port(log)
        if st.vbt_vswing_port is None and vbt_ports:
            if (st.kernel_version or (0, 0)) >= (7, 4) or self.display_has_vspeo:
                st.vspeo = "tables" if vbt_version >= 218 and vbt_tables else "none"
            elif not driver_start_logged(log):
                # A log flood (DSB errors) can push the boot-time warning out; the VBT itself still says it.
                st.vbt_vswing_port, st.vbt_vswing_from_table = vbt_ports[0], True
        st.awaiting_reboot = frozenset(k for k in (DSB_OFF_KARG, PANEL_REPLAY_OFF_KARG)
                                       if self.karg_pending(k) and not self.karg_active(k))
        if st.awaiting_reboot:
            st.reboot_advice = self.reboot_advice("activate it")
        st.panel_replay_blocked = any(self.karg_active(k) or self.karg_pending(k)
                                      for k in ("xe.enable_psr=0", "xe.enable_psr2_sel_fetch=0"))
        st.kwin, st.kwin_is_mine = self._kwin_outputs(panel[1] if panel else None)
        return st

    @cached_property
    def display_has_vspeo(self) -> bool:
        """The loaded display driver has the 'Vswing / Pre-emphasis Override' series (Linux 7.4, or a backport such
        as linux-cachyos-deckify-vsw): functions it adds to xe.ko and i915.ko, called across files, so in kallsyms."""
        return _VSPEO_SYMBOL_RE.search(self.sys.read_bytes("/proc/kallsyms") or b"") is not None

    @property
    def show_refresh(self) -> bool:
        """The refresh checks are written for the OXP3 panel on xe; other panels are skipped."""
        return bool(self.xe_loaded or self.refresh_state.kwin) and self.panel_is_known is not False

    def _kwin_outputs(self, edid: Optional[EdidInfo]) -> tuple[list[KwinOutput], bool]:
        """KWin's saved settings for the panel: only the invoking user's when they have any
        (then True), otherwise every user's."""
        vendor, product = (edid.vendor, edid.product) if edid else (OXP3_PANEL_VENDOR, OXP3_PANEL_PRODUCT)
        try:
            me = int(os.environ.get("SUDO_UID") or os.environ.get("PKEXEC_UID") or os.getuid())
        except ValueError:
            me = os.getuid()
        by_user: dict[str, list[KwinOutput]] = {}
        mine: list[str] = []
        for home_root in ("/home", "/var/home"):
            for path in self.sys.glob(f"{home_root}/*/{KWIN_CONFIG}"):
                user = safe_name(path.split("/")[-3])
                if user in by_user:
                    continue
                data = self.sys.read_regular(path, KWIN_CONFIG_MAX)
                by_user[user] = parse_kwin_outputs(data, vendor, product, user) if data else []
                try:
                    if os.stat(self.sys.path(path.rsplit("/", 2)[0])).st_uid == me:
                        mine.append(user)
                except OSError:
                    pass
        users = [u for u in mine if by_user[u]]
        return [out for u in (users or by_user) for out in by_user[u]], bool(users)

    @cached_property
    def gamescope_known_display(self) -> Optional[str]:
        """The gamescope display profile Game Mode used this boot ("Got known display: <name>"), if logged."""
        # -r -n 1: only the newest match (journalctl stops at the first one).
        res = self.sys.run(["journalctl", "-b", "0", "-r", "-n", "1", "--no-pager", "-q", "-o", "cat",
                            "--grep", "Got known display"], timeout=60)
        found = _KNOWN_DISPLAY_RE.findall(res.stdout)
        return found[0] if found else None

    @cached_property
    def steam_screen_reader_users(self) -> list[str]:
        """Users whose Steam has its screen reader on: it replaces the Steam button
        chords (Steam+X keyboard, Steam+R1 screenshot) with screen-reader controls."""
        users = []
        for home_root in ("/home", "/var/home"):
            for path in self.sys.glob(f"{home_root}/*/{STEAM_CONFIG_VDF}"):
                user = safe_name(path.split("/")[-6])
                if user in users:
                    continue
                data = self.sys.read_regular(path, STEAM_CONFIG_VDF_MAX)
                if data and _SCREEN_READER_RE.search(data):
                    users.append(user)
        return users

    # -- Game Mode -------------------------------------------------------

    @cached_property
    def game_mode_input(self) -> Optional[str]:
        """OGUI when Game Mode runs Steam behind the OpenGamepadUI overlay, NATIVE_STEAM
        when it uses native Steam Input, None without a Game Mode session."""
        if not self.has_game_mode:
            return None
        if self.os_family == CACHYOS:
            return NATIVE_STEAM
        text = self.sys.read_text(OGUI_SESSION) or ""
        if "--overlay-mode" not in text:
            return NATIVE_STEAM
        if self.ogui_disabled_for:
            return NATIVE_STEAM
        return OGUI

    @cached_property
    def ogui_disabled_for(self) -> list[str]:
        """Users who ran `ujust configure-opengamepadui disable`."""
        users = set()
        for root in ("/home", "/var/home"):
            for p in self.sys.glob(f"{root}/*/{OGUI_USER_OVERRIDE}"):
                users.add(p.split("/")[-5])
        return sorted(users)

    # -- input hardware --------------------------------------------------

    @cached_property
    def iio_names(self) -> list[str]:
        names = []
        for p in self.sys.glob("/sys/bus/iio/devices/iio:device*/name"):
            v = self.sys.read_attr(p)
            if v:
                names.append(v)
        return names

    @cached_property
    def imu_present(self) -> bool:
        return self.sys.exists(f"/sys/bus/i2c/devices/{IMU_I2C_DEVICE}")

    @cached_property
    def imu_driver(self) -> Optional[str]:
        target = self.sys.realpath(f"/sys/bus/i2c/devices/{IMU_I2C_DEVICE}/driver")
        return target.rsplit("/", 1)[-1] if target else None

    def module_has_alias(self, module: str, needle: str) -> Optional[bool]:
        res = self.sys.run(["modinfo", "-F", "alias", module], timeout=30)
        if not res.ok:
            return None
        return any(needle.lower() in a.lower() for a in res.stdout.split())

    @cached_property
    def atkbd_force_release(self) -> Optional[set[int]]:
        for serio in self.sys.glob("/sys/bus/serio/devices/serio*"):
            driver = self.sys.realpath(f"{serio}/driver")
            if driver and driver.rsplit("/", 1)[-1] == "atkbd":
                text = self.sys.read_attr(f"{serio}/force_release")
                if text is not None:
                    return parse_ranges(text)
        return None

    @cached_property
    def oxp_vendor_hidraw(self) -> Optional[str]:
        """/dev node of the controller MCU's vendor interface (1a86:fe00 interface 2)."""
        from .xbox_helper import find_hidraw

        found = find_hidraw(str(self.sys.root))
        if found is None:
            return None
        return "/" + os.path.relpath(found, str(self.sys.root))

    @cached_property
    def ip_undecoded_buttons(self) -> Optional[set[int]]:
        """Vendor button ids InputPlumber logged this boot as undecodable; None if the log is unreadable."""
        res = self.sys.run(["journalctl", "-u", "inputplumber.service", "-b", "0", "--no-pager", "-o", "cat",
                            "--grep", "failed to parse B2 report", "-n", "500"], timeout=60)
        found = {int(m.group(1), 16) for m in _UNDECODED_RE.finditer(res.stdout)}
        if found or res.returncode in (0, 1):
            return found
        return None

    @cached_property
    def hid_oxp_devices(self) -> list[str]:
        return [n for n in self.sys.listdir("/sys/bus/hid/drivers/hid-oxp") if ":" in n]


def _sorted_multidir(system: System, dirs: list[str]) -> list[str]:
    """Mimic InputPlumber's get_multidir_sorted_files() for *.yaml files."""
    entries: list[tuple[str, int, str]] = []
    for prio, d in enumerate(dirs):
        for name in system.listdir(d):
            if name.endswith(".yaml") and system.is_file(f"{d}/{name}"):
                entries.append((name, prio, f"{d}/{name}"))
    entries.sort()
    return [p for _, _, p in entries]
