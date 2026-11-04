"""xess-xmx-linux (github.com/HHHHanasak1/xess-xmx-linux): what is installed, and install, update, uninstall, the
default driver and the shim in system Protons.

The package is a folder the user unpacked somewhere (the owner's: ~/Documents/xess-xmx-linux-20261031) whose
install.sh points the session at it: ~/.config/environment.d/50-xess-xmx.conf (VK_DRIVER_FILES=PKG/run/
intel_icd.json:...), the shim in every Proton and prefix the user can write, a watcher unit (xess-xmx-shim.path) and
a driver check run before each session (xess-xmx-check.service, which writes ~/.cache/xess-xmx/driver-status). The
release carries no version file: the tag is only in the folder name.

Every package script runs as the desktop user (userexec). Root does two things only: the download (into a folder
only root can write) and the shim in system Protons (/usr/share/steam/compatibilitytools.d, CachyOS'
proton-cachyos-*), which install.sh merely lists with sudo commands.

An update never unpacks over a package folder: the running session has its drivers mapped. It unpacks the new
release next to the plugin's other ones, runs the old folder's uninstall.sh and then the new one's install.sh (the
20261104 README: "to move it: uninstall.sh first"), and keeps the old folder until the user removes it after a
re-login (until then Steam and its games still use the old folder's driver).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import time

from . import context, downloads, steamlib, sysexec, userexec

log = logging.getLogger("oxp3deck")

REPO = "HHHHanasak1/xess-xmx-linux"
NAME_PREFIX = "xess-xmx-linux-"
ENV_FILE = ".config/environment.d/50-xess-xmx.conf"
STATUS_FILE = ".cache/xess-xmx/driver-status"
UNIT_DIR = ".config/systemd/user"
CHECK_UNIT = "xess-xmx-check.service"
WATCH_PATH = "xess-xmx-shim.path"
WATCH_SERVICE = "xess-xmx-shim.service"
MANAGED_DIR = ".local/share/oxp3deck/xess"
IGDEXT_REL = "files/lib/wine/igdext/x86_64-windows/igdext64.dll"
MARKER = b"IGDEXT_"        # every build of the shim names its IGDEXT_* switches; a stock igdext64.dll does not
MAX_ZIP = 200 << 20        # the releases are 16 MB
MAX_UNPACKED = 1 << 30
DLL_MAX = 64 << 20
TMP_PARENT: str | None = None    # where the download folder is made (tests)

NEXT_INSTALL = "Log out of Game Mode and back in (or reboot) so Steam starts with the new driver."
NEXT_UNINSTALL = "Log out of Game Mode and back in (or reboot) so Steam starts without the patched driver."
NEXT_STOCK = ("Log out of Game Mode and back in (or reboot): Steam then starts with the system's stock Intel driver "
              "(XeSS on DP4a).")
NEXT_REBOOT = ("Reboot (not just log out): the session check, which falls back to the package's other driver or to the "
               "stock one, runs again only at boot. After a log-out alone Game Mode may not start.")
SCRIPT_RECORD = "xess-script.json"   # in sysexec.run_dir: the package script running detached (_run_script)
HOOK_NOTE = ("System Protons get the shim but not the Proton hook (proton-cachyos-native ships its own "
             "user_settings.py as a package file): XMX and frame generation work there, the launch option shorthands "
             "XESS=latest, XESS=dp4a and XESS_LOG=1 (and FSR, FSR_FG) do not.")
UPDATE_NOTE = ("An update of that Proton package puts its stock igdext64.dll back; the XeSS page then offers to shim "
               "again.")

_DRIVER_PREFIX = re.compile(r"^[0-9]+(\.[0-9A-Za-z-]+)*$")
_DATE_TAG = re.compile(r"^([0-9]{8})(?:-([0-9]+))?$")
_V_TAG = re.compile(r"^v([0-9]+(?:\.[0-9]+)*)$")
_SAFE_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,99}$")

# Host tools the scripts call: get-igc.sh (curl, ar, tar; zstd for the .deb's data.tar.zst, README 20261104:101),
# get-xess.sh and get-ffx.sh (curl, sha256sum; unzip or python3, and python3 is needed anyway), session-check.sh
# (python3 loads the driver; vulkaninfo is optional: without it the device test is skipped), install.sh (cmp).
# (name, required, CachyOS package, Bazzite package)
TOOLS = (
    ("curl", True, "curl", "curl"),
    ("ar", True, "binutils", "binutils"),
    ("tar", True, "tar", "tar"),
    ("zstd", True, "zstd", "zstd"),
    ("sha256sum", True, "coreutils", "coreutils"),
    ("cmp", True, "diffutils", "diffutils"),
    ("python3", True, "python", "python3"),
    ("vulkaninfo", False, "vulkan-tools", "vulkan-tools"),
)

# Re-checks every name (the archive is read again by another process) and unpacks with zipfile as the desktop user.
# zipfile drops the permission bits: the release's .sh and .py files are 0755 (make-release.py), install.sh only
# chmods tools/*, and launch options call xmx-launch.sh directly, so the bits are put back from the archive.
_EXTRACT = r"""
import os, stat, sys, zipfile
zpath, dest, top, limit = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
with zipfile.ZipFile(zpath) as z:
    infos = z.infolist()
    total = 0
    for i in infos:
        n = i.filename
        parts = n.rstrip("/").split("/")
        kind = stat.S_IFMT(i.external_attr >> 16)
        if ("\\" in n or n.startswith("/") or any(p in ("", ".", "..") for p in parts) or parts[0] != top
                or kind not in (0, stat.S_IFREG, stat.S_IFDIR) or (len(parts) == 1 and not i.is_dir())):
            sys.exit("unsafe entry in the archive: %r" % n)
        total += i.file_size
    if total > limit:
        sys.exit("the archive unpacks to %d bytes, more than %d" % (total, limit))
    files = 0
    for i in infos:
        target = os.path.join(dest, *i.filename.rstrip("/").split("/"))
        if i.is_dir():
            os.makedirs(target, 0o755, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), 0o755, exist_ok=True)
        mode = 0o755 if (i.external_attr >> 16) & 0o111 else 0o644
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, "wb") as out, z.open(i) as src:
            left = i.file_size
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                left -= len(chunk)
                if left < 0:
                    sys.exit("%s is bigger than the archive says" % i.filename)
                out.write(chunk)
        os.chmod(target, mode)
        files += 1
print("unpacked %d files" % files)
"""

# One XeSS task at a time (install, uninstall, driver switch, system Protons, folder removal); a second one is
# refused, not queued.
_BUSY = threading.Lock()
# SCRIPT_RECORD is read, checked and removed (script_running, any thread: xess_state takes no _BUSY), or written
# (_run_script), by one thread at a time (RS-4).
_RECORD = threading.Lock()
_marker_cache: dict = {}


def _err(step: str, msg: str, **extra) -> dict:
    d = {"ok": False, "error": f"{step}: {msg}"}
    d.update(extra)
    return d


def _say(job, line: str) -> None:
    if job is not None:
        job.say(line)
    else:
        log.info("xess: %s", line)


def _tail(r: sysexec.Result, n: int = 6) -> str:
    lines = [s for s in (r.out + "\n" + r.err).splitlines() if s.strip()]
    return " / ".join(lines[-n:]) or f"exit {r.rc}"


def _real(p: str | None) -> str | None:
    return os.path.realpath(p) if p else None


def _under(path: str, folder: str) -> bool:
    folder = folder.rstrip("/")
    return path == folder or path.startswith(folder + "/")


# ---- files and markers ---------------------------------------------------------------------------------------------

def has_marker(path: str, marker: bytes = MARKER) -> bool | None:
    """Whether the file contains `marker` (a shim igdext64.dll), None when it cannot be read as a regular file.
    Results are cached by inode, size and mtime: state() asks this of every Proton each time the page polls."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        key = (path, marker, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        if key in _marker_cache:
            return _marker_cache[key]
        found, tail, done = False, b"", 0
        while done < DLL_MAX:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            done += len(chunk)
            if marker in chunk or marker in tail + chunk[:len(marker)]:
                found = True
                break
            tail = chunk[-(len(marker) - 1):]
        if len(_marker_cache) > 512:
            _marker_cache.clear()
        _marker_cache[key] = found
        return found
    except OSError:
        return None
    finally:
        os.close(fd)


def _read_text(path: str, max_bytes: int = 1 << 20) -> str | None:
    raw = context.read_small(path, max_bytes)
    return raw.decode("utf-8", errors="replace") if raw is not None else None


def env_values(ctx) -> dict:
    """~/.config/environment.d/50-xess-xmx.conf as a dict (quotes removed)."""
    out = {}
    for line in (_read_text(os.path.join(ctx.home, ENV_FILE)) or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def _parse_env_listing(text: str) -> dict:
    """`systemctl --user show-environment` output. A value with special characters is printed as $'...'."""
    out = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if not sep:
            continue
        if v.startswith("$'") and v.endswith("'"):
            v = v[2:-1]
        elif len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k] = v
    return out


def _icd_package(vk: str | None) -> str | None:
    """The package a VK_DRIVER_FILES value puts first (PKG/run/intel_icd.json)."""
    first = (vk or "").split(":")[0]
    if first.endswith("/run/intel_icd.json"):
        return os.path.dirname(os.path.dirname(first))
    return None


def _points_at(vk: str | None, pkg: str | None) -> bool:
    """VK_DRIVER_FILES starts with PKG/run/intel_icd.json (switch-driver.sh's test), PKG compared resolved."""
    p = _icd_package(vk)
    return bool(p and pkg and os.path.realpath(p) == os.path.realpath(pkg))


def _references(vk: str | None, folder: str) -> bool:
    """Any VK_DRIVER_FILES entry lies inside `folder`."""
    real = os.path.realpath(folder)
    for entry in (vk or "").split(":"):
        if entry and (_under(entry, folder) or _under(os.path.realpath(entry), real)):
            return True
    return False


# ---- which package ---------------------------------------------------------------------------------------------------

def looks_like_package(p: str | None) -> bool:
    return bool(p) and all(os.path.isfile(os.path.join(p, f))
                           for f in ("install.sh", "uninstall.sh", "tools/session-check.sh"))


def _package_from_env(env: dict) -> list[str]:
    out = []
    p = _icd_package(env.get("VK_DRIVER_FILES"))
    if p is None:   # later entries, in case another tool put an ICD in front
        for icd in env.get("VK_DRIVER_FILES", "").split(":"):
            if icd.endswith("/run/intel_icd.json"):
                p = os.path.dirname(os.path.dirname(icd))
                break
    if p:
        out.append(p)
    helper = env.get("ANV_CM_HELPER", "")
    if helper.endswith("/tools/cm-compile.sh"):
        h = os.path.dirname(os.path.dirname(helper))
        if h not in out:
            out.append(h)
    return out


def _package_from_unit(ctx) -> str | None:
    text = _read_text(os.path.join(ctx.home, UNIT_DIR, CHECK_UNIT)) or ""
    m = re.search(r'^ExecStart=\S*bash\s+"([^"]+)/tools/session-check\.sh"', text, re.M) or \
        re.search(r"^ExecStart=\S*bash\s+(\S+)/tools/session-check\.sh\s*$", text, re.M)
    return m.group(1) if m else None


def detect(ctx, settings) -> dict:
    """{path, how, installed, env_pkg}: the package folder (resolved) and how it was found. "env": the session
    environment file names it (VK_DRIVER_FILES, cross-checked with ANV_CM_HELPER); "check-unit": the driver check
    unit runs its session-check.sh; "settings": only the plugin's record of what it installed, i.e. install.sh's
    files are gone (uninstalled by hand), so `installed` is false."""
    env = env_values(ctx)
    env_pkgs = _package_from_env(env)
    cands = [(p, "env") for p in env_pkgs]
    unit = _package_from_unit(ctx)
    if unit:
        cands.append((unit, "check-unit"))
    s = settings.get("xess_package") if settings is not None else None
    if isinstance(s, str) and s.startswith("/"):
        cands.append((s, "settings"))
    env_pkg = env_pkgs[0] if env_pkgs else None
    for p, how in cands:
        if looks_like_package(p):
            return {"path": os.path.realpath(p), "how": how, "installed": how != "settings", "env_pkg": env_pkg,
                    "env": env}
    return {"path": None, "how": None, "installed": False, "env_pkg": env_pkg, "env": env}


def package_version(pkg: str | None, settings) -> str | None:
    """The release tag: the folder name xess-xmx-linux-<tag>, else the tag the plugin recorded for that folder."""
    if not pkg:
        return None
    name = os.path.basename(pkg.rstrip("/"))
    if name.startswith(NAME_PREFIX) and len(name) > len(NAME_PREFIX):
        return name[len(NAME_PREFIX):]
    if settings is not None:
        rec = settings.get("xess_package")
        tag = settings.get("xess_tag")
        if isinstance(rec, str) and isinstance(tag, str) and tag and os.path.realpath(rec) == os.path.realpath(pkg):
            return tag
    return None


def _managed_root(ctx) -> str:
    return os.path.join(ctx.home, MANAGED_DIR)


def is_managed(ctx, pkg: str | None) -> bool:
    return bool(pkg) and _under(os.path.realpath(pkg), os.path.realpath(_managed_root(ctx)))


def tag_key(tag: str | None):
    """Sort key of a release tag: YYYYMMDD[-N] as (date, N), the old v1.x tags below every date tag; None when the
    tag has neither form."""
    if not tag:
        return None
    m = _DATE_TAG.match(tag)
    if m:
        return (1, int(m.group(1)), int(m.group(2) or 1))
    m = _V_TAG.match(tag)
    if m:
        return (0, tuple(int(p) for p in m.group(1).split(".")))
    return None


def newer(latest: str | None, installed: str | None) -> bool | None:
    """latest > installed, or None when either tag is not comparable."""
    a, b = tag_key(latest), tag_key(installed)
    if a is None or b is None:
        return None
    return a > b


def _update_offered(latest: str | None, installed: str | None) -> bool:
    """check_update's rule: a newer tag; when either tag cannot be ordered (or the installed one is unknown), any
    other tag."""
    if not latest:
        return False
    cmp = newer(latest, installed)
    return latest != installed if cmp is None else cmp


# ---- driver status ---------------------------------------------------------------------------------------------------

_OK = re.compile(r"^ok (\S+) (\S+) (.*?/libvulkan_intel\.so)(?: (.*))?$")
_FAILED = re.compile(r"^FAILED (\S+) (\S+) (.*?/libvulkan_intel\.so): (.*)$")
_SWITCHED = re.compile(r"\s*\(the previous driver failed: (.*); switched to Mesa (\S+?)\)\s*$")
_NOTE = re.compile(r"\s*\(note: ([^)]*)\)")
_LIB_MESA = re.compile(r"/lib/mesa-([^/]+)/libvulkan_intel\.so$")
_INFO_MESA = re.compile(r"\bMesa ([0-9][0-9A-Za-z.+-]*)")


def parse_status(line: str | None) -> dict:
    """The first line of ~/.cache/xess-xmx/driver-status, in the forms tools/session-check.sh writes:
    "ok DATE TIME LIB DRIVERINFO" with " (note: ...)" parts (:102, :111, :116 in 20261031), plus
    " (the previous driver failed: REASON; switched to Mesa V)" when the session check changed the default (:33),
    and "FAILED DATE TIME LIB: REASON" (:39)."""
    out = {"state": "unknown", "line": None, "driver": None, "notes": [], "date": None, "lib": None,
           "driver_info": None, "reason": None, "switched_to": None}
    first = (line or "").splitlines()[0].strip() if (line or "").strip() else ""
    if not first:
        return out
    out["line"] = first
    m = _OK.match(first)
    if m:
        out.update(state="ok", date=f"{m.group(1)} {m.group(2)}", lib=m.group(3))
        rest = m.group(4) or ""
        sw = _SWITCHED.search(rest)
        if sw:
            rest = rest[:sw.start()]
            out["reason"], out["switched_to"] = sw.group(1), sw.group(2)
        out["notes"] = [n.strip() for n in _NOTE.findall(rest)]
        info = _NOTE.sub("", rest).strip()
        out["driver_info"] = info or None
        if sw:
            out["notes"].append(f"the previous driver failed: {sw.group(1)}; switched to Mesa {sw.group(2)}")
    else:
        m = _FAILED.match(first)
        if not m:
            return out
        out.update(state="failed", date=f"{m.group(1)} {m.group(2)}", lib=m.group(3), reason=m.group(4))
        out["notes"] = [m.group(4)]
    lm = _LIB_MESA.search(out["lib"] or "")
    im = _INFO_MESA.search(out["driver_info"] or "")
    out["driver"] = lm.group(1) if lm else (im.group(1) if im else None)
    return out


def read_status(ctx) -> dict:
    return parse_status(_read_text(os.path.join(ctx.home, STATUS_FILE), 65536))


def drivers(pkg: str | None) -> list[dict]:
    """[{version, default}] from lib/mesa-*/libvulkan_intel.so; lib/active names the default."""
    if not pkg:
        return []
    lib = os.path.join(pkg, "lib")
    active = (_read_text(os.path.join(lib, "active"), 4096) or "").strip()
    try:
        names = os.listdir(lib)
    except OSError:
        return []
    vers = [n[5:] for n in names
            if n.startswith("mesa-") and os.path.isfile(os.path.join(lib, n, "libvulkan_intel.so"))]

    def natural(v: str):
        return [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"([0-9]+)", v) if p]

    return [{"version": v, "default": v == active} for v in sorted(vers, key=natural)]


# ---- user session ----------------------------------------------------------------------------------------------------

def _session_env(ctx) -> dict | None:
    r = userexec.run(ctx, ["systemctl", "--user", "show-environment"], timeout=10)
    return _parse_env_listing(r.out) if r.rc == 0 else None


def _units(ctx) -> dict:
    """{check, watcher}: the check unit's file state (is-enabled) and the watcher's active state (is-active), read
    with one `systemctl --user show` for both units."""
    r = userexec.run(ctx, ["systemctl", "--user", "show", "-p", "LoadState", "-p", "UnitFileState", "-p",
                           "ActiveState", CHECK_UNIT, WATCH_PATH], timeout=10)
    blocks = [b for b in r.out.strip().split("\n\n")] if r.rc == 0 else []
    if len(blocks) != 2:
        return {"check": None, "watcher": None}
    props = [dict(line.partition("=")[::2] for line in b.splitlines() if "=" in line) for b in blocks]

    def value(p: dict, key: str) -> str | None:
        if p.get("LoadState") == "not-found":
            return "not-found"
        return p.get(key) or None

    return {"check": value(props[0], "UnitFileState"), "watcher": value(props[1], "ActiveState")}


def _stop_watcher(ctx, job) -> None:
    # uninstall.sh does not stop the watcher's service, whose 90 s loop could shim again right after it.
    r = userexec.run(ctx, ["systemctl", "--user", "stop", WATCH_PATH, WATCH_SERVICE], timeout=120)
    _say(job, "Stopped the shim watcher" if r.rc == 0 else f"Stopping the shim watcher: {_tail(r, 2)} (ignored)")


def _start_watcher(ctx, job) -> None:
    r = userexec.run(ctx, ["systemctl", "--user", "start", WATCH_PATH], timeout=60)
    if r.rc != 0:
        _say(job, f"Starting the shim watcher again: {_tail(r, 2)}")


# ---- package scripts (detached) --------------------------------------------------------------------------------------

def _record_dir(ctx) -> int | None:
    """sysexec.run_dir (root's, 0700) as a folder descriptor, without creating it; None when missing or not ours."""
    try:
        fd = os.open(ctx.sys_path(sysexec.RUN_DIR), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if os.fstat(fd).st_uid == os.geteuid():
            return fd
    except OSError:
        pass
    os.close(fd)
    return None


def _record_in(dfd: int) -> dict | None:
    raw = context.read_small(SCRIPT_RECORD, 4096, dir_fd=dfd)
    try:
        rec = json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        rec = None
    return rec if isinstance(rec, dict) else None


def _read_record(ctx) -> dict | None:
    dfd = _record_dir(ctx)
    if dfd is None:
        return None
    try:
        return _record_in(dfd)
    finally:
        os.close(dfd)


def _clear_record(ctx, expect: dict) -> None:
    """Remove the record while it is still `expect`, the one read or written: another may have replaced it since
    (a plugin process started meanwhile). Call with _RECORD held."""
    dfd = _record_dir(ctx)
    if dfd is None:
        return
    try:
        if _record_in(dfd) == expect:
            os.unlink(SCRIPT_RECORD, dir_fd=dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def script_running(ctx) -> str | None:
    """The package script (its path and arguments) that an earlier plugin process started and that still runs: a
    plugin restart does not end it (_run_script), and a second install.sh or uninstall.sh beside it would mix up
    the session files and shims. None when there is none; the record of one that has ended is removed."""
    with _RECORD:   # RS-4: else a job could write its live record between our read and our removal of an old one
        rec = _read_record(ctx)
        if rec is None or rec.get("plugin") == os.getpid():    # ours: a job of this process runs it (or just did)
            return None
        if sysexec.proc_alive(ctx, rec.get("pid"), rec.get("starttime")):
            return str(rec.get("what") or "a package script")
        _clear_record(ctx, rec)
    return None


def _refuse_while_script(ctx, step: str, **extra) -> dict | None:
    what = script_running(ctx)
    if what is None:
        return None
    return _err(step, f"an xess-xmx-linux script from before the plugin restarted is still running ({what}); try "
                      "again when it has ended", **extra)


def _run_script(ctx, script: str, *args: str, cwd: str, on_line=None, timeout=None) -> sysexec.Result:
    """bash SCRIPT ARGS as the user, detached (userexec.run detach=True): its output goes through a root-only file,
    so a plugin stop or update mid-install does not kill it at its next echo (SIGPIPE) and it runs to its end, EXIT
    trap included. While it runs, run_dir/xess-script.json names it with runuser's pid and /proc start time, so that
    a plugin process started meanwhile leaves the package alone until it has ended (script_running)."""
    what = " ".join([script, *args])
    recorded = []

    def started(pid: int) -> None:
        start = sysexec.proc_starttime(ctx, pid)
        if start is None:       # already gone
            return
        data = json.dumps({"pid": pid, "starttime": start, "what": what, "plugin": os.getpid(),
                           "started": time.time()})
        try:   # through a folder descriptor, a new file renamed into place (context.write_atomic)
            with _RECORD:
                context.write_atomic(os.path.join(sysexec.run_dir(ctx), SCRIPT_RECORD), data.encode(), 0o600)
            recorded.append(json.loads(data))      # as _clear_record reads it back
        except OSError as e:
            log.warning("xess: cannot record the running %s: %s", what, e)

    try:
        return userexec.run(ctx, ["bash", script, *args], cwd=cwd, on_line=on_line, timeout=timeout, detach=True,
                            on_start=started)
    finally:
        if recorded:
            with _RECORD:
                _clear_record(ctx, recorded[0])


# ---- Protons ---------------------------------------------------------------------------------------------------------

def _system_dlls(ctx) -> list[str]:
    out = []
    for base in steamlib.SYSTEM_TOOL_DIRS:
        b = ctx.sys_path(base)
        try:
            names = sorted(os.listdir(b))
        except OSError:
            continue
        for n in names:
            f = os.path.join(b, n, IGDEXT_REL)
            if os.path.isfile(f):
                out.append(f)
    return out


def _system_protons(ctx) -> list[dict]:
    out = []
    for f in _system_dlls(ctx):
        name = f[:-len(IGDEXT_REL)].rstrip("/").rsplit("/", 1)[-1]
        out.append({"path": f, "name": name, "shimmed": bool(has_marker(f)), "stock": os.path.isfile(f + ".stock")})
    return out


def _user_proton_dirs(ctx) -> list[str]:
    dirs = []
    for lib in steamlib.libraries(ctx):
        common = os.path.join(lib, "steamapps", "common")
        try:
            dirs += [os.path.join(common, n) for n in sorted(os.listdir(common)) if n.startswith("Proton")]
        except OSError:
            pass
    root = steamlib.steam_root(ctx)
    if root:
        tools = os.path.join(root, "compatibilitytools.d")
        try:
            dirs += [os.path.join(tools, n) for n in sorted(os.listdir(tools))]
        except OSError:
            pass
    return dirs


def has_hook(proton_dir: str) -> bool:
    """install.sh's user_settings.py, which runs PKG/tools/proton-hook.py."""
    return bool(has_marker(os.path.join(proton_dir, "user_settings.py"), b"proton-hook.py"))


def _user_protons(ctx) -> list[dict]:
    out = []
    for p in _user_proton_dirs(ctx):
        f = os.path.join(p, IGDEXT_REL)
        if os.path.isfile(f):
            out.append({"name": os.path.basename(p), "path": p, "shimmed": bool(has_marker(f)), "hook": has_hook(p)})
    return out


# ---- host tools ------------------------------------------------------------------------------------------------------

def distro(ctx) -> str | None:
    """cachyos or bazzite, from /etc/os-release; None for anything else."""
    text = _read_text(ctx.sys_path("/etc/os-release"), 65536) or ""
    ids = []
    for line in text.splitlines():
        k, _, v = line.partition("=")
        if k in ("ID", "ID_LIKE"):
            ids += v.strip().strip("\"'").lower().split()
    if "bazzite" in ids:
        return "bazzite"
    if "cachyos" in ids or "arch" in ids:
        return "cachyos"
    return None


def missing_tools(ctx) -> list[str]:
    return [name if req else f"{name} (optional)" for name, req, _c, _b in TOOLS if sysexec.which(name) is None]


def _how_to_get(ctx, names: list[str]) -> str:
    plat = distro(ctx)
    parts = []
    for name, _req, cachy, bazz in TOOLS:
        if name not in names:
            continue
        if plat == "cachyos":
            parts.append(f"{name} (sudo pacman -S {cachy})")
        elif plat == "bazzite":
            parts.append(f"{name} (rpm-ostree install {bazz}, then reboot)")
        else:
            parts.append(f"{name} (CachyOS package {cachy}, Bazzite package {bazz})")
    return ", ".join(parts)


def _preflight(ctx, step: str, *, tools: bool) -> dict | None:
    game = steamlib.running_game(ctx)
    if game:
        return _err(step, f"a game is running (Steam app {game}); quit it first: the scripts copy DLLs into the "
                          "games' prefixes")
    if not tools:
        return None
    if steamlib.steam_root(ctx) is None and steamlib.flatpak_steam(ctx):
        return _err(step, "only Flatpak Steam is installed; xess-xmx-linux supports the native Steam client only "
                          "(its games cannot see the package inside the Flatpak sandbox)")
    missing = [name for name, req, _c, _b in TOOLS if req and sysexec.which(name) is None]
    if missing:
        return _err(step, "the scripts need tools this system lacks: " + _how_to_get(ctx, missing))
    return None


# ---- state -----------------------------------------------------------------------------------------------------------

def _old_folders(settings, pkg: str | None) -> list[str]:
    out = []
    for p in settings.get("xess_old_folders") or []:
        if isinstance(p, str) and p.startswith("/") and os.path.isdir(p):
            real = os.path.realpath(p)
            if real != pkg and real not in out:
                out.append(real)
    return out


def _left_to_user(ctx, real: str) -> str | None:
    """Why remove_folder never deletes this earlier package folder (a real path), whatever else changes, as the end
    of a sentence that starts with the path; None when it may (once nothing uses it). _state lists such a folder
    apart, with no Remove button (RC-2)."""
    home = os.path.realpath(ctx.home)
    if not real.startswith(home.rstrip("/") + "/"):
        return f"is outside {home}"
    if not looks_like_package(real):
        return "does not look like an xess-xmx-linux folder (no install.sh, uninstall.sh, tools/session-check.sh)"
    # XESS-6: a clone of the repository has those files too, with the user's own work in it
    if os.path.lexists(os.path.join(real, ".git")):
        return "is a git checkout, not just an unpacked release; delete it yourself if you no longer need it"
    name = os.path.basename(real)
    if not is_managed(ctx, real) and not (name.startswith(NAME_PREFIX) and _SAFE_TAG.match(name[len(NAME_PREFIX):])):
        return (f"is not named {NAME_PREFIX}<tag> like an unpacked release and was not installed by OXP3 Deck; delete "
                "it yourself if you no longer need it")
    return None


def _remember_old(settings, folder: str | None, *, exclude: str | None = None) -> list[str]:
    olds = [p for p in (settings.get("xess_old_folders") or []) if isinstance(p, str)]
    if folder and folder not in olds:
        olds.append(folder)
    if exclude:
        olds = [p for p in olds if os.path.realpath(p) != os.path.realpath(exclude)]
    return olds


def features(pkg: str | None) -> dict:
    # FSR=xess / FSR_FG arrived with 20261104 (tools/fsr-fg.sh, get-ffx.sh, the hook's FSR_FG).
    return {"fsr": bool(pkg) and os.path.isfile(os.path.join(pkg, "tools", "fsr-fg.sh")),
            "alt_driver_store": bool(pkg) and has_marker(os.path.join(pkg, "install.sh"),
                                                         b"INTC_ALT_DRIVER_EXTENSIONS_PATH") is True}


def _status_notes(ctx, det: dict, st: dict) -> None:
    pkg, env_pkg = det["path"], det["env_pkg"]
    if env_pkg and (not pkg or os.path.realpath(env_pkg) != pkg):
        st["notes"].append(f"{ENV_FILE} points at {env_pkg}, which is not a package folder (moved or deleted?): "
                           "after a re-login games would find no Intel Vulkan driver. Install again, or uninstall.")
    if pkg and st["lib"] and not _under(os.path.realpath(st["lib"]), pkg):
        st["notes"].append(f"The last driver check was for {st['lib']}, not this package.")


def _state(ctx, settings) -> dict:
    det = detect(ctx, settings)
    pkg = det["path"]
    st = read_status(ctx)
    _status_notes(ctx, det, st)
    if not det["installed"] and st["line"]:
        st["notes"].append("xess-xmx-linux is not installed: this line is from an earlier driver check.")
    session = _session_env(ctx) if pkg else None
    live = bool(pkg) and session is not None and _points_at(session.get("VK_DRIVER_FILES"), pkg)
    env_points = bool(pkg) and _points_at(det["env"].get("VK_DRIVER_FILES"), pkg)
    steam = steamlib.steam_environ(ctx)
    steam_points = steam is not None and _points_at(steam.get("VK_DRIVER_FILES"), pkg)
    relogin = (bool(pkg) and det["installed"] and (live or env_points) and steam is not None and not steam_points
               and st["state"] != "failed")
    sysp = _system_protons(ctx)
    shim_wanted = settings.get("xess_shim_system") is True
    version = package_version(pkg, settings)
    units = _units(ctx) if pkg else {"check": None, "watcher": None}
    missing = _missing_downloads(pkg) if det["installed"] else []
    # The last check's tag only when it is an update (FE-1, XESS-7: one installed by hand since can be newer)
    latest = settings.get("xess_latest") if isinstance(settings.get("xess_latest"), str) else None
    update = det["installed"] and _update_offered(latest, version)
    olds = _old_folders(settings, pkg)
    if "igc" in missing:      # XESS-1: an earlier folder's compiler is the only one there is (and a repair copies it)
        olds = [p for p in olds if not _igc_present(p)]
    kept = [{"path": p, "why": _left_to_user(ctx, p)} for p in olds]     # RC-2: no Remove button for these
    kept = [k for k in kept if k["why"]]
    olds = [p for p in olds if p not in [k["path"] for k in kept]]
    return {
        "installed": det["installed"],
        "package_dir": pkg,
        "found_by": det["how"],
        "version": version,
        "managed": is_managed(ctx, pkg),
        "package_dir_kept": _left_to_user(ctx, pkg) if pkg else None,
        "drivers": drivers(pkg),
        "preferred_driver": settings.get("xess_driver"),
        "status": st,
        "live": live,
        "steam_running": steam is not None,
        "relogin_needed": relogin,
        "units": units,
        "system_protons": sysp,
        "system_protons_need_shim": det["installed"] and shim_wanted and any(not p["shimmed"] for p in sysp),
        "user_protons": _user_protons(ctx),
        "missing_tools": missing_tools(ctx),
        "old_folders": olds,
        "old_folders_kept": kept,
        "flatpak_steam": steamlib.flatpak_steam(ctx),
        "latest": latest if update or not det["installed"] else None,
        "update_available": update,
        "checked_at": settings.get("xess_checked_at"),
        "missing_downloads": missing,
        # _install repairs only a release it can name (an unknown one gets the newest release, as check_update says)
        "repair": None if update or not version else _repair_reason(det, st, units, missing),
        "script_running": script_running(ctx),
        "features": features(pkg),
    }


def state(ctx, settings) -> dict:
    """{ok, error, xess: XessState}. Reads files and asks systemd twice; no network."""
    try:
        return {"ok": True, "error": None, "xess": _state(ctx, settings)}
    except Exception as e:     # a Plugin method never raises
        log.exception("xess state")
        return _err("xess state", f"{type(e).__name__}: {e}", xess=None)


def summary(ctx, settings) -> dict:
    """XessSummary for the overview: files and Steam's environment only (no systemctl). `live` is what Steam runs
    with when it runs, else what the environment file sets up."""
    try:
        det = detect(ctx, settings)
        pkg = det["path"]
        st = read_status(ctx)
        env_points = bool(pkg) and _points_at(det["env"].get("VK_DRIVER_FILES"), pkg)
        steam = steamlib.steam_environ(ctx) if pkg else None
        if steam is None:
            live, relogin = env_points and st["state"] == "ok", False
        else:
            live = _points_at(steam.get("VK_DRIVER_FILES"), pkg)
            relogin = det["installed"] and env_points and not live and st["state"] != "failed"
        return {"installed": det["installed"], "version": package_version(pkg, settings), "status": st["state"],
                "line": st["line"], "live": bool(live), "relogin_needed": bool(relogin)}
    except Exception as e:
        log.exception("xess summary")
        return {"installed": False, "version": None, "status": "unknown", "line": f"xess summary: {e}",
                "live": False, "relogin_needed": False}


def system_protons_need_shim(ctx, settings) -> bool:
    """A package is installed, the shim was put into the system Protons before (xess_shim_system), and one of them
    has its stock igdext64.dll again (a package update). Not while a package script from before a plugin restart
    still runs (the start-up re-shim waits for the next start)."""
    try:
        if settings.get("xess_shim_system") is not True or not detect(ctx, settings)["installed"]:
            return False
        if script_running(ctx) is not None:
            return False
        return any(not has_marker(f) for f in _system_dlls(ctx))
    except Exception:
        log.exception("xess system_protons_need_shim")
        return False


# ---- update check ----------------------------------------------------------------------------------------------------

def check_update(ctx, settings) -> dict:
    det = detect(ctx, settings)
    installed = package_version(det["path"], settings) if det["installed"] else None
    try:
        latest = downloads.latest_release_tag(REPO)
    except downloads.DownloadError as e:
        return _err("xess update check", str(e), installed=installed, latest=settings.get("xess_latest"),
                    update_available=False, checked_at=settings.get("xess_checked_at") or 0, note=None)
    except Exception as e:
        log.exception("xess update check")
        return _err("xess update check", f"{type(e).__name__}: {e}", installed=installed, latest=None,
                    update_available=False, checked_at=0, note=None)
    now = time.time()
    settings.update({"xess_latest": latest, "xess_checked_at": now})
    note = None
    if not det["installed"]:
        available = False
    elif installed is None:
        available = True
        note = (f"The installed package ({det['path']}) does not say which release it is (its folder is not named "
                f"{NAME_PREFIX}<tag>); updating installs {latest} next to it.")
    else:
        cmp = newer(latest, installed)
        if cmp is None:
            available = latest != installed
            note = f"Cannot compare the releases {installed} and {latest}."
        else:
            available = cmp
    return {"ok": True, "error": None, "installed": installed, "latest": latest, "update_available": available,
            "checked_at": now, "note": note}


# ---- install / update ------------------------------------------------------------------------------------------------

def _igc_present(pkg: str) -> bool:
    # install.sh: [ -x "$(ls "$PKG"/igc/neo/bin/ocloc* | head -1)" ]
    b = os.path.join(pkg, "igc", "neo", "bin")
    try:
        names = sorted(n for n in os.listdir(b) if n.startswith("ocloc"))
    except OSError:
        return False
    if not names:
        return False
    try:
        st = os.stat(os.path.join(b, names[0]))
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and bool(st.st_mode & 0o111)


# The big downloads install.sh fetches only when missing (install.sh:30, :35; 20261104 :46-59): folder, the script
# that fetches it, the presence test install.sh uses, and the script's lines that pin what it fetches. Pins: get-igc.sh
# IGC_TAG / NEO_TAG (v2.10.10 and 25.18.33578.6 in both 20261031 and 20261104, although 20261104 rewrote the script:
# checksums, no igc-opencl, which SPIR-V input never used); get-xess.sh the SDK's tag and checksum and the libraries
# it takes; get-ffx.sh the SDK tag and each file with its checksum.
PIECES = (
    ("igc", "tools/get-igc.sh", _igc_present, re.compile(rb"^(?:IGC_TAG|NEO_TAG)=.*$", re.M)),
    ("xess", "tools/get-xess.sh", lambda p: os.path.isfile(os.path.join(p, "xess", "libxess.dll")),
     re.compile(rb"^(?:XESS_TAG|XESS_SHA256|LIBS)=.*$", re.M)),
    ("ffx", "tools/get-ffx.sh", lambda p: all(os.path.isfile(os.path.join(p, "ffx", f)) for f in (
        "amd_fidelityfx_loader_dx12.dll", "amd_fidelityfx_framegeneration_dx12.dll")),
     re.compile(rb"^(?:FFX_TAG=.*|\S+ [0-9a-f]{64})$", re.M)),
)
PIECE_NAMES = {"igc": "the compiler (igc/)", "xess": "Intel's XeSS SDK (xess/)", "ffx": "AMD's FidelityFX SDK (ffx/)"}


def _missing_downloads(pkg: str | None) -> list[str]:
    """The pieces whose get script the package has but whose files are missing: install.sh only prints a line when a
    download fails (rate limit, offline) and still exits 0 (20261104 install.sh:46-59, :275)."""
    if not pkg:
        return []
    return [sub for sub, script, present, _pins in PIECES
            if os.path.isfile(os.path.join(pkg, script)) and not present(pkg)]


def _and(names: list[str]) -> str:
    return names[0] if len(names) < 2 else ", ".join(names[:-1]) + " and " + names[-1]


def _repair_reason(det: dict, st: dict, units: dict, missing: list[str]) -> str | None:
    """Why the installed package needs its install.sh again (same folder, nothing downloaded but what is missing),
    or None. install.sh writes the environment file first, then the shims and units, and the driver check's line
    last (20261104 :62-87, :224-273): a run cut short leaves a package that reads as installed with the earlier
    package's check line (XESS-2)."""
    pkg = det["path"]
    if not det["installed"] or not pkg:
        return None
    if missing:
        names = [PIECE_NAMES.get(m, f"{m}/") for m in missing]
        return f"{_and(names)} {'was' if len(names) == 1 else 'were'} not downloaded"
    if det["how"] != "env":
        return f"{ENV_FILE} does not name this package"
    if not st["lib"]:
        return "the driver check has not run for this package"
    if not _under(os.path.realpath(st["lib"]), pkg):
        return "the driver check belongs to another folder"
    if units.get("check") == "not-found":
        return "the login check unit is missing"
    if st["state"] == "failed":
        return "the driver check failed"
    return None


def _same_downloads(script: str, pins, a: bytes | None, b: bytes | None) -> str | None:
    """Why two releases' get scripts fetch the same files ("is unchanged", "pins the same downloads"), or None."""
    if a is None or b is None:
        return None
    if a == b:
        return f"{script} is unchanged"
    pa = [m.group(0).strip() for m in pins.finditer(a)]
    if pa and pa == [m.group(0).strip() for m in pins.finditer(b)]:
        return f"{script} pins the same downloads"
    return None


def _reuse_downloads(ctx, job, old: str, new: str) -> list[str]:
    """Copy igc/, xess/ and ffx/ from the old package when the new release fetches the same files (its get script is
    unchanged, or pins the same versions and checksums): install.sh then skips 95-230 MB, 77 MB and 87 MB of
    downloads, and a rate-limited GitHub API cannot leave the new package without a compiler."""
    copied = []
    for sub, script, present, pins in PIECES:
        if not present(old) or os.path.lexists(os.path.join(new, sub)) or not os.path.isfile(os.path.join(new, script)):
            continue
        same = _same_downloads(script, pins, context.read_small(os.path.join(old, script), 1 << 20),
                               context.read_small(os.path.join(new, script), 1 << 20))
        if same is None:
            _say(job, f"{sub}/: {script} differs in this release, so install.sh downloads it again")
            continue
        _say(job, f"Copying {sub}/ from {old} ({same})")
        r = userexec.run(ctx, ["cp", "-a", "--", os.path.join(old, sub), os.path.join(new, sub)], timeout=1800)
        if r.rc == 0:
            copied.append(sub)
        else:
            _say(job, f"Copying {sub}/ failed ({_tail(r, 2)}); install.sh downloads it instead")
            userexec.run(ctx, ["rm", "-rf", "--", os.path.join(new, sub)], timeout=300)
    return copied


def _in_use(ctx, det: dict, folder: str) -> bool:
    """The folder is the installed package, or the session or the running Steam has its driver."""
    real = os.path.realpath(folder)
    if det["installed"] and det["path"] == real:
        return True
    steam = steamlib.steam_environ(ctx)
    if steam is not None and _references(steam.get("VK_DRIVER_FILES"), real):
        return True
    session = _session_env(ctx)
    return session is not None and _references(session.get("VK_DRIVER_FILES"), real)


def _unpack(ctx, job, det: dict, tag: str, new: str) -> dict | None:
    """Download the release and put its top folder at `new`. None on success, else the error result."""
    step = "xess install"
    top = NAME_PREFIX + tag
    url = f"https://github.com/{REPO}/releases/download/{tag}/{top}.zip"
    xroot = _managed_root(ctx)
    incoming = os.path.join(xroot, ".incoming-" + tag)
    try:
        userexec.user_mkdirs(ctx, xroot)
    except OSError as e:
        return _err(step, f"cannot create {xroot}: {e.strerror or e}")
    tmp = tempfile.mkdtemp(prefix="oxp3deck-xess-", dir=TMP_PARENT)    # 0700, root's
    try:
        zpath = os.path.join(tmp, top + ".zip")
        _say(job, f"Downloading {url}")
        _say(job, "xess-xmx-linux publishes no checksum for its releases: the archive is only checked to be a plain "
                  "zip with one top folder")
        marks = [0]

        def progress(done: int, total: int | None) -> None:
            if done - marks[0] >= 4 << 20 or (total and done == total):
                marks[0] = done
                _say(job, f"  {done >> 20} MB" + (f" of {total >> 20} MB" if total else ""))

        try:
            digest = downloads.download(url, zpath, max_bytes=MAX_ZIP, on_progress=progress)
            names = downloads.safe_zip_members(zpath)
        except downloads.DownloadError as e:
            return _err(step, str(e))
        _say(job, f"Downloaded {top}.zip, sha256 {digest}")
        tops = {n.split("/", 1)[0] for n in names}
        if tops != {top}:
            return _err(step, f"the archive's top folder is {', '.join(sorted(tops))}, not {top}")
        os.chmod(tmp, 0o755)       # the user unpacks it; still only root can write here
        os.chmod(zpath, 0o644)
        if os.path.lexists(incoming):
            r = userexec.run(ctx, ["rm", "-rf", "--", incoming], timeout=300)
            if r.rc != 0:
                return _err(step, f"cannot remove the leftover {incoming}: {_tail(r, 2)}")
        try:
            userexec.user_mkdirs(ctx, incoming)
        except OSError as e:
            return _err(step, f"cannot create {incoming}: {e.strerror or e}")
        r = userexec.run(ctx, ["/usr/bin/python3", "-I", "-c", _EXTRACT, zpath, incoming, top, str(MAX_UNPACKED)],
                         timeout=600)
        if r.rc != 0:
            return _err(step, f"unpacking failed: {_tail(r, 3)}")
        _say(job, f"Unpacked as {ctx.user}: {_tail(r, 1)}")
        if os.path.lexists(new):
            if _in_use(ctx, det, new):
                return _err(step, f"{new} is still used by the running session; log out and back in, then try again")
            _say(job, f"Removing the earlier copy {new}")
            r = userexec.run(ctx, ["rm", "-rf", "--", new], timeout=300)
            if r.rc != 0:
                return _err(step, f"cannot remove {new}: {_tail(r, 2)}")
        r = userexec.run(ctx, ["mv", "-T", "--", os.path.join(incoming, top), new], timeout=120)
        if r.rc != 0:
            return _err(step, f"cannot move the release to {new}: {_tail(r, 2)}")
        _say(job, f"Release {tag} is in {new}")
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if os.path.lexists(incoming):
            userexec.run(ctx, ["rm", "-rf", "--", incoming], timeout=300)


def _apply_driver(ctx, settings, job, pkg: str) -> str | None:
    """Make the user's preferred Mesa driver the default again (lib/active lives in the package, so every release
    brings its own default). Returns a warning, or None."""
    pref = settings.get("xess_driver")
    if not isinstance(pref, str) or not pref:
        return None
    if not _DRIVER_PREFIX.match(pref):
        return f"The stored driver choice {pref!r} is not a version; the release's default stays."
    have = drivers(pkg)
    if not any(d["version"].startswith(pref) for d in have):
        default = next((d["version"] for d in have if d["default"]), None)
        return f"This release has no Mesa {pref} driver; its default ({default or 'unknown'}) stays."
    if any(d["default"] and d["version"].startswith(pref) for d in have):
        return None
    _say(job, f"Making Mesa {pref} the default again (tools/switch-driver.sh {pref})")
    r = _run_script(ctx, os.path.join(pkg, "tools", "switch-driver.sh"), pref, cwd=pkg,
                    on_line=job.say if job else None)
    if r.rc != 0:
        return f"Mesa {pref} could not be made the default again: {_tail(r, 2)}"
    return None


def _install_into(ctx, settings, job, *, tag: str, new: str, old: str | None, known: str | None) -> dict:
    """Steps 5-10: stop the watcher, the old package's uninstall.sh, the new one's install.sh (rolled back with the
    old install.sh on failure), the driver choice, the system Protons, the settings. `old` is None when install.sh
    runs again in the installed folder."""
    step = "xess install"
    in_place = old is None and known == new
    _stop_watcher(ctx, job)
    if old:
        _say(job, f"Running {old}/uninstall.sh")
        r = _run_script(ctx, os.path.join(old, "uninstall.sh"), cwd=old, on_line=job.say)
        if r.rc != 0:
            _start_watcher(ctx, job)
            settings.set("xess_old_folders", _remember_old(settings, new))
            return _err(step, f"uninstall.sh of {old} failed (exit {r.rc}): {_tail(r)}; nothing was changed, the "
                              f"new release stays in {new}", tag=tag, package_dir=old)
    _say(job, f"Running {new}/install.sh (it downloads the compiler and Intel's SDK when missing)")
    r = _run_script(ctx, os.path.join(new, "install.sh"), cwd=new, on_line=job.say)
    if r.rc != 0:
        rolled_back = None
        if old:
            _say(job, f"install.sh failed; putting {old} back with its install.sh")
            rb = _run_script(ctx, os.path.join(old, "install.sh"), cwd=old, on_line=job.say)
            rolled_back = rb.rc == 0
            if not rolled_back:
                _say(job, f"{old}/install.sh failed too (exit {rb.rc})")
        if not rolled_back:
            _start_watcher(ctx, job)     # install.sh restarts it only when it gets to the end
        if not in_place:
            settings.set("xess_old_folders", _remember_old(settings, new))
        return _err(step, f"install.sh failed (exit {r.rc}): {_tail(r)}", tag=tag, package_dir=old or known,
                    rolled_back=rolled_back)
    warnings, notes = [], []
    previous = old or (known if known != new else None)
    # XESS-1: install.sh exits 0 when a download fails; the old folder (still listed) is hidden while it holds the
    # only compiler (_state), and Repair copies it back or downloads again
    for sub in _missing_downloads(new):
        w = (f"{PIECE_NAMES.get(sub, sub + '/')} was not downloaded (see install.sh's output above); Repair on the "
             "XeSS tab runs install.sh again to fetch it.")
        if sub == "igc" and previous and _igc_present(previous):
            w += f" {previous} keeps its compiler until then."
        warnings.append(w)
        _say(job, w)
    w = _apply_driver(ctx, settings, job, new)
    if w:
        warnings.append(w)
        _say(job, w)
    if _system_dlls(ctx) and settings.get("xess_shim_system") is not False:
        res = _shim_system(ctx, settings, job, new)
        notes += res.get("notes", [])
        if not res["ok"]:
            warnings.append(res["error"])
    settings.update({"xess_package": new, "xess_tag": tag,
                     "xess_old_folders": _remember_old(settings, previous, exclude=new),
                     "xess_installed_at": time.time()})
    st = read_status(ctx)
    reboot = False
    nxt = NEXT_INSTALL
    if st["state"] == "failed":
        st, w, nxt = _check_again(ctx, job, new, st)
        reboot = nxt == NEXT_REBOOT
        if w:
            warnings.append(w)
            _say(job, w)
    steam = steamlib.steam_environ(ctx)
    relogin = steam is None or not _points_at(steam.get("VK_DRIVER_FILES"), new) or not in_place
    if not relogin and nxt == NEXT_INSTALL:
        nxt = None
    _say(job, f"Done. {nxt}" if nxt else "Done.")
    return {"ok": True, "error": None, "tag": tag, "package_dir": new, "previous": previous,
            "relogin_needed": relogin, "reboot_needed": reboot, "next": nxt, "warnings": warnings,
            "notes": notes, "status": st}


def _check_again(ctx, job, pkg: str, st: dict) -> tuple[dict, str | None, str]:
    """install.sh's driver check (session-check.sh --dry-run) failed; the dry run only reports. The check unit
    (Type=oneshot, RemainAfterExit=yes, both releases) has been active since boot and install.sh only enables it, so
    a log-out would not run it again while the user manager lives: restart it now. session-check.sh then switches to
    the package's other driver, or points the user manager at the stock drivers (its fail()). Returns the status,
    a warning and the next step: NEXT_REBOOT when the fallback is not in place."""
    reason = st["reason"]
    _say(job, f"The new driver failed its check ({reason}); running the session check now (systemctl --user "
              f"restart {CHECK_UNIT})")
    r = userexec.run(ctx, ["systemctl", "--user", "restart", CHECK_UNIT], timeout=120)
    st = read_status(ctx)
    if r.rc != 0:
        return st, (f"The new driver failed its check ({reason}), and the session check could not run again "
                    f"({_tail(r, 2)})."), NEXT_REBOOT
    if st["state"] == "ok":
        if st["switched_to"]:
            return st, (f"The new default driver failed its check ({reason}); the session check made the package's "
                        f"Mesa {st['switched_to']} driver the default instead."), NEXT_INSTALL
        _say(job, "The session check passes now")
        return st, None, NEXT_INSTALL
    session = _session_env(ctx)
    if session is not None and not _points_at(session.get("VK_DRIVER_FILES"), pkg):
        return st, (f"The package's drivers failed their check ({st['reason'] or reason}): the session check set the "
                    "system's stock Intel driver for the next session (XeSS runs on DP4a). A newer release of the "
                    "package, or tools/rebuild-driver.sh, can fix it (the package's docs/debugging.md)."), NEXT_STOCK
    return st, (f"The new driver failed its check ({st['reason'] or reason}), and the session check did not "
                "switch this session to another driver."), NEXT_REBOOT


def install(ctx, settings, job) -> dict:
    """Install xess-xmx-linux, or update it to the newest release (a job)."""
    if not _BUSY.acquire(blocking=False):
        return _err("xess install", "another XeSS task is running")
    try:
        return _refuse_while_script(ctx, "xess install") or _install(ctx, settings, job)
    finally:
        _BUSY.release()


def _install(ctx, settings, job) -> dict:
    step = "xess install"
    pre = _preflight(ctx, step, tools=True)
    if pre:
        return pre
    _say(job, "Looking up the newest xess-xmx-linux release")
    try:
        tag = downloads.latest_release_tag(REPO)
    except downloads.DownloadError as e:
        # Offline or GitHub's rate limit (the usual reason a download is missing): a repair needs no lookup
        det = detect(ctx, settings)
        pkg = det["path"]
        version = package_version(pkg, settings)
        if det["installed"] and version:
            missing = _missing_downloads(pkg)
            reason = _repair_reason(det, read_status(ctx), _units(ctx), missing)
            if reason:
                _say(job, f"Could not look up the newest release ({e}); repairing the installed {version} anyway")
                return _repair_in_place(ctx, settings, job, pkg, version, reason, missing)
        return _err(step, str(e))
    settings.update({"xess_latest": tag, "xess_checked_at": time.time()})
    if not _SAFE_TAG.match(tag):
        return _err(step, f"unexpected release tag {tag!r}")
    det = detect(ctx, settings)
    pkg = det["path"]
    version = package_version(pkg, settings)
    old = pkg if det["installed"] else None
    if old:
        _say(job, f"Newest release: {tag}; installed: {version or 'a release of unknown date'} in {old}")
    else:
        _say(job, f"Newest release: {tag}; nothing installed" + (f" ({pkg} is known but not set up)" if pkg else ""))
    if old and (version == tag or newer(version, tag) is True):
        # Nothing newer: up to date, or install.sh again in the folder (XESS-1, XESS-2), never a downgrade (FE-1)
        missing = _missing_downloads(pkg)
        reason = _repair_reason(det, read_status(ctx), _units(ctx), missing)
        if reason is None and version == tag:
            _say(job, "Already up to date")
            return {"ok": True, "error": None, "up_to_date": True, "tag": tag, "package_dir": pkg,
                    "relogin_needed": False, "next": None}
        if reason is None:
            return _err(step, f"the installed {version} is newer than the newest release {tag}; nothing was changed",
                        tag=tag, package_dir=pkg)
        return _repair_in_place(ctx, settings, job, pkg, version, reason, missing)
    if pkg and version == tag:
        st = read_status(ctx)
        _say(job, f"{tag} is already unpacked in {pkg} (driver check: {st['state']}); running its install.sh again")
        return _install_into(ctx, settings, job, tag=tag, new=pkg, old=None, known=pkg)
    new = os.path.join(_managed_root(ctx), NAME_PREFIX + tag)
    failed = _unpack(ctx, job, det, tag, new)
    if failed:
        return failed
    new = os.path.realpath(new)
    if old:
        _reuse_downloads(ctx, job, old, new)
    return _install_into(ctx, settings, job, tag=tag, new=new, old=old, known=pkg)


def _repair_in_place(ctx, settings, job, pkg: str, version: str, reason: str, missing: list[str]) -> dict:
    """install.sh again in the installed package's own folder; a missing piece comes from an old folder first."""
    _say(job, f"{version} in {pkg}: {reason}; running its install.sh again (nothing else is downloaded)")
    for folder in _old_folders(settings, pkg) if missing else []:   # an earlier folder's, with the same pins
        _reuse_downloads(ctx, job, folder, pkg)
    return _install_into(ctx, settings, job, tag=version, new=pkg, old=None, known=pkg)


# ---- uninstall -------------------------------------------------------------------------------------------------------

def uninstall(ctx, settings, job) -> dict:
    """uninstall.sh of the installed package (a job). The folder stays; it is listed among the old folders."""
    step = "xess uninstall"
    if not _BUSY.acquire(blocking=False):
        return _err(step, "another XeSS task is running")
    try:
        pre = _refuse_while_script(ctx, step) or _preflight(ctx, step, tools=False)
        if pre:
            return pre
        det = detect(ctx, settings)
        pkg = det["path"]
        if not pkg:
            return _err(step, "xess-xmx-linux is not installed")
        _stop_watcher(ctx, job)
        _say(job, f"Running {pkg}/uninstall.sh")
        r = _run_script(ctx, os.path.join(pkg, "uninstall.sh"), cwd=pkg, on_line=job.say)
        if r.rc != 0:
            _start_watcher(ctx, job)
            return _err(step, f"uninstall.sh failed (exit {r.rc}): {_tail(r)}", package_dir=pkg)
        res = _unshim_system(ctx, job)
        warnings = [res["error"]] if not res["ok"] else []
        settings.update({"xess_package": None, "xess_old_folders": _remember_old(settings, pkg),
                         "xess_uninstalled_at": time.time()})
        _say(job, "Done. " + NEXT_UNINSTALL)
        return {"ok": True, "error": None, "package_dir": pkg, "restored": res["done"], "warnings": warnings,
                "relogin_needed": True, "next": NEXT_UNINSTALL}
    finally:
        _BUSY.release()


# ---- default driver --------------------------------------------------------------------------------------------------

def set_driver(ctx, settings, prefix: str) -> dict:
    """Make the package's Mesa <prefix> driver the default (tools/switch-driver.sh PREFIX, checked by the script
    first; takes effect at the next game start) and remember the choice for updates."""
    step = "xess set driver"
    if not isinstance(prefix, str) or not _DRIVER_PREFIX.match(prefix):
        return _err(step, f"not a driver version: {prefix!r}", drivers=[])
    if not _BUSY.acquire(blocking=False):
        return _err(step, "another XeSS task is running", drivers=[])
    try:
        busy = _refuse_while_script(ctx, step, drivers=[])
        if busy:
            return busy
        det = detect(ctx, settings)
        pkg = det["path"]
        if not det["installed"]:
            return _err(step, "xess-xmx-linux is not installed", drivers=[])
        have = drivers(pkg)
        if not any(d["version"].startswith(prefix) for d in have):
            names = ", ".join(d["version"] for d in have) or "none"
            return _err(step, f"no Mesa {prefix} driver in {pkg} (it has: {names})", drivers=have)
        r = _run_script(ctx, os.path.join(pkg, "tools", "switch-driver.sh"), prefix, cwd=pkg, timeout=180)
        output = [s for s in (r.out + "\n" + r.err).splitlines() if s.strip()]
        have = drivers(pkg)
        if r.rc != 0:
            return _err(step, f"switch-driver.sh {prefix} failed: {_tail(r, 2)}", drivers=have, output=output)
        settings.set("xess_driver", prefix)
        return {"ok": True, "error": None, "drivers": have, "output": output}
    except Exception as e:
        log.exception(step)
        return _err(step, f"{type(e).__name__}: {e}", drivers=[])
    finally:
        _BUSY.release()


# ---- system Protons --------------------------------------------------------------------------------------------------

def _unsafe_dir(ctx, folder: str) -> str | None:
    """Why root must not write in `folder`: every folder from the system tools folder down must be a real folder
    owned by the backend's user (root) that nobody else can write. A user-writable one is install.sh's job."""
    me = os.geteuid()
    for base in steamlib.SYSTEM_TOOL_DIRS:
        b = ctx.sys_path(base).rstrip("/")
        if not _under(folder, b):
            continue
        cur = b
        rel = folder[len(b):].strip("/")
        for part in [""] + (rel.split("/") if rel else []):
            cur = os.path.join(cur, part) if part else cur
            try:
                st = os.lstat(cur)
            except OSError as e:
                return f"{cur}: {e.strerror}"
            if not stat.S_ISDIR(st.st_mode):
                return f"{cur} is not a plain folder"
            if st.st_uid != me or st.st_mode & 0o022:
                return f"{cur} is writable by others than root; install.sh handles such Protons as the user"
        return None
    return "not in a system tools folder"


def _restorecon(paths: list[str]) -> None:
    rc = sysexec.which("restorecon")      # SELinux (Fedora / Bazzite): the new files get the folder's label
    if rc and paths:
        sysexec.run([rc, *paths], timeout=30)


def _shim_system(ctx, settings, job, pkg: str) -> dict:
    """As install.sh's install_shim() does for files the user can write (install.sh:83-102): a stock file is kept
    as F.stock (replaced when a package update brought a different stock file), then F becomes PKG/igdext64.dll.
    user_settings.py is left alone: proton-cachyos-native ships its own as a package file."""
    step = "xess shim system Protons"
    shim = context.read_small(os.path.join(pkg, "igdext64.dll"), DLL_MAX)
    if shim is None or MARKER not in shim:
        return _err(step, f"{pkg}/igdext64.dll is missing or is not the shim", done=[], failed=[], already=[],
                    notes=[])
    done, failed, already = [], [], []
    for f in _system_dlls(ctx):
        data = context.read_small(f, DLL_MAX)
        if data is None:
            failed.append(f"{f}: cannot be read")
            continue
        if data == shim:
            already.append(f)
            continue
        why = _unsafe_dir(ctx, os.path.dirname(f))
        if why:
            failed.append(f"{f}: {why}")
            continue
        try:
            st = os.lstat(f)
            touched = [f]
            if MARKER not in data:
                stock = f + ".stock"
                if context.read_small(stock, DLL_MAX) != data:
                    context.write_atomic(stock, data, stat.S_IMODE(st.st_mode))
                    os.utime(stock, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)   # cp -p
                    touched.append(stock)
            context.write_atomic(f, shim, 0o644)
            _restorecon(touched)
            done.append(f)
            _say(job, f"shim -> {f}")
        except OSError as e:
            failed.append(f"{f}: {e.strerror or e}")
    if done or already:
        settings.set("xess_shim_system", True)
    notes = [HOOK_NOTE, UPDATE_NOTE] if (done or already) else []
    return {"ok": not failed, "error": (f"{step}: " + "; ".join(failed)) if failed else None, "done": done,
            "failed": failed, "already": already, "notes": notes}


def shim_system_protons(ctx, settings, job=None) -> dict:
    """Root: the installed package's shim into every Proton under /usr/share and /usr/local/share
    steam/compatibilitytools.d (install.sh only prints sudo commands for them)."""
    step = "xess shim system Protons"
    if not _BUSY.acquire(blocking=False):
        return _err(step, "another XeSS task is running", done=[], failed=[], already=[], notes=[])
    try:
        busy = _refuse_while_script(ctx, step, done=[], failed=[], already=[], notes=[])
        if busy:
            return busy
        det = detect(ctx, settings)
        if not det["installed"]:
            return _err(step, "xess-xmx-linux is not installed", done=[], failed=[], already=[], notes=[])
        return _shim_system(ctx, settings, job, det["path"])
    except Exception as e:
        log.exception(step)
        return _err(step, f"{type(e).__name__}: {e}", done=[], failed=[], already=[], notes=[])
    finally:
        _BUSY.release()


def _unshim_system(ctx, job) -> dict:
    step = "xess restore system Protons"
    done, failed = [], []
    for f in _system_dlls(ctx):
        stock = f + ".stock"
        try:
            st = os.lstat(stock)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode) or not has_marker(f):
            continue
        why = _unsafe_dir(ctx, os.path.dirname(f))
        if why:
            failed.append(f"{f}: {why}")
            continue
        try:
            os.replace(stock, f)        # uninstall.sh's restore_shim: mv -f F.stock F
            _restorecon([f])
            done.append(f)
            _say(job, f"stock igdext64.dll back in {f}")
        except OSError as e:
            failed.append(f"{f}: {e.strerror or e}")
    return {"ok": not failed, "error": (f"{step}: " + "; ".join(failed)) if failed else None, "done": done,
            "failed": failed}


def unshim_system_protons(ctx, job=None) -> dict:
    """Root: put F.stock back where a system Proton's igdext64.dll is the shim (uninstall.sh cannot: not writable)."""
    try:
        return _unshim_system(ctx, job)
    except Exception as e:
        log.exception("xess restore system Protons")
        return _err("xess restore system Protons", f"{type(e).__name__}: {e}", done=[], failed=[])


# ---- old folders -----------------------------------------------------------------------------------------------------

def remove_folder(ctx, settings, path: str) -> dict:
    """Delete an earlier package folder, as the user, once nothing uses it any more."""
    step = "xess remove folder"
    if not isinstance(path, str) or not path.startswith("/"):
        return _err(step, f"not a folder: {path!r}")
    if not _BUSY.acquire(blocking=False):
        return _err(step, "another XeSS task is running")
    try:
        busy = _refuse_while_script(ctx, step)
        if busy:
            return busy
        real = os.path.realpath(path)
        listed = [p for p in (settings.get("xess_old_folders") or []) if isinstance(p, str)]
        if real not in [os.path.realpath(p) for p in listed]:
            return _err(step, f"{path} is not one of the earlier package folders")
        why = _left_to_user(ctx, real)     # outside the home, no package, a git checkout, not a release's name
        if why:
            return _err(step, f"{real} {why}")
        det = detect(ctx, settings)
        for p in [det["path"], det["env_pkg"], _package_from_unit(ctx)]:
            if p and (os.path.realpath(p) == real or _under(os.path.realpath(p), real)
                      or _under(real, os.path.realpath(p))):
                return _err(step, f"{real} is the package in use")
        if det["installed"] and "igc" in _missing_downloads(det["path"]) and _igc_present(real):
            return _err(step, f"{real} has the only compiler (igc/): {det['path']} has none yet; use Repair first")
        steam = steamlib.steam_environ(ctx)
        if steam is not None and _references(steam.get("VK_DRIVER_FILES"), real):
            return _err(step, f"Steam still runs with the driver in {real}; log out and back in first")
        session = _session_env(ctx)
        if session is not None and _references(session.get("VK_DRIVER_FILES"), real):
            return _err(step, f"the session still uses the driver in {real}; log out and back in first")
        r = userexec.run(ctx, ["rm", "-rf", "--", real], timeout=600)
        if r.rc != 0:
            return _err(step, f"rm failed: {_tail(r, 2)}")
        settings.set("xess_old_folders", [p for p in listed if os.path.realpath(p) != real])
        return {"ok": True, "error": None, "removed": real}
    except Exception as e:
        log.exception(step)
        return _err(step, f"{type(e).__name__}: {e}")
    finally:
        _BUSY.release()
