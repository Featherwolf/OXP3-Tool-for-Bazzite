"""Collect a diagnostic bundle for bug reports."""

from __future__ import annotations

import datetime
import io
import os
import re
import tarfile
from typing import Optional

from .detect import CACHYOS, IMU_I2C_DEVICE, Context, parse_edid
from .kargs import LimineBackend
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
    (r"asks to use VBT vswing/preemph tables",
     "The firmware asks for its own eDP signal levels, which kernels before 7.4 ignore; on the OXP3 this goes with "
     "random white dots that flicker on dark scenes (kernel-anatase#7). Fixed by the 'Vswing / Pre-emphasis "
     "Override' series, queued for Linux 7.4."),
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
                   "bazzite-hardware-setup.service", "oxp3-tool-xbox-key.service", "oxp3-tool-steam-actions.service",
                   "oxp3-tool-osk-key.service"]),
    ("units-failed.txt", ["systemctl", "--failed", "--no-pager"]),
    ("journal-kernel-this-boot.txt", ["journalctl", "-k", "-b", "0", "--no-pager", "-o", "short-monotonic"]),
    ("journal-kernel-previous-boot.txt", ["journalctl", "-k", "-b", "-1", "--no-pager", "-o", "short-monotonic"]),
    ("journal-sleep-this-boot.txt", ["journalctl", "-b", "0", "--no-pager", "-o", "short-monotonic",
                                     "-u", "systemd-suspend.service", "-u", "systemd-logind.service",
                                     "-u", "inputplumber-suspend.service"]),
    ("journal-inputplumber-this-boot.txt", ["journalctl", "-b", "0", "--no-pager", "-o", "short-monotonic",
                                            "-u", "inputplumber.service", "-n", "3000"]),
    ("journal-oxp3-helpers-this-boot.txt", ["journalctl", "-b", "0", "--no-pager", "-o", "short-monotonic",
                                            "-u", "oxp3-tool-xbox-key.service", "-u", "oxp3-tool-steam-actions.service",
                                            "-u", "oxp3-tool-osk-key.service", "-n", "500"]),
    ("decky-loader.txt", ["systemctl", "status", "--no-pager", "--lines=0", "plugin_loader.service"]),
    ("journal-decky-loader-this-boot.txt", ["journalctl", "-b", "0", "--no-pager", "-o", "short-monotonic",
                                            "-u", "plugin_loader.service", "-n", "300"]),
    ("journal-gamescope-this-boot.txt", ["journalctl", "-b", "0", "--no-pager", "-o", "short-monotonic",
                                         "--grep", "gamescope|known display", "-n", "3000"]),
]

# CachyOS replaces the rpm-ostree and rpm commands; the rest of COMMANDS is shared.
CACHYOS_COMMANDS: list[tuple[str, list[str]]] = [
    ("packages.txt", ["pacman", "-Q", "linux-cachyos", "linux-cachyos-deckify", "linux-cachyos-lts", "inputplumber",
                      "gamescope", "gamescope-session-cachyos", "cachyos-handheld", "steam", "steamos-manager",
                      "steamos-powerbuttond", "hhd", "systemd", "linux-firmware", "mkinitcpio", "dracut", "limine",
                      "limine-mkinitcpio-hook", "systemd-boot-manager", "grub"]),
    ("bootctl-status.txt", ["bootctl", "status", "--no-pager"]),
    ("units-cachyos.txt", ["systemctl", "status", "--no-pager", "--lines=10", "plasmalogin.service",
                           "hhd@*.service"]),
    # Game Mode's units are user units; plain field matches find them from root (--user-unit adds _UID=0).
    ("journal-game-mode-units-this-boot.txt", ["journalctl", "-b", "0", "--no-pager", "-o", "short-monotonic",
                                               "_SYSTEMD_USER_UNIT=steamos-powerbuttond.service", "+",
                                               "USER_UNIT=steamos-powerbuttond.service", "+",
                                               "USER_UNIT=gamescope-session.service", "-n", "300"]),
]
# Per Steam user: the user manager's view of the Game Mode units (CachyOS).
CACHYOS_USER_UNITS = ["steamos-powerbuttond.service", "gamescope-session.service", "steam-launcher.service"]
CACHYOS_TEXT_GLOBS = [
    "/etc/default/limine", "/etc/limine-entry-tool.conf", "/etc/limine-entry-tool.d/*.conf",
    "/etc/sdboot-manage.conf", "/etc/sdboot-manage.conf.d/*.conf", "/usr/lib/sdboot-manage.conf.d/*.conf",
    "/etc/kernel/cmdline", "/etc/default/grub", "/etc/default/grub.d/*.cfg", "/boot/refind_linux.conf",
    "/etc/mkinitcpio.conf", "/etc/mkinitcpio.conf.d/*.conf", "/etc/inputplumber/devices.d/*.yaml",
    "/etc/inputplumber/capability_maps.d/*.yaml", "/etc/modprobe.d/*.conf",
    "/boot/limine.conf", "/efi/limine.conf", "/boot/efi/limine.conf", "/boot/loader/entries/*.conf",
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
        self.literals: dict[str, str] = {}  # e.g. the machine id (in Limine's boot entry paths)

    def _mac(self, m: re.Match) -> str:
        key = m.group(0).lower()
        if key == "00:00:00:00:00:00" or key == "ff:ff:ff:ff:ff:ff":
            return m.group(0)
        if key not in self.macs:
            self.macs[key] = f"mac-{len(self.macs) + 1}"
        return self.macs[key]

    def __call__(self, text: str) -> str:
        for value, tag in self.literals.items():
            text = text.replace(value, tag)
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
    machine_id = (sys.read_attr("/etc/machine-id") or "").strip()
    if re.fullmatch(r"[0-9a-f]{32}", machine_id):
        bundle.redact.literals[machine_id] = "<machine-id>"
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

    # Steam's update channel and version: a Steam update often lands at the same restart as an apply.
    steam = []
    seen = set()

    def user_file(path: str, limit: int) -> str:
        # In users' homes: never through a symlink, never unbounded, never a FIFO.
        data = sys.read_regular(path, limit)
        return data.decode(errors="replace") if data is not None else ""

    for root in ("/home", "/var/home"):  # /home is a symlink to /var/home on Bazzite (plain on CachyOS)
        for pkg in sys.glob(f"{root}/*/.local/share/Steam/package"):
            user = pkg.split("/")[-5]
            if user in seen:
                continue
            seen.add(user)
            beta = user_file(f"{pkg}/beta", 4096).strip()
            steam.append(f"{pkg}: beta={beta or '(none)'}")
            for manifest in sys.glob(f"{pkg}/steam_client_*.manifest"):
                version = re.search(r'"version"\s+"(\d+)"', user_file(manifest, 1 << 20))
                steam.append(f"  {manifest.rsplit('/', 1)[-1]}: version {version.group(1) if version else '?'}")
            log = user_file(f"{pkg[:-len('package')]}logs/bootstrap_log.txt", 8 << 20)
            if log:
                steam += ["  bootstrap_log.txt (end):"] + [f"    {line}" for line in log.splitlines()[-25:]]
    if steam:
        bundle.add("steam-client.txt", "\n".join(steam) + "\n")

    stats = []
    for p in sys.glob("/sys/power/suspend_stats/*"):
        stats.append(f"{p.rsplit('/', 1)[-1]}: {sys.read_attr(p)}")
    if stats:
        bundle.add("suspend_stats.txt", "\n".join(stats) + "\n")

    commands = COMMANDS
    if ctx.os_family == CACHYOS:
        commands = [c for c in COMMANDS if c[1][0] not in ("rpm-ostree", "rpm")] + CACHYOS_COMMANDS
        for pattern in CACHYOS_TEXT_GLOBS:
            for path in sys.glob(pattern):
                data = sys.read_bytes(path)
                if data is not None:
                    bundle.add("files" + path, data)
        for user in sorted(seen):
            if re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
                commands.append((f"units-user-{user}.txt", ["systemctl", "--user", "-M", f"{user}@", "status",
                                                            "--no-pager", "--lines=10", *CACHYOS_USER_UNITS]))
        backend = ctx.karg_backend
        if isinstance(backend, LimineBackend):
            for kernel in backend.kernels():
                commands.append((f"limine-cmdline-{kernel}.txt", ["limine-entry-tool", "--get-cmdline", kernel]))
        bundle.add("boot.txt", f"kernel arguments: {backend.name}; {backend.describe() or backend.refusal()}\n")
    for name, argv in commands:
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
