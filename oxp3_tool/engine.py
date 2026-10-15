"""Apply and revert fixes, recording every change in the state manifest."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .detect import Context
from .fixes import BY_ID, Fix, FixError, Plan, PostStep
from .state import BACKUP_DIR, STATE_DIR, State, backup_path_for
from .system import sha256_bytes

Printer = Callable[[str], None]

HARDWARE_SETUP_UNIT = "bazzite-hardware-setup.service"


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


class Engine:
    def __init__(self, ctx: Context, state: State, out: Printer = print):
        self.ctx = ctx
        self.sys = ctx.sys
        self.state = state
        self.out = out

    # -- helpers ---------------------------------------------------------

    def _restorecon(self, path: str) -> None:
        # Keep SELinux labels right for files created by this tool.
        if self.sys.is_real_root and self.sys.which("restorecon"):
            self.sys.run(["restorecon", path], timeout=30)

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
            notes.append("A staged update is waiting for a reboot; the kernel arguments are added to it.")
        return notes

    def _run_post(self, steps: list[PostStep], outcome: Outcome) -> None:
        seen = set()
        for step in steps:
            if step.key in seen:
                continue
            seen.add(step.key)
            self.out(f"  - {step.description}: {' '.join(step.argv)}")
            res = self.sys.run(step.argv, timeout=120)
            if not res.ok:
                outcome.messages.append(
                    f"warning: `{' '.join(step.argv)}` failed ({res.returncode}): {res.stderr.strip()[:300]}"
                )

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
                plans.append((fix, fix.plan(self.ctx, opts)))
            except FixError as e:
                errors.append(f"{fix.id}: {e}")
        if errors:
            raise EngineError("\n".join(errors))
        return plans

    def describe(self, plans: list[tuple[Fix, Plan]]) -> list[str]:
        lines = []
        for fix, plan in plans:
            lines.append(f"{fix.id}: {fix.title}")
            entry = self.state.get(fix.id)
            for spec in plan.files:
                current = self.sys.sha256(spec.path)
                if current == sha256_bytes(spec.content):
                    verb = "keep (unchanged)"
                elif current is None:
                    verb = "create"
                elif entry and any(f["path"] == spec.path for f in entry.get("files", [])):
                    verb = "update"
                else:
                    verb = "replace (the current file is backed up)"
                lines.append(f"    {verb} {spec.path}")
            for karg in plan.kargs:
                if self.ctx.karg_pending(karg):
                    lines.append(f"    kernel argument {karg} is already set")
                else:
                    lines.append(f"    add kernel argument {karg} (rpm-ostree kargs, reboot needed)")
            for unit in plan.enable_units:
                lines.append(f"    enable {unit}")
            for step in plan.post:
                lines.append(f"    then {step.description}")
            lines.append(f"    takes effect: {fix.takes_effect}")
        return lines

    # -- apply -----------------------------------------------------------

    def apply(self, plans: list[tuple[Fix, Plan]]) -> Outcome:
        outcome = Outcome()
        wanted_kargs = [k for _, p in plans for k in p.kargs]
        if wanted_kargs:
            outcome.messages += self._check_kargs_preconditions()

        post: list[PostStep] = []
        for fix, plan in plans:
            try:
                changed = self._apply_local(fix, plan)
            except OSError as e:
                outcome.ok = False
                outcome.errors.append(f"{fix.id}: {e}")
                continue
            outcome.changed |= changed
            if changed:
                post += plan.post
            outcome.messages += plan.messages

        if wanted_kargs:
            self._apply_kargs(plans, outcome)

        if post:
            self.out("Finishing up:")
            self._run_post(post, outcome)
        return outcome

    def _apply_local(self, fix: Fix, plan: Plan) -> bool:
        """Write files and enable units for one fix; returns True if anything changed."""
        existing = self.state.get(fix.id)
        entry = self.state.entry(fix.id)
        old_files = {rec["path"]: rec for rec in entry["files"]}
        new_files = []
        changed = existing is None

        for spec in plan.files:
            rec = old_files.pop(spec.path, None)
            backup = rec.get("backup") if rec else None
            current = self.sys.sha256(spec.path)
            wanted = sha256_bytes(spec.content)
            if current is not None and rec is None:
                # Not ours: keep a copy so revert can put it back.
                backup = backup_path_for(fix.id, spec.path)
                self.sys.copy(spec.path, backup)
                self.out(f"  backed up {spec.path} to {backup}")
            if current != wanted:
                self.sys.write_atomic(spec.path, spec.content, spec.mode)
                self._restorecon(spec.path)
                self.out(f"  wrote {spec.path}")
                changed = True
            new_files.append({"path": spec.path, "sha256": wanted, "backup": backup})

        # Files from an earlier apply that this plan no longer writes.
        for rec in old_files.values():
            self._remove_file(fix.id, rec)
            changed = True

        entry["files"] = new_files

        for unit in plan.enable_units:
            if unit in entry["units_enabled"]:
                continue
            if self.ctx.unit_enabled(unit) == "enabled":
                entry["notes"].setdefault("units_already_enabled", [])
                if unit not in entry["notes"]["units_already_enabled"]:
                    entry["notes"]["units_already_enabled"].append(unit)
                continue
            res = self.sys.run(["systemctl", "enable", unit], timeout=60)
            if not res.ok:
                self.state.save()
                raise OSError(f"systemctl enable {unit} failed: {res.stderr.strip()[:300]}")
            entry["units_enabled"].append(unit)
            self.out(f"  enabled {unit}")
            changed = True

        if entry.get("options") != plan.options:
            changed = True
        entry["options"] = plan.options
        entry["notes"].update(plan.notes)
        if changed or not entry.get("applied_at"):
            self.state.stamp(fix.id)
        if plan.files or plan.enable_units or existing is not None:
            self.state.save()
        return changed

    def _apply_kargs(self, plans: list[tuple[Fix, Plan]], outcome: Outcome) -> None:
        pending = self.ctx.pending_kargs or []
        missing: list[str] = []
        for _, plan in plans:
            for karg in plan.kargs:
                if karg not in pending and karg not in missing:
                    missing.append(karg)

        if missing:
            argv = ["rpm-ostree", "kargs"] + [f"--append-if-missing={k}" for k in missing]
            self.out(f"  running: {' '.join(argv)}")
            res = self.sys.run(argv, timeout=600)
            if not res.ok:
                outcome.ok = False
                outcome.errors.append(f"rpm-ostree kargs failed ({res.returncode}): {res.stderr.strip()[:500]}")
                for fix, plan in plans:
                    if plan.kargs and not plan.files and not plan.enable_units and not self.state.get(fix.id)["kargs"]:
                        self.state.forget(fix.id)
                self.state.save()
                return
            outcome.changed = True
            outcome.reboot_needed = True
            self.ctx.invalidate_kargs()

        for fix, plan in plans:
            if not plan.kargs:
                continue
            entry = self.state.entry(fix.id)
            for karg in plan.kargs:
                if karg in missing:
                    if karg not in entry["kargs"]:
                        entry["kargs"].append(karg)
                elif karg not in entry["kargs"]:
                    # Set before this tool touched it: never removed on revert.
                    pre = entry["notes"].setdefault("kargs_already_set", [])
                    if karg not in pre:
                        pre.append(karg)
            if not entry.get("applied_at"):
                self.state.stamp(fix.id)
            if not any(self.ctx.karg_active(k) for k in plan.kargs):
                outcome.reboot_needed = True
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
        kargs_to_delete: dict[str, list[str]] = {}

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
            if entry.get("kargs"):
                kargs_to_delete[fix_id] = list(entry["kargs"])
            else:
                self.state.forget(fix_id)
            self.state.save()
            if fix:
                post += fix.revert_post(self.ctx)
                outcome.messages += fix.revert_messages()

        if kargs_to_delete:
            self._revert_kargs(kargs_to_delete, outcome)

        if post:
            self.out("Finishing up:")
            self._run_post(post, outcome)
        self.sys.prune_empty_dirs(BACKUP_DIR)
        return outcome

    def _revert_kargs(self, kargs_by_fix: dict[str, list[str]], outcome: Outcome) -> None:
        try:
            outcome.messages += self._check_kargs_preconditions()
        except EngineError as e:
            outcome.ok = False
            outcome.errors.append(str(e))
            return
        pending = self.ctx.pending_kargs or []
        present = sorted({k for ks in kargs_by_fix.values() for k in ks if k in pending})
        if present:
            argv = ["rpm-ostree", "kargs"] + [f"--delete-if-present={k}" for k in present]
            self.out(f"  running: {' '.join(argv)}")
            res = self.sys.run(argv, timeout=600)
            if not res.ok:
                outcome.ok = False
                outcome.errors.append(f"rpm-ostree kargs failed ({res.returncode}): {res.stderr.strip()[:500]}")
                return
            outcome.reboot_needed = True
            self.ctx.invalidate_kargs()
        for fix_id in kargs_by_fix:
            self.state.forget(fix_id)
        self.state.save()
