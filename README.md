# OXP3 Tool for Bazzite

`oxp3-tool` fixes controller and wake-from-sleep problems on the **OneXPlayer 3** (Intel Arc G3 Extreme / Panther Lake, 8.8" OLED) running **Bazzite**.

It checks what your machine needs and shows the exact changes before making them. Every change is recorded, so `oxp3-tool revert` undoes exactly what it did. It never touches `/usr`, never installs packages, and never writes to the controller's firmware.

> **Status: untested on real hardware.** Every fix is based on bugs reported by OXP3 owners on Bazzite, SteamOS and CachyOS, checked against the source of the kernel, InputPlumber and gamescope that Bazzite ships (October 2026). Please report what works and what doesn't, attaching the archive from `oxp3-tool diagnose`.

## Install

In Desktop Mode, open **Konsole** and paste:

```bash
curl -fsSL https://raw.githubusercontent.com/Featherwolf/OXP3-Tool-for-Bazzite/HEAD/get.sh | sudo bash
```

This downloads the tool (one file, about 40 KB), checks it, and installs it as `/usr/local/bin/oxp3-tool`. That location is writable on Bazzite and survives OS updates. It also adds **OXP3 Tool** to the app menu, which opens Konsole, shows the status and offers the recommended fixes. Run the same command again to update.

Then:

```bash
sudo oxp3-tool status       # what was detected and what each fix would do
sudo oxp3-tool apply        # apply the recommended fixes (shows the plan, asks first)
systemctl reboot            # kernel arguments take effect at boot
```

Reboot before you install a system update. A Bazzite update installed first is built from the kernel arguments the system booted with, so it drops the change. `oxp3-tool status` notices this, and running `apply` (or `revert`) again fixes it.

### Other ways to get it

- **Run it without installing.** Download the single file and run it with Python:
  ```bash
  curl -fsSLO https://raw.githubusercontent.com/Featherwolf/OXP3-Tool-for-Bazzite/HEAD/dist/oxp3-tool
  sudo python3 oxp3-tool status
  ```
  You can also open [`dist/oxp3-tool`](dist/oxp3-tool) on GitHub, click "Download raw file", and run `sudo python3 ~/Downloads/oxp3-tool status`.
- **From a clone or the "Download ZIP" button:** `sudo ./install.sh` installs it the same way, and `sudo ./bin/oxp3-tool status` runs it in place.
- **From a release:** once a version is tagged, [Releases](https://github.com/Featherwolf/OXP3-Tool-for-Bazzite/releases) has the same file.

The only requirement is Python 3, which Bazzite already includes. Use `sudo` for `status` too: reading the panel's capabilities and the kernel logs needs root.

### Uninstall

```bash
sudo oxp3-tool revert --all         # undo every fix, then reboot if kernel arguments were removed
curl -fsSL https://raw.githubusercontent.com/Featherwolf/OXP3-Tool-for-Bazzite/HEAD/uninstall.sh | sudo bash
```

## Common problems

| What you see | Most likely cause | What to do |
|---|---|---|
| The **keyboard, home, console or Xbox key** does the wrong thing | Stock InputPlumber 0.82 maps the keyboard and home keys to the wrong actions. In Bazzite's default Game Mode, which runs Steam behind the OpenGamepadUI (OGUI) overlay, the keyboard key opens the OGUI menu and the home key does nothing | `sudo oxp3-tool apply buttons`. It writes the map for the Game Mode input you use (`status` shows which). Check each key with `inputplumber device 0 test`, and press **q** to quit; closing the window instead leaves InputPlumber in test mode until `sudo systemctl restart inputplumber`. Change any of the four keys with `--keyboard-key`, `--home`, `--onex-key` and `--xbox-key`. |
| After **waking from sleep the screen stays black**, and the device **will not sleep again until you power it off** | The SSD does not come back from sleep, so the system stalls | `sudo oxp3-tool apply nvme-resume`, then reboot. The OXP3 firmware sends the SSD into the sleep mode that fails on the Predator GM7, whichever SSD is fitted, so this is recommended on every unit unless `sudo oxp3-tool status` finds no sign of it. In that case `--any-ssd` still applies it. |
| **Turning on HDR makes the screen black** until the setting reverts (Desktop Mode display settings, or Steam) | Probably: the kernel never turns on the OLED's HDR controls (gamescope and KDE send HDR, the panel shows black) | In Game Mode, `oled-profile` keeps HDR tone-mapped so the panel never goes black. For Desktop Mode, the experimental `xe-hdr-backlight` fix may make HDR work. Until then, leave HDR off in Desktop Mode; the display settings revert on their own after 15 seconds. |
| The screen feels **far below 144 Hz** and **scrolling stutters** | Usually variable refresh: on Bazzite's current kernel (7.2.9), Adaptive Sync or VRR makes the Intel driver hold the panel at about 30 to 80 Hz ([drm/xe#8976](https://gitlab.freedesktop.org/drm/xe/-/issues/8976), fixed in kernel 7.2.10). Less often, Desktop Mode is set to 60 Hz | `sudo oxp3-tool status` shows the rate the panel runs at, the Desktop Mode setting and what to change. In Desktop Mode set Adaptive sync to **Never** and the refresh rate to **144 Hz** (System Settings > Display & Monitor), or run `kscreen-doctor output.eDP-1.vrrpolicy.never output.eDP-1.mode.1920x1200@144`. In Game Mode leave VRR off until Bazzite ships kernel 7.2.10 or later. If scrolling still stutters at 144 Hz, try the experimental `xe-panel-replay-off` fix. |

If a problem persists after the fixes, run `sudo oxp3-tool diagnose` after the next failure (reboot first if the screen is black) and attach the archive to an issue.

## What it fixes

| Fix | Level | Problem | What it changes |
|---|---|---|---|
| `buttons` | recommended | On stock InputPlumber 0.82, the **keyboard key** opens the OpenGamepadUI menu (OGUI overlay, Bazzite's default) or takes a screenshot (native Steam Input). The **home key** does nothing (OGUI) or opens the on-screen keyboard (native). | Adds an InputPlumber capability map override in `/etc/inputplumber/capability_maps.d/`, written for the Game Mode input in use. Defaults: keyboard key opens Steam's on-screen keyboard, home key opens the Steam menu, ONEX/console key opens Quick Access, and the Xbox key stays the Steam button. You can remap all four. After switching with `ujust configure-opengamepadui`, apply it again; `status` reminds you. The override replaces the whole stock map, so it is only recommended while the stock map is the 0.82.0 one it was written for. |
| `volume-keys` | recommended | The **volume rocker sticks**: the embedded controller drops the key-release, so volume keys keep changing after you let go. | Adds a udev hwdb entry (`/etc/udev/hwdb.d/61-oxp3-tool-volume-keys.hwdb`) that forces key-release for both volume keys. Holding a key no longer auto-repeats. Not offered while Handheld Daemon runs, because HHD then drops every volume press ([hhd#342](https://github.com/hhd-dev/hhd/issues/342)). |
| `nvme-resume` | recommended | **Black screen after waking, and no sleep again until a power-off.** The OXP3 firmware puts the SSD slot into ACPI "simple suspend", and the Acer Predator GM7 (PCI `1dee:1602`) does not come back from it. The kernel logs `Disabling device after reset failure: -19` and the root filesystem drops out. | Adds the kernel argument `nvme.noacpi=1`, which fixed resume in community testing on the OXP3. It is recommended whenever the firmware uses simple suspend, which is every OXP3 so far, but only the GM7 is confirmed to fail. Costs a little battery while asleep. |
| `oled-profile` | recommended on bazzite-deck | **Black panel in Game Mode** (after boot, a session switch or resume) and a dead brightness slider while HDR is on. gamescope drives this panel (the same Samsung panel as the Legion Go 2) with BT.2020/PQ, which the OXP3 does not display. | Adds a gamescope display profile (`/etc/gamescope/scripts/90-oxp3-tool-oled.lua`) that keeps the panel in gamma 2.2 mode, the way the Steam Deck OLED works. HDR still works through gamescope's tone mapping. It also lists the panel's 48–144 Hz refresh rates. |
| `xe-dsb-off` | optional | `DSB 0 poll error` floods the journal, and some users see cursor trails. | Kernel argument `xe.enable_dsb=0`. Cosmetic only. |
| `xe-panel-replay-off` | experimental | Intermittent black or garbled screen (Panther Lake Panel Replay bugs). | Kernel argument `xe.enable_panel_replay=0`. Not shown to fix resume. Refused if `xe.enable_psr=0` or `xe.enable_psr2_sel_fetch=0` is set, because those combinations blank the OXP3 screen. |
| `xe-hdr-backlight` | experimental | **Turning on HDR makes the panel black.** The likely reason: in HDR mode the panel needs Intel's eDP HDR backlight interface, and the kernel only uses it when the firmware asks. | Kernel argument `xe.enable_dpcd_backlight=3`. Only offered when a read-only check, run as root with the screen on, shows that the panel has this interface. Untested on the OXP3. HDR brightness moves to the panel. SDR brightness moves too if `status` shows "SDR brightness over AUX"; otherwise it stays on the PWM output, and a brightness slider that works today may stop working. Before trying it, run `ujust configure-grub show`: if the screen stays dark, you can then boot the previous version from the boot menu with a keyboard, or revert over SSH. |
| `inputplumber-suspend` | experimental | Controller misbehaves after waking. | Enables `inputplumber-suspend.service`, which InputPlumber ships but Bazzite leaves disabled. |
| `gyro` | experimental | Gyro missing: no IIO device named `bmi260`. | Blacklists `bmi160_i2c` (`/etc/modprobe.d/oxp3-tool-imu.conf`) so `bmi270_i2c` can drive the BMI260 IMU. Only offered when the running kernel's `bmi270_i2c` knows the ACPI ID `10EC5280`. |

`sudo oxp3-tool apply` with no arguments applies only the fixes marked **recommended** for your machine in `oxp3-tool status`. Optional and experimental fixes are applied only when you name them:

```bash
sudo oxp3-tool apply xe-dsb-off
sudo oxp3-tool apply buttons --home quick-access --keyboard-key keyboard --onex-key guide --xbox-key guide
sudo oxp3-tool apply oled-profile --min-refresh 40     # below 48 Hz is untested
sudo oxp3-tool apply nvme-resume --any-ssd             # when status finds no sign of the SSD problem
sudo oxp3-tool apply xe-hdr-backlight                  # experimental; read its warning first
```

Button actions are:
- `guide`: the Steam menu
- `quick-access`: the Quick Access menu
- `keyboard`: Steam's on-screen keyboard
- `screenshot`
- `ogui-menu`: the OpenGamepadUI menu; with native Steam Input it takes a screenshot instead

 Your button choices and `--min-refresh` are remembered when you apply again; `--defaults` resets them. `oxp3-tool list` explains every fix in detail.

## Commands

| Command | What it does |
|---|---|
| `oxp3-tool status` | Device, OS image, InputPlumber, panel, SSD, gyro, and the state of every fix (default command). With `sudo` it also shows the refresh rate the panel runs at, its power-saving mode, the display firmware version, the Desktop Mode refresh and Adaptive Sync settings, and what to change when they hold the screen below 144 Hz. |
| `oxp3-tool list` | Detailed description of every fix. |
| `sudo oxp3-tool apply [FIX...]` | Shows the plan, asks, then applies. Takes `--dry-run`, `--yes`, `--defaults` and the per-fix options above. Safe to run again: it only changes what differs. |
| `sudo oxp3-tool revert FIX...` / `--all` | Undoes the tool's changes. It restores any file it replaced and removes the directories it created. It removes only the kernel arguments it added and disables only the units it enabled. A file you edited after it was installed is saved to `/var/lib/oxp3-tool/modified/` before removal. |
| `sudo oxp3-tool diagnose` | Writes `oxp3-diagnose-<time>.tar.gz` and points out known problem messages. The archive holds: status; kernel logs of this and the previous boot; InputPlumber and gamescope logs; display, backlight, SSD and IMU state. DMI serials, USB serial numbers, input-device unique IDs and MAC addresses are replaced. Hostnames and Wi-Fi network names can still appear, so look through the archive before posting it. |

## After applying

- **`buttons`**: InputPlumber restarts at once. If the buttons do nothing, restart Steam or reboot.
- **`oled-profile`**: switch to Desktop Mode and back, or reboot.
- **Kernel arguments** (`nvme-resume`, `xe-*`): reboot before installing a system update. Once booted, they stay through Bazzite updates.
- `oxp3-tool status` shows whether each change is active:
  - A kernel argument shows as *active* or *set for the next boot*. It also shows when an update dropped one.
  - Volume keys show whether the kernel is forcing releases.
- After a Bazzite update, run `oxp3-tool status`. It warns you if InputPlumber's stock OXP3 map changed under the button override. The override still replaces the new map until you revert it.

## What it deliberately does not do

These were ruled out because they break things on this hardware or on Bazzite:

- **No Handheld Daemon.** Bazzite Deck 44 uses InputPlumber and OpenGamepadUI, and InputPlumber's package conflicts with HHD. HHD's installer refuses Bazzite, and no HHD release supports the OXP3 yet. If HHD is running, the `buttons` fix steps aside.
- **No writes to the controller MCU** (hidraw `1a86:fe00`) and **no rebinding of `hid-oxp`**. Raw button-map writes have silenced the Home/Xbox keys until rewritten, and rebinding has crashed the kernel.
- **No `/etc/modprobe.d` options for `xe` or `nvme`, and no initramfs regeneration.** Both drivers load from Bazzite's prebuilt initramfs, and NVMe is built into the kernel, so only kernel arguments work. `rpm-ostree initramfs --enable` would also break bootc updates.
- **No display firmware swap.** The variable-refresh bug needs Intel's display firmware (DMC) v2.33 or later, and the community fix pins v2.20 instead. On Bazzite the driver loads its firmware from the initramfs, so this needs `rpm-ostree initramfs-etc` and `firmware_class.path`. That makes bootc treat the system as locally modified, slows every update, and runs 2025 firmware that Intel never tested with this kernel. Turning variable refresh off avoids the same bug with no system change, and kernel 7.2.10 fixes it.
- **No `i915.*` parameters.** Panther Lake is driven by `xe`, so `ujust toggle-i915-sleep-fix` does nothing on this device.
- **No `xe.enable_psr2_sel_fetch=0`**, and never `xe.enable_psr=0` together with `xe.enable_panel_replay=0`. Both were reported to blank the OXP3 screen.
- **No panel-orientation argument.** The OXP3 panel is natively landscape.
- **No system-sleep hook scripts.** systemd only reads them from `/usr/lib/systemd/system-sleep`, which is read-only on Bazzite.

## Requirements

- A OneXPlayer 3. The DMI must read `sys_vendor=ONE-NETBOOK`, `product_name=ONEXPLAYER 3`. The tool changes nothing on any other device.
- Bazzite. The `buttons` and `inputplumber-suspend` fixes need a **bazzite-deck** image, which ships InputPlumber 0.82.0 or newer. `oled-profile` is only useful in Game Mode, which only deck images have. Desktop images do not ship InputPlumber.

## Files and state

- Program (when installed): `/usr/local/bin/oxp3-tool`, plus the app menu entry `/usr/local/share/applications/oxp3-tool.desktop`
- Record of applied changes: `/var/lib/oxp3-tool/state.json`
- Backups of any file it replaced: `/var/lib/oxp3-tool/backups/`
- Copies of files you edited before a revert: `/var/lib/oxp3-tool/modified/`

`uninstall.sh` (see [Uninstall](#uninstall)) leaves `/var/lib/oxp3-tool` in place if it still holds copies of your files.

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

CI runs all of these when started from the Actions tab (it is manual while the account has no Actions minutes). `oxp3-tool --root DIR` runs against a fake root with commands simulated.

`dist/oxp3-tool` is the downloadable single-file build of `oxp3_tool/`. After changing the code, rebuild it with `python3 tools/build_zipapp.py`; a test fails while it is out of date. To publish a release, bump `__version__` in `oxp3_tool/__init__.py`, rebuild, and push a tag such as `v0.1.0`. The Release workflow then attaches the file. Without Actions minutes, create the release on GitHub by hand and attach `dist/oxp3-tool` and `dist/oxp3-tool.sha256`.
