// The backend's answers, as DESIGN.md "Backend API" defines them. Oxp3Status, Oxp3Catalog and Oxp3Kernel are
// oxp3-tool's own --json objects (oxp3_tool/cli.py status_data / catalog_data, kernel_cmd.py describe_data).

/** Every backend result carries these two. */
export type Res = { ok: boolean; error: string | null };

export type Oxp3Install = {
  installed: boolean; path: string | null; version: string | null; json_ok: boolean;
  min_version: string; bundled_version: string | null;
};

export type XessSummary = {
  installed: boolean; version: string | null; status: "ok" | "failed" | "unknown"; line: string | null;
  live: boolean; relogin_needed: boolean;
};

export type Job = {
  id: string; kind: string; title: string; state: "running" | "done" | "failed";
  started: number; ended: number | null; log: string[]; result: any; error: string | null;
};
export type JobStart = Res & { job: Job | null };

export type Overview = Res & {
  plugin_version: string;
  user: string; home: string;
  platform: "cachyos" | "bazzite" | null;
  device: { vendor: string | null; product: string | null; supported: boolean };
  oxp3: Oxp3Install;
  xess: XessSummary;
  job: Job | null;
  reboot_pending: boolean;
  do_not_reboot: string | null;
};

// ---- oxp3-tool -----------------------------------------------------------------------------------------------------

export type FixState = "applied" | "damaged" | "recommended" | "optional" | "experimental" | "n/a" | "reverting"
  | "incomplete" | "unknown";

export type Oxp3Fix = {
  id: string; group: "controller" | "display"; level: "recommended" | "optional" | "experimental";
  title: string; summary: string; takes_effect: string; state: FixState;
  notes: string[]; applied_at: string | null; options: Record<string, any>;
  pending: "reboot" | "inputplumber-restart" | null;
};

export type Oxp3Status = {
  device: { vendor: string | null; product: string | null; bios: string | null; supported: boolean };
  os: { name: string | null; platform: "bazzite" | "cachyos" | null; handheld: boolean; image: string | null;
        kernel: string | null };
  boot: string; secure_boot: boolean; can_change: boolean; needs_root: boolean; game_mode: boolean;
  game_mode_input: "ogui" | "steam" | null; warnings: string[]; notes: string[]; fixes: Oxp3Fix[];
  reboot_pending: boolean; rebuild_unfinished: string | null;
  refresh: { summary: string[]; advice: string[] } | null; text: string;
};

export type Oxp3CatalogFix = {
  id: string; group: "controller" | "display"; level: "recommended" | "optional" | "experimental";
  title: string; summary: string; details: string; takes_effect: string;
};
export type ButtonKey = "onex" | "keyboard" | "home" | "xbox";
export type Oxp3Catalog = {
  fixes: Oxp3CatalogFix[];
  button_actions: Record<string, string>;
  button_keys: Record<ButtonKey, { label: string; default: string; stock: string; flag: string }>;
  fps_limit: { min: number; max: number; default: number };
  min_refresh: { min: number; max: number; default: number };
};

export type Oxp3KernelEntry = {
  package: string; release: string; running: boolean; entry: string | null; usable: boolean; default: boolean;
};
export type Oxp3Kernel = {
  conf: string; kernels: Oxp3KernelEntry[];
  next_boot: { package: string | null; why: string; then: string | null };
  remember_last_entry: boolean; trying: boolean; notes: string[];
};

export type ApplyOpts = {
  home?: string; keyboard_key?: string; onex_key?: string; xbox_key?: string;
  fps_limit?: number; min_refresh?: number; any_ssd?: boolean; no_restart?: boolean;
};

export type StatusRes = Res & { status: Oxp3Status | null };
export type CatalogRes = Res & { catalog: Oxp3Catalog | null };
export type PlanRes = Res & { plan: string[]; log: string[] };
export type KernelRes = Res & { kernel: Oxp3Kernel | null };
export type Oxp3UpdateCheck = Res & {
  installed: string | null; bundled: string | null; online: string | null; online_commit: string | null;
  update: "bundled" | "online" | null;
};

// ---- xess-xmx-linux ------------------------------------------------------------------------------------------------

export type XessDriver = { version: string; default: boolean };
/** ~/.cache/xess-xmx/driver-status's first line, in every form tools/session-check.sh writes (xess.parse_status). */
export type XessStatus = {
  state: "ok" | "failed" | "unknown"; line: string | null; driver: string | null; notes: string[];
  date: string | null; lib: string | null; driver_info: string | null; reason: string | null; switched_to: string | null;
};
export type XessState = {
  installed: boolean; package_dir: string | null; version: string | null;
  found_by: "env" | "check-unit" | "settings" | null;
  managed: boolean;
  package_dir_kept: string | null;         // why the XeSS tab will not remove package_dir once it is old (RC-2), or null
  drivers: XessDriver[]; preferred_driver: string | null;
  status: XessStatus;
  live: boolean;
  steam_running: boolean;
  relogin_needed: boolean;
  units: { check: string | null; watcher: string | null };
  system_protons: { path: string; name: string; shimmed: boolean; stock: boolean }[];
  system_protons_need_shim: boolean;
  user_protons: { name: string; path: string; shimmed: boolean; hook: boolean }[];
  missing_tools: string[];
  old_folders: string[];                   // earlier package folders the XeSS tab can remove (after a re-login)
  old_folders_kept: { path: string; why: string }[];   // earlier ones it leaves to the user: a git checkout, ...
  flatpak_steam: boolean;
  latest: string | null;                   // from the last update check; null unless newer than the installed package
  update_available: boolean;               // latest is set and something is installed
  checked_at: number | null;
  missing_downloads: string[];             // "igc", "xess", "ffx": the package's get-<piece>.sh exists, its files not
  repair: string | null;                   // install.sh should run again in this folder (same release), and why
  script_running: string | null;           // a package script an earlier plugin process started, still running
  features: { fsr: boolean; alt_driver_store: boolean };
};
export type XessStateRes = Res & { xess: XessState | null };
export type XessUpdateCheck = Res & {
  installed: string | null; latest: string | null; update_available: boolean; checked_at: number; note: string | null;
};
export type DriversRes = Res & { drivers: XessDriver[]; output?: string[] };
export type ShimRes = Res & { done: string[]; failed: string[]; already: string[]; notes: string[] };

// ---- per game ------------------------------------------------------------------------------------------------------

export type GameDll = { name: string; path: string; version: string | null; kind: "xess" | "ffx"; link: boolean };
export type GameLastRun = {
  trace: boolean; fallback: string | null; compile_failed: boolean; canary: boolean;
  xell_unsupported: boolean | null; logged_at: number | null;
};
export type GameInfo = {
  appid: string; install_dir: string | null; prefix: string | null;
  compat_tool: string | null;              // the game's own choice in Steam; null: Steam's default
  default_compat_tool: string | null;
  compat_tool_dir: string | null; compat_tool_system: boolean;
  compat_tool_hook: boolean | null;        // install.sh's user_settings.py (XESS=latest, XESS_LOG, FSR need it)
  compat_tool_shimmed: boolean | null;     // its igdext64.dll is the shim (XeSS on XMX needs it)
  dlls: GameDll[];
  last_run: GameLastRun | null;
  advice: string[];
};
export type GameInfoRes = Res & { game: GameInfo | null };

// ---- jobs and misc -------------------------------------------------------------------------------------------------

export type JobRes = Res & { job: Job | null };
export type JobListRes = Res & { jobs: Job[] };
