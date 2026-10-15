"""Read-only detection of the device, the OS image and the software stack.

Everything is feature-detected at runtime. Nothing is gated on kernel, BIOS or
package version strings: Bazzite moves its kernel pin almost daily, and the
OXP3 has shipped with several BIOS versions and GPU PCI IDs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import cached_property
from typing import Optional

from .system import System

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
    def is_bazzite(self) -> bool:
        return self.os_release.get("ID") == "bazzite"

    @property
    def os_ok(self) -> bool:
        return self.is_bazzite or self.force

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
    def pending_kargs(self) -> Optional[list[str]]:
        """Kernel arguments of the next boot (the staged deployment if any)."""
        if not self.has_rpm_ostree:
            return None
        res = self.sys.run(["rpm-ostree", "kargs"], timeout=120)
        if not res.ok:
            return None
        return parse_kargs(res.stdout)

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
        for name in ("pending_kargs", "rpm_ostree_status"):
            self.__dict__.pop(name, None)

    def karg_pending(self, karg: str) -> bool:
        pending = self.pending_kargs
        return pending is not None and karg in pending

    def karg_active(self, karg: str) -> bool:
        return karg in self.active_kargs

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

        res = self.sys.run(["rpm", "-q", "--qf", "%{VERSION}", "inputplumber"], timeout=30)
        if res.ok and res.stdout.strip():
            info.version = res.stdout.strip()
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
