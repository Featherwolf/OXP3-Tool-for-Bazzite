"""Steam's files as the desktop user's Steam client keeps them: libraries, app folders, prefixes, the compatibility
tool chosen per game, and the client's processes.

Everything here only reads. The files live in the user's home (or on an SD card the user owns), so they are
untrusted input: read through context.read_small (no symlinks, no FIFOs, size-capped) and parsed leniently.
Library discovery follows xess-xmx-linux's tools/steam-libraries.sh, so the plugin sees the same libraries its
scripts work on.
"""
from __future__ import annotations

import os
import re

from . import context

STEAM_DIRS = (".local/share/Steam", ".steam/steam")
FLATPAK_STEAM = ".var/app/com.valvesoftware.Steam"
SYSTEM_TOOL_DIRS = ("/usr/share/steam/compatibilitytools.d", "/usr/local/share/steam/compatibilitytools.d")
VDF_MAX = 16 << 20
_APPID = re.compile(r"^[0-9]+$")


# ---- KeyValues text ------------------------------------------------------------------------------------------------

def _tokens(text: str):
    """("str", value) / ("{", None) / ("}", None). Quoted strings know \\" \\\\ \\n \\t; // starts a comment; a
    bracketed conditional such as [$WIN32] is dropped."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n﻿":
            i += 1
        elif c == "/" and text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif c in "{}":
            yield c, None
            i += 1
        elif c == '"':
            out = []
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    out.append({"n": "\n", "t": "\t", "\\": "\\", '"': '"'}.get(nxt, "\\" + nxt))
                    i += 2
                else:
                    out.append(text[i])
                    i += 1
            i += 1   # the closing quote (or the end of an unterminated string)
            yield "str", "".join(out)
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n{}"':
                j += 1
            word = text[i:j]
            i = j
            if not (word.startswith("[") and word.endswith("]")):
                yield "str", word


def vdf_parse(text: str) -> dict:
    """Valve's text KeyValues as nested dicts. Lenient: a broken file gives what could be read, never an error.
    A key given twice keeps the last value; two blocks of the same name are merged."""
    root: dict = {}
    stack = [root]
    key = None
    for kind, tok in _tokens(text or ""):
        if kind == "{":
            if key is None:
                key = ""
            cur = stack[-1]
            block = cur.get(key)
            if not isinstance(block, dict):
                block = {}
                cur[key] = block
            stack.append(block)
            key = None
        elif kind == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        elif key is None:
            key = tok
        else:
            stack[-1][key] = tok
            key = None
    return root


def vdf_get(d, *keys, default=None):
    """d[k1][k2]..., each key matched exactly first, then without regard to case (Steam writes "Valve" and
    "valve")."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        if k in cur:
            cur = cur[k]
            continue
        low = str(k).lower()
        for kk, v in cur.items():
            if kk.lower() == low:
                cur = v
                break
        else:
            return default
    return cur


def read_vdf(path: str) -> dict | None:
    raw = context.read_small(path, VDF_MAX)
    if raw is None:
        return None
    return vdf_parse(raw.decode("utf-8", errors="replace"))


# ---- folders -------------------------------------------------------------------------------------------------------

def steam_root(ctx) -> str | None:
    """The native Steam client's folder (the one holding steamapps/), resolved."""
    for rel in STEAM_DIRS:
        p = os.path.join(ctx.home, rel)
        if os.path.isdir(p):
            return os.path.realpath(p)
    return None


def flatpak_steam(ctx) -> bool:
    return os.path.isdir(os.path.join(ctx.home, FLATPAK_STEAM))


def _library_paths(vdf: dict) -> list[str]:
    folders = vdf_get(vdf, "libraryfolders")
    if not isinstance(folders, dict):
        return []
    out = []
    for k, v in folders.items():
        if isinstance(v, dict):
            p = vdf_get(v, "path")
            if isinstance(p, str):
                out.append(p)
        elif isinstance(v, str) and k.isdigit():
            out.append(v)    # the old format: "1" "/path"
    return out


def libraries(ctx) -> list[str]:
    """Every library root (the folder that holds steamapps/), the main one first. Like steam-libraries.sh, the list
    comes from steamapps/libraryfolders.vdf, or config/libraryfolders.vdf when that one is missing."""
    root = steam_root(ctx)
    if root is None:
        return []
    out = [root]
    vdf = None
    for rel in ("steamapps/libraryfolders.vdf", "config/libraryfolders.vdf"):
        vdf = read_vdf(os.path.join(root, rel))
        if vdf is not None:
            break
    for p in _library_paths(vdf or {}):
        p = p.replace("\\\\", "/")   # steam-libraries.sh's unescape of Windows-style separators
        if not p.startswith("/"):
            continue
        real = os.path.realpath(p)
        if real not in out and os.path.isdir(os.path.join(real, "steamapps")):
            out.append(real)
    return out


def app_install_dir(ctx, appid: str) -> str | None:
    """LIB/steamapps/common/<installdir> from the library whose appmanifest_<appid>.acf names it."""
    appid = str(appid)
    if not _APPID.match(appid):
        return None
    for lib in libraries(ctx):
        acf = read_vdf(os.path.join(lib, "steamapps", f"appmanifest_{appid}.acf"))
        if acf is None:
            continue
        name = vdf_get(acf, "AppState", "installdir")
        if not isinstance(name, str) or not name or "/" in name or name in (".", ".."):
            continue
        path = os.path.join(lib, "steamapps", "common", name)
        if os.path.isdir(path):
            return path
    return None


def prefix_dir(ctx, appid: str) -> str | None:
    """LIB/steamapps/compatdata/<appid>/pfx in the first library that has it (steam_prefix_of)."""
    appid = str(appid)
    if not _APPID.match(appid):
        return None
    for lib in libraries(ctx):
        p = os.path.join(lib, "steamapps", "compatdata", appid, "pfx")
        if os.path.isdir(p):
            return p
    return None


def _compat_mapping(ctx) -> dict:
    root = steam_root(ctx)
    if root is None:
        return {}
    cfg = read_vdf(os.path.join(root, "config", "config.vdf")) or {}
    m = vdf_get(cfg, "InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping")
    return m if isinstance(m, dict) else {}


def _mapped_name(mapping: dict, key: str) -> str | None:
    entry = mapping.get(key)
    name = vdf_get(entry, "name") if isinstance(entry, dict) else None
    return name if isinstance(name, str) and name.strip() else None


def compat_tool(ctx, appid: str) -> str | None:
    """The compatibility tool chosen for this game in Steam (config.vdf CompatToolMapping), or None: then Steam's
    default applies (default_compat_tool)."""
    appid = str(appid)
    if not _APPID.match(appid) or appid == "0":
        return None
    return _mapped_name(_compat_mapping(ctx), appid)


def default_compat_tool(ctx) -> str | None:
    """Steam's choice for every other title (CompatToolMapping "0"), or None when it is not recorded."""
    return _mapped_name(_compat_mapping(ctx), "0")


def _tool_in(base: str, name: str) -> str | None:
    """A tool under a compatibilitytools.d folder: the one whose compatibilitytool.vdf declares `name`, else the
    folder called `name`."""
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return None
    declared = []    # (internal name, tool folder)
    for d in entries:
        folder = os.path.join(base, d)
        tools = vdf_get(read_vdf(os.path.join(folder, "compatibilitytool.vdf")) or {}, "compatibilitytools",
                        "compat_tools")
        if not isinstance(tools, dict):
            continue
        for internal, info in tools.items():
            rel = vdf_get(info, "install_path") if isinstance(info, dict) else None
            path = os.path.normpath(os.path.join(folder, rel if isinstance(rel, str) and rel else "."))
            if not path.startswith(base.rstrip("/") + "/"):
                path = folder     # an install_path that leaves the tools folder is not followed
            declared.append((internal, path))
    for match in (lambda s: s == name, lambda s: s.lower() == name.lower()):
        for internal, path in declared:
            if match(internal) and os.path.isdir(path):
                return path
    if name in entries and os.path.isdir(os.path.join(base, name)):
        return os.path.join(base, name)
    return None


def _valve_names(name: str) -> list[str]:
    """Folder names in steamapps/common for one of Valve's tool names; [] when there is no clear mapping."""
    if name == "proton_experimental":
        return ["Proton - Experimental"]
    if name == "proton_hotfix":
        return ["Proton Hotfix"]
    m = re.match(r"^proton_([0-9]+)$", name)
    if not m:
        return []
    digits = m.group(1)
    if int(digits) >= 7:          # proton_7 ... proton_10: Proton 7.0 ... 10.0
        return [f"Proton {int(digits)}.0", f"Proton {int(digits)}.0 (Beta)"]
    if len(digits) >= 2:          # proton_63 = Proton 6.3, proton_513 = 5.13
        return [f"Proton {digits[0]}.{digits[1:]}"]
    return [f"Proton {digits}.0"]


def find_tool_dir(ctx, name: str | None) -> str | None:
    """The folder of a compatibility tool by the name Steam records, or None when unsure."""
    if not name or "/" in name or name in (".", ".."):
        return None
    root = steam_root(ctx)
    bases = ([os.path.join(root, "compatibilitytools.d")] if root else []) + [ctx.sys_path(p) for p in SYSTEM_TOOL_DIRS]
    for base in bases:
        hit = _tool_in(base, name)
        if hit:
            return hit
    wanted = _valve_names(name)
    hits = []
    for lib in libraries(ctx):
        for w in wanted:
            p = os.path.join(lib, "steamapps", "common", w)
            if os.path.isdir(p) and p not in hits:
                hits.append(p)
    return hits[0] if len(hits) == 1 else None


def is_system_tool(ctx, path: str | None) -> bool:
    """A tool installed by a distribution package (/usr/share/steam/compatibilitytools.d, ...)."""
    if not path:
        return False
    for base in SYSTEM_TOOL_DIRS:
        b = ctx.sys_path(base).rstrip("/") + "/"
        if path.startswith(b):
            return True
    return False


# ---- processes -----------------------------------------------------------------------------------------------------

def _user_pids(ctx):
    proc = ctx.sys_path("/proc")
    try:
        names = os.listdir(proc)
    except OSError:
        return
    for n in names:
        if not n.isdigit():
            continue
        d = os.path.join(proc, n)
        try:
            if os.stat(d).st_uid != ctx.uid:
                continue
        except OSError:
            continue
        yield int(n), d


def _argv(pdir: str) -> list[str]:
    raw = context.read_small(os.path.join(pdir, "cmdline"), 1 << 20) or b""
    return [a.decode("utf-8", errors="replace") for a in raw.split(b"\0") if a]


def _comm(pdir: str) -> str:
    raw = context.read_small(os.path.join(pdir, "comm"), 4096) or b""
    return raw.decode("utf-8", errors="replace").strip()


def _start_time(pdir: str) -> int:
    raw = context.read_small(os.path.join(pdir, "stat"), 65536) or b""
    rest = raw.rsplit(b")", 1)[-1].split()
    try:
        return int(rest[19])    # field 22, counting the pid and (comm) as fields 1 and 2
    except (IndexError, ValueError):
        return 0


def running_game(ctx) -> str | None:
    """The app id of the game Steam runs now (its reaper: `reaper SteamLaunch AppId=<n> -- ...`), else None."""
    found = []
    for pid, d in _user_pids(ctx):
        argv = _argv(d)
        if not argv or os.path.basename(argv[0]) != "reaper" or "SteamLaunch" not in argv:
            continue
        for a in argv[1:]:
            if a == "--":
                break
            m = re.match(r"^AppId=([0-9]+)$", a)
            if m and m.group(1) != "0":
                found.append((_start_time(d), pid, m.group(1)))
                break
    return max(found)[2] if found else None


def _steam_rank(d: str) -> int | None:
    """0: the client binary (exe is ubuntu12_32/steam), 1: argv[0] names it, 2: steam.sh; None: not Steam."""
    if _comm(d) != "steam":
        return None
    try:
        exe = os.readlink(os.path.join(d, "exe"))
    except OSError:
        exe = ""
    if exe.endswith("/ubuntu12_32/steam"):
        return 0
    argv = _argv(d)
    if argv and argv[0].endswith("ubuntu12_32/steam"):
        return 1
    if any(a.endswith("/steam.sh") or a == "steam.sh" for a in argv[:3]):
        return 2
    return None


def steam_environ(ctx) -> dict | None:
    """The environment of the user's running Steam client (what games started now inherit), or None."""
    best = None
    for pid, d in _user_pids(ctx):
        rank = _steam_rank(d)
        if rank is None:
            continue
        key = (rank, _start_time(d), pid)
        if best is None or key < best[0]:
            best = (key, d)
    if best is None:
        return None
    raw = context.read_small(os.path.join(best[1], "environ"), 4 << 20)
    if raw is None:
        return None
    env = {}
    for item in raw.split(b"\0"):
        k, sep, v = item.partition(b"=")
        if sep and k:
            env[k.decode("utf-8", errors="replace")] = v.decode("utf-8", errors="replace")
    return env
