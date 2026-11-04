"""xess.py against a fake home, a fake root and a fake desktop user (FakeUser runs mkdir/rm/mv/cp and the unpack
for real in the temporary folders, and fakes systemctl and the package's bash scripts)."""
import hashlib
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

from oxp3deck import context, downloads, jobs, sysexec, xess
from tests.test_steamlib import World, fixture, patch_user, write

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

    def fake_install_of(self, pkg, v="26.3.0-rc3"):
        def effect(argv, cwd):
            self.assertEqual(cwd, pkg)
            write(os.path.join(self.home, xess.ENV_FILE), env_text(pkg, alt=True))
            write(os.path.join(self.home, xess.UNIT_DIR, xess.CHECK_UNIT),
                  f'ExecStart=/bin/bash "{pkg}/tools/session-check.sh"\n')
            write(os.path.join(self.home, xess.STATUS_FILE), ok_line(pkg, v))
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
        self.assertEqual(x["old_folders"], [os.path.join(self.home, "elsewhere")])
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
            f"cp {self.old}/xess {new}/xess",
            "systemctl stop", f"bash {self.old}/uninstall.sh", f"bash {new}/install.sh",
            f"bash {new}/tools/switch-driver.sh 26.1",
        ])
        cmds = self.u.commands()
        self.assertFalse([c for c in cmds if c.startswith("cp ") and c.endswith("/igc")])   # get-igc.sh changed
        self.assertNotIn(f"bash {self.old}/install.sh", cmds)                                # no rollback
        stop = next(c for c in self.u.calls if c["argv"][:3] == ["systemctl", "--user", "stop"])
        self.assertEqual(stop["argv"][3:], ["xess-xmx-shim.path", "xess-xmx-shim.service"])
        for c in self.u.calls:
            if c["argv"][0] == "bash" and c["argv"][1].endswith(("install.sh", "uninstall.sh")):
                self.assertEqual(c["cwd"], os.path.dirname(c["argv"][1]))
                self.assertIsNone(c["timeout"])         # downloads hundreds of MB: never killed
                self.assertTrue(c["streamed"])
        # the download: one URL, a 0700 folder of root's, 200 MB at most; then cleaned up
        self.assertEqual([d["url"] for d in self.downloads], [self.url(TAG_NEW)])
        self.assertEqual(self.downloads[0]["parent_mode"], 0o700)
        self.assertEqual(self.downloads[0]["parent_uid"], os.geteuid())
        self.assertEqual(self.downloads[0]["max_bytes"], 200 << 20)
        self.assertEqual(os.listdir(self.tmp_parent), [])
        self.assertFalse(os.path.lexists(self.incoming))
        self.assertTrue(any("no checksum" in line for line in self.job.log))
        # the unpacked tree: exec bits back, the reused SDK, no igc (install.sh downloads it)
        self.assertTrue(os.stat(os.path.join(new, "xmx-launch.sh")).st_mode & 0o111)
        self.assertTrue(os.stat(os.path.join(new, "tools", "get-igc.sh")).st_mode & 0o111)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(new, "README.md")).st_mode) & 0o111, 0)
        self.assertTrue(os.path.isdir(os.path.join(new, "kernels")))
        with open(os.path.join(new, "xess", "libxess.dll"), "rb") as f:
            self.assertEqual(f.read(), b"sdk libxess")
        self.assertFalse(os.path.exists(os.path.join(new, "igc")))
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
        r = xess.install(self.ctx, self.settings, self.job)
        self.assertEqual((r["ok"], r["up_to_date"], r["package_dir"]), (True, True, os.path.realpath(self.old)))
        self.assertEqual((self.u.calls, self.downloads), ([], []))

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


if __name__ == "__main__":
    unittest.main()
