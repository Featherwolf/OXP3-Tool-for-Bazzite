"""Command-line interface."""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Any, Optional, Sequence

from . import PROJECT_URL, __version__, diagnose, kernel_cmd, steam_session, templates
from .detect import CACHYOS, NATIVE_STEAM, OGUI, Context
from .engine import Engine, EngineError, Outcome, file_statuses, reconcile
from .fixes import ALL_FIXES, BY_ID, CONTROLLER, DISPLAY, OPTIONAL, RECOMMENDED, Fix, restart_pending
from .kargs import REBUILD_MARKER
from .state import State, StateError
from .system import SimulatedRunner, System


# With --json every line goes into the JSON result instead of the terminal (stdout carries one JSON object).
_LOG: Optional[list[dict[str, str]]] = None
_RESULT: dict[str, Any] = {}


def _out(line: str = "") -> None:
    if _LOG is not None:
        _LOG.append({"stream": "out", "text": line})
        return
    print(line, flush=True)


def _err(line: str) -> None:
    if _LOG is not None:
        _LOG.append({"stream": "err", "text": line})
        return
    print(line, file=sys.stderr, flush=True)


# -- status ----------------------------------------------------------------------


def _fix_state(ctx: Context, state: State, fix: Fix) -> tuple[str, list[str]]:
    if state.load_error:
        return "unknown", []
    entry = state.get(fix.id)
    if entry and entry.get("reverted"):
        lines = []
        for karg in entry.get("kargs_removal_pending", []):
            if ctx.karg_left(karg):
                why = ("came back with a system update installed before the reboot"
                       if ctx.karg_backend.name == "rpm-ostree" else "is still in the next boot's configuration")
                lines.append(f"{karg} {why}; run `sudo oxp3-tool revert {fix.id}` again")
            elif ctx.kargs_left is None and ctx.karg_backend.name != "rpm-ostree":
                lines.append(f"{karg} is being removed; run `sudo oxp3-tool status` to read the boot entries")
            else:
                lines.append(f"{karg} is removed from the next boot {ctx.reboot_advice('finish')}")
        return "reverting", lines
    if entry and not entry.get("applied_at"):
        return "incomplete", [f"an earlier run did not finish; run `sudo oxp3-tool apply {fix.id}` "
                              f"or `sudo oxp3-tool revert {fix.id}`"]
    if entry:
        problems = []
        for fs in file_statuses(ctx, entry):
            if not fs.present:
                problems.append(f"{fs.path} is missing")
            elif not fs.matches:
                problems.append(f"{fs.path} was changed by something else")
        for unit in entry.get("units_enabled", []):
            if ctx.unit_enabled(unit) != "enabled":
                problems.append(f"{unit} is not enabled any more")
        label = "applied" if not problems else "damaged"
        return label, problems + fix.live_state(ctx, entry)
    check = fix.check(ctx)
    if not check.applicable:
        return "n/a", [check.reason]
    if check.recommended:
        label = "recommended"
    elif fix.level == RECOMMENDED:
        label = OPTIONAL  # e.g. nvme-resume without the GM7 SSD
    else:
        label = fix.level
    return label, ([check.reason] if check.reason else []) + fix.note(ctx)


def _wrap(text: str, first: str, rest: str, width: int = 100) -> str:
    """Wrap advice for a small screen without breaking `commands` apart."""
    parts = text.split("`")
    for i in range(1, len(parts), 2):
        parts[i] = parts[i].replace(" ", "\0")
    wrapped = textwrap.fill("`".join(parts), width=width, initial_indent=first, subsequent_indent=rest,
                            break_on_hyphens=False, break_long_words=False)
    return wrapped.replace("\0", " ")


def status_text(ctx: Context, state: State) -> str:
    return "\n".join(_status(ctx, state)[0])


def _status(ctx: Context, state: State) -> tuple[list[str], list[str], list[str]]:
    """(the status lines, the warnings among them, the notes among them) for text and --json."""
    lines = [f"oxp3-tool {__version__}"]
    warnings: list[str] = []
    notes: list[str] = []

    def warn(text: str, wrapped: bool = False) -> None:
        warnings.append(text)
        lines.append(_wrap(f"warning: {text}", "          ", "          ") if wrapped else f"          warning: {text}")
    dmi = ctx.dmi
    device = f"{dmi.get('sys_vendor') or '?'} {dmi.get('product_name') or '?'}"
    bios = dmi.get("bios_version")
    if bios:
        device += f" (BIOS {bios})"
    if ctx.is_oxp3:
        device += " - supported"
    elif ctx.force:
        device += " - NOT a ONEXPLAYER 3 (--force)"
    else:
        device += " - NOT a ONEXPLAYER 3"
    lines.append(f"Device:   {device}")

    os_name = ctx.os_release.get("PRETTY_NAME") or ctx.os_release.get("NAME") or "unknown OS"
    image = f", image {ctx.image_name}" if ctx.image_name else ""
    if ctx.cachyos_handheld:
        image = ", Handheld Edition"
    unsupported = "" if ctx.platform else (
        " - NOT Bazzite or CachyOS (--force)" if ctx.force else " - NOT Bazzite or CachyOS")
    lines.append(f"OS:       {os_name}{image}, kernel {ctx.kernel_release or '?'}{unsupported}")
    boot = ctx.karg_backend.describe() if ctx.os_family == CACHYOS else ""
    if boot:
        lines.append(boot + (", Secure Boot on" if ctx.secure_boot else ""))
    finish = ctx.karg_backend.unfinished()
    if finish:
        warn(f"oxp3-tool's last boot-entry rebuild did not finish (or is still running): do not reboot before "
             f"`sudo {finish}` has ended without errors (this note stays until the next boot, also after that command "
             "succeeds)")
    backend = ctx.karg_backend
    taken = getattr(backend, "dropin_problem", lambda: None)()
    ids = [f.id for f in ALL_FIXES if (state.get(f.id) or {}).get("kargs") or
           (state.get(f.id) or {}).get("kargs_pending_add")]
    if taken and taken[0]:
        undo = (f"`sudo oxp3-tool revert {' '.join(ids)}` (it rebuilds the entries)" if ids else
                f"`sudo rm {backend.dropin}` and `sudo limine-mkinitcpio`")
        warn(f"{taken[1]}. Undo it now with {undo}, or put your own KERNEL_CMDLINE line back and run "
             "`sudo limine-mkinitcpio`, before rebooting or updating")
    elif taken:
        owned = set(ctx.owned_kargs) if not ids else set()
        adopt = ids or [f.id for f in ALL_FIXES if getattr(f, "karg", "") in owned]
        again = (f"`sudo oxp3-tool apply {' '.join(adopt)}` rewrites it without those lines" if adopt else
                 f"delete those lines from {backend.dropin} (that kernel is not installed, so nothing needs "
                 "rebuilding)")
        warn(f"{taken[1]}; {again}")
    rootless = getattr(backend, "entries_without_root", lambda: [])()
    if rootless:
        warn(f"the boot entries of {', '.join(rootless)} have no root=, so booting them fails: do not reboot before "
             + ("undoing the drop-in above" if taken and taken[0] else "`sudo limine-mkinitcpio` has rebuilt them"))

    for note in kernel_cmd.status_lines(ctx) if ctx.os_family == CACHYOS else []:
        (warnings if note.startswith(("warning:", "DO NOT REBOOT")) else notes).append(note)
        lines.append(_wrap(note, "          ", "          "))

    ip = ctx.inputplumber
    if ip.installed:
        ip_line = f"InputPlumber {ip.version or '(version unknown)'} ({ip.active or 'state unknown'})"
        if ip.oxp3_config:
            ip_line += f", OXP3 config {ip.oxp3_config}, map {ip.map_id}"
        else:
            ip_line += ", no OXP3 device config"
    else:
        ip_line = "InputPlumber not installed"
    lines.append(f"Input:    {ip_line}")
    hhd = ctx.hhd
    if hhd.installed:
        what = ", ".join(hhd.active_units) if hhd.running else "installed, not running"
        lines.append(f"          Handheld Daemon: {what}")
    for problem in ip.problems:
        warn(problem)
    if ctx.hid_oxp_devices:
        lines.append(f"          hid-oxp bound to {len(ctx.hid_oxp_devices)} MCU interface(s)")

    panel = ctx.internal_panel
    if not panel:
        p = "no eDP panel found"
    elif panel[1] is None:
        p = f"{panel[0]}: EDID unreadable"
    else:
        name, edid = panel
        p = f"{name}: {edid.vendor} 0x{edid.product:04x}" + (f" ({edid.name})" if edid.name else "")
    gpu = "xe loaded" if ctx.xe_loaded else "xe not loaded"
    if ctx.has_game_mode:
        gs = "gamescope Game Mode"
    else:
        gs = "gamescope installed, no Game Mode session" if ctx.gamescope_installed else "no gamescope"
    lines.append(f"Display:  {p}; {gpu}; {gs}")

    drives = ", ".join(
        f"{d.name} {d.model or '?'} ({(d.vendor or '?').replace('0x', '')}:{(d.device or '?').replace('0x', '')})"
        for d in ctx.nvme_drives
    ) or "no NVMe drives found"
    lines.append(f"Storage:  {drives}")
    lines.append(f"Gyro:     IMU driver {ctx.imu_driver or 'none'}; IIO devices: {', '.join(ctx.iio_names) or 'none'}")
    sleep = ctx.sys.read_attr("/sys/power/mem_sleep")
    if sleep:
        lines.append(f"Sleep:    mem_sleep {sleep}")
    for note in ctx.resume_evidence.lines():
        lines.append(f"          {note}")
    caps, why = ctx.edp_probe
    if caps is not None:
        lines.append(f"Panel:    {caps.describe()}")
    elif not ctx.sys.is_real_root or os.geteuid() == 0:
        lines.append(f"Panel:    not checked: {why}")
    refresh = ctx.refresh_state
    show_refresh = ctx.show_refresh
    if show_refresh:
        for i, line in enumerate(refresh.summary()):
            lines.append(f"{'Refresh:' if i == 0 else '':<10}{line}")
    game = {OGUI: "Steam behind the OpenGamepadUI overlay (Bazzite default; "
                  "`ujust configure-opengamepadui disable` switches to native Steam Input)",
            NATIVE_STEAM: "native Steam Input"}.get(ctx.game_mode_input or "")
    if game and ctx.os_family == CACHYOS:
        if ctx.hhd.running:
            pad = "Handheld Daemon's controller"
        elif not ip.installed:
            pad = "the kernel's plain Xbox 360 pad (install InputPlumber)"
        elif ip.active not in ("active", None):
            pad = ("the kernel's plain Xbox 360 pad (InputPlumber is not running: reboot after installing it, or "
                   "see `systemctl status inputplumber`)")
        elif not ip.oxp3_config:
            pad = "the kernel's plain Xbox 360 pad (InputPlumber has no OXP3 device config)"
        elif ctx.steamos_manager:
            pad = "the Steam Deck controller, which steamos-manager selects"
        else:
            pad = "InputPlumber's Xbox Elite controller (no steamos-manager)"
        game += f" (CachyOS gamescope-session; Steam gets {pad})"
    if game:
        lines.append(f"Game Mode: {game}")
    if ctx.power_key_unhandled:
        warn("the power button does nothing in Game Mode: logind ignores it (HandlePowerKey=ignore) and "
             "steamos-powerbuttond, which passes it to Steam, is not installed (CachyOS installs it only on handhelds "
             "it knows). `sudo pacman -S steamos-powerbuttond`, then reboot", wrapped=True)

    if state.load_error:
        warnings.append(state.load_error)
        lines.append("")
        lines.append(f"warning: {state.load_error}")
        lines.append("Which fixes are applied is unknown until that file is fixed or moved aside.")

    for group, title in ((CONTROLLER, "Controller fixes"), (DISPLAY, "Display and wake-from-sleep fixes")):
        lines.append("")
        lines.append(title)
        for fix in ALL_FIXES:
            if fix.group != group:
                continue
            label, fix_notes = _fix_state(ctx, state, fix)
            lines.append(f"  [{label:<12}] {fix.id:<21} {fix.summary}")
            for note in fix_notes:
                lines.append(f"  {'':14} {'':21} {note}")
    advice = refresh.advice() if show_refresh else []
    if advice:
        lines.append("")
        lines.append("Refresh rate")
        for item in advice:
            lines.append(_wrap(item, "  - ", "    "))
    lines.append("")
    if not (ctx.device_ok and ctx.os_ok):
        lines.append("This tool only changes anything on a ONEXPLAYER 3 running Bazzite or CachyOS.")
    else:
        lines.append("`sudo oxp3-tool apply` applies the recommended fixes; `oxp3-tool list` explains each one.")
    return lines, warnings, notes


def status_data(ctx: Context, state: State) -> dict[str, Any]:
    """`status --json`: what status shows, as data."""
    lines, warnings, notes = _status(ctx, state)
    dmi = ctx.dmi
    fixes = []
    for fix in ALL_FIXES:
        label, fix_notes = _fix_state(ctx, state, fix)
        entry = state.get(fix.id) or {}
        fixes.append({"id": fix.id, "group": fix.group, "level": fix.level, "title": fix.title,
                      "summary": fix.summary, "takes_effect": fix.takes_effect, "state": label, "notes": fix_notes,
                      "applied_at": entry.get("applied_at"), "options": entry.get("options", {}),
                      "pending": _pending(ctx, fix, entry, label)})
    refresh = ctx.refresh_state if ctx.show_refresh else None
    return {
        "device": {"vendor": dmi.get("sys_vendor"), "product": dmi.get("product_name"),
                   "bios": dmi.get("bios_version"), "supported": ctx.is_oxp3},
        "os": {"name": ctx.os_release.get("PRETTY_NAME") or ctx.os_release.get("NAME"), "platform": ctx.platform,
               "handheld": ctx.cachyos_handheld, "image": ctx.image_name, "kernel": ctx.kernel_release},
        "boot": ctx.karg_backend.name, "secure_boot": ctx.secure_boot,
        "can_change": ctx.device_ok and ctx.os_ok,
        "needs_root": ctx.sys.is_real_root and os.geteuid() != 0,
        "game_mode": ctx.has_game_mode, "game_mode_input": ctx.game_mode_input,
        "warnings": warnings, "notes": notes, "fixes": fixes,
        "reboot_pending": any(f["pending"] == "reboot" for f in fixes),
        "rebuild_unfinished": ctx.karg_backend.unfinished(),
        "refresh": {"summary": refresh.summary(), "advice": refresh.advice()} if refresh else None,
        "text": "\n".join(lines),
    }


def _pending(ctx: Context, fix: Fix, entry: dict, label: str) -> Optional[str]:
    """What a fix still waits for: "reboot" (a kernel argument added or removed for the next boot),
    "inputplumber-restart" (deferred with --no-restart), or None."""
    if label == "reverting":
        return "reboot"
    karg = getattr(fix, "karg", "")
    if karg and entry and (karg in entry.get("kargs_pending_add", []) or
                           (ctx.karg_pending(karg) and not ctx.karg_active(karg))):
        return "reboot"
    if entry and restart_pending(ctx, entry.get("notes", {})):
        return "inputplumber-restart"
    return None


def catalog_data() -> dict[str, Any]:
    """`list --json`: every fix and the button actions."""
    return {"fixes": [{"id": f.id, "group": f.group, "level": f.level, "title": f.title, "summary": f.summary,
                       "details": f.details, "takes_effect": f.takes_effect} for f in ALL_FIXES],
            "button_actions": {name: desc for name, (_, _, desc) in templates.BUTTON_ACTIONS.items()},
            "button_keys": {key: {"label": label, "default": templates.DEFAULT_BUTTON_ACTIONS[key],
                                  "stock": templates.STOCK_BUTTON_ACTIONS[key],
                                  "flag": {"onex": "--onex-key", "keyboard": "--keyboard-key", "home": "--home",
                                           "xbox": "--xbox-key"}[key]}
                            for key, label in templates.BUTTON_KEYS.items()},
            "fps_limit": {"min": steam_session.MIN_FPS_LIMIT, "max": steam_session.MAX_FPS_LIMIT,
                          "default": steam_session.DEFAULT_FPS_LIMIT},
            "min_refresh": {"min": templates.PANEL_MIN_HZ, "max": templates.LEGIONGO2_MIN_HZ,
                            "default": templates.LEGIONGO2_MIN_HZ}}


def cmd_status(ctx: Context, state: State, args: argparse.Namespace) -> int:
    if getattr(args, "json", False):
        _RESULT["status"] = status_data(ctx, state)
        return 0
    _out(status_text(ctx, state))
    return 0


def cmd_list(ctx: Context, state: State, args: argparse.Namespace) -> int:
    if getattr(args, "json", False):
        _RESULT["catalog"] = catalog_data()
        return 0
    for fix in ALL_FIXES:
        _out(f"{fix.id}  [{fix.group}, {fix.level}]")
        _out(f"  {fix.title}")
        for line in fix.details.splitlines():
            _out(f"    {line}")
        _out(f"  Takes effect: {fix.takes_effect}")
        _out()
    _out("Button actions for --onex-key, --keyboard-key, --home and --xbox-key:")
    for name, (_, _, desc) in templates.BUTTON_ACTIONS.items():
        _out(f"  {name:<15} {desc}")
    return 0


# -- apply / revert ----------------------------------------------------------------


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if _LOG is not None or not sys.stdin.isatty():
        _err("Not asking for confirmation on a non-interactive input; pass --yes to proceed.")
        return False
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _need_root(system: System) -> Optional[int]:
    if system.is_real_root and os.geteuid() != 0:
        _err("This needs root. Run it with sudo.")
        return 1
    return None


def _report(outcome: Outcome) -> int:
    _RESULT["outcome"] = dataclasses.asdict(outcome)
    for msg in outcome.messages:
        _out(f"note: {msg}")
    for err in outcome.errors:
        _err(f"error: {err}")
    if outcome.reboot_needed:
        _out("Reboot to finish (kernel arguments change at boot).")
    if outcome.ok:
        _out("Done." if outcome.changed else "Nothing to change.")
        return 0
    return 1


def _select_fixes(ids: Sequence[str]) -> list[Fix]:
    unknown = [i for i in ids if i not in BY_ID]
    if unknown:
        raise EngineError(f"unknown fix: {', '.join(unknown)} (see `oxp3-tool list`)")
    seen = []
    for i in ids:
        if BY_ID[i] not in seen:
            seen.append(BY_ID[i])
    return seen


def cmd_apply(ctx: Context, state: State, args: argparse.Namespace) -> int:
    if args.fixes:
        fixes = _select_fixes(args.fixes)
    else:
        fixes = [f for f in ALL_FIXES if f.check(ctx).applicable and f.check(ctx).recommended]
        if not fixes:
            _out("No recommended fixes apply to this machine. See `oxp3-tool status`.")
            return 0
    opts = {
        "home": args.home,
        "keyboard_key": args.keyboard_key,
        "onex_key": args.onex_key,
        "xbox_key": args.xbox_key,
        "fps_limit": args.fps_limit,
        "min_refresh": args.min_refresh,
        "any_ssd": args.any_ssd,
        "restart": not args.no_restart,
        "defaults": args.defaults,
    }
    engine = Engine(ctx, state, out=_out)
    plans = engine.plan(fixes, opts)
    _RESULT["plan"] = engine.describe(plans)
    _out("Planned changes:")
    for line in _RESULT["plan"]:
        _out(f"  {line}")
    if args.dry_run:
        _out("(dry run: nothing changed)")
        return 0
    rc = _need_root(ctx.sys)
    if rc is not None:
        return rc
    if not any(engine.changes(fix, plan) for fix, plan in plans):
        _out("Nothing to change.")
        return 0
    if not _confirm("Apply these changes?", args.yes):
        _out("Cancelled.")
        return 1
    _out("Applying:")
    return _report(engine.apply(plans))


def _revert_settled(ctx: Context, entry: Optional[dict]) -> bool:
    """A fix whose revert has done everything it can; only the reboot is left."""
    if not entry or not entry.get("reverted") or ctx.karg_backend.unfinished():
        return False
    if any(entry.get(k) for k in ("files", "dirs_created", "kargs", "kargs_pending_add", "units_enabled")):
        return False
    if ctx.kargs_left is None and ctx.karg_backend.name != "rpm-ostree":
        return False  # the boot entries could not be read
    return not any(ctx.karg_left(k) for k in entry.get("kargs_removal_pending", []))


def cmd_revert(ctx: Context, state: State, args: argparse.Namespace) -> int:
    if args.all:
        ids = [f.id for f in ALL_FIXES if state.get(f.id)] + [i for i in state.fixes if i not in BY_ID]
    elif args.fixes:
        _select_fixes([i for i in args.fixes if i not in state.fixes])
        ids = list(dict.fromkeys(args.fixes))
    else:
        _err("Name the fixes to revert, or pass --all.")
        return 2
    if not ids:
        _out("Nothing applied by this tool.")
        return 0
    settled = [i for i in ids if _revert_settled(ctx, state.get(i))]
    if settled and len(settled) == len(ids):
        for fix_id in settled:
            _out(f"  {fix_id}: already reverted")
        _out(f"Nothing left to revert {ctx.reboot_advice('finish')}.")
        return 0
    ids = [i for i in ids if i not in settled]
    for fix_id in settled:
        _out(f"  {fix_id}: already reverted {ctx.reboot_advice('finish')}")
    _out("Will revert:")
    for fix_id in ids:
        entry = state.get(fix_id)
        if not entry:
            _out(f"  {fix_id}: not applied")
            continue
        _out(f"  {fix_id}:")
        for rec in entry.get("files", []):
            verb = "restore" if rec.get("backup") else "remove"
            _out(f"    {verb} {rec['path']}")
        for unit in entry.get("units_enabled", []):
            _out(f"    disable {unit}")
        kargs = entry.get("kargs", []) + [k for k in entry.get("kargs_removal_pending", []) if ctx.karg_left(k)]
        for karg in kargs:
            _out(f"    remove kernel argument {karg} (reboot needed)")
        if (kargs or entry.get("kargs_removal_pending")) and ctx.karg_backend.unfinished():
            _out("    rebuild the boot entries again (the last rebuild did not finish)")
    if args.dry_run:
        _out("(dry run: nothing changed)")
        return 0
    rc = _need_root(ctx.sys)
    if rc is not None:
        return rc
    if not _confirm("Revert these changes?", args.yes):
        _out("Cancelled.")
        return 1
    engine = Engine(ctx, state, out=_out)
    return _report(engine.revert(ids))


# -- diagnose ----------------------------------------------------------------------


def cmd_diagnose(ctx: Context, state: State, args: argparse.Namespace) -> int:
    if ctx.sys.is_real_root and os.geteuid() != 0:
        _out("Not running as root: kernel debug files and some logs will be missing (use sudo for a full report).")
    bundle, findings = diagnose.collect(ctx, status_text(ctx, state))
    path = args.output or diagnose.default_output_name()
    owner = None
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid and gid and os.geteuid() == 0:
        owner = (int(uid), int(gid))
    try:
        diagnose.write_bundle(bundle, path, owner)
    except FileExistsError:
        _err(f"error: {path} already exists; choose another name with --output")
        return 1
    except OSError as e:
        _err(f"error: cannot write {path}: {e.strerror or e}")
        return 1
    _out(f"Wrote {path}")
    _RESULT["diagnose"] = {"path": os.path.abspath(path), "findings": findings}
    if findings:
        _out("Known problem patterns in the kernel logs (this boot and the previous one):")
        for line in findings:
            _out(f"  {line}")
    else:
        _out("No known problem patterns in the kernel logs.")
    _out("Serial numbers and MAC addresses were replaced, but the archive still contains system logs "
         "(hostnames, Wi-Fi network names); look through it before sharing it publicly.")
    return 0


def cmd_kernel(ctx: Context, state: State, args: argparse.Namespace) -> int:
    action = args.kernel_action
    try:
        if action is None and getattr(args, "json", False):
            _RESULT["kernel"] = kernel_cmd.describe_data(ctx)
        rc = kernel_cmd.cmd_kernel(ctx, action, getattr(args, "package", None), args.yes, args.dry_run, _out,
                                   _confirm)
        if action and getattr(args, "json", False):
            _RESULT["action"] = action
            try:
                _RESULT["kernel"] = kernel_cmd.describe_data(ctx)  # after the change
            except kernel_cmd.KernelError:
                pass
        return rc
    except kernel_cmd.KernelError as e:
        _err(f"error: {e}")
        return 1


# -- main ----------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    """With --json anywhere in the arguments, a usage error is one JSON object too (exit 2)."""

    def error(self, message: str) -> Any:
        if "--json" in sys.argv[1:] or _JSON_ARGV:
            print(json.dumps({"schema": 1, "version": __version__, "command": None, "rc": 2, "ok": False,
                              "log": [{"stream": "err", "text": f"error: {message}"}]}), flush=True)
            raise SystemExit(2)
        super().error(message)


_JSON_ARGV = False


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(
        prog="oxp3-tool",
        description="Controller and wake-from-sleep fixes for the OneXPlayer 3 on Bazzite and CachyOS.",
        epilog=f"Fix ids: {', '.join(f.id for f in ALL_FIXES)}. Project: {PROJECT_URL}",
    )
    p.add_argument("--version", action="version", version=f"oxp3-tool {__version__}")
    p.add_argument("--force", action="store_true",
                   help="skip the ONEXPLAYER 3 / operating system checks (development only)")
    p.add_argument("--root", default="/", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command")
    jp = argparse.ArgumentParser(add_help=False)
    jp.add_argument("--json", action="store_true",
                    help="print one JSON object (the result, and every line it would print) instead of text")

    sub.add_parser("status", parents=[jp], help="show the device, the software stack and each fix's state (default)")
    sub.add_parser("list", parents=[jp], help="explain every fix")

    a = sub.add_parser("apply", parents=[jp], help="apply the recommended fixes, or the ones named")
    a.add_argument("fixes", nargs="*", metavar="FIX")
    a.add_argument("-n", "--dry-run", action="store_true", help="show what would change")
    a.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    actions = list(templates.BUTTON_ACTIONS)
    a.add_argument("--home", choices=actions, help="buttons: action for the home key (default guide)")
    a.add_argument("--keyboard-key", choices=actions,
                   help="buttons: action for the keyboard key (default keyboard)")
    a.add_argument("--onex-key", choices=actions, help="buttons: action for the ONEX key (default quick-access)")
    a.add_argument("--xbox-key", choices=actions,
                   help="buttons: action for the Xbox key, also for the xbox-key helper (default guide)")
    a.add_argument("--fps-limit", type=int, choices=range(steam_session.MIN_FPS_LIMIT, steam_session.MAX_FPS_LIMIT + 1),
                   metavar=f"{{{steam_session.MIN_FPS_LIMIT}..{steam_session.MAX_FPS_LIMIT}}}",
                   help=f"buttons: frame limit the fps-limit action sets (default {steam_session.DEFAULT_FPS_LIMIT})")
    a.add_argument("--no-restart", action="store_true", help="buttons: do not restart InputPlumber")
    a.add_argument("--min-refresh", type=int, choices=range(templates.PANEL_MIN_HZ, templates.LEGIONGO2_MIN_HZ + 1),
                   metavar=f"{{{templates.PANEL_MIN_HZ}..{templates.LEGIONGO2_MIN_HZ}}}",
                   help="oled-profile: lowest refresh rate offered (default 48; below 48 is untested)")
    a.add_argument("--any-ssd", action="store_true", help="nvme-resume: apply even without a Predator GM7")
    a.add_argument("--defaults", action="store_true",
                   help="ignore the options remembered from the last apply (button actions, --min-refresh)")

    r = sub.add_parser("revert", parents=[jp], help="undo fixes applied by this tool")
    r.add_argument("fixes", nargs="*", metavar="FIX")
    r.add_argument("--all", action="store_true", help="revert everything this tool applied")
    r.add_argument("-n", "--dry-run", action="store_true", help="show what would change")
    r.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")

    d = sub.add_parser("diagnose", parents=[jp], help="write a diagnostic archive for bug reports")
    d.add_argument("-o", "--output", help="archive path (default: oxp3-diagnose-<time>.tar.gz)")

    k = sub.add_parser("kernel", parents=[jp], help="CachyOS with Limine: list the kernels, boot another one once safely, keep it",
                       description="Without an action: the installed kernels and what Limine boots next. `try "
                                   "PACKAGE` boots PACKAGE once; a power-on after that (also after a hang) boots "
                                   "Limine's default entry. `keep` keeps booting the kernel running now.")
    k.set_defaults(kernel_action=None, dry_run=False, yes=False)
    kinds = k.add_subparsers(dest="kernel_action")
    t = kinds.add_parser("try", parents=[jp], help="boot an installed kernel once (no keyboard needed to get back)")
    t.add_argument("package", help="the kernel's package, e.g. linux-cachyos-deckify-vsw")
    keep = kinds.add_parser("keep", parents=[jp], help="keep booting the kernel running now (undoes what try changed)")
    for kp in (t, keep):
        kp.add_argument("-n", "--dry-run", action="store_true", help="show what would change")
        kp.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    return p


COMMANDS = {
    None: cmd_status,
    "status": cmd_status,
    "list": cmd_list,
    "apply": cmd_apply,
    "revert": cmd_revert,
    "diagnose": cmd_diagnose,
    "kernel": cmd_kernel,
}


MUTATING = ("apply", "revert")
LOCK_FILE = "/run/oxp3-tool.lock"


def acquire_lock(system: System) -> Optional[int]:
    """Non-blocking exclusive lock; None if another run holds it. Released at exit."""
    path = system.path(LOCK_FILE)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def main(argv: Optional[Sequence[str]] = None, system: Optional[System] = None) -> int:
    global _LOG, _RESULT, _JSON_ARGV
    _JSON_ARGV = argv is not None and "--json" in argv
    args = build_parser().parse_args(argv)
    if not getattr(args, "json", False):
        return _main(args, system)
    _LOG, _RESULT = [], {}
    rc: Optional[int] = None
    try:
        rc = _main(args, system)
    except Exception as e:  # still one JSON object for the caller
        _err(f"error: oxp3-tool crashed: {type(e).__name__}: {e}")
        rc = 1
    finally:
        log, result = _LOG, _RESULT
        _LOG, _RESULT = None, {}
        print(json.dumps({"schema": 1, "version": __version__, "command": args.command or "status", "rc": rc,
                          "ok": rc == 0, **result, "log": log}, indent=None), flush=True)
    return rc if rc is not None else 1


def _main(args: argparse.Namespace, system: Optional[System]) -> int:
    if system is None:
        # Simulate commands only for a fake root; any spelling of / is the real system.
        if Path(args.root).resolve() == Path("/"):
            system = System("/")
        else:
            system = System(args.root, SimulatedRunner(args.root))
    ctx = Context(system, force=args.force)
    lock = None
    try:
        mutating = (args.command in MUTATING or getattr(args, "kernel_action", None)) \
            and not getattr(args, "dry_run", False)
        if mutating and _need_root(system) is not None:
            return 1
        if not system.is_real_root or os.geteuid() == 0:
            lock = acquire_lock(system)
        if mutating and lock is None:
            raise EngineError("another oxp3-tool run is changing the system; try again when it is done")
        state = State(system, tolerant=args.command not in MUTATING)
        # Persist settled kernel-argument bookkeeping whenever this run may write.
        if reconcile(ctx, state) and lock is not None and not state.load_error \
                and not getattr(args, "dry_run", False):
            state.save()
        return COMMANDS[args.command](ctx, state, args)
    except (EngineError, StateError) as e:
        _err(f"error: {e}")
        return 1
    except KeyboardInterrupt:
        _err("Interrupted.")
        try:
            finish = (system.read_text(REBUILD_MARKER) or "").strip()
        except Exception:
            finish = ""
        if finish:
            _err(f"A boot-entry rebuild did not finish: do not reboot before `sudo {finish}` has ended without "
                 "errors.")
        return 130
    finally:
        if lock is not None:
            os.close(lock)
