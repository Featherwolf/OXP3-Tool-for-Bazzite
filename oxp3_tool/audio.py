"""PipeWire as the logged-in users see it: who runs a session, and their graph from `pw-dump`.

PipeWire runs per user, so root reads each user's graph by running `pw-dump` as that user (runuser). Everything
here only reads; the speaker-profile fix in fixes.py decides what to do with it.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from . import templates
from .system import System

RUN_USER = "/run/user"
PW_DUMP_TIMEOUT = 10
# Users' own PipeWire drop-ins replace a system file with the same name (PipeWire reads ~/.config last).
USER_CONFD = ".config/pipewire/pipewire.conf.d"
SYSTEM_CONFDS = ("/etc/pipewire/pipewire.conf.d", "/usr/share/pipewire/pipewire.conf.d")
HOME_ROOTS = ("/home", "/var/home")
CONF_MAX = 1024 * 1024
_USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}\$?$")

# PipeWire 1.4 and newer keep the builtin filters in this plugin; WirePlumber 0.5 brought the smart-filter policy.
LIB_DIRS = ("/usr/lib64", "/usr/lib")  # Fedora (Bazzite), Arch (CachyOS)
FILTER_CHAIN_MODULES = tuple(f"{d}/pipewire-0.3/libpipewire-module-filter-chain.so" for d in LIB_DIRS)
BUILTIN_FILTERS = tuple(f"{d}/spa-0.2/filter-graph/libspa-filter-graph-plugin-builtin.so" for d in LIB_DIRS)
SMART_FILTER_SCRIPTS = ("/usr/share/wireplumber/scripts/linking/find-filter-target.lua",)


@dataclass
class Session:
    user: str
    uid: int


@dataclass
class PwGraph:
    """The parts of a `pw-dump` the speaker-profile fix reads."""
    nodes: dict[int, dict[str, Any]] = field(default_factory=dict)  # id -> info.props
    volumes: dict[int, list[float]] = field(default_factory=dict)  # id -> Props channelVolumes (linear)
    links: list[tuple[int, int]] = field(default_factory=list)  # (output node id, input node id)
    defaults: dict[str, str] = field(default_factory=dict)  # "default" metadata key -> node name

    def node_id(self, name: str) -> Optional[int]:
        return next((i for i, p in self.nodes.items() if p.get("node.name") == name), None)

    def speaker_sinks(self) -> list[str]:
        """Names of the ALSA speaker sinks (the UCM HiFi profile's Speaker device)."""
        return sorted(p["node.name"] for p in self.nodes.values()
                      if p.get("media.class") == "Audio/Sink" and isinstance(p.get("node.name"), str)
                      and templates.SPEAKER_NODE_RE.fullmatch(p["node.name"]))

    def linked(self, out_name: str, in_name: str) -> bool:
        a, b = self.node_id(out_name), self.node_id(in_name)
        return a is not None and b is not None and (a, b) in self.links

    def volume(self, name: str) -> Optional[float]:
        """The loudest channel's volume of node ``name`` (1.0 is 100%), or None."""
        i = self.node_id(name)
        vols = self.volumes.get(i) if i is not None else None
        return max(vols) if vols else None


def parse_pw_dump(text: str) -> Optional[PwGraph]:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    graph = PwGraph()
    for obj in data:
        if not isinstance(obj, dict) or not isinstance(obj.get("id"), int):
            continue
        kind = obj.get("type", "")
        info = obj.get("info") if isinstance(obj.get("info"), dict) else {}
        if kind == "PipeWire:Interface:Node":
            props = info.get("props") if isinstance(info.get("props"), dict) else {}
            graph.nodes[obj["id"]] = props
            params = info.get("params") if isinstance(info.get("params"), dict) else {}
            for p in params.get("Props") or []:
                vols = p.get("channelVolumes") if isinstance(p, dict) else None
                if isinstance(vols, list) and vols and all(isinstance(v, (int, float)) for v in vols):
                    graph.volumes[obj["id"]] = [float(v) for v in vols]
                    break
        elif kind == "PipeWire:Interface:Link":
            out, inp = info.get("output-node-id"), info.get("input-node-id")
            if isinstance(out, int) and isinstance(inp, int):
                graph.links.append((out, inp))
        elif kind == "PipeWire:Interface:Metadata":
            props = obj.get("props") if isinstance(obj.get("props"), dict) else {}
            if props.get("metadata.name") != "default":
                continue
            for item in obj.get("metadata") or []:
                if not isinstance(item, dict) or not isinstance(item.get("key"), str):
                    continue
                value = item.get("value")
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        pass
                if isinstance(value, dict) and isinstance(value.get("name"), str):
                    graph.defaults[item["key"]] = value["name"]
    return graph


def _passwd(system: System) -> dict[int, str]:
    out: dict[int, str] = {}
    for line in (system.read_text("/etc/passwd") or "").splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[2].isdigit():
            out.setdefault(int(parts[2]), parts[0])
    return out


def sessions(system: System) -> list[Session]:
    """Users with a running session (logind's /run/user/UID), regular accounts only."""
    names = _passwd(system)
    out = []
    for entry in system.listdir(RUN_USER):
        if not entry.isdigit() or not system.is_dir(f"{RUN_USER}/{entry}"):
            continue
        uid = int(entry)
        if uid < 1000 or uid >= 60000:
            continue
        name = names.get(uid)
        if name is None and system.is_real_root:
            try:
                import pwd
                name = pwd.getpwuid(uid).pw_name
            except (KeyError, ImportError):
                name = None
        if name and _USER_RE.match(name):
            out.append(Session(name, uid))
    return sorted(out, key=lambda s: s.uid)


def pw_dump_argv(system: System, s: Session) -> Optional[list[str]]:
    """How to run pw-dump against ``s``'s PipeWire, or None when this process cannot."""
    if not system.which("pw-dump"):
        return None
    env = ["env", f"XDG_RUNTIME_DIR={RUN_USER}/{s.uid}", "pw-dump"]
    if system.is_real_root and os.geteuid() != 0:
        return env if os.geteuid() == s.uid else None
    return ["runuser", "-u", s.user, "--", *env]


def read_graph(system: System, s: Session) -> Optional[PwGraph]:
    argv = pw_dump_argv(system, s)
    if argv is None:
        return None
    res = system.run(argv, timeout=PW_DUMP_TIMEOUT)
    return parse_pw_dump(res.stdout) if res.ok else None


def percent(volume: float) -> int:
    """A PipeWire channel volume as the percentage KDE, Steam and wpctl show (they use a cubic scale)."""
    return round(math.copysign(abs(volume) ** (1 / 3), volume) * 100)


def homes(system: System) -> list[tuple[str, str]]:
    """(user name, home directory) for every directory under /home and /var/home, without repeats."""
    seen: set[str] = set()
    out = []
    for root in HOME_ROOTS:
        for name in system.listdir(root):
            home = f"{root}/{name}"
            real = system.realpath(home) or home
            if real in seen or not system.is_dir(home):
                continue
            seen.add(real)
            out.append((name, home))
    return out


def read_conf(system: System, path: str, user_file: bool) -> Optional[str]:
    """A PipeWire config file; in a home directory only a regular file of bounded size, never through a symlink."""
    data = system.read_regular(path, CONF_MAX) if user_file else system.read_bytes(path)
    return None if data is None else data.decode("utf-8", errors="replace")
