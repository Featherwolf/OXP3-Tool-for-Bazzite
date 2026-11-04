"""xess.py against a fake home, a fake root and a fake desktop user (FakeUser runs mkdir/rm/mv/cp and the unpack
for real in the temporary folders, and fakes systemctl and the package's bash scripts)."""
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from unittest import mock

from oxp3deck import context, downloads, jobs, sysexec, xess
from tests.test_steamlib import World, fixture, patch_user, write

TYPES_TS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "types.ts")


def ts_fields(name: str) -> set:
    """The top-level field names of `export type NAME = [Res &] { ... }` in src/types.ts (Res adds ok and error):
    what the frontend expects of an answer."""
    with open(TYPES_TS) as f:
        text = re.sub(r"//[^\n]*", "", f.read())
    m = re.search(rf"export type {name} = (Res & )?\{{", text)
    assert m, f"types.ts has no type {name}"
    flat, depth = [], 1
    for ch in text[m.end():]:
        depth += (ch == "{") - (ch == "}")
        if depth == 0:
            break
        flat.append(ch if depth == 1 else " ")      # nested objects' fields are not this type's
    fields = set(re.findall(r"\b([A-Za-z_]\w*)\??\s*:", "".join(flat)))
    return fields | {"ok", "error"} if m.group(1) else fields


TAG_OLD, TAG_NEW = "20261031", "20261104"
SHIM_OLD = b"MZ shim build one IGDEXT_TRACE IGDEXT_FORCE_FALLBACK"
SHIM_NEW = b"MZ shim build two, IGDEXT_TRACE IGDEXT_XELL_CAP IGDEXT_FORCE_FALLBACK"
STOCK = b"MZ stock igdext64.dll of proton-cachyos"
STOCK2 = b"MZ stock igdext64.dll of proton-cachyos, updated by pacman"
OWNER_LINE = ("ok 2026-11-01 09:26:46 /home/jordan/Documents/xess-xmx-linux-20261031/lib/mesa-26.3.0-rc2/"
              "libvulkan_intel.so Mesa 26.3.0-rc2 (git-4930fa53e8) (note: system has Mesa 27.0.0, the patched "
              "driver is Mesa 26.3.0)")

PACKAGE_FILES = {
    "install.sh": 0o755, "uninstall.sh": 0o755, "enable.sh": 0o755, "xmx-launch.sh": 0o755,
    "tools/session-check.sh": 0o755, "tools/switch-driver.sh": 0o755, "tools/cm-compile.sh": 0o755,
    "tools/proton-hook.py": 0o755, "lib/libvulkan_intel.so": 0o644, "README.md": 0o644, "ControlLib.dll": 0o644,
}


def package_files(tag, drivers, active, shim) -> dict:
    """{relative path: (bytes, mode)} of a release."""
    files = {rel: (f"# {rel} of {tag}\n".encode(), mode) for rel, mode in PACKAGE_FILES.items()}
    if tag >= "20261104":
        files["tools/fsr-fg.sh"] = (b"# fsr-fg\n", 0o755)
        files["install.sh"] = (b"# install.sh\necho INTC_ALT_DRIVER_EXTENSIONS_PATH\n", 0o755)
    files["igdext64.dll"] = (shim, 0o644)
    files["lib/active"] = (active.encode() + b"\n", 0o644)
    for v in drivers:
        files[f"lib/mesa-{v}/libvulkan_intel.so"] = (f"ELF mesa {v}".encode(), 0o644)
    for name in os.listdir(os.path.join(os.path.dirname(__file__), "fixtures", "xess", tag, "tools")):
        files[f"tools/{name}"] = (fixture(tag, "tools", name), 0o755)
    return files


def make_package(path, tag, drivers=("26.1.2", "26.3.0-rc2"), active="26.3.0-rc2", shim=SHIM_OLD):
    for rel, (data, mode) in package_files(tag, drivers, active, shim).items():
        write(os.path.join(path, rel), data, mode)
    os.makedirs(os.path.join(path, "kernels"), exist_ok=True)
    return path


def release_zip(tag, top=None, drivers=("26.1.2", "26.3.0-rc3"), active="26.3.0-rc3", extra=None) -> bytes:
    top = top or f"xess-xmx-linux-{tag}"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        zi = zipfile.ZipInfo(top + "/kernels/")
        zi.create_system = 3
        zi.external_attr = (0o40755 << 16) | 0x10
        z.writestr(zi, b"")
        for rel, (data, mode) in package_files(tag, drivers, active, SHIM_NEW).items():
            zi = zipfile.ZipInfo(f"{top}/{rel}")
            zi.create_system = 3
            zi.external_attr = (0o100000 | mode) << 16
            z.writestr(zi, data)
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def env_text(pkg, alt=False) -> str:
    lines = ["# XeSS XMX (xess-xmx-linux): patched ANV as the Intel Vulkan driver",
             f"VK_DRIVER_FILES={pkg}/run/intel_icd.json:/usr/share/vulkan/icd.d/intel_hasvk_icd.json",
             "VKD3D_DISABLE_EXTENSIONS=VK_EXT_descriptor_buffer", f"ANV_CM_KERNEL_DIR={pkg}/kernels",
             f"ANV_CM_HELPER={pkg}/tools/cm-compile.sh", "force_vk_vendor=0"]
    if alt:
        lines.append("INTC_ALT_DRIVER_EXTENSIONS_PATH='C:\\windows\\system32\\driverstore\\filerepository\\igd_faux.inf_1'")
    return "\n".join(lines) + "\n"


def ok_line(pkg, v="26.3.0-rc2"):
    return f"ok 2026-11-01 09:26:46 {pkg}/lib/mesa-{v}/libvulkan_intel.so Mesa {v} (git-4930fa53e8)\n"


def failed_line(pkg, v="26.3.0-rc3", why="libSPIRV-Tools.so: cannot open shared object file"):
    return f"FAILED 2026-11-04 10:00:00 {pkg}/lib/mesa-{v}/libvulkan_intel.so: {why}\n"


def fetch_piece(pkg, sub):
    """What get-igc.sh / get-xess.sh / get-ffx.sh leave in the package."""
    if sub == "igc":
        write(os.path.join(pkg, "igc", "neo", "bin", "ocloc-25.18.1"), b"ocloc", 0o755)
        write(os.path.join(pkg, "igc", "igc", "local", "lib", "libigc.so.2"), b"igc")
    elif sub == "xess":
        write(os.path.join(pkg, "xess", "libxess.dll"), b"sdk libxess (downloaded)")
    else:
        for f in ("amd_fidelityfx_loader_dx12.dll", "amd_fidelityfx_framegeneration_dx12.dll"):
            write(os.path.join(pkg, "ffx", f), b"ffx")


UNITS_ON = ("LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n\n"
            "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n")
UNITS_GONE = ("LoadState=not-found\nActiveState=inactive\nUnitFileState=\n\n"
              "LoadState=not-found\nActiveState=inactive\nUnitFileState=\n")


class XessCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.w = World(self.tmp.name)
        self.ctx = self.w.ctx
        self.home = self.ctx.home
        os.makedirs(self.w.sys("/proc"))
        write(self.w.sys("/etc/os-release"), 'NAME="CachyOS Linux"\nID=cachyos\nID_LIKE=arch\n')
        self.settings = context.Settings(os.path.join(self.ctx.settings_dir, "settings.json"))
        self.job = jobs.Job("xess_install", "Install XeSS")
        self.u = patch_user(self)
        self.missing = {"restorecon"}
        p = mock.patch.object(sysexec, "which", lambda n: None if n in self.missing else "/usr/bin/" + n)
        p.start()
        self.addCleanup(p.stop)
        self.tmp_parent = os.path.join(self.tmp.name, "roottmp")
        os.makedirs(self.tmp_parent)
        p = mock.patch.object(xess, "TMP_PARENT", self.tmp_parent)
        p.start()
        self.addCleanup(p.stop)
        self.latest = TAG_NEW
        self.zips = {}
        self.downloads = []
        p = mock.patch.object(downloads, "latest_release_tag", self.fake_latest)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(downloads, "download", self.fake_download)
        p.start()
        self.addCleanup(p.stop)
        xess._marker_cache.clear()
        self.old = os.path.join(self.home, "Documents", f"xess-xmx-linux-{TAG_OLD}")
        self.xroot = os.path.join(self.home, ".local", "share", "oxp3deck", "xess")
        self.new = os.path.join(self.xroot, f"xess-xmx-linux-{TAG_NEW}")
        self.incoming = os.path.join(self.xroot, f".incoming-{TAG_NEW}")

    # -- fakes --

    def fake_latest(self, repo):
        self.assertEqual(repo, "HHHHanasak1/xess-xmx-linux")
        if isinstance(self.latest, Exception):
            raise self.latest
        return self.latest

    def fake_download(self, url, dest, *, expected_sha256=None, max_bytes, on_progress=None):
        st = os.lstat(os.path.dirname(dest))
        self.downloads.append({"url": url, "dest": dest, "max_bytes": max_bytes, "sha": expected_sha256,
                               "parent_mode": stat.S_IMODE(st.st_mode), "parent_uid": st.st_uid})
        if url not in self.zips:
            raise downloads.DownloadError(f"download {os.path.basename(url)}: HTTP 404 Not Found")
        data = self.zips[url]
        with open(dest, "wb") as f:
            f.write(data)
        if on_progress:
            on_progress(len(data), len(data))
        return hashlib.sha256(data).hexdigest()

    def url(self, tag):
        return f"https://github.com/HHHHanasak1/xess-xmx-linux/releases/download/{tag}/xess-xmx-linux-{tag}.zip"

    # -- the owner's machine --

    def owner(self, *, system_proton=True):
        self.w.make_steam()
        make_package(self.old, TAG_OLD)
        write(os.path.join(self.old, "igc", "neo", "bin", "ocloc-25.18.1"), b"ocloc", 0o755)
        write(os.path.join(self.old, "igc", "igc", "local", "lib", "libigc.so.2"), b"igc")
        write(os.path.join(self.old, "xess", "libxess.dll"), b"sdk libxess")
        write(os.path.join(self.old, "xess", "VERSION"), b"v3.0.2\n")
        write(os.path.join(self.home, xess.ENV_FILE), env_text(self.old))
        write(os.path.join(self.home, xess.UNIT_DIR, xess.CHECK_UNIT),
              f'[Unit]\n[Service]\nExecStart=/bin/bash "{self.old}/tools/session-check.sh"\n')
        write(os.path.join(self.home, xess.STATUS_FILE), ok_line(self.old))
        if system_proton:
            self.slr = self.w.proton("proton-cachyos-slr", "system", dll=STOCK)
            self.slr_dll = os.path.join(self.slr, xess.IGDEXT_REL)
        self.zips[self.url(TAG_NEW)] = release_zip(TAG_NEW)

    def script(self, path, rc=0, out="", effect=None):
        def fn(argv, cwd):
            if effect:
                effect(argv, cwd)
            return sysexec.Result(rc, out, "")
        self.u.scripts[path] = fn

    def fake_install_of(self, pkg, v="26.3.0-rc3", fail=(), status=None):
        """install.sh: downloads each missing piece its get script fetches (except those in `fail`: install.sh only
        prints a line then), the environment file, the check unit, the check line (`status`, else ok)."""
        def effect(argv, cwd):
            self.assertEqual(cwd, pkg)
            self.fetched = []
            for sub, script, present, _pins in xess.PIECES:
                if os.path.isfile(os.path.join(pkg, script)) and not present(pkg) and sub not in fail:
                    fetch_piece(pkg, sub)
                    self.fetched.append(sub)
            write(os.path.join(self.home, xess.ENV_FILE), env_text(pkg, alt=True))
            write(os.path.join(self.home, xess.UNIT_DIR, xess.CHECK_UNIT),
                  f'ExecStart=/bin/bash "{pkg}/tools/session-check.sh"\n')
            write(os.path.join(self.home, xess.STATUS_FILE), status or ok_line(pkg, v))
        return effect

    def fake_uninstall(self, argv, cwd):
        for rel in (xess.ENV_FILE, os.path.join(xess.UNIT_DIR, xess.CHECK_UNIT)):
            p = os.path.join(self.home, rel)
            if os.path.exists(p):
                os.unlink(p)

    def fake_switch(self, pkg):
        def effect(argv, cwd):
            want = argv[2]
            vers = [d["version"] for d in xess.drivers(pkg) if d["version"].startswith(want)]
            write(os.path.join(pkg, "lib", "active"), vers[-1] + "\n")
        return effect

    def assert_in_order(self, expected):
        cmds = self.u.commands()
        last = -1
        for e in expected:
            idx = [i for i, c in enumerate(cmds) if c == e and i > last]
            self.assertTrue(idx, f"{e!r} missing after position {last} in:\n" + "\n".join(cmds))
            last = idx[0]


class StatusLineTest(unittest.TestCase):
    def test_owner(self):
        s = xess.parse_status(OWNER_LINE + "\nsecond line ignored\n")
        self.assertEqual(s["state"], "ok")
        self.assertEqual(s["driver"], "26.3.0-rc2")
        self.assertEqual(s["date"], "2026-11-01 09:26:46")
        self.assertEqual(s["driver_info"], "Mesa 26.3.0-rc2 (git-4930fa53e8)")
        self.assertEqual(s["notes"], ["system has Mesa 27.0.0, the patched driver is Mesa 26.3.0"])
        self.assertEqual(s["line"], OWNER_LINE)
        self.assertTrue(s["lib"].endswith("/lib/mesa-26.3.0-rc2/libvulkan_intel.so"))

    def test_no_vulkaninfo(self):
        # ours is empty and the note follows the library after a second space (session-check.sh:102, :118)
        s = xess.parse_status("ok 2026-11-01 09:26:46 /p/lib/mesa-26.1.2/libvulkan_intel.so  (note: vulkaninfo not "
                              "installed, device enumeration not checked)")
        self.assertEqual((s["state"], s["driver"], s["driver_info"]), ("ok", "26.1.2", None))
        self.assertEqual(s["notes"], ["vulkaninfo not installed, device enumeration not checked"])

    def test_two_notes(self):
        s = xess.parse_status("ok 2026-11-01 09:26:46 /p/lib/mesa-26.3.0-rc3/libvulkan_intel.so Mesa 26.3.0-rc3 "
                              "(git-1) (note: system has Mesa 26.2.5, the patched driver is Mesa 26.3.0) (note: "
                              "this driver does not read /usr/share/drirc.d, so Mesa's per-game workarounds are off "
                              "- update the package or rebuild with tools/rebuild-driver.sh)")
        self.assertEqual(len(s["notes"]), 2)
        self.assertTrue(s["notes"][1].startswith("this driver does not read /usr/share/drirc.d"))
        self.assertEqual(s["driver_info"], "Mesa 26.3.0-rc3 (git-1)")

    def test_switched(self):
        s = xess.parse_status("ok 2026-11-02 08:00:01 /p/lib/mesa-26.1.2/libvulkan_intel.so Mesa 26.1.2 (git-2) "
                              "(the previous driver failed: vulkaninfo found no device (ERROR: x (y)); switched to "
                              "Mesa 26.1.2)")
        self.assertEqual((s["state"], s["driver"], s["switched_to"]), ("ok", "26.1.2", "26.1.2"))
        self.assertEqual(s["reason"], "vulkaninfo found no device (ERROR: x (y))")
        self.assertEqual(s["driver_info"], "Mesa 26.1.2 (git-2)")
        self.assertIn("switched to Mesa 26.1.2", s["notes"][-1])

    def test_failed(self):
        s = xess.parse_status("FAILED 2026-11-02 08:00:01 /p/lib/mesa-26.3.0-rc2/libvulkan_intel.so: "
                              "libSPIRV-Tools.so: cannot open shared object file: No such file or directory")
        self.assertEqual((s["state"], s["driver"]), ("failed", "26.3.0-rc2"))
        self.assertEqual(s["reason"], "libSPIRV-Tools.so: cannot open shared object file: No such file or directory")
        self.assertEqual(s["notes"], [s["reason"]])

    def test_driver_from_info_without_active(self):
        s = xess.parse_status("ok 2026-11-02 08:00:01 /p/lib/libvulkan_intel.so Mesa 26.1.2 (git-2)")
        self.assertEqual(s["driver"], "26.1.2")

    def test_unknown(self):
        for line in (None, "", "\n", "garbage", "ok", "FAILED x"):
            s = xess.parse_status(line)
            self.assertEqual(s["state"], "unknown", line)
            self.assertEqual(s["notes"], [])


class TagTest(unittest.TestCase):
    def test_order(self):
        order = ["v1.0", "v1.2", "v1.2.1", "v1.3.2", "v1.10", "20261005", "20261029", "20261029-2", "20261031",
                 "20261102", "20261102-2", "20261104"]
        keys = [xess.tag_key(t) for t in order]
        self.assertEqual(keys, sorted(keys))
        self.assertTrue(xess.newer("20261104", "20261031"))
        self.assertFalse(xess.newer("20261031", "20261031"))
        self.assertTrue(xess.newer("20261029-2", "20261029"))
        self.assertTrue(xess.newer("20261005", "v1.3.2"))
        self.assertFalse(xess.newer("v1.3.2", "20261005"))
        self.assertIsNone(xess.newer("20261104", "my-build"))
        self.assertIsNone(xess.tag_key(None))


class DetectTest(XessCase):
    def test_env_file(self):
        self.owner()
        d = xess.detect(self.ctx, self.settings)
        self.assertEqual((d["path"], d["how"], d["installed"]), (os.path.realpath(self.old), "env", True))
        self.assertEqual(xess.package_version(d["path"], self.settings), TAG_OLD)
        self.assertFalse(xess.is_managed(self.ctx, d["path"]))

    def test_helper_when_vk_names_no_package(self):
        self.owner()
        write(os.path.join(self.home, xess.ENV_FILE), f"VK_DRIVER_FILES=/usr/share/x.json\n"
                                                      f"ANV_CM_HELPER={self.old}/tools/cm-compile.sh\n")
        self.assertEqual(xess.detect(self.ctx, self.settings)["path"], os.path.realpath(self.old))

    def test_env_points_at_missing_folder(self):
        self.owner()
        write(os.path.join(self.home, xess.ENV_FILE), env_text("/gone/xess-xmx-linux-20261029"))
        d = xess.detect(self.ctx, self.settings)
        self.assertEqual((d["path"], d["how"]), (os.path.realpath(self.old), "check-unit"))
        self.u.session_env, self.u.units = "", UNITS_ON
        st = xess.state(self.ctx, self.settings)["xess"]
        self.assertTrue(any("/gone/xess-xmx-linux-20261029" in n for n in st["status"]["notes"]))

    def test_settings_only_is_not_installed(self):
        renamed = make_package(os.path.join(self.xroot, "my-xess"), TAG_NEW)
        self.settings.update({"xess_package": renamed, "xess_tag": TAG_NEW})
        d = xess.detect(self.ctx, self.settings)
        self.assertEqual((d["path"], d["how"], d["installed"]), (os.path.realpath(renamed), "settings", False))
        self.assertEqual(xess.package_version(d["path"], self.settings), TAG_NEW)     # recorded tag of a renamed folder
        self.assertTrue(xess.is_managed(self.ctx, d["path"]))
        self.assertIsNone(xess.package_version("/x/other", self.settings))

    def test_nothing(self):
        d = xess.detect(self.ctx, self.settings)
        self.assertEqual((d["path"], d["installed"]), (None, False))
        s = xess.state(self.ctx, self.settings)
        self.assertTrue(s["ok"])
        self.assertFalse(s["xess"]["installed"])
        self.assertEqual(self.u.calls, [])       # nothing to ask systemd about


class StateTest(XessCase):
    def test_owner_state(self):
        self.owner()
        ge = self.w.proton("GE-Proton10-3", "tools", dll=SHIM_OLD, hook=True)
        self.w.proton("Proton - Experimental", "common", dll=STOCK)
        self.w.proton("Proton 11.0", "common", dll=None)        # no igdext of its own: not listed
        os.makedirs(os.path.join(self.home, "elsewhere"))
        self.settings.update({"xess_driver": "26.3", "xess_latest": TAG_NEW, "xess_checked_at": 5.0,
                              "xess_old_folders": [os.path.join(self.home, "elsewhere"), "/gone", self.old]})
        self.missing |= {"vulkaninfo"}
        self.u.session_env = f"HOME={self.home}\nVK_DRIVER_FILES={self.old}/run/intel_icd.json:/usr/share/x.json\n"
        self.u.units = UNITS_ON
        self.w.steam_client({"VK_DRIVER_FILES": f"{self.old}/run/intel_icd.json:/usr/share/x.json"})
        r = xess.state(self.ctx, self.settings)
        self.assertTrue(r["ok"], r)
        x = r["xess"]
        self.assertTrue(x["installed"])
        self.assertEqual(x["package_dir"], os.path.realpath(self.old))
        self.assertEqual(x["version"], TAG_OLD)
        self.assertFalse(x["managed"])
        self.assertEqual(x["drivers"], [{"version": "26.1.2", "default": False},
                                        {"version": "26.3.0-rc2", "default": True}])
        self.assertEqual(x["preferred_driver"], "26.3")
        self.assertEqual((x["status"]["state"], x["status"]["driver"]), ("ok", "26.3.0-rc2"))
        self.assertTrue(x["live"])
        self.assertFalse(x["relogin_needed"])
        self.assertEqual(x["units"], {"check": "enabled", "watcher": "active"})
        self.assertEqual(x["system_protons"], [{"path": self.slr_dll, "name": "proton-cachyos-slr", "shimmed": False,
                                                "stock": False}])
        self.assertFalse(x["system_protons_need_shim"])
        names = {p["name"]: p for p in x["user_protons"]}
        self.assertEqual(set(names), {"GE-Proton10-3", "Proton - Experimental"})
        self.assertEqual((names["GE-Proton10-3"]["shimmed"], names["GE-Proton10-3"]["hook"]), (True, True))
        self.assertEqual(names["GE-Proton10-3"]["path"], ge)
        self.assertEqual((names["Proton - Experimental"]["shimmed"], names["Proton - Experimental"]["hook"]),
                         (False, False))
        self.assertEqual(x["missing_tools"], ["vulkaninfo (optional)"])
        self.assertEqual(x["old_folders"], [])        # /gone is gone, the package in use is not old
        self.assertEqual(x["old_folders_kept"], [{"path": os.path.join(self.home, "elsewhere"), "why": (
            "does not look like an xess-xmx-linux folder (no install.sh, uninstall.sh, tools/session-check.sh)")}])
        self.assertIsNone(x["package_dir_kept"])     # ~/Documents/xess-xmx-linux-20261031 can go once it is old
        self.assertFalse(x["flatpak_steam"])
        self.assertEqual((x["latest"], x["checked_at"]), (TAG_NEW, 5.0))
        self.assertEqual(x["features"], {"fsr": False, "alt_driver_store": False})
        self.assertEqual([c["argv"][:3] for c in self.u.calls],
                         [["systemctl", "--user", "show-environment"], ["systemctl", "--user", "show"]])

    def test_relogin_needed(self):
        self.owner()
        self.u.session_env = f"VK_DRIVER_FILES={self.old}/run/intel_icd.json:/x.json\n"
        self.u.units = UNITS_GONE
        self.w.steam_client({"VK_DRIVER_FILES": "/home/jordan/.local/share/oxp3deck/xess/old/run/intel_icd.json"})
        x = xess.state(self.ctx, self.settings)["xess"]
        self.assertTrue(x["live"])
        self.assertTrue(x["relogin_needed"])
        self.assertEqual(x["units"], {"check": "not-found", "watcher": "not-found"})
        s = xess.summary(self.ctx, self.settings)
        self.assertEqual(s, {"installed": True, "version": TAG_OLD, "status": "ok", "line": ok_line(self.old).strip(),
                             "live": False, "relogin_needed": True})

    def test_no_relogin_when_steam_is_off_or_driver_failed(self):
        self.owner()
        self.u.session_env = f"VK_DRIVER_FILES={self.old}/run/intel_icd.json\n"
        self.assertFalse(xess.state(self.ctx, self.settings)["xess"]["relogin_needed"])    # Steam not running
        self.w.steam_client({"VK_DRIVER_FILES": "/usr/share/vulkan/icd.d/intel_icd.json"})
        write(os.path.join(self.home, xess.STATUS_FILE), f"FAILED 2026-11-02 08:00:01 {self.old}/lib/mesa-26.3.0-rc2/"
                                                         "libvulkan_intel.so: cannot be loaded\n")
        x = xess.state(self.ctx, self.settings)["xess"]
        self.assertEqual(x["status"]["state"], "failed")
        self.assertFalse(x["relogin_needed"])

    def test_summary_makes_no_systemctl_call(self):
        self.owner()
        s = xess.summary(self.ctx, self.settings)
        self.assertEqual((s["installed"], s["version"], s["status"], s["live"], s["relogin_needed"]),
                         (True, TAG_OLD, "ok", True, False))
        self.assertEqual(self.u.calls, [])
        self.w.steam_client({"VK_DRIVER_FILES": f"{self.old}/run/intel_icd.json"})
        self.assertTrue(xess.summary(self.ctx, self.settings)["live"])

    def test_flatpak_and_status_of_another_folder(self):
        self.owner()
        os.makedirs(os.path.join(self.home, ".var/app/com.valvesoftware.Steam"))
        write(os.path.join(self.home, xess.STATUS_FILE), ok_line("/elsewhere/xess-xmx-linux-1"))
        x = xess.state(self.ctx, self.settings)["xess"]
        self.assertTrue(x["flatpak_steam"])
        self.assertTrue(any("not this package" in n for n in x["status"]["notes"]))

    def test_types_ts_names_the_same_fields(self):
        """src/types.ts against the answers (missing_downloads, repair, update_available, script_running...): a field
        the backend adds or drops has to be added or dropped there too."""
        self.owner()
        self.u.units = UNITS_ON
        x = xess.state(self.ctx, self.settings)["xess"]
        self.assertEqual(set(x), ts_fields("XessState"))
        self.assertEqual(set(x["status"]), ts_fields("XessStatus"))
        self.assertEqual(set(xess.summary(self.ctx, self.settings)), ts_fields("XessSummary"))
        self.assertEqual(set(xess.check_update(self.ctx, self.settings)), ts_fields("XessUpdateCheck"))
        self.assertEqual(set(xess.parse_status(None)), ts_fields("XessStatus"))


class CheckUpdateTest(XessCase):
    def test_hand_install_is_older(self):
        self.owner()
        r = xess.check_update(self.ctx, self.settings)
        self.assertEqual((r["ok"], r["installed"], r["latest"], r["update_available"]), (True, TAG_OLD, TAG_NEW, True))
        self.assertEqual(self.settings.get("xess_latest"), TAG_NEW)
        self.assertEqual(self.settings.get("xess_checked_at"), r["checked_at"])

    def test_up_to_date(self):
        self.owner()
        self.latest = TAG_OLD
        self.assertFalse(xess.check_update(self.ctx, self.settings)["update_available"])

    def test_unknown_version(self):
        self.w.make_steam()
        pkg = make_package(os.path.join(self.home, "xess"), TAG_OLD)
        write(os.path.join(self.home, xess.ENV_FILE), env_text(pkg))
        r = xess.check_update(self.ctx, self.settings)
        self.assertEqual((r["installed"], r["update_available"]), (None, True))
        self.assertIn("does not say which release", r["note"])

    def test_not_installed_and_offline(self):
        r = xess.check_update(self.ctx, self.settings)
        self.assertEqual((r["ok"], r["update_available"]), (True, False))
        self.latest = downloads.DownloadError("latest release of x: HTTP 403 (GitHub's rate limit?)")
        r = xess.check_update(self.ctx, self.settings)
        self.assertFalse(r["ok"])
        self.assertTrue(r["error"].startswith("xess update check: "))
        self.assertEqual(r["latest"], TAG_NEW)      # the last good answer stays


class InstallTest(XessCase):
    def standard_scripts(self):
        new = os.path.realpath(self.new)
        self.script(os.path.join(self.old, "uninstall.sh"), out="restored 2 igdext64.dll file(s)\n",
                    effect=self.fake_uninstall)
        self.script(os.path.join(new, "install.sh"), out="== shim: 3 file(s) updated\ndone. Reboot\n",
                    effect=self.fake_install_of(new))
        self.script(os.path.join(new, "tools", "switch-driver.sh"), out="== default: Mesa 26.1.2\n",
                    effect=self.fake_switch(new))
        return new

    def test_update_from_hand_install(self):
        self.owner()
        self.settings.set("xess_driver", "26.1")
        new = self.standard_scripts()
        r = xess.install(self.ctx, self.settings, self.job)
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["tag"], r["package_dir"], r["previous"]), (TAG_NEW, new, os.path.realpath(self.old)))
        self.assertTrue(r["relogin_needed"])
        self.assertEqual(r["next"], xess.NEXT_INSTALL)
        self.assertEqual(r["warnings"], [])
        self.assert_in_order([
            f"mkdir {self.xroot}", f"mkdir {self.incoming}", "python3 unpack",
            f"mv {self.incoming}/xess-xmx-linux-{TAG_NEW} {self.new}", f"rm {self.incoming}",
            f"cp {self.old}/igc {new}/igc", f"cp {self.old}/xess {new}/xess",
            "systemctl stop", f"bash {self.old}/uninstall.sh", f"bash {new}/install.sh",
            f"bash {new}/tools/switch-driver.sh 26.1",
        ])
        cmds = self.u.commands()
        # XESS-1: 20261104 rewrote get-igc.sh but pins the same IGC and ocloc, so the 95 MB are not fetched again
        self.assertIn(f"Copying igc/ from {self.old} (tools/get-igc.sh pins the same downloads)", self.job.log)
        self.assertIn(f"Copying xess/ from {self.old} (tools/get-xess.sh is unchanged)", self.job.log)
        self.assertEqual(self.fetched, ["ffx"])                     # only what 20261031 never had
        self.assertNotIn(f"bash {self.old}/install.sh", cmds)                                # no rollback
        stop = next(c for c in self.u.calls if c["argv"][:3] == ["systemctl", "--user", "stop"])
        self.assertEqual(stop["argv"][3:], ["xess-xmx-shim.path", "xess-xmx-shim.service"])
        for c in self.u.calls:
            if c["argv"][0] == "bash":
                self.assertTrue(c["detach"], c)          # RT-1: survives a plugin restart
                self.assertEqual(c["cwd"], os.path.dirname(c["argv"][1]).removesuffix("/tools"))
            if c["argv"][0] == "bash" and c["argv"][1].endswith(("install.sh", "uninstall.sh")):
                self.assertIsNone(c["timeout"])         # downloads hundreds of MB: never killed
                self.assertTrue(c["streamed"])
        self.assertFalse(os.path.exists(self.w.sys(f"/run/oxp3deck/{xess.SCRIPT_RECORD}")))
        # the download: one URL, a 0700 folder of root's, 200 MB at most; then cleaned up
        self.assertEqual([d["url"] for d in self.downloads], [self.url(TAG_NEW)])
        self.assertEqual(self.downloads[0]["parent_mode"], 0o700)
        self.assertEqual(self.downloads[0]["parent_uid"], os.geteuid())
        self.assertEqual(self.downloads[0]["max_bytes"], 200 << 20)
        self.assertEqual(os.listdir(self.tmp_parent), [])
        self.assertFalse(os.path.lexists(self.incoming))
        self.assertTrue(any("no checksum" in line for line in self.job.log))
        # the unpacked tree: exec bits back, the reused SDK and compiler
        self.assertTrue(os.stat(os.path.join(new, "xmx-launch.sh")).st_mode & 0o111)
        self.assertTrue(os.stat(os.path.join(new, "tools", "get-igc.sh")).st_mode & 0o111)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(new, "README.md")).st_mode) & 0o111, 0)
        self.assertTrue(os.path.isdir(os.path.join(new, "kernels")))
        with open(os.path.join(new, "xess", "libxess.dll"), "rb") as f:
            self.assertEqual(f.read(), b"sdk libxess")
        self.assertTrue(os.access(os.path.join(new, "igc", "neo", "bin", "ocloc-25.18.1"), os.X_OK))
        self.assertEqual(xess.drivers(new)[0], {"version": "26.1.2", "default": True})     # the choice re-applied
        # the system Proton, as root: shim in, stock kept
        with open(self.slr_dll, "rb") as f:
            self.assertEqual(f.read(), SHIM_NEW)
        with open(self.slr_dll + ".stock", "rb") as f:
            self.assertEqual(f.read(), STOCK)
        self.assertEqual(stat.S_IMODE(os.stat(self.slr_dll).st_mode), 0o644)
        self.assertIn(xess.HOOK_NOTE, r["notes"])
        # settings; the hand install stays where it is
        s = self.settings.all()
        self.assertEqual((s["xess_package"], s["xess_tag"]), (new, TAG_NEW))
        self.assertEqual(s["xess_old_folders"], [os.path.realpath(self.old)])
        self.assertTrue(s["xess_shim_system"])
        self.assertIn("xess_installed_at", s)
        self.assertTrue(os.path.isfile(os.path.join(self.old, "install.sh")))
        x = xess.state(self.ctx, self.settings)["xess"]
        self.assertEqual((x["package_dir"], x["version"], x["managed"]), (new, TAG_NEW, True))
        self.assertEqual(x["old_folders"], [os.path.realpath(self.old)])
        self.assertTrue(x["features"]["fsr"])
        self.assertEqual((x["missing_downloads"], x["repair"], x["latest"], x["update_available"]),
                         ([], None, None, False))
        self.assertIsNone(x["script_running"])
        self.assertIs(r["reboot_needed"], False)

    def test_install_failure_rolls_back(self):
        self.owner()
        new = self.standard_scripts()
        self.script(os.path.join(new, "install.sh"), rc=1, out="missing x - run this from the unpacked package\n")
        self.script(os.path.join(self.old, "install.sh"), out="done.\n",
                    effect=self.fake_install_of(self.old, "26.3.0-rc2"))
        r = xess.install(self.ctx, self.settings, self.job)
        self.assertFalse(r["ok"])
        self.assertIn("install.sh failed (exit 1)", r["error"])
        self.assertIn("missing x", r["error"])
        self.assertTrue(r["rolled_back"])
        self.assert_in_order([f"bash {self.old}/uninstall.sh", f"bash {new}/install.sh", f"bash {self.old}/install.sh"])
        self.assertTrue(all(c["detach"] for c in self.u.calls if c["argv"][0] == "bash"))     # the rollback too
        self.assertNotIn(f"bash {new}/tools/switch-driver.sh 26.1", self.u.commands())
        self.assertIsNone(self.settings.get("xess_package"))
        self.assertEqual(self.settings.get("xess_old_folders"), [new])       # can be removed from the page
        with open(self.slr_dll, "rb") as f:
            self.assertEqual(f.read(), STOCK)                                # root changed nothing
        self.assertEqual(xess.detect(self.ctx, self.settings)["path"], os.path.realpath(self.old))

    def test_uninstall_failure_stops_before_install(self):
        self.owner()
        new = self.standard_scripts()
        self.script(os.path.join(self.old, "uninstall.sh"), rc=2, out="boom\n")
        r = xess.install(self.ctx, self.settings, self.job)
        self.assertFalse(r["ok"])
        self.assertIn("uninstall.sh of", r["error"])
        cmds = self.u.commands()
        self.assertNotIn(f"bash {new}/install.sh", cmds)
        self.assert_in_order(["systemctl stop", f"bash {self.old}/uninstall.sh", "systemctl start"])
        self.assertTrue(os.path.isdir(new))                     # the new folder stays
        self.assertEqual(self.settings.get("xess_old_folders"), [new])

    def test_refused_while_a_game_runs(self):
        self.owner()
        self.w.reaper("292030")
        r = xess.install(self.ctx, self.settings, self.job)
        self.assertFalse(r["ok"])
        self.assertIn("a game is running (Steam app 292030)", r["error"])
        self.assertEqual((self.u.calls, self.downloads), ([], []))
        r = xess.uninstall(self.ctx, self.settings, self.job)
        self.assertIn("a game is running", r["error"])
        self.assertEqual(self.u.calls, [])

    def test_refused_with_only_flatpak_steam(self):
        os.makedirs(os.path.join(self.home, ".var/app/com.valvesoftware.Steam"))
        r = xess.install(self.ctx, self.settings, self.job)
        self.assertFalse(r["ok"])
        self.assertIn("only Flatpak Steam", r["error"])
        self.assertEqual(self.downloads, [])

    def test_refused_without_tools(self):
        self.owner()
        self.missing |= {"ar", "zstd", "vulkaninfo"}
        r = xess.install(self.ctx, self.settings, self.job)
        self.assertFalse(r["ok"])
        self.assertIn("ar (sudo pacman -S binutils)", r["error"])
        self.assertIn("zstd (sudo pacman -S zstd)", r["error"])
        self.assertNotIn("vulkaninfo", r["error"])           # optional
        write(self.w.sys("/etc/os-release"), "ID=bazzite\nID_LIKE=\"fedora\"\n")
        r = xess.install(self.ctx, self.settings, self.job)
        self.assertIn("ar (rpm-ostree install binutils, then reboot)", r["error"])
        self.assertEqual(self.downloads, [])

    def test_up_to_date(self):
        self.owner()
        self.latest = TAG_OLD
        self.u.units = UNITS_ON
        r = xess.install(self.ctx, self.settings, self.job)
        self.assertEqual((r["ok"], r["up_to_date"], r["package_dir"]), (True, True, os.path.realpath(self.old)))
        self.assertEqual((self.u.commands(), self.downloads), (["systemctl show"], []))   # the units asked only

    def test_same_release_with_a_failed_check_runs_install_again(self):
        self.owner()
        self.latest = TAG_OLD
        write(os.path.join(self.home, xess.STATUS_FILE), f"FAILED 2026-11-02 08:00:01 {self.old}/lib/mesa-26.3.0-rc2/"
                                                         "libvulkan_intel.so: cannot be loaded\n")
        self.script(os.path.join(self.old, "install.sh"), out="done.\n",
                    effect=self.fake_install_of(self.old, "26.3.0-rc2"))
        r = xess.install(self.ctx, self.settings, self.job)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.downloads, [])
        self.assertEqual([c for c in self.u.commands() if c.startswith("bash")], [f"bash {self.old}/install.sh"])
        self.assertEqual(self.settings.get("xess_package"), os.path.realpath(self.old))
        self.assertEqual(self.settings.get("xess_old_folders") or [], [])
        # the same, failing: the watcher is started again, nothing is listed for removal
        self.u.calls.clear()
        write(os.path.join(self.home, xess.STATUS_FILE), "FAILED 2026-11-02 08:00:01 x/libvulkan_intel.so: no\n")
        self.script(os.path.join(self.old, "install.sh"), rc=1, out="boom\n")
        r = xess.install(self.ctx, self.settings, self.job)
        self.assertFalse(r["ok"])
        self.assertEqual(self.u.commands()[-1], "systemctl start")
        self.assertEqual(self.settings.get("xess_old_folders") or [], [])

    def test_fresh_install(self):
        self.w.make_steam()
        self.zips[self.url(TAG_NEW)] = release_zip(TAG_NEW)
        new = os.path.realpath(self.new)
        self.script(os.path.join(new, "install.sh"), effect=self.fake_install_of(new))
        r = xess.install(self.ctx, self.settings, self.job)
        self.assertTrue(r["ok"], r)
        cmds = self.u.commands()
        self.assertEqual([c for c in cmds if c.startswith("bash")], [f"bash {new}/install.sh"])
        self.assertFalse([c for c in cmds if c.startswith("cp ")])
        self.assertIsNone(r["previous"])
        self.assertEqual(self.settings.get("xess_old_folders"), [])
        self.assertNotIn("xess_shim_system", self.settings.all())     # no system Proton: nothing to shim

    def test_earlier_unpacked_copy_is_replaced(self):
        self.owner()
        new = self.standard_scripts()
        make_package(new, TAG_NEW)
        write(os.path.join(new, "stale.txt"), "old")
        r = xess.install(self.ctx, self.settings, self.job)
        self.assertTrue(r["ok"], r)
        self.assertFalse(os.path.exists(os.path.join(new, "stale.txt")))
        self.assert_in_order(["python3 unpack", f"rm {self.new}",
                              f"mv {self.incoming}/xess-xmx-linux-{TAG_NEW} {self.new}"])

    def test_copy_in_use_is_not_replaced(self):
        self.owner()
        new = self.standard_scripts()
        make_package(new, TAG_NEW)
        self.w.steam_client({"VK_DRIVER_FILES": f"{new}/run/intel_icd.json"})
        r = xess.install(self.ctx, self.settings, self.job)
        self.assertFalse(r["ok"])
        self.assertIn("still used by the running session", r["error"])
        self.assertNotIn(f"bash {self.old}/uninstall.sh", self.u.commands())

    def test_bad_archives(self):
        self.owner()
        self.standard_scripts()
        cases = {
            "wrong top": release_zip(TAG_NEW, top="xess-xmx-linux-20261031"),
            "two tops": release_zip(TAG_NEW, extra={"other/x": b"x"}),
            "not a zip": b"<html>rate limited</html>",
        }
        for why, data in cases.items():
            with self.subTest(why):
                self.u.calls.clear()
                self.zips[self.url(TAG_NEW)] = data
                r = xess.install(self.ctx, self.settings, self.job)
                self.assertFalse(r["ok"], why)
                self.assertTrue(r["error"].startswith("xess install: "))
                self.assertFalse([c for c in self.u.commands() if c.startswith(("bash", "python3"))])
                self.assertEqual(os.listdir(self.tmp_parent), [])
        self.latest = downloads.DownloadError("latest release of x: offline")
        self.assertIn("offline", xess.install(self.ctx, self.settings, self.job)["error"])
        self.latest = "../evil"
        self.assertIn("unexpected release tag", xess.install(self.ctx, self.settings, self.job)["error"])

    def test_reuse_igc_when_get_igc_is_unchanged(self):
        self.owner()
        new = self.standard_scripts()
        # an old package of the same series: get-igc.sh identical, igc/ complete
        shutil.copy(os.path.join(os.path.dirname(__file__), "fixtures", "xess", TAG_NEW, "tools", "get-igc.sh"),
                    os.path.join(self.old, "tools", "get-igc.sh"))
        r = xess.install(self.ctx, self.settings, self.job)
        self.assertTrue(r["ok"], r)
        self.assertIn(f"cp {self.old}/igc {new}/igc", self.u.commands())
        self.assertTrue(os.access(os.path.join(new, "igc", "neo", "bin", "ocloc-25.18.1"), os.X_OK))

    def test_no_reuse_of_an_incomplete_igc(self):
        self.owner()
        new = self.standard_scripts()
        shutil.copy(os.path.join(os.path.dirname(__file__), "fixtures", "xess", TAG_NEW, "tools", "get-igc.sh"),
                    os.path.join(self.old, "tools", "get-igc.sh"))
        os.chmod(os.path.join(self.old, "igc", "neo", "bin", "ocloc-25.18.1"), 0o644)   # install.sh's -x test fails
        self.assertTrue(xess.install(self.ctx, self.settings, self.job)["ok"])
        self.assertNotIn(f"cp {self.old}/igc {new}/igc", self.u.commands())

    def test_busy(self):
        self.owner()
        with xess._BUSY:
            import threading
            out = {}
            t = threading.Thread(target=lambda: out.update(r=xess.install(self.ctx, self.settings, self.job)))
            t.start()
            t.join()
        self.assertIn("another XeSS task is running", out["r"]["error"])


class ExtractScriptTest(unittest.TestCase):
    """The unpack script on its own (the user's process checks the names again)."""

    def run_script(self, data, top, limit=1 << 30):
        with tempfile.TemporaryDirectory() as d:
            z = os.path.join(d, "r.zip")
            with open(z, "wb") as f:
                f.write(data)
            dest = os.path.join(d, "out")
            os.mkdir(dest)
            r = subprocess.run([sys.executable, "-I", "-c", xess._EXTRACT, z, dest, top, str(limit)],
                               capture_output=True, text=True)
            files = sorted(os.path.relpath(os.path.join(p, n), dest) for p, _, ns in os.walk(dest) for n in ns)
            return r, files

    def test_rejects(self):
        top = "xess-xmx-linux-1"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(f"{top}/ok.txt", b"x")
            z.writestr("evil/../../x", b"x")
        r, files = self.run_script(buf.getvalue(), top)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unsafe entry", r.stderr)
        self.assertEqual(files, [])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            zi = zipfile.ZipInfo(f"{top}/link")
            zi.create_system = 3
            zi.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(zi, b"/etc/passwd")
        r, files = self.run_script(buf.getvalue(), top)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(files, [])

    def test_size_limit_and_modes(self):
        data = release_zip(TAG_NEW)
        top = f"xess-xmx-linux-{TAG_NEW}"
        r, files = self.run_script(data, top, limit=100)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unpacks to", r.stderr)
        r, files = self.run_script(data, top)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"{top}/install.sh", files)
        self.assertIn("unpacked", r.stdout)


class UninstallTest(XessCase):
    def test_uninstall(self):
        self.owner()
        os.replace(self.slr_dll, self.slr_dll + ".stock")          # shimmed by root earlier
        write(self.slr_dll, SHIM_OLD, 0o644)
        self.script(os.path.join(self.old, "uninstall.sh"), out="restored 3 igdext64.dll file(s)\n",
                    effect=self.fake_uninstall)
        r = xess.uninstall(self.ctx, self.settings, self.job)
        self.assertTrue(r["ok"], r)
        self.assert_in_order(["systemctl stop", f"bash {self.old}/uninstall.sh"])
        self.assertTrue(next(c for c in self.u.calls if c["argv"][0] == "bash")["detach"])
        self.assertEqual(r["restored"], [self.slr_dll])
        with open(self.slr_dll, "rb") as f:
            self.assertEqual(f.read(), STOCK)
        self.assertFalse(os.path.exists(self.slr_dll + ".stock"))
        self.assertEqual(r["next"], xess.NEXT_UNINSTALL)
        self.assertIsNone(self.settings.get("xess_package"))
        self.assertEqual(self.settings.get("xess_old_folders"), [os.path.realpath(self.old)])
        self.assertTrue(os.path.isfile(os.path.join(self.old, "install.sh")))       # never deleted
        self.assertFalse(xess.detect(self.ctx, self.settings)["installed"])

    def test_uninstall_failure(self):
        self.owner()
        self.script(os.path.join(self.old, "uninstall.sh"), rc=1, out="no\n")
        r = xess.uninstall(self.ctx, self.settings, self.job)
        self.assertFalse(r["ok"])
        self.assertIn("uninstall.sh failed (exit 1)", r["error"])
        self.assertEqual(self.u.commands()[-1], "systemctl start")

    def test_nothing_to_uninstall(self):
        r = xess.uninstall(self.ctx, self.settings, self.job)
        self.assertEqual(r["error"], "xess uninstall: xess-xmx-linux is not installed")


class SetDriverTest(XessCase):
    def test_validation(self):
        self.owner()
        for bad in ("", "26.1; rm -rf /", "../26", "a.b", "26.", "26..1", "-1", "26 .1", None, 26):
            r = xess.set_driver(self.ctx, self.settings, bad)
            self.assertFalse(r["ok"], bad)
            self.assertIn("not a driver version", r["error"])
        r = xess.set_driver(self.ctx, self.settings, "27")
        self.assertIn("no Mesa 27 driver", r["error"])
        self.assertEqual(len(r["drivers"]), 2)
        self.assertEqual(self.u.calls, [])

    def test_switch(self):
        self.owner()
        self.script(os.path.join(self.old, "tools", "switch-driver.sh"), out="== default: Mesa 26.1.2.\n",
                    effect=self.fake_switch(self.old))
        r = xess.set_driver(self.ctx, self.settings, "26.1")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["drivers"], [{"version": "26.1.2", "default": True},
                                        {"version": "26.3.0-rc2", "default": False}])
        self.assertEqual(r["output"], ["== default: Mesa 26.1.2."])
        c = self.u.calls[-1]
        self.assertEqual(c["argv"], ["bash", os.path.join(os.path.realpath(self.old), "tools", "switch-driver.sh"),
                                     "26.1"])
        self.assertEqual(c["cwd"], os.path.realpath(self.old))
        self.assertEqual(self.settings.get("xess_driver"), "26.1")

    def test_switch_failure_keeps_the_choice(self):
        self.owner()
        self.settings.set("xess_driver", "26.3")
        self.script(os.path.join(self.old, "tools", "switch-driver.sh"), rc=1, out="!! Mesa 26.1.2 failed its check\n")
        r = xess.set_driver(self.ctx, self.settings, "26.1")
        self.assertFalse(r["ok"])
        self.assertIn("failed its check", r["error"])
        self.assertEqual(self.settings.get("xess_driver"), "26.3")

    def test_not_installed(self):
        self.assertIn("not installed", xess.set_driver(self.ctx, self.settings, "26.1")["error"])


class SystemProtonTest(XessCase):
    def setUp(self):
        super().setUp()
        self.owner()
        self.native = self.w.proton("proton-cachyos-native", "system", dll=STOCK)
        self.native_dll = os.path.join(self.native, xess.IGDEXT_REL)
        write(os.path.join(self.native, "user_settings.py"), "# the package's own\n")

    def read(self, p):
        with open(p, "rb") as f:
            return f.read()

    def test_shim_and_unshim(self):
        os.chmod(self.slr_dll, 0o640)
        r = xess.shim_system_protons(self.ctx, self.settings)
        self.assertTrue(r["ok"], r)
        self.assertEqual(sorted(r["done"]), sorted([self.slr_dll, self.native_dll]))
        self.assertEqual(r["failed"], [])
        self.assertIn(xess.HOOK_NOTE, r["notes"])
        for f in (self.slr_dll, self.native_dll):
            self.assertEqual(self.read(f), SHIM_OLD)
            self.assertEqual(self.read(f + ".stock"), STOCK)
            self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(os.stat(self.slr_dll + ".stock").st_mode), 0o640)    # cp -p
        self.assertEqual(self.read(os.path.join(self.native, "user_settings.py")), b"# the package's own\n")
        self.assertTrue(self.settings.get("xess_shim_system"))
        self.assertFalse(xess.system_protons_need_shim(self.ctx, self.settings))
        # again: nothing to do
        r = xess.shim_system_protons(self.ctx, self.settings)
        self.assertEqual((r["done"], sorted(r["already"])), ([], sorted([self.slr_dll, self.native_dll])))
        # a package update brings a new stock file: needs the shim again, and .stock follows it
        os.unlink(self.slr_dll)
        write(self.slr_dll, STOCK2, 0o644)
        self.assertTrue(xess.system_protons_need_shim(self.ctx, self.settings))
        x = xess.state(self.ctx, self.settings)["xess"]
        self.assertTrue(x["system_protons_need_shim"])
        r = xess.shim_system_protons(self.ctx, self.settings)
        self.assertEqual(r["done"], [self.slr_dll])
        self.assertEqual(self.read(self.slr_dll + ".stock"), STOCK2)
        # an update of the package's shim replaces the old shim (no new .stock)
        write(os.path.join(self.old, "igdext64.dll"), SHIM_NEW)
        r = xess.shim_system_protons(self.ctx, self.settings)
        self.assertEqual(len(r["done"]), 2)
        self.assertEqual(self.read(self.slr_dll + ".stock"), STOCK2)
        # back to stock
        r = xess.unshim_system_protons(self.ctx)
        self.assertTrue(r["ok"], r)
        self.assertEqual(sorted(r["done"]), sorted([self.slr_dll, self.native_dll]))
        self.assertEqual(self.read(self.slr_dll), STOCK2)
        self.assertEqual(self.read(self.native_dll), STOCK)
        self.assertFalse(os.path.exists(self.slr_dll + ".stock"))
        self.assertEqual(xess.unshim_system_protons(self.ctx)["done"], [])

    def test_restorecon_when_present(self):
        self.missing.discard("restorecon")
        with mock.patch.object(sysexec, "run", return_value=sysexec.Result(0, "", "")) as run:
            r = xess.shim_system_protons(self.ctx, self.settings)
        self.assertTrue(r["ok"])
        argvs = [c.args[0] for c in run.call_args_list]
        self.assertIn(["/usr/bin/restorecon", self.slr_dll, self.slr_dll + ".stock"], argvs)

    def test_user_writable_folder_is_left_alone(self):
        os.chmod(os.path.dirname(self.slr_dll), 0o777)
        r = xess.shim_system_protons(self.ctx, self.settings)
        self.assertFalse(r["ok"])
        self.assertEqual(r["done"], [self.native_dll])
        self.assertIn("writable by others than root", r["failed"][0])
        self.assertEqual(self.read(self.slr_dll), STOCK)

    def test_symlinked_folder_is_left_alone(self):
        real = os.path.join(self.tmp.name, "elsewhere")
        shutil.move(os.path.dirname(self.slr_dll), real)
        os.symlink(real, os.path.dirname(self.slr_dll))
        r = xess.shim_system_protons(self.ctx, self.settings)
        self.assertIn("is not a plain folder", " ".join(r["failed"]))
        self.assertEqual(self.read(os.path.join(real, "igdext64.dll")), STOCK)

    def test_needs_a_real_shim(self):
        write(os.path.join(self.old, "igdext64.dll"), b"not a shim at all")
        r = xess.shim_system_protons(self.ctx, self.settings)
        self.assertFalse(r["ok"])
        self.assertIn("is not the shim", r["error"])
        self.assertEqual(self.read(self.slr_dll), STOCK)

    def test_not_installed(self):
        os.unlink(os.path.join(self.home, xess.ENV_FILE))
        os.unlink(os.path.join(self.home, xess.UNIT_DIR, xess.CHECK_UNIT))
        self.assertIn("not installed", xess.shim_system_protons(self.ctx, self.settings)["error"])
        self.settings.set("xess_shim_system", True)
        self.assertFalse(xess.system_protons_need_shim(self.ctx, self.settings))


class RemoveFolderTest(XessCase):
    def setUp(self):
        super().setUp()
        self.owner()
        self.older = make_package(os.path.join(self.xroot, "xess-xmx-linux-20261029"), TAG_OLD)
        self.settings.set("xess_old_folders", [self.older, self.old, "/etc", self.home])

    def test_guards(self):
        c, s = self.ctx, self.settings
        self.assertIn("not one of the earlier", xess.remove_folder(c, s, os.path.join(self.home, "Documents"))["error"])
        self.assertIn("is the package in use", xess.remove_folder(c, s, self.old)["error"])
        self.assertIn("is outside", xess.remove_folder(c, s, "/etc")["error"])
        self.assertIn("is outside", xess.remove_folder(c, s, self.home)["error"])
        self.assertIn("not a folder", xess.remove_folder(c, s, "relative/path")["error"])
        os.unlink(os.path.join(self.older, "uninstall.sh"))
        self.assertIn("does not look like", xess.remove_folder(c, s, self.older)["error"])
        self.assertTrue(os.path.isdir(self.older))
        self.assertFalse([x for x in self.u.commands() if x.startswith("rm")])

    def test_steam_still_uses_it(self):
        self.w.steam_client({"VK_DRIVER_FILES": f"{self.older}/run/intel_icd.json:/x.json"})
        r = xess.remove_folder(self.ctx, self.settings, self.older)
        self.assertIn("Steam still runs with the driver", r["error"])
        self.assertTrue(os.path.isdir(self.older))

    def test_session_still_uses_it(self):
        self.u.session_env = f"VK_DRIVER_FILES={self.older}/run/intel_icd.json\n"
        self.assertIn("the session still uses", xess.remove_folder(self.ctx, self.settings, self.older)["error"])

    def test_remove(self):
        self.w.steam_client({"VK_DRIVER_FILES": f"{self.old}/run/intel_icd.json"})
        self.u.session_env = f"VK_DRIVER_FILES={self.old}/run/intel_icd.json\n"
        r = xess.remove_folder(self.ctx, self.settings, self.older + "/")
        self.assertTrue(r["ok"], r)
        self.assertFalse(os.path.exists(self.older))
        self.assertEqual(self.u.calls[-1]["argv"], ["rm", "-rf", "--", os.path.realpath(self.older)])
        self.assertEqual(self.settings.get("xess_old_folders"), [self.old, "/etc", self.home])

    def test_own_work_is_left_to_the_user(self):
        # XESS-6: a clone of the repository (docs/building.md) has install.sh, uninstall.sh and session-check.sh too
        clone = make_package(os.path.join(self.home, "src", "xess-xmx-linux"), TAG_OLD)
        os.makedirs(os.path.join(clone, ".git"))
        named_clone = make_package(os.path.join(self.home, "Documents", "xess-xmx-linux-20261020"), TAG_OLD)
        write(os.path.join(named_clone, ".git"), "gitdir: /elsewhere\n")         # a worktree: .git is a file
        build = make_package(os.path.join(self.home, "xess-build"), TAG_OLD)
        renamed = make_package(os.path.join(self.xroot, "my-xess"), TAG_OLD)   # the plugin's own folder
        self.settings.set("xess_old_folders", [clone, named_clone, build, renamed])
        for folder, why in ((clone, "is a git checkout"), (named_clone, "is a git checkout"),
                            (build, "is not named xess-xmx-linux-<tag>")):
            with self.subTest(folder):
                r = xess.remove_folder(self.ctx, self.settings, folder)
                self.assertFalse(r["ok"])
                self.assertIn(why, r["error"])
                self.assertIn("delete it yourself", r["error"])
                self.assertTrue(os.path.isfile(os.path.join(folder, "install.sh")))
        self.assertFalse([x for x in self.u.commands() if x.startswith("rm")])
        # RC-2: the XeSS tab offers Remove only where it can work; the others are listed as the user's to delete
        x = xess.state(self.ctx, self.settings)["xess"]
        self.assertEqual(x["old_folders"], [renamed])
        self.assertEqual([(k["path"], k["why"].split(";")[0]) for k in x["old_folders_kept"]],
                         [(clone, "is a git checkout, not just an unpacked release"),
                          (named_clone, "is a git checkout, not just an unpacked release"),
                          (build, "is not named xess-xmx-linux-<tag> like an unpacked release and was not installed "
                                  "by OXP3 Deck")])
        self.assertTrue(xess.remove_folder(self.ctx, self.settings, renamed)["ok"])

    def test_an_update_from_a_checkout_leaves_it_to_the_user(self):
        """RC-2: after an update from a hand install in a git checkout, the checkout was listed with a Remove button
        that failed every time."""
        clone = make_package(os.path.join(self.home, "src", "xess-xmx-linux"), TAG_OLD)
        os.makedirs(os.path.join(clone, ".git"))
        write(os.path.join(self.home, xess.ENV_FILE), env_text(clone))
        write(os.path.join(self.home, xess.UNIT_DIR, xess.CHECK_UNIT),
              f'[Unit]\n[Service]\nExecStart=/bin/bash "{clone}/tools/session-check.sh"\n')
        write(os.path.join(self.home, xess.STATUS_FILE), ok_line(clone))
        self.settings.set("xess_old_folders", [])
        before = xess.state(self.ctx, self.settings)["xess"]
        self.assertEqual(before["package_dir"], clone)
        self.assertIn("is a git checkout", before["package_dir_kept"])     # the update dialog says so
        new_real = os.path.realpath(self.new)
        self.script(os.path.join(clone, "uninstall.sh"), effect=self.fake_uninstall)
        self.script(os.path.join(new_real, "install.sh"), effect=self.fake_install_of(new_real))
        self.assertTrue(xess.install(self.ctx, self.settings, self.job)["ok"])
        x = xess.state(self.ctx, self.settings)["xess"]
        self.assertEqual(x["old_folders"], [])
        self.assertEqual([k["path"] for k in x["old_folders_kept"]], [clone])
        self.assertIsNone(x["package_dir_kept"])             # the plugin's own folder can go once it is old
        self.assertIn("is a git checkout", xess.remove_folder(self.ctx, self.settings, clone)["error"])

    def test_not_the_only_compiler(self):
        # XESS-1: the installed package has no igc/ (its download failed), this folder has one
        write(os.path.join(self.older, "igc", "neo", "bin", "ocloc-25.18.1"), b"ocloc", 0o755)
        newer_pkg = make_package(self.new, TAG_NEW, shim=SHIM_NEW)
        fetch_piece(newer_pkg, "xess")
        fetch_piece(newer_pkg, "ffx")
        write(os.path.join(self.home, xess.ENV_FILE), env_text(newer_pkg))
        write(os.path.join(self.home, xess.STATUS_FILE), ok_line(newer_pkg))
        r = xess.remove_folder(self.ctx, self.settings, self.older)
        self.assertIn("has the only compiler (igc/)", r["error"])
        self.assertNotIn(self.older, xess.state(self.ctx, self.settings)["xess"]["old_folders"])
        fetch_piece(newer_pkg, "igc")
        self.assertIn(self.older, xess.state(self.ctx, self.settings)["xess"]["old_folders"])
        self.assertTrue(xess.remove_folder(self.ctx, self.settings, self.older)["ok"])


class DownloadsTest(XessCase):
    """XESS-1: install.sh exits 0 when a download fails; the plugin reuses what it can and says what is missing."""

    def setUp(self):
        super().setUp()
        self.owner()
        self.new_real = os.path.realpath(self.new)
        self.script(os.path.join(self.old, "uninstall.sh"), effect=self.fake_uninstall)

    def test_same_downloads_of_the_real_scripts(self):
        old_igc, new_igc = fixture(TAG_OLD, "tools", "get-igc.sh"), fixture(TAG_NEW, "tools", "get-igc.sh")
        pins = {sub: p for sub, _s, _f, p in xess.PIECES}
        self.assertNotEqual(old_igc, new_igc)
        self.assertEqual(xess._same_downloads("get-igc.sh", pins["igc"], old_igc, new_igc),
                         "get-igc.sh pins the same downloads")
        self.assertIsNone(xess._same_downloads("get-igc.sh", pins["igc"], old_igc,
                                               new_igc.replace(b"25.18.33578.6", b"25.40.1")))
        x = fixture(TAG_OLD, "tools", "get-xess.sh")

        def same_xess(b):
            return xess._same_downloads("get-xess.sh", pins["xess"], x, b)
        self.assertEqual(same_xess(fixture(TAG_NEW, "tools", "get-xess.sh")), "get-xess.sh is unchanged")
        self.assertEqual(same_xess(x.replace(b"# Needs curl", b"# Uses curl")), "get-xess.sh pins the same downloads")
        self.assertIsNone(same_xess(x.replace(b"libxell.dll", b"libxell2.dll")))     # another library taken
        f = fixture(TAG_NEW, "tools", "get-ffx.sh")
        self.assertEqual(len(pins["ffx"].findall(f)), 5)                # FFX_TAG and the four files with checksums
        self.assertIsNone(xess._same_downloads("get-ffx.sh", pins["ffx"], f, f.replace(b"e2191d3c", b"00000000")))
        self.assertIsNone(xess._same_downloads("get-ffx.sh", pins["ffx"], None, f))

    def test_compiler_download_failed(self):
        # another IGC pinned: nothing to reuse, and get-igc.sh hits GitHub's rate limit
        p = os.path.join(self.old, "tools", "get-igc.sh")
        write(p, fixture(TAG_OLD, "tools", "get-igc.sh").replace(b"25.18.33578.6", b"25.13.1"), 0o755)
        self.script(os.path.join(self.new_real, "install.sh"), out="compiler download failed - the run-time kernel "
                    "path will be unavailable\n", effect=self.fake_install_of(self.new_real, fail=("igc",)))
        r = xess.install(self.ctx, self.settings, self.job)
        self.assertTrue(r["ok"], r)
        self.assertNotIn(f"cp {self.old}/igc {self.new_real}/igc", self.u.commands())
        self.assertIn("igc/: tools/get-igc.sh differs in this release, so install.sh downloads it again", self.job.log)
        self.assertEqual(len(r["warnings"]), 1)
        self.assertIn("the compiler (igc/) was not downloaded", r["warnings"][0])
        self.assertIn(f"{os.path.realpath(self.old)} keeps its compiler until then", r["warnings"][0])
        x = xess.state(self.ctx, self.settings)["xess"]
        self.assertEqual(x["missing_downloads"], ["igc"])
        self.assertEqual(x["repair"], "the compiler (igc/) was not downloaded")
        self.assertEqual(x["old_folders"], [])                          # the only compiler stays out of reach
        self.assertEqual(self.settings.get("xess_old_folders"), [os.path.realpath(self.old)])
        # Repair: the same release, install.sh again in its folder; this time the download works
        self.u.calls.clear()
        self.script(os.path.join(self.new_real, "install.sh"), effect=self.fake_install_of(self.new_real))
        r = xess.install(self.ctx, self.settings, self.job)
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(self.downloads), 1)                        # no second release download
        self.assertEqual([c for c in self.u.commands() if c.startswith("bash")], [f"bash {self.new_real}/install.sh"])
        self.assertEqual(self.fetched, ["igc"])
        self.assertEqual((r["warnings"], r["previous"]), ([], None))
        x = xess.state(self.ctx, self.settings)["xess"]
        self.assertEqual((x["missing_downloads"], x["repair"]), ([], None))
        self.assertEqual(x["old_folders"], [os.path.realpath(self.old)])

    def test_repair_copies_the_compiler_from_an_old_folder(self):
        self.script(os.path.join(self.new_real, "install.sh"), effect=self.fake_install_of(self.new_real))
        self.assertTrue(xess.install(self.ctx, self.settings, self.job)["ok"])
        shutil.rmtree(os.path.join(self.new_real, "igc"))           # as if the copy and the download had failed
        self.assertEqual(xess.state(self.ctx, self.settings)["xess"]["missing_downloads"], ["igc"])
        self.u.calls.clear()
        r = xess.install(self.ctx, self.settings, self.job)
        self.assertTrue(r["ok"], r)
        self.assert_in_order([f"cp {self.old}/igc {self.new_real}/igc", f"bash {self.new_real}/install.sh"])
        self.assertEqual(self.fetched, [])


class RepairTest(XessCase):
    """XESS-2: up to date only when install.sh got to its end for this package."""

    def setUp(self):
        super().setUp()
        self.owner()
        self.pkg = os.path.realpath(make_package(self.new, TAG_NEW, shim=SHIM_NEW))
        for sub in ("igc", "xess", "ffx"):
            fetch_piece(self.pkg, sub)
        # install.sh of 20261104 cut short after its environment file: the check line is still the old package's
        write(os.path.join(self.home, xess.ENV_FILE), env_text(self.pkg, alt=True))
        os.unlink(os.path.join(self.home, xess.UNIT_DIR, xess.CHECK_UNIT))
        self.u.units = UNITS_GONE
        self.latest = TAG_NEW

    def reason(self):
        det = xess.detect(self.ctx, self.settings)
        return xess._repair_reason(det, xess.read_status(self.ctx), xess._units(self.ctx),
                                   xess._missing_downloads(det["path"]))

    def test_reasons(self):
        self.assertEqual(self.reason(), "the driver check belongs to another folder")
        write(os.path.join(self.home, xess.STATUS_FILE), ok_line(self.pkg))
        self.assertEqual(self.reason(), "the login check unit is missing")
        self.u.units = UNITS_ON
        self.assertIsNone(self.reason())
        write(os.path.join(self.home, xess.STATUS_FILE), failed_line(self.pkg))
        self.assertEqual(self.reason(), "the driver check failed")
        os.unlink(os.path.join(self.home, xess.STATUS_FILE))
        self.assertEqual(self.reason(), "the driver check has not run for this package")
        shutil.rmtree(os.path.join(self.pkg, "xess"))
        shutil.rmtree(os.path.join(self.pkg, "ffx"))
        self.assertEqual(self.reason(), "Intel's XeSS SDK (xess/) and AMD's FidelityFX SDK (ffx/) were not downloaded")
        # found by the check unit only: the environment file names a folder that is gone
        for sub in ("xess", "ffx"):
            fetch_piece(self.pkg, sub)
        write(os.path.join(self.home, xess.STATUS_FILE), ok_line(self.pkg))
        write(os.path.join(self.home, xess.ENV_FILE), env_text("/gone/xess-xmx-linux-1"))
        write(os.path.join(self.home, xess.UNIT_DIR, xess.CHECK_UNIT),
              f'ExecStart=/bin/bash "{self.pkg}/tools/session-check.sh"\n')
        self.assertEqual(self.reason(), f"{xess.ENV_FILE} does not name this package")

    def test_interrupted_update_is_repaired_in_place(self):
        x = xess.state(self.ctx, self.settings)["xess"]
        self.assertEqual((x["installed"], x["version"]), (True, TAG_NEW))
        self.assertEqual(x["repair"], "the driver check belongs to another folder")
        self.script(os.path.join(self.pkg, "install.sh"), effect=self.fake_install_of(self.pkg))
        r = xess.install(self.ctx, self.settings, self.job)
        self.assertTrue(r["ok"], r)
        self.assertNotIn("up_to_date", r)
        self.assertEqual(self.downloads, [])
        self.assertEqual([c for c in self.u.commands() if c.startswith("bash")], [f"bash {self.pkg}/install.sh"])
        self.assertIn(f"{TAG_NEW} in {self.pkg}: the driver check belongs to another folder; running its install.sh "
                      "again (nothing else is downloaded)", self.job.log)
        self.u.units = UNITS_ON
        self.assertIsNone(xess.state(self.ctx, self.settings)["xess"]["repair"])
        self.assertTrue(xess.install(self.ctx, self.settings, self.job)["up_to_date"])

    def test_repair_works_without_the_release_lookup(self):
        self.latest = downloads.DownloadError("latest release of x: HTTP 403 (GitHub's rate limit?)")
        self.script(os.path.join(self.pkg, "install.sh"), effect=self.fake_install_of(self.pkg))
        r = xess.install(self.ctx, self.settings, self.job)
        self.assertTrue(r["ok"], r)
        self.assertEqual([c for c in self.u.commands() if c.startswith("bash")], [f"bash {self.pkg}/install.sh"])
        self.assertTrue(any(f"repairing the installed {TAG_NEW} anyway" in line for line in self.job.log), self.job.log)
        # nothing to repair: the lookup error is the answer, and nothing runs
        self.u.units = UNITS_ON
        r = xess.install(self.ctx, self.settings, self.job)
        self.assertFalse(r["ok"])
        self.assertIn("rate limit", r["error"])
        self.assertEqual(len([c for c in self.u.commands() if c.startswith("bash")]), 1)

    def test_no_repair_offered_while_an_update_is(self):
        self.settings.set("xess_latest", "20261110")
        x = xess.state(self.ctx, self.settings)["xess"]
        self.assertEqual((x["latest"], x["update_available"], x["repair"]), ("20261110", True, None))


class LatestTest(XessCase):
    """FE-1, XESS-7: the stored tag is an update only when it is newer; install never downgrades."""

    def test_state_latest_only_when_newer(self):
        self.owner()
        for stored, latest, update in (("20261029", None, False), (TAG_OLD, None, False), (TAG_NEW, TAG_NEW, True),
                                       (None, None, False), ("my-build", "my-build", True)):
            with self.subTest(stored):
                self.settings.set("xess_latest", stored)
                x = xess.state(self.ctx, self.settings)["xess"]
                self.assertEqual((x["latest"], x["update_available"]), (latest, update))
        # nothing installed: the newest release is shown, there is nothing to update
        os.unlink(os.path.join(self.home, xess.ENV_FILE))
        os.unlink(os.path.join(self.home, xess.UNIT_DIR, xess.CHECK_UNIT))
        self.settings.set("xess_latest", "20261029")
        x = xess.state(self.ctx, self.settings)["xess"]
        self.assertEqual((x["installed"], x["latest"], x["update_available"]), (False, "20261029", False))

    def test_unknown_version_is_offered_the_update(self):
        self.w.make_steam()
        pkg = make_package(os.path.join(self.home, "xess"), TAG_OLD)
        write(os.path.join(self.home, xess.ENV_FILE), env_text(pkg))
        self.settings.set("xess_latest", TAG_NEW)
        x = xess.state(self.ctx, self.settings)["xess"]
        self.assertEqual((x["version"], x["latest"], x["update_available"]), (None, TAG_NEW, True))
        # never checked: no Repair either (it would promise install.sh in place; install updates such a package)
        self.settings.set("xess_latest", None)
        x = xess.state(self.ctx, self.settings)["xess"]
        self.assertEqual((x["version"], x["update_available"], x["repair"]), (None, False, None))
        self.assertIsNotNone(xess._repair_reason(xess.detect(self.ctx, self.settings), xess.read_status(self.ctx),
                                                 xess._units(self.ctx), []))   # there would be a reason

    def test_install_refuses_a_downgrade(self):
        self.owner()
        self.u.units = UNITS_ON
        self.latest = "20261029"           # GitHub's newest went backwards (a release pulled)
        r = xess.install(self.ctx, self.settings, self.job)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "xess install: the installed 20261031 is newer than the newest release 20261029; "
                                     "nothing was changed")
        self.assertEqual(self.downloads, [])
        self.assertFalse([c for c in self.u.commands() if c != "systemctl show"])
        self.assertEqual(xess.detect(self.ctx, self.settings)["path"], os.path.realpath(self.old))

    def test_newer_installed_package_can_still_be_repaired(self):
        self.owner()
        self.u.units = UNITS_ON
        self.latest = "20261029"
        write(os.path.join(self.home, xess.STATUS_FILE), failed_line(self.old, "26.3.0-rc2"))
        self.script(os.path.join(self.old, "install.sh"), effect=self.fake_install_of(self.old, "26.3.0-rc2"))
        r = xess.install(self.ctx, self.settings, self.job)
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["tag"], self.downloads), (TAG_OLD, []))
        self.assertEqual([c for c in self.u.commands() if c.startswith("bash")], [f"bash {self.old}/install.sh"])


class FailedCheckTest(XessCase):
    """XESS-4: after install.sh's dry run failed, the session check runs for real right away."""

    def setUp(self):
        super().setUp()
        self.owner()
        self.new_real = os.path.realpath(self.new)
        self.script(os.path.join(self.old, "uninstall.sh"), effect=self.fake_uninstall)
        self.script(os.path.join(self.new_real, "install.sh"),
                    effect=self.fake_install_of(self.new_real, status=failed_line(self.new_real)))
        self.restarts = []

    def check_unit(self, rc=0, line=None):
        def fn(argv):
            self.restarts.append(argv)
            if line:
                write(os.path.join(self.home, xess.STATUS_FILE), line)
            return sysexec.Result(rc, "", "Job for xess-xmx-check.service failed." if rc else "")
        self.u.systemctl["restart"] = fn

    def test_switched_to_the_other_driver(self):
        self.check_unit(line=ok_line(self.new_real, "26.1.2").strip() + " (the previous driver failed: libSPIRV-Tools"
                             ".so: cannot open shared object file; switched to Mesa 26.1.2)\n")
        r = xess.install(self.ctx, self.settings, self.job)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.restarts, [["systemctl", "--user", "restart", "xess-xmx-check.service"]])
        restart = next(c for c in self.u.calls if c["argv"][:3] == ["systemctl", "--user", "restart"])
        self.assertEqual(restart["timeout"], 120)
        self.assertEqual(r["status"]["state"], "ok")
        self.assertIn("made the package's Mesa 26.1.2 driver the default instead", r["warnings"][-1])
        self.assertEqual((r["next"], r["reboot_needed"]), (xess.NEXT_INSTALL, False))

    def test_stock_drivers_for_the_next_session(self):
        self.check_unit(line=failed_line(self.new_real, "26.1.2"))
        self.u.session_env = "VK_DRIVER_FILES=/usr/share/vulkan/icd.d/intel_icd.x86_64.json\n"
        r = xess.install(self.ctx, self.settings, self.job)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["status"]["state"], "failed")
        self.assertIn("set the system's stock Intel driver for the next session", r["warnings"][-1])
        self.assertEqual((r["next"], r["reboot_needed"]), (xess.NEXT_STOCK, False))

    def test_reboot_when_the_check_cannot_run_or_did_not_fall_back(self):
        self.check_unit(rc=1)
        r = xess.install(self.ctx, self.settings, self.job)
        self.assertTrue(r["ok"], r)
        self.assertIn("the session check could not run again (Job for xess-xmx-check.service failed.)",
                      r["warnings"][-1])
        self.assertEqual((r["next"], r["reboot_needed"]), (xess.NEXT_REBOOT, True))
        self.assertTrue(r["next"].startswith("Reboot (not just log out)"))
        # it ran, but the user manager still has the failing driver
        self.check_unit()
        self.u.session_env = f"VK_DRIVER_FILES={self.new_real}/run/intel_icd.json\n"
        write(os.path.join(self.home, xess.STATUS_FILE), failed_line(self.new_real))
        self.u.calls.clear()
        r = xess.install(self.ctx, self.settings, self.job)      # same release: install.sh again (the check failed)
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["next"], r["reboot_needed"]), (xess.NEXT_REBOOT, True))
        self.assertIn("did not switch this session to another driver", r["warnings"][-1])


class DetachedScriptTest(XessCase):
    """RT-1: package scripts run detached and are recorded while they run; after a plugin restart nothing touches
    the package until the one the earlier process started has ended."""

    def setUp(self):
        super().setUp()
        self.owner()
        self.record = self.w.sys(f"/run/oxp3deck/{xess.SCRIPT_RECORD}")

        def spawn(argv):
            pid = self.w.proc(["runuser", "-u", "jordan", "--", *argv], comm="runuser")
            return pid, lambda: shutil.rmtree(os.path.join(self.w.sys("/proc"), str(pid)))
        self.u.spawn = spawn

    def earlier_process(self, start=None):
        """A record written by an earlier plugin process, of a runuser that still runs."""
        pid = self.w.proc(["runuser", "-u", "jordan", "--", "bash", f"{self.old}/install.sh"], comm="runuser")
        os.makedirs(os.path.dirname(self.record), mode=0o700, exist_ok=True)
        write(self.record, json.dumps({"pid": pid, "starttime": pid if start is None else start,
                                       "what": f"{self.old}/install.sh", "plugin": os.getpid() + 1}))
        return pid

    def test_recorded_while_running(self):
        seen = {}

        def effect(argv, cwd):
            with open(self.record) as f:
                seen.update(json.load(f))
            seen["mode"] = stat.S_IMODE(os.stat(self.record).st_mode)
            seen["running"] = xess.state(self.ctx, self.settings)["xess"]["script_running"]
        self.script(os.path.join(self.old, "tools", "switch-driver.sh"), effect=effect)
        self.assertTrue(xess.set_driver(self.ctx, self.settings, "26.1")["ok"])
        self.assertTrue(next(c for c in self.u.calls if c["argv"][0] == "bash")["detach"])
        self.assertEqual(seen["what"], f"{os.path.realpath(self.old)}/tools/switch-driver.sh 26.1")
        self.assertEqual((seen["plugin"], seen["mode"]), (os.getpid(), 0o600))
        self.assertEqual(sysexec.proc_starttime(self.ctx, seen["pid"]), None)         # it has ended since
        self.assertIsInstance(seen["starttime"], int)
        self.assertIsNone(seen["running"])              # this process's own job: not "from before the restart"
        self.assertFalse(os.path.lexists(self.record))

    def test_refused_while_an_earlier_one_runs(self):
        pid = self.earlier_process()
        self.w.proton("proton-cachyos-native", "system", dll=STOCK)
        self.settings.set("xess_shim_system", True)
        self.settings.set("xess_old_folders", [self.old])
        want = (f"an xess-xmx-linux script from before the plugin restarted is still running ({self.old}/install.sh); "
                "try again when it has ended")
        for name, fn in (("xess install", lambda: xess.install(self.ctx, self.settings, self.job)),
                         ("xess uninstall", lambda: xess.uninstall(self.ctx, self.settings, self.job)),
                         ("xess set driver", lambda: xess.set_driver(self.ctx, self.settings, "26.1")),
                         ("xess shim system Protons", lambda: xess.shim_system_protons(self.ctx, self.settings)),
                         ("xess remove folder", lambda: xess.remove_folder(self.ctx, self.settings, self.old))):
            with self.subTest(name):
                r = fn()
                self.assertEqual((r["ok"], r["error"]), (False, f"{name}: {want}"))
        self.assertEqual((self.u.calls, self.downloads), ([], []))
        self.assertFalse(xess.system_protons_need_shim(self.ctx, self.settings))   # no start-up re-shim either
        self.assertEqual(xess.state(self.ctx, self.settings)["xess"]["script_running"], f"{self.old}/install.sh")
        # it ends: the record goes, and the package can be changed again
        shutil.rmtree(os.path.join(self.w.sys("/proc"), str(pid)))
        self.assertTrue(xess.system_protons_need_shim(self.ctx, self.settings))
        self.assertIsNone(xess.state(self.ctx, self.settings)["xess"]["script_running"])
        self.assertFalse(os.path.lexists(self.record))

    def test_a_reused_pid_is_not_the_script(self):
        self.earlier_process(start=1)          # the pid now belongs to a process started later
        self.assertIsNone(xess.script_running(self.ctx))
        self.assertFalse(os.path.lexists(self.record))

    def test_an_old_record_found_ended_does_not_take_a_live_one_with_it(self):
        """RS-4: xess_state (no _BUSY) finds an old record ended while a job writes its own live record; the old one
        was removed by name, the live one with it, and a plugin restart then let a second script run beside it."""
        self.owner()
        self.earlier_process(start=1)              # ended (its pid now belongs to a newer process)
        real_alive = sysexec.proc_alive
        paused, go, seen = threading.Event(), threading.Event(), {}

        def alive(ctx, pid, starttime):
            r = real_alive(ctx, pid, starttime)
            if threading.current_thread().name == "state":
                paused.set()
                go.wait(5)                         # between reading the old record and removing it
            return r

        def effect(argv, cwd):                     # switch-driver.sh runs: its record is in place
            go.set()
            state.join(5)
            with open(self.record) as f:
                seen.update(json.load(f))

        self.script(os.path.join(self.old, "tools", "switch-driver.sh"), effect=effect)
        answers = {}
        with mock.patch.object(sysexec, "proc_alive", alive):
            state = threading.Thread(target=lambda: answers.update(state=xess.script_running(self.ctx)), name="state")
            state.start()
            self.assertTrue(paused.wait(5))
            job = threading.Thread(target=lambda: answers.update(job=xess.set_driver(self.ctx, self.settings, "26.1")))
            job.start()
            time.sleep(0.3)                        # the job waits for the record lock (it reached the script before)
            go.set()
            job.join(10)
            state.join(5)
        self.assertIsNone(answers["state"])
        self.assertTrue(answers["job"]["ok"], answers["job"])
        self.assertEqual((seen["what"], seen["plugin"]), (f"{os.path.realpath(self.old)}/tools/switch-driver.sh 26.1",
                                                          os.getpid()))
        self.assertFalse(os.path.lexists(self.record))       # the job removed its own record at the end

    def test_only_the_record_read_is_removed(self):
        os.makedirs(os.path.dirname(self.record), mode=0o700)
        live = {"pid": 7, "starttime": 70, "what": "install.sh", "plugin": 1}
        write(self.record, json.dumps(live))
        with xess._RECORD:
            xess._clear_record(self.ctx, dict(live, pid=8))    # another process's record, replaced since
        self.assertTrue(os.path.lexists(self.record))
        with xess._RECORD:
            xess._clear_record(self.ctx, live)
        self.assertFalse(os.path.lexists(self.record))

    def test_bad_records_are_ignored(self):
        os.makedirs(os.path.dirname(self.record), mode=0o700)
        for raw in ("", "[]", "{", '{"pid": "1", "starttime": 1}', "\xff"):
            with self.subTest(raw):
                write(self.record, raw.encode("latin-1"))
                self.assertIsNone(xess.script_running(self.ctx))
        if os.path.lexists(self.record):
            os.unlink(self.record)
        os.symlink("/etc/passwd", self.record)
        self.assertIsNone(xess.script_running(self.ctx))


if __name__ == "__main__":
    unittest.main()
