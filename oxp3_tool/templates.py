"""Contents of the files the tool installs."""

from __future__ import annotations

import inspect
from typing import Optional

from . import PROJECT_URL

# First-line marker of every file this tool writes.
OWN_MARKER = "Installed by oxp3-tool ("

# -- InputPlumber capability map --------------------------------------------

# Where an action has to go depends on how Game Mode feeds Steam:
# - native Steam Input loads InputPlumber's default profile, which turns
#   gamepad Keyboard into Guide+North (Steam+X, the on-screen keyboard) and
#   QuickAccess2 into a screenshot;
# - the OpenGamepadUI overlay (Bazzite's default on non-Valve handhelds) loads
#   its own profile instead: Keyboard goes to OGUI and is lost on the Steam
#   Deck target, QuickAccess2 opens the OGUI menu, and LeftTop becomes
#   Guide+North, so the keyboard action is sent as LeftTop there.
# The Steam Deck target turns Screenshot into Steam+R1 by itself in both.
# With native Steam Input the keyboard action does not use Steam+X at all
# (it does nothing on some setups): it becomes the InputPlumber D-Bus action
# OSK_DBUS_ACTION, which the OSK helper (osk_helper.py) turns into
# steam://open/keyboard. "keyboard-chord" keeps the Steam+X route.
OGUI_SESSION = "ogui"
OSK_ACTION = "keyboard"
OSK_DBUS_ACTION = "ui_osk"

# User-facing action name -> (button, button under the OGUI overlay, description)
BUTTON_ACTIONS: dict[str, tuple[str, str, str]] = {
    "guide": ("Guide", "Guide", "Steam button (opens the Steam menu)"),
    "quick-access": ("QuickAccess", "QuickAccess", "Quick Access menu (the '...' button)"),
    OSK_ACTION: ("Keyboard", "LeftTop", "Steam on-screen keyboard (native Steam Input: through oxp3-tool's helper)"),
    "keyboard-chord": ("Keyboard", "LeftTop", "Steam on-screen keyboard through the Steam+X shortcut (no helper)"),
    "screenshot": ("Screenshot", "Screenshot", "Steam screenshot"),
    "ogui-menu": ("QuickAccess2", "QuickAccess2",
                  "OpenGamepadUI menu (with native Steam Input this takes a screenshot instead)"),
}


def uses_osk_helper(action: str, session: Optional[str]) -> bool:
    return action == OSK_ACTION and session != OGUI_SESSION


def action_target(action: str, session: Optional[str]) -> tuple[str, str]:
    """("gamepad", button) or ("dbus", action) that ``action`` is sent as."""
    if uses_osk_helper(action, session):
        return ("dbus", OSK_DBUS_ACTION)
    native, ogui, _ = BUTTON_ACTIONS[action]
    return ("gamepad", ogui if session == OGUI_SESSION else native)


def _target_lines(target: tuple[str, str]) -> list[str]:
    kind, value = target
    if kind == "dbus":
        return ["    target_event:", f"      dbus: {value}"]
    return ["    target_event:", "      gamepad:", f"        button: {value}"]


# The four face keys, what InputPlumber 0.82.0's stock map "oxp9" does with
# them, and this tool's defaults.
BUTTON_KEYS = {
    "onex": "ONEX / console key (sends Ctrl+Alt+Meta)",
    "keyboard": "keyboard key (sends Ctrl+Meta+O)",
    "home": "home key (MCU button 0x24, decoded by InputPlumber as gamepad Keyboard)",
    "xbox": "Xbox key (arrives as the gamepad Guide button)",
}
STOCK_BUTTON_ACTIONS = {"onex": "quick-access", "keyboard": "ogui-menu", "home": "keyboard-chord", "xbox": "guide"}
DEFAULT_BUTTON_ACTIONS = {"onex": "quick-access", "keyboard": "keyboard", "home": "guide", "xbox": "guide"}

_CHORDS = {
    "onex": ("ONEX", ["KeyLeftCtrl", "KeyLeftAlt", "KeyLeftMeta"]),
    "keyboard": ("Keyboard", ["KeyLeftCtrl", "KeyLeftMeta", "KeyO"]),
}
# Native events of the other two keys.
_NATIVE = {"home": "Keyboard", "xbox": "Guide"}


def capability_map_yaml(map_id: str, actions: dict[str, str], upstream_path: Optional[str],
                        upstream_sha256: Optional[str], session: Optional[str] = None) -> str:
    for key, action in actions.items():
        if key not in BUTTON_KEYS:
            raise ValueError(f"unknown key {key!r}")
        if action not in BUTTON_ACTIONS:
            raise ValueError(f"unknown action {action!r} for {key}")
    mode = "OpenGamepadUI overlay" if session == OGUI_SESSION else "native Steam Input"
    lines = [
        f"# Installed by oxp3-tool ({PROJECT_URL}).",
        "# Do not edit: change it with `sudo oxp3-tool apply buttons --home=... --keyboard-key=... --onex-key=...",
        "# --xbox-key=...` and remove it with `sudo oxp3-tool revert buttons`.",
        "#",
        f"# Replaces InputPlumber's stock capability map {map_id!r} for the ONEXPLAYER 3.",
        "# A map in /etc/inputplumber/capability_maps.d with the same file name and id",
        "# is loaded after the stock one and takes its place.",
        f"# Written for Game Mode with {mode}; apply again after switching.",
        "# " + ", ".join(f"{k}: {actions.get(k, DEFAULT_BUTTON_ACTIONS[k])}" for k in BUTTON_KEYS),
    ]
    if any(uses_osk_helper(a, session) for a in actions.values()):
        lines.append(f"# dbus: {OSK_DBUS_ACTION} goes to {OSK_KEY_UNIT_NAME}, which asks Steam to open its keyboard.")
    if upstream_path:
        lines.append(f"# Stock map: {upstream_path}" + (f" (sha256 {upstream_sha256})" if upstream_sha256 else ""))
    lines += [
        "version: 1",
        "kind: CapabilityMap",
        "name: OneXPlayer 3 (oxp3-tool)",
        f"id: {map_id}",
        "mapping:",
    ]
    for key in ("onex", "keyboard"):
        name, chord = _CHORDS[key]
        lines.append(f"  - name: {name}")
        lines.append("    source_events:")
        lines += [f"      - keyboard: {k}" for k in chord]
        lines += _target_lines(action_target(actions[key], session))
    # The home key already arrives as gamepad Keyboard and the Xbox key as
    # gamepad Guide. Translated events are not translated again, so these do
    # not touch the chords above (or each other).
    for name, key in (("Home", "home"), ("Xbox", "xbox")):
        native = _NATIVE[key]
        target = action_target(actions.get(key, DEFAULT_BUTTON_ACTIONS[key]), session)
        if target != ("gamepad", native):
            lines += [
                f"  - name: {name}",
                "    source_events:",
                "      - gamepad:",
                f"          button: {native}",
            ]
            lines += _target_lines(target)
    lines.append("filtered_events: []")
    return "\n".join(lines) + "\n"


# -- udev hwdb: volume rocker ------------------------------------------------

VOLUME_HWDB = f"""\
# Installed by oxp3-tool ({PROJECT_URL}).
# Remove with `sudo oxp3-tool revert volume-keys`.
#
# ONEXPLAYER 3: the embedded controller drops the key-release scancodes of the
# volume rocker, so volume keeps changing after the key is let go. The '!'
# prefix marks the keys as force-release: the kernel sends the release right
# after the press. Holding a volume key no longer auto-repeats.
evdev:atkbd:dmi:bvn*:bvr*:bd*:svnONE-NETBOOK:pnONEXPLAYER3:*
 KEYBOARD_KEY_ae=!volumedown
 KEYBOARD_KEY_b0=!volumeup
"""

# Scancodes 0xae and 0xb0 as listed in atkbd's force_release attribute.
VOLUME_SCANCODES = {0xAE, 0xB0}


# -- Xbox key helper -------------------------------------------------------------

XBOX_KEY_HELPER_PATH = "/usr/local/libexec/oxp3-tool/xbox-key-helper"
XBOX_KEY_UNIT_NAME = "oxp3-tool-xbox-key.service"
XBOX_KEY_UNIT_PATH = f"/etc/systemd/system/{XBOX_KEY_UNIT_NAME}"


def xbox_key_helper_script() -> str:
    """The standalone helper script: oxp3_tool/xbox_helper.py behind a shebang and our marker."""
    from . import xbox_helper

    return (
        "#!/usr/bin/python3 -I\n"
        f"# Installed by oxp3-tool ({PROJECT_URL}).\n"
        "# Remove with `sudo oxp3-tool revert xbox-key`.\n"
        + inspect.getsource(xbox_helper)
    )


XBOX_KEY_UNIT = f"""\
# Installed by oxp3-tool ({PROJECT_URL}).
# Remove with `sudo oxp3-tool revert xbox-key`.
#
# Reads the ONEXPLAYER 3 controller's vendor interface (read-only) and sends the
# Steam button to InputPlumber when the Xbox key reports as button 0x25.
[Unit]
Description=ONEXPLAYER 3 Xbox key as the Steam button (oxp3-tool)
After=inputplumber.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 -I {XBOX_KEY_HELPER_PATH}
Restart=always
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
"""


# -- on-screen keyboard helper ------------------------------------------------

OSK_KEY_HELPER_PATH = "/usr/local/libexec/oxp3-tool/osk-key-helper"
OSK_KEY_UNIT_NAME = "oxp3-tool-osk-key.service"
OSK_KEY_UNIT_PATH = f"/etc/systemd/system/{OSK_KEY_UNIT_NAME}"


def osk_key_helper_script() -> str:
    """The standalone helper script: oxp3_tool/osk_helper.py behind a shebang and our marker."""
    from . import osk_helper

    return (
        "#!/usr/bin/python3 -I\n"
        f"# Installed by oxp3-tool ({PROJECT_URL}).\n"
        "# Remove with `sudo oxp3-tool revert buttons`, or choose another keyboard action with\n"
        "# `sudo oxp3-tool apply buttons --keyboard-key=...`.\n"
        + inspect.getsource(osk_helper)
    )


# ProtectHome=read-only, not yes: the helper writes to the FIFO
# ~/.steam/steam.pipe (allowed on a read-only mount), and on Bazzite /home is
# a symlink to /var/home, which ProtectHome=yes also hides.
OSK_KEY_UNIT = f"""\
# Installed by oxp3-tool ({PROJECT_URL}).
# Remove with `sudo oxp3-tool revert buttons`.
#
# Opens Steam's on-screen keyboard when InputPlumber sends the D-Bus action
# {OSK_DBUS_ACTION} (the ONEXPLAYER 3 keyboard key with native Steam Input).
[Unit]
Description=ONEXPLAYER 3 keyboard key opens Steam's on-screen keyboard (oxp3-tool)
After=dbus.service inputplumber.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 -I {OSK_KEY_HELPER_PATH}
Restart=always
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
"""


# Table key of oxp3-tool's known display in the gamescope Lua (gamescope logs
# "Got known display: <key> (<pretty name>)" when it picks one).
OLED_DISPLAY_KEY = "oxp3_tool_sdc_oled"


# -- modprobe: IMU -------------------------------------------------------------

IMU_MODPROBE = f"""\
# Installed by oxp3-tool ({PROJECT_URL}).
# Remove with `sudo oxp3-tool revert gyro` and reboot.
#
# ONEXPLAYER 3: the BMI260 IMU sits behind ACPI ID 10EC5280, which bmi160_i2c
# also claims and cannot drive. Keeping bmi160_i2c from loading leaves the
# device to bmi270_i2c, which exposes it as IIO device "bmi260".
blacklist bmi160_i2c
"""


# -- gamescope display profile ---------------------------------------------------

# Vertical front porch per refresh rate, copied from gamescope's
# scripts/00-gamescope/displays/lenovo.legiongo2.oled.lua (same panel,
# BSD-2-Clause, Copyright Valve Corporation and contributors).
LEGIONGO2_VFPS = [
    2696, 2615, 2538, 2463, 2391, 2322, 2256, 2192, 2130, 2071,
    2013, 1958, 1904, 1852, 1802, 1753, 1706, 1660, 1616, 1572,
    1531, 1491, 1451, 1413, 1376, 1340, 1305, 1270, 1237, 1205,
    1173, 1142, 1112, 1083, 1054, 1026, 999, 972, 946, 921,
    896, 872, 848, 825, 802, 780, 758, 737, 716, 696,
    676, 656, 637, 618, 600, 581, 564, 546, 529, 512,
    496, 480, 464, 448, 433, 418, 403, 389, 375, 361,
    347, 333, 320, 307, 294, 281, 269, 257, 245, 233,
    221, 209, 198, 187, 176, 165, 155, 144, 134, 123,
    113, 103, 94, 84, 75, 65, 56,
]
LEGIONGO2_MIN_HZ = 48
PANEL_MAX_HZ = 144
PANEL_MIN_HZ = 30


def panel_vfp(refresh: int) -> int:
    """Front porch for ``refresh`` Hz at the panel's fixed 380.16 MHz clock.

    The native modes (60 Hz and 144 Hz) share htotal 2000 and 1264 lines of
    vdisplay + sync + back porch, so vtotal = clock / (2000 * refresh).
    Reproduces the gamescope table above for every rate except 67 Hz, where
    that table rounds down.
    """
    if LEGIONGO2_MIN_HZ <= refresh <= PANEL_MAX_HZ:
        return LEGIONGO2_VFPS[refresh - LEGIONGO2_MIN_HZ]
    if not PANEL_MIN_HZ <= refresh <= PANEL_MAX_HZ:
        raise ValueError(f"refresh {refresh} out of range")
    return round(380_160_000 / (2000 * refresh)) - 1264


def oled_lua(min_refresh: int = LEGIONGO2_MIN_HZ) -> str:
    if not PANEL_MIN_HZ <= min_refresh <= LEGIONGO2_MIN_HZ:
        raise ValueError(f"min_refresh must be between {PANEL_MIN_HZ} and {LEGIONGO2_MIN_HZ}")
    rates = list(range(min_refresh, PANEL_MAX_HZ + 1))
    rate_rows = []
    for i in range(0, len(rates), 10):
        rate_rows.append("    " + ", ".join(str(r) for r in rates[i:i + 10]) + ",")
    vfp_rows = []
    for i in range(0, len(rates), 6):
        vfp_rows.append("        " + " ".join(f"[{r}] = {panel_vfp(r)}," for r in rates[i:i + 6]))
    rates_lua = "\n".join(rate_rows)
    vfps_lua = "\n".join(vfp_rows)
    return f"""\
-- Installed by oxp3-tool ({PROJECT_URL}).
-- Remove with `sudo oxp3-tool revert oled-profile`, then re-enter Game Mode.
--
-- ONEXPLAYER 3 OLED panel (Samsung AMS881KB01-0, EDID SDC 0x4301).
-- gamescope's bundled lenovo.legiongo2.oled.lua matches the same panel with
-- priority 5000 and drives it with BT.2020/PQ while HDR is on. Under the
-- Intel xe driver the OXP3 panel then stays black, including after resume,
-- and the brightness slider stops working. This profile wins with priority
-- 5100 and keeps the panel in its native gamma 2.2 mode, the way the Steam
-- Deck OLED works: gamescope tone-maps HDR content itself and never sends
-- PQ metadata to the panel.

local oxp3_tool_refresh_rates = {{
{rates_lua}
}}

gamescope.config.known_displays.{OLED_DISPLAY_KEY} = {{
    pretty_name = "ONEXPLAYER 3 OLED (oxp3-tool)",
    hdr = {{
        supported = true,
        eotf = gamescope.eotf.gamma22,
        max_content_light_level = 1100,
        max_frame_average_luminance = 475,
        min_content_light_level = 0.0005,
    }},
    dynamic_refresh_rates = oxp3_tool_refresh_rates,
    dynamic_modegen = function(base_mode, refresh)
        debug("Generating mode "..refresh.."Hz for ONEXPLAYER 3 OLED")
        -- Fixed pixel clock; only the vertical front porch changes.
        local vfps = {{
{vfps_lua}
        }}
        local vfp = vfps[refresh]
        if vfp == nil then
            warn("Couldn't do refresh "..refresh.." on ONEXPLAYER 3 OLED")
            return base_mode
        end
        local mode = base_mode
        gamescope.modegen.adjust_front_porch(mode, vfp)
        mode.vrefresh = gamescope.modegen.calc_vrefresh(mode)
        return mode
    end,
    matches = function(display)
        if display.vendor == "SDC" and display.product == 0x4301 then
            debug("[{OLED_DISPLAY_KEY}] Matched vendor: "..display.vendor.." product: "..display.product)
            return 5100
        end
        return -1
    end,
}}
debug("Registered ONEXPLAYER 3 OLED (oxp3-tool) as a known display")
"""
