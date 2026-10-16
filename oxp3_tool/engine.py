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
from .fixes import BY_ID, Fix, FixError, Plan, PostStep
from .state import BACKUP_DIR, STATE_DIR, State, backup_path_for
from .system import sha256_bytes
from .templates import OWN_MARKER

Printer = Callable[[str], None]

HARDWARE_SETUP_UNIT = "bazzite-hardware-setup.service"

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
      argument is in the next boot's arguments, and dropped otherwise;
    - a removal is finished once the argument is neither pending nor active;
    - an entry that records nothing and never completed is forgotten.
    """
    pending = ctx.pending_kargs
    if pending is None:
        return False
    changed = False
    for fix_id in list(state.fixes):
        entry = state.fixes[fix_id]
        for karg in list(entry.get("kargs_pending_add", [])):
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
            if unit not in (entry or {}).get("units_enabled", []) and self.ctx.unit_enabled(unit) != "enabled":
                lines.append(f"enable {unit}")
        old_opts = (entry or {}).get("options", {})
        for key, value in plan.options.items():
            if entry and old_opts.get(key) != value:
                lines.append(f"change {key}: {old_opts.get(key)} -> {value}")
        if entry and entry.get("notes", {}).get("restart_pending") and plan.post:
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
            for step in sorted(plan.post, key=lambda s: s.order):
                lines.append(f"    then {step.description}")
            lines.append(f"    takes effect: {fix.takes_effect}")
        return lines

    # -- apply -----------------------------------------------------------

    def apply(self, plans: list[tuple[Fix, Plan]]) -> Outcome:
        outcome = Outcome()
        todo = [(fix, plan) for fix, plan in plans if self.changes(fix, plan)]
        wanted_kargs = [k for _, p in todo for k in p.kargs]
        if wanted_kargs:
            outcome.messages += self._check_kargs_preconditions()

        post: list[PostStep] = []
        done: list[Fix] = []
        for fix, plan in todo:
            try:
                self._apply_local(fix, plan)
            except OSError as e:
                outcome.ok = False
                outcome.errors.append(f"{fix.id}: {e}")
                continue
            outcome.changed = True
            post += plan.post
            outcome.messages += plan.messages
            done.append(fix)

        if wanted_kargs:
            self._apply_kargs([(f, p) for f, p in todo if f in done], outcome)

        if post:
            self.out("Finishing up:")
            self._run_post(post, outcome)
        for fix in done:
            outcome.messages += fix.after_apply(self.ctx)
        return outcome

    def _apply_local(self, fix: Fix, plan: Plan) -> None:
        """Write files and enable units for one fix, recording each change first."""
        existing = self.state.get(fix.id)
        entry = self.state.entry(fix.id)
        entry.pop("reverted", None)
        try:
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

    def _apply_files(self, fix: Fix, plan: Plan, entry: dict) -> None:
        planned = {s.path for s in plan.files}
        for rec in [r for r in entry["files"] if r["path"] not in planned]:
            # Written by an earlier apply, no longer wanted.
            self._remove_file(fix.id, rec)
            entry["files"].remove(rec)
            self.state.save()

        for spec in plan.files:
            rec = next((r for r in entry["files"] if r["path"] == spec.path), None)
            wanted = sha256_bytes(spec.content)
            current = self.sys.sha256(spec.path)
            backup = rec.get("backup") if rec else None
            if rec is None and current is not None and current != wanted and not self._is_ours(spec.path):
                backup = self._backup(fix.id, spec.path)
            elif rec is None and current is not None:
                # Our own file left by a run whose record was lost: never back
                # it up as if it were the user's, but keep an older backup.
                earlier = backup_path_for(fix.id, spec.path)
                backup = earlier if self.sys.exists(earlier) else None
            new_dirs = _missing_dirs(self.ctx, spec.path)
            for d in new_dirs:
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

    def _backup(self, fix_id: str, path: str) -> str:
        backup = backup_path_for(fix_id, path)
        if self.sys.exists(backup):
            # Never overwrite an earlier backup; it may be the only copy of the original.
            n = 1
            while self.sys.exists(f"{backup}.{n}"):
                n += 1
            backup = f"{backup}.{n}"
        self.sys.copy(path, backup)
        self.out(f"  backed up {path} to {backup}")
        return backup

    def _apply_units(self, plan: Plan, entry: dict) -> None:
        for unit in plan.enable_units:
            if unit in entry["units_enabled"]:
                continue
            if self.ctx.unit_enabled(unit) == "enabled":
                pre = entry["notes"].setdefault("units_already_enabled", [])
                if unit not in pre:
                    pre.append(unit)
                continue
            entry["units_enabled"].append(unit)
            self.state.save()  # write-ahead
            res = self.sys.run(["systemctl", "enable", unit], timeout=60)
            if not res.ok:
                if self.ctx.unit_enabled(unit) != "enabled":
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
                    entry["kargs_removal_pending"].remove(karg)
                    if karg in pending and karg not in entry["kargs"]:
                        entry["kargs"].append(karg)  # removal never reached a boot
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
        path = rec["path"]
        current = self.sys.sha256(path)
        note = None
        if current is not None and current != rec.get("sha256"):
            saved = f"{STATE_DIR}/modified/{fix_id}{path}"
            self.sys.copy(path, saved)
            note = f"{path} had been edited since it was installed; a copy was kept at {saved}"
        backup = rec.get("backup")
        if backup and self.sys.exists(backup):
            self.sys.copy(backup, path)
            self._restorecon(path)
            self.sys.remove(backup)
            self.out(f"  restored {path} from {backup}")
        elif current is not None:
            self.sys.remove(path)
            self.out(f"  removed {path}")
        return note

    def revert(self, fix_ids: list[str]) -> Outcome:
        outcome = Outcome()
        post: list[PostStep] = []
        karg_fixes: list[str] = []

        for fix_id in fix_ids:
            entry = self.state.get(fix_id)
            if not entry:
                outcome.messages.append(f"{fix_id}: not applied by this tool, nothing to revert")
                continue
            fix = BY_ID.get(fix_id)
            self.out(f"{fix_id}:")
            try:
                for rec in list(entry.get("files", [])):
                    note = self._remove_file(fix_id, rec)
                    if note:
                        outcome.messages.append(note)
                    entry["files"].remove(rec)
                    self.state.save()
                for d in sorted(entry.get("dirs_created", []), key=len, reverse=True):
                    self.sys.rmdir_if_empty(d)
                    if not self.sys.exists(d):
                        entry["dirs_created"].remove(d)
                entry["dirs_created"] = []  # a directory still in use is no longer ours to remove
                for unit in list(entry.get("units_enabled", [])):
                    res = self.sys.run(["systemctl", "disable", unit], timeout=60)
                    if not res.ok:
                        raise OSError(f"systemctl disable {unit} failed: {res.stderr.strip()[:300]}")
                    entry["units_enabled"].remove(unit)
                    self.out(f"  disabled {unit}")
            except OSError as e:
                outcome.ok = False
                outcome.errors.append(f"{fix_id}: {e}")
                self.state.save()
                continue
            outcome.changed = True
            entry["reverted"] = True
            entry["options"] = {}
            if entry.get("kargs") or entry.get("kargs_removal_pending"):
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
        return outcome

    def _revert_kargs(self, fix_ids: list[str], outcome: Outcome) -> None:
        try:
            outcome.messages += self._check_kargs_preconditions()
        except EngineError as e:
            outcome.ok = False
            outcome.errors.append(str(e))
            return
        pending = self.ctx.pending_kargs or []
        for fix_id in fix_ids:
            entry = self.state.fixes[fix_id]
            for karg in entry.get("kargs", []):
                if karg not in entry["kargs_removal_pending"]:
                    entry["kargs_removal_pending"].append(karg)
            entry["kargs"] = []
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
                    entry.pop("reverted", None)
            else:
                outcome.reboot_needed = True
                outcome.messages.append(KARGS_UPDATE_WARNING)
        reconcile(self.ctx, self.state)
        self.state.save()
