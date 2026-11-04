# OXP3 Deck: design

A Decky Loader plugin for the ONEXPLAYER 3 that installs, configures and updates, from one Quick Access panel:

1. **oxp3-tool** (this repository): the controller, sleep and display fixes, the button layout, the kernel switch.
2. **xess-xmx-linux** (github.com/HHHHanasak1/xess-xmx-linux): XeSS super resolution and frame generation on the XMX
   cores, and its per-game launch options.

Modelled on ClawDeck (github.com/IHulkSmashI/ClawDeck, MIT, MSI Claw on Bazzite): some backend helpers are adapted
from it (credited in CREDITS.md). It runs on CachyOS (Handheld Edition and desktop) and Bazzite.

## Rules

- **The plugin adds no logic of its own to what oxp3-tool does.** Every fix, state and check comes from
  `oxp3-tool <command> --json` (oxp3-tool 0.4.0 or later), run as root by the backend. The plugin is a front end.
- **xess-xmx-linux scripts run as the desktop user**, never as root (`runuser -u <user>`), with a minimal
  environment (`env -i` + HOME, USER, LOGNAME, PATH, XDG_RUNTIME_DIR, DBUS_SESSION_BUS_ADDRESS): its 20261031
  install.sh runs `systemctl --user import-environment` with no names, which would copy Decky's variables into the
  user's session.
- **Every backend call returns a dict** `{ok: bool, error: str|null, ...}` and never raises: Decky loses the message
  of a Python exception on its way to the frontend.
- **Nothing blocks the asyncio loop**: subprocesses and downloads run in an executor thread.
- **Long work is a job** (see Jobs): oxp3-tool apply/revert (a Limine rebuild can take minutes), xess install,
  update, uninstall, oxp3-tool update, diagnose, kernel try/keep. One job at a time; oxp3-tool also allows only one
  root run at a time (`/run/oxp3-tool.lock`), so the backend serialises every oxp3-tool call behind one lock.
- **Children get a clean environment**: Decky's PyInstaller bundle sets LD_LIBRARY_PATH (its own OpenSSL) and
  friends; main.py restores the `*_ORIG` values at import and every subprocess gets `clean_env()`.
- **HTTPS** uses the system CA bundle (or certifi when importable): Decky's Python has no CA store.
- **Root never writes through a link the user planted.** Decky gives the settings, data and log folders (and the
  home) to the user at every load. settings.json and the job files are written with `context.write_atomic`, which
  walks the folders above with `userexec.open_dir` (a symlink is followed only when root owns it and the folder it
  sits in, like `/home -> var/home` on Bazzite) and opens the last one with O_NOFOLLOW; files in the home are
  created by `userexec.write_new` (O_EXCL, O_NOFOLLOW, owned by the user) or by the user (`userexec.run`,
  `user_mkdirs`); downloads go into a root-only `mkdtemp`.
- **`_uninstall` undoes nothing**: Decky runs it on every update of the plugin too. Uninstalling oxp3-tool or
  xess-xmx-linux is an explicit button.
- **Every control explains itself** in its own `description` (the D-pad scrolls it into view), and every action
  shows its result or its error in a row under the control.

## Layout (the plugin folder, `decky/` in this repo, shipped as `OXP3Deck/` in the zip)

```
plugin.json            name "OXP3Deck", flags ["root"], api_version 1
package.json           version = DECKY_PLUGIN_VERSION
main.py                class Plugin: one async method per frontend call, thin
py_modules/oxp3deck/
  env.py               clean_env(), restore of Decky's *_ORIG variables
  context.py           Ctx (the desktop user, Decky's folders, a fake root for tests), settings.json
  sysexec.py           run a program with clean_env(), stdin /dev/null, output streamed line by line
  tls.py               SSL context with the system CA bundle (adapted from ClawDeck)
  userexec.py          run as the desktop user with a minimal environment (adapted from ClawDeck)
  downloads.py         GitHub release lookup, download, safe extract (adapted from ClawDeck)
  jobs.py              background jobs: one at a time, log lines, result, state file
  oxp3tool.py          run oxp3-tool --json; find, install and update the oxp3-tool binary
  xess.py              xess-xmx-linux: state, install/update, uninstall, driver switch, system Protons
  steamlib.py          Steam libraries, app install folders, compat tool of an app, prefixes
  games.py             per-game info: XeSS DLL versions in the game folder, last-run shim logs, advice
  pe.py                a DLL's file version (VS_FIXEDFILEINFO, the rule tools/proton-hook.py uses)
src/                   frontend (TypeScript, @decky/ui)
  api.ts               one `callable` per backend method; backend.ts wraps each so it never rejects
  types.ts             the answers below, as TypeScript types
  store.ts             cached answers per key, the current job and its polling
  actions.tsx          the confirm-then-start flows (apply, revert, installs, reboot)
  launchopts.ts        parse and change the managed launch-option variables (pure, tested under node)
  steam.ts             the running game, launch options through SteamClient
  pages/, components/  the quick panel, the full page's tabs, shared rows (Hint, JobView, GameControls, ...)
tests/                 unittest, with a stub `decky` module and fake roots; fixtures/oxp3: real oxp3-tool --json
                       output (tests/make_fixtures.py regenerates it from the repository's CLI); js/: node tests
                       of launchopts.ts; test_main.py also checks that api.ts and main.py name the same methods and
                       that main.py calls the real modules' functions with arguments they take
tools/build_zip.py     assembles OXP3Deck.zip from dist/ and the backend files
```

State the plugin keeps: `DECKY_PLUGIN_SETTINGS_DIR/settings.json` (the xess keys listed under the install job,
`oxp3_online` {version, commit, checked_at} of the last online check, `oxp3_reboot_pending` {boot_id, value} from
the last status or change, and the frontend's `ui_*` keys), `DECKY_PLUGIN_RUNTIME_DIR/jobs/<id>.json` (job state
and logs, the newest 30). oxp3-tool's output files and the diagnose archive pass through `/run/oxp3deck` (0700,
root's). Nothing inside the plugin folder (an update deletes it).

## Backend API (main.py)

All methods are `async def name(self, ...)` on `Plugin`, called from `src/api.ts` with `callable`. Types below are
TypeScript as seen by the frontend. Every result has `ok: boolean` and `error: string | null`.

### Overview

```ts
get_overview(): {
  ok, error,
  plugin_version: string,
  user: string, home: string,
  platform: "cachyos" | "bazzite" | null,      // from /etc/os-release
  device: { vendor: string|null, product: string|null, supported: boolean },  // DMI, ONEXPLAYER 3?
  oxp3: Oxp3Install,
  xess: XessSummary,
  job: Job | null,                              // the running job, if any
  reboot_pending: boolean,                      // from the last oxp3 status (kept in settings.json for this boot)
  do_not_reboot: string | null,                 // oxp3-tool's unfinished boot-entry rebuild (/run marker)
}
type Oxp3Install = { installed: boolean, path: string|null, version: string|null, json_ok: boolean,
                     min_version: "0.4.0", bundled_version: string|null }
type XessSummary = { installed: boolean, version: string|null, status: "ok"|"failed"|"unknown", line: string|null,
                     live: boolean, relogin_needed: boolean }
```

### oxp3-tool

```ts
oxp3_status(): { ok, error, status: Oxp3Status | null }        // oxp3-tool status --json, .status
oxp3_catalog(): { ok, error, catalog: Oxp3Catalog | null }     // oxp3-tool list --json, .catalog (cached per version)
oxp3_plan(fixes: string[], opts: ApplyOpts): { ok, error, plan: string[], log: string[] }   // apply --dry-run
oxp3_apply(fixes: string[], opts: ApplyOpts): JobStart          // apply FIX... --yes --json
oxp3_revert(fixes: string[]): JobStart                          // revert FIX... --yes --json
oxp3_kernel(): { ok, error, kernel: Oxp3Kernel | null }        // kernel --json, .kernel (Limine on CachyOS only)
oxp3_kernel_try(pkg: string): JobStart                          // kernel try PKG --yes --json
oxp3_kernel_keep(): JobStart                                    // kernel keep --yes --json
oxp3_diagnose(): JobStart                                       // diagnose -o /run/oxp3deck/... --json, then
                                                                // copied to ~/oxp3-diagnose-<time>.tar.gz (0600)
oxp3_update_check(): { ok, error, installed: string|null, bundled: string|null, online: string|null,
                       online_commit: string|null, update: "bundled"|"online"|null }
oxp3_install(source: "bundled"|"online"): JobStart              // install/update /usr/local/bin/oxp3-tool

type ApplyOpts = { home?: string, keyboard_key?: string, onex_key?: string, xbox_key?: string,
                   fps_limit?: number, min_refresh?: number, any_ssd?: boolean, no_restart?: boolean }
```

Fix ids, options and kernel packages are checked against `list --json` (button actions, the key flags, the
fps_limit / min_refresh ranges) and `kernel --json` before anything runs; a bad one answers `ok: false` and starts
no job. The fixes are always named (a plain `apply` would apply every recommended fix). oxp3-tool's output goes to
files in `/run/oxp3deck` (root only) and it runs in its own session, so a plugin reload does not cut it off; with
`--json` it prints nothing until it ends, so a job logs "still running" every 30 s and oxp3-tool's own lines at
the end. Status, list, kernel and a dry run answer `ok: false` ("busy") at once while a job's oxp3-tool run holds
the lock. The online update reads `__version__` and the dist files from one commit (the API's newest commit of the
default branch, `online_commit`; HEAD when the API does not answer), and the check is reused for an hour.

Job results (`Job.result`): apply / revert `{ok, error, rc, version, plan?, outcome}` (oxp3-tool's `outcome`:
`{ok, changed, reboot_needed, messages, errors}`; `version` is oxp3-tool's), kernel try / keep `{ok, error, rc,
version, action, kernel}`, diagnose `{ok, error, rc, path, findings}`, install `{ok, error, version, path, source,
commit}`. A refused argument or a failed run gives `{ok: false, error}` with whatever else was known. An apply or
revert whose outcome says `reboot_needed` sets `reboot_pending` at once (the next status confirms it).

`Oxp3Status`, `Oxp3Catalog` and `Oxp3Kernel` are exactly oxp3-tool's `--json` objects (`status`, `catalog`,
`kernel` keys); see oxp3_tool/cli.py `status_data`, `catalog_data` and kernel_cmd.py `describe_data`:

```ts
type Oxp3Fix = { id: string, group: "controller"|"display", level: "recommended"|"optional"|"experimental",
  title: string, summary: string, takes_effect: string,
  state: "applied"|"damaged"|"recommended"|"optional"|"experimental"|"n/a"|"reverting"|"incomplete"|"unknown",
  notes: string[], applied_at: string|null, options: Record<string, any>,
  pending: "reboot"|"inputplumber-restart"|null }
type Oxp3Status = { device: {vendor, product, bios, supported}, os: {name, platform, handheld, image, kernel},
  boot: string, secure_boot: boolean, can_change: boolean, needs_root: boolean, game_mode: boolean,
  game_mode_input: "ogui"|"steam"|null, warnings: string[], notes: string[], fixes: Oxp3Fix[],
  reboot_pending: boolean, rebuild_unfinished: string|null,
  refresh: { summary: string[], advice: string[] } | null, text: string }
type Oxp3Catalog = { fixes: {id, group, level, title, summary, details, takes_effect}[],
  button_actions: Record<string, string>,                         // action -> description
  button_keys: Record<"onex"|"keyboard"|"home"|"xbox", {label, default, stock, flag}>,
  fps_limit: {min, max, default}, min_refresh: {min, max, default} }
type Oxp3Kernel = { conf: string, kernels: {package, release, running, entry, usable, default}[],
  next_boot: {package: string|null, why: string, then: string|null}, remember_last_entry: boolean,
  trying: boolean, notes: string[] }
```

### xess-xmx-linux

```ts
xess_state(): { ok, error, xess: XessState }
xess_check_update(): { ok, error, installed: string|null, latest: string|null, update_available: boolean,
                       checked_at: number, note: string|null }    // note: e.g. "version unknown"
xess_install(): JobStart              // install, or update to the newest release (new folder; old one uninstalled)
xess_uninstall(): JobStart            // uninstall.sh of the installed package (keeps the folder)
xess_set_driver(prefix: string): { ok, error, drivers: XessDriver[], output?: string[] }
                                      // tools/switch-driver.sh PREFIX
xess_shim_system_protons(): { ok, error, done: string[], failed: string[], already: string[], notes: string[] }
                                      // root: proton-cachyos-* shim
xess_remove_folder(path: string): { ok, error, removed?: string }   // an old package folder (listed in
                                      // old_folders, in the home), only when nothing uses it any more

type XessDriver = { version: string, default: boolean }
type XessState = {
  installed: boolean,                   // install.sh's files (environment file or check unit) point at package_dir
  package_dir: string|null, version: string|null,      // version = folder tag
  found_by: "env"|"check-unit"|"settings"|null,  // settings: only the plugin's record (installed is then false)
  managed: boolean,                     // installed by this plugin (else: by hand, e.g. ~/Documents/...)
  drivers: XessDriver[], preferred_driver: string|null,
  status: { state: "ok"|"failed"|"unknown", line: string|null, driver: string|null, notes: string[],
            date: string|null, lib: string|null, driver_info: string|null, reason: string|null,
            switched_to: string|null },  // driver-status's first line, every form session-check.sh writes
  live: boolean,                        // the user session's VK_DRIVER_FILES points at this package
  steam_running: boolean,
  relogin_needed: boolean,              // installed/updated, but Steam still runs with the old environment
  units: { check: string|null, watcher: string|null },   // UnitFileState / ActiveState ("not-found"), one
                                                          // `systemctl --user show` for both units
  system_protons: { path: string, name: string, shimmed: boolean, stock: boolean }[],  // /usr/share and
                                        // /usr/local/share steam/compatibilitytools.d/*; stock: F.stock exists
  system_protons_need_shim: boolean,    // shimmed before (xess_shim_system), and a package update undid it
  user_protons: { name: string, path: string, shimmed: boolean, hook: boolean }[],
  missing_tools: string[],              // curl, ar, tar, zstd, sha256sum, cmp, python3; "vulkaninfo (optional)"
  old_folders: string[],                // earlier package folders that can be removed after a re-login
  flatpak_steam: boolean,
  latest: string|null, checked_at: number|null,   // from the last update check
  features: { fsr: boolean, alt_driver_store: boolean },   // FSR=xess (tools/fsr-fg.sh), INTC_ALT_... (20261104+)
}
```

The install job's result: `{ ok, error, tag, package_dir, previous, relogin_needed, next, warnings: string[],
notes: string[], status }`, or `{ ok: true, up_to_date: true, ... }`; on an install.sh failure `rolled_back`. The
uninstall job's: `{ ok, error, package_dir, restored: string[] (system Protons given their stock file back),
warnings, relogin_needed: true, next }`. `next` is the re-login line the frontend shows under the finished job.
At start-up (`_main`) a job `xess-reshim` shims the system Protons again when `system_protons_need_shim`. Its
steps (xess.py): refuse while a game runs, with only Flatpak Steam, or without the tools; the newest tag (same tag
with an ok driver check: up to date; same tag otherwise: install.sh again in that folder, nothing downloaded);
download into a root-only `mkdtemp` (no checksum is published); unpack as the user into
`~/.local/share/oxp3deck/xess/xess-xmx-linux-<tag>` (exec bits from the archive); copy `igc/`, `xess/`, `ffx/` from
the old package when the new release's `get-igc.sh` / `get-xess.sh` / `get-ffx.sh` is byte-identical; stop the shim
watcher; the old `uninstall.sh`; the new `install.sh` (rolled back with the old `install.sh` on failure); the stored
driver (`switch-driver.sh`); the system Protons as root; settings. Settings it keeps: `xess_package`, `xess_tag`,
`xess_old_folders`, `xess_installed_at`, `xess_uninstalled_at`, `xess_driver`, `xess_shim_system`, `xess_latest`,
`xess_checked_at`.

Where this follows the scripts rather than a simpler rule: libraries come from `steamapps/libraryfolders.vdf`, else
`config/libraryfolders.vdf` (tools/steam-libraries.sh); a system Proton's `F.stock` is written when it is missing
**or differs** from a stock `F` (install.sh's install_shim: a package update brings a new stock file), and a shim
older than the package's is replaced.

### Per game

```ts
game_info(appid: string): { ok, error, game: GameInfo }
type GameInfo = {
  appid: string, install_dir: string|null, prefix: string|null,
  compat_tool: string|null,             // Steam's CompatToolMapping for the app (null: Steam's default)
  default_compat_tool: string|null,     // CompatToolMapping "0"
  compat_tool_dir: string|null, compat_tool_system: boolean, compat_tool_hook: boolean|null,
  compat_tool_shimmed: boolean|null,    // does the Proton the game uses (its own, else Steam's default) carry the
                                        // shim? null: folder unknown, or a Proton without igdext (Proton 11.0)
  dlls: { name: string, path: string, version: string|null, kind: "xess"|"ffx", link: boolean }[],
                                        // libxess*.dll / libxell.dll and FSR 3.1+'s amd_fidelityfx_*.dll, searched
                                        // 10 folders deep (as tools/proton-hook.py), at most 20000 entries
  last_run: { trace: boolean, fallback: string|null, compile_failed: boolean, canary: boolean,
              xell_unsupported: boolean|null, logged_at: number|null } | null,
                                        // from the prefix (null: no prefix; xell_unsupported null: not logged)
  advice: string[],                     // e.g. "XeSS 2.0.1: add XESS=latest", "XeLL 1.2: frame generation may fail"
}
```

Launch options are read and written by the **frontend** (`SteamClient.Apps.RegisterForAppDetails`,
`SetAppLaunchOptions`): the plugin manages only these variables before `%command%` and keeps every other token:

| Variable | Values | Meaning |
|---|---|---|
| `XESS` | `latest`, `dp4a` | Intel's newer XeSS libraries while the game runs / XeSS without XMX |
| `XESS_FG` | `2`..`4` (`5`..`8` experimental) | frame generation multiplier |
| `MESA` | `26.1`, `26.3` | which patched driver the game uses |
| `force_vk_vendor` | `-1` | hide the Intel GPU from the game (Mesa's own profile for some games) |
| `XESS_LOG` | `1` | the shim's, XeLL's and XeFG's logs in the prefix |
| `FSR` | `xess` | the game's FSR 3.1+ upscaling and frame generation on XeSS (xess-xmx-linux 20261104+) |
| `FSR_FG` | `xess`, `inject` | only FSR frame generation on XeSS; `inject` (with `FSR=xess`) adds XeSS frame generation (20261104+) |

The frontend offers `FSR` / `FSR_FG` only when the installed package is 20261104 or newer (its tag, or
`features.fsr`), or when they are set already.

Options without `%command%` are game arguments: the plugin writes `VARS %command% ARGS`. A `%command` without the
closing `%` is shown as a warning.

### Jobs and misc

```ts
type JobStart = { ok, error, job: Job | null }
type Job = { id: string, kind: string, title: string, state: "running"|"done"|"failed",
             started: number, ended: number|null, log: string[], result: any, error: string|null }
job_status(id: string | null): { ok, error, job: Job | null }   // null: the running job, else the last one
job_list(): { ok, error, jobs: Job[] }                          // the last 10
reboot(): { ok, error }                                          // systemctl reboot
get_setting(key: string): any; set_setting(key: string, value: any): { ok, error }
```

Settings the frontend may read and write: `xess_driver` (a Mesa prefix such as "26.3", or null),
`xess_shim_system` (boolean) and `ui_*` keys (at most 64, each a JSON value of up to 4 KB); any other key reads as
null and is refused. `reboot()` is refused while a job runs or a boot-entry rebuild is unfinished. xess_set_driver,
xess_shim_system_protons and xess_remove_folder are refused while an xess job runs.

Jobs run in an executor thread, one at a time (a second start returns `ok: false`, "another task is running").
Their log is the job's own lines plus, for xess-xmx-linux scripts, the script's output as it arrives. A job's state
is written to `RUNTIME_DIR/jobs/<id>.json` when it starts, every few seconds while it logs, and when it ends; one
that was running when the plugin stopped is shown as failed ("interrupted") at the next start. oxp3-tool and xess
scripts started by a job are never killed.

## Frontend

- **Quick Access panel** (short):
  - Status: device and OS line; red "Do not reboot" when a boot-entry rebuild is unfinished; "Reboot to finish"
    button when a change waits for a reboot; the running job with its last log line.
  - oxp3-tool: install/update button when missing or outdated; recommended fixes not applied yet, with "Apply
    recommended" (confirm first).
  - XeSS: status line (driver, live, re-login needed), Install / Update button.
  - This game (only while a game runs): XeSS options as dropdowns writing the launch options, the advice lines.
  - "More" button → full page.
- **Full page** (`/oxp3deck/<tab>`, SidebarNavigation): Fixes, Buttons, XeSS, Games, Kernel (CachyOS + Limine
  only), Updates & logs.
  - Fixes: every fix with its state, notes and takes-effect text; Apply / Revert per fix with a confirmation that
    shows the dry-run plan.
  - Buttons: one dropdown per key (actions from the catalog), the frame-limit value, "Apply layout" (applies
    `buttons`, and `xbox-key` when the Xbox key needs its helper).
  - XeSS: version, driver status, default Mesa driver dropdown, Install / Update / Uninstall, system Protons,
    missing tools, old folders.
  - Games: the running game, or the last one; same controls as the quick panel plus the full advice and last-run
    details.
  - Kernel: installed kernels, next boot, Try (boot once) / Keep.
  - Updates & logs: oxp3-tool and xess versions with update checks, diagnose archive, the last jobs and their logs.
- **Jobs on screen** (components/JobView.tsx): the job a control started shows under that control, any other one at
  the top of the tab; once it ends, its result's lines follow the log (oxp3-tool's outcome errors and messages, the
  xess jobs' `warnings` and `notes`, diagnose's `findings`), then `next`, "Reboot to finish" for an outcome that
  needs one, and the diagnose archive's path.
- **A game's Proton** (components/GameControls.tsx) is described from `compat_tool_shimmed` (the shim: XeSS on the
  XMX cores) and `compat_tool_hook` (the hook: XESS=latest/dp4a, XESS_LOG, FSR); FSR choices show when the package
  is 20261104 or newer by its tag or `features.fsr`, or when they are set already.
