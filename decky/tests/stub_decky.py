"""A stand-in for the `decky` module Decky Loader injects into the plugin process.

install(tmpdir) lays out a fake home with Decky's folders under tmpdir and puts the stub into sys.modules (as
`decky` and `decky_plugin`, like Decky does). emit() records its calls in `emitted`. make_ctx(tmpdir) builds a
Ctx for the same layout without touching sys.modules.
"""
from __future__ import annotations

import logging
import os
import sys
import types

PLUGIN = "OXP3Deck"


def layout(tmpdir: str, user: str = "jordan") -> dict:
    """The paths of the fake system: home, Decky's folders, and `root` for Ctx.sys_path."""
    home = os.path.join(tmpdir, "home", user)
    brew = os.path.join(home, "homebrew")
    paths = {
        "root": os.path.join(tmpdir, "root"),
        "home": home,
        "decky_home": brew,
        "settings_dir": os.path.join(brew, "settings", PLUGIN),
        "runtime_dir": os.path.join(brew, "data", PLUGIN),
        "log_dir": os.path.join(brew, "logs", PLUGIN),
        "plugin_dir": os.path.join(brew, "plugins", PLUGIN),
    }
    for p in paths.values():
        os.makedirs(p, exist_ok=True)
    return paths


def install(tmpdir: str, user: str = "jordan") -> types.ModuleType:
    p = layout(tmpdir, user)
    mod = types.ModuleType("decky")
    mod.HOME = "/root"
    mod.USER = "root"
    mod.DECKY_VERSION = "v3.3.1"
    mod.DECKY_USER = user
    mod.DECKY_USER_HOME = p["home"]
    mod.DECKY_HOME = p["decky_home"]
    mod.DECKY_PLUGIN_SETTINGS_DIR = p["settings_dir"]
    mod.DECKY_PLUGIN_RUNTIME_DIR = p["runtime_dir"]
    mod.DECKY_PLUGIN_LOG_DIR = p["log_dir"]
    mod.DECKY_PLUGIN_DIR = p["plugin_dir"]
    mod.DECKY_PLUGIN_NAME = PLUGIN
    mod.DECKY_PLUGIN_VERSION = "0.1.0"
    mod.DECKY_PLUGIN_AUTHOR = "Featherwolf"
    mod.DECKY_PLUGIN_LOG = os.path.join(p["log_dir"], "plugin.log")
    mod.logger = logging.getLogger("decky")
    mod.emitted = []

    async def emit(event: str, *args) -> None:
        mod.emitted.append((event, args))

    mod.emit = emit
    for name in ("migrate_any", "migrate_settings", "migrate_runtime", "migrate_logs"):
        setattr(mod, name, lambda *a, **k: {})
    sys.modules["decky"] = mod
    sys.modules["decky_plugin"] = mod
    return mod


def uninstall() -> None:
    sys.modules.pop("decky", None)
    sys.modules.pop("decky_plugin", None)


def make_ctx(tmpdir: str, **overrides):
    """A Ctx for layout(tmpdir): user jordan, uid and gid 1000 (not root, so userexec accepts it)."""
    from oxp3deck.context import Ctx
    p = layout(tmpdir, overrides.get("user", "jordan"))
    fields = {"user": "jordan", "uid": 1000, "gid": 1000, "home": p["home"], "settings_dir": p["settings_dir"],
              "runtime_dir": p["runtime_dir"], "log_dir": p["log_dir"], "plugin_dir": p["plugin_dir"],
              "root": p["root"]}
    fields.update(overrides)
    return Ctx(**fields)
