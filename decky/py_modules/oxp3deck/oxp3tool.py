"""oxp3-tool, this repository's CLI: every fix, state and check of the plugin comes from `oxp3-tool ... --json`.

oxp3-tool runs as root through the system Python (`/usr/bin/python3 -I`, never Decky's PyInstaller loader). Its
output goes to files in a folder only root can use, and it runs in its own session: Decky kills only the plugin
process on a reload, and a Limine rebuild must be allowed to finish and leave its answer. oxp3-tool takes a
non-blocking lock for every root run, so the plugin runs one oxp3-tool at a time (LOCK).

Everything here returns {ok, error, ...} and does not raise for expected failures.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
import time

from . import context, downloads, sysexec, userexec

log = logging.getLogger("oxp3deck")

MIN_VERSION = (0, 4, 0)
MIN_VERSION_TEXT = ".".join(map(str, MIN_VERSION))
LOCK = threading.Lock()          # held by every oxp3-tool run and by install

PYTHON = "/usr/bin/python3"      # the system's; tests point it elsewhere
SHELL = "/bin/sh"
INSTALL_PATH = "/usr/local/bin/oxp3-tool"
SYSTEM_PATHS = (INSTALL_PATH, "/usr/bin/oxp3-tool")
OLD_LIB_DIR = "/usr/local/lib/oxp3-tool"            # where an older install.sh put it (get.sh removes it)
RUN_DIR = "/run/oxp3deck"                             # oxp3-tool's output files; root only
REBUILD_MARKER = "/run/oxp3-tool-boot-entries-unfinished"   # oxp3_tool/kargs.py REBUILD_MARKER
# What kargs.py names when the marker is empty (its backends' finish_command), by the tool that is installed.
FINISH_COMMANDS = (("/usr/bin/limine-mkinitcpio", "limine-update"), ("/usr/bin/sdboot-manage", "sdboot-manage gen"),
                   ("/usr/bin/grub-mkconfig", "grub-mkconfig -o /boot/grub/grub.cfg"))

REPO = "Featherwolf/OXP3-Tool-for-Bazzite"
RAW = "https://raw.githubusercontent.com/" + REPO + "/{ref}/"
COMMITS_API = "https://api.github.com/repos/" + REPO + "/commits?per_page=1"
ONLINE_TTL = 3600.0              # seconds an online version check is reused
ONLINE_KEY = "oxp3_online"       # settings.json: {version, commit, checked_at}
MAX_TOOL_BYTES = 10_000_000      # dist/oxp3-tool is about 130 KB
MAX_OUTPUT = 64 << 20

HEARTBEAT = 30.0                 # seconds between "still running" lines (--json prints nothing until the end)
READ_WAIT = 120.0                # how long status, list, kernel and a dry run wait for another oxp3-tool run

# The apply options (DESIGN.md ApplyOpts) -> (key in the catalog's button_keys, flag in cli.py's apply parser).
BUTTON_OPTS = {"home": ("home", "--home"), "keyboard_key": ("keyboard", "--keyboard-key"),
               "onex_key": ("onex", "--onex-key"), "xbox_key": ("xbox", "--xbox-key")}
NUMBER_OPTS = {"fps_limit": ("--fps-limit", (10, 144)), "min_refresh": ("--min-refresh", (30, 48))}
SWITCH_OPTS = {"any_ssd": "--any-ssd", "no_restart": "--no-restart"}
_FIX_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_PACKAGE = re.compile(r"^[A-Za-z0-9._+-]+$")
_VERSION = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")

_cache_lock = threading.Lock()
_versions: dict = {}             # (path, inode, mtime, size) -> version text or None
_catalog: dict = {}              # {"key": the binary's stat key, "catalog": dict}
_holder: dict = {"cmd": None, "job": False}   # the oxp3-tool run holding LOCK


def _fail(error: str, **extra) -> dict:
    return {"ok": False, "error": error, **extra}


# ---- finding and versions -----------------------------------------------------------------------------------------


def _trusted(path: str, executable: bool = True) -> os.stat_result | None:
    """A regular file root may run: owned by root (or by us, in tests) and writable by nobody else."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or (executable and not st.st_mode & 0o111):
        return None
    if st.st_uid not in (0, os.geteuid()) or st.st_mode & 0o022:
        return None
    return st


def _key(path: str, st: os.stat_result) -> tuple:
    return (path, st.st_ino, st.st_mtime_ns, st.st_size)


def find(ctx) -> str | None:
    """/usr/local/bin/oxp3-tool (get.sh, the plugin), else /usr/bin/oxp3-tool (a package)."""
    for p in SYSTEM_PATHS:
        path = ctx.sys_path(p)
        if _trusted(path):
            return path
    return None


def parse_version(text) -> tuple | None:
    m = _VERSION.match(str(text or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)) if m else None


def _version_of(path: str, executable: bool = True) -> str | None:
    """`python3 -I PATH --version` -> "X.Y.Z", cached until the file changes."""
    st = _trusted(path, executable)
    if st is None:
        return None
    key = _key(path, st)
    with _cache_lock:
        if key in _versions:
            return _versions[key]
    version = _run_version(path)
    with _cache_lock:
        if len(_versions) > 16:
            _versions.clear()
        _versions[key] = version
    return version


def _run_version(path: str) -> str | None:
    r = sysexec.run([PYTHON, "-I", path, "--version"], timeout=60)
    m = re.match(r"^oxp3-tool (\S+)$", r.out.strip()) if r.rc == 0 else None
    if not m:
        log.warning("oxp3-tool: %s --version: rc %d, %s", path, r.rc, (r.err or r.out).strip()[-200:])
    return m.group(1) if m else None


def installed(ctx) -> dict:
    """Oxp3Install: the installed oxp3-tool and the copy bundled with the plugin."""
    path = find(ctx)
    version = _version_of(path) if path else None
    ver = parse_version(version)
    bundled = _version_of(os.path.join(ctx.plugin_dir, "bin", "oxp3-tool"), executable=False)
    return {"installed": path is not None, "path": path, "version": version,
            "json_ok": ver is not None and ver >= MIN_VERSION, "min_version": MIN_VERSION_TEXT,
            "bundled_version": bundled}


# ---- running it ---------------------------------------------------------------------------------------------------


def _run_dir(ctx) -> str:
    """ctx.sys_path(RUN_DIR), created 0700. Refused when it is a symlink or someone else's folder (the owner check is
    against our own uid, which is root in the plugin). Raises OSError."""
    path = ctx.sys_path(RUN_DIR)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as e:
        raise OSError(f"{path} is not a folder (a symlink?): {e.strerror or e}") from e
    try:
        st = os.fstat(fd)
        if st.st_uid != os.geteuid():
            raise OSError(f"{path} belongs to uid {st.st_uid}, not to root")
        if stat.S_IMODE(st.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
    return path


def _new_file(path: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    os.close(fd)


def _read(path: str, limit: int = MAX_OUTPUT) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(limit)
    except OSError:
        return b""


def _unlink(*paths: str) -> None:
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass


def _parse(text: str):
    """The whole output as one JSON object, else the last line that is one."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def _log_lines(obj: dict) -> list[tuple[str, str]]:
    out = []
    for e in obj.get("log") or []:
        if isinstance(e, dict):
            out.append((str(e.get("stream") or "out"), str(e.get("text") or "")))
        elif isinstance(e, str):
            out.append(("out", e))
    return out


def _why(obj: dict, lines: list[tuple[str, str]], rc: int) -> str:
    """Why a run failed, from its own words: its "error: " lines, else its last line."""
    errors = list(dict.fromkeys(t[len("error: "):] for s, t in lines if s == "err" and t.startswith("error: ")))
    if errors:
        return "; ".join(errors)
    outcome = obj.get("outcome")
    if isinstance(outcome, dict) and outcome.get("errors"):
        return "; ".join(str(e) for e in outcome["errors"])
    for stream in ("err", "out"):
        texts = [t.strip() for s, t in lines if s == stream and t.strip()]
        if texts:
            return texts[-1]
    return f"exited {rc}"


def _tail(*texts: str, n: int = 3) -> str:
    lines = [ln.strip() for t in texts for ln in (t or "").splitlines() if ln.strip()]
    return "; ".join(lines[-n:]) or "no output"


def run_json(ctx, args, *, on_line=None, wait: float | None = None) -> dict:
    """`oxp3-tool ARGS --json` as root: its JSON object with `ok`, `error`, `rc`, and `log` as a list of strings.
    on_line(line) gets a heartbeat every 30 s while it runs and, at the end, every line it logged (a job's say).
    wait: seconds to wait for another oxp3-tool run (None: as long as it takes); reads pass READ_WAIT and give up
    at once when a job's run holds the lock."""
    args = [str(a) for a in args]
    cmd = " ".join(args[:2] if args[:1] == ["kernel"] and len(args) > 1 else args[:1]) or "status"
    path = find(ctx)
    if path is None:
        return _fail(f"oxp3-tool {cmd}: oxp3-tool is not installed (install it under Updates)", rc=None)
    version = _version_of(path)
    if version is None:
        return _fail(f"oxp3-tool {cmd}: `{PYTHON} -I {path} --version` named no version (is python3 installed?)",
                     rc=None)
    if parse_version(version) is None or parse_version(version) < MIN_VERSION:
        return _fail(f"oxp3-tool {cmd}: version {version} is installed; OXP3 Deck needs {MIN_VERSION_TEXT} or newer "
                     "(update it under Updates)", rc=None)
    if wait is not None and _holder["job"]:
        return _fail(f"oxp3-tool {cmd}: busy: oxp3-tool {_holder['cmd']} is running (see the task)", rc=None)
    if not (LOCK.acquire() if wait is None else LOCK.acquire(timeout=wait)):
        return _fail(f"oxp3-tool {cmd}: busy: another oxp3-tool run has not finished; try again", rc=None)
    _holder.update(cmd=cmd, job=on_line is not None)
    try:
        return _run_locked(ctx, path, args, cmd, on_line)
    finally:
        _holder.update(cmd=None, job=False)
        LOCK.release()


def _run_locked(ctx, path: str, args: list[str], cmd: str, on_line) -> dict:
    say = _sayer(on_line)
    try:
        folder = _run_dir(ctx)
        stem = os.path.join(folder, f"{cmd.replace(' ', '-')}-{time.strftime('%Y%m%d-%H%M%S')}-"
                                    f"{secrets.token_hex(3)}")
        out_path, err_path = stem + ".out", stem + ".err"
        _new_file(out_path)
        _new_file(err_path)
    except OSError as e:
        return _fail(f"oxp3-tool {cmd}: cannot use {ctx.sys_path(RUN_DIR)}: {e.strerror or e}", rc=None)
    argv = [PYTHON, "-I", path, *args, "--json"]   # --json last: `kernel --json try` loses it
    # sh replaces itself with oxp3-tool writing to the files: no pipe to us, so a plugin restart does not cut it off.
    wrapper = [SHELL, "-c", 'o=$1; e=$2; shift 2; exec "$@" >>"$o" 2>>"$e"', "oxp3deck", out_path, err_path, *argv]
    extra = {"SUDO_UID": str(ctx.uid), "SUDO_GID": str(ctx.gid)} if ctx.uid else None   # like `sudo oxp3-tool`
    say(f"oxp3-tool {' '.join(args)}")
    stop = threading.Event()
    beat = threading.Thread(target=_heartbeat, args=(stop, cmd, say), name="oxp3-heartbeat", daemon=True)
    if on_line is not None:
        beat.start()
    try:
        r = sysexec.run(wrapper, env_extra=extra, start_new_session=True)
    finally:
        stop.set()
        if beat.is_alive():
            beat.join(5)   # no heartbeat after the log
    out = _read(out_path).decode("utf-8", errors="replace")
    err = _read(err_path, 1 << 20).decode("utf-8", errors="replace")
    _unlink(out_path, err_path)
    obj = _parse(out)
    if not isinstance(obj, dict):
        for line in (err + r.err).splitlines()[-20:]:
            say(line)
        return _fail(f"oxp3-tool {cmd} exited {r.rc}: {_tail(err, r.err, r.out)}", rc=r.rc)
    if obj.get("schema") != 1:
        return _fail(f"oxp3-tool {cmd}: JSON schema {obj.get('schema')!r}; OXP3 Deck reads schema 1 (update the "
                     "plugin)", rc=r.rc)
    lines = _log_lines(obj)
    for _stream, text in lines:
        say(text)
    ok = obj.get("ok") is True and r.rc == 0
    result = dict(obj)
    result.update(ok=ok, rc=r.rc, log=[t for _s, t in lines],
                  error=None if ok else f"oxp3-tool {cmd}: {_why(obj, lines, r.rc)}")
    return result


def _sayer(on_line):
    def say(line: str) -> None:
        if on_line is not None:
            try:
                on_line(line)
            except Exception:
                log.exception("oxp3-tool: on_line failed")
    return say


def _heartbeat(stop: threading.Event, cmd: str, say) -> None:
    start = time.monotonic()
    while not stop.wait(HEARTBEAT):
        s = int(time.monotonic() - start)
        say(f"oxp3-tool {cmd} is still running ({s // 60} min {s % 60:02d} s); its output comes when it ends")


# ---- read-only commands -------------------------------------------------------------------------------------------


def _part(r: dict, key: str) -> dict:
    """{ok, error, KEY}: oxp3-tool's object under KEY, as it gave it."""
    value = r.get(key) if isinstance(r.get(key), dict) else None
    if r.get("ok") and value is None:
        return {"ok": False, "error": f"oxp3-tool: no {key!r} in its answer", key: None}
    return {"ok": bool(r.get("ok")), "error": r.get("error"), key: value}


def status(ctx) -> dict:
    """{ok, error, status}: `oxp3-tool status --json`."""
    return _part(run_json(ctx, ["status"], wait=READ_WAIT), "status")


def catalog(ctx) -> dict:
    """{ok, error, catalog}: `oxp3-tool list --json`, kept until the binary changes."""
    path = find(ctx)
    st = _trusted(path) if path else None
    key = _key(path, st) if st else None
    with _cache_lock:
        if key is not None and _catalog.get("key") == key:
            return {"ok": True, "error": None, "catalog": json.loads(json.dumps(_catalog["catalog"]))}
    res = _part(run_json(ctx, ["list"], wait=READ_WAIT), "catalog")
    if res["ok"] and key is not None:
        with _cache_lock:
            _catalog.update(key=key, catalog=json.loads(json.dumps(res["catalog"])))
    return res


def kernel(ctx) -> dict:
    """{ok, error, kernel}: `oxp3-tool kernel --json` (CachyOS with Limine only; elsewhere oxp3-tool's error)."""
    return _part(run_json(ctx, ["kernel"], wait=READ_WAIT), "kernel")


# ---- validation ---------------------------------------------------------------------------------------------------


def check_fixes(ctx, fixes, what: str = "apply") -> tuple[list[str] | None, str | None]:
    """The fix ids, checked against the catalog, without repeats."""
    if not isinstance(fixes, (list, tuple)) or not fixes:
        return None, f"{what}: name at least one fix"
    if len(fixes) > 32 or not all(isinstance(f, str) and _FIX_ID.match(f) for f in fixes):
        return None, f"{what}: fix ids are short lowercase names, not {repr(fixes)[:200]}"
    cat = catalog(ctx)
    if not cat["ok"]:
        return None, f"{what}: cannot read the fix list: {cat['error']}"
    known = [f.get("id") for f in cat["catalog"].get("fixes") or [] if isinstance(f, dict)]
    unknown = [f for f in fixes if f not in known]
    if unknown:
        return None, f"{what}: unknown fix {', '.join(unknown)} (oxp3-tool knows {', '.join(map(str, known))})"
    return list(dict.fromkeys(fixes)), None


def _number(name: str, value, rng: dict | None, fallback: tuple[int, int]) -> tuple[int | None, str | None]:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        return None, f"{name} must be a whole number, not {value!r}"
    lo, hi = fallback
    if isinstance(rng, dict) and isinstance(rng.get("min"), int) and isinstance(rng.get("max"), int):
        lo, hi = rng["min"], rng["max"]
    if not lo <= value <= hi:
        return None, f"{name} must be {lo} to {hi}, not {value}"
    return value, None


def apply_args(ctx, fixes, opts) -> tuple[list[str] | None, str | None]:
    """["apply", FIX..., FLAGS...] for ApplyOpts, every value checked against the catalog and cli.py's parser.
    (None, why) when anything is off; nothing runs then."""
    ids, err = check_fixes(ctx, fixes, "apply")
    if err:
        return None, err
    opts = {} if opts is None else opts
    if not isinstance(opts, dict):
        return None, f"apply: options must be an object, not {type(opts).__name__}"
    unknown = sorted(str(k) for k in opts if k not in BUTTON_OPTS and k not in NUMBER_OPTS and k not in SWITCH_OPTS)
    if unknown:
        return None, f"apply: unknown option {', '.join(unknown)}"
    cat = catalog(ctx)["catalog"] or {}
    actions = cat.get("button_actions") if isinstance(cat.get("button_actions"), dict) else {}
    keys = cat.get("button_keys") if isinstance(cat.get("button_keys"), dict) else {}
    flags: list[str] = []
    for name, (key, flag) in BUTTON_OPTS.items():
        value = opts.get(name)
        if value is None:
            continue
        given = keys.get(key, {}).get("flag") if isinstance(keys.get(key), dict) else None
        if given not in (None, flag):
            return None, f"apply: oxp3-tool names the {key} key's option {given!r}, not {flag!r} (update the plugin)"
        if not isinstance(value, str) or value not in actions:
            return None, f"apply: {name} must be one of {', '.join(actions) or '(no actions known)'}, not {value!r}"
        flags += [flag, value]
    for name, (flag, fallback) in NUMBER_OPTS.items():
        value = opts.get(name)
        if value is None:
            continue
        n, err = _number(name, value, cat.get(name), fallback)
        if err:
            return None, f"apply: {err}"
        flags += [flag, str(n)]
    for name, flag in SWITCH_OPTS.items():
        value = opts.get(name)
        if value is None:
            continue
        if not isinstance(value, bool):
            return None, f"apply: {name} must be true or false, not {value!r}"
        if value:
            flags.append(flag)
    return ["apply", *ids, *flags], None


def check_kernel_package(pkg) -> str | None:
    """Why `pkg` is not a kernel package name, or None."""
    if not isinstance(pkg, str) or not _PACKAGE.match(pkg) or pkg.startswith("-") or len(pkg) > 128:
        return f"kernel try: {repr(pkg)[:60]} is not a package name"
    return None


# ---- changes (jobs) -----------------------------------------------------------------------------------------------


def _job_result(r: dict, *keys: str) -> dict:
    out = {"ok": bool(r.get("ok")), "error": r.get("error"), "rc": r.get("rc"), "version": r.get("version")}
    for k in keys:
        out[k] = r.get(k)
    return out


def plan(ctx, fixes, opts) -> dict:
    """{ok, error, plan, log}: `apply FIX... --dry-run`."""
    argv, err = apply_args(ctx, fixes, opts)
    if err:
        return {"ok": False, "error": err, "plan": [], "log": []}
    r = run_json(ctx, [*argv, "--dry-run"], wait=READ_WAIT)
    steps = r.get("plan") if isinstance(r.get("plan"), list) else []
    return {"ok": bool(r.get("ok")), "error": r.get("error"), "plan": [str(s) for s in steps],
            "log": r.get("log") or []}


def apply(ctx, job, fixes, opts) -> dict:
    argv, err = apply_args(ctx, fixes, opts)
    if err:
        return _fail(err)
    return _job_result(run_json(ctx, [*argv, "--yes"], on_line=job.say), "plan", "outcome")


def revert(ctx, job, fixes) -> dict:
    ids, err = check_fixes(ctx, fixes, "revert")
    if err:
        return _fail(err)
    return _job_result(run_json(ctx, ["revert", *ids, "--yes"], on_line=job.say), "outcome")


def kernel_try(ctx, job, pkg) -> dict:
    err = check_kernel_package(pkg)
    if err:
        return _fail(err)
    k = kernel(ctx)
    if not k["ok"]:
        return _fail(f"kernel try: {k['error']}")
    packages = [str(x.get("package")) for x in k["kernel"].get("kernels") or [] if isinstance(x, dict)]
    if pkg not in packages:
        return _fail(f"kernel try: {pkg} is not an installed kernel (installed: {', '.join(packages) or 'none'})")
    return _job_result(run_json(ctx, ["kernel", "try", pkg, "--yes"], on_line=job.say), "action", "kernel")


def kernel_keep(ctx, job) -> dict:
    return _job_result(run_json(ctx, ["kernel", "keep", "--yes"], on_line=job.say), "action", "kernel")


def diagnose(ctx, job) -> dict:
    """`oxp3-tool diagnose` into the root-only run folder, then a new file in the user's home, created by
    userexec.write_new (no symlink followed, owned by the user). {ok, error, path, findings}."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        tmp = os.path.join(_run_dir(ctx), f"oxp3-diagnose-{stamp}-{secrets.token_hex(3)}.tar.gz")
    except OSError as e:
        return _fail(f"diagnose: cannot use {ctx.sys_path(RUN_DIR)}: {e.strerror or e}")
    try:
        r = run_json(ctx, ["diagnose", "-o", tmp], on_line=job.say)
        if not r["ok"]:
            return _job_result(r, "path", "findings")
        data = _read(tmp, 1 << 30)
        if not data:
            return _fail(f"diagnose: oxp3-tool wrote no archive at {tmp}")
        dest = _free_name(ctx.home, f"oxp3-diagnose-{stamp}", ".tar.gz")
        try:
            userexec.write_new(ctx, dest, data, 0o600)
        except OSError as e:
            return _fail(f"diagnose: cannot write {dest}: {e.strerror or e}")
    finally:
        _unlink(tmp)
    job.say(f"Saved {dest}")
    found = (r.get("diagnose") or {}).get("findings") if isinstance(r.get("diagnose"), dict) else None
    return {"ok": True, "error": None, "rc": r.get("rc"), "path": dest,
            "findings": [str(f) for f in found] if isinstance(found, list) else []}


def _free_name(folder: str, stem: str, ext: str) -> str:
    for i in range(1, 100):
        p = os.path.join(folder, stem + ("" if i == 1 else "-" + str(i)) + ext)
        if not os.path.lexists(p):
            return p
    return os.path.join(folder, f"{stem}-{secrets.token_hex(3)}{ext}")


# ---- boot-entry rebuild marker ------------------------------------------------------------------------------------


def rebuild_unfinished(ctx) -> str | None:
    """The command that finishes oxp3-tool's unfinished boot-entry rebuild (its /run marker: do not reboot before it
    has run), or None. Reads one small file."""
    path = ctx.sys_path(REBUILD_MARKER)
    if not os.path.lexists(path):
        return None
    raw = context.read_small(path, 4096) or b""
    text = raw.decode("utf-8", errors="replace").strip().splitlines()
    line = "".join(ch for ch in (text[0] if text else "") if ch.isprintable())[:200].strip()
    if line:
        return line
    for tool, command in FINISH_COMMANDS:
        if os.path.exists(ctx.sys_path(tool)):
            return command
    return "limine-update"


# ---- updates ------------------------------------------------------------------------------------------------------


def _head_commit() -> str | None:
    """The newest commit of the default branch, so both files of an update come from one commit (raw HEAD files are
    cached separately). None when the API does not answer (60 requests an hour)."""
    try:
        data = json.loads(downloads.get_text(COMMITS_API, timeout=20, max_bytes=1_000_000))
    except (downloads.DownloadError, ValueError) as e:
        log.info("oxp3-tool: newest commit unknown: %s", e)
        return None
    sha = data[0].get("sha") if isinstance(data, list) and data and isinstance(data[0], dict) else None
    return sha if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha) else None


def _online(settings, force: bool = False) -> tuple[str | None, str | None, str | None]:
    """(version, commit, error) of oxp3-tool on GitHub, reused for an hour."""
    cached = settings.get(ONLINE_KEY)
    now = time.time()
    if not force and isinstance(cached, dict) and isinstance(cached.get("version"), str) \
            and isinstance(cached.get("checked_at"), (int, float)) and 0 <= now - cached["checked_at"] < ONLINE_TTL:
        commit = cached.get("commit")
        return cached["version"], commit if isinstance(commit, str) else None, None
    commit = _head_commit()
    url = RAW.format(ref=commit or "HEAD") + "oxp3_tool/__init__.py"
    try:
        text = downloads.get_text(url, timeout=30, max_bytes=100_000)
    except downloads.DownloadError as e:
        return None, None, f"online check: {e}"
    m = re.search(r"""^__version__\s*=\s*["']([0-9][0-9A-Za-z.+-]{0,40})["']""", text, re.M)
    if not m or parse_version(m.group(1)) is None:
        return None, None, f"online check: no __version__ in {url}"
    settings.set(ONLINE_KEY, {"version": m.group(1), "commit": commit, "checked_at": now})
    return m.group(1), commit, None


def update_check(ctx, settings) -> dict:
    """{ok, error, installed, bundled, online, online_commit, update}: "bundled" when the plugin's copy is newer
    than the installed one (or none is installed), else "online" when GitHub's is, else None."""
    inst = installed(ctx)
    online, commit, err = _online(settings)
    cur, bun, onl = parse_version(inst["version"]), parse_version(inst["bundled_version"]), parse_version(online)
    update = None
    if bun is not None and (cur is None or bun > cur):
        update = "bundled"
    elif onl is not None and (cur is None or onl > cur):
        update = "online"
    ok = err is None or update == "bundled"
    return {"ok": ok, "error": None if ok else err, "installed": inst["version"], "bundled": inst["bundled_version"],
            "online": online, "online_commit": commit, "update": update}


def _sha256_from(text: str, name: str = "oxp3-tool") -> str | None:
    """The digest for `name` in sha256sum's format ("HEX  NAME" or "HEX *NAME")."""
    lines = [ln.split() for ln in text.splitlines() if ln.strip()]
    for parts in lines:
        if len(parts) >= 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    if len(lines) == 1 and lines[0] and re.fullmatch(r"[0-9a-fA-F]{64}", lines[0][0]):
        return lines[0][0].lower()
    return None


def _write_new_file(path: str, data: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _fetch_bundled(ctx, dest: str) -> str | None:
    """Copy bin/oxp3-tool to dest after checking it against bin/oxp3-tool.sha256. Returns why not, or None."""
    src = os.path.join(ctx.plugin_dir, "bin", "oxp3-tool")
    if _trusted(src, executable=False) is None:
        return f"install: {src} is missing (or writable by others)"
    data = _read(src, MAX_TOOL_BYTES + 1)
    sums = (context.read_small(src + ".sha256", 10_000) or b"").decode("utf-8", errors="replace")
    expected = _sha256_from(sums)
    if expected is None:
        return f"install: {src}.sha256 holds no sha256 for oxp3-tool"
    digest = hashlib.sha256(data).hexdigest()
    if len(data) > MAX_TOOL_BYTES or digest != expected:
        return f"install: the plugin's oxp3-tool has sha256 {digest}, but bin/oxp3-tool.sha256 says {expected}"
    _write_new_file(dest, data, 0o700)
    return None


def _fetch_online(dest: str, job) -> tuple[str | None, str | None]:
    """Download dist/oxp3-tool and its .sha256 of one commit (HEAD when the API does not answer) to dest.
    Returns (commit, why not)."""
    commit = _head_commit()
    base = RAW.format(ref=commit or "HEAD") + "dist/"
    job.say(f"Downloading {base}oxp3-tool")
    try:
        expected = _sha256_from(downloads.get_text(base + "oxp3-tool.sha256", timeout=30, max_bytes=10_000))
        if expected is None:
            return commit, f"install: {base}oxp3-tool.sha256 holds no sha256 for oxp3-tool"
        downloads.download(base + "oxp3-tool", dest, expected_sha256=expected, max_bytes=MAX_TOOL_BYTES)
    except downloads.DownloadError as e:
        return commit, f"install: {e}"
    return commit, None


def _put_binary(ctx, data: bytes) -> str:
    """Write .oxp3-tool.new next to the target (exclusively, never through a link), fsync, rename it over
    oxp3-tool: the old binary stays until the new one is complete. Raises OSError."""
    folder = ctx.sys_path(os.path.dirname(INSTALL_PATH))
    os.makedirs(folder, mode=0o755, exist_ok=True)
    tmp, name = ".oxp3-tool.new", os.path.basename(INSTALL_PATH)
    dfd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        try:
            os.unlink(tmp, dir_fd=dfd)
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o755, dir_fd=dfd)
        try:
            with os.fdopen(fd, "wb") as f:
                os.fchmod(f.fileno(), 0o755)
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            raise
        try:
            os.fsync(dfd)
        except OSError:
            pass
    finally:
        os.close(dfd)
    return os.path.join(folder, name)


def install(ctx, settings, job, source) -> dict:
    """Install or update /usr/local/bin/oxp3-tool from the plugin's copy ("bundled") or from GitHub ("online"),
    the way get.sh does: checked sha256, `python3 -I FILE --version` must say oxp3-tool 0.4.0 or newer, then an
    atomic replace, the old /usr/local/lib/oxp3-tool removed, restorecon. {ok, error, version, path, source}."""
    if source not in ("bundled", "online"):
        return _fail(f"install: source must be 'bundled' or 'online', not {source!r}")
    with LOCK:
        _holder.update(cmd="install", job=True)
        try:
            try:
                tmpdir = tempfile.mkdtemp(prefix="install-", dir=_run_dir(ctx))   # 0700, ours only
            except OSError as e:
                return _fail(f"install: cannot make a private folder: {e.strerror or e}")
            try:
                return _install(ctx, settings, job, source, tmpdir)
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
        finally:
            _holder.update(cmd=None, job=False)


def _install(ctx, settings, job, source: str, tmpdir: str) -> dict:
    new = os.path.join(tmpdir, "oxp3-tool")
    commit = None
    try:
        if source == "bundled":
            why = _fetch_bundled(ctx, new)
        else:
            commit, why = _fetch_online(new, job)
    except OSError as e:
        why = f"install: {e.strerror or e}"
    if why:
        return _fail(why)
    r = sysexec.run([PYTHON, "-I", new, "--version"], timeout=60)
    m = re.match(r"^oxp3-tool (\S+)$", r.out.strip()) if r.rc == 0 else None
    if not m:
        return _fail(f"install: the file is not oxp3-tool: {_tail(r.out, r.err, n=2)}")
    version = m.group(1)
    ver = parse_version(version)
    if ver is None or ver < MIN_VERSION:
        return _fail(f"install: it is oxp3-tool {version}; OXP3 Deck needs {MIN_VERSION_TEXT} or newer")
    data = _read(new, MAX_TOOL_BYTES + 1)
    try:
        dest = _put_binary(ctx, data)
    except OSError as e:
        return _fail(f"install: {e.filename or ctx.sys_path(INSTALL_PATH)}: {e.strerror or e}")
    old = ctx.sys_path(OLD_LIB_DIR)
    try:
        if os.path.isdir(old) and not os.path.islink(old):
            shutil.rmtree(old)
        elif os.path.lexists(old):
            os.unlink(old)
    except OSError as e:   # the new binary is in place; the old folder is only clutter
        job.say(f"Could not remove {old}: {e.strerror or e}")
    restorecon = sysexec.which("restorecon")
    if restorecon:   # SELinux (Bazzite): give the new file its usual label
        rr = sysexec.run([restorecon, dest], timeout=60)
        if rr.rc != 0:
            job.say(f"restorecon {dest}: {_tail(rr.err, rr.out, n=1)}")
    with _cache_lock:
        _versions.clear()
    if source == "online":
        settings.set(ONLINE_KEY, {"version": version, "commit": commit, "checked_at": time.time()})
    job.say(f"Installed oxp3-tool {version} to {dest}")
    return {"ok": True, "error": None, "version": version, "path": dest, "source": source, "commit": commit}
