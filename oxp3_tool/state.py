"""Manifest of every change the tool made, so revert undoes exactly that."""

from __future__ import annotations

import datetime
import json
from typing import Any, Optional

from . import __version__
from .system import System

STATE_DIR = "/var/lib/oxp3-tool"
STATE_FILE = f"{STATE_DIR}/state.json"
BACKUP_DIR = f"{STATE_DIR}/backups"
SCHEMA = 1


def backup_path_for(fix_id: str, path: str) -> str:
    return f"{BACKUP_DIR}/{fix_id}{path}"


class StateError(Exception):
    pass


class State:
    """``fixes`` maps a fix id to its entry::

        {
          "applied_at": "..." or null,     # null until the fix was fully applied
          "tool_version": "...",
          "files": [{"path": ..., "sha256": ..., "backup": path-or-null}],
          "dirs_created": [...],           # directories created for those files
          "kargs": [...],                  # kernel args this tool appended
          "kargs_pending_add": [...],      # being appended, outcome not yet known
          "kargs_removal_pending": [...],  # removed from the next boot, still active
          "units_enabled": [...],          # units this tool enabled
          "reverted": true,                # set while a revert waits for a reboot
          "options": {...}, "notes": {...}
        }
    """

    def __init__(self, system: System, tolerant: bool = False):
        """With ``tolerant``, an unreadable state file is reported in
        ``load_error`` instead of raising, and the state cannot be saved."""
        self.sys = system
        self.fixes: dict[str, dict[str, Any]] = {}
        self.load_error: Optional[str] = None
        try:
            self._load()
        except StateError as e:
            if not tolerant:
                raise
            self.load_error = str(e)
            self.fixes = {}

    def _load(self) -> None:
        text = self.sys.read_text(STATE_FILE)
        if text is None:
            return
        try:
            data = json.loads(text)
        except ValueError as e:
            raise StateError(
                f"{STATE_FILE} is corrupt ({e}). Move it aside to continue; "
                "changes it recorded will then have to be reverted by hand."
            ) from e
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            raise StateError(f"{STATE_FILE} has an unknown format; refusing to touch it.")
        fixes = data.get("fixes", {})
        if not isinstance(fixes, dict):
            raise StateError(f"{STATE_FILE} has an unknown format; refusing to touch it.")
        self.fixes = fixes
        for fix_id, entry in list(fixes.items()):
            if not isinstance(entry, dict):
                raise StateError(f"{STATE_FILE} has an unknown format; refusing to touch it.")
            self.entry(fix_id)

    def get(self, fix_id: str) -> Optional[dict[str, Any]]:
        return self.fixes.get(fix_id)

    def entry(self, fix_id: str) -> dict[str, Any]:
        """Return the entry for ``fix_id``, creating an empty one if needed."""
        e = self.fixes.setdefault(fix_id, {"applied_at": None, "tool_version": __version__})
        for key in ("files", "dirs_created", "kargs", "kargs_pending_add", "kargs_removal_pending", "units_enabled"):
            e.setdefault(key, [])
        for key in ("options", "notes"):
            e.setdefault(key, {})
        return e

    def stamp(self, fix_id: str) -> None:
        e = self.entry(fix_id)
        e["applied_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        e["tool_version"] = __version__

    def forget(self, fix_id: str) -> None:
        self.fixes.pop(fix_id, None)

    def save(self) -> None:
        if self.load_error:
            raise StateError(self.load_error)
        if not self.fixes:
            self.sys.remove(STATE_FILE)
            self.sys.prune_empty_dirs(STATE_DIR)
            return
        data = {"schema": SCHEMA, "fixes": self.fixes}
        self.sys.write_atomic(STATE_FILE, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode(), mode=0o644)
