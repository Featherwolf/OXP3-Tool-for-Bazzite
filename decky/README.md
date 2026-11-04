# OXP3 Deck

A [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) plugin for the **ONEXPLAYER 3** (Intel Panther
Lake, Arc B390) on **CachyOS** (the Handheld Edition) and **Bazzite**. From one Quick Access panel in Game Mode, it
installs, sets up and updates two things:

1. **oxp3-tool**, from [this repository](https://github.com/Featherwolf/OXP3-Tool-for-Bazzite): the controller,
   sleep and display fixes, the button layout and the kernel switch. The plugin runs `oxp3-tool` as root and shows
   what it says. It has no fixes of its own.
2. **xess-xmx-linux**, by [HHHHanasak1](https://github.com/HHHHanasak1/xess-xmx-linux): XeSS super resolution and
   frame generation on the GPU's XMX cores instead of the slower DP4a path, with patched Mesa drivers. The plugin
   also sets its per-game launch options for you.

> **Status: new, and not yet tested on a real device.** If something goes wrong, save a diagnose archive on the
> Updates & logs page and attach it to an issue.

## Install

You need Decky Loader. In Desktop Mode, open Konsole and run Decky's own installer as your user:

```bash
curl -L https://github.com/SteamDeckHomebrew/decky-installer/releases/latest/download/install_release.sh | sh
```

Then, in Game Mode:

1. Open Quick Access (the ONEX key) and go to Decky (the plug icon). Open Decky's settings (the gear).
2. Under **General**, turn on **Developer mode**.
3. Open **Developer**. Under **Install Plugin from URL**, enter this address and tap **Install**:

   ```
   https://raw.githubusercontent.com/Featherwolf/OXP3-Tool-for-Bazzite/HEAD/decky/release/OXP3Deck.zip
   ```

4. Confirm. OXP3 Deck appears in Decky's list.

You can also download [`OXP3Deck.zip`](release/OXP3Deck.zip) and pick it with **Install Plugin from ZIP File** >
**Browse**. Keep the file name `OXP3Deck.zip`: Decky uses it to find the installed copy it replaces.

To update the plugin, install it the same way again. Your settings and logs stay: they live outside the plugin's
folder.

## First steps

1. Open **OXP3 Deck** in Quick Access. **Status** shows the device and the system.
2. If oxp3-tool is missing or too old (the plugin needs 0.4.0 or later), tap **Install** or **Update**. It puts
   oxp3-tool at `/usr/local/bin/oxp3-tool`, where `get.sh` puts it, so `sudo oxp3-tool` keeps working in Konsole.
3. Tap **Apply** under the recommended fixes. It shows the plan and asks first. If a fix needs a reboot, a
   **Reboot** button appears.
4. Close any game, then tap **Install XeSS XMX**. It downloads the newest release (about 16 MB) and runs its
   `install.sh` as your user. That script downloads Intel's shader compiler and XeSS SDK, a few hundred MB more, so
   this takes a while.
   - Already installed by hand, for example in `~/Documents/xess-xmx-linux-20261031`? The plugin finds it and
     shows it as installed. There is no need to install it again.
5. Log out and back in, or reboot. Steam keeps its old Vulkan driver settings until then.
6. Start a game and open Quick Access. **This game** shows its XeSS options.

## The pages

**Quick panel** (Quick Access):

- **Status:** the device and OS. A red **Do not reboot** line when oxp3-tool could not finish rebuilding the boot
  entries. **Reboot to finish** when a change waits for a reboot. The running task and its last log line.
- **oxp3-tool:** Install or Update when it is missing or old. The recommended fixes not applied yet, with Apply.
- **XeSS:** the driver status (is the patched driver in use, is a re-login needed) and Install or Update.
- **This game** (only while a game runs): the XeSS launch options as dropdowns, and advice for that game.
- **More:** the full page, with these tabs.

**Fixes:** every oxp3-tool fix with its state, notes and when it takes effect. Apply and Revert for each one. Both
show the plan first.

**Buttons:** what each key does (keyboard, Home, ONEX and Xbox keys), the frame-limit value, and **Apply layout**.
That applies the `buttons` fix, and `xbox-key` too when your Xbox key needs its helper.

**XeSS:** the installed version and folder, the driver check, the default Mesa driver for all games, Install,
Update and Uninstall. Also the system Protons and whether they have the shim, missing tools, and old package folders
you can remove.

**Games:** the running game, or the last one. The same options as the quick panel, plus the full advice: the XeSS
version in the game's folder, which Proton it uses, and what its last run logged (XMX, a fallback to DP4a, a kernel
that did not compile).

**Kernel** (CachyOS with Limine only): the installed kernels and what boots next. **Try** boots another kernel once.
If it hangs, hold the power button: the next boot is the kernel you have now, with no keyboard needed. **Keep**, on
the kernel you want, sets Limine back to booting whatever booted last. See
[Trying another kernel](https://github.com/Featherwolf/OXP3-Tool-for-Bazzite#trying-another-kernel-cachyos).

**Updates & logs:** oxp3-tool and xess-xmx-linux versions with update checks, the diagnose archive, and the last
tasks with their logs.

## How updates work

### oxp3-tool

- The plugin carries its own copy (`bin/oxp3-tool`, checked against its SHA-256). Updates & logs compares the
  installed version with that copy and with the newest one on GitHub.
- **Update** installs either the plugin's copy or the newest `dist/oxp3-tool` from GitHub (the file `get.sh`
  installs), checked against its published SHA-256.
- Fixes you applied keep working. If `status` says a helper is from an older version, apply that fix again.

### xess-xmx-linux

- **Each release gets its own folder** in `~/.local/share/oxp3deck/xess/`, such as `xess-xmx-linux-20261104`.
  Unpacking over the old folder would break the driver that Steam and games have loaded.
- **An update** runs the old folder's `uninstall.sh`, then the new folder's `install.sh`, both as your user. Then
  log out and back in.
- **The old folder stays** until after that re-login, because Steam still uses its driver until then. The XeSS page
  lists it under old package folders, with a button to remove it.
- **A hand install** such as `~/Documents/xess-xmx-linux-20261031` is adopted: the plugin shows it, and updates it
  the same way. Its folder is kept like any old folder.
- **Your default Mesa driver is kept.** Each release brings its own default. The plugin remembers your choice and
  sets it again after an update.
- **System Protons** such as CachyOS's `proton-cachyos-native` and `proton-cachyos-slr` live in
  `/usr/share/steam/compatibilitytools.d`, where `install.sh` cannot write. Games on them would stay on DP4a. The
  plugin adds the shim to them as root at the end of Install and Update, and when you tap the button on the XeSS
  page, and keeps the stock file next to it. A pacman update of such a Proton brings the stock file back; the
  plugin then adds the shim again when it next starts (at boot), or when you tap the button.
- **Check for an update** only asks GitHub for the newest release. It changes nothing.

## Per-game launch options

The plugin writes these into the game's Steam launch options, before `%command%`. A change applies at the game's
next start. No re-login is needed.

| Option | What it does | When to use it |
|---|---|---|
| `XESS=latest` | The game uses the XeSS libraries of Intel's XeSS SDK instead of its own older ones. They are swapped in only while the game runs, so Steam still sees the game as shipped. | The game's XeSS is older than 2.0.2.68 (XeSS then looks like DP4a, or offers no frame generation), or simply old. The Games page says so. |
| `XESS=dp4a` | XeSS without Intel's extension: super resolution on DP4a, frame generation on its generic path (2x only). | Comparing, or ruling out the XMX path as the cause of a problem. |
| `XESS_FG=2`, `3`, `4` | XeSS frame generation makes 2x, 3x or 4x frames, whatever the game's menu chooses. Frame generation must still be on in the game. | A game that offers only 2x. |
| `XESS_FG=5` to `8` | **Experimental**: beyond what Intel supports. 7x and 8x add a lot of latency. | A high refresh rate display that 4x does not fill. |
| `MESA=26.1`, `MESA=26.3` | This game uses that patched Mesa driver instead of the default. | A game misbehaves on one of the drivers. |
| `force_vk_vendor=-1` | Hides the Intel GPU from the game, so Mesa's own profile for it applies. XeSS then runs on DP4a. | A game that needs Mesa's workaround. |
| `XESS_LOG=1` | Writes the shim's, XeLL's and XeSS frame generation's logs into the game's prefix (`C:\igdext_trace.log`, `igdext_xell.log`, `igdext_xefg.log`, `igcl_stub.log`). | A bug report. |
| `FSR=xess` | The game's FSR 3.1+ upscaling runs on XeSS super resolution, and its FSR frame generation on XeSS frame generation. Needs xess-xmx-linux 20261104 or later. | A game with FSR 3.1+ and no XeSS. A game that has XeSS should use it. |
| `FSR_FG=xess` | Only the game's FSR frame generation runs on XeSS frame generation. Needs 20261104 or later. | As `FSR=xess`, but keeping the game's own FSR upscaling. |
| `FSR=xess FSR_FG=inject` | Adds XeSS frame generation to a game with FSR 3.1+ upscaling but no FSR frame generation. Needs 20261104 or later. | A game whose frame generation is DLSS only. |

- `XESS`, `XESS_LOG`, `FSR` and `FSR_FG` work through a small hook that `install.sh` puts into each Proton it can
  write to. The Games page says whether the game's Proton has the shim and the hook. Without the shim, XeSS stays on
  DP4a.
- Keep a frame limit on with frame generation, or the generated frames reach the screen unevenly.
- Everything else in the launch options stays as you wrote it: wrappers such as `gamemoderun`, other variables, and
  the game's own arguments. Options without `%command%` are game arguments, so the plugin writes
  `VARIABLES %command% ARGUMENTS`. A `%command` without its closing `%` is shown as a warning.

## What it never does

- **It changes nothing without a tap.** Fixes, installs, updates and reboots ask first.
- **Uninstalling the plugin reverts nothing and removes nothing.** Decky runs the same cleanup on every update of a
  plugin, so the plugin cannot tell a removal from an update. To undo things, use the buttons first: Revert on the
  Fixes page, Uninstall on the XeSS page. To remove oxp3-tool itself, see
  [Uninstall](https://github.com/Featherwolf/OXP3-Tool-for-Bazzite#uninstall).
- It never runs xess-xmx-linux's scripts as root. They run as your user, with a clean environment, so nothing of
  Decky's ends up in your session.
- It never stops a running oxp3-tool or xess-xmx-linux script halfway.
- It installs no system packages. The XeSS page lists tools that are missing, for you to install with pacman.
- It changes no launch option except the ones in the table above.

## Troubleshooting

- **The plugin's log** is in `~/homebrew/logs/OXP3Deck/`, one file per start (Decky keeps the newest few).
- **Updates & logs** shows the last tasks and their full logs. **Save a diagnose archive** writes
  `~/oxp3-diagnose-<time>.tar.gz`. Attach it to an issue.
- **Do not reboot** (red): a boot-entry rebuild did not finish. Reboot only after the command the warning names has
  run without errors. See [CachyOS](https://github.com/Featherwolf/OXP3-Tool-for-Bazzite#cachyos).
- **Re-login needed:** XeSS was installed or updated, but Steam still runs with the old driver settings. Log out and
  back in, or reboot.
- **XeSS driver check failed:** the XeSS page shows the line from `~/.cache/xess-xmx/driver-status`.
  `journalctl --user -u xess-xmx-check` has the details.
- **Steam from Flathub** (Flatpak) is not supported by xess-xmx-linux. The XeSS page says so.
- **The panel shows an error or does not load:** restart Decky with `sudo systemctl restart plugin_loader`, or
  reboot.
- The plugin keeps its settings in `~/homebrew/settings/OXP3Deck/settings.json` and task logs in
  `~/homebrew/data/OXP3Deck/jobs/`.

## Building from source

```bash
cd decky
pnpm install
pnpm build    # the frontend: dist/index.js
pnpm zip      # release/OXP3Deck.zip (python3 tools/build_zip.py)
```

`tools/build_zip.py` packs the frontend, `main.py`, `py_modules/oxp3deck/*.py`, the docs and the repository's
`dist/oxp3-tool` (checked against `dist/oxp3-tool.sha256`) into one `OXP3Deck/` folder. The same files always give
the same zip. `--out FILE` writes it elsewhere. If you changed `oxp3_tool/`, run `python3 tools/build_zipapp.py` in
the repository root first.

Tests:

```bash
python3 -m unittest discover -s tests -t . -v
node --experimental-strip-types --test tests/js/*.test.mjs   # launch options (Node 22.6 or later)
pnpm typecheck
```

[DESIGN.md](DESIGN.md) describes how the backend and the frontend fit together. [CREDITS.md](CREDITS.md) lists the
projects this one builds on.
