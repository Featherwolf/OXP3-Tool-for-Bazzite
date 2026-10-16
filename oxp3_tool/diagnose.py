"""Collect a diagnostic bundle for bug reports."""

from __future__ import annotations

import datetime
import io
import os
import re
import tarfile
from typing import Optional

from .detect import IMU_I2C_DEVICE, Context, parse_edid
from .state import STATE_FILE

# Kernel log patterns worth calling out, with what they usually mean.
PATTERNS: list[tuple[str, str]] = [
    (r"Disabling device after reset failure",
     "The NVMe SSD did not come back from sleep (black or frozen screen after resume). "
     "The nvme-resume fix addresses this."),
    (r"platform quirk: setting simple suspend",
     "Firmware asks for ACPI simple suspend on the SSD; this is what breaks resume with the Predator GM7."),
    (r"FIFO underrun", "Display pipe FIFO underruns (xe)."),
    (r"DSB \d+ poll error", "xe Display State Buffer errors; xe-dsb-off silences them."),
    (r"flip_done timed out", "A display update timed out; often seen together with a black screen."),
    (r"Timed out waiting PSR idle state", "Panel Self Refresh did not idle; xe-panel-replay-off may help."),
    (r"Selective fetch area calculation failed", "PSR2/Panel Replay selective-fetch failure."),
    (r"Failed to write aux backlight level", "Backlight control over the DisplayPort AUX channel failed."),
    (r"Chip id not found", "bmi160 probed the BMI260 IMU and failed (gyro); see the gyro fix."),
    (r"DMC \d+ has \d+ handlers for event",
     "The display firmware lacks an event handler the kernel expects (e.g. an older DMC with variable refresh on)."),
    (r"Panel is missing HDR static metadata",
     "The kernel did not enable the panel's Intel HDR backlight interface; see the xe-hdr-backlight fix."),
]

DMI_PUBLIC = (
    "sys_vendor", "product_name", "product_version", "product_family", "product_sku",
    "board_vendor", "board_name", "board_version", "bios_vendor", "bios_version",
    "bios_date", "bios_release", "ec_firmware_release", "chassis_type",
)

COMMANDS: list[tuple[str, list[str]]] = [
    ("uname.txt", ["uname", "-a"]),
    ("rpm-ostree-status.txt", ["rpm-ostree", "status"]),
    ("rpm-ostree-kargs.txt", ["rpm-ostree", "kargs"]),
    ("packages.txt", ["rpm", "-q", "kernel", "inputplumber", "gamescope", "gamescope-session-plus",
                      "systemd", "linux-firmware", "steamos-manager", "powerstation", "hhd"]),
    ("lspci.txt", ["lspci", "-nnk"]),
    ("lsusb.txt", ["lsusb"]),
    ("units.txt", ["systemctl", "status", "--no-pager", "--lines=20", "inputplumber.service",
                   "inputplumber-suspend.service", "powerstation.service", "steamos-manager.service",
                   "bazzite-hardware-setup.service", "oxp3-tool-xbox-key.service", "oxp3-tool-osk-key.service"]),
    ("units-failed.txt", ["systemctl", "--failed", "--no-pager"]),
    ("journal-kernel-this-boot.txt", ["journalctl", "-k", "-b", "0", "--no-pager", "-o", "short-monotonic"]),
    ("journal-kernel-previous-boot.txt", ["journalctl", "-k", "-b", "-1", "--no-pager", "-o", "short-monotonic"]),
    ("journal-sleep-this-boot.txt", ["journalctl", "-b", "0", "--no-pager", "-o", "short-monotonic",
                                     "-u", "systemd-suspend.service", "-u", "systemd-logind.service",
                                     "-u", "inputplumber-suspend.service"]),
    ("journal-inputplumber-this-boot.txt", ["journalctl", "-b", "0", "--no-pager", "-o", "short-monotonic",
                                            "-u", "inputplumber.service", "-n", "3000"]),
    ("journal-oxp3-helpers-this-boot.txt", ["journalctl", "-b", "0", "--no-pager", "-o", "short-monotonic",
                                            "-u", "oxp3-tool-xbox-key.service", "-u", "oxp3-tool-osk-key.service",
                                            "-n", "500"]),
    ("journal-gamescope-this-boot.txt", ["journalctl", "-b", "0", "--no-pager", "-o", "short-monotonic",
                                         "--grep", "gamescope|known display", "-n", "3000"]),
]

TEXT_FILES = [
    "/proc/cmdline",
    "/etc/os-release",
    "/usr/share/ublue-os/image-info.json",
    "/proc/bus/input/devices",
    "/sys/power/mem_sleep",
    "/sys/kernel/security/lockdown",
    "/sys/module/nvme/parameters/noacpi",
]

DEBUGFS_GLOBS = [
    "/sys/kernel/debug/dri/*/i915_edp_psr_status",
    "/sys/kernel/debug/dri/*/i915_display_info",
    "/sys/kernel/debug/dri/*/i915_dmc_info",
    "/sys/kernel/debug/dri/*/eDP-*/i915_psr_status",
    "/sys/kernel/debug/dri/*/eDP-*/vrr_range",
    "/sys/kernel/debug/pmc_core/substate_residencies",
    "/sys/kernel/debug/pmc_core/slp_s0_residency_usec",
]

MAX_FILE = 4 * 1024 * 1024


_SERIAL_RE = re.compile(r"(SerialNumber:\s*)\S.*")
_UNIQ_RE = re.compile(r"^(U: Uniq=)\S.*$", re.M)
_MAC_RE = re.compile(r"(?i)(?<![0-9a-f:])[0-9a-f]{2}(?::[0-9a-f]{2}){5}(?![0-9a-f:])")


class Redactor:
    """Removes USB serial numbers, input-device unique IDs and MAC addresses.

    MAC addresses become stable per-archive tags (mac-1, mac-2, ...) so lines
    stay comparable without exposing the address.
    """

    def __init__(self) -> None:
        self.macs: dict[str, str] = {}

    def _mac(self, m: re.Match) -> str:
        key = m.group(0).lower()
        if key == "00:00:00:00:00:00" or key == "ff:ff:ff:ff:ff:ff":
            return m.group(0)
        if key not in self.macs:
            self.macs[key] = f"mac-{len(self.macs) + 1}"
        return self.macs[key]

    def __call__(self, text: str) -> str:
        text = _SERIAL_RE.sub(r"\1<redacted>", text)
        text = _UNIQ_RE.sub(r"\1<redacted>", text)
        return _MAC_RE.sub(self._mac, text)


class Bundle:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.redact = Redactor()

    def add(self, name: str, data: bytes | str) -> None:
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        self.files[name] = self.redact(data).encode()[:MAX_FILE]

    def write(self, fileobj) -> None:
        stamp = int(datetime.datetime.now().timestamp())
        with tarfile.open(fileobj=fileobj, mode="w:gz") as tar:
            for name, data in sorted(self.files.items()):
                info = tarfile.TarInfo(f"oxp3-diagnose/{name}")
                info.size = len(data)
                info.mtime = stamp
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))


def scan_patterns(text: str) -> list[tuple[str, int, str]]:
    hits = []
    for pattern, meaning in PATTERNS:
        count = len(re.findall(pattern, text))
        if count:
            hits.append((pattern, count, meaning))
    return hits


def collect(ctx: Context, status_text: str) -> tuple[Bundle, list[str]]:
    sys = ctx.sys
    bundle = Bundle()
    bundle.add("status.txt", status_text)
    state = sys.read_bytes(STATE_FILE)
    if state is not None:
        bundle.add("files" + STATE_FILE, state)

    dmi_lines = []
    for f in DMI_PUBLIC:
        v = sys.read_attr(f"/sys/class/dmi/id/{f}")
        if v is not None:
            dmi_lines.append(f"{f}: {v}")
    bundle.add("dmi.txt", "\n".join(dmi_lines) + "\n")

    for path in TEXT_FILES:
        data = sys.read_bytes(path)
        if data is not None:
            bundle.add("files" + path, data)

    stats = []
    for p in sys.glob("/sys/power/suspend_stats/*"):
        stats.append(f"{p.rsplit('/', 1)[-1]}: {sys.read_attr(p)}")
    if stats:
        bundle.add("suspend_stats.txt", "\n".join(stats) + "\n")

    for name, argv in COMMANDS:
        res = sys.run(argv, timeout=120)
        bundle.add(f"commands/{name}", f"$ {' '.join(argv)}\n(exit {res.returncode})\n{res.stdout}{res.stderr}")

    debug_lines = []
    for pattern in DEBUGFS_GLOBS:
        for p in sys.glob(pattern):
            data = sys.read_bytes(p)
            if data is not None:
                bundle.add("debugfs" + p.replace("/sys/kernel/debug", ""), data)
            else:
                debug_lines.append(f"unreadable: {p}")
    if debug_lines:
        bundle.add("debugfs/README.txt", "\n".join(debug_lines) + "\n")

    hw = []
    for conn in sys.glob("/sys/class/drm/card*-*"):
        name = conn.rsplit("/", 1)[-1]
        if not re.match(r"card\d+-", name):
            continue
        status = sys.read_attr(f"{conn}/status")
        enabled = sys.read_attr(f"{conn}/enabled")
        dpms = sys.read_attr(f"{conn}/dpms")
        edid_bytes = sys.read_bytes(f"{conn}/edid")
        edid = parse_edid(edid_bytes) if edid_bytes else None
        edid_s = f"{edid.vendor} 0x{edid.product:04x} {edid.name or ''}".strip() if edid else "-"
        hw.append(f"drm {name}: status={status} enabled={enabled} dpms={dpms} edid={edid_s}")
    for bl in sys.glob("/sys/class/backlight/*"):
        vals = {a: sys.read_attr(f"{bl}/{a}") for a in ("type", "brightness", "actual_brightness", "max_brightness")}
        hw.append(f"backlight {bl.rsplit('/', 1)[-1]}: " + " ".join(f"{k}={v}" for k, v in vals.items()))
    for d in ctx.nvme_drives:
        hw.append(f"nvme {d.name}: {d.vendor}:{d.device} model={d.model} fw={d.firmware}")
    hw.append(f"iio devices: {', '.join(ctx.iio_names) or 'none'}")
    hw.append(f"{IMU_I2C_DEVICE}: present={ctx.imu_present} driver={ctx.imu_driver}")
    hw.append(f"hid-oxp devices: {', '.join(ctx.hid_oxp_devices) or 'none'}")
    caps, why = ctx.edp_probe
    hw.append(f"eDP DPCD: {caps.aux_device}: {caps.describe()} (tcon caps {caps.tcon.hex()}, edp rev {caps.edp_rev})"
              if caps else f"eDP DPCD: not checked: {why}")
    for note in ctx.resume_evidence.lines():
        hw.append(f"sleep: {note}")
    released = ctx.atkbd_force_release
    hw.append(f"atkbd force_release: {sorted(released) if released is not None else 'unreadable'}")
    if ctx.show_refresh:
        for line in ctx.refresh_state.summary():
            hw.append(f"refresh: {line}")
    for param in ("enable_psr", "enable_panel_replay", "enable_psr2_sel_fetch", "enable_dsb", "enable_dc",
                  "enable_dc_balance"):
        v = sys.read_attr(f"/sys/module/xe/parameters/{param}")
        if v is not None:
            hw.append(f"xe.{param} = {v}")
    bundle.add("hardware.txt", "\n".join(hw) + "\n")

    kernel_logs = ""
    for name in ("journal-kernel-this-boot.txt", "journal-kernel-previous-boot.txt"):
        kernel_logs += bundle.files.get(f"commands/{name}", b"").decode(errors="replace")
    findings = []
    for pattern, count, meaning in scan_patterns(kernel_logs):
        findings.append(f"{count:6d}x  /{pattern}/  {meaning}")
    text = "\n".join(findings) or "No known problem patterns in the kernel logs."
    advice = ctx.refresh_state.advice() if ctx.show_refresh else []
    if advice:
        text += "\n\nRefresh rate:\n" + "\n".join(f"- {a}" for a in advice)
    bundle.add("findings.txt", text + "\n")
    return bundle, findings


def default_output_name() -> str:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"oxp3-diagnose-{stamp}.tar.gz"


def write_bundle(bundle: Bundle, path: str, owner: Optional[tuple[int, int]] = None) -> None:
    """Create ``path`` exclusively (never through a symlink) and hand it to ``owner``."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            if owner:
                os.fchown(f.fileno(), owner[0], owner[1])
            bundle.write(f)
    except BaseException:
        # Leave nothing half-written behind to block a retry (O_EXCL).
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
