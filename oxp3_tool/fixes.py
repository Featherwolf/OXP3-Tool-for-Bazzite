"""The fixes the tool can apply.

A fix never touches the read-only /usr. It only writes files under /etc (and
/usr/local, which is /var/usrlocal on Bazzite), appends kernel arguments with
rpm-ostree, or enables a systemd unit, and every one of those changes is
recorded so ``revert`` can undo exactly what was done.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from . import osk_helper, templates, xbox_helper
from .detect import IP_ETC_CAPABILITY_MAPS, NATIVE_STEAM, OGUI, Context
from .system import sha256_bytes

CONTROLLER = "controller"
DISPLAY = "display"

RECOMMENDED = "recommended"
OPTIONAL = "optional"
EXPERIMENTAL = "experimental"

# Vendor button id the OXP3 controller can report its Xbox key as.
XBOX_KEY_BUTTON = 0x25

# InputPlumber's OXP3 capability map id whose chords the button template mirrors.
SUPPORTED_MAP_ID = "oxp9"
# normalized_yaml_sha256() of the stock oxp9 map the override was written
# against (InputPlumber 0.82.0, onexplayer_type9.yaml).
KNOWN_STOCK_MAPS = {"660fb0c977d803fff773c6abd84421e06264fe2b58e67fcd1a8839773d7c5327"}


def normalized_yaml_sha256(text: str) -> str:
    """Hash of a YAML file ignoring comment-only lines, blank lines and trailing spaces."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    kept = [ln for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    return hashlib.sha256(("\n".join(kept) + "\n").encode()).hexdigest()


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
    order: int = 50  # steps run in ascending order
    # When set, the step runs only if the apply changes one of these files, or
    # finishes a restart that an earlier --no-restart skipped.
    only_if_changed: tuple[str, ...] = ()
    note: str = ""  # message shown when the step runs


@dataclass
class Plan:
    files: list[FileSpec] = field(default_factory=list)
    kargs: list[str] = field(default_factory=list)
    enable_units: list[str] = field(default_factory=list)
    post: list[PostStep] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)
    notes: dict[str, Any] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)
    # Units an earlier apply enabled and this plan drops are disabled; False
    # leaves them running until the next boot.
    stop_dropped_units: bool = True


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

    def plan(self, ctx: Context, opts: dict[str, Any], prev: dict[str, Any]) -> Plan:
        """``opts`` come from the command line, ``prev`` are the options of the last apply."""
        raise NotImplementedError

    def after_apply(self, ctx: Context) -> list[str]:
        """Messages after the post steps ran (e.g. a check that the change took effect)."""
        return []

    def revert_post(self, ctx: Context) -> list[PostStep]:
        return []

    def revert_messages(self) -> list[str]:
        return []

    def live_state(self, ctx: Context, entry: Optional[dict[str, Any]]) -> list[str]:
        """Extra status lines describing whether the change is in effect."""
        return []

    def note(self, ctx: Context) -> list[str]:
        """Extra status lines for a fix that is not applied (status only, never during apply)."""
        return []


def _karg_live_state(ctx: Context, karg: str, fix_id: str) -> list[str]:
    active = ctx.karg_active(karg)
    pending = ctx.karg_pending(karg)
    again = f"run `sudo oxp3-tool apply {fix_id}` again"
    if active and (pending or ctx.pending_kargs is None):
        return [f"{karg} is active"]
    if active:
        return [f"{karg} is active now but missing from the next boot (a system update installed "
                f"before rebooting can drop it); {again}"]
    if pending:
        return [f"{karg} is set for the next boot (reboot to activate)"]
    return [f"{karg} is not set (a system update installed before rebooting can drop it); {again}"]


def restart_pending(ctx: Context, notes: dict[str, Any]) -> bool:
    """An InputPlumber restart skipped with --no-restart that no reboot has done since."""
    if not notes.get("restart_pending"):
        return False
    boot = notes.get("restart_pending_boot")
    return boot is None or boot == ctx.boot_id


RESTART_INPUTPLUMBER = "restart-inputplumber"


def _restart_inputplumber(only_if_changed: tuple[str, ...] = (), note: str = "") -> PostStep:
    return PostStep(RESTART_INPUTPLUMBER, ["systemctl", "try-restart", "inputplumber.service"],
                    "restart InputPlumber so it loads the capability map", order=60,
                    only_if_changed=only_if_changed, note=note)


def _daemon_reload() -> PostStep:
    return PostStep("daemon-reload", ["systemctl", "daemon-reload"], "reload systemd units", order=10)


def _helper_live_state(ctx: Context, entry: Optional[dict[str, Any]], unit: str, failed: str, warning: str,
                       wanted: dict[str, bytes], fix_id: str, ok: str = "") -> list[str]:
    """Status of a helper service: unit state, delivery failures this boot, stale files.

    With ``ok`` (a success line the helper logs) only a failure after the last success is reported.
    """
    state = ctx.unit_state(unit) or "unknown"
    line = f"{unit} is {state}"
    if state != "active":
        line += f"; see `journalctl -u {unit}`"
    lines = [line]
    # Both markers are plain text, so they are safe in journalctl's regular expression.
    res = ctx.sys.run(["journalctl", "-u", unit, "-b", "0", "--no-pager", "-q", "-o", "cat", "-r",
                       "--grep", f"{failed}|{ok}" if ok else failed, "-n", "1"], timeout=30)
    if failed in res.stdout:
        lines.append(f"warning: {warning} this boot; see `journalctl -u {unit}`")
    recorded = {r["path"]: r.get("sha256") for r in (entry or {}).get("files", [])}
    if any(recorded.get(p) == ctx.sys.sha256(p) != sha256_bytes(c) for p, c in wanted.items()):
        lines.append(f"installed by an older oxp3-tool; run `sudo oxp3-tool apply {fix_id}` to update it")
    return lines


# -- controller ----------------------------------------------------------------


class ButtonsFix(Fix):
    id = "buttons"
    group = CONTROLLER
    level = RECOMMENDED
    title = "Chassis buttons do what their labels say"
    summary = "Keyboard key opens the on-screen keyboard, Home opens Steam (InputPlumber map)"
    details = (
        "InputPlumber 0.82 supports the OXP3 controller, but its stock map (id oxp9)\n"
        "sends the keyboard key to QuickAccess2 and the home key to gamepad Keyboard.\n"
        "In Bazzite's default Game Mode, which runs Steam behind the OpenGamepadUI\n"
        "overlay, that makes the keyboard key open the OpenGamepadUI menu and the home key\n"
        "do nothing; with native Steam Input they take a screenshot and open the keyboard.\n"
        "This installs a replacement map with the same id in\n"
        f"{IP_ETC_CAPABILITY_MAPS}, written for the Game Mode input in use.\n"
        "Defaults: ONEX/console -> Quick Access, keyboard key -> Steam on-screen keyboard,\n"
        "home -> Steam menu, Xbox key -> Steam menu (as stock). Choose others with\n"
        "--onex-key, --keyboard-key, --home and --xbox-key (guide, quick-access, keyboard,\n"
        "keyboard-chord, screenshot, ogui-menu).\n"
        "With native Steam Input the keyboard action skips Steam's Steam+X shortcut,\n"
        "which does nothing on some setups: a small helper service\n"
        f"({templates.OSK_KEY_UNIT_NAME}) asks Steam directly to open its keyboard\n"
        "(steam://open/keyboard, as HHD does). keyboard-chord sends Steam+X instead.\n"
        "Check each key with `inputplumber device 0 test` (press q to quit; closing the\n"
        "window instead leaves InputPlumber in test mode until\n"
        "`sudo systemctl restart inputplumber`). A key that goes to the helper shows\n"
        f"nothing there; `journalctl -u {templates.OSK_KEY_UNIT_NAME} -f` logs its presses.\n"
        "Your choice is kept when you apply again\n"
        "(--defaults resets it). Apply again after `ujust configure-opengamepadui` switches\n"
        "Game Mode input. The override replaces the whole stock map, so it is only\n"
        "recommended while the stock map is the 0.82.0 one it was written for.\n"
        "InputPlumber is restarted when the map changes."
    )
    takes_effect = "immediately (InputPlumber restarts when the map changes; restart Steam if buttons do nothing)"
    OSK_UNIT = templates.OSK_KEY_UNIT_NAME

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
        if not self._stock_map_known(ctx):
            return Check(True, f"InputPlumber's stock map {ip.upstream_map} is not the 0.82.0 one this "
                               "override was written for; compare them before applying", recommended=False)
        return Check(True, "", recommended=True)

    @staticmethod
    def _stock_map_known(ctx: Context) -> bool:
        path = ctx.inputplumber.upstream_map
        text = ctx.sys.read_text(path) if path else None
        return text is not None and normalized_yaml_sha256(text) in KNOWN_STOCK_MAPS

    def plan(self, ctx: Context, opts: dict[str, Any], prev: dict[str, Any]) -> Plan:
        ip = ctx.inputplumber
        actions = dict(templates.DEFAULT_BUTTON_ACTIONS)
        for key, action in (prev.get("actions") or {}).items():
            if key in actions and action in templates.BUTTON_ACTIONS:
                actions[key] = action
        for key, opt in (("onex", "onex_key"), ("keyboard", "keyboard_key"), ("home", "home"),
                         ("xbox", "xbox_key")):
            if opts.get(opt):
                actions[key] = opts[opt]
        if not (ip.upstream_map and ip.map_id):
            raise FixError("InputPlumber's stock OXP3 capability map was not found")
        upstream_name = ip.upstream_map.rsplit("/", 1)[-1]
        upstream_sha = ctx.sys.sha256(ip.upstream_map)
        session = ctx.game_mode_input
        content = templates.capability_map_yaml(ip.map_id, actions, ip.upstream_map, upstream_sha, session)
        map_path = f"{IP_ETC_CAPABILITY_MAPS}/{upstream_name}"
        restart = opts.get("restart", True)
        plan = Plan(
            files=[FileSpec(map_path, content.encode())],
            options={"actions": actions},
            notes={"upstream_map": ip.upstream_map, "upstream_sha256": upstream_sha, "map_id": ip.map_id,
                   "game_mode_input": session},
            # Without the restart InputPlumber keeps the old map, which may still need a dropped helper.
            stop_dropped_units=restart,
        )
        # The engine records a restart as pending while it is due and clears it once it ran.
        map_changes = ctx.sys.sha256(map_path) != sha256_bytes(content.encode())
        if session == OGUI:
            plan.messages.append(
                "Game Mode runs Steam behind the OpenGamepadUI overlay, so the map is written for it. If you "
                "switch with `ujust configure-opengamepadui`, run `sudo oxp3-tool apply buttons` again."
            )
        if restart:
            # Only when the map changes: a helper-only update leaves InputPlumber alone.
            note = ("Reboot (or leave Game Mode and start it again) afterwards: Bazzite selects the Steam Deck "
                    "controller for native Steam Input only when Game Mode starts, and the InputPlumber restart "
                    "resets it.") if session == NATIVE_STEAM else ""
            plan.post.append(_restart_inputplumber(only_if_changed=(map_path,), note=note))
        elif map_changes:
            plan.notes.update(restart_pending=True, restart_pending_boot=ctx.boot_id)
            plan.messages.append("InputPlumber was not restarted; the new map loads on the next reboot.")
        if not self._stock_map_known(ctx):
            plan.messages.append(
                f"The stock map differs from the 0.82.0 one this override was written for; compare with "
                f"`diff {ip.upstream_map} {IP_ETC_CAPABILITY_MAPS}/{upstream_name}`."
            )
        if any(templates.uses_osk_helper(a, session) for a in actions.values()):
            if not ctx.sys.exists(osk_helper.BUSCTL):
                raise FixError(f"{osk_helper.BUSCTL} is missing, which the on-screen keyboard helper needs; "
                               "choose keyboard-chord for that key instead")
            plan.files += [
                FileSpec(templates.OSK_KEY_HELPER_PATH, templates.osk_key_helper_script().encode(), 0o755),
                FileSpec(templates.OSK_KEY_UNIT_PATH, templates.OSK_KEY_UNIT.encode()),
            ]
            plan.enable_units.append(self.OSK_UNIT)
            plan.post += [
                _daemon_reload(),
                PostStep("restart-osk-key", ["systemctl", "restart", self.OSK_UNIT],
                         "start the on-screen keyboard helper", order=70),
            ]
        return plan

    def after_apply(self, ctx: Context) -> list[str]:
        if not ctx.sys.exists(templates.OSK_KEY_UNIT_PATH):
            return []
        state = ctx.unit_state(self.OSK_UNIT)
        if state != "active":
            return [f"{self.OSK_UNIT} is {state or 'not running'}; see `journalctl -u {self.OSK_UNIT}`"]
        return []

    def revert_post(self, ctx: Context) -> list[PostStep]:
        steps = [_restart_inputplumber()]
        if ctx.unit_state(self.OSK_UNIT) in ("active", "activating", "reloading"):
            steps += [
                PostStep("stop-osk-key", ["systemctl", "stop", self.OSK_UNIT],
                         "stop the on-screen keyboard helper", order=5),
                _daemon_reload(),
            ]
        return steps

    def live_state(self, ctx: Context, entry: Optional[dict[str, Any]]) -> list[str]:
        lines = []
        ip = ctx.inputplumber
        if not entry:
            return lines
        actions = entry.get("options", {}).get("actions", {})
        if actions:
            lines.append(", ".join(f"{k} -> {v}" for k, v in sorted(actions.items())))
        override = entry["files"][0]["path"] if entry.get("files") else None
        if ip.upstream_map and override and not self._stock_map_known(ctx):
            lines.append(
                "InputPlumber's stock map changed (an update?). This override still replaces it completely. "
                f"Compare with `diff {ip.upstream_map} {override}`; "
                "`sudo oxp3-tool revert buttons` returns to the stock map."
            )
        if ip.effective_map and override and ip.effective_map != override:
            lines.append(f"warning: InputPlumber loads {ip.effective_map} instead of this override")
        # Maps written before the tool knew about the overlay were native-Steam-Input maps.
        written_for = entry.get("notes", {}).get("game_mode_input", NATIVE_STEAM)
        if ctx.game_mode_input is not None and written_for != ctx.game_mode_input:
            lines.append("this map was written for a different Game Mode input (OpenGamepadUI overlay or native "
                         "Steam Input, or an older oxp3-tool); run `sudo oxp3-tool apply buttons` again")
        if restart_pending(ctx, entry.get("notes", {})):
            lines.append("InputPlumber has not been restarted since; reboot or run `sudo oxp3-tool apply buttons`")
        if any(r.get("path") == templates.OSK_KEY_UNIT_PATH for r in entry.get("files", [])):
            lines += _helper_live_state(
                ctx, entry, self.OSK_UNIT, osk_helper.SEND_FAILED,
                "the last keyboard request could not be handed to Steam",
                {templates.OSK_KEY_HELPER_PATH: templates.osk_key_helper_script().encode(),
                 templates.OSK_KEY_UNIT_PATH: templates.OSK_KEY_UNIT.encode()}, "buttons",
                ok=osk_helper.SENT_GREP)
        for user in ctx.steam_screen_reader_users:
            lines.append(f"warning: Steam's screen reader is on for {user}; it replaces the Steam button shortcuts "
                         "(Steam+X keyboard, Steam+R1 screenshot). See README > Common problems to turn it off")
        if ctx.game_mode_input == OGUI and written_for == OGUI and \
                actions.get("keyboard") in (templates.OSK_ACTION, "keyboard-chord"):
            lines.append("under the OpenGamepadUI overlay the keyboard key's Steam+X shortcut often does nothing "
                         "(OpenGamepadUI issue #549); for a working on-screen keyboard switch to native Steam Input "
                         "(`ujust configure-opengamepadui disable`, reboot, `sudo oxp3-tool apply buttons`)")
        return lines


class XboxKeyFix(Fix):
    id = "xbox-key"
    group = CONTROLLER
    level = OPTIONAL
    title = "Xbox key as the Steam button"
    summary = "Helper that makes the Xbox key (vendor button 0x25) tap the Steam button"
    details = (
        "On some OXP3 units the controller reports the Xbox key (the lit logo key below\n"
        "Home) only on its vendor interface, as button 0x25. InputPlumber 0.82 cannot\n"
        "decode that id: it logs 'OXP HID: failed to parse B2 report ... 25 ...' and the\n"
        "key does nothing. Which units do this depends on the button layout stored in the\n"
        "controller (OneXConsole on Windows can change it); on others the key arrives as\n"
        "the normal Xbox guide button and works without this fix.\n"
        "This installs a small helper and a systemd service that read that interface\n"
        "(read-only: nothing is written to the controller) and, on each press, ask\n"
        "InputPlumber over D-Bus for a Steam button tap (opens the Steam menu). Holding\n"
        "the key for Steam+button shortcuts does not work this way (InputPlumber 0.82's\n"
        "hold-capable D-Bus call is broken); use Home for those.\n"
        f"Files: {templates.XBOX_KEY_HELPER_PATH} and {templates.XBOX_KEY_UNIT_PATH}.\n"
        "Recommended when InputPlumber's log shows button 25 this boot (press the key\n"
        "once, then run `sudo oxp3-tool status`). The key always sends the Steam button\n"
        "here; --xbox-key does not apply to it."
    )
    takes_effect = "immediately"
    UNIT = templates.XBOX_KEY_UNIT_NAME

    def check(self, ctx: Context) -> Check:
        if ctx.hhd.running:
            return Check(False, "Handheld Daemon is running and handles the controller instead of InputPlumber")
        if not ctx.inputplumber.installed:
            return Check(False, "InputPlumber is not installed")
        if ctx.oxp_vendor_hidraw is None:
            return Check(False, "the controller's vendor interface (1a86:fe00, interface 2) was not found")
        if not ctx.sys.exists("/usr/bin/busctl"):
            return Check(False, "busctl is not installed")
        if ctx.sys.exists(templates.XBOX_KEY_HELPER_PATH):
            # Keeps a plain `apply` after a tool update refreshing the installed helper.
            return Check(True, "the helper is installed", recommended=True)
        seen = ctx.ip_undecoded_buttons
        if seen and XBOX_KEY_BUTTON in seen:
            return Check(True, "InputPlumber logged the Xbox key as button 0x25 this boot, which it cannot read",
                         recommended=True)
        return Check(True, "only needed when the Xbox key does nothing; press it once, and if InputPlumber "
                           "cannot read it this becomes recommended")

    def plan(self, ctx: Context, opts: dict[str, Any], prev: dict[str, Any]) -> Plan:
        return Plan(
            files=[
                FileSpec(templates.XBOX_KEY_HELPER_PATH, templates.xbox_key_helper_script().encode(), 0o755),
                FileSpec(templates.XBOX_KEY_UNIT_PATH, templates.XBOX_KEY_UNIT.encode()),
            ],
            enable_units=[self.UNIT],
            post=[
                _daemon_reload(),
                PostStep("restart-xbox-key", ["systemctl", "restart", self.UNIT], "start the Xbox key helper",
                         order=70),
            ],
        )

    def after_apply(self, ctx: Context) -> list[str]:
        state = ctx.unit_state(self.UNIT)
        if state != "active":
            return [f"{self.UNIT} is {state or 'not running'}; see `journalctl -u {self.UNIT}`"]
        return ["Press the Xbox key in Game Mode; if nothing happens, `sudo oxp3-tool status` shows "
                "whether the helper could reach InputPlumber."]

    def revert_post(self, ctx: Context) -> list[PostStep]:
        return [
            PostStep("stop-xbox-key", ["systemctl", "stop", self.UNIT], "stop the Xbox key helper", order=5),
            _daemon_reload(),
        ]

    def live_state(self, ctx: Context, entry: Optional[dict[str, Any]]) -> list[str]:
        return _helper_live_state(
            ctx, entry, self.UNIT, xbox_helper.SEND_FAILED,
            "the helper could not deliver the Xbox key to InputPlumber",
            {templates.XBOX_KEY_HELPER_PATH: templates.xbox_key_helper_script().encode(),
             templates.XBOX_KEY_UNIT_PATH: templates.XBOX_KEY_UNIT.encode()}, self.id)


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
        "longer auto-repeats. Not for use with Handheld Daemon, which drops the forced\n"
        "releases together with the presses (hhd#342). A reboot is needed to fully undo\n"
        "it after revert."
    )
    takes_effect = "immediately (otherwise after a reboot)"
    HWDB = "/etc/udev/hwdb.d/61-oxp3-tool-volume-keys.hwdb"

    def check(self, ctx: Context) -> Check:
        hhd = ctx.hhd
        if hhd.running:
            return Check(False, "Handheld Daemon is running (" + ", ".join(hhd.active_units) +
                         "); it grabs the built-in keyboard and would drop every volume press (hhd#342)")
        if hhd.installed:
            return Check(True, "Handheld Daemon is installed; if it runs it drops every volume press "
                               "with this fix (hhd#342)", recommended=False)
        return Check(True, "", recommended=True)

    def _trigger_steps(self, ctx: Context) -> list[PostStep]:
        steps = [
            PostStep("hwdb-update", ["systemd-hwdb", "update"], "rebuild the udev hwdb", order=10),
            # Force udevd to load the new hwdb.bin now; its own reload check is
            # throttled and may still be using the old database.
            PostStep("udev-reload", ["udevadm", "control", "--reload"], "make udev load the new hwdb", order=20),
        ]
        for serio in ctx.sys.glob("/sys/bus/serio/devices/serio*"):
            driver = ctx.sys.realpath(f"{serio}/driver")
            target = ctx.sys.realpath(serio)
            if driver and driver.rsplit("/", 1)[-1] == "atkbd" and target:
                steps.append(PostStep(
                    f"udev-trigger:{target}",
                    ["udevadm", "trigger", "--action=change", f"--parent-match={target}"],
                    "re-run udev rules for the built-in keyboard", order=30,
                ))
                steps.append(PostStep("udev-settle", ["udevadm", "settle", "--timeout=10"],
                                      "wait for udev", order=40))
        return steps

    def plan(self, ctx: Context, opts: dict[str, Any], prev: dict[str, Any]) -> Plan:
        return Plan(files=[FileSpec(self.HWDB, templates.VOLUME_HWDB.encode())], post=self._trigger_steps(ctx))

    def after_apply(self, ctx: Context) -> list[str]:
        ctx.__dict__.pop("atkbd_force_release", None)
        released = ctx.atkbd_force_release
        if released is not None and not templates.VOLUME_SCANCODES <= released:
            return ["The kernel is not forcing volume-key releases yet; reboot to finish volume-keys."]
        return []

    def revert_post(self, ctx: Context) -> list[PostStep]:
        return self._trigger_steps(ctx)

    def revert_messages(self) -> list[str]:
        return ["The kernel keeps forcing volume-key releases until the next reboot."]

    def live_state(self, ctx: Context, entry: Optional[dict[str, Any]]) -> list[str]:
        lines = []
        if entry and ctx.hhd.running:
            lines.append("warning: Handheld Daemon is running and drops forced volume presses; "
                         "`sudo oxp3-tool revert volume-keys`, then reboot")
        released = ctx.atkbd_force_release
        if released is None:
            lines.append("could not read the built-in keyboard's force_release list")
        elif templates.VOLUME_SCANCODES <= released:
            lines.append("kernel is forcing volume-key releases")
        else:
            lines.append("kernel is not forcing volume-key releases" + (" yet (reboot)" if entry else ""))
        return lines


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

    def plan(self, ctx: Context, opts: dict[str, Any], prev: dict[str, Any]) -> Plan:
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

    def plan(self, ctx: Context, opts: dict[str, Any], prev: dict[str, Any]) -> Plan:
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

    def plan(self, ctx: Context, opts: dict[str, Any], prev: dict[str, Any]) -> Plan:
        return Plan(kargs=[self.karg])

    def live_state(self, ctx: Context, entry: Optional[dict[str, Any]]) -> list[str]:
        return _karg_live_state(ctx, self.karg, self.id)


class NvmeResumeFix(_KargFix):
    id = "nvme-resume"
    group = DISPLAY
    level = RECOMMENDED
    title = "SSD survives sleep"
    summary = "nvme.noacpi=1 so the SSD comes back from sleep (black screen, no further sleep)"
    details = (
        "On the OXP3 the firmware asks for ACPI 'simple suspend' on the SSD, and the\n"
        "Acer Predator GM7 (PCI 1dee:1602) then fails to come back: the kernel logs\n"
        "'Disabling device after reset failure: -19', the root file system drops out,\n"
        "the screen stays black and the device will not sleep again until it is powered\n"
        "off. nvme.noacpi=1 turns simple suspend off and fixed resume in community\n"
        "testing on the OXP3. Recommended with the GM7, when the kernel log shows the\n"
        "failure, or when the firmware uses simple suspend for the installed SSD;\n"
        "--any-ssd applies it anyway. Costs a little battery while asleep. Needs a reboot."
    )
    takes_effect = "after reboot"
    karg = "nvme.noacpi=1"

    def _evidence(self, ctx: Context) -> list[str]:
        ev = ctx.resume_evidence
        out = []
        if ctx.has_gm7:
            out.append("the Predator GM7 SSD is installed (the hardware-tested case)")
        if ev.nvme_reset_failure:
            out.append("the kernel log shows the SSD failing to come back from sleep")
        if ev.simple_suspend:
            out.append("the firmware puts the SSD slot into ACPI simple suspend, the path the GM7 fails on; "
                       "only the GM7 is confirmed to fail")
        if ctx.karg_active(self.karg) or ctx.karg_pending(self.karg) \
                or ctx.sys.read_attr("/sys/module/nvme/parameters/noacpi") == "Y":
            # nvme.noacpi=1 hides the firmware's simple-suspend message, so
            # the fix's own effect stands in for that evidence.
            out.append("nvme.noacpi=1 is set, which hides the firmware's simple-suspend request")
        return out

    def _check(self, ctx: Context) -> Check:
        evidence = self._evidence(ctx)
        if evidence:
            return Check(True, "; ".join(evidence), recommended=True)
        return Check(True, "no sign of the SSD sleep problem; apply with --any-ssd if waking from sleep "
                           "leaves a black screen")

    def plan(self, ctx: Context, opts: dict[str, Any], prev: dict[str, Any]) -> Plan:
        any_ssd = bool(opts.get("any_ssd") or prev.get("any_ssd"))
        evidence = self._evidence(ctx) or list(prev.get("evidence") or [])
        if not evidence and not any_ssd:
            raise FixError("no sign of the SSD sleep problem on this unit; pass --any-ssd to apply anyway")
        plan = super().plan(ctx, opts, prev)
        # Remember why, since the fix itself hides the firmware's message.
        if evidence:
            plan.options["evidence"] = list(prev.get("evidence") or evidence)
        if any_ssd:
            plan.options["any_ssd"] = True
        return plan

    def live_state(self, ctx: Context, entry: Optional[dict[str, Any]]) -> list[str]:
        lines = _karg_live_state(ctx, self.karg, self.id)
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
        "48-144 Hz refresh rates; --min-refresh=30 extends that down to 30 Hz (untested,\n"
        "kept when you apply again). Only Game Mode (bazzite-deck images) uses it; it does\n"
        "not change HDR in Desktop Mode, where KDE always sends PQ (keep HDR off there)."
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
        if not ctx.has_game_mode:
            return Check(True, "no gamescope Game Mode session on this image (desktop images run gamescope "
                               "nested, which ignores display profiles)", recommended=False)
        return Check(True, "", recommended=True)

    def plan(self, ctx: Context, opts: dict[str, Any], prev: dict[str, Any]) -> Plan:
        min_refresh = opts.get("min_refresh") or prev.get("min_refresh") or templates.LEGIONGO2_MIN_HZ
        try:
            content = templates.oled_lua(int(min_refresh))
        except (TypeError, ValueError) as e:
            raise FixError(str(e)) from e
        return Plan(
            files=[FileSpec(self.LUA, content.encode())],
            options={"min_refresh": int(min_refresh)},
            messages=["Switch to Desktop Mode and back (or reboot) so gamescope loads the profile."],
        )

    def revert_messages(self) -> list[str]:
        return ["Switch to Desktop Mode and back (or reboot) so gamescope drops the profile."]

    def live_state(self, ctx: Context, entry: Optional[dict[str, Any]]) -> list[str]:
        lines = []
        if entry and entry.get("options", {}).get("min_refresh"):
            lines.append(f"refresh rates {entry['options']['min_refresh']}-{templates.PANEL_MAX_HZ} Hz")
        panel = ctx.internal_panel
        if not panel:
            return lines + ["no eDP connector found"]
        name, edid = panel
        if not edid:
            return lines + [f"{name}: EDID not readable"]
        model = f" ({edid.name})" if edid.name else ""
        lines.append(f"{name}: {edid.vendor} 0x{edid.product:04x}{model}")
        used = ctx.gamescope_known_display
        if used == templates.OLED_DISPLAY_KEY:
            lines.append("Game Mode used this profile this boot")
        elif used:
            lines.append(f"warning: Game Mode used the {used!r} display profile this boot, not this one; "
                         "restart Game Mode")
        return lines


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

    def note(self, ctx: Context) -> list[str]:
        psr = ctx.refresh_state.psr_mode
        return [f"PSR mode now: {psr}"] if psr and psr.startswith("Panel Replay") else []


class XeHdrBacklightFix(_KargFix):
    id = "xe-hdr-backlight"
    group = DISPLAY
    level = EXPERIMENTAL
    title = "Force Intel's panel backlight interface"
    summary = "xe.enable_dpcd_backlight=3 (not shown to fix HDR blanking; n/a on eDP 1.5 panels)"
    details = (
        "Makes the kernel use Intel's eDP HDR backlight interface, which switches the\n"
        "panel into HDR decoding and sets its brightness in nits. Only offered when the\n"
        "panel reports the interface and eDP 1.4b or older (read-only DPCD check, needs\n"
        "root): the kernel never uses it on eDP 1.5 panels, which is what OXP3 panels have\n"
        "reported so far. No report shows it fixing the Desktop Mode HDR black screen,\n"
        "which also happens on other Intel OLED laptops (KDE bug 525650); on a Panther\n"
        "Lake Dell XPS 14 OLED it left brightness dead. The kernel log hint suggesting\n"
        "enable_dpcd_backlight=3 is not a reliable sign that it helps.\n"
        "Untested on the OXP3: HDR brightness moves to the panel's AUX channel, and SDR\n"
        "brightness too if the panel reports 'SDR brightness over AUX' (otherwise it stays\n"
        "on the SoC's PWM output, and a slider that works today may stop working). Before\n"
        "trying it, run `ujust configure-grub show` so the boot menu can start the previous\n"
        "version if the screen stays dark (you need a keyboard there), or keep SSH ready.\n"
        "Test in Desktop Mode (System Settings > Display > HDR). Game Mode keeps HDR\n"
        "tone-mapped while oled-profile is applied. Needs a reboot."
    )
    takes_effect = "after reboot"
    karg = "xe.enable_dpcd_backlight=3"

    def _check(self, ctx: Context) -> Check:
        other = [k for k in ctx.active_kargs + (ctx.pending_kargs or [])
                 if k.startswith("xe.enable_dpcd_backlight=") and k != self.karg]
        if other:
            return Check(False, f"{other[0]} is already set")
        caps, why = ctx.edp_probe
        if caps is None:
            if ctx.sys.is_real_root and os.geteuid() != 0:
                return Check(True, "run `sudo oxp3-tool status` to check whether the panel supports this")
            return Check(False, f"could not check the panel: {why}")
        if not caps.intel_hdr_backlight:
            return Check(False, f"the panel reports {caps.describe()}; forcing the interface would change nothing")
        if not caps.kernel_can_use_intel_hdr:
            return Check(False, f"the panel reports {caps.describe()}; the kernel uses the VESA interface there")
        return Check(True, f"the panel reports {caps.describe()}; untested, not shown to fix HDR")

    def live_state(self, ctx: Context, entry: Optional[dict[str, Any]]) -> list[str]:
        lines = _karg_live_state(ctx, self.karg, self.id)
        caps = ctx.edp_caps
        if caps is not None:
            lines.append(f"panel: {caps.describe()}")
        return lines


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

    def note(self, ctx: Context) -> list[str]:
        errors = ctx.refresh_state.dsb_errors
        return [f"the kernel logged {errors} 'DSB poll error' messages this boot"] if errors else []


ALL_FIXES: list[Fix] = [
    ButtonsFix(),
    XboxKeyFix(),
    VolumeKeysFix(),
    GyroFix(),
    InputPlumberSuspendFix(),
    NvmeResumeFix(),
    OledProfileFix(),
    XePanelReplayFix(),
    XeHdrBacklightFix(),
    XeDsbFix(),
]

BY_ID = {f.id: f for f in ALL_FIXES}
