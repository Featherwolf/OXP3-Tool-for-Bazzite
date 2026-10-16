"""Read-only refresh-rate checks for the internal panel.

Everything here only reads: the xe driver's debugfs files (root), the kernel
log, the xe module parameters and KWin's saved output settings. Nothing is
changed; the advice tells the user what to change themselves.

Background (kernel 7.0 to 7.2.9, drm/xe issue 8976): on display version 30
(Panther Lake) the driver turns on "VRR DC balance" whenever variable refresh
is active. With display firmware (DMC) v2.33 or later, which carries the
matching handler, that holds the refresh rate far below the panel maximum
(about 30 to 80 Hz on the OXP3). Kernel 7.2.10 turned it off by default and
added the xe.enable_dc_balance parameter, so the parameter's presence shows
that the kernel has the fix. With variable refresh off the panel runs at a
fixed rate and the bug does not apply.

The kernel does not report the live refresh rate: debugfs shows the display
mode that is set, which variable refresh only stretches. It is presented as
the mode, never as a measured rate.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

DEBUGFS_DRI = "/sys/kernel/debug/dri"
DC_BALANCE_PARAM = "/sys/module/xe/parameters/enable_dc_balance"
KWIN_CONFIG = ".config/kwinoutputconfig.json"
KWIN_CONFIG_MAX = 1024 * 1024
DSB_OFF_KARG = "xe.enable_dsb=0"
PANEL_REPLAY_OFF_KARG = "xe.enable_panel_replay=0"

# Firmware with the DC-balance handler (PIPEDMC_EVENT_ADAPTIVE_DCB_TRIGGER).
DMC_WITH_DC_BALANCE = (2, 33)
# A mode this much below the panel's fastest mode counts as "not the full rate".
FULL_RATE_SLACK_HZ = 4
DEFAULT_FULL_HZ = 144

# DRM_MODE_FMT (include/drm/drm_modes.h): "name": vrefresh clock hdisplay
# hsync_start hsync_end htotal vdisplay vsync_start vsync_end vtotal 0xtype 0xflags
_MODE = (r'"(?P<name>[^"]*)": (?P<vrefresh>\d+) (?P<clock>\d+) (?P<hdisplay>\d+) \d+ \d+ (?P<htotal>\d+) '
         r'(?P<vdisplay>\d+) \d+ \d+ (?P<vtotal>\d+) 0x[0-9a-f]+ 0x[0-9a-f]+')
_CRTC_RE = re.compile(r"^\[CRTC:\d+:(?P<crtc>[^\]]+)\]:$")
_UAPI_RE = re.compile(r"^\tuapi: enable=(?P<enable>yes|no), active=(?P<active>yes|no), mode=" + _MODE + "$")
_CRTC_CONNECTOR_RE = re.compile(r"^\t\t\[CONNECTOR:\d+:(?P<conn>[^\]]+)\]$")
_CONNECTOR_RE = re.compile(r"^\[CONNECTOR:\d+:(?P<conn>[^\]]+)\]: status: ")
_LIST_MODE_RE = re.compile(r"^\t\t" + _MODE + "$")
_PSR_MODE_RE = re.compile(r"^PSR mode: (?P<mode>.*)$", re.M)
_DMC_VERSION_RE = re.compile(r"^version: (\d+)\.(\d+)$", re.M)
_DMC_LOADED_RE = re.compile(r"^fw loaded: (yes|no)$", re.M)
# "xe 0000:00:02.0: [drm] Finished loading DMC firmware i915/xe3lpd_dmc.bin (v2.36)"
_DMC_LOG_RE = re.compile(r"(\S+): \[drm\] Finished loading DMC firmware \S+ \(v(\d+)\.(\d+)\)")
_DSB_ERROR_RE = re.compile(r"DSB \d+ poll error")
_PSR_ERROR_RE = re.compile(r"Selective fetch area calculation failed|Timed out waiting PSR idle state|PSR aux error")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def safe_name(name: str) -> str:
    """A user-controlled name made safe to print to a terminal."""
    return _SAFE_NAME_RE.sub("?", name)[:32]


@dataclass
class Mode:
    hdisplay: int
    vdisplay: int
    vrefresh: int
    clock: int  # kHz
    htotal: int
    vtotal: int

    @property
    def hz(self) -> float:
        if not self.htotal or not self.vtotal:
            return float(self.vrefresh)
        return self.clock * 1000 / (self.htotal * self.vtotal)

    @property
    def size(self) -> str:
        return f"{self.hdisplay}x{self.vdisplay}"

    def describe(self) -> str:
        # The mode name comes from userspace; describe the mode by its numbers only.
        return f"{self.size} at {self.hz:.0f} Hz"


def _mode(m: re.Match) -> Mode:
    return Mode(hdisplay=int(m.group("hdisplay")), vdisplay=int(m.group("vdisplay")),
                vrefresh=int(m.group("vrefresh")), clock=int(m.group("clock")),
                htotal=int(m.group("htotal")), vtotal=int(m.group("vtotal")))


def parse_crtc_mode(text: str, connector: str) -> Optional[Mode]:
    """The mode of the active CRTC that drives ``connector`` (e.g. "eDP-1") in i915_display_info."""
    crtc_mode: Optional[Mode] = None
    active = False
    for line in text.splitlines():
        if line == "Connector info":
            break
        if _CRTC_RE.match(line):
            crtc_mode, active = None, False
            continue
        m = _UAPI_RE.match(line)
        if m:
            crtc_mode = _mode(m)
            active = m.group("enable") == "yes" and m.group("active") == "yes"
            continue
        m = _CRTC_CONNECTOR_RE.match(line)
        if m and m.group("conn") == connector and active and crtc_mode:
            return crtc_mode
    return None


def parse_fixed_modes(text: str, connector: str) -> list[Mode]:
    """The panel's fixed modes for ``connector`` in i915_display_info."""
    modes: list[Mode] = []
    in_connector = in_fixed = False
    for line in text.splitlines():
        m = _CONNECTOR_RE.match(line)
        if m:
            in_connector, in_fixed = m.group("conn") == connector, False
            continue
        if not in_connector:
            continue
        if line == "\tfixed modes:":
            in_fixed = True
            continue
        if in_fixed:
            m = _LIST_MODE_RE.match(line)
            if not m:
                break
            modes.append(_mode(m))
    return modes


def parse_psr_mode(text: str) -> Optional[str]:
    """The "PSR mode:" line of i915_psr_status, e.g. "Panel Replay Selective Update enabled"."""
    m = _PSR_MODE_RE.search(text)
    return m.group("mode").strip() if m else None


def parse_dmc_info(text: str) -> tuple[Optional[bool], Optional[tuple[int, int]]]:
    """(firmware loaded, version) from i915_dmc_info; None where the file does not say."""
    loaded = _DMC_LOADED_RE.search(text)
    version = _DMC_VERSION_RE.search(text)
    return (loaded.group(1) == "yes" if loaded else None,
            (int(version.group(1)), int(version.group(2))) if version else None)


def dmc_version_from_log(kernel_log: str, device: Optional[str] = None) -> Optional[tuple[int, int]]:
    """The last display firmware version the kernel loaded this boot, for ``device`` (PCI address) if given."""
    found = [(int(major), int(minor)) for dev, major, minor in _DMC_LOG_RE.findall(kernel_log)
             if device is None or dev == device]
    return found[-1] if found else None


def count_dsb_errors(kernel_log: str) -> int:
    return len(_DSB_ERROR_RE.findall(kernel_log))


def count_psr_errors(kernel_log: str) -> int:
    return len(_PSR_ERROR_RE.findall(kernel_log))


@dataclass
class KwinOutput:
    refresh_mhz: Optional[int]
    vrr_policy: Optional[str]  # "Never", "Automatic", "Always" or None (not saved)
    hdr: Optional[bool]
    user: Optional[str] = None

    @property
    def refresh_hz(self) -> Optional[float]:
        return self.refresh_mhz / 1000 if self.refresh_mhz else None

    def describe(self) -> str:
        rate = f"{self.refresh_hz:.0f} Hz" if self.refresh_hz else "refresh rate not saved"
        if self.vrr_policy:
            vrr = f"Adaptive Sync {self.vrr_policy}"
        else:
            vrr = "Adaptive Sync not saved (KWin's default is Never)"
        hdr = ", HDR on" if self.hdr else ""
        return f"{rate}, {vrr}{hdr}"


def parse_kwin_outputs(data: bytes, panel_vendor: str, panel_product: int,
                       user: Optional[str] = None) -> list[KwinOutput]:
    """Saved settings for the internal panel in KWin's kwinoutputconfig.json.

    The file is a JSON array with an {"name": "outputs", "data": [...]} entry;
    each output carries an edidIdentifier ("<vendor> <product in decimal> ..."),
    a connectorName, a mode with refreshRate in mHz and an optional vrrPolicy.
    Like KWin, an entry with the panel's EDID identifier wins over one that
    only names the connector, and exact duplicates are ignored.
    """
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return []
    if not isinstance(doc, list):
        return []
    prefix = f"{panel_vendor} {panel_product} "
    by_id: list[KwinOutput] = []
    by_connector: list[KwinOutput] = []
    for section in doc:
        if not isinstance(section, dict) or section.get("name") != "outputs":
            continue
        if not isinstance(section.get("data"), list):
            continue
        seen = set()
        for out in section["data"]:
            if not isinstance(out, dict):
                continue
            key = tuple(str(out.get(k, "")) for k in ("edidIdentifier", "edidHash", "connectorName", "mstPath"))
            if key in seen:
                continue
            seen.add(key)
            ident = out.get("edidIdentifier")
            conn = out.get("connectorName")
            if isinstance(ident, str) and ident:
                if not ident.startswith(prefix):
                    continue
                target = by_id
            elif isinstance(conn, str) and conn.startswith("eDP-"):
                target = by_connector
            else:
                continue
            mode = out.get("mode") if isinstance(out.get("mode"), dict) else {}
            rate = mode.get("refreshRate")
            valid_rate = isinstance(rate, int) and not isinstance(rate, bool) and 0 < rate <= 1_000_000
            policy = out.get("vrrPolicy")
            hdr = out.get("highDynamicRange")
            target.append(KwinOutput(
                refresh_mhz=rate if valid_rate else None,
                vrr_policy=policy if policy in ("Never", "Automatic", "Always") else None,
                hdr=hdr if isinstance(hdr, bool) else None,
                user=user,
            ))
        break  # KWin reads only the first "outputs" section
    return by_id or by_connector


@dataclass
class RefreshState:
    connector: Optional[str] = None  # e.g. "eDP-1", as KWin and kscreen-doctor name it
    mode: Optional[Mode] = None
    fixed_modes: list[Mode] = field(default_factory=list)
    psr_mode: Optional[str] = None
    debugfs_why: str = ""  # why the debugfs files could not be read
    needs_root: bool = False  # a real system read without root
    dmc_loaded: Optional[bool] = None
    dmc_version: Optional[tuple[int, int]] = None
    dc_balance_param: Optional[bool] = None  # None: xe not loaded
    dc_balance_on: bool = False  # xe.enable_dc_balance=Y (readable as root only)
    dsb_errors: int = 0
    psr_errors: int = 0
    kwin: list[KwinOutput] = field(default_factory=list)
    game_mode: bool = True
    awaiting_reboot: frozenset = frozenset()  # kernel arguments set for the next boot only
    panel_replay_blocked: bool = False  # PSR kargs set that xe-panel-replay-off refuses to combine with

    @property
    def top_mode(self) -> Optional[Mode]:
        return max(self.fixed_modes, key=lambda m: m.hz, default=None)

    @property
    def full_hz(self) -> int:
        top = self.top_mode
        return round(top.hz) if top else DEFAULT_FULL_HZ

    @property
    def dc_balance_risk(self) -> Optional[bool]:
        """True when variable refresh would be held down by DC balance; None when unknown."""
        if self.dc_balance_param is None:
            return None
        if self.dc_balance_param and not self.dc_balance_on:
            return False
        if self.dmc_loaded is False:
            return False  # no firmware, no DC-balance handler
        if self.dmc_version is None:
            return None
        return self.dmc_version >= DMC_WITH_DC_BALANCE

    def _label(self, out: KwinOutput) -> str:
        users = {k.user for k in self.kwin}
        return f" ({out.user})" if out.user and len(users) > 1 else ""

    def summary(self) -> list[str]:
        lines = []
        if self.mode:
            panel = ""
            rates = sorted({round(m.hz) for m in self.fixed_modes}, reverse=True)
            if rates:
                panel = f" (panel modes: {', '.join(f'{r} Hz' for r in rates)})"
            lines.append(f"{self.connector} display mode {self.mode.describe()}{panel}")
            if self.dc_balance_risk is not False or any(k.vrr_policy in ("Always", "Automatic") for k in self.kwin):
                lines.append("with variable refresh (VRR) on the screen can refresh slower; "
                             "the kernel shows no live rate")
        elif self.debugfs_why:
            lines.append(f"display mode not checked: {self.debugfs_why}")
        if self.psr_mode is not None:
            lines.append(f"power saving (PSR mode): {self.psr_mode or 'none'}")
        if self.dmc_loaded is False:
            dmc = "not loaded"
        elif self.dmc_version:
            dmc = f"v{self.dmc_version[0]}.{self.dmc_version[1]}"
        else:
            dmc = "version unknown"
        if self.dc_balance_param is None:
            kernel = "xe is not loaded"
        elif self.dc_balance_on:
            kernel = "xe.enable_dc_balance=1 turns the variable-refresh bug back on"
        elif self.dc_balance_param:
            kernel = "the kernel has the variable-refresh fix (xe.enable_dc_balance)"
        else:
            kernel = "the kernel predates the variable-refresh fix (7.2.10)"
        lines.append(f"display firmware (DMC) {dmc}; {kernel}")
        seen = []
        for out in self.kwin:
            text = f"Desktop Mode setting{self._label(out)}: {out.describe()}"
            if text not in seen:
                seen.append(text)
        lines += seen
        return lines

    def advice(self) -> list[str]:
        name = self.connector or "eDP-1"
        full = self.full_hz
        top = self.top_mode
        target = f"{top.size}@{full}" if top else f"1920x1200@{DEFAULT_FULL_HZ}"
        threshold = full - FULL_RATE_SLACK_HZ
        out = []
        low_kwin = [k for k in self.kwin if k.refresh_hz and k.refresh_hz < threshold]
        if low_kwin:
            k = low_kwin[0]
            out.append(f"Desktop Mode{self._label(k)} is set to {k.refresh_hz:.0f} Hz. Choose {full} Hz in System "
                       f"Settings > Display & Monitor, or run `kscreen-doctor output.{name}.mode.{target}` "
                       "in Desktop Mode.")
        elif self.mode and self.mode.hz < threshold:
            out.append(f"The display mode is {self.mode.hz:.0f} Hz. In Desktop Mode choose {full} Hz in System "
                       "Settings > Display & Monitor; in Game Mode check the refresh rate in the Quick Access "
                       "menu's Performance tab.")
        risk = self.dc_balance_risk
        if risk:
            if self.dc_balance_on:
                cause = ("xe.enable_dc_balance=1 turns VRR DC balance back on, so variable refresh holds the screen "
                         f"far below {full} Hz. Remove it (`sudo rpm-ostree kargs --delete=xe.enable_dc_balance=1`)"
                         " or keep variable refresh off.")
            else:
                cause = (f"On this kernel with this display firmware, variable refresh holds the screen far below "
                         f"{full} Hz (Intel bug, fixed in kernel 7.2.10).")
            fix = (f"Set Adaptive sync to Never in System Settings > Display & Monitor, or run "
                   f"`kscreen-doctor output.{name}.vrrpolicy.never` in Desktop Mode.")
            for k in self.kwin:
                if k.vrr_policy == "Always":
                    out.append(f"Adaptive Sync{self._label(k)} is set to Always. {cause} {fix}")
                    break
            else:
                for k in self.kwin:
                    if k.vrr_policy == "Automatic":
                        out.append(f"Adaptive Sync{self._label(k)} is set to Automatic, so fullscreen games and "
                                   f"videos switch on variable refresh. {cause} {fix}")
                        break
            if self.game_mode:
                if self.dc_balance_on:
                    out.append("In Game Mode, leave variable refresh (VRR) off while xe.enable_dc_balance=1 is set.")
                else:
                    out.append(f"In Game Mode, leave variable refresh (VRR) off until Bazzite ships kernel 7.2.10 or "
                               f"later: on this kernel with this display firmware it holds the screen far below "
                               f"{full} Hz.")
        elif risk is None and self.dc_balance_param is False and self.dmc_loaded is not False:
            how = " (run with sudo)" if self.needs_root else ""
            out.append(f"Could not read the display firmware version{how}. If it is v2.33 or later, variable "
                       f"refresh (Adaptive Sync, VRR) holds the screen far below {full} Hz on this kernel.")
        if self.dmc_loaded is False:
            out.append("The display firmware (DMC) did not load (see `journalctl -k | grep DMC`), so the display's "
                       "power saving is off.")
        if self.dsb_errors:
            if DSB_OFF_KARG in self.awaiting_reboot:
                out.append(f"The kernel logged {self.dsb_errors} 'DSB poll error' messages this boot; xe-dsb-off "
                           "is set for the next boot (reboot to activate it).")
            else:
                out.append(f"The kernel logged {self.dsb_errors} 'DSB poll error' messages this boot; the optional "
                           "xe-dsb-off fix stops them and the cursor trails or flicker that can come with them.")
        replay_on = bool(self.psr_mode and self.psr_mode.startswith("Panel Replay") and "enabled" in self.psr_mode)
        if replay_on and self.psr_errors and not self.panel_replay_blocked:
            if PANEL_REPLAY_OFF_KARG in self.awaiting_reboot:
                out.append("Panel Replay is still on this boot; xe-panel-replay-off is set for the next boot "
                           "(reboot to activate it).")
            else:
                out.append(f"The kernel logged {self.psr_errors} Panel Replay/PSR errors this boot. If scrolling "
                           f"stutters or the picture tears at {full} Hz with Adaptive Sync off, the experimental "
                           "xe-panel-replay-off fix turns Panel Replay off (costs some battery).")
        return out
