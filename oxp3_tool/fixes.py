"""The fixes the tool can apply.

A fix never touches /usr. It only writes files under /etc, appends kernel
arguments with rpm-ostree, or enables a systemd unit, and every one of those
changes is recorded so ``revert`` can undo exactly what was done.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import templates
from .detect import IP_ETC_CAPABILITY_MAPS, Context

CONTROLLER = "controller"
DISPLAY = "display"

RECOMMENDED = "recommended"
OPTIONAL = "optional"
EXPERIMENTAL = "experimental"

# InputPlumber's OXP3 capability map id whose chords the button template mirrors.
SUPPORTED_MAP_ID = "oxp9"


@dataclass
class FileSpec:
    path: str
    content: bytes
    mode: int = 0o644


@dataclass
class PostStep:
    key: str  # steps with the same key run once per invocation
    argv: list[str]
    description: str


@dataclass
class Plan:
    files: list[FileSpec] = field(default_factory=list)
    kargs: list[str] = field(default_factory=list)
    enable_units: list[str] = field(default_factory=list)
    post: list[PostStep] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)
    notes: dict[str, Any] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)


@dataclass
class Check:
    applicable: bool
    reason: str = ""
    # Selected by a plain `oxp3-tool apply` on this machine.
    recommended: bool = False


class FixError(Exception):
    pass


class Fix:
    id = ""
    group = ""
    level = RECOMMENDED
    title = ""
    summary = ""
    details = ""
    takes_effect = ""
    conflicts: tuple[str, ...] = ()

    def check(self, ctx: Context) -> Check:
        raise NotImplementedError

    def plan(self, ctx: Context, opts: dict[str, Any]) -> Plan:
        raise NotImplementedError

    def revert_post(self, ctx: Context) -> list[PostStep]:
        return []

    def revert_messages(self) -> list[str]:
        return []

    def live_state(self, ctx: Context, entry: Optional[dict[str, Any]]) -> list[str]:
        """Extra status lines describing whether the change is in effect."""
        return []


def _karg_live_state(ctx: Context, karg: str) -> list[str]:
    active = ctx.karg_active(karg)
    pending = ctx.karg_pending(karg)
    if active and (pending or ctx.pending_kargs is None):
        return [f"{karg} is active"]
    if active and not pending:
        return [f"{karg} is active now but removed from the next boot"]
    if pending:
        return [f"{karg} is set for the next boot (reboot to activate)"]
    return [f"{karg} is not set"]


# -- controller ----------------------------------------------------------------


class ButtonsFix(Fix):
    id = "buttons"
    group = CONTROLLER
    level = RECOMMENDED
    title = "Chassis buttons do what their labels say"
    summary = "Keyboard key opens the on-screen keyboard, Home opens Steam (InputPlumber map)"
    details = (
        "InputPlumber 0.82 supports the OXP3 controller, but its stock map (id oxp9) sends\n"
        "the keyboard key to QuickAccess2, which takes a screenshot, and the home key to\n"
        "the on-screen keyboard. This installs a replacement map with the same id in\n"
        f"{IP_ETC_CAPABILITY_MAPS}. Defaults: ONEX -> Quick Access, keyboard key ->\n"
        "on-screen keyboard, home -> Steam/Guide. Choose others with --onex-key,\n"
        "--keyboard-key and --home (guide, quick-access, keyboard, screenshot).\n"
        "The stock device config is left alone, and InputPlumber is restarted."
    )
    takes_effect = "immediately (InputPlumber restarts; restart Steam if buttons do nothing)"

    def check(self, ctx: Context) -> Check:
        hhd = ctx.hhd
        if hhd.running:
            return Check(False, "Handheld Daemon is running (" + ", ".join(hhd.active_units) +
                         "); it handles the buttons instead of InputPlumber")
        ip = ctx.inputplumber
        if not ip.installed:
            return Check(False, "InputPlumber is not installed (Bazzite desktop images do not ship it; "
                                "use a bazzite-deck image)")
        if not ip.oxp3_config:
            return Check(False, "this InputPlumber has no ONEXPLAYER 3 device config (needs 0.82.0 or newer)")
        if ip.map_id != SUPPORTED_MAP_ID:
            return Check(False, f"{ip.oxp3_config} uses capability map {ip.map_id!r}; this tool only "
                                f"knows {SUPPORTED_MAP_ID!r} (InputPlumber 0.82). Check for a tool update.")
        if not ip.upstream_map:
            return Check(False, f"no stock capability map with id {ip.map_id!r} found")
        return Check(True, "", recommended=True)

    def plan(self, ctx: Context, opts: dict[str, Any]) -> Plan:
        ip = ctx.inputplumber
        actions = dict(templates.DEFAULT_BUTTON_ACTIONS)
        for key, opt in (("onex", "onex_key"), ("keyboard", "keyboard_key"), ("home", "home")):
            if opts.get(opt):
                actions[key] = opts[opt]
        assert ip.upstream_map and ip.map_id
        upstream_name = ip.upstream_map.rsplit("/", 1)[-1]
        upstream_sha = ctx.sys.sha256(ip.upstream_map)
        content = templates.capability_map_yaml(ip.map_id, actions, ip.upstream_map, upstream_sha)
        plan = Plan(
            files=[FileSpec(f"{IP_ETC_CAPABILITY_MAPS}/{upstream_name}", content.encode())],
            options={"actions": actions},
            notes={"upstream_map": ip.upstream_map, "upstream_sha256": upstream_sha, "map_id": ip.map_id},
        )
        if opts.get("restart", True):
            plan.post.append(PostStep(
                "restart-inputplumber",
                ["systemctl", "try-restart", "inputplumber.service"],
                "restart InputPlumber so it loads the new map",
            ))
        else:
            plan.messages.append("InputPlumber was not restarted; the new map loads on the next reboot.")
        return plan

    def revert_post(self, ctx: Context) -> list[PostStep]:
        return [PostStep("restart-inputplumber", ["systemctl", "try-restart", "inputplumber.service"],
                         "restart InputPlumber so it loads the stock map")]

    def live_state(self, ctx: Context, entry: Optional[dict[str, Any]]) -> list[str]:
        lines = []
        ip = ctx.inputplumber
        if entry:
            actions = entry.get("options", {}).get("actions", {})
            if actions:
                lines.append(", ".join(f"{k} -> {v}" for k, v in sorted(actions.items())))
            recorded = entry.get("notes", {}).get("upstream_sha256")
            if ip.upstream_map and recorded:
                current = ctx.sys.sha256(ip.upstream_map)
                if current != recorded:
                    lines.append(
                        "InputPlumber's stock map changed since this was applied; check that the "
                        "override is still wanted (`sudo oxp3-tool apply buttons` refreshes it)"
                    )
            if ip.effective_map and entry.get("files") and ip.effective_map != entry["files"][0]["path"]:
                lines.append(f"warning: InputPlumber loads {ip.effective_map} instead of this override")
        return lines


class VolumeKeysFix(Fix):
    id = "volume-keys"
    group = CONTROLLER
    level = RECOMMENDED
    title = "Volume rocker stops sticking"
    summary = "Force key releases for the volume rocker (EC drops them)"
    details = (
        "The OXP3 embedded controller drops the key-release scancodes of the volume\n"
        "rocker, so volume keeps changing after the key is let go. This adds a udev hwdb\n"
        "entry marking both keys as force-release. Trade-off: holding a volume key no\n"
        "longer auto-repeats. A reboot is needed to fully undo it after revert."
    )
    takes_effect = "immediately"
    HWDB = "/etc/udev/hwdb.d/61-oxp3-tool-volume-keys.hwdb"

    def check(self, ctx: Context) -> Check:
        return Check(True, "", recommended=True)

    def _trigger_steps(self, ctx: Context) -> list[PostStep]:
        steps = [PostStep("hwdb-update", ["systemd-hwdb", "update"], "rebuild the udev hwdb")]
        for serio in ctx.sys.glob("/sys/bus/serio/devices/serio*"):
            driver = ctx.sys.realpath(f"{serio}/driver")
            target = ctx.sys.realpath(serio)
            if driver and driver.rsplit("/", 1)[-1] == "atkbd" and target:
                steps.append(PostStep(
                    f"udev-trigger:{target}",
                    ["udevadm", "trigger", "--action=change", f"--parent-match={target}"],
                    "re-run udev rules for the built-in keyboard",
                ))
                steps.append(PostStep("udev-settle", ["udevadm", "settle", "--timeout=10"], "wait for udev"))
        return steps

    def plan(self, ctx: Context, opts: dict[str, Any]) -> Plan:
        return Plan(files=[FileSpec(self.HWDB, templates.VOLUME_HWDB.encode())], post=self._trigger_steps(ctx))

    def revert_post(self, ctx: Context) -> list[PostStep]:
        return self._trigger_steps(ctx)

    def revert_messages(self) -> list[str]:
        return ["The kernel keeps forcing volume-key releases until the next reboot."]

    def live_state(self, ctx: Context, entry: Optional[dict[str, Any]]) -> list[str]:
        released = ctx.atkbd_force_release
        if released is None:
            return ["could not read the built-in keyboard's force_release list"]
        if templates.VOLUME_SCANCODES <= released:
            return ["kernel is forcing volume-key releases"]
        return ["kernel is not forcing volume-key releases yet"]


class GyroFix(Fix):
    id = "gyro"
    group = CONTROLLER
    level = EXPERIMENTAL
    title = "Let bmi270 drive the gyro"
    summary = "Blacklist bmi160_i2c so the BMI260 IMU shows up for InputPlumber"
    details = (
        "The OXP3's BMI260 IMU uses ACPI ID 10EC5280, which both bmi160_i2c and (on\n"
        "Bazzite's kernel) bmi270_i2c claim. bmi160 cannot drive the chip. If the gyro is\n"
        "missing (no IIO device named 'bmi260'), this keeps bmi160_i2c from loading so\n"
        "bmi270 can bind. Only offered when the running kernel's bmi270_i2c knows 10EC5280.\n"
        "Untested on hardware. Needs a reboot."
    )
    takes_effect = "after reboot"
    CONF = "/etc/modprobe.d/oxp3-tool-imu.conf"

    def check(self, ctx: Context) -> Check:
        if "bmi260" in ctx.iio_names:
            return Check(False, "the gyro already works (IIO device 'bmi260' is present)")
        if not ctx.imu_present:
            return Check(False, "no IMU found at ACPI 10EC5280")
        if ctx.imu_driver not in (None, "bmi160_i2c"):
            return Check(False, f"the IMU is bound to {ctx.imu_driver}, not bmi160")
        supported = ctx.module_has_alias("bmi270_i2c", "10EC5280")
        if supported is None:
            return Check(False, "could not inspect the bmi270_i2c module (modinfo failed)")
        if not supported:
            return Check(False, "this kernel's bmi270_i2c does not support ACPI ID 10EC5280")
        return Check(True)

    def plan(self, ctx: Context, opts: dict[str, Any]) -> Plan:
        return Plan(files=[FileSpec(self.CONF, templates.IMU_MODPROBE.encode())],
                    messages=["Reboot, then check `oxp3-tool status` for an IIO device named bmi260."])

    def live_state(self, ctx: Context, entry: Optional[dict[str, Any]]) -> list[str]:
        driver = ctx.imu_driver or "none"
        names = ", ".join(ctx.iio_names) or "none"
        return [f"IMU driver: {driver}; IIO devices: {names}"]


class InputPlumberSuspendFix(Fix):
    id = "inputplumber-suspend"
    group = CONTROLLER
    level = EXPERIMENTAL
    title = "InputPlumber sleep hook"
    summary = "Enable InputPlumber's own suspend/resume hook (shipped but disabled)"
    details = (
        "InputPlumber ships inputplumber-suspend.service, which pauses its virtual\n"
        "controllers before sleep and restores them after resume. Bazzite does not enable\n"
        "it. Try this if the controller misbehaves after waking from sleep. Untested on\n"
        "the OXP3."
    )
    takes_effect = "from the next suspend"
    UNIT = "inputplumber-suspend.service"

    def check(self, ctx: Context) -> Check:
        if not ctx.inputplumber.installed:
            return Check(False, "InputPlumber is not installed")
        if not ctx.unit_file_exists(self.UNIT):
            return Check(False, f"{self.UNIT} is not installed")
        return Check(True)

    def plan(self, ctx: Context, opts: dict[str, Any]) -> Plan:
        return Plan(enable_units=[self.UNIT])

    def live_state(self, ctx: Context, entry: Optional[dict[str, Any]]) -> list[str]:
        return [f"{self.UNIT} is {ctx.unit_enabled(self.UNIT) or 'unknown'}"]


# -- display / resume ----------------------------------------------------------


class _KargFix(Fix):
    karg = ""

    def check(self, ctx: Context) -> Check:
        if not ctx.has_rpm_ostree:
            return Check(False, "rpm-ostree is not available")
        return self._check(ctx)

    def _check(self, ctx: Context) -> Check:
        return Check(True)

    def plan(self, ctx: Context, opts: dict[str, Any]) -> Plan:
        return Plan(kargs=[self.karg])

    def live_state(self, ctx: Context, entry: Optional[dict[str, Any]]) -> list[str]:
        return _karg_live_state(ctx, self.karg)


class NvmeResumeFix(_KargFix):
    id = "nvme-resume"
    group = DISPLAY
    level = RECOMMENDED
    title = "SSD survives sleep"
    summary = "nvme.noacpi=1 so the Predator GM7 SSD comes back from s2idle"
    details = (
        "OXP3 units with the Acer Predator GM7 SSD (PCI 1dee:1602) can fail to resume:\n"
        "the kernel logs 'Disabling device after reset failure: -19', the root file\n"
        "system goes read-only and the screen stays black or frozen. The firmware asks\n"
        "for ACPI 'simple suspend' on this drive; nvme.noacpi=1 turns that off and fixed\n"
        "resume in community testing on the OXP3. Recommended only when a GM7 is\n"
        "installed; --any-ssd applies it anyway. Needs a reboot."
    )
    takes_effect = "after reboot"
    karg = "nvme.noacpi=1"

    def _check(self, ctx: Context) -> Check:
        if ctx.has_gm7:
            return Check(True, "", recommended=True)
        return Check(True, "no Predator GM7 SSD found; only needed with --any-ssd if you see NVMe resume errors")

    def plan(self, ctx: Context, opts: dict[str, Any]) -> Plan:
        if not ctx.has_gm7 and not opts.get("any_ssd"):
            raise FixError("no Predator GM7 SSD (1dee:1602) found; pass --any-ssd to apply anyway")
        return super().plan(ctx, opts)

    def live_state(self, ctx: Context, entry: Optional[dict[str, Any]]) -> list[str]:
        lines = _karg_live_state(ctx, self.karg)
        if ctx.karg_active(self.karg):
            value = ctx.sys.read_attr("/sys/module/nvme/parameters/noacpi")
            if value is not None:
                lines.append(f"nvme noacpi parameter reads {value}")
        return lines


class OledProfileFix(Fix):
    id = "oled-profile"
    group = DISPLAY
    level = RECOMMENDED
    title = "No black screen from HDR"
    summary = "gamescope profile that keeps the OLED out of PQ/BT.2020 mode"
    details = (
        "gamescope drives the OXP3 panel (Samsung SDC 0x4301, the Legion Go 2 panel) with\n"
        "BT.2020/PQ when HDR is on. On the OXP3 that leaves the panel black in Game Mode,\n"
        "including after boot, session switches and resume, and the brightness slider\n"
        "stops working. This installs a gamescope display profile in /etc/gamescope/scripts\n"
        "that keeps the panel in its native gamma 2.2 mode (the Steam Deck OLED approach):\n"
        "HDR still works through gamescope's tone mapping. It also lists the panel's\n"
        "48-144 Hz refresh rates; --min-refresh=30 extends that down to 30 Hz (untested)."
    )
    takes_effect = "next time Game Mode starts"
    LUA = "/etc/gamescope/scripts/90-oxp3-tool-oled.lua"

    def check(self, ctx: Context) -> Check:
        if not ctx.gamescope_installed:
            return Check(False, "gamescope is not installed")
        known = ctx.panel_is_known
        if known is False:
            panel = ctx.internal_panel
            edid = panel[1] if panel else None
            found = f"{edid.vendor} 0x{edid.product:04x}" if edid else "unknown"
            return Check(False, f"the internal panel is {found}, not the SDC 0x4301 OLED this profile is for")
        return Check(True, "", recommended=True)

    def plan(self, ctx: Context, opts: dict[str, Any]) -> Plan:
        min_refresh = int(opts.get("min_refresh") or templates.LEGIONGO2_MIN_HZ)
        try:
            content = templates.oled_lua(min_refresh)
        except ValueError as e:
            raise FixError(str(e)) from e
        return Plan(
            files=[FileSpec(self.LUA, content.encode())],
            options={"min_refresh": min_refresh},
            messages=["Switch to Desktop Mode and back (or reboot) so gamescope loads the profile."],
        )

    def revert_messages(self) -> list[str]:
        return ["Switch to Desktop Mode and back (or reboot) so gamescope drops the profile."]

    def live_state(self, ctx: Context, entry: Optional[dict[str, Any]]) -> list[str]:
        panel = ctx.internal_panel
        if not panel:
            return ["no eDP connector found"]
        name, edid = panel
        if not edid:
            return [f"{name}: EDID not readable"]
        model = f" ({edid.name})" if edid.name else ""
        return [f"{name}: {edid.vendor} 0x{edid.product:04x}{model}"]


class XePanelReplayFix(_KargFix):
    id = "xe-panel-replay-off"
    group = DISPLAY
    level = EXPERIMENTAL
    title = "Disable Panel Replay (xe)"
    summary = "xe.enable_panel_replay=0 for intermittent black or garbled screens"
    details = (
        "Panel Replay is the eDP power-saving mode that most often breaks on Panther Lake\n"
        "OLED panels. Setting xe.enable_panel_replay=0 falls back to PSR1. On the OXP3 it\n"
        "kept the display working in community testing but did not cure all tearing, and\n"
        "nobody has shown it fixes resume problems. Intel calls these debug knobs; the\n"
        "kernel marks itself tainted when they are set. Costs some battery. Never combined\n"
        "with xe.enable_psr=0 or xe.enable_psr2_sel_fetch=0: both combinations gave a\n"
        "black screen on the OXP3. Needs a reboot."
    )
    takes_effect = "after reboot"
    karg = "xe.enable_panel_replay=0"
    BAD = ("xe.enable_psr=0", "xe.enable_psr2_sel_fetch=0")

    def _check(self, ctx: Context) -> Check:
        present = [k for k in self.BAD if ctx.karg_active(k) or ctx.karg_pending(k)]
        if present:
            return Check(False, f"{', '.join(present)} is set; combined with this it blanks the OXP3 screen")
        return Check(True)


class XeDsbFix(_KargFix):
    id = "xe-dsb-off"
    group = DISPLAY
    level = OPTIONAL
    title = "Disable DSB (xe)"
    summary = "xe.enable_dsb=0 to stop 'DSB poll error' log floods and cursor trails"
    details = (
        "Under gamescope the xe driver can log '[CRTC:...] DSB 0 poll error' every frame,\n"
        "flooding the journal; some Panther Lake users also see cursor trails or flicker.\n"
        "xe.enable_dsb=0 makes the driver program the display without the Display State\n"
        "Buffer. Cosmetic: it does not fix suspend or resume. Taints the kernel. Needs a\n"
        "reboot."
    )
    takes_effect = "after reboot"
    karg = "xe.enable_dsb=0"


ALL_FIXES: list[Fix] = [
    ButtonsFix(),
    VolumeKeysFix(),
    GyroFix(),
    InputPlumberSuspendFix(),
    NvmeResumeFix(),
    OledProfileFix(),
    XePanelReplayFix(),
    XeDsbFix(),
]

BY_ID = {f.id: f for f in ALL_FIXES}
