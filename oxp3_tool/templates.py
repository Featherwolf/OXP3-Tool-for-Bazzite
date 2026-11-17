"""Contents of the files the tool installs."""

from __future__ import annotations

import inspect
import json
import re
from types import ModuleType
from typing import Optional

from . import PROJECT_URL, cdp, steam_session, steam_ui_js

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
# Some actions are not a button at all: the capability map sends them as an
# InputPlumber D-Bus action (steam_session.DBUS_ACTIONS), which the Steam
# actions helper (steam_actions.py) carries out in the Steam user's session. With native
# Steam Input that includes the keyboard action, because Steam+X does nothing
# on some setups; "keyboard-chord" keeps the Steam+X route.
OGUI_SESSION = "ogui"
OSK_ACTION = "keyboard"
HOME_ACTION = "home"
FPS_ACTION = "fps-limit"
DBUS_ACTIONS = steam_session.DBUS_ACTIONS

# User-facing action name -> (button, button under the OGUI overlay, description)
BUTTON_ACTIONS: dict[str, tuple[str, str, str]] = {
    "guide": ("Guide", "Guide", "Steam button (opens the Steam menu; hold it for Steam+button shortcuts)"),
    "quick-access": ("QuickAccess", "QuickAccess", "Quick Access menu (the '...' button)"),
    OSK_ACTION: ("Keyboard", "LeftTop", "Steam on-screen keyboard (native Steam Input: through oxp3-tool's helper; "
                                         "a second press closes it in Game Mode with Decky Loader)"),
    "keyboard-chord": ("Keyboard", "LeftTop", "Steam on-screen keyboard through the Steam+X shortcut (no helper)"),
    HOME_ACTION: ("", "", "Steam Home screen (through oxp3-tool's helper)"),
    FPS_ACTION: ("", "", "turn the frame limit on or off (--fps-limit, default 72; Steam's own with Decky Loader; "
                         "through oxp3-tool's helper)"),
    "screenshot": ("Screenshot", "Screenshot", "Steam screenshot"),
    "ogui-menu": ("QuickAccess2", "QuickAccess2",
                  "OpenGamepadUI menu (with native Steam Input this takes a screenshot instead)"),
}


def uses_helper(action: str, session: Optional[str]) -> bool:
    """Whether ``action`` goes to the Steam actions helper in this Game Mode session."""
    if action == OSK_ACTION:
        return session != OGUI_SESSION
    return action in (HOME_ACTION, FPS_ACTION)


def action_target(action: str, session: Optional[str]) -> tuple[str, str]:
    """("gamepad", button) or ("dbus", action) that ``action`` is sent as."""
    if uses_helper(action, session):
        return ("dbus", DBUS_ACTIONS[action])
    native, ogui, _ = BUTTON_ACTIONS[action]
    return ("gamepad", ogui if session == OGUI_SESSION else native)


def _target_lines(target: tuple[str, str]) -> list[str]:
    kind, value = target
    if kind == "dbus":
        return ["    target_event:", f"      dbus: {value}"]
    return ["    target_event:", "      gamepad:", f"        button: {value}"]


# The four face keys, what InputPlumber 0.82.0's stock map "oxp9" does with
# them, and this tool's defaults. The stock map is InputPlumber's (https://github.com/ShadowBlip/InputPlumber)
# rootfs/usr/share/inputplumber/capability_maps/onexplayer_type9.yaml; the key chords are the ones it lists,
# checked with evtest (see also InputPlumber issue #693).
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
                        upstream_sha256: Optional[str], session: Optional[str] = None,
                        xbox_by_helper: bool = False) -> str:
    """The override map. With ``xbox_by_helper`` the Xbox key gets no entry: InputPlumber cannot read it
    there (vendor button 0x25), so the xbox-key helper does its action and an entry would never fire."""
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
    if any(uses_helper(a, session) for a in actions.values()):
        lines.append(f"# dbus: targets go to {STEAM_ACTIONS_UNIT_NAME}, which carries them out in Steam.")
    if xbox_by_helper:
        lines.append(f"# The Xbox key reports as button 0x25 here: {XBOX_KEY_UNIT_NAME} handles it.")
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
        if key == "xbox" and xbox_by_helper:
            continue
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

# The problem and the force-release fix: https://github.com/hhd-dev/hhd/issues/342. The same three data lines are in
# ZeroSegFault's https://github.com/ZeroSegFault/CachyOS-OneXPlayer3 (files/etc/udev/hwdb.d/61-oxp3-volume.hwdb).
# The format and the '!' prefix are systemd's (hwdb.d/60-keyboard.hwdb).
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


# -- helper scripts ------------------------------------------------------------

_RELATIVE_IMPORT_RE = re.compile(r"^from \.\w+ import [^\n(]+$", re.M)
_FUTURE = "from __future__ import annotations\n"


def _bundle(remove_with: str, *modules: ModuleType) -> str:
    """One standalone script from ``modules`` (dependencies first) behind a shebang and our marker."""
    parts = []
    for module in modules:
        source = inspect.getsource(module).replace(_FUTURE, "", 1)
        parts.append(_RELATIVE_IMPORT_RE.sub("", source))
    return (
        "#!/usr/bin/python3 -I\n"
        f"# Installed by oxp3-tool ({PROJECT_URL}).\n"
        f"# {remove_with}\n"
        + _FUTURE
        + "\n".join(parts)
    )


XBOX_KEY_HELPER_PATH = "/usr/local/libexec/oxp3-tool/xbox-key-helper"
XBOX_KEY_UNIT_NAME = "oxp3-tool-xbox-key.service"
XBOX_KEY_UNIT_PATH = f"/etc/systemd/system/{XBOX_KEY_UNIT_NAME}"


def xbox_key_helper_script() -> str:
    from . import xbox_helper

    return _bundle("Remove with `sudo oxp3-tool revert xbox-key`.", cdp, steam_ui_js, steam_session, xbox_helper)


# ProtectHome=read-only, not yes, for helpers that act in the Steam user's
# session: they write to the FIFO ~/.steam/steam.pipe (allowed on a read-only
# mount) and connect to gamescope's socket in /run/user, and on Bazzite /home
# is a symlink to /var/home, which ProtectHome=yes also hides.
XBOX_KEY_UNIT = f"""\
# Installed by oxp3-tool ({PROJECT_URL}).
# Remove with `sudo oxp3-tool revert xbox-key`.
#
# Reads the ONEXPLAYER 3 controller's vendor interface (read-only) and, when the
# Xbox key reports as button 0x25, does what `oxp3-tool apply buttons --xbox-key`
# chose (the Steam button by default).
[Unit]
Description=ONEXPLAYER 3 Xbox key helper (oxp3-tool)
After=inputplumber.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 -I {XBOX_KEY_HELPER_PATH}
Restart=always
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
"""


STEAM_ACTIONS_HELPER_PATH = "/usr/local/libexec/oxp3-tool/steam-actions-helper"
STEAM_ACTIONS_UNIT_NAME = "oxp3-tool-steam-actions.service"
STEAM_ACTIONS_UNIT_PATH = f"/etc/systemd/system/{STEAM_ACTIONS_UNIT_NAME}"


def steam_actions_helper_script() -> str:
    from . import steam_actions

    return _bundle("Remove with `sudo oxp3-tool revert buttons`, or give the keys other actions with "
                   "`sudo oxp3-tool apply buttons ...`.", cdp, steam_ui_js, steam_session, steam_actions)


STEAM_ACTIONS_UNIT = f"""\
# Installed by oxp3-tool ({PROJECT_URL}).
# Remove with `sudo oxp3-tool revert buttons`.
#
# Carries out the InputPlumber D-Bus actions the ONEXPLAYER 3 keys are mapped to
# ({", ".join(sorted(DBUS_ACTIONS.values()))}): Steam's on-screen keyboard, its Home
# screen, the frame limit.
[Unit]
Description=ONEXPLAYER 3 keys: Steam keyboard, Home screen and frame limit (oxp3-tool)
After=dbus.service inputplumber.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 -I {STEAM_ACTIONS_HELPER_PATH}
Restart=always
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
"""

KEYS_CONFIG_PATH = steam_session.KEYS_CONFIG


def keys_config_json(actions: dict[str, str], session: Optional[str], fps_limit: int) -> str:
    """Settings the helpers read on each press: the Xbox key's action (for the 0x25 helper) and the frame limit."""
    return json.dumps({
        "_comment": f"Installed by oxp3-tool ({PROJECT_URL}). Change it with `sudo oxp3-tool apply buttons`.",
        "xbox": actions["xbox"], "session": session, "fps_limit": fps_limit,
    }, indent=2, sort_keys=True) + "\n"


# Units earlier releases installed for the same job.
LEGACY_STEAM_ACTIONS_UNITS = ("oxp3-tool-osk-key.service",)


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

# The profile oled_lua() writes is modelled on gamescope's scripts/00-gamescope/displays/lenovo.legiongo2.oled.lua
# (https://github.com/ValveSoftware/gamescope, master 2b5cf191; same panel; BSD-2-Clause, Copyright (c) 2013-2022,
# Valve Corporation; Copyright (c) 2022, NVIDIA CORPORATION) and on the HDR profile in oxp3-apply-fixes.sh of
# https://github.com/HHHHanasak1/onexplayer3-steamos-setup (fix pack 20261015; MIT, Copyright (c) 2026 HANA & Claude).
# LEGIONGO2_VFPS is gamescope's vertical front porch per refresh rate, unchanged.
# Both licences in full: THIRD_PARTY_NOTICES.md (also packed into dist/oxp3-tool).
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
    vdisplay + sync + back porch, so vtotal = clock / (2000 * refresh), the
    formula of the fix pack's profile (see the comment above LEGIONGO2_VFPS).
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


# -- PipeWire speaker profile ----------------------------------------------------

# A filter-chain that only the OXP3's built-in speaker output passes through: WirePlumber's smart-filter policy
# (0.5 and newer) puts it in front of the node named by filter.smart.target whenever a stream is about to link to
# that node, so headphones, HDMI and Bluetooth never pass through it and the speaker stays the default output.
# Only PipeWire's builtin filter-graph plugin is used (PipeWire 1.4 and newer, on Bazzite and CachyOS alike): it has
# no limiter, so the look-ahead peak limiter below is built from its abs, max, delay, bq_lowpass, sine, recip and
# mult nodes. The syntax follows PipeWire's module-filter-chain documentation and WirePlumber's smart-filter
# documentation (docs/rst/policies/smart_filters.rst); no file of another project is copied.
SPEAKER_PROFILE_PATH = "/etc/pipewire/pipewire.conf.d/80-oxp3-speaker-profile.conf"
SPEAKER_PROFILE_NAME = SPEAKER_PROFILE_PATH.rsplit("/", 1)[-1]
# The OXP3's speaker sink (SOF, skl_hda_dsp_generic, UCM HiFi profile), as PipeWire names it on Bazzite and CachyOS.
SPEAKER_NODE_DEFAULT = "alsa_output.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__Speaker__sink"
SPEAKER_NODE_SUFFIX = ".HiFi__Speaker__sink"
# Built-in (PCI) cards only: a USB device with a UCM "Speaker" output (some docks and USB codecs) is not the OXP3's.
SPEAKER_NODE_RE = re.compile(r"alsa_output\.pci-[A-Za-z0-9_.:-]+\.HiFi__Speaker__sink")
SPEAKER_FILTER_NODE = "oxp3_speaker_profile"
SPEAKER_FILTER_OUTPUT = f"{SPEAKER_FILTER_NODE}.output"
SPEAKER_FILTER_SMART_NAME = "oxp3-speaker-profile"
SPEAKER_FILTER_DESCRIPTION = "OXP3 Speakers"

SPEAKER_RATE_REF = 48000  # the rate the delays are rounded for (the OXP3's SOF card runs at 48 kHz)
SPEAKER_HIGHPASS_HZ = 180.0
# Two biquads with these Qs make a 4th-order Butterworth high-pass.
SPEAKER_HIGHPASS_QS = (0.5412, 1.3066)
# (label, Hz, Q, dB): a little body, presence and air.
SPEAKER_EQ = (("bq_peaking", 300.0, 0.9, 2.0), ("bq_peaking", 3000.0, 1.0, 2.5), ("bq_highshelf", 8000.0, 0.707, 2.0))
SPEAKER_MAKEUP_DB = 8.0
SPEAKER_LIMIT_DB = -1.5  # the limiter's threshold
SPEAKER_CEILING_DB = -0.3  # the hard clamp after it, a backstop that should never engage
SPEAKER_LOOKAHEAD = 640  # samples at 48 kHz (13.3 ms)
# Running maximum over the last 41.8 ms (more than the look-ahead plus the smoother's rise) as max+delay stages:
# (step, taps) in samples at SPEAKER_HOLD_RATE_REF. PipeWire turns a delay in seconds into whole samples at the rate
# the graph runs at, so the taps sit on a 192 kHz grid: then the stages cover every sample of the window without gaps
# at any rate up to 192 kHz (on a 48 kHz grid, 88.2 and 96 kHz leave every other sample out of the hold, and the
# limiter lets peaks through to the hard ceiling).
SPEAKER_HOLD_RATE_REF = 192000
SPEAKER_HOLD_STAGES = ((1, 7), (6, 7), (36, 7), (216, 7), (1296, 6))
SPEAKER_ENVELOPE_HZ = 60.0  # critically damped smoothing of the held peak (Q 0.5: no overshoot)


def _db(db: float) -> float:
    return 10 ** (db / 20)


def _delay_s(samples: int, ref: int = SPEAKER_RATE_REF) -> str:
    # +0.5 so the filter's (uint32)(delay * rate) lands on the intended sample.
    return f"{(samples + 0.5) / ref:.9f}"


def speaker_profile_graph() -> tuple[list[dict], list[tuple[str, str]]]:
    """The filter graph as data: nodes ({name, label, control, config}) and links (output port, input port)."""
    nodes: list[dict] = []
    links: list[tuple[str, str]] = []

    def node(name: str, label: str, control: Optional[dict] = None, config: Optional[dict] = None) -> None:
        nodes.append({"name": name, "label": label, "control": control or {}, "config": config or {}})

    def delay(name: str, samples: int, ref: int = SPEAKER_RATE_REF) -> None:
        node(name, "delay", {"Delay (s)": _delay_s(samples, ref)}, {"max-delay": f"{(samples + 64) / ref:.6f}"})

    threshold, ceiling, makeup = _db(SPEAKER_LIMIT_DB), _db(SPEAKER_CEILING_DB), _db(SPEAKER_MAKEUP_DB)
    for ch in ("l", "r"):
        prev = ""
        for i, q in enumerate(SPEAKER_HIGHPASS_QS, 1):
            node(f"hp{i}_{ch}", "bq_highpass", {"Freq": SPEAKER_HIGHPASS_HZ, "Q": q})
            if prev:
                links.append((f"{prev}:Out", f"hp{i}_{ch}:In"))
            prev = f"hp{i}_{ch}"
        for i, (label, freq, q, gain) in enumerate(SPEAKER_EQ, 1):
            node(f"eq{i}_{ch}", label, {"Freq": freq, "Q": q, "Gain": gain})
            links.append((f"{prev}:Out", f"eq{i}_{ch}:In"))
            prev = f"eq{i}_{ch}"
        node(f"gain_{ch}", "linear", {"Mult": f"{makeup:.6f}", "Add": 0.0})
        links.append((f"{prev}:Out", f"gain_{ch}:In"))
        node(f"abs_{ch}", "abs")
        links.append((f"gain_{ch}:Out", f"abs_{ch}:In"))
        delay(f"look_{ch}", SPEAKER_LOOKAHEAD)
        links.append((f"gain_{ch}:Out", f"look_{ch}:In"))
        node(f"vca_{ch}", "mult")
        links.append((f"look_{ch}:Out", f"vca_{ch}:In 1"))
        node(f"ceil_{ch}", "clamp", {"Min": f"{-ceiling:.6f}", "Max": f"{ceiling:.6f}"})
        links.append((f"vca_{ch}:Out", f"ceil_{ch}:In"))
    # One detector for both channels (stereo-linked, so the image does not shift): max(|L|, |R|) ...
    node("peak0", "max")
    links += [("abs_l:Out", "peak0:In 1"), ("abs_r:Out", "peak0:In 2")]
    # ... held at its maximum over the last 41.8 ms ...
    # PipeWire 1.4's max has two inputs (1.6's has eight), so each stage is a tree of two-input max nodes.
    src = "peak0"
    for s, (step, taps) in enumerate(SPEAKER_HOLD_STAGES, 1):
        level = [src]
        for k in range(1, taps):
            name = f"hold{s}_d{k}"
            delay(name, step * k, SPEAKER_HOLD_RATE_REF)
            links.append((f"{src}:Out", f"{name}:In"))
            level.append(name)
        m = 0
        while len(level) > 1:
            nxt = []
            for a, b in zip(level[::2], level[1::2], strict=False):  # an odd one out goes up a level
                m += 1
                name = f"hold{s}" if len(level) == 2 else f"hold{s}_m{m}"
                node(name, "max")
                links += [(f"{a}:Out", f"{name}:In 1"), (f"{b}:Out", f"{name}:In 2")]
                nxt.append(name)
            level = nxt + level[len(level) // 2 * 2:]
        src = level[0]
    # ... smoothed, never below the threshold, so gain = threshold / envelope <= 1 (exactly 1 below it).
    node("env", "bq_lowpass", {"Freq": SPEAKER_ENVELOPE_HZ, "Q": 0.5})
    links.append((f"{src}:Out", "env:In"))
    node("thresh", "sine", {"Freq": 0.0, "Ampl": 0.0, "Offset": f"{threshold:.6f}"})  # a constant
    node("envt", "max")
    links += [("env:Out", "envt:In 1"), ("thresh:Out", "envt:In 2")]
    node("inv", "recip")
    links.append(("envt:Out", "inv:In"))
    for ch in ("l", "r"):
        links += [("inv:Out", f"vca_{ch}:In 2"), ("thresh:Out", f"vca_{ch}:In 3")]
    return nodes, links


def _spa_values(values: dict) -> str:
    return " ".join(f'"{k}" = {v}' for k, v in values.items())


def speaker_profile_conf(speaker_node: str = SPEAKER_NODE_DEFAULT) -> str:
    """The PipeWire drop-in of the speaker-profile fix, for the speaker sink named ``speaker_node``."""
    if not SPEAKER_NODE_RE.fullmatch(speaker_node):
        raise ValueError(f"not an ALSA speaker sink name: {speaker_node!r}")
    nodes, links = speaker_profile_graph()
    node_lines = []
    for n in nodes:
        line = f"                    {{ type = builtin name = {n['name']} label = {n['label']}"
        if n["config"]:
            line += f" config = {{ {_spa_values(n['config'])} }}"
        if n["control"]:
            line += f" control = {{ {_spa_values(n['control'])} }}"
        node_lines.append(line + " }")
    link_lines = [f'                    {{ output = "{o}" input = "{i}" }}' for o, i in links]
    lookahead_ms = SPEAKER_LOOKAHEAD * 1000 / SPEAKER_RATE_REF
    nl = "\n"
    return f"""\
# Installed by oxp3-tool ({PROJECT_URL}).
# Do not edit: remove it with `sudo oxp3-tool revert speaker-profile`.
#
# ONEXPLAYER 3 built-in speakers ("{SPEAKER_FILTER_DESCRIPTION}"): 4th-order high-pass at {SPEAKER_HIGHPASS_HZ:.0f} Hz,
# +2 dB at 300 Hz, +2.5 dB at 3 kHz, +2 dB shelf from 8 kHz, +{SPEAKER_MAKEUP_DB:.0f} dB, then a look-ahead peak
# limiter ({SPEAKER_LIMIT_DB} dBFS, {lookahead_ms:.1f} ms) made of PipeWire's builtin filters and a hard
# ceiling at {SPEAKER_CEILING_DB} dBFS, so the extra loudness never clips.
# WirePlumber's smart-filter policy puts it in front of the speaker sink only: the
# speaker stays the default output, and headphones, HDMI and Bluetooth bypass it.
# Keep the speaker volume at 100% or below (above that is software gain after the limiter).
context.modules = [
    {{ name = libpipewire-module-filter-chain
        flags = [ nofail ]
        args = {{
            node.description = "{SPEAKER_FILTER_DESCRIPTION}"
            media.name       = "{SPEAKER_FILTER_DESCRIPTION}"
            audio.channels   = 2
            audio.position   = [ FL FR ]
            filter.graph = {{
                nodes = [
{nl.join(node_lines)}
                ]
                links = [
{nl.join(link_lines)}
                ]
                inputs  = [ "hp1_l:In" "hp1_r:In" ]
                outputs = [ "ceil_l:Out" "ceil_r:Out" ]
            }}
            capture.props = {{
                node.name           = "{SPEAKER_FILTER_NODE}"
                media.class         = Audio/Sink
                audio.position      = [ FL FR ]
                priority.session    = 0
                filter.smart        = true
                filter.smart.name   = "{SPEAKER_FILTER_SMART_NAME}"
                filter.smart.target = {{ node.name = "{speaker_node}" }}
            }}
            playback.props = {{
                node.name          = "{SPEAKER_FILTER_OUTPUT}"
                audio.position     = [ FL FR ]
                node.passive       = true
                node.dont-fallback = true
                node.linger        = true
                stream.dont-remix  = true
            }}
        }}
    }}
]
"""
