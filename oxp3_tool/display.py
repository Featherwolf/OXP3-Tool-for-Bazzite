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

# Firmware with the DC-balance handler (PIPEDMC_EVENT_ADAPTIVE_DCB_TRIGGER).
DMC_WITH_DC_BALANCE = (2, 33)
# A mode below this counts as "not the 144 Hz mode".
FULL_RATE_MIN_HZ = 140

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
_DMC_INFO_RE = re.compile(r"^version: (\d+)\.(\d+)$", re.M)
_DMC_LOG_RE = re.compile(r"Finished loading DMC firmware \S+ \(v(\d+)\.(\d+)\)")
_DSB_ERROR_RE = re.compile(r"DSB \d+ poll error")


@dataclass
class Mode:
    name: str
    vrefresh: int
    clock: int  # kHz
    htotal: int
    vtotal: int

    @property
    def hz(self) -> float:
        if not self.htotal or not self.vtotal:
            return float(self.vrefresh)
        return self.clock * 1000 / (self.htotal * self.vtotal)

    def describe(self) -> str:
        return f"{self.name} at {self.hz:.0f} Hz"


def _mode(m: re.Match) -> Mode:
    return Mode(name=m.group("name"), vrefresh=int(m.group("vrefresh")), clock=int(m.group("clock")),
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


def parse_dmc_version(dmc_info: Optional[str], kernel_log: str) -> Optional[tuple[int, int]]:
    """The loaded display firmware version, from i915_dmc_info or the kernel log."""
    m = _DMC_INFO_RE.search(dmc_info or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    found = _DMC_LOG_RE.findall(kernel_log)
    if found:
        major, minor = found[-1]
        return int(major), int(minor)
    return None


def count_dsb_errors(kernel_log: str) -> int:
    return len(_DSB_ERROR_RE.findall(kernel_log))


@dataclass
class KwinOutput:
    refresh_mhz: Optional[int]
    vrr_policy: Optional[str]  # "Never", "Automatic", "Always" or None (not saved)
    hdr: Optional[bool]

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


def parse_kwin_outputs(data: bytes, panel_vendor: str, panel_product: int) -> list[KwinOutput]:
    """Saved settings for the internal panel in KWin's kwinoutputconfig.json.

    The file is a JSON array with an {"name": "outputs", "data": [...]} entry;
    each output carries an edidIdentifier ("<vendor> <product in decimal> ..."),
    a connectorName, a mode with refreshRate in mHz and an optional vrrPolicy.
    """
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return []
    if not isinstance(doc, list):
        return []
    prefix = f"{panel_vendor} {panel_product} "
    found: list[KwinOutput] = []
    for section in doc:
        if not isinstance(section, dict) or section.get("name") != "outputs":
            continue
        if not isinstance(section.get("data"), list):
            continue
        for out in section["data"]:
            if not isinstance(out, dict):
                continue
            ident = out.get("edidIdentifier")
            conn = out.get("connectorName")
            if isinstance(ident, str) and ident:
                if not ident.startswith(prefix):
                    continue
            elif not (isinstance(conn, str) and conn.startswith("eDP-")):
                continue
            mode = out.get("mode") if isinstance(out.get("mode"), dict) else {}
            rate = mode.get("refreshRate")
            policy = out.get("vrrPolicy")
            hdr = out.get("highDynamicRange")
            found.append(KwinOutput(
                refresh_mhz=rate if isinstance(rate, int) and not isinstance(rate, bool) and rate > 0 else None,
                vrr_policy=policy if policy in ("Never", "Automatic", "Always") else None,
                hdr=hdr if isinstance(hdr, bool) else None,
            ))
        break  # KWin reads only the first "outputs" section
    return found


@dataclass
class RefreshState:
    connector: Optional[str] = None  # e.g. "eDP-1", as KWin and kscreen-doctor name it
    mode: Optional[Mode] = None
    fixed_modes: list[Mode] = field(default_factory=list)
    psr_mode: Optional[str] = None
    debugfs_why: str = ""  # why the debugfs files could not be read
    dmc_version: Optional[tuple[int, int]] = None
    dc_balance_param: Optional[bool] = None  # None: xe not loaded
    dsb_errors: int = 0
    kwin: list[KwinOutput] = field(default_factory=list)

    @property
    def dc_balance_risk(self) -> Optional[bool]:
        """True when variable refresh would be held down by DC balance; None when unknown."""
        if self.dc_balance_param is None:
            return None
        if self.dc_balance_param:
            return False
        if self.dmc_version is None:
            return None
        return self.dmc_version >= DMC_WITH_DC_BALANCE

    def summary(self) -> list[str]:
        lines = []
        if self.mode:
            panel = ""
            rates = sorted({round(m.hz) for m in self.fixed_modes}, reverse=True)
            if rates:
                panel = f" (panel modes: {', '.join(f'{r} Hz' for r in rates)})"
            lines.append(f"{self.connector} runs {self.mode.describe()} now{panel}")
        elif self.debugfs_why:
            lines.append(f"current mode not checked: {self.debugfs_why}")
        if self.psr_mode is not None:
            lines.append(f"power saving (PSR mode): {self.psr_mode or 'none'}")
        dmc = f"v{self.dmc_version[0]}.{self.dmc_version[1]}" if self.dmc_version else "version unknown"
        if self.dc_balance_param is None:
            kernel = "xe is not loaded"
        elif self.dc_balance_param:
            kernel = "the kernel has the variable-refresh fix (xe.enable_dc_balance)"
        else:
            kernel = "the kernel predates the variable-refresh fix (7.2.10)"
        lines.append(f"display firmware (DMC) {dmc}; {kernel}")
        seen = []
        for out in self.kwin:
            text = out.describe()
            if text not in seen:
                seen.append(text)
        for text in seen:
            lines.append(f"Desktop Mode setting: {text}")
        return lines

    def advice(self) -> list[str]:
        name = self.connector or "eDP-1"
        out = []
        low_kwin = [k for k in self.kwin if k.refresh_hz and k.refresh_hz < FULL_RATE_MIN_HZ]
        if low_kwin:
            out.append(f"Desktop Mode is set to {low_kwin[0].refresh_hz:.0f} Hz. Choose 144 Hz in System Settings > "
                       f"Display & Monitor, or run `kscreen-doctor output.{name}.mode.1920x1200@144` in Desktop Mode.")
        elif self.mode and self.mode.hz < FULL_RATE_MIN_HZ:
            out.append(f"The panel runs at {self.mode.hz:.0f} Hz right now. In Desktop Mode choose 144 Hz in "
                       "System Settings > Display & Monitor; in Game Mode check the refresh rate in the "
                       "Quick Access menu's Performance tab.")
        risk = self.dc_balance_risk
        if risk:
            policies = {k.vrr_policy for k in self.kwin}
            fix = (f"Set Adaptive sync to Never in System Settings > Display & Monitor, or run "
                   f"`kscreen-doctor output.{name}.vrrpolicy.never` in Desktop Mode.")
            if "Always" in policies:
                out.append("Adaptive Sync is set to Always. On this kernel with this display firmware, variable "
                           "refresh holds the screen far below 144 Hz (Intel bug, fixed in kernel 7.2.10). " + fix)
            elif "Automatic" in policies:
                out.append("Adaptive Sync is set to Automatic, so fullscreen games and videos switch on variable "
                           "refresh, which this kernel holds far below 144 Hz (Intel bug, fixed in kernel 7.2.10). "
                           + fix)
            out.append("In Game Mode, leave variable refresh (VRR) off until Bazzite ships kernel 7.2.10 or later; "
                       "the same bug applies there.")
        elif risk is None and self.dc_balance_param is False:
            out.append("Could not read the display firmware version (run with sudo). If it is v2.33 or later, "
                       "variable refresh (Adaptive Sync, VRR) holds the screen far below 144 Hz on this kernel.")
        if self.dsb_errors:
            out.append(f"The kernel logged {self.dsb_errors} 'DSB poll error' messages this boot; the optional "
                       "xe-dsb-off fix stops them and the cursor trails or flicker that can come with them.")
        if self.psr_mode and self.psr_mode.startswith("Panel Replay") and "enabled" in self.psr_mode:
            out.append("If scrolling still stutters at 144 Hz with Adaptive Sync off, Panel Replay may be the "
                       "cause; the experimental xe-panel-replay-off fix turns it off (costs some battery).")
        return out
