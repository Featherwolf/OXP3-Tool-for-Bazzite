# Adapted from ClawDeck (MIT, Copyright (c) 2026 IHulkSmashI and ClawDeck contributors).
"""OXP3Deck: the Decky Loader backend. Thin: each method is one call into py_modules/oxp3deck, run in an executor
thread, and answers {ok, error, ...} (DESIGN.md "Backend API"). Runs as root (plugin.json flag "root").

Decky starts _main as a task nobody awaits and serves calls at the same time, so everything a call needs is set up
in __init__; when that fails, every call answers {ok: false, error} instead of raising (Decky drops the message of
a Python exception on its way to the frontend).
"""
import asyncio
import functools
import importlib
import json
import logging
import os
import re
import sys
import traceback

PLUGIN_DIR = os.path.dirname(os.path.realpath(__file__))
_PY_MODULES = os.path.join(PLUGIN_DIR, "py_modules")
if sys.path[:1] != [_PY_MODULES]:
    sys.path.insert(0, _PY_MODULES)

from oxp3deck import env  # noqa: E402

# Decky is a PyInstaller bundle: its LD_LIBRARY_PATH (and friends) would reach every program the plugin starts.
env.scrub_process_env()

import decky  # noqa: E402

from oxp3deck import context, jobs, sysexec  # noqa: E402

log = logging.getLogger("oxp3deck")
_BROKEN: dict = {}     # module name -> why it did not import


def _load(name: str):
    """A feature module; one that fails to import turns only its own calls into errors."""
    try:
        return importlib.import_module("oxp3deck." + name)
    except Exception as e:
        _BROKEN[name] = f"{type(e).__name__}: {e}"
        log.error("cannot load oxp3deck.%s:\n%s", name, traceback.format_exc())
        return None


oxp3tool = _load("oxp3tool")
xess = _load("xess")
games = _load("games")

OXP3_VENDOR = "ONE-NETBOOK"          # oxp3_tool/detect.py OXP3_SYS_VENDOR / OXP3_PRODUCT_NAME
OXP3_PRODUCT = "ONEXPLAYER 3"
SETTING_KEYS = {"xess_driver": (str, type(None)), "xess_shim_system": (bool,)}
UI_KEY = re.compile(r"^ui_[A-Za-z0-9_]{1,60}$")
MAX_SETTING_BYTES = 4096
MAX_UI_KEYS = 64
REBOOT_KEY = "oxp3_reboot_pending"   # settings.json: {boot_id, value} from the last oxp3-tool status
NO_OXP3 = {"installed": False, "path": None, "version": None, "json_ok": False, "min_version": "0.4.0",
           "bundled_version": None}


class Unavailable(Exception):
    """A call that cannot run: its message is the whole answer."""


def _mod(name: str):
    mod = globals().get(name)
    if mod is None:
        raise Unavailable(f"the {name} module did not load: {_BROKEN.get(name, 'unknown error')}")
    return mod


def _answer(name: str, res) -> dict:
    if not isinstance(res, dict):
        return {"ok": False, "error": f"{name}: returned {type(res).__name__}, not a dict"}
    res = json.loads(json.dumps(res, default=str))   # what Decky can send
    res["ok"] = bool(res.get("ok"))
    res.setdefault("error", None)
    return res


def _read_text(path: str, limit: int = 65536) -> str:
    try:
        with open(path, "rb") as f:
            return f.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _platform(ctx):
    """"cachyos" or "bazzite" from os-release (ID, then ID_LIKE; CachyOS repositories on an Arch ID count, as in
    oxp3_tool/detect.py), else None."""
    text = _read_text(ctx.sys_path("/etc/os-release")) or _read_text(ctx.sys_path("/usr/lib/os-release"))
    rel = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep and k.strip() and not k.startswith("#"):
            rel[k.strip()] = v.strip().strip("\"'")
    ids = [rel.get("ID", "")] + rel.get("ID_LIKE", "").split()
    for name in ("cachyos", "bazzite"):
        if name in ids:
            return name
    if rel.get("ID") == "arch" and re.search(r"^\[cachyos", _read_text(ctx.sys_path("/etc/pacman.conf")), re.M):
        return "cachyos"
    return None


def _device(ctx) -> dict:
    def dmi(field):
        return _read_text(ctx.sys_path(f"/sys/class/dmi/id/{field}"), 256).strip() or None
    vendor, product = dmi("sys_vendor"), dmi("product_name")
    return {"vendor": vendor, "product": product, "supported": vendor == OXP3_VENDOR and product == OXP3_PRODUCT}


def _title(verb: str, names) -> str:
    names = [str(n) for n in names] if isinstance(names, (list, tuple)) else []
    if not names:
        return verb
    return f"{verb} {', '.join(names[:4])}{' and more' if len(names) > 4 else ''}"


class Plugin:
    def __init__(self):
        self.ctx = None
        self.settings = None
        self.jobs = None
        self._broken = None
        self._boot = None
        try:
            self.ctx = context.from_decky()
            self.settings = context.Settings(os.path.join(self.ctx.settings_dir, "settings.json"))
            self.jobs = jobs.Jobs(self.ctx.runtime_dir)
            log.info("OXP3Deck %s for %s (%s)", getattr(decky, "DECKY_PLUGIN_VERSION", "?"), self.ctx.user,
                     self.ctx.home)
        except Exception as e:
            self._broken = f"{type(e).__name__}: {e}"
            log.error("OXP3Deck cannot start:\n%s", traceback.format_exc())

    # ---- plumbing ------------------------------------------------------------------------------------------------

    def _check(self) -> None:
        if self._broken:
            raise Unavailable(f"the plugin could not start: {self._broken}")

    async def _call(self, name: str, fn, *args) -> dict:
        """fn(*args) in the default executor; whatever happens, a dict with ok and error."""
        try:
            self._check()
            res = await asyncio.get_running_loop().run_in_executor(None, functools.partial(fn, *args))
            return _answer(name, res)
        except Unavailable as e:
            return {"ok": False, "error": f"{name}: {e}"}
        except Exception as e:
            log.error("%s failed:\n%s", name, traceback.format_exc())
            return {"ok": False, "error": f"{name}: {type(e).__name__}: {e}"}

    async def _start(self, name: str, kind: str, title: str, work, precheck=None) -> dict:
        """Start work(job) as a job: {ok, error, job}. precheck() -> why not (or None) runs first, so a bad
        argument is refused before anything starts."""
        def go():
            running = self.jobs.running()
            if running is not None:
                return {"ok": False, "error": f"another task is running: {running.title}", "job": None}
            why = precheck() if precheck is not None else None
            if why:
                return {"ok": False, "error": why, "job": None}
            job, why = self.jobs.start(kind, title, work)
            return {"ok": job is not None, "error": why, "job": job.to_dict() if job else None}

        res = await self._call(name, go)
        res.setdefault("job", None)
        return res

    def _not_during_xess_job(self, what: str) -> None:
        running = self.jobs.running()
        if running is not None and running.kind.startswith("xess"):
            raise Unavailable(f"{running.title} is running; {what} when it has ended")

    def _boot_id(self):
        if self._boot is None:
            self._boot = _read_text(self.ctx.sys_path("/proc/sys/kernel/random/boot_id"), 100).strip()
        return self._boot

    def _note_reboot(self, value: bool) -> None:
        mark = {"boot_id": self._boot_id(), "value": bool(value)}
        if self.settings.get(REBOOT_KEY) != mark:
            self.settings.set(REBOOT_KEY, mark)

    def _reboot_pending(self) -> bool:
        mark = self.settings.get(REBOOT_KEY)
        return isinstance(mark, dict) and mark.get("value") is True and mark.get("boot_id") == self._boot_id()

    # ---- lifecycle -----------------------------------------------------------------------------------------------

    async def _main(self):
        """A pacman update of proton-cachyos puts the stock DLL back: shim the system Protons again when needed."""
        try:
            self._check()
            await asyncio.get_running_loop().run_in_executor(None, self._reshim)
        except Exception:
            log.error("start-up check failed (carrying on):\n%s", traceback.format_exc())

    def _reshim(self) -> None:
        x = _mod("xess")
        if not x.system_protons_need_shim(self.ctx, self.settings):
            return
        job, why = self.jobs.start("xess-reshim", "Shim the system Protons again",
                                   lambda job: x.shim_system_protons(self.ctx, self.settings, job))
        log.info("system Protons lost the shim: %s", f"job {job.id}" if job else why)

    async def _unload(self):
        # Decky kills the process 5 s after asking it to stop; a running job's program keeps running.
        log.info("OXP3Deck unloading")

    async def _uninstall(self):
        # Decky runs this on every update of the plugin too: nothing is reverted or removed here.
        log.info("OXP3Deck removed (or updated): oxp3-tool, its fixes and xess-xmx-linux stay installed")

    # ---- overview ------------------------------------------------------------------------------------------------

    async def get_overview(self):
        return await self._call("get_overview", self._overview)

    def _overview(self) -> dict:
        ctx = self.ctx

        def part(what, fn, default):
            try:
                return fn()
            except Exception as e:
                log.error("overview: %s:\n%s", what, traceback.format_exc())
                return default(e) if callable(default) else default

        job = self.jobs.running()
        return {
            "ok": True, "error": None,
            "plugin_version": str(getattr(decky, "DECKY_PLUGIN_VERSION", "") or ""),
            "user": ctx.user, "home": ctx.home,
            "platform": part("platform", lambda: _platform(ctx), None),
            "device": part("device", lambda: _device(ctx), {"vendor": None, "product": None, "supported": False}),
            "oxp3": part("oxp3-tool", lambda: _mod("oxp3tool").installed(ctx), dict(NO_OXP3)),
            "xess": part("xess", lambda: _mod("xess").summary(ctx, self.settings),
                         lambda e: {"installed": False, "version": None, "status": "unknown",
                                    "line": f"XeSS state unknown: {e}", "live": False, "relogin_needed": False}),
            "job": job.to_dict(tail=20) if job else None,
            "reboot_pending": part("reboot", self._reboot_pending, False),
            "do_not_reboot": part("rebuild marker", lambda: _mod("oxp3tool").rebuild_unfinished(ctx), None),
        }

    # ---- oxp3-tool -----------------------------------------------------------------------------------------------

    async def oxp3_status(self):
        return await self._call("oxp3_status", self._oxp3_status)

    def _oxp3_status(self) -> dict:
        res = _mod("oxp3tool").status(self.ctx)
        if isinstance(res, dict) and res.get("ok") and isinstance(res.get("status"), dict):
            self._note_reboot(bool(res["status"].get("reboot_pending")))
        return res

    async def oxp3_catalog(self):
        return await self._call("oxp3_catalog", lambda: _mod("oxp3tool").catalog(self.ctx))

    async def oxp3_plan(self, fixes, opts=None):
        return await self._call("oxp3_plan", lambda: _mod("oxp3tool").plan(self.ctx, fixes, opts))

    def _noting_reboot(self, res):
        """An apply or revert whose outcome needs a reboot: "Reboot to finish" shows before the next status."""
        outcome = res.get("outcome") if isinstance(res, dict) else None
        if isinstance(outcome, dict) and outcome.get("reboot_needed"):
            self._note_reboot(True)
        return res

    async def oxp3_apply(self, fixes, opts=None):
        return await self._start("oxp3_apply", "oxp3-apply", _title("Apply", fixes),
                                 lambda job: self._noting_reboot(_mod("oxp3tool").apply(self.ctx, job, fixes, opts)),
                                 lambda: _mod("oxp3tool").apply_args(self.ctx, fixes, opts)[1])

    async def oxp3_revert(self, fixes):
        return await self._start("oxp3_revert", "oxp3-revert", _title("Revert", fixes),
                                 lambda job: self._noting_reboot(_mod("oxp3tool").revert(self.ctx, job, fixes)),
                                 lambda: _mod("oxp3tool").check_fixes(self.ctx, fixes, "revert")[1])

    async def oxp3_kernel(self):
        return await self._call("oxp3_kernel", lambda: _mod("oxp3tool").kernel(self.ctx))

    async def oxp3_kernel_try(self, pkg):
        return await self._start("oxp3_kernel_try", "oxp3-kernel-try", f"Boot {pkg} once",
                                 lambda job: _mod("oxp3tool").kernel_try(self.ctx, job, pkg),
                                 lambda: _mod("oxp3tool").check_kernel_package(pkg))

    async def oxp3_kernel_keep(self):
        return await self._start("oxp3_kernel_keep", "oxp3-kernel-keep", "Keep the running kernel",
                                 lambda job: _mod("oxp3tool").kernel_keep(self.ctx, job))

    async def oxp3_diagnose(self):
        return await self._start("oxp3_diagnose", "oxp3-diagnose", "Diagnose archive",
                                 lambda job: _mod("oxp3tool").diagnose(self.ctx, job))

    async def oxp3_update_check(self):
        return await self._call("oxp3_update_check", lambda: _mod("oxp3tool").update_check(self.ctx, self.settings))

    async def oxp3_install(self, source):
        where = "from the plugin" if source == "bundled" else "from GitHub"
        return await self._start("oxp3_install", "oxp3-install", f"Install oxp3-tool {where}",
                                 lambda job: _mod("oxp3tool").install(self.ctx, self.settings, job, source),
                                 lambda: None if source in ("bundled", "online") else
                                 f"oxp3_install: source must be 'bundled' or 'online', not {source!r}")

    # ---- xess-xmx-linux ------------------------------------------------------------------------------------------

    async def xess_state(self):
        return await self._call("xess_state", lambda: _mod("xess").state(self.ctx, self.settings))

    async def xess_check_update(self):
        return await self._call("xess_check_update", lambda: _mod("xess").check_update(self.ctx, self.settings))

    async def xess_install(self):
        return await self._start("xess_install", "xess-install", "Install xess-xmx-linux",
                                 lambda job: _mod("xess").install(self.ctx, self.settings, job))

    async def xess_uninstall(self):
        return await self._start("xess_uninstall", "xess-uninstall", "Uninstall xess-xmx-linux",
                                 lambda job: _mod("xess").uninstall(self.ctx, self.settings, job))

    async def xess_set_driver(self, prefix):
        def go():
            self._not_during_xess_job("switch the driver")
            return _mod("xess").set_driver(self.ctx, self.settings, prefix)
        return await self._call("xess_set_driver", go)

    async def xess_shim_system_protons(self):
        def go():
            self._not_during_xess_job("shim the system Protons")
            return _mod("xess").shim_system_protons(self.ctx, self.settings)
        return await self._call("xess_shim_system_protons", go)

    async def xess_remove_folder(self, path):
        def go():
            self._not_during_xess_job("remove the folder")
            return _mod("xess").remove_folder(self.ctx, self.settings, path)
        return await self._call("xess_remove_folder", go)

    # ---- per game ------------------------------------------------------------------------------------------------

    async def game_info(self, appid):
        return await self._call("game_info", lambda: _mod("games").game_info(self.ctx, self.settings, str(appid)))

    # ---- jobs and misc -------------------------------------------------------------------------------------------

    async def job_status(self, job_id=None):
        def go():
            if job_id is not None and not isinstance(job_id, str):
                return {"ok": False, "error": f"job_status: bad task id {job_id!r}", "job": None}
            job = self.jobs.get(job_id or None)
            if job is None and job_id:
                return {"ok": False, "error": f"job_status: no task {job_id}", "job": None}
            return {"ok": True, "error": None, "job": job.to_dict() if job else None}
        return await self._call("job_status", go)

    async def job_list(self):
        return await self._call("job_list", lambda: {"ok": True, "error": None,
                                                     "jobs": [j.to_dict(tail=20) for j in self.jobs.recent(10)]})

    async def reboot(self):
        return await self._call("reboot", self._reboot)

    def _reboot(self) -> dict:
        running = self.jobs.running()
        if running is not None:
            return {"ok": False, "error": f"reboot: {running.title} is running; reboot when it has ended"}
        finish = _mod("oxp3tool").rebuild_unfinished(self.ctx)
        if finish:
            return {"ok": False, "error": f"reboot: do not reboot: oxp3-tool's boot-entry rebuild did not finish; "
                                          f"run `sudo {finish}` in a terminal first"}
        systemctl = sysexec.which("systemctl") or "/usr/bin/systemctl"
        r = sysexec.run([systemctl, "reboot"], timeout=60, start_new_session=True)
        if r.rc != 0:
            why = (r.err or r.out).strip().splitlines()[-1:] or [f"exit {r.rc}"]
            return {"ok": False, "error": f"reboot: systemctl reboot: {why[0]}"}
        return {"ok": True, "error": None}

    async def get_setting(self, key):
        """The stored value (DESIGN.md: any), or None for an unknown key."""
        try:
            if self._broken or _setting_key_error(key):
                return None
            return self.settings.get(key)
        except Exception:
            log.error("get_setting failed:\n%s", traceback.format_exc())
            return None

    async def set_setting(self, key, value):
        def go():
            why = _setting_key_error(key) or _setting_value_error(key, value)
            if why:
                return {"ok": False, "error": f"set_setting: {why}"}
            if key.startswith("ui_") and self.settings.get(key) is None and \
                    sum(1 for k in self.settings.all() if k.startswith("ui_")) >= MAX_UI_KEYS:
                return {"ok": False, "error": f"set_setting: more than {MAX_UI_KEYS} ui_ settings"}
            err = self.settings.set(key, value)
            return {"ok": err is None, "error": err}
        return await self._call("set_setting", go)


def _setting_key_error(key):
    if not isinstance(key, str) or not (key in SETTING_KEYS or UI_KEY.match(key)):
        return f"{key!r} is not a setting the frontend may use"[:120]
    return None


def _setting_value_error(key, value):
    if key in SETTING_KEYS and not isinstance(value, SETTING_KEYS[key]):
        return f"{key} cannot be {type(value).__name__}"
    if key == "xess_driver" and isinstance(value, str) and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", value):
        return f"xess_driver: {value[:40]!r} is not a driver version"
    try:
        blob = json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return f"{key}: not a JSON value"
    if len(blob) > MAX_SETTING_BYTES:
        return f"{key}: more than {MAX_SETTING_BYTES} bytes"
    return None
