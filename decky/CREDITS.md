# Credits

## ClawDeck

OXP3 Deck is modelled on [ClawDeck](https://github.com/IHulkSmashI/ClawDeck), a Decky Loader plugin for the MSI
Claw on Bazzite. MIT License, Copyright (c) 2026 IHulkSmashI and ClawDeck contributors.

Adapted from it:

- **Backend helpers:** undoing the library paths Decky's bundled Python sets for its children (`env.py`), HTTPS with
  the system's CA certificates (`tls.py`), running commands as the desktop user (`userexec.py`), and GitHub release
  lookups, downloads and safe unpacking (`downloads.py`).
- **UI patterns:** a short Quick Access panel with a full page behind it, the page's routes and tabs, confirmation
  dialogs and hints, the per-game controls, and how Steam launch options are read and written.

Each adapted file starts with the line "Adapted from ClawDeck (MIT, Copyright (c) 2026 IHulkSmashI and ClawDeck
contributors)." ClawDeck's licence notice is in [LICENSE](LICENSE).

## xess-xmx-linux

[xess-xmx-linux](https://github.com/HHHHanasak1/xess-xmx-linux) by HHHHanasak1 runs XeSS super resolution and frame
generation on the XMX cores of Intel Arc GPUs under Linux. It brings patched Mesa drivers, a shim for Proton and the
per-game launch options. MIT License, Copyright (c) 2026 HHHHanasak1. The parts it uses from others (Mesa, Intel's
XeSS SDK and graphics compiler, AMD FidelityFX and more) keep their own licences, listed in its `THIRD_PARTY.md`.

OXP3 Deck does not include any of it. When you tap Install or Update, the plugin downloads a release from the
project's GitHub releases and runs the project's own scripts as your user. The launch options in
[README.md](README.md) are described after its `docs/launch-options.md`.

## Decky Loader

[Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) (GPL-2.0) loads and runs the plugin.
`dist/index.js` includes `@decky/api` (LGPL-2.1) and uses `@decky/ui` (LGPL-2.1), which Decky provides at run time.
Both are by the SteamDeckHomebrew team. The frontend is built with `@decky/rollup` (BSD-3-Clause).

## Icons

`dist/index.js` includes icons from [react-icons](https://github.com/react-icons/react-icons) (MIT). They are
Google's Material Design icons (Apache License 2.0).

## oxp3-tool

`bin/oxp3-tool` is oxp3-tool from this repository (`dist/oxp3-tool`). Where its fixes come from is in the
[main README](https://github.com/Featherwolf/OXP3-Tool-for-Bazzite#background).
