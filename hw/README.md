<!--
SPDX-FileCopyrightText: 2026 Featherwolf
SPDX-License-Identifier: MIT
-->

# oxp3-hw: the ONEXPLAYER 3 hardware helper

`oxp3-hw` lets Steam's own sliders and [Bathysphere](https://github.com/Featherwolf/Bathysphere) plugins control the **OneXPlayer 3**'s power limits, GPU clocks, CPU limits, fan and charging on CachyOS, without Decky and without any root daemon.

Its daemon, `oxp3-hwd`, runs as its own system user `oxp3-hw`, **never as root**. A udev rule and a grant list give that user's group write access to an exact list of kernel files, and only on the ONEXPLAYER 3. The kernel's file permissions enforce that list. So even a compromised helper can at worst heat the device, drain the battery or hang it until a reboot. It cannot become root, read your home folder or reach the network.

> **Status: untested on real hardware.** It is built from the kernel sources (7.2 to 7.4), steamos-manager 26.4.1 and OXP3 Deck's measurements on one OXP3 (BIOS 5.09). Its tests run it against a fake device that follows the kernel's rules. See [What only the device can show](#what-only-the-device-can-show).

## What it does

- **Steam's sliders through steamos-manager.** steamos-manager relays Steam's Performance settings to four "remote" interfaces that `oxp3-hwd` serves, each on its own bus name and only while that knob works:

  | Steam setting | What `oxp3-hwd` writes | Range |
  |---|---|---|
  | TDP limit (`TdpLimit1`) | RAPL PL1 (`long_term`) on both package zones, MMIO first | 5–35 W (an admin may allow up to 52 W on AC) |
  | GPU clock (`GpuPerformanceLevel1`) | the render GT's `min_freq`/`max_freq` (auto: the boot range rpn..rp0; manual: pinned) | rpn..rp0 (100–2300 MHz) |
  | GPU power profile (`GpuPowerProfile1`) | the render GT's `power_profile` | base, power_saving |
  | Charge limit (`BatteryChargeLimit1`, Linux 7.4+) | `charge_control_end_threshold` (−1 means 100, never 0) | 50–100 % |

- **More for Bathysphere plugins** through the `io.github.featherwolf.Bathysphere.Hw1` interface ([schemas/hw1.xml](schemas/hw1.xml), a copy of Bathysphere's): boost power (PL2: firmware 52 W, "follow sustained + offset" or manual, never below PL1), energy preference per core type (when power-profiles-daemon is not running), CPU speed cap and floor and per-core-type maximum frequency, the SoC power slider, the GPU auto range, fan mode (auto, quiet, max; Linux 7.4+), charging pause while awake (Linux 7.4+), and package power in 0.1 W steps.
- **Holds values against the firmware.** The firmware rewrites the MMIO power limits on every charger change; `oxp3-hwd` writes the held values again 2 s after the last change and 3 s after a resume. If another program changes a value twice in a row outside those windows, it stops and reports the knob as overridden instead of fighting (thermald lowering PL1 is never fought).
- **Fan safety that needs no app.** A non-auto fan mode goes back to Auto at 90 °C, when the CPU temperature can't be read, before sleep, when the app that set it disconnects, when the helper stops, and, through a watchdog and the unit's `ExecStopPost`, when it hangs or crashes.
- **Never:** PL4, RAPL time windows or `enabled`, other RAPL zones, the media GT, `no_turbo`, `scaling_min_freq`, `intel_pstate/status`, SMT or CPU online files, the legacy `/sys/firmware/acpi/platform_profile`, `inhibit-charge`, `force-discharge`, raw EC, MSR, SMU or `acpi_call`, undervolting, kernel arguments. No network, no programs run, no home access, no self-update.

Only the active local user may change anything: every change is checked against logind's active seat0 session, and root is refused as a caller. Values outside the range are refused, never clamped, and every write is read back.

## Install (CachyOS Handheld)

In Desktop Mode, open Konsole (run scripts with `bash`; fish is the login shell):

```bash
sudo pacman -S --needed steamos-manager inputplumber steamos-powerbuttond power-profiles-daemon
```

Download `oxp3-hw-<version>-1-x86_64.pkg.tar.zst` from this repository's `hw-v*` release, check it, install it, then **reboot** (the grants apply at boot, and steamos-manager and Steam read their configuration only when they start):

```bash
echo "<sha256 from the release>  oxp3-hw-<version>-1-x86_64.pkg.tar.zst" | sha256sum -c
sudo pacman -U ./oxp3-hw-<version>-1-x86_64.pkg.tar.zst
systemctl reboot
```

The package conflicts with `oxp3-support` (both ship an OXP3 steamos-manager device file and TDP remotes). It enables its service by itself; on any other device the service does nothing.

To build it yourself instead: `cd hw/packaging && makepkg -si` (it builds the `hw-v<version>` tag).

### Check it

```bash
systemctl status oxp3-hw.service
busctl --user get-property com.steampowered.SteamOSManager1 /com/steampowered/SteamOSManager1 \
  com.steampowered.SteamOSManager1.RemoteInterface1 RemoteInterfaces
stat -c '%G %a %n' /sys/class/powercap/intel-rapl{,-mmio}:0/constraint_[01]_power_limit_uw \
  /sys/class/drm/card*/device/tile0/gt0/freq0/{min,max}_freq
/usr/lib/oxp3-hw/oxp3-hwd --check    # what is available and why, writes nothing (run it as yourself)
```

`RemoteInterfaces` should list `TdpLimit1`, `GpuPerformanceLevel1` and `GpuPowerProfile1` (and `BatteryChargeLimit1` on Linux 7.4+), and Steam's Quick Access → Performance shows the TDP and GPU sliders.

### More sustained power on AC (optional)

```bash
sudo mkdir -p /etc/oxp3-hw
sudo cp /usr/share/doc/oxp3-hw/limits.toml.example /etc/oxp3-hw/limits.toml
sudoedit /etc/oxp3-hw/limits.toml      # uncomment sustained_ac_max_w = 40
sudo systemctl restart oxp3-hw
```

Steam's slider then goes up to that value; on battery the helper holds it and applies 35 W until the charger is back. The same file can only narrow every other limit. A file that tries to widen anything stops the helper from starting, so it never runs with limits wider than you wrote.

## Stop, remove, reset

- Stop it for good: `sudo systemctl mask --now oxp3-hw`, or `sudo touch /etc/oxp3-hw/disabled && sudo systemctl stop oxp3-hw`. The fan and charge behaviour go back to Auto, and steamos-manager drops the remotes.
- Remove it: `sudo pacman -R oxp3-hw`, then reboot (the grants end at reboot; the firmware's values come back).
- The only value it keeps is the charge limit, in `/var/lib/oxp3-hw/persist.json`: `sudo rm -r /var/lib/oxp3-hw`. If the EC keeps the threshold after removal, `echo 100 | sudo tee /sys/class/power_supply/BAT*/charge_control_end_threshold`.

## Files

| File | What it is |
|---|---|
| `/usr/lib/oxp3-hw/oxp3-hwd` | the daemon |
| `/usr/lib/systemd/system/oxp3-hw.service` | its hardened unit (system user, no capabilities, no network, `ProtectSystem=strict`, watchdog) |
| `/usr/lib/udev/rules.d/70-oxp3-hw.rules` | group grants on device attributes (RAPL PL1/PL2, energy counter read, fan, SoC profile, battery), board-DMI gated |
| `/usr/lib/oxp3-hw/grants.conf` | group grants on cpufreq, intel_pstate, SoC slider and xe render-GT attributes, applied by the unit |
| `/usr/lib/sysusers.d/oxp3-hw.conf` | the `oxp3-hw` user |
| `/usr/share/dbus-1/system.d/…Hw1.conf`, `/usr/share/dbus-1/system-services/…Hw1.service` | who may own and call it, and activation through systemd |
| `/usr/share/steamos-manager/devices/onexplayer-3.toml`, `/usr/share/steamos-manager/remotes.d/50-oxp3-hw.toml` | steamos-manager's device file (no hardware sections, on purpose: see its comments) and the four remotes |
| `/etc/oxp3-hw/limits.toml` | your optional admin limits |
| `/var/lib/oxp3-hw/persist.json`, `/run/oxp3-hw/snapshot.json` | the charge limit; this boot's firmware values |

The journal (`journalctl -u oxp3-hw`) has one line per write: the caller's uid, the knob, the old value, the new value and what it read back.

## What only the device can show

Everything below was built from source and tested against a fake device, and needs checking on a real OXP3:

- the RAPL, xe and oxpec behaviour itself: MSR and MMIO read-back under load, the firmware's MMIO rewrite on a charger change, locked constraints after resume, thermald's interplay;
- that the udev and tmpfiles grants apply at boot, after suspend and after charger changes, and that `ConditionFirmware=smbios-field(board_vendor = ONE-NETBOOK)` matches (the exact four-field match is the unit's `ExecCondition=oxp3-hwd --dmi-match`, which runs before the privileged grants step);
- whether the oxpec battery extension raises the udev change event that repeats its grant;
- that Steam shows sliders for the remotes on CachyOS, what it sends with its TDP toggle off, and whether steamos-manager relays `PropertiesChanged`;
- that logind's seat0 active session in Game Mode is the Game Mode user (local, class `user`, state `active`);
- the oxpec fan duty read-back (`pwm1` goes through the EC's 0–184 steps, so 102 reads back as 101) and whether the EC overrides a manual fan when hot. Do not provoke real overheating to test the fan guard: use the unreadable-sensor and watchdog paths;
- the rpn, rpe, rpa and rp0 frequencies of the render GT, and that "SoC Power Slider" is the only platform-profile handler.

## Development

```bash
cd hw
cargo test --features test-sysfs     # unit tests, the ported fake-sysfs tests, the private-bus tests
ci/run.sh                            # all of it, plus clippy, the release build, grant lint, PKGBUILD,
                                     # systemd-analyze, udevadm verify, dbus-daemon and tmpfiles checks
```

- `tests/powerhw.rs` ports OXP3 Deck's 55 fake-sysfs power tests to the helper's own state machine. `tests/bus.rs` runs the real daemon on a private `dbus-daemon` that loads the package's real policy file, against a fake logind, with callers as separate processes. It checks that the served introspection matches `schemas/hw1.xml` member by member. It also covers authorization, name ownership, bounds, leases, the fan guards, the watchdog and `ExecStopPost`, drift, and AC and resume re-assertion.
- The fake device (`--sysfs-root`) follows the kernel's rules for each attribute: RAPL truncation, xe range checks and EAGAIN, intel_pstate clamps, the SoC slider's hex and pending balance, the oxpec EC scaling. Those rules live in `src/emu.rs`. That file, `--sysfs-root`, `--bus`, `--time-scale` and the test caller are compiled only with the `test-sysfs` feature; the release build and the PKGBUILD never enable it, and a release binary refuses those options.
- Nothing needs root. Run as a normal user, the daemon and the callers share your uid, and a wrong caller is logind reporting another active user. Run as root (a CI container), the daemon and each caller get their own uid through `setpriv`, so uid 0, a wrong uid and name ownership are checked with real credentials. The stand-in accounts are `daemon`, `bin` and `sys` (`OXP3_HW_TEST_USERS=provider,session,other` to change them), because dbus-daemon refuses uids without a passwd entry.
- `schemas/hw1.xml` must stay identical to Bathysphere's; set `BATHYSPHERE_HW1_XML` to its path and the introspection test compares them too.
- `tests/hostile.rs` plays a hostile local user and a hardware-safety reviewer. It checks these cases:
  - a forged `PrepareForSleep` or `NameOwnerChanged` from another uid, unicast or broadcast, changes nothing;
  - a fan request that races logind's sleep announcement is refused (`Busy("sleep")`);
  - PL1 is never raised above a PL2 the firmware accepted but did not take;
  - a register PL1 above the envelope never pulls PL2 above 52 W;
  - a manual fan or a charging pause that nobody holds is reset at start;
  - a peripheral's battery (`scope` Device) is never the battery and never means AC;
  - 24 MiB arguments and deep variants are refused, and memory stays bounded;
  - a flood of slow open reads never starves the safety task, so the watchdog keeps getting its pings;
  - `--dmi-match` is exact, and a test build still refuses the real root.

### The Bathysphere conformance suite

Bathysphere's `tools/hw-conformance` runs against a test build (`cargo build --features test-sysfs`), as a normal user. It has two modes:

```bash
# oxp3-hwd emulates the kernel itself (src/emu.rs)
hw-conformance --provider target/debug/oxp3-hwd --kernel provider
# the suite's own kernel model answers every write: turn the emulation off for both runs
hw-conformance --provider target/debug/oxp3-hwd --kernel suite \
  --arg --sysfs-root --arg '{root}' --arg --bus --arg '{bus}' --arg --no-emulation \
  --restore-arg --restore-safe --restore-arg --sysfs-root --restore-arg '{root}' --restore-arg --no-emulation
```

Only test builds have these switches:
- `--no-emulation` serves the fake root without `src/emu.rs`.
- With `--sysfs-root`, a test build also starts as uid 0, but only inside an unprivileged user namespace, so the suite can check the uid-0 caller rule (`auth.uid0`). As the host's real root it still refuses.

oxp3-hw is under the MIT License, like the rest of this repository ([LICENSE](../LICENSE)). It is not affiliated with ONE-NETBOOK (ONEXPLAYER), Intel, Valve or CachyOS.
