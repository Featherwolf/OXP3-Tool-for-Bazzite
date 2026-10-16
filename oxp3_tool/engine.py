"""Apply and revert fixes, recording every change in the state manifest.

Changes are recorded before they are made (write-ahead), so an interrupted
run never leaves a change the manifest does not know about. Kernel argument
changes only land in a staged deployment, and a system update installed
before the reboot is built from the booted arguments and drops them, so the
manifest tracks additions and removals until /proc/cmdline confirms them
(see ``reconcile``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .detect import Context
from .fixes import BY_ID, RESTART_INPUTPLUMBER, Fix, FixError, Plan, PostStep, restart_pending
from .state import BACKUP_DIR, STATE_DIR, State, backup_path_for
from .system import sha256_bytes
from .templates import OWN_MARKER

Printer = Callable[[str], None]

HARDWARE_SETUP_UNIT = "bazzite-hardware-setup.service"
UNIT_DIR = "/etc/systemd/system"
# Directories only this tool writes to. Every fix that writes into one records
# it, so whichever is reverted last removes it.
TOOL_DIRS = ("/usr/local/libexec/oxp3-tool",)

KARGS_UPDATE_WARNING = (
    "Reboot before installing a system update: an update installed first is built from the "
    "current kernel arguments and drops this change."
)


class EngineError(Exception):
    pass


@dataclass
class Outcome:
    ok: bool = True
    changed: bool = False
    reboot_needed: bool = False
    messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class FileStatus:
    path: str
    present: bool
    matches: bool


def file_statuses(ctx: Context, entry: dict) -> list[FileStatus]:
    out = []
    for rec in entry.get("files", []):
        current = ctx.sys.sha256(rec["path"])
        out.append(FileStatus(rec["path"], current is not None, current == rec.get("sha256")))
    return out


def entry_is_empty(entry: dict) -> bool:
    """True when an entry no longer records any change on the system."""
    return not any(entry.get(k) for k in (
        "files", "dirs_created", "kargs", "kargs_pending_add", "kargs_removal_pending", "units_enabled",
    ))


def reconcile(ctx: Context, state: State) -> bool:
    """Settle kernel-argument bookkeeping against the system; returns True if state changed.

    - an addition whose outcome was unknown (interrupted run) is owned if the
      argument is in the next boot's arguments, and dropped otherwise (not
      while an rpm-ostree transaction is still running);
    - a removal is finished once the argument is neither pending nor active;
    - an entry that records nothing and never completed is forgotten.
    """
    pending = ctx.pending_kargs
    if pending is None:
        return False
    # A transaction still running may yet add the argument: settle it later.
    busy = ctx.rpm_ostree_busy
    changed = False
    for fix_id in list(state.fixes):
        entry = state.fixes[fix_id]
        for karg in [] if busy else list(entry.get("kargs_pending_add", [])):
            entry["kargs_pending_add"].remove(karg)
            if karg in pending and karg not in entry.setdefault("kargs", []):
                entry["kargs"].append(karg)
            changed = True
        for karg in list(entry.get("kargs_removal_pending", [])):
            if karg not in pending and not ctx.karg_active(karg):
                entry["kargs_removal_pending"].remove(karg)
                changed = True
        if entry_is_empty(entry) and (not entry.get("applied_at") or entry.get("reverted")):
            state.forget(fix_id)
            changed = True
    return changed


def _missing_dirs(ctx: Context, path: str) -> list[str]:
    """Ancestors of ``path`` that do not exist yet, outermost first."""
    out = []
    d = path.rsplit("/", 1)[0]
    while d and d != "/" and not ctx.sys.exists(d):
        out.append(d)
        d = d.rsplit("/", 1)[0]
    return list(reversed(out))


class Engine:
    def __init__(self, ctx: Context, state: State, out: Printer = print):
        self.ctx = ctx
        self.sys = ctx.sys
        self.state = state
        self.out = out
        self.notes: list[str] = []

    # -- helpers ---------------------------------------------------------

    def _restorecon(self, path: str) -> None:
        # Keep SELinux labels right for files and directories this tool creates.
        if self.sys.is_real_root and self.sys.which("restorecon"):
            self.sys.run(["restorecon", path], timeout=30)

    def _is_ours(self, path: str) -> bool:
        head = (self.sys.read_bytes(path) or b"")[:512].decode("utf-8", errors="replace")
        return OWN_MARKER in "\n".join(head.splitlines()[:3])

    def _check_kargs_preconditions(self) -> list[str]:
        ctx = self.ctx
        if not ctx.has_rpm_ostree:
            raise EngineError("rpm-ostree is not available, so kernel arguments cannot be changed")
        if ctx.rpm_ostree_busy:
            raise EngineError("an rpm-ostree transaction is running (an update?); try again when it is done")
        if ctx.unit_state(HARDWARE_SETUP_UNIT) == "activating":
            raise EngineError(f"{HARDWARE_SETUP_UNIT} is still running; try again in a minute")
        if ctx.pending_kargs is None:
            raise EngineError("could not read the current kernel arguments with `rpm-ostree kargs`")
        notes = []
        if ctx.has_staged_deployment:
            notes.append("A staged update is waiting for a reboot; the kernel arguments are changed in it.")
        return notes

    def _run_post(self, steps: list[PostStep], outcome: Outcome) -> None:
        seen = set()
        for step in sorted(steps, key=lambda s: s.order):
            if step.key in seen:
                continue
            seen.add(step.key)
            self.out(f"  - {step.description}: {' '.join(step.argv)}")
            res = self.sys.run(step.argv, timeout=120)
            if not res.ok:
                outcome.messages.append(
                    f"warning: `{' '.join(step.argv)}` failed ({res.returncode}): {res.stderr.strip()[:300]}"
                )

    def _prev_options(self, fix: Fix, opts: dict) -> dict:
        if opts.get("defaults"):
            return {}
        entry = self.state.get(fix.id)
        return dict(entry.get("options", {})) if entry else {}

    # -- planning --------------------------------------------------------

    def plan(self, fixes: list[Fix], opts: dict) -> list[tuple[Fix, Plan]]:
        if not self.ctx.device_ok:
            raise EngineError("this is not a ONEXPLAYER 3 (use --force only for development)")
        if not self.ctx.os_ok:
            raise EngineError("this is not Bazzite (use --force only for development)")
        ids = {f.id for f in fixes}
        plans = []
        errors = []
        for fix in fixes:
            for other in fix.conflicts:
                if other in ids or self.state.get(other):
                    errors.append(f"{fix.id}: conflicts with {other}")
            check = fix.check(self.ctx)
            if not check.applicable:
                errors.append(f"{fix.id}: not applicable: {check.reason}")
                continue
            try:
                plans.append((fix, fix.plan(self.ctx, opts, self._prev_options(fix, opts))))
            except FixError as e:
                errors.append(f"{fix.id}: {e}")
        if errors:
            raise EngineError("\n".join(errors))
        return plans

    def changes(self, fix: Fix, plan: Plan) -> list[str]:
        """Human-readable list of what applying ``plan`` would change (empty: nothing)."""
        entry = self.state.get(fix.id)
        recorded = {rec["path"]: rec for rec in (entry or {}).get("files", [])}
        lines = []
        for spec in plan.files:
            current = self.sys.sha256(spec.path)
            if current == sha256_bytes(spec.content):
                if spec.path not in recorded:
                    lines.append(f"record {spec.path} (already has the wanted content)")
                continue
            if current is None:
                lines.append(f"create {spec.path}")
            elif spec.path in recorded and current != recorded[spec.path].get("sha256"):
                lines.append(f"replace {spec.path} (edited since it was installed; your version is "
                             f"saved to {STATE_DIR}/modified/)")
            elif spec.path in recorded or self._is_ours(spec.path):
                lines.append(f"update {spec.path}")
            else:
                lines.append(f"replace {spec.path} (the current file is backed up)")
        for path in recorded:
            if path not in {s.path for s in plan.files}:
                lines.append(f"remove {path}")
        for karg in plan.kargs:
            removal = karg in (entry or {}).get("kargs_removal_pending", [])
            if not self.ctx.karg_pending(karg):
                lines.append(f"add kernel argument {karg} (rpm-ostree kargs, reboot needed)")
            elif removal:
                lines.append(f"keep kernel argument {karg} (cancels the pending removal)")
        for unit in plan.enable_units:
            if self.ctx.unit_enabled(unit) != "enabled":
                lines.append(f"enable {unit}")
        for unit in (entry or {}).get("units_enabled", []):
            if unit not in plan.enable_units:
                lines.append(f"stop and disable {unit}" if plan.stop_dropped_units
                             else f"disable {unit} (it keeps running until the next boot)")
        old_opts = (entry or {}).get("options", {})
        for key, value in plan.options.items():
            if entry and old_opts.get(key) != value:
                lines.append(f"change {key}: {old_opts.get(key)} -> {value}")
        if entry and restart_pending(self.ctx, entry.get("notes", {})) and \
                any(s.key == RESTART_INPUTPLUMBER for s in plan.post):
            lines.append("finish the InputPlumber restart skipped earlier")
        if entry is None or not entry.get("applied_at") or entry.get("reverted"):
            if not lines:
                lines.append("record as applied (already in place)")
        return lines

    def describe(self, plans: list[tuple[Fix, Plan]]) -> list[str]:
        lines = []
        for fix, plan in plans:
            lines.append(f"{fix.id}: {fix.title}")
            changes = self.changes(fix, plan)
            if not changes:
                lines.append("    already applied, nothing to change")
                continue
            lines += [f"    {c}" for c in changes]
            for step in sorted(self._due_steps(fix, plan), key=lambda s: s.order):
                lines.append(f"    then {step.description}")
            lines.append(f"    takes effect: {fix.takes_effect}")
        return lines

    def _due_steps(self, fix: Fix, plan: Plan) -> list[PostStep]:
        """The post steps applying ``plan`` runs; call it before any file is written."""
        entry = self.state.get(fix.id)
        pending = bool(entry) and restart_pending(self.ctx, entry.get("notes", {}))
        wanted = {s.path: sha256_bytes(s.content) for s in plan.files}
        recorded = {r["path"] for r in (entry or {}).get("files", [])}

        def changes(path: str) -> bool:
            if path in wanted:
                return self.sys.sha256(path) != wanted[path]
            return path in recorded and self.sys.exists(path)  # removed

        return [s for s in plan.post
                if not s.only_if_changed or pending or any(changes(p) for p in s.only_if_changed)]

    # -- apply -----------------------------------------------------------

    def apply(self, plans: list[tuple[Fix, Plan]]) -> Outcome:
        outcome = Outcome()
        self.notes = []
        todo = [(fix, plan) for fix, plan in plans if self.changes(fix, plan)]
        wanted_kargs = [k for _, p in todo for k in p.kargs]
        if wanted_kargs:
            outcome.messages += self._check_kargs_preconditions()

        post: list[PostStep] = []
        done: list[Fix] = []
        restarting: list[Fix] = []
        for fix, plan in todo:
            due = self._due_steps(fix, plan)
            gated = any(s.only_if_changed for s in due)
            if gated:
                # Write-ahead: a failed or interrupted run leaves the restart pending, so the next run does it.
                self.state.entry(fix.id)["notes"].update(restart_pending=True, restart_pending_boot=self.ctx.boot_id)
                self.state.save()
            try:
                dropped = self._apply_local(fix, plan)
            except OSError as e:
                outcome.ok = False
                outcome.errors.append(f"{fix.id}: {e}")
                continue
            outcome.changed = True
            if dropped:
                post.append(PostStep("daemon-reload", ["systemctl", "daemon-reload"], "reload systemd units",
                                     order=10))
            post += due
            outcome.messages += plan.messages + [s.note for s in due if s.note]
            done.append(fix)
            if gated:
                restarting.append(fix)

        if wanted_kargs:
            self._apply_kargs([(f, p) for f, p in todo if f in done], outcome)

        if post:
            self.out("Finishing up:")
            self._run_post(post, outcome)
        for fix in restarting:
            self.state.entry(fix.id)["notes"].update(restart_pending=False, restart_pending_boot=None)
        if restarting:
            self.state.save()
        for fix in done:
            outcome.messages += fix.after_apply(self.ctx)
        outcome.messages = self.notes + outcome.messages
        return outcome

    def _apply_local(self, fix: Fix, plan: Plan) -> bool:
        """Write files and enable units for one fix, recording each change first.

        Returns whether a unit enabled by an earlier apply was dropped (systemd needs a reload).
        """
        existing = self.state.get(fix.id)
        entry = self.state.entry(fix.id)
        entry.pop("reverted", None)
        try:
            # Units first: disabling needs a unit file this fix may be about to remove.
            dropped = self._drop_units(plan, entry)
            self._apply_files(fix, plan, entry)
            self._apply_units(plan, entry)
        except OSError:
            if existing is None and entry_is_empty(entry):
                self.state.forget(fix.id)
            self.state.save()
            raise
        entry["options"] = plan.options
        entry["notes"].update(plan.notes)
        if not plan.kargs:
            self.state.stamp(fix.id)
        self.state.save()
        return dropped

    def _drop_units(self, plan: Plan, entry: dict) -> bool:
        """Disable (and stop) units an earlier apply enabled that ``plan`` no longer wants."""
        dropped = False
        for unit in [u for u in entry["units_enabled"] if u not in plan.enable_units]:
            self._disable_unit(unit, now=plan.stop_dropped_units)
            entry["units_enabled"].remove(unit)
            self.state.save()
            dropped = True
        return dropped

    def _apply_files(self, fix: Fix, plan: Plan, entry: dict) -> None:
        planned = {s.path for s in plan.files}
        for rec in [r for r in entry["files"] if r["path"] not in planned]:
            # Written by an earlier apply, no longer wanted.
            self._drop_file(fix.id, entry, rec)

        for spec in plan.files:
            rec = next((r for r in entry["files"] if r["path"] == spec.path), None)
            wanted = sha256_bytes(spec.content)
            current = self.sys.sha256(spec.path)
            backup = rec.get("backup") if rec else None
            if rec is not None and current not in (None, rec.get("sha256"), wanted):
                self._save_edited(fix.id, spec.path)
            if rec is None and current is not None and current != wanted and not self._is_ours(spec.path):
                backup = self._backup(fix.id, spec.path)
            elif rec is None and current is not None:
                # Our own file left by a run whose record was lost: never back
                # it up as if it were the user's, but keep an older backup.
                earlier = backup_path_for(fix.id, spec.path)
                backup = earlier if self.sys.exists(earlier) else None
            new_dirs = _missing_dirs(self.ctx, spec.path)
            parent = spec.path.rsplit("/", 1)[0]
            for d in new_dirs + ([parent] if parent in TOOL_DIRS else []):
                if d not in entry["dirs_created"]:
                    entry["dirs_created"].append(d)
            if rec is None:
                rec = {"path": spec.path}
                entry["files"].append(rec)
            rec.update({"sha256": wanted, "backup": backup})
            self.state.save()  # write-ahead
            if current != wanted:
                self.sys.write_atomic(spec.path, spec.content, spec.mode)
                for d in new_dirs:
                    self._restorecon(d)
                self._restorecon(spec.path)
                self.out(f"  wrote {spec.path}")

    def _free_path(self, base: str) -> str:
        """``base`` or the first free ``base.N``: earlier copies are never overwritten."""
        if not self.sys.exists(base):
            return base
        n = 1
        while self.sys.exists(f"{base}.{n}"):
            n += 1
        return f"{base}.{n}"

    def _backup(self, fix_id: str, path: str) -> str:
        backup = self._free_path(backup_path_for(fix_id, path))
        self.sys.copy(path, backup)
        self.out(f"  backed up {path} to {backup}")
        return backup

    def _save_edited(self, fix_id: str, path: str) -> None:
        saved = self._free_path(f"{STATE_DIR}/modified/{fix_id}{path}")
        self.sys.copy(path, saved)
        self.notes.append(f"{path} had been edited since it was installed; a copy was kept at {saved}")

    def _drop_file(self, fix_id: str, entry: dict, rec: dict) -> None:
        """Undo one recorded file. The backup is deleted only after the saved
        manifest no longer points at it, so an interrupted revert can never
        lose the user's original."""
        backup = self._remove_file(fix_id, rec)
        entry["files"].remove(rec)
        self.state.save()
        if backup:
            self.sys.remove(backup)

    def _apply_units(self, plan: Plan, entry: dict) -> None:
        for unit in plan.enable_units:
            recorded = unit in entry["units_enabled"]
            if self.ctx.unit_enabled(unit) == "enabled":
                if recorded:
                    continue
                if any(s.path == f"{UNIT_DIR}/{unit}" for s in plan.files):
                    # Our own unit file, enabled by a run whose record was lost.
                    entry["units_enabled"].append(unit)
                    self.state.save()
                else:
                    pre = entry["notes"].setdefault("units_already_enabled", [])
                    if unit not in pre:
                        pre.append(unit)
                continue
            if not recorded:
                entry["units_enabled"].append(unit)
                self.state.save()  # write-ahead
            res = self.sys.run(["systemctl", "enable", unit], timeout=60)
            if not res.ok:
                if not recorded and self.ctx.unit_enabled(unit) != "enabled":
                    entry["units_enabled"].remove(unit)
                raise OSError(f"systemctl enable {unit} failed: {res.stderr.strip()[:300]}")
            self.out(f"  enabled {unit}")

    def _apply_kargs(self, plans: list[tuple[Fix, Plan]], outcome: Outcome) -> None:
        pending = self.ctx.pending_kargs or []
        missing: list[str] = []
        for fix, plan in plans:
            entry = self.state.entry(fix.id)
            for karg in plan.kargs:
                if karg in entry["kargs_removal_pending"]:
                    # Still ours: active in the booted deployment even if the
                    # next boot no longer has it.
                    entry["kargs_removal_pending"].remove(karg)
                    if karg not in entry["kargs"]:
                        entry["kargs"].append(karg)
                if karg in pending:
                    if karg not in entry["kargs"]:
                        pre = entry["notes"].setdefault("kargs_already_set", [])
                        if karg not in pre:
                            pre.append(karg)  # set by someone else: never removed on revert
                    continue
                if karg not in missing:
                    missing.append(karg)
                if karg not in entry["kargs_pending_add"]:
                    entry["kargs_pending_add"].append(karg)
            if all(k in pending for k in plan.kargs):
                self.state.stamp(fix.id)  # nothing to add; record it before reconcile runs
        self.state.save()  # write-ahead

        if missing:
            argv = ["rpm-ostree", "kargs"] + [f"--append-if-missing={k}" for k in missing]
            self.out(f"  running: {' '.join(argv)}")
            res = self.sys.run(argv, timeout=600)
            if not res.ok:
                outcome.ok = False
                outcome.errors.append(f"rpm-ostree kargs failed ({res.returncode}): {res.stderr.strip()[:500]}")
            self.ctx.invalidate_kargs()
            reconcile(self.ctx, self.state)
            if res.ok:
                outcome.reboot_needed = True
                outcome.messages.append(KARGS_UPDATE_WARNING)

        for fix, plan in plans:
            entry = self.state.get(fix.id)
            if entry and all(self.ctx.karg_pending(k) for k in plan.kargs):
                self.state.stamp(fix.id)
        self.state.save()

    # -- revert ----------------------------------------------------------

    def _remove_file(self, fix_id: str, rec: dict) -> Optional[str]:
        """Remove or restore one file; returns the backup to delete once the record is gone."""
        path = rec["path"]
        current = self.sys.sha256(path)
        backup = rec.get("backup")
        edited = current is not None and current != rec.get("sha256")
        if backup and not self.sys.exists(backup) and edited:
            # An interrupted revert already put the original back.
            self.out(f"  kept {path} (the original was already restored)")
            return None
        if edited:
            self._save_edited(fix_id, path)
        if backup and self.sys.exists(backup):
            self.sys.copy(backup, path)
            self._restorecon(path)
            self.out(f"  restored {path} from {backup}")
            return backup
        if current is not None:
            self.sys.remove(path)
            self.out(f"  removed {path}")
        return None

    def _needs_kargs_change(self, entry: dict) -> bool:
        return bool(entry.get("kargs") or entry.get("kargs_pending_add")) or any(
            self.ctx.karg_pending(k) for k in entry.get("kargs_removal_pending", [])
        )

    def _disable_unit(self, unit: str, now: bool = False) -> None:
        res = self.sys.run(["systemctl", "disable", *(["--now"] if now else []), unit], timeout=60)
        if res.ok:
            self.out(f"  {'stopped and ' if now else ''}disabled {unit}")
            return
        if self.ctx.unit_enabled(unit) != "enabled" and not self.ctx.unit_file_exists(unit):
            self.notes.append(f"{unit} is no longer installed; nothing to disable")
            return
        raise OSError(f"systemctl disable {unit} failed: {res.stderr.strip()[:300]}")

    def revert(self, fix_ids: list[str]) -> Outcome:
        outcome = Outcome()
        self.notes = []
        post: list[PostStep] = []
        karg_fixes: list[str] = []

        # Refuse up front, before anything is removed, if kernel arguments
        # cannot be changed right now.
        if any(self._needs_kargs_change(self.state.get(i)) for i in fix_ids if self.state.get(i)):
            outcome.messages += self._check_kargs_preconditions()

        for fix_id in fix_ids:
            entry = self.state.get(fix_id)
            if not entry:
                outcome.messages.append(f"{fix_id}: not applied by this tool, nothing to revert")
                continue
            fix = BY_ID.get(fix_id)
            self.out(f"{fix_id}:")
            try:
                # Units first: disabling needs a unit file this fix may have installed.
                for unit in list(entry.get("units_enabled", [])):
                    self._disable_unit(unit)
                    entry["units_enabled"].remove(unit)
                    self.state.save()
                records = list(entry.get("files", []))
                for rec in records:
                    self._drop_file(fix_id, entry, rec)
                # Also for state from before every fix recorded the shared directories.
                tool_dirs = [d for d in TOOL_DIRS if any(r["path"].startswith(d + "/") for r in records)]
                for d in sorted(set(entry.get("dirs_created", []) + tool_dirs), key=len, reverse=True):
                    self.sys.rmdir_if_empty(d)
                entry["dirs_created"] = []  # a directory still in use is no longer ours to remove
            except OSError as e:
                outcome.ok = False
                outcome.errors.append(f"{fix_id}: {e}")
                self.state.save()
                continue
            outcome.changed = True
            entry["reverted"] = True
            entry["options"] = {}
            if entry.get("kargs") or entry.get("kargs_pending_add") or entry.get("kargs_removal_pending"):
                karg_fixes.append(fix_id)
            elif entry_is_empty(entry):
                self.state.forget(fix_id)
            self.state.save()
            if fix:
                post += fix.revert_post(self.ctx)
                outcome.messages += fix.revert_messages()

        if karg_fixes:
            self._revert_kargs(karg_fixes, outcome)

        if post:
            self.out("Finishing up:")
            self._run_post(post, outcome)
        self.sys.prune_empty_dirs(BACKUP_DIR)
        outcome.messages = self.notes + outcome.messages
        return outcome

    def _revert_kargs(self, fix_ids: list[str], outcome: Outcome) -> None:
        try:
            self._check_kargs_preconditions()
        except EngineError as e:
            outcome.ok = False
            outcome.errors.append(str(e))
            for fix_id in fix_ids:
                self.state.fixes[fix_id].pop("reverted", None)
            self.state.save()
            return
        pending = self.ctx.pending_kargs or []
        for fix_id in fix_ids:
            entry = self.state.fixes[fix_id]
            # kargs_pending_add only ever holds arguments that were missing before.
            for karg in entry.get("kargs", []) + entry.get("kargs_pending_add", []):
                if karg not in entry["kargs_removal_pending"]:
                    entry["kargs_removal_pending"].append(karg)
            entry["kargs"] = []
            entry["kargs_pending_add"] = []
        self.state.save()  # write-ahead

        present = sorted({k for f in fix_ids for k in self.state.fixes[f]["kargs_removal_pending"] if k in pending})
        if present:
            argv = ["rpm-ostree", "kargs"] + [f"--delete-if-present={k}" for k in present]
            self.out(f"  running: {' '.join(argv)}")
            res = self.sys.run(argv, timeout=600)
            self.ctx.invalidate_kargs()
            if not res.ok:
                outcome.ok = False
                outcome.errors.append(f"rpm-ostree kargs failed ({res.returncode}): {res.stderr.strip()[:500]}")
                # Whatever is still in the next boot's arguments is still ours to remove.
                still = self.ctx.pending_kargs or []
                for fix_id in fix_ids:
                    entry = self.state.fixes[fix_id]
                    for karg in list(entry["kargs_removal_pending"]):
                        if karg in still:
                            entry["kargs_removal_pending"].remove(karg)
                            entry["kargs"].append(karg)
                    if entry["kargs"]:
                        entry.pop("reverted", None)
            else:
                outcome.reboot_needed = True
                outcome.messages.append(KARGS_UPDATE_WARNING)
        reconcile(self.ctx, self.state)
        self.state.save()
