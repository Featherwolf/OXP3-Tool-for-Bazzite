# Adapted from ClawDeck (MIT, Copyright (c) 2026 IHulkSmashI and ClawDeck contributors).
"""The environment of the programs the plugin starts.

Decky Loader's Python is a PyInstaller bundle. Its bootloader points LD_LIBRARY_PATH at the bundle (its own OpenSSL
and libpython) and keeps the previous value in LD_LIBRARY_PATH_ORIG. Every child inherits that and loads the wrong
libraries: on Bazzite rpm-ostree died with "undefined symbol: EVP_MD_CTX_dup" before doing anything.
"""
from __future__ import annotations

import os

SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Set by the bootloader; the *_ORIG copy holds the value from before it.
LOADER_VARS = ("LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONPATH", "PYTHONHOME")
LOADER_PREFIXES = ("_MEIPASS", "_PYI_")


def _is_loader_var(name: str) -> bool:
    if name.startswith(LOADER_PREFIXES):
        return True
    return any(name in (v, v + "_ORIG") for v in LOADER_VARS)


def scrub_process_env() -> None:
    """Undo the bootloader's changes in this process (main.py calls it at import): put each *_ORIG value back, or
    drop the variable when there was none, and drop PyInstaller's own variables. The *_ORIG copies stay, so a
    second call changes nothing; clean_env() leaves them out."""
    for name in LOADER_VARS:
        orig = os.environ.get(name + "_ORIG")
        if orig:
            os.environ[name] = orig
        else:
            os.environ.pop(name, None)
    for name in list(os.environ):
        if name.startswith(LOADER_PREFIXES):
            del os.environ[name]


def clean_env(extra: dict | None = None) -> dict:
    """A copy of the environment for a child: without the loader variables (even restored ones: system tools run
    as root need none of them), PATH set when missing, plus `extra`."""
    env = {k: v for k, v in os.environ.items() if not _is_loader_var(k)}
    if not env.get("PATH"):
        env["PATH"] = SYSTEM_PATH
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env
