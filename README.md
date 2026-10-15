# OXP3 Tool for Bazzite

`oxp3-tool` fixes controller and wake-from-sleep problems on the **OneXPlayer 3** (Intel Arc G3 Extreme / Panther Lake, 8.8" OLED) running **Bazzite**.

It checks what your machine needs and shows the exact changes before making them. Every change is recorded, so `oxp3-tool revert` undoes exactly what it did. It never touches `/usr`, never installs packages, and never writes to the controller's firmware.

> **Status: untested on real hardware.** Every fix is based on bugs reported by OXP3 owners on Bazzite, SteamOS and CachyOS, checked against the source of the kernel, InputPlumber and gamescope that Bazzite ships (October 2026). Please report what works and what doesn't, attaching the archive from `oxp3-tool diagnose`.

## Quick start

```bash
git clone https://github.com/Featherwolf/OXP3-Tool-for-Bazzite.git
cd OXP3-Tool-for-Bazzite
sudo ./install.sh           # optional: installs to /usr/local (writable on Bazzite)

oxp3-tool status            # what was detected and what each fix would do
sudo oxp3-tool apply        # apply the recommended fixes (asks first)
systemctl reboot            # kernel arguments take effect at boot
```

You can also run it from the source folder without installing it: `sudo ./bin/oxp3-tool apply`.

Run it in Desktop Mode (Konsole) or over SSH. The only requirement is Python 3, which Bazzite already includes.

## What it fixes

| Fix | Level | Problem | What it changes |
|---|---|---|---|
| `buttons` | recommended | On stock InputPlumber 0.82, the **keyboard key takes a screenshot** and the **home key opens the on-screen keyboard**. | Adds an InputPlumber capability map override in `/etc/inputplumber/capability_maps.d/`. Defaults: keyboard key opens the on-screen keyboard, home key opens Steam (Guide), ONEX key opens Quick Access. You can remap each key. |
| `volume-keys` | recommended | The **volume rocker sticks**: the embedded controller drops the key-release, so volume keeps changing after you let go. | Adds a udev hwdb entry (`/etc/udev/hwdb.d/61-oxp3-tool-volume-keys.hwdb`) that forces key-release for both volume keys. Holding a key no longer auto-repeats. |
| `nvme-resume` | recommended with the Predator GM7 SSD | **Black or frozen screen after sleep.** The Acer Predator GM7 SSD (PCI `1dee:1602`) does not come back from s2idle. The kernel logs `Disabling device after reset failure: -19` and the root filesystem goes read-only. | Adds the kernel argument `nvme.noacpi=1`, which fixed resume in community testing on the OXP3. |
| `oled-profile` | recommended | **Black panel in Game Mode** (after boot, a session switch or resume) and a dead brightness slider while HDR is on. gamescope drives this panel (the same Samsung panel as the Legion Go 2) with BT.2020/PQ, which the OXP3 does not display. | Adds a gamescope display profile (`/etc/gamescope/scripts/90-oxp3-tool-oled.lua`) that keeps the panel in gamma 2.2 mode, the way the Steam Deck OLED works. HDR still works through gamescope's tone mapping. It also lists the panel's 48–144 Hz refresh rates. |
| `xe-dsb-off` | optional | `DSB 0 poll error` floods the journal, and some users see cursor trails. | Kernel argument `xe.enable_dsb=0`. Cosmetic only. |
| `xe-panel-replay-off` | experimental | Intermittent black or garbled screen (Panther Lake Panel Replay bugs). | Kernel argument `xe.enable_panel_replay=0`. Not shown to fix resume. Refused if `xe.enable_psr=0` or `xe.enable_psr2_sel_fetch=0` is set, because those combinations blank the OXP3 screen. |
| `inputplumber-suspend` | experimental | Controller misbehaves after waking. | Enables `inputplumber-suspend.service`, which InputPlumber ships but Bazzite leaves disabled. |
| `gyro` | experimental | Gyro missing: no IIO device named `bmi260`. | Blacklists `bmi160_i2c` (`/etc/modprobe.d/oxp3-tool-imu.conf`) so `bmi270_i2c` can drive the BMI260 IMU. Only offered when the running kernel's `bmi270_i2c` knows the ACPI ID `10EC5280`. |

`sudo oxp3-tool apply` with no arguments applies only the fixes marked **recommended** for your machine in `oxp3-tool status`. Optional and experimental fixes are applied only when you name them:

```bash
sudo oxp3-tool apply xe-dsb-off
sudo oxp3-tool apply buttons --home quick-access --keyboard-key keyboard --onex-key guide
sudo oxp3-tool apply oled-profile --min-refresh 40     # below 48 Hz is untested
sudo oxp3-tool apply nvme-resume --any-ssd             # SSD other than the GM7 that fails to resume
```

Button actions are `guide`, `quick-access`, `keyboard` (on-screen keyboard) and `screenshot`. `oxp3-tool list` explains every fix in detail.

## Commands

| Command | What it does |
|---|---|
| `oxp3-tool status` | Device, OS image, InputPlumber, panel, SSD, gyro, and the state of every fix (default command). |
| `oxp3-tool list` | Detailed description of every fix. |
| `sudo oxp3-tool apply [FIX...]` | Shows the plan, asks, then applies. Takes `--dry-run`, `--yes` and the per-fix options above. Safe to run again. |
| `sudo oxp3-tool revert FIX...` / `--all` | Undoes the tool's changes: restores any file it replaced, removes only the kernel arguments it added, disables only the units it enabled. |
| `sudo oxp3-tool diagnose` | Writes `oxp3-diagnose-<time>.tar.gz` (status, kernel logs of this and the previous boot, InputPlumber and gamescope logs, display, backlight, SSD and IMU state) and points out known problem messages. Serial numbers are left out. The logs are otherwise complete, so look through the archive before posting it. |

## After applying

- **`buttons`**: InputPlumber restarts at once. If the buttons do nothing, restart Steam or reboot.
- **`oled-profile`**: switch to Desktop Mode and back, or reboot.
- **Kernel arguments** (`nvme-resume`, `xe-*`): reboot. They stay through Bazzite updates.
- `oxp3-tool status` shows whether each change is active: a kernel argument shows as *active* or *set for the next boot*, and volume keys show whether the kernel is forcing releases.
- After a Bazzite update, run `oxp3-tool status`. It warns you if InputPlumber's stock OXP3 map changed under the button override.

## What it deliberately does not do

These were ruled out because they break things on this hardware or on Bazzite:

- **No Handheld Daemon.** Bazzite Deck 44 uses InputPlumber and OpenGamepadUI, and InputPlumber's package conflicts with HHD. HHD's installer refuses Bazzite, and no HHD release supports the OXP3 yet. If HHD is running, the `buttons` fix steps aside.
- **No writes to the controller MCU** (hidraw `1a86:fe00`) and **no rebinding of `hid-oxp`**. Raw button-map writes have silenced the Home/Xbox keys until rewritten, and rebinding has crashed the kernel.
- **No `/etc/modprobe.d` options for `xe` or `nvme`, and no initramfs regeneration.** Both drivers load from Bazzite's prebuilt initramfs, and NVMe is built into the kernel, so only kernel arguments work. `rpm-ostree initramfs --enable` would also break bootc updates.
- **No `i915.*` parameters.** Panther Lake is driven by `xe`, so `ujust toggle-i915-sleep-fix` does nothing on this device.
- **No `xe.enable_psr2_sel_fetch=0`**, and never `xe.enable_psr=0` together with `xe.enable_panel_replay=0`. Both were reported to blank the OXP3 screen.
- **No panel-orientation argument.** The OXP3 panel is natively landscape.
- **No system-sleep hook scripts.** systemd only reads them from `/usr/lib/systemd/system-sleep`, which is read-only on Bazzite.

## Requirements

- A OneXPlayer 3. The DMI must read `sys_vendor=ONE-NETBOOK`, `product_name=ONEXPLAYER 3`. The tool changes nothing on any other device.
- Bazzite. The `buttons` and `inputplumber-suspend` fixes need a **bazzite-deck** image, which ships InputPlumber 0.82.0 or newer. Desktop images do not ship InputPlumber.

## Files and state

- Program (when installed): `/usr/local/lib/oxp3-tool`, `/usr/local/bin/oxp3-tool`
- Record of applied changes: `/var/lib/oxp3-tool/state.json`, plus backups of any file it replaced in `/var/lib/oxp3-tool/backups/`

To uninstall, run `sudo oxp3-tool revert --all`, reboot if kernel arguments were removed, then `sudo ./uninstall.sh`.

## Background

- InputPlumber added OXP3 support in 0.82.0 (map `oxp9`: ONEX → QuickAccess, keyboard key → QuickAccess2, home key → gamepad Keyboard). Upstream HHD added OXP3 support in September 2026 but has not released it.
- The volume rocker problem and its force-release fix: [hhd-dev/hhd#342](https://github.com/hhd-dev/hhd/issues/342) and the CachyOS OXP3 notes.
- The resume (Predator GM7 / `nvme.noacpi=1`) and HDR (gamma 2.2 profile) findings come from the [ONEXPLAYER 3 SteamOS setup](https://github.com/HHHHanasak1/onexplayer3-steamos-setup) fix pack, tested on hardware. The refresh-rate table comes from gamescope's `lenovo.legiongo2.oled.lua` (same panel).
- Intermittent OXP3 black screen on Bazzite: [ublue-os/bazzite#5975](https://github.com/ublue-os/bazzite/issues/5975). Its root cause is still unknown, so attach a `diagnose` archive there if you hit it.

## Development

```bash
python3 -m unittest discover -s tests -t . -v
```

The tests run every fix against a fake OXP3 filesystem with a simulated `rpm-ostree` and `systemctl`.

- If `lua5.4` is installed, the gamescope profile is executed against a stubbed gamescope API.
- If `systemd-hwdb` is installed, the hwdb entry is compiled and queried with the real OXP3 DMI modalias.
- If `OXP3_IP_CAPABILITY_MAP_SCHEMA` points to InputPlumber's `capability_map_v1.json`, the generated maps are validated against it.

CI runs all of these. `oxp3-tool --root DIR` runs against a fake root with commands simulated.
