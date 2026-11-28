#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Featherwolf
# SPDX-License-Identifier: MIT
"""Check the steamos-manager files oxp3-hw ships against steamos-manager 26.4.1's own types.

usage: check_steamos_manager.py PACKAGING_DIR

The field lists are copied from steamos-manager v26.4.1 (MIT): steamos-manager/src/hardware.rs
(DeviceConfig, DeviceMatch, DmiMatch, TdpLimitConfig, InputPlumberConfig, InputPlumberTargetDevice),
steamos-manager/src/power.rs (TdpLimitingMethod) and README.md (the remote-capable interfaces).
Beyond parsing, the oxp3-hw device file must carry only what Bathysphere SPEC 11.6 allows: [[device]],
[tdp_limit] method = "remote_interface" and [inputplumber]. The remotes.d file must name exactly the
four remotes oxp3-hwd serves, the bus names its D-Bus policy lets it own, and its object path.
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

DEVICE_CONFIG = {"device", "tdp_limit", "fan_speed", "gpu_performance", "gpu_power_profile",
                 "battery_charge_limit", "performance_profile", "inputplumber", "cec_hw", "firmware_debug"}
DEVICE_MATCH = {"dmi", "device", "variant", "friendly_name", "oui"}
DMI_MATCH = {"sys_vendor", "board_name", "product_name"}
TDP_LIMIT_CONFIG = {"method", "range", "download_mode_limit", "firmware_attribute", "performance_profile"}
TDP_METHODS = {"amdgpu_hwmon", "firmware_attribute", "remote_interface"}
INPUTPLUMBER_TARGETS = {"deck-uhid", "keyboard", "mouse"}
REMOTE_CAPABLE = {"BatteryChargeLimit1", "CpuBoost1", "FactoryReset1", "FanControl1", "GpuPerformanceLevel1",
                  "GpuPowerProfile1", "PerformanceProfile1", "Storage1", "TdpLimit1", "UpdateBios1", "UpdateDock1"}

ALLOWED_SECTIONS = {"device", "tdp_limit", "inputplumber"}
SERVED = {"TdpLimit1", "GpuPerformanceLevel1", "GpuPowerProfile1", "BatteryChargeLimit1"}
OBJECT_PATH = "/io/github/featherwolf/Bathysphere/Hw1/SteamOSManager"
PREFIX = "io.github.featherwolf.Bathysphere.Hw1."


def check(folder: Path) -> list[str]:
    errors: list[str] = []
    dev = tomllib.loads((folder / "onexplayer-3.toml").read_text())
    for k in dev:
        if k not in DEVICE_CONFIG:
            errors.append(f"onexplayer-3.toml: {k} is not a steamos-manager DeviceConfig field")
        elif k not in ALLOWED_SECTIONS:
            errors.append(f"onexplayer-3.toml: [{k}] is not allowed in a provider's device file (SPEC 11.6)")
    devices = dev.get("device")
    if not isinstance(devices, list) or len(devices) != 1:
        errors.append("onexplayer-3.toml: exactly one [[device]]")
    else:
        d = devices[0]
        errors += [f"onexplayer-3.toml: device.{k} is not a DeviceMatch field" for k in d if k not in DEVICE_MATCH]
        for req in ("device", "variant"):
            if not isinstance(d.get(req), str):
                errors.append(f"onexplayer-3.toml: device.{req} is required")
        dmi = d.get("dmi", {})
        errors += [f"onexplayer-3.toml: dmi.{k} is not a DmiMatch field" for k in dmi if k not in DMI_MATCH]
        if dmi.get("sys_vendor") != "ONE-NETBOOK" or dmi.get("product_name") != "ONEXPLAYER 3":
            errors.append("onexplayer-3.toml: the DMI match must be ONE-NETBOOK / ONEXPLAYER 3")
    tdp = dev.get("tdp_limit", {})
    errors += [f"onexplayer-3.toml: tdp_limit.{k} is not a TdpLimitConfig field"
               for k in tdp if k not in TDP_LIMIT_CONFIG]
    if tdp != {"method": "remote_interface"}:
        errors.append('onexplayer-3.toml: [tdp_limit] must be exactly method = "remote_interface"')
    if tdp.get("method") not in TDP_METHODS:
        errors.append("onexplayer-3.toml: unknown TdpLimitingMethod")
    ip = dev.get("inputplumber", {})
    if set(ip) != {"target_devices"} or not set(ip.get("target_devices", [])) <= INPUTPLUMBER_TARGETS:
        errors.append("onexplayer-3.toml: [inputplumber] target_devices from deck-uhid, keyboard, mouse")

    rem = tomllib.loads((folder / "50-oxp3-hw.toml").read_text())
    if set(rem) != SERVED:
        errors.append(f"50-oxp3-hw.toml: sections must be {sorted(SERVED)}, not {sorted(rem)}")
    for k, v in rem.items():
        if k not in REMOTE_CAPABLE:
            errors.append(f"50-oxp3-hw.toml: {k} cannot be implemented remotely in steamos-manager")
        if set(v) != {"bus_name", "object_path"}:
            errors.append(f"50-oxp3-hw.toml: [{k}] needs exactly bus_name and object_path")
        if v.get("bus_name") != PREFIX + k:
            errors.append(f"50-oxp3-hw.toml: [{k}] bus_name must be {PREFIX + k}")
        if v.get("object_path") != OBJECT_PATH:
            errors.append(f"50-oxp3-hw.toml: [{k}] object_path must be {OBJECT_PATH}")

    policy = (folder / "io.github.featherwolf.Bathysphere.Hw1.conf").read_text()
    owned = set(re.findall(r'<allow own="([^"]+)"/>', policy))
    sent = set(re.findall(r'<allow send_destination="([^"]+)"/>', policy))
    want = {PREFIX.rstrip(".")} | {PREFIX + k for k in SERVED}
    if owned != want or sent != want:
        errors.append(f"D-Bus policy: own and send_destination must be exactly {sorted(want)}")
    daemon = (folder.parent / "src" / "dbus.rs").read_text()
    block = daemon[daemon.index("pub const REMOTES"):]
    block = block[:block.index("];")]
    if set(re.findall(r'"(io\.github\.featherwolf\.Bathysphere\.Hw1\.\w+)"', block)) != {PREFIX + k for k in SERVED}:
        errors.append("src/dbus.rs REMOTES does not name the four remote bus names")
    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    errors = check(Path(argv[1]))
    for e in errors:
        print(e, file=sys.stderr)
    if errors:
        return 1
    print("steamos-manager files: they parse as steamos-manager 26.4.1 types and carry only what SPEC 11.6 allows")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
