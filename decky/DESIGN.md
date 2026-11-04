# OXP3 Deck: design

A Decky Loader plugin for the ONEXPLAYER 3 that installs, configures and updates, from one Quick Access panel:

1. **oxp3-tool** (this repository): the controller, sleep and display fixes, the button layout, the kernel switch.
2. **xess-xmx-linux** (github.com/HHHHanasak1/xess-xmx-linux): XeSS super resolution and frame generation on the XMX
   cores, and its per-game launch options.

Modelled on ClawDeck (github.com/IHulkSmashI/ClawDeck, MIT, MSI Claw on Bazzite): some backend helpers are adapted
from it (credited in CREDITS.md). It runs on CachyOS (Handheld Edition and desktop) and Bazzite.

## Rules

- **The plugin adds no logic of its own to what oxp3-tool does.** Every fix, state and check comes from
  `oxp3-tool <command> --json` (oxp3-tool 0.4.1 or later; an early 0.4.0 build lacks fields the plugin reads),
  run as root by the backend. The plugin is a front end.
- **xess-xmx-linux scripts run as the desktop user**, never as root (`runuser -u <user>`), with a minimal
  environment (`env -i` + HOME, USER, LOGNAME, PATH, XDG_RUNTIME_DIR, DBUS_SESSION_BUS_ADDRESS): its 20261031
  install.sh runs `systemctl --user import-environment` with no names, which would copy Decky's variables into the
  user's session.
- **Every backend call returns a dict** `{ok: bool, error: str|null, ...}` and never raises: Decky loses the message
  of a Python exception on its way to the frontend. Answers (and get_setting's value) pass through main.py's
  `_sendable`: JSON types only, and text that is not UTF-8 (a lone surrogate from a file name) becomes "?", since
  Decky drops an answer it cannot encode and the frontend's call then never settles.
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
  sits in, like `/home -> var/home` on Bazzite) and opens the last one with O_NOFOLLOW (old job files are listed,
  read and removed through that same kind of folder descriptor, never by path again); files in the home are
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
  sysexec.py           run a program with clean_env(), stdin /dev/null, output streamed line by line (from pipes,
                       or from a root-only file it writes to); /proc start times of a pid
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
  xessoffer.ts         what the xess install button offers (install / update / repair; release tags ordered as
                       xess.tag_key) and what its confirmation says (pure, tested under node)
  steam.ts             the running game, launch options through SteamClient
  pages/, components/  the quick panel, the full page's tabs, shared rows (Hint, JobView, GameControls, ...)
tests/                 unittest, with a stub `decky` module and fake roots; fixtures/oxp3: real oxp3-tool --json
                       output (tests/make_fixtures.py regenerates it from the repository's CLI); js/: node tests
                       of launchopts.ts and xessoffer.ts; test_main.py also checks that api.ts and main.py name the
                       same methods and that main.py calls the real modules' functions with arguments they take
tools/build_zip.py     assembles OXP3Deck.zip from dist/ and the backend files
```

State the plugin keeps: `DECKY_PLUGIN_SETTINGS_DIR/settings.json` (the xess keys listed under the install job,
`oxp3_online` {version, commit, checked_at} of the last online check, `oxp3_reboot_pending` {boot_id, value} from
the last status or change, and the frontend's `ui_*` keys), `DECKY_PLUGIN_RUNTIME_DIR/jobs/<id>.json` (job state
and logs, the newest 30). oxp3-tool's output files, the xess-xmx-linux scripts' output (`user-*.log`) and the
diagnose archive pass through `/run/oxp3deck` (0700, root's); oxp3-tool's that a plugin stop left behind are removed
at the next start once oxp3-tool's lock is free, a script's `user-*.log` once no xess-xmx-linux script from before the
restart runs (`xess-script.json`). Nothing inside the plugin folder (an update deletes it).

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
                     min_version: "0.4.1", bundled_version: string|null }
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
xess_install(): JobStart              // install, or update to the newest release (new folder; old one uninstalled),
                                      // or repair (install.sh again in the installed folder); never a downgrade
xess_uninstall(): JobStart            // uninstall.sh of the installed package (keeps the folder)
xess_set_driver(prefix: string): { ok, error, drivers: XessDriver[], output?: string[] }
                                      // tools/switch-driver.sh PREFIX
xess_shim_system_protons(): { ok, error, done: string[], failed: string[], already: string[], notes: string[] }
                                      // root: proton-cachyos-* shim
xess_remove_folder(path: string): { ok, error, removed?: string }   // an old package folder (listed in
                                      // old_folders, in the home, named xess-xmx-linux-<tag> or the plugin's own,
                                      // not a git checkout), only when nothing uses it any more and it does not
                                      // hold the only compiler (igc/)

type XessDriver = { version: string, default: boolean }
type XessState = {
  installed: boolean,                   // install.sh's files (environment file or check unit) point at package_dir
  package_dir: string|null, version: string|null,      // version = folder tag
  found_by: "env"|"check-unit"|"settings"|null,  // settings: only the plugin's record (installed is then false)
  managed: boolean,                     // installed by this plugin (else: by hand, e.g. ~/Documents/...)
  package_dir_kept: string|null,        // why xess_remove_folder would refuse package_dir once it is old, whatever
                                        // else changes (outside the home, no package files, a git checkout, a name
                                        // other than xess-xmx-linux-<tag> and not the plugin's), as the end of a
                                        // sentence after the path; the update confirmation says the user deletes it
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
  old_folders: string[],                // earlier package folders that can be removed after a re-login; one with a
                                        // compiler is left out while the installed package has none
  old_folders_kept: { path: string, why: string }[],   // earlier ones xess_remove_folder always refuses (why: as
                                        // package_dir_kept); the XeSS tab shows them with no Remove button
  flatpak_steam: boolean,
  latest: string|null, checked_at: number|null,   // from the last update check; latest only when it is newer
                                        // than the installed release (xess.newer; any other tag when either cannot
                                        // be ordered), or when nothing is installed
  update_available: boolean,            // latest is set and something is installed
  missing_downloads: string[],          // "igc", "xess", "ffx": the package has tools/get-<piece>.sh, not its files
                                        // (install.sh only prints a line when a download fails, and exits 0)
  repair: string|null,                  // no newer release, a known version, but install.sh should run again in
                                        // this folder, and why: a missing download, the environment file names
                                        // another folder, the driver check line is missing or for another folder
                                        // (a run cut short), the check unit is missing, the driver check failed
  script_running: string|null,          // a package script an earlier plugin process started and that still runs
  features: { fsr: boolean, alt_driver_store: boolean },   // FSR=xess (tools/fsr-fg.sh), INTC_ALT_... (20261104+)
}
```

The install job's result: `{ ok, error, tag, package_dir, previous, relogin_needed, reboot_needed, next, warnings:
string[], notes: string[], status }`, or `{ ok: true, up_to_date: true, ... }`; on an install.sh failure
`rolled_back`. The uninstall job's: `{ ok, error, package_dir, restored: string[] (system Protons given their stock
file back), warnings, relogin_needed: true, next }`. `next` is the re-login line the frontend shows under the
finished job (a reboot line, with `reboot_needed: true`, when the new driver failed its check and the fallback is
not in place). At start-up (`_main`) a job `xess-reshim` shims the system Protons again when
`system_protons_need_shim`. Its steps (xess.py): refuse while a game runs, with only Flatpak Steam, or without the
tools; the newest tag (the installed tag, or a newer installed one: up to date when `repair` has nothing to say,
else install.sh again in that folder after copying a missing `igc/` / `xess/` / `ffx/` from an old folder as below,
nothing downloaded; an installed release newer than the newest with nothing to repair is refused, nothing changed);
download into a root-only `mkdtemp` (no checksum is published); unpack as the user into
`~/.local/share/oxp3deck/xess/xess-xmx-linux-<tag>` (exec bits from the archive); copy `igc/`, `xess/`, `ffx/` from
the old package when the new release's `get-igc.sh` / `get-xess.sh` / `get-ffx.sh` is byte-identical or pins the
same downloads (IGC_TAG and NEO_TAG, the same in 20261031 and 20261104; XESS_TAG, XESS_SHA256 and LIBS; FFX_TAG and
each file's checksum); stop the shim watcher; the old `uninstall.sh`; the new `install.sh` (rolled back with the old
`install.sh` on failure); a warning per piece install.sh could not download; the stored driver
(`switch-driver.sh`); the system Protons as root; settings; when the driver check failed, `systemctl --user restart
xess-xmx-check.service` (the unit is active since boot, so a log-out would not run it again): session-check.sh then
switches to the package's other driver or points the user manager at the stock drivers, and `next` asks for a
reboot only when neither happened. Settings it keeps: `xess_package`, `xess_tag`,
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

Options without `%command%` are game arguments: the plugin writes `VARS %command% ARGS`, each argument as a literal
shell word (`-name (Player One)` becomes `-name '(Player' 'One)'`), since Steam runs a line with `%command%` through a
shell. The old text is split at spaces, tabs and line ends (a no-break space stays in its word); quotes group words
as before and are dropped, a backslash is an ordinary character (`C:\Games\cfg.ini` stays as it is); with an
unclosed quote every word is taken as it is. A `%command` without the closing `%` is shown as a warning.

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
null and is refused. `reboot()` is refused while a job runs, an oxp3-tool run holds `/run/oxp3-tool.lock` (one that
outlived a plugin restart, or one from a terminal), an xess-xmx-linux script from before a plugin restart still runs
(`xess.script_running`) or a boot-entry rebuild is unfinished. xess_set_driver,
xess_shim_system_protons and xess_remove_folder are refused while an xess job runs.

Jobs run in an executor thread, one at a time (a second start returns `ok: false`, "another task is running").
Their log is the job's own lines plus, for xess-xmx-linux scripts, the script's output as it arrives. A job's state
is written to `RUNTIME_DIR/jobs/<id>.json` when it starts, every few seconds while it logs, and when it ends; one
that was running when the plugin stopped is shown as failed ("interrupted (the plugin restarted)") at the next start,
an oxp3-tool job whose run still holds oxp3-tool's lock as "the plugin restarted while oxp3-tool was running; it may
still be running: read the status when it has ended".

A plugin stop (Decky kills the process 5 s after asking) ends the job's own steps, not the programs it started:
oxp3-tool and the xess-xmx-linux scripts (install.sh, uninstall.sh, switch-driver.sh: `userexec.run(...,
detach=True)`) run in their own session with stdout and stderr going to a new root-only file in `/run/oxp3deck`
instead of a pipe, so they never die of SIGPIPE and run to their end, EXIT traps included; the plugin follows that
file while it lives and removes it when the program ends. A file does not slow its writer down as a pipe does, so
it is read every 20 ms while nothing new is in it, emptied when a read reaches its end past 8 MB, and emptied unread
at 16 MB (a program that writes faster than its lines are logged; one line says output was skipped): `yes` peaks at
about 35 MB. Short helpers (cp, rm, mv, the unpacker, systemctl) keep
their pipes. Every program run as the user gets its own session, so a timeout kills runuser's child too, not only
runuser. While an xess-xmx-linux script runs, `/run/oxp3deck/xess-script.json` (root's, 0600, written through the
folder's descriptor) names it with runuser's pid, its /proc start time and the plugin process; it is removed when the
script ends. After a plugin restart, while that process still runs (same pid and start time), xess_install,
xess_uninstall, xess_set_driver, xess_shim_system_protons and xess_remove_folder answer `ok: false` ("an
xess-xmx-linux script from before the plugin restarted is still running (...); try again when it has ended"),
`reboot()` too, `xess_state.script_running` names it, and the start-up re-shim does not run. The record is read,
checked and removed under one lock, and removed only while it is still the one read (or written), so a thread that
finds an old record ended cannot remove the live one a job has written since.

## Frontend

- **Quick Access panel** (short):
  - Status: device and OS line; red "Do not reboot" when a boot-entry rebuild is unfinished; "Reboot to finish"
    button when a change waits for a reboot; the running job with its last log line.
  - oxp3-tool: install/update button when missing or outdated; recommended fixes not applied yet, with "Apply
    recommended" (confirm first).
  - XeSS: status line (driver, live, re-login needed), Install / Update / Repair button (xessoffer.ts: Update only
    to a release tag newer than the installed one, from the overview's summary or a check tapped in this session;
    Repair, install.sh again for the same release, when `xess_state` says `repair`, with its reason).
  - This game (only while a game runs): XeSS options as dropdowns writing the launch options, the advice lines.
  - "More" button → full page.
- **Full page** (`/oxp3deck/<tab>`, SidebarNavigation): Fixes, Buttons, XeSS, Games, Kernel (CachyOS + Limine
  only), Updates & logs.
  - Fixes: every fix with its state, notes and takes-effect text; Apply / Revert per fix with a confirmation that
    shows the dry-run plan.
  - Buttons: one dropdown per key (actions from the catalog), the frame-limit value, "Apply layout" (applies
    `buttons`, and `xbox-key` when the Xbox key needs its helper).
  - XeSS: version, driver status, a package script from before a plugin restart that still runs (`script_running`:
    the xess buttons wait for it), default Mesa driver dropdown, Install / Update / Repair / Uninstall, system
    Protons, missing tools, downloads install.sh could not fetch (`missing_downloads`), old folders (Remove, or a
    note for one in `old_folders_kept`). The install confirmation says what the job will do: an update uninstalls the
    old package first (and says when its folder is the user's to delete: `package_dir_kept`); the default Mesa driver
    is promised back only when one was chosen in OXP3 Deck (`preferred_driver`); the system Protons get the shim as
    root. A page without `xess_state` (the quick panel, Updates) asks for it before the confirmation; when that fails
    too, the confirmation says both: the driver chosen in OXP3 Deck, else the release's, and the shim in any system
    Proton.
  - Games: the running game, or the last one; same controls as the quick panel plus the full advice and last-run
    details.
  - Kernel: installed kernels, next boot, Try (boot once) / Keep.
  - Updates & logs: oxp3-tool and xess versions with update checks, diagnose archive, the last jobs and their logs.
- **Jobs on screen** (components/JobView.tsx): the job a control started shows under that control, any other one at
  the top of the tab; once it ends, its result's lines follow the log (oxp3-tool's outcome errors and messages, the
  xess jobs' `warnings` and `notes`, diagnose's `findings`), then `next` (after an xess install whose new driver
  failed its check, a reboot instead of the log-out line, unless the result says `reboot_needed: false`), "Reboot to
  finish" for an outcome or a result with `reboot_needed`, and the diagnose archive's path.
- **A game's Proton** (components/GameControls.tsx) is described from `compat_tool_shimmed` (the shim: XeSS on the
  XMX cores) and `compat_tool_hook` (the hook: XESS=latest/dp4a, XESS_LOG, FSR); FSR choices show when the package
  is 20261104 or newer by its tag or `features.fsr`, or when they are set already.
