"""Act on the running Steam client and its gamescope session from a root helper.

Shared by the Steam actions helper (steam_actions.py) and the Xbox key helper
(xbox_helper.py); oxp3-tool bundles this file into each installed helper
script. What writes to the user's files or sockets (~/.steam/steam.pipe,
gamescope's socket in /run/user) runs as the Steam user, never as root. The
helper itself only reads gamescope's limiter state (4 bytes of a regular file
the Steam user owns, not through a link at the file itself) and talks to Steam's remote debugging over
TCP on 127.0.0.1:8080, only when the Steam user owns that listener, treating
every answer as untrusted data:
- steam:// URLs go to the running client the way Valve's
  steam-runtime-steam-remote does: one shell-quoted command line written to
  ~/.steam/steam.pipe. steam://open/keyboard opens the on-screen keyboard;
  steam://open/library takes Game Mode to its Home screen (the Steam menu's
  Home item).
- The frame limit is Steam's own global setting, changed in Steam's UI over its
  remote debugging (which Decky Loader turns on; cdp.py, steam_ui_js.py), so
  Quick Access shows it. When that is not possible (debugging off, a game with
  its own performance profile, Steam's UI code changed) and nothing was
  written, it falls back to gamescope's `gamescopectl debug_set_fps_limit`,
  the value Steam's limiter also sets: Steam is not told then, so Quick Access
  keeps showing its own setting and Steam replaces the limit when it next
  sends its own. Either way it caps games only, never Steam's own screens.

Each action logs one line starting with "ok: " or "failed: "; oxp3-tool status
looks at the newest.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import stat
import struct
import subprocess
import sys
import time
from typing import Optional, Union

from .cdp import CDP_TIMEOUT, CdpError, evaluate
from .steam_ui_js import STEAM_FPS_TOGGLE_JS

PYTHON = "/usr/bin/python3"
GAMESCOPECTL = "/usr/bin/gamescopectl"
# Written by the buttons fix: {"xbox": <action>, "session": <Game Mode input>, "fps_limit": <fps>}.
KEYS_CONFIG = "/etc/oxp3-tool/keys.json"
KEYS_CONFIG_MAX = 4096
ENVIRON_MAX = 1 << 20
DEFAULT_FPS_LIMIT = 72  # half of 144 Hz: the panel keeps its mode with VRR off
PANEL_MAX_FPS = 144  # for Steam's frame-limit script when gamescope's own list is not available
STEAM_JS_BUDGET_MS = 1800  # the script's own time limit, well inside the DevTools deadline
# A script Steam only starts this long after the press (its UI was busy) writes nothing: by then the
# helper may have given up on the answer, and a change nobody reports should not land later.
STEAM_JS_NOT_AFTER = CDP_TIMEOUT - 1.0
# Steam's remote debugging port in /proc/net/tcp: 127.0.0.1:8080 (or any address), state LISTEN.
DEBUG_PORT_ADDRESSES = ("0100007F:1F90", "00000000:1F90")
MIN_FPS_LIMIT = 10
UNCHANGED = "unchanged"  # toggle_in_steam: failed, and nothing was written anywhere
MAX_FPS_LIMIT = 144

URLS = {"keyboard": "steam://open/keyboard", "home": "steam://open/library"}
WHAT = {"keyboard": "open the keyboard", "home": "open the Home screen"}
# InputPlumber D-Bus action a capability map sends for each helper action.
DBUS_ACTIONS = {"keyboard": "ui_osk", "home": "oxp3_home", "fps-limit": "oxp3_fps_limit"}
# journalctl --grep pattern for the outcome lines (plain text apart from the anchor).
OUTCOME_GREP = "^(ok|failed): "
NOT_READING = 3

# Runs as the Steam user. Exit codes: 0 sent, 3 nobody reads the pipe, 1 other errors.
_WRITE = r"""
import errno, os, stat, sys
try:
    fd = os.open(sys.argv[1], os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOCTTY)
except OSError as e:
    sys.exit(3 if e.errno in (errno.ENOENT, errno.ENXIO) else 1)
try:
    if not stat.S_ISFIFO(os.fstat(fd).st_mode):
        sys.exit(1)
    os.write(fd, sys.argv[2].encode())
except OSError:
    sys.exit(1)
finally:
    os.close(fd)
"""

_DISPLAY_RE = re.compile(r"gamescope-[0-9]+")
_LIMITER_RE = re.compile(r"/[A-Za-z0-9._/-]*/gamescope-limiter[.-][A-Za-z0-9]+")


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def log(message: str) -> None:
    # One journal line per call: text from Steam or /proc cannot start a line of its own
    # (status reads the newest "ok: "/"failed: " line) or reach a terminal as escape codes.
    print(_CONTROL.sub(" ", message), file=sys.stderr, flush=True)


def pipe_line(url: str) -> str:
    # Valve's writer puts a fixed program path first; Steam ignores it.
    return f"'~/.steam/root/ubuntu12_32/steam' '{url}'\n"


def steam_processes(proc: str = "/proc") -> list[tuple[int, int]]:
    """(pid, uid) of processes named steam (the client or its launcher), root excluded."""
    found: list[tuple[int, int]] = []
    try:
        entries = os.listdir(proc)
    except OSError:
        return found
    for entry in sorted((e for e in entries if e.isdigit()), key=int):
        try:
            # Bytes: the kernel cuts names at 15 bytes, also inside a UTF-8 character.
            with open(os.path.join(proc, entry, "comm"), "rb") as f:
                if f.read().strip() != b"steam":
                    continue
            uid = os.stat(os.path.join(proc, entry)).st_uid
        except OSError:
            continue
        if uid != 0:
            found.append((int(entry), uid))
    return found


def steam_uids(proc: str = "/proc") -> list[int]:
    uids: list[int] = []
    for _, uid in steam_processes(proc):
        if uid not in uids:
            uids.append(uid)
    return uids


def read_environ(proc: str, pid: int) -> dict[str, str]:
    try:
        with open(os.path.join(proc, str(pid), "environ"), "rb") as f:
            data = f.read(ENVIRON_MAX)
    except OSError:
        return {}
    env = {}
    for item in data.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep:
            env[key.decode(errors="replace")] = value.decode(errors="replace")
    return env


def game_mode_steam(proc: str = "/proc") -> Optional[tuple[int, int, dict[str, str]]]:
    """(pid, uid, environment) of a Steam client running in a gamescope Game Mode session."""
    for pid, uid in steam_processes(proc):
        env = read_environ(proc, pid)
        if _DISPLAY_RE.fullmatch(env.get("GAMESCOPE_WAYLAND_DISPLAY", "")):
            return pid, uid, env
    return None


def run_as(uid: int, argv: list[str], env: Optional[dict[str, str]] = None,
           timeout: float = 5) -> Optional[subprocess.CompletedProcess]:
    """Run ``argv`` as ``uid`` (its group, no supplementary groups, a minimal environment)."""
    try:
        pw = pwd.getpwuid(uid)
    except KeyError:
        log(f"no user with uid {uid}")
        return None
    full_env = {"HOME": pw.pw_dir, "PATH": "/usr/bin"}
    full_env.update(env or {})
    try:
        return subprocess.run(argv, user=uid, group=pw.pw_gid, extra_groups=[], env=full_env, cwd="/",
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"cannot run {argv[0]} as uid {uid}: {type(e).__name__}")
        return None


def send_as(uid: int, url: str) -> int:
    """Write ``url`` to the user's ~/.steam/steam.pipe as that user; _WRITE's exit code."""
    try:
        home = pwd.getpwuid(uid).pw_dir
    except KeyError:
        return 1
    res = run_as(uid, [PYTHON, "-I", "-c", _WRITE, os.path.join(home, ".steam", "steam.pipe"), pipe_line(url)])
    return 1 if res is None else res.returncode


def open_url(action: str, proc: str = "/proc") -> bool:
    uids = steam_uids(proc)
    if not uids:
        log(f"failed: Steam is not running; cannot {WHAT[action]}")
        return False
    codes = []
    for uid in uids:
        code = send_as(uid, URLS[action])
        if code == 0:
            log(f"ok: asked Steam to {WHAT[action]} (uid {uid})")
            return True
        codes.append(code)
    why = "Steam is not reading ~/.steam/steam.pipe" if all(c == NOT_READING for c in codes) \
        else "writing ~/.steam/steam.pipe failed"
    log(f"failed: could not hand the request to Steam: {why}")
    return False


def limiter_active(proc: str, pid: int, uid: int, env: dict[str, str]) -> Optional[bool]:
    """Whether gamescope's frame limiter is on, from the state file it keeps (None: unknown).

    Read through the Steam process's root, so the helper's private /tmp does not matter.
    """
    path = env.get("GAMESCOPE_LIMITER_FILE", "")
    if not _LIMITER_RE.fullmatch(path) or "/../" in path:
        return None
    try:
        fd = os.open(f"{proc}/{pid}/root{path}", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != uid:
            return None
        data = os.pread(fd, 4, 0)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) != 4:
        return None
    return struct.unpack("=I", data)[0] != 0


def set_fps_limit(uid: int, env: dict[str, str], fps: int) -> Optional[str]:
    """Set gamescope's frame limit (0: off); an error message, or None."""
    res = run_as(uid, [GAMESCOPECTL, "debug_set_fps_limit", str(fps)],
                 {"XDG_RUNTIME_DIR": f"/run/user/{uid}",
                  "GAMESCOPE_WAYLAND_DISPLAY": env["GAMESCOPE_WAYLAND_DISPLAY"]}, timeout=3)
    if res is None:
        return "gamescopectl did not run"
    err = res.stderr.decode(errors="replace")
    if res.returncode != 0:
        return f"gamescopectl exited with {res.returncode}: {err.strip()[-200:]}"
    # gamescopectl exits 0 even when gamescope rejects the command.
    for bad in ("Command not found.", "Failed to parse FPS."):
        if bad in err:
            return f"gamescope: {bad}"
    return None


def describe_steam(proc: str = "/proc") -> str:
    """The Steam processes found, for a log line: why no Game Mode session was found."""
    found = steam_processes(proc)
    if not found:
        return "no process named steam"
    parts = []
    for pid, uid in found[:4]:
        try:
            exe = os.readlink(os.path.join(proc, str(pid), "exe"))
        except OSError:
            exe = "?"
        game_mode = _DISPLAY_RE.fullmatch(read_environ(proc, pid).get("GAMESCOPE_WAYLAND_DISPLAY", ""))
        parts.append(f"pid {pid} uid {uid} {exe}{'' if game_mode else ' (no gamescope display)'}")
    return "; ".join(parts)


def debug_port_uids(proc: str = "/proc") -> set[int]:
    """Owners of a socket listening on Steam's remote debugging port."""
    try:
        with open(os.path.join(proc, "net", "tcp")) as f:
            lines = f.read(1 << 22).splitlines()[1:]
    except OSError:
        return set()
    uids = set()
    for line in lines:
        fields = line.split()
        if len(fields) > 7 and fields[1] in DEBUG_PORT_ADDRESSES and fields[3] == "0A":
            try:
                uids.add(int(fields[7]))
            except ValueError:
                pass
    return uids


def toggle_in_steam(fps: int, uid: int, proc: str = "/proc") -> Union[None, bool, str]:
    """Toggle Steam's global frame limit in its UI.

    None: nothing was written and Steam's setting cannot be used (use gamescope). UNCHANGED: a
    failure that changed nothing. True or False: Steam's setting changed, or may have.
    """
    # Only talk to Steam itself: the port must belong to the Steam user, not to some other process.
    if uid not in debug_port_uids(proc):
        log("Steam's remote debugging is off (Decky Loader turns it on); setting the limit in gamescope, "
            "which Quick Access does not show")
        return None
    opts = {"action": "toggle", "limitFps": int(fps), "maxFps": PANEL_MAX_FPS, "budgetMs": STEAM_JS_BUDGET_MS,
            "notAfter": int((time.time() + STEAM_JS_NOT_AFTER) * 1000)}
    try:
        value = evaluate(f"({STEAM_FPS_TOGGLE_JS})({json.dumps(opts)}).then(JSON.stringify)",
                         await_promise=True, eval_timeout_ms=1500)
    except CdpError as e:
        if e.maybe_ran:
            # Steam may have changed the setting: changing gamescope too could undo it.
            log(f"failed: Steam did not confirm the frame limit change ({e}); it may still apply, "
                "check Quick Access before pressing again")
            return False
        log(f"Steam's frame limit setting is not reachable ({e}); setting the limit in gamescope")
        return None
    try:
        reply = json.loads(value) if isinstance(value, str) else None
    except (ValueError, RecursionError):
        reply = None
    if not isinstance(reply, dict) or reply.get("v") != 1 or not isinstance(reply.get("ok"), bool) \
            or not isinstance(reply.get("wrote"), bool):
        log("failed: Steam's answer about the frame limit is not understood")
        return False
    error = str(reply.get("error"))[:120]
    if not reply["ok"]:
        if reply["wrote"]:
            log(f"failed: Steam's frame limit change failed ({error})")
            return False
        if error == "too late":
            # Steam is in charge of the limit and only slow: gamescope's limit would hide from Quick Access.
            log("failed: Steam was too busy to change the frame limit; nothing was changed, press again")
            return UNCHANGED
        if error == "per-game profile active":
            log("this game has its own performance profile in Steam, which the global frame limit does not "
                "change; setting the limit in gamescope, which Quick Access does not show")
        else:
            log(f"Steam's frame limit setting cannot be used ({error}); setting the limit in gamescope")
        return None
    target = reply.get("target")
    fps_target = target if isinstance(target, int) and not isinstance(target, bool) else None
    if reply.get("confirmed") is True:
        if reply.get("limitedAfter") is True and fps_target is not None:
            line = f"ok: frame limit {fps_target} fps in Steam (global, shown in Quick Access)"
        else:
            line = "ok: frame limit off in Steam (global)"
    else:
        # Until Steam echoes the change, the state the script read back is the old one: report the request.
        on = reply.get("on")
        if not isinstance(on, bool):
            on = reply.get("limitedBefore") is False  # this helper always toggles
        if on and fps_target is not None:
            line = f"ok: asked Steam for a frame limit of {fps_target} fps (global)"
        else:
            line = "ok: asked Steam to turn its frame limit off (global)"
        line += "; Steam has not confirmed it yet"
    warnings = reply.get("warnings")
    if isinstance(warnings, list) and warnings:
        line += "; " + "; ".join(str(w)[:80] for w in warnings[:3])
    log(line)
    return True


class FrameLimit:
    def __init__(self) -> None:
        self.sent = 0  # the limit this helper set last in this Game Mode session (0: none or off)
        self.session: Optional[tuple[int, str]] = None

    def toggle(self, fps: int, proc: str = "/proc") -> bool:
        """Set ``fps``, or turn whatever limit is on off when this helper's last press set one.

        gamescope's state file only says whether some limit is on. Steam can keep one that
        changes nothing (its target at the panel's full rate), so a limit that was on before this
        helper set one is replaced rather than turned off: the first press always shows a limit.
        If Steam changed the limit since the last press, that press may only turn it off.
        """
        found = game_mode_steam(proc)
        if found is None:
            log("failed: Steam is not running in Game Mode; no frame limit to change "
                f"(Steam processes: {describe_steam(proc)})")
            return False
        pid, uid, env = found
        session = (pid, env.get("GAMESCOPE_LIMITER_FILE", ""))
        if session != self.session:
            self.sent, self.session = 0, session  # a new Game Mode session starts without our limit
        in_steam = toggle_in_steam(fps, uid, proc)
        if in_steam == UNCHANGED:
            return False  # nothing changed anywhere: a gamescope limit this helper set is still on
        if in_steam is not None:
            self.sent = 0  # Steam owns the limit now; a later gamescope press starts afresh
            return in_steam
        active = limiter_active(proc, pid, uid, env)
        target = 0 if self.sent and active is not False else fps
        error = set_fps_limit(uid, env, target)
        if error:
            log(f"failed: could not change the frame limit: {error}")
            return False
        self.sent = target
        now = limiter_active(proc, pid, uid, env)
        if now is not None and now != (target != 0):
            log(f"failed: gamescope did not {'set' if target else 'clear'} the frame limit")
            return False
        if target:
            log(f"ok: frame limit {target} fps" + (" (replaced a limit that was already on)" if active else ""))
        else:
            log("ok: frame limit off")
        return True


def key_config(path: str = KEYS_CONFIG) -> dict:
    """The buttons fix's key settings; empty when missing or unreadable."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return {}
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return {}
        data = os.read(fd, KEYS_CONFIG_MAX + 1)
    except OSError:
        return {}
    finally:
        os.close(fd)
    try:
        config = json.loads(data) if len(data) <= KEYS_CONFIG_MAX else None
    except ValueError:
        return {}
    return config if isinstance(config, dict) else {}


def fps_limit(config: dict) -> int:
    value = config.get("fps_limit")
    if isinstance(value, int) and not isinstance(value, bool) and MIN_FPS_LIMIT <= value <= MAX_FPS_LIMIT:
        return value
    return DEFAULT_FPS_LIMIT


class Actions:
    """The actions that need the Steam user's session."""

    def __init__(self, proc: str = "/proc", config_path: str = KEYS_CONFIG) -> None:
        self.proc = proc
        self.config_path = config_path
        self.frame_limit = FrameLimit()

    def perform(self, action: str) -> bool:
        try:
            if action in URLS:
                return open_url(action, self.proc)
            if action == "fps-limit":
                return self.frame_limit.toggle(fps_limit(key_config(self.config_path)), self.proc)
            log(f"failed: unknown action {action!r}")
        except Exception as e:  # one bad press must not stop the service
            log(f"failed: {action}: {type(e).__name__}: {e}")
        return False
