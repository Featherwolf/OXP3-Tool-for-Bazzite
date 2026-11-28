#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Featherwolf
# SPDX-License-Identifier: MIT
"""Check that every oxp3-hw grant names an allowed attribute (Bathysphere SPEC 10 and 11.7).

usage: lint_grants.py PACKAGING_DIR    check grants.conf and 70-oxp3-hw.rules
       lint_grants.py --self-test      also prove that broken inputs fail

grants.conf: every non-comment line is exactly `z <path> 0664 root oxp3-hw -`, with a path from
ALLOWED_TMPFILES (exact text, globs only where listed). 70-oxp3-hw.rules: RUN+= runs only
/usr/bin/chgrp oxp3-hw or /usr/bin/chmod 0664|0440 on `%S%p/<attr>` with <attr> allowed for that
rule's SUBSYSTEM; every rule that grants is DMI-gated by the GOTO block above it; the same
attributes get both chgrp and chmod; nothing on NEVER appears anywhere.
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

ALLOWED_TMPFILES = {
    "/sys/devices/system/cpu/cpufreq/policy*/energy_performance_preference",
    "/sys/devices/system/cpu/cpufreq/policy*/scaling_max_freq",
    "/sys/devices/system/cpu/intel_pstate/max_perf_pct",
    "/sys/devices/system/cpu/intel_pstate/min_perf_pct",
    "/sys/module/processor_thermal_soc_slider/parameters/slider_balance",
    "/sys/module/processor_thermal_soc_slider/parameters/slider_offset",
    "/sys/class/drm/card*/device/tile0/gt0/freq0/min_freq",
    "/sys/class/drm/card*/device/tile0/gt0/freq0/max_freq",
    "/sys/class/drm/card*/device/tile0/gt0/freq0/power_profile",
}

# attribute -> (subsystem, mode)
ALLOWED_UDEV = {
    "constraint_0_power_limit_uw": ("powercap", "0664"),
    "constraint_1_power_limit_uw": ("powercap", "0664"),
    "energy_uj": ("powercap", "0440"),
    "pwm1_enable": ("hwmon", "0664"),
    "pwm1": ("hwmon", "0664"),
    "profile": ("platform-profile", "0664"),
    "charge_control_end_threshold": ("power_supply", "0664"),
    "charge_behaviour": ("power_supply", "0664"),
}

NEVER = re.compile(
    r"constraint_[2-9]|time_window|/enabled\b|%p/enabled|no_turbo|intel_pstate/status|scaling_min_freq|"
    r"smt/control|/online\b|cpu[0-9*]+/online|/gt1/|debugfs|efivar|/dev/port|/dev/mem|msr|acpi_call|"
    r"firmware/acpi/platform_profile|inhibit-charge\b|force-discharge"
)

TMPFILES_LINE = re.compile(r"^z\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)$")
RUN = re.compile(r'RUN\+="([^"]*)"')


def lint_tmpfiles(text: str) -> list[str]:
    errors = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if NEVER.search(line):
            errors.append(f"grants.conf:{n}: names a never-granted attribute: {line}")
        m = TMPFILES_LINE.match(line)
        if not m:
            errors.append(f"grants.conf:{n}: not `z PATH 0664 root oxp3-hw -`: {line}")
            continue
        path, mode, user, group, arg = m.groups()
        if path not in ALLOWED_TMPFILES:
            errors.append(f"grants.conf:{n}: {path} is not an allowed attribute")
        if (mode, user, group, arg) != ("0664", "root", "oxp3-hw", "-"):
            errors.append(f"grants.conf:{n}: must be 0664 root oxp3-hw -, not {mode} {user} {group} {arg}")
    return errors


def lint_udev(text: str) -> list[str]:
    errors = []
    gated = False
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if NEVER.search(line):
            errors.append(f"rules:{n}: names a never-granted attribute")
        if line.startswith("LABEL="):
            gated = False
            continue
        if 'ATTR{[dmi/id]board_name}!="ONEXPLAYER 3"' in line and "GOTO=" in line:
            gated = True
            continue
        runs = RUN.findall(line)
        if not runs:
            continue
        if not gated:
            errors.append(f"rules:{n}: grants outside the DMI-gated block")
        sub = re.search(r'SUBSYSTEM=="([^"]+)"', line)
        sub = sub.group(1) if sub else None
        chgrp, chmod = set(), set()
        for cmd in runs:
            words = cmd.split()
            if len(words) < 3:
                errors.append(f"rules:{n}: malformed RUN {cmd!r}")
                continue
            prog, arg, targets = words[0], words[1], words[2:]
            if prog == "/usr/bin/chgrp" and arg == "oxp3-hw":
                into = chgrp
            elif prog == "/usr/bin/chmod" and arg in ("0664", "0440"):
                into = chmod
            else:
                errors.append(f"rules:{n}: only `/usr/bin/chgrp oxp3-hw` and `/usr/bin/chmod 0664|0440`: {cmd!r}")
                continue
            for t in targets:
                if not t.startswith("%S%p/") or "/" in t[len("%S%p/"):]:
                    errors.append(f"rules:{n}: {t} is not an attribute of the matched device")
                    continue
                attr = t[len("%S%p/"):]
                allowed = ALLOWED_UDEV.get(attr)
                if allowed is None:
                    errors.append(f"rules:{n}: {attr} is not an allowed attribute")
                    continue
                if allowed[0] != sub:
                    errors.append(f"rules:{n}: {attr} is allowed for {allowed[0]}, not {sub}")
                if into is chmod and arg != allowed[1]:
                    errors.append(f"rules:{n}: {attr} must be mode {allowed[1]}, not {arg}")
                into.add(attr)
        if chgrp != chmod:
            errors.append(f"rules:{n}: chgrp and chmod name different attributes: {sorted(chgrp ^ chmod)}")
    return errors


def lint(folder: Path) -> list[str]:
    return lint_tmpfiles((folder / "grants.conf").read_text()) + lint_udev((folder / "70-oxp3-hw.rules").read_text())


def self_test(folder: Path) -> list[str]:
    """Each mutation of the real files must be caught."""
    good_t = (folder / "grants.conf").read_text()
    good_u = (folder / "70-oxp3-hw.rules").read_text()
    mutants = [
        ("tmpfiles", good_t + "z /sys/devices/system/cpu/intel_pstate/no_turbo 0664 root oxp3-hw -\n"),
        ("tmpfiles", good_t + "z /sys/devices/system/cpu/cpufreq/policy*/scaling_min_freq 0664 root oxp3-hw -\n"),
        ("tmpfiles", good_t + "z /sys/class/drm/card*/device/tile0/gt1/freq0/max_freq 0664 root oxp3-hw -\n"),
        ("tmpfiles", good_t.replace("energy_performance_preference 0664", "energy_performance_preference 0666")),
        ("tmpfiles", good_t.replace("0664 root oxp3-hw -", "0664 root users -", 1)),
        ("tmpfiles", good_t + "Z /sys/devices/system/cpu 0664 root oxp3-hw -\n"),
        ("tmpfiles", good_t + "z /sys/devices/system/cpu/cpufreq/* 0664 root oxp3-hw -\n"),
        ("udev", good_u.replace("chgrp oxp3-hw %S%p/constraint_0_power_limit_uw %S%p/constraint_1_power_limit_uw",
                                "chgrp oxp3-hw %S%p/constraint_0_power_limit_uw %S%p/constraint_2_power_limit_uw", 1)),
        ("udev", good_u.replace("%S%p/pwm1_enable %S%p/pwm1\"", "%S%p/pwm1_enable %S%p/pwm1 %S%p/temp1_crit\"", 1)),
        ("udev", good_u.replace("/usr/bin/chmod 0440", "/usr/bin/chmod 0444", 1)),
        ("udev", good_u.replace("/usr/bin/chgrp oxp3-hw %S%p/profile", "/usr/bin/chgrp users %S%p/profile", 1)),
        ("udev", good_u.replace('RUN+="/usr/bin/chmod 0664 %S%p/profile"', 'RUN+="/bin/sh -c x"', 1)),
        ("udev", good_u.replace("%S%p/charge_control_end_threshold %S%p/charge_behaviour\", RUN+=\"/usr/bin/chmod",
                                "%S%p/../../x %S%p/charge_behaviour\", RUN+=\"/usr/bin/chmod", 1)),
        ("udev", good_u.replace('ATTR{[dmi/id]board_name}!="ONEXPLAYER 3", GOTO="oxp3_hw_end"\n', "", 1)),
        ("udev", good_u.replace("%S%p/energy_uj\", RUN+=\"/usr/bin/chmod 0440 %S%p/energy_uj",
                                "%S%p/energy_uj\", RUN+=\"/usr/bin/chmod 0440 %S%p/name", 1)),
    ]
    failures = []
    for i, (kind, text) in enumerate(mutants):
        if (kind == "tmpfiles" and text == good_t) or (kind == "udev" and text == good_u):
            failures.append(f"mutant {i} did not change the file (fix the self-test)")
            continue
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "grants.conf").write_text(text if kind == "tmpfiles" else good_t)
            (d / "70-oxp3-hw.rules").write_text(text if kind == "udev" else good_u)
            if not lint(d):
                failures.append(f"mutant {i} ({kind}) passed the lint")
    return failures


def main(argv: list[str]) -> int:
    here = Path(__file__).resolve().parent.parent / "packaging"
    if argv[1:] == ["--self-test"]:
        errors = lint(here) + self_test(here)
    elif len(argv) == 2:
        errors = lint(Path(argv[1]))
    else:
        print(__doc__, file=sys.stderr)
        return 2
    for e in errors:
        print(e, file=sys.stderr)
    if errors:
        return 1
    print("grants: every line names an allowed attribute")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
