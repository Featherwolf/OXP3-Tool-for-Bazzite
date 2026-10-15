"""Command-line interface."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional, Sequence

from . import PROJECT_URL, __version__, diagnose, templates
from .detect import Context
from .engine import Engine, EngineError, Outcome, file_statuses
from .fixes import ALL_FIXES, BY_ID, CONTROLLER, DISPLAY, Fix
from .state import State, StateError
from .system import SimulatedRunner, System


def _out(line: str = "") -> None:
    print(line, flush=True)


def _err(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


# -- status ----------------------------------------------------------------------


def _fix_state(ctx: Context, state: State, fix: Fix) -> tuple[str, list[str]]:
    entry = state.get(fix.id)
    if entry:
        problems = []
        for fs in file_statuses(ctx, entry):
            if not fs.present:
                problems.append(f"{fs.path} is missing")
            elif not fs.matches:
                problems.append(f"{fs.path} was changed by something else")
        label = "applied" if not problems else "applied, damaged"
        return label, problems + fix.live_state(ctx, entry)
    check = fix.check(ctx)
    if not check.applicable:
        return "n/a", [check.reason]
    label = "recommended" if check.recommended else fix.level
    return label, ([check.reason] if check.reason else [])


def status_text(ctx: Context, state: State) -> str:
    lines = [f"oxp3-tool {__version__}"]
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
    lines.append(f"OS:       {os_name}{image}, kernel {ctx.kernel_release or '?'}")

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
        lines.append(f"          warning: {problem}")
    if ctx.hid_oxp_devices:
        lines.append(f"          hid-oxp bound to {len(ctx.hid_oxp_devices)} MCU interface(s)")

    panel = ctx.internal_panel
    if panel:
        name, edid = panel
        p = f"{name}: {edid.vendor} 0x{edid.product:04x}" + (f" ({edid.name})" if edid and edid.name else "") \
            if edid else f"{name}: EDID unreadable"
    else:
        p = "no eDP panel found"
    gpu = "xe loaded" if ctx.xe_loaded else "xe not loaded"
    gs = "gamescope installed" if ctx.gamescope_installed else "no gamescope"
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

    for group, title in ((CONTROLLER, "Controller fixes"), (DISPLAY, "Display and wake-from-sleep fixes")):
        lines.append("")
        lines.append(title)
        for fix in ALL_FIXES:
            if fix.group != group:
                continue
            label, notes = _fix_state(ctx, state, fix)
            lines.append(f"  [{label:<12}] {fix.id:<21} {fix.summary}")
            for note in notes:
                lines.append(f"  {'':14} {'':21} {note}")
    lines.append("")
    if not ctx.device_ok:
        lines.append("This tool only changes anything on a ONEXPLAYER 3 running Bazzite.")
    else:
        lines.append("`sudo oxp3-tool apply` applies the recommended fixes; `oxp3-tool list` explains each one.")
    return "\n".join(lines)


def cmd_status(ctx: Context, state: State, args: argparse.Namespace) -> int:
    _out(status_text(ctx, state))
    return 0


def cmd_list(ctx: Context, state: State, args: argparse.Namespace) -> int:
    for fix in ALL_FIXES:
        _out(f"{fix.id}  [{fix.group}, {fix.level}]")
        _out(f"  {fix.title}")
        for line in fix.details.splitlines():
            _out(f"    {line}")
        _out(f"  Takes effect: {fix.takes_effect}")
        _out()
    _out("Button actions for --onex-key, --keyboard-key and --home:")
    for name, (_, desc) in templates.BUTTON_ACTIONS.items():
        _out(f"  {name:<13} {desc}")
    return 0


# -- apply / revert ----------------------------------------------------------------


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
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
        "min_refresh": args.min_refresh,
        "any_ssd": args.any_ssd,
        "restart": not args.no_restart,
    }
    engine = Engine(ctx, state, out=_out)
    plans = engine.plan(fixes, opts)
    _out("Planned changes:")
    for line in engine.describe(plans):
        _out(f"  {line}")
    if args.dry_run:
        _out("(dry run: nothing changed)")
        return 0
    rc = _need_root(ctx.sys)
    if rc is not None:
        return rc
    if not _confirm("Apply these changes?", args.yes):
        _out("Cancelled.")
        return 1
    _out("Applying:")
    return _report(engine.apply(plans))


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
        for karg in entry.get("kargs", []):
            _out(f"    remove kernel argument {karg} (reboot needed)")
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

    def chown(p: str) -> None:
        uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
        if uid and gid and os.geteuid() == 0:
            os.chown(p, int(uid), int(gid))

    diagnose.write_bundle(bundle, path, chown)
    _out(f"Wrote {path}")
    if findings:
        _out("Known problem patterns in the kernel logs (this boot and the previous one):")
        for line in findings:
            _out(f"  {line}")
    else:
        _out("No known problem patterns in the kernel logs.")
    _out("The archive contains system logs; look through it before sharing it publicly.")
    return 0


# -- main ----------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="oxp3-tool",
        description="Controller and wake-from-sleep fixes for the OneXPlayer 3 on Bazzite.",
        epilog=f"Fix ids: {', '.join(f.id for f in ALL_FIXES)}. Project: {PROJECT_URL}",
    )
    p.add_argument("--version", action="version", version=f"oxp3-tool {__version__}")
    p.add_argument("--force", action="store_true",
                   help="skip the ONEXPLAYER 3 / Bazzite checks (development only)")
    p.add_argument("--root", default="/", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command")

    sub.add_parser("status", help="show the device, the software stack and each fix's state (default)")
    sub.add_parser("list", help="explain every fix")

    a = sub.add_parser("apply", help="apply the recommended fixes, or the ones named")
    a.add_argument("fixes", nargs="*", metavar="FIX")
    a.add_argument("-n", "--dry-run", action="store_true", help="show what would change")
    a.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    actions = list(templates.BUTTON_ACTIONS)
    a.add_argument("--home", choices=actions, help="buttons: action for the home key (default guide)")
    a.add_argument("--keyboard-key", choices=actions,
                   help="buttons: action for the keyboard key (default keyboard)")
    a.add_argument("--onex-key", choices=actions, help="buttons: action for the ONEX key (default quick-access)")
    a.add_argument("--no-restart", action="store_true", help="buttons: do not restart InputPlumber")
    a.add_argument("--min-refresh", type=int, choices=range(templates.PANEL_MIN_HZ, templates.LEGIONGO2_MIN_HZ + 1),
                   metavar=f"{{{templates.PANEL_MIN_HZ}..{templates.LEGIONGO2_MIN_HZ}}}",
                   help="oled-profile: lowest refresh rate offered (default 48; below 48 is untested)")
    a.add_argument("--any-ssd", action="store_true", help="nvme-resume: apply even without a Predator GM7")

    r = sub.add_parser("revert", help="undo fixes applied by this tool")
    r.add_argument("fixes", nargs="*", metavar="FIX")
    r.add_argument("--all", action="store_true", help="revert everything this tool applied")
    r.add_argument("-n", "--dry-run", action="store_true", help="show what would change")
    r.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")

    d = sub.add_parser("diagnose", help="write a diagnostic archive for bug reports")
    d.add_argument("-o", "--output", help="archive path (default: oxp3-diagnose-<time>.tar.gz)")
    return p


COMMANDS = {
    None: cmd_status,
    "status": cmd_status,
    "list": cmd_list,
    "apply": cmd_apply,
    "revert": cmd_revert,
    "diagnose": cmd_diagnose,
}


def main(argv: Optional[Sequence[str]] = None, system: Optional[System] = None) -> int:
    args = build_parser().parse_args(argv)
    if system is None:
        runner = None if args.root == "/" else SimulatedRunner(args.root)
        system = System(args.root, runner)
    ctx = Context(system, force=args.force)
    try:
        state = State(system)
        return COMMANDS[args.command](ctx, state, args)
    except (EngineError, StateError) as e:
        _err(f"error: {e}")
        return 1
    except KeyboardInterrupt:
        _err("Interrupted.")
        return 130
