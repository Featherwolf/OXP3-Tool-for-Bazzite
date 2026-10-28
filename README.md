# OXP3 Tool for Bazzite and CachyOS

`oxp3-tool` fixes controller and wake-from-sleep problems on the **OneXPlayer 3** (Intel Arc G3 Extreme / Panther Lake, 8.8" OLED) running **Bazzite** or **CachyOS** (the Handheld Edition, or a desktop install).

It checks what your machine needs and shows the exact changes before making them. Every change is recorded, so `oxp3-tool revert` undoes exactly what it did. It never changes files that packages install, never installs packages, and never writes to the controller's firmware. (On CachyOS it rebuilds the generated udev hardware database and the boot entries the same way pacman's own hooks do; see [CachyOS](#cachyos).)

> **Status: untested on real hardware.** Every fix is based on bugs reported by OXP3 owners on Bazzite, SteamOS and CachyOS, checked against the source of the kernel, InputPlumber and gamescope that Bazzite ships (October 2026), and against the CachyOS packages of 28 October 2026 (kernel, InputPlumber 0.83.1, gamescope 3.17, limine-entry-tool, sdboot-manage, grub). The CachyOS support has not been run on a CachyOS install yet. Please report what works and what doesn't, attaching the archive from `oxp3-tool diagnose`.

## Install

In Desktop Mode, open **Konsole** and paste:

```bash
curl -fsSL https://raw.githubusercontent.com/Featherwolf/OXP3-Tool-for-Bazzite/HEAD/get.sh | sudo bash
```

This downloads the tool (one file, about 60 KB), checks it, and installs it as `/usr/local/bin/oxp3-tool`. That location is writable on Bazzite and survives OS updates on both systems. On CachyOS the same command works in Konsole (it runs in bash, whatever your shell). It also adds **OXP3 Tool** to the app menu, which opens Konsole, shows the status and offers the recommended fixes. Run the same command again to update.

Then:

```bash
sudo oxp3-tool status       # what was detected and what each fix would do
sudo oxp3-tool apply        # apply the recommended fixes (shows the plan, asks first)
systemctl reboot            # kernel arguments take effect at boot
```

On Bazzite, reboot before you install a system update. A Bazzite update installed first is built from the kernel arguments the system booted with, so it drops the change. `oxp3-tool status` notices this, and running `apply` (or `revert`) again fixes it. On CachyOS kernel arguments live in a configuration file the boot loader's tool reads, so kernel updates keep them.

### Other ways to get it

- **Run it without installing.** Download the single file and run it with Python:
  ```bash
  curl -fsSLO https://raw.githubusercontent.com/Featherwolf/OXP3-Tool-for-Bazzite/HEAD/dist/oxp3-tool
  sudo python3 oxp3-tool status
  ```
  You can also open [`dist/oxp3-tool`](dist/oxp3-tool) on GitHub, click "Download raw file", and run `sudo python3 ~/Downloads/oxp3-tool status`.
- **From a clone or the "Download ZIP" button:** `sudo ./install.sh` installs it the same way, and `sudo ./bin/oxp3-tool status` runs it in place.
- **From a release:** once a version is tagged, [Releases](https://github.com/Featherwolf/OXP3-Tool-for-Bazzite/releases) has the same file.

The only requirement is Python 3, which Bazzite and CachyOS's Handheld Edition already include (otherwise `sudo pacman -S python`). Use `sudo` for `status` too: reading the panel's capabilities and the kernel logs needs root.

### Uninstall

```bash
sudo oxp3-tool revert --all         # undo every fix, then reboot if kernel arguments were removed
curl -fsSL https://raw.githubusercontent.com/Featherwolf/OXP3-Tool-for-Bazzite/HEAD/uninstall.sh | sudo bash
```

## Common problems

| What you see | Most likely cause | What to do |
|---|---|---|
| The **keyboard, home, console or Xbox key** does the wrong thing | Stock InputPlumber 0.82 maps the keyboard and home keys to the wrong actions. In Bazzite's default Game Mode, which runs Steam behind the OpenGamepadUI (OGUI) overlay, the keyboard key opens the OGUI menu and the home key does nothing | `sudo oxp3-tool apply buttons`. It writes the map for the Game Mode input you use (`status` shows which). Check each key with `sudo inputplumber device 0 test`, and press **q** to quit; closing the window instead leaves InputPlumber in test mode until `sudo systemctl restart inputplumber` (with native Steam Input, then run `systemctl --user restart bazzite-native-steam-input` or reboot to get the Steam Deck controller back). After a sleep the test can lose the tiles of remapped keys (Quick, LeftTop), so a blank result for ONEX or the keyboard key there is not conclusive. A key set to an action that goes to the helper (`keyboard` with native Steam Input, `home`, `fps-limit`) always shows nothing in the test; `journalctl -u oxp3-tool-steam-actions -f` logs its presses instead (`journalctl -u oxp3-tool-xbox-key -f` for an Xbox key the `xbox-key` helper reads). Change any of the four keys with `--keyboard-key`, `--home`, `--onex-key` and `--xbox-key`. |
| The **Xbox key** (the lit logo key below Home) does nothing, and InputPlumber's log (`journalctl -u inputplumber -b | grep 'OXP HID'`) shows `failed to parse B2 report` with `25` | The controller reports the key as vendor button 0x25 (its stored button layout differs from the stock one), which InputPlumber 0.82 cannot decode | `sudo oxp3-tool apply xbox-key`. It installs a small read-only helper service that, on each press, does what `--xbox-key` chose for `buttons`: a Steam button tap (the Steam menu) by default, or any other button action. Nothing is written to the controller. The key can only tap: holding it for Steam+button shortcuts does not work, because InputPlumber 0.82's hold-capable D-Bus call is broken; use Home for those. `status` recommends it once the log shows button 25 this boot. |
| The **keyboard key** does nothing in Game Mode although `buttons` is applied | Under the OpenGamepadUI overlay the key goes through OpenGamepadUI's Steam+X shortcut, which is broken in Game Mode ([OpenGamepadUI #549](https://github.com/ShadowBlip/OpenGamepadUI/issues/549)) | Switch Game Mode to native Steam Input: `ujust configure-opengamepadui disable`, then `sudo oxp3-tool apply buttons`, then reboot. This drops OpenGamepadUI's own menu; Steam's Quick Access menu stays. Without Decky Loader the key then opens the keyboard only over a game (see the next row). Or give the key another job with `--keyboard-key`. |
| The **keyboard key** does nothing with native Steam Input, or only in Desktop Mode | Older oxp3-tool versions sent the key as Steam's Steam+X shortcut, which does nothing on some setups (Home+X does nothing either). The next ones sent `steam://open/keyboard`, which Steam's Game Mode only acts on while a game is in front: on the Library, Home or any Steam screen it does nothing | Update (run the install command again), then `sudo oxp3-tool apply buttons`. With native Steam Input the key goes to a small helper service (`oxp3-tool-steam-actions.service`). In Game Mode, with Steam's remote debugging on (Decky Loader turns it on), it opens and closes the keyboard in Steam's own interface the way Steam+X does, on any screen and over games; in Desktop Mode, or without remote debugging, it sends `steam://open/keyboard`. `journalctl -u oxp3-tool-steam-actions -b` logs each press: `ok: opened Steam's keyboard`, `ok: closed Steam's keyboard`, or a line saying why Steam's interface could not be used followed by `ok: asked Steam to open the keyboard` (the steam.pipe route, which in Game Mode only works over a game). `sudo oxp3-tool status` warns when the last press failed. If the log says `Steam's remote debugging is off`, the key opens the keyboard in Game Mode only while a game is in front: install Decky Loader (`ujust setup-decky install`; on CachyOS, Decky's own installer, see [CachyOS](#cachyos)) and reboot so that Steam starts with remote debugging, or try `--keyboard-key keyboard-chord` (Steam+X) if Steam+X works on your setup. Big Picture opened from Desktop Mode treats the key like Game Mode without remote debugging. With no game running, the keyboard opens on Steam's current screen and types into the selected text field. While a game is running, even with the Library or Store in front, Steam switches to the game and shows its keyboard over it, as Steam+X and Quick Access's keyboard button do: what you type goes to the game, and closing the keyboard returns to the game. To type into a Steam text field while a game runs, select the field instead. |
| **Home and the Xbox key both just open the Steam menu** | That is the default: both are the Steam button. Only Home can be held for Steam+button shortcuts (Steam+R1 screenshot, Steam+L1 magnifier, holding Steam+B to quit a game, Steam+left stick brightness), because on units that need `xbox-key` the Xbox key can only tap | Give one of them another job. `sudo oxp3-tool apply buttons --xbox-key fps-limit` makes the Xbox key turn a frame limit on and off (`--fps-limit`, default 72) and keeps Home's shortcuts; `--xbox-key home` makes it open the Home screen. `--home home` makes Home open the Home screen and leaves the Xbox key as the Steam menu, but then no key can be held for Steam's shortcuts. When Steam's remote debugging is on (Decky Loader turns it on, and it stays on while Decky itself shows an error), the key changes Steam's own global frame limit, so Quick Access shows it: a press turns a limit that is on off, and otherwise sets `--fps-limit`, or the nearest value Quick Access offers. Otherwise, and while the running game uses its own performance profile ("Use per-game profile" in Quick Access), it falls back to gamescope's limit (`gamescopectl debug_set_fps_limit`), which Steam is not told about: Quick Access keeps showing its own setting, Steam replaces the key's limit when it next sends its own (the slider, a per-game setting), and turning the limit off also returns the panel to 144 Hz until Steam's next change. Either way it caps games only, never Steam's own screens (library, menus), so check it in a game that runs faster than the limit, with the performance overlay on. With gamescope's limit, values that divide 144 (72, 48, 36, 24, 18, 16, 12) keep the panel at 144 Hz with VRR off; other values make gamescope switch the refresh rate. Each press logs which way it went: `ok: frame limit 72 fps in Steam (global, shown in Quick Access)`, or `ok: frame limit 72 fps` after a line saying why Steam's setting could not be used. `failed: Steam was too busy…` means Steam's UI was too slow and nothing changed: press again. After `failed: Steam did not confirm…` the change may still apply once Steam's UI recovers: check Quick Access, and press again only if the limit did not change. `ok: frame limit 72 fps` with no line before it comes from a helper installed by an older oxp3-tool, which only knows gamescope's limit: run `sudo oxp3-tool apply buttons` after updating (`status` says when a helper is from an older version). On units that need `xbox-key`, the Xbox key's presses are logged by `journalctl -u oxp3-tool-xbox-key -f`, and after updating oxp3-tool run `sudo oxp3-tool apply xbox-key` once if `status` says that helper is from an older version: older ones only send the Steam button, set only gamescope's frame limit, or lack later fixes. |
| **Decky Loader** and all its plugins show an error (for example a TypeError) after an apply, an update or a reboot | A Steam client update, which Steam installs when it restarts, changed what Decky hooks into. Decky 3.2.10 has this with the October 2026 Steam clients ([decky-loader #988](https://github.com/SteamDeckHomebrew/decky-loader/issues/988)). oxp3-tool writes nothing in your home directory and never restarts Steam or Decky | `systemctl status plugin_loader` and `journalctl -b -u plugin_loader` show Decky's side. If Steam's update channel is Beta (Settings > System), switch to Stable. Otherwise update Decky with `ujust setup-decky install` (on CachyOS, run Decky's own installer again), or `ujust setup-decky install-prerelease` when a fix is published there first. `sudo oxp3-tool diagnose` records Steam's update channel and version. |
| Holding the Steam button (Home) with X or R1 does nothing | Steam's screen reader is on. It replaces the Steam button shortcuts with screen-reader controls (Steam+X becomes "stop talking"), shows no sign that it is on, and Steam+View can switch it on by accident ([Bazzite #3787](https://github.com/ublue-os/bazzite/issues/3787)). `status` warns about it | In Desktop Mode, quit Steam completely (tray icon > Exit), then run `sed -i -E 's#("ScreenReaderEnabled"\s+)"1"#\1"0"#' ~/.local/share/Steam/config/config.vdf` and go back to Game Mode. Steam rewrites that file when it exits, so edit it only while Steam is closed. |
| After **waking from sleep the screen stays black**, and the device **will not sleep again until you power it off** | The SSD does not come back from sleep, so the system stalls | `sudo oxp3-tool apply nvme-resume`, then reboot. The OXP3 firmware sends the SSD into the sleep mode that fails on the Predator GM7, whichever SSD is fitted, so this is recommended on every unit unless `sudo oxp3-tool status` finds no sign of it. In that case `--any-ssd` still applies it. |
| **Turning on HDR makes the screen black** until the setting reverts (Desktop Mode display settings, or Steam) | Desktop Mode: KDE's HDR switch sends real HDR10 (PQ, BT.2020), and with the Intel xe driver this Samsung OLED stays black. The same happens on other Intel OLED laptops ([KDE bug 525650](https://bugs.kde.org/show_bug.cgi?id=525650)); there is no fix yet, and no kernel argument is known to help. Game Mode: gamescope's stock profile for this panel also sends PQ | In Game Mode, `oled-profile` keeps HDR tone-mapped (gamma 2.2) so the panel never gets PQ; re-enter Game Mode after applying it. Steam shows its HDR switch only for external displays: on the built-in screen, games that support HDR use it on their own (turn it on in the game), and `oled-profile` keeps that tone-mapped. `status` shows whether Game Mode used `oled-profile`. In Desktop Mode, leave HDR off. Never reboot during the 15-second revert countdown: KDE has already saved HDR as on, and Desktop Mode then starts black. `status` warns when HDR or wide color gamut is saved as on; switch them off with `kscreen-doctor output.eDP-1.hdr.disable output.eDP-1.wcg.disable` (over SSH if the screen is black). |
| The screen feels **far below 144 Hz** and **scrolling stutters** | Usually variable refresh: on kernels before 7.2.10 (Bazzite's current kernel is 7.2.9), Adaptive Sync or VRR makes the Intel driver hold the panel at about 30 to 80 Hz ([drm/xe#8976](https://gitlab.freedesktop.org/drm/xe/-/issues/8976), fixed in kernel 7.2.10). Less often, Desktop Mode is set to 60 Hz | `sudo oxp3-tool status` shows the display mode that is set, the Desktop Mode setting and what to change (the kernel does not report the live rate under VRR). In Desktop Mode set Adaptive sync to **Never** and the refresh rate to **144 Hz** (System Settings > Display & Monitor), or run `kscreen-doctor output.eDP-1.vrrpolicy.never output.eDP-1.mode.1920x1200@144`. In Game Mode leave VRR off on kernels before 7.2.10 (Bazzite: until it ships 7.2.10; CachyOS: `sudo pacman -Syu` and reboot, since its current kernels have the fix). If scrolling still stutters at 144 Hz, try the experimental `xe-panel-replay-off` fix. |
| **White dots or short lines flicker** on dark scenes, a pixel or two at a time, and do not show in screenshots | Most likely the link to the screen: the OXP3 firmware asks for its own eDP signal levels, which kernels before Linux 7.4 ignore (they log `Port A asks to use VBT vswing/preemph tables` at boot and use Intel's generic levels). The same dots are reported on another OXP3 under Linux ([kernel-anatase #7](https://github.com/anatase-org/kernel-anatase/issues/7)), where a backport of the fix stopped them in a first test; early Windows units had similar dots, fixed by a BIOS from OneXPlayer support | Check with `journalctl -k -b | grep -i vswing`, and take a screenshot (hold Home, press R1) while dots show: dots that are in the screenshot come from the game instead. The fix (Intel's "Vswing / Pre-emphasis Override" series) is queued for Linux 7.4 and is not in Bazzite's kernel yet; no kernel argument works around it, and `xe-panel-replay-off` and `xe-dsb-off` do not stop the dots. `sudo oxp3-tool status` says when the kernel logged the warning. |

If a problem persists after the fixes, run `sudo oxp3-tool diagnose` after the next failure (reboot first if the screen is black) and attach the archive to an issue.

## What it fixes

| Fix | Level | Problem | What it changes |
|---|---|---|---|
| `buttons` | recommended | On stock InputPlumber 0.82, the **keyboard key** opens the OpenGamepadUI menu (OGUI overlay, Bazzite's default) or takes a screenshot (native Steam Input). The **home key** does nothing (OGUI) or opens the on-screen keyboard (native). | Adds an InputPlumber capability map override in `/etc/inputplumber/capability_maps.d/`, written for the Game Mode input in use. Defaults: keyboard key opens Steam's on-screen keyboard (with native Steam Input through the helper service `oxp3-tool-steam-actions.service`, which asks Steam to open it directly instead of using the Steam+X shortcut), home key opens the Steam menu, ONEX/console key opens Quick Access, and the Xbox key stays the Steam button. You can remap all four, also to the Home screen or a frame-limit toggle, which the same helper carries out. The Xbox key's choice is also what the `xbox-key` helper does, read from `/etc/oxp3-tool/keys.json`. After switching with `ujust configure-opengamepadui`, apply it again; `status` reminds you. The override replaces the whole stock map, so it is only recommended while the stock map is the 0.82.0 one it was written for. |
| `xbox-key` | recommended when needed | The **Xbox key does nothing**: on some units the controller reports it only as vendor button 0x25, which InputPlumber 0.82 cannot decode (its log shows `failed to parse B2 report ... 25`). | Installs `/usr/local/libexec/oxp3-tool/xbox-key-helper` and `oxp3-tool-xbox-key.service`, which read the controller's vendor interface (read-only) and on each press do what `buttons --xbox-key` chose: a button tap through InputPlumber over D-Bus (the Steam button by default), or the keyboard, Home screen or frame-limit action in your Steam session. No holds (InputPlumber 0.82's SendEvent call is broken). Recommended once InputPlumber logged button 25 this boot, and kept recommended while installed so `apply` after a tool update refreshes it. |
| `volume-keys` | recommended | The **volume rocker sticks**: the embedded controller drops the key-release, so volume keys keep changing after you let go. | Adds a udev hwdb entry (`/etc/udev/hwdb.d/61-oxp3-tool-volume-keys.hwdb`) that forces key-release for both volume keys. Holding a key no longer auto-repeats. Not offered while Handheld Daemon runs, because HHD then drops every volume press ([hhd#342](https://github.com/hhd-dev/hhd/issues/342)). |
| `nvme-resume` | recommended | **Black screen after waking, and no sleep again until a power-off.** The OXP3 firmware puts the SSD slot into ACPI "simple suspend", and the Acer Predator GM7 (PCI `1dee:1602`) does not come back from it. The kernel logs `Disabling device after reset failure: -19` and the root filesystem drops out. | Adds the kernel argument `nvme.noacpi=1`, which fixed resume in community testing on the OXP3. It is recommended whenever the firmware uses simple suspend, which is every OXP3 so far, but only the GM7 is confirmed to fail. Costs a little battery while asleep. |
| `oled-profile` | recommended with Game Mode (bazzite-deck, CachyOS Handheld Edition) | **Black panel in Game Mode** (after boot, a session switch or resume) and a dead brightness slider while HDR is on. gamescope drives this panel (the same Samsung panel as the Legion Go 2) with BT.2020/PQ, which the OXP3 does not display. | Adds a gamescope display profile (`/etc/gamescope/scripts/90-oxp3-tool-oled.lua`) that keeps the panel in gamma 2.2 mode, the way the Steam Deck OLED works. HDR still works through gamescope's tone mapping. It also lists the panel's 48–144 Hz refresh rates. |
| `xe-dsb-off` | optional | `DSB 0 poll error` floods the journal, and some users see cursor trails. | Kernel argument `xe.enable_dsb=0`. Cosmetic only. |
| `xe-panel-replay-off` | experimental | Intermittent black or garbled screen (Panther Lake Panel Replay bugs). | Kernel argument `xe.enable_panel_replay=0`. Not shown to fix resume. Refused if `xe.enable_psr=0` or `xe.enable_psr2_sel_fetch=0` is set, because those combinations blank the OXP3 screen. |
| `xe-hdr-backlight` | experimental | Intended for **HDR blanking**, but no report shows it fixing that, and it never applies to eDP 1.5 panels. | Kernel argument `xe.enable_dpcd_backlight=3`, which forces Intel's eDP HDR backlight interface. Only offered when a read-only check, run as root with the screen on, shows that the panel has this interface and reports eDP 1.4b or older: the kernel never uses the interface on eDP 1.5 panels, which is what OXP3 panels have reported so far, so it shows n/a there. On a Panther Lake Dell XPS 14 OLED this setting left brightness dead. Before trying it, run `ujust configure-grub show` so you can boot the previous version from the boot menu if the screen stays dark (on CachyOS, see *If the screen stays dark* under [CachyOS](#cachyos)). |
| `inputplumber-suspend` | experimental | Controller misbehaves after waking. | Enables `inputplumber-suspend.service`, which InputPlumber ships but Bazzite and CachyOS leave disabled. |
| `gyro` | experimental | Gyro missing: no IIO device named `bmi260`. | Blacklists `bmi160_i2c` (`/etc/modprobe.d/oxp3-tool-imu.conf`) so `bmi270_i2c` can drive the BMI260 IMU. Only offered when the running kernel's `bmi270_i2c` knows the ACPI ID `10EC5280`. |

`sudo oxp3-tool apply` with no arguments applies only the fixes marked **recommended** for your machine in `oxp3-tool status`. Optional and experimental fixes are applied only when you name them:

```bash
sudo oxp3-tool apply xe-dsb-off
sudo oxp3-tool apply buttons --home quick-access --keyboard-key keyboard --onex-key guide --xbox-key guide
sudo oxp3-tool apply buttons --xbox-key fps-limit --fps-limit 48   # Xbox key toggles a 48 fps limit
sudo oxp3-tool apply oled-profile --min-refresh 40     # below 48 Hz is untested
sudo oxp3-tool apply nvme-resume --any-ssd             # when status finds no sign of the SSD problem
sudo oxp3-tool apply xe-hdr-backlight                  # experimental; read its warning first
```

Button actions are:
- `guide`: the Steam button: the Steam menu, and held, Steam's Steam+button shortcuts
- `quick-access`: the Quick Access menu
- `keyboard`: Steam's on-screen keyboard. With native Steam Input a helper service opens and closes it in Steam's own interface as Steam+X does (Game Mode with Decky Loader), or opens it with `steam://open/keyboard` (Desktop Mode; in Game Mode only over a game, and a second press does not close it: use B); under the OpenGamepadUI overlay it goes through OpenGamepadUI
- `keyboard-chord`: Steam's on-screen keyboard through the Steam+X shortcut, without the helper
- `home`: the Steam Home screen (`steam://open/library`, the Steam menu's Home), through the helper
- `fps-limit`: turns a frame limit of `--fps-limit` (default 72) on, and off on the next press, through the helper. With Steam's remote debugging on (Decky Loader turns it on) it changes Steam's global frame limit, which Quick Access shows. Otherwise, or while the game uses a per-game profile, it sets gamescope's limit, which Steam is not told about: Quick Access keeps showing its own setting, and turning the limit off also undoes a refresh rate chosen there. It never affects Steam's own screens
- `screenshot`
- `ogui-menu`: the OpenGamepadUI menu; with native Steam Input it takes a screenshot instead

Only `guide` can be held for Steam's shortcuts, and the `xbox-key` helper can only tap, so keep `guide` on Home to keep them; `status` warns when no key can open the Steam menu. Your button choices, `--fps-limit` and `--min-refresh` are remembered when you apply again; `--defaults` resets them. `oxp3-tool list` explains every fix in detail.

## CachyOS

The tool detects CachyOS (`ID=cachyos`) and adapts:

- **Install InputPlumber first.** CachyOS's hardware detection does not set up the ONEXPLAYER 3 yet, so a fresh install has no InputPlumber, and Home, ONEX and the keyboard key do nothing: `sudo pacman -S inputplumber`, then reboot (it starts by itself on the OXP3). Then `sudo oxp3-tool apply`.
- **Steam Deck controller in Game Mode:** on CachyOS, steamos-manager switches InputPlumber's controller to the Steam Deck one. Without it Steam sees an Xbox Elite controller (Xbox button layout; Quick Access still works through Steam's Xbox+A shortcut): `sudo pacman -S steamos-manager`, then reboot. `oxp3-tool status` says which one Steam gets. The power button in Game Mode needs steamos-powerbuttond (`sudo pacman -S steamos-powerbuttond`).
- **Game Mode** (Handheld Edition) is CachyOS's SteamOS-style gamescope session, always with native Steam Input; there is no OpenGamepadUI overlay.
- **Kernel arguments** (`nvme-resume`, `xe-*`) go into one drop-in file of the tool's own, which the boot loader's tool reads; then the tool rebuilds the boot entries and checks that every kernel's entry has the change:

  | Boot loader | File | Rebuild |
  |---|---|---|
  | Limine (CachyOS's default) | `/etc/limine-entry-tool.d/90-oxp3-tool.conf` | `limine-mkinitcpio` (rebuilds every kernel's initramfs and entry; a minute or two) |
  | systemd-boot with sdboot-manage | `/etc/sdboot-manage.conf.d/90-oxp3-tool.conf` | `sdboot-manage gen` |
  | GRUB | `/etc/default/grub.d/90-oxp3-tool.cfg` | `grub-mkconfig -o /boot/grub/grub.cfg` |

  Before rebuilding, the tool works out what the new command line would be (Limine's with `limine-entry-tool --get-cmdline`; systemd-boot's and GRUB's by reading their configuration and drop-in files the way sdboot-manage and grub-mkconfig do), and refuses (changing nothing) if anything else would change, `root=` would be lost, or the change would not reach every kernel. That happens, for example, when `/etc/default/limine` sets `KERNEL_CMDLINE[default]="..."` with a plain `=` (CachyOS's command-line installer does this), which overrides every drop-in, or when no file sets `KERNEL_CMDLINE[default]` at all (Limine then takes the command line from `/etc/kernel/cmdline`): the error then says exactly what to add by hand. rEFInd, unified kernel images, hand-made setups and systems where several boot loaders are set up and the firmware does not say which one started the system are never changed: `status` shows the steps to do by hand. If your install builds fallback initramfs images (for example with dracut), systemd-boot's fallback entries boot without the tool's arguments; current CachyOS installs with mkinitcpio have none.
- **If a rebuild reports an error, do not reboot** until the command the error names (`sudo limine-update`, or the rebuild command above) ends without errors; this matters most with Secure Boot. `oxp3-tool status` shows this warning until the next boot, because it cannot tell when you ran that command yourself: once it has ended without errors, you can reboot. To have the tool run the rebuild again, repeat the same `sudo oxp3-tool apply <fix>` or `sudo oxp3-tool revert <fix>` (a plain `sudo oxp3-tool apply` applies the recommended fixes and would put a reverted argument back).
- **If `status` warns that the tool's drop-in is now the whole command line** (no other file sets `KERNEL_CMDLINE[default]`, or a kernel's own line, any more), the next rebuild or kernel update would write boot entries without `root=`: revert the kernel-argument fixes it names first, or put your own `KERNEL_CMDLINE` line back.
- **If the screen stays dark** after a kernel-argument change: editing the boot entry needs a USB keyboard. In the Limine menu, pick the entry, press `E` and delete the argument for one boot; for systemd-boot run `sudo systemctl reboot --boot-loader-menu=30` beforehand and press `e`; in GRUB press `e`. Then `sudo oxp3-tool revert <fix>`. When Limine's config checksum is enrolled (`ENABLE_ENROLL_LIMINE_CONFIG=yes` in `/etc/default/limine`, as the CachyOS Secure Boot guide sets up), Limine has no entry editor (Secure Boot alone does not turn it off): keep SSH ready and run the revert over SSH (the system usually finishes booting with only the panel dark), or boot a CachyOS USB stick.
- **`volume-keys`** rebuilds `/usr/lib/udev/hwdb.bin` the way Arch's pacman hook does (`systemd-hwdb --usr update`). Because its file is in `/etc/udev/hwdb.d`, systemd also builds `/etc/udev/hwdb.bin` at the first boot after an update; the tool rebuilds that copy too when it is there, and `revert` deletes it unless other files in `/etc/udev/hwdb.d` need it.
- **`gyro`** is not offered: CachyOS's kernels do not have the patch that lets `bmi270_i2c` drive this IMU yet.
- **Decky Loader** (needed for the Steam-route frame limit and the keyboard key on Steam's own screens): install it with Decky's own installer, as your user in Desktop Mode: `curl -L https://github.com/SteamDeckHomebrew/decky-installer/releases/latest/download/install_release.sh | sh`. It also creates `~/.steam/steam/.cef-enable-remote-debugging`, which turns on Steam's remote debugging. Then restart Steam or reboot.
- Other OXP3 setups for CachyOS: an InputPlumber device config in `/etc/inputplumber/devices.d` that uses its own map (for example from CachyOS-OneXPlayer3) makes `buttons` step aside; a capability map at the same path as the tool's is backed up and put back on `revert`.

## Commands

| Command | What it does |
|---|---|
| `oxp3-tool status` | Device, OS image, InputPlumber, panel, SSD, gyro, and the state of every fix (default command). With `sudo` it also shows the panel's display mode (its refresh rate; variable refresh can run slower), its power-saving mode, the display firmware version, the Desktop Mode refresh and Adaptive Sync settings, and what to change when they hold the screen below 144 Hz. |
| `oxp3-tool list` | Detailed description of every fix. |
| `sudo oxp3-tool apply [FIX...]` | Shows the plan, asks, then applies. Takes `--dry-run`, `--yes`, `--defaults` and the per-fix options above. Safe to run again: it only changes what differs. |
| `sudo oxp3-tool revert FIX...` / `--all` | Undoes the tool's changes. It restores any file it replaced and removes the directories it created. It removes only the kernel arguments it added and disables only the units it enabled. A file you edited after it was installed is saved to `/var/lib/oxp3-tool/modified/` before removal. |
| `sudo oxp3-tool diagnose` | Writes `oxp3-diagnose-<time>.tar.gz` and points out known problem messages. The archive holds: status; kernel logs of this and the previous boot; InputPlumber and gamescope logs; display, backlight, SSD and IMU state. DMI serials, USB serial numbers, input-device unique IDs and MAC addresses are replaced. Hostnames and Wi-Fi network names can still appear, so look through the archive before posting it. |

## After applying

- **`buttons`**: InputPlumber restarts when the map changes. On Bazzite, with native Steam Input, the tool then runs Bazzite's `bazzite-native-steam-input` service again so Steam keeps its Steam Deck controller; on CachyOS steamos-manager (if installed) selects it again by itself. If the buttons act like an Xbox controller or do nothing, reboot.
- **`oled-profile`**: switch to Desktop Mode and back, or reboot.
- **Kernel arguments** (`nvme-resume`, `xe-*`): reboot. On Bazzite, reboot before installing a system update; once booted, they stay through updates. On CachyOS they stay through kernel updates (the boot entries are rebuilt from the same configuration).
- `oxp3-tool status` shows whether each change is active:
  - A kernel argument shows as *active* or *set for the next boot*. It also shows when an update dropped one.
  - Volume keys show whether the kernel is forcing releases.
- After a system update, run `oxp3-tool status`. It warns you if InputPlumber's stock OXP3 map changed under the button override. The override still replaces the new map until you revert it.

## What it deliberately does not do

These were ruled out because they break things on this hardware or on Bazzite (most also hold on CachyOS):

- **No Handheld Daemon.** Bazzite Deck 44 uses InputPlumber and OpenGamepadUI, and InputPlumber's package conflicts with HHD. HHD's installer refuses Bazzite, and no HHD release supports the OXP3 yet. If HHD is running, the `buttons` fix steps aside.
- **No writes to the controller MCU** (hidraw `1a86:fe00`) and **no rebinding of `hid-oxp`**. Raw button-map writes have silenced the Home/Xbox keys until rewritten, and rebinding has crashed the kernel.
- **No `/etc/modprobe.d` options for `xe` or `nvme`.** On Bazzite both drivers load from the prebuilt initramfs and NVMe is built into the kernel, so only kernel arguments work, and `rpm-ostree initramfs --enable` would also break bootc updates. On CachyOS nvme is a module in the initramfs, so a modprobe option would also need an initramfs rebuild; kernel arguments need none with systemd-boot or GRUB. With Limine, though, the tool's rebuild (`limine-mkinitcpio`) rebuilds every kernel's initramfs, as a kernel update does.
- **No display firmware swap.** The variable-refresh bug needs Intel's display firmware (DMC) v2.33 or later, and the community fix pins v2.20 instead. On Bazzite the driver loads its firmware from the initramfs, so this needs `rpm-ostree initramfs-etc` and `firmware_class.path`. That makes bootc treat the system as locally modified, slows every update, and runs 2025 firmware that Intel never tested with this kernel. Turning variable refresh off avoids the same bug with no system change, and kernel 7.2.10 fixes it.
- **No `i915.*` parameters.** Panther Lake is driven by `xe`, so `ujust toggle-i915-sleep-fix` does nothing on this device.
- **No `xe.enable_psr2_sel_fetch=0`**, and never `xe.enable_psr=0` together with `xe.enable_panel_replay=0`. Both were reported to blank the OXP3 screen.
- **No panel-orientation argument.** The OXP3 panel is natively landscape.
- **No system-sleep hook scripts.** systemd only reads them from `/usr/lib/systemd/system-sleep`, which is read-only on Bazzite.
- **On CachyOS, no edits to `/etc/default/limine`, `/etc/kernel/cmdline`, `limine.conf` or the boot entries themselves.** The tool writes only its own drop-in file, checks the result with the boot loader's own tool before anything on the EFI partition changes, and gives exact steps instead when a setup does not take drop-ins.

## Requirements

- A OneXPlayer 3. The DMI must read `sys_vendor=ONE-NETBOOK`, `product_name=ONEXPLAYER 3`. The tool changes nothing on any other device.
- Bazzite or CachyOS.
  - **Bazzite:** the `buttons` and `inputplumber-suspend` fixes need a **bazzite-deck** image, which ships InputPlumber 0.82.0 or newer. `oled-profile` is only useful in Game Mode, which only deck images have. Desktop images do not ship InputPlumber.
  - **CachyOS** (`ID=cachyos` in `/etc/os-release`): InputPlumber is not installed on the OXP3 by default (`sudo pacman -S inputplumber`, then reboot). The kernel-argument fixes need Limine with limine-mkinitcpio-hook (the default), systemd-boot with sdboot-manage, or GRUB; other setups get exact steps to do by hand. `oled-profile` is only useful with the Handheld Edition's Game Mode.

## Files and state

- Program (when installed): `/usr/local/bin/oxp3-tool`, plus the app menu entry `/usr/local/share/applications/oxp3-tool.desktop`
- Record of applied changes: `/var/lib/oxp3-tool/state.json`
- Backups of any file it replaced: `/var/lib/oxp3-tool/backups/`
- Copies of files you edited before a revert: `/var/lib/oxp3-tool/modified/`
- CachyOS kernel arguments: `/etc/limine-entry-tool.d/90-oxp3-tool.conf`, `/etc/sdboot-manage.conf.d/90-oxp3-tool.conf` or `/etc/default/grub.d/90-oxp3-tool.cfg` (whichever boot loader is in use), removed by `revert`

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

The tests run every fix against a fake OXP3 filesystem: a fake Bazzite with a simulated `rpm-ostree` and `systemctl`, and a fake CachyOS with simulated `pacman`, `limine-entry-tool`/`limine-mkinitcpio`, `sdboot-manage` and `grub-mkconfig` (rEFInd and ambiguous setups too).

- If `lua5.4` is installed, the gamescope profile is executed against a stubbed gamescope API.
- If `systemd-hwdb` is installed, the hwdb entry is compiled and queried with the real OXP3 DMI modalias.
- If `OXP3_IP_CAPABILITY_MAP_SCHEMA` points to InputPlumber's `capability_map_v1.json`, the generated maps are validated against it.

CI runs all of these when started from the Actions tab (it is manual while the account has no Actions minutes). `oxp3-tool --root DIR` runs against a fake root with commands simulated.

`dist/oxp3-tool` is the downloadable single-file build of `oxp3_tool/`. After changing the code, rebuild it with `python3 tools/build_zipapp.py`; a test fails while it is out of date. To publish a release, bump `__version__` in `oxp3_tool/__init__.py`, rebuild, and push a tag such as `v0.1.0`. The Release workflow then attaches the file. Without Actions minutes, create the release on GitHub by hand and attach `dist/oxp3-tool` and `dist/oxp3-tool.sha256`.
