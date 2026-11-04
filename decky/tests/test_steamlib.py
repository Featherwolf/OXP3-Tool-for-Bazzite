"""steamlib against a fake home and a fake root. World and FakeUser are shared with test_xess and test_games."""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from oxp3deck import steamlib, sysexec, userexec
from tests import stub_decky

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "xess")
ROOT = os.geteuid() == 0


def write(path, data, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data.encode() if isinstance(data, str) else data)
    if mode is not None:
        os.chmod(path, mode)
    return path


def fixture(*parts) -> bytes:
    with open(os.path.join(FIXTURES, *parts), "rb") as f:
        return f.read()


class World:
    """A fake system: the user's home with Steam, libraries, Protons and processes under a fake /proc."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.uid = 1000 if ROOT else os.getuid()    # files must be owned by ctx.uid for the /proc scan
        self.ctx = stub_decky.make_ctx(tmp, uid=self.uid, gid=self.uid)
        self.home = self.ctx.home
        self.root = self.ctx.root
        self.steam = os.path.join(self.home, ".local", "share", "Steam")
        self.libs = []
        self.next_pid = 100

    def sys(self, p):
        return self.ctx.sys_path(p)

    def own(self, path):
        if ROOT:
            os.chown(path, self.uid, self.uid, follow_symlinks=False)

    def make_steam(self):
        for sub in ("steamapps/common", "steamapps/compatdata", "config", "compatibilitytools.d"):
            os.makedirs(os.path.join(self.steam, sub), exist_ok=True)
        self._write_libraryfolders()
        return self.steam

    def add_library(self, path):
        os.makedirs(os.path.join(path, "steamapps", "common"), exist_ok=True)
        os.makedirs(os.path.join(path, "steamapps", "compatdata"), exist_ok=True)
        self.libs.append(path)
        self._write_libraryfolders()
        return path

    def _write_libraryfolders(self):
        blocks = []
        for i, p in enumerate([self.steam] + self.libs):
            blocks.append(f'\t"{i}"\n\t{{\n\t\t"path"\t\t"{p}"\n\t\t"label"\t\t""\n\t\t"apps"\n\t\t{{\n\t\t}}\n\t}}\n')
        write(os.path.join(self.steam, "steamapps", "libraryfolders.vdf"),
              '"libraryfolders"\n{\n' + "".join(blocks) + "}\n")

    def app(self, appid, installdir, lib=None, prefix=True):
        lib = lib or self.steam
        write(os.path.join(lib, "steamapps", f"appmanifest_{appid}.acf"),
              f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"name"\t\t"{installdir}"\n'
              f'\t"installdir"\t\t"{installdir}"\n}}\n')
        game = os.path.join(lib, "steamapps", "common", installdir)
        os.makedirs(game, exist_ok=True)
        pfx = None
        if prefix:
            pfx = os.path.join(lib, "steamapps", "compatdata", str(appid), "pfx")
            os.makedirs(os.path.join(pfx, "drive_c"), exist_ok=True)
        return game, pfx

    def compat(self, mapping, valve_key="Valve"):
        entries = "".join(f'\t\t\t\t\t"{k}"\n\t\t\t\t\t{{\n\t\t\t\t\t\t"name"\t\t"{v}"\n\t\t\t\t\t\t"config"\t\t""\n'
                          f'\t\t\t\t\t\t"priority"\t\t"250"\n\t\t\t\t\t}}\n' for k, v in mapping.items())
        write(os.path.join(self.steam, "config", "config.vdf"),
              '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"' + valve_key + '"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n'
              '\t\t\t\t"CompatToolMapping"\n\t\t\t\t{\n' + entries + "\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n")

    def proton(self, name, where="common", dll=b"stock igdext64", hook=False, internal=None, lib=None):
        """A Proton folder. where: common (Valve's, in a library), tools (~/.../compatibilitytools.d), system
        (/usr/share/...), local (/usr/local/share/...)."""
        base = {"common": os.path.join(lib or self.steam, "steamapps", "common"),
                "tools": os.path.join(self.steam, "compatibilitytools.d"),
                "system": self.sys("/usr/share/steam/compatibilitytools.d"),
                "local": self.sys("/usr/local/share/steam/compatibilitytools.d")}[where]
        p = os.path.join(base, name)
        write(os.path.join(p, "proton"), "#!/usr/bin/env python3\n", 0o755)
        if dll is not None:
            write(os.path.join(p, "files/lib/wine/igdext/x86_64-windows/igdext64.dll"), dll, 0o644)
        if hook:
            write(os.path.join(p, "user_settings.py"),
                  '# xess-xmx proton hook\n_h = "/pkg/tools/proton-hook.py"\n')
        if internal or where in ("tools", "system", "local"):
            write(os.path.join(p, "compatibilitytool.vdf"),
                  '"compatibilitytools"\n{\n  "compat_tools"\n  {\n    "%s" // Internal name\n    {\n'
                  '      "install_path" "."\n      "display_name" "%s"\n      "from_oslist"  "windows"\n'
                  '      "to_oslist"    "linux"\n    }\n  }\n}\n' % (internal or name, name))
        return p

    def proc(self, argv, comm=None, environ=None, exe=None, uid=None, start=None):
        pid = self.next_pid
        self.next_pid += 1
        d = os.path.join(self.sys("/proc"), str(pid))
        os.makedirs(d)
        write(os.path.join(d, "cmdline"), b"\0".join(a.encode() for a in argv) + b"\0")
        write(os.path.join(d, "comm"), (comm or os.path.basename(argv[0]))[:15] + "\n")
        write(os.path.join(d, "stat"), f"{pid} ({comm or 'x'}) S 1 " + " ".join(["0"] * 17)
              + f" {start if start is not None else pid} 0 0\n")
        if environ is not None:
            write(os.path.join(d, "environ"), b"".join(f"{k}={v}".encode() + b"\0" for k, v in environ.items()))
        if exe:
            os.symlink(exe, os.path.join(d, "exe"))
        if uid is None:
            for name in os.listdir(d):
                self.own(os.path.join(d, name))
            self.own(d)
        elif ROOT:
            os.chown(d, uid, uid)
        return pid

    def steam_client(self, environ):
        exe = os.path.join(self.steam, "ubuntu12_32", "steam")
        return self.proc([exe, "-gamepadui"], comm="steam", environ=environ, exe=exe)

    def reaper(self, appid):
        return self.proc([os.path.join(self.steam, "ubuntu12_32", "reaper"), "SteamLaunch", f"AppId={appid}", "--",
                          "/x/steam-launch-wrapper", "--", "proton", "waitforexitandrun", "game.exe"])


class FakeUser:
    """Stands in for userexec.run. Records every call; runs mkdir, rm, mv, cp and the python3 unpack for real (on
    the test's temporary folders); answers systemctl from `session_env` / `units`, and `bash SCRIPT` from
    `scripts` (full path or basename -> fn(argv, cwd) -> Result), other systemctl verbs from `systemctl`
    (verb -> fn(argv) -> Result, else rc 0). `spawn`: fn(argv) -> (pid, end) of a fake process for a script run with
    on_start; on_start gets the pid before the script's fn runs, and end() is called after it."""

    def __init__(self):
        self.calls = []
        self.scripts = {}
        self.systemctl = {}
        self.spawn = None
        self.session_env = None     # text of show-environment, or None for "no bus"
        self.units = None           # text of `systemctl --user show ...`

    def __call__(self, ctx, argv, *, timeout=None, on_line=None, env_extra=None, cwd=None, detach=False,
                 on_start=None):
        argv = [str(a) for a in argv]
        self.calls.append({"argv": argv, "cwd": cwd, "timeout": timeout, "streamed": on_line is not None,
                           "detach": detach})
        prog = argv[0]
        if prog in ("mkdir", "rm", "mv", "cp"):
            r = subprocess.run(argv, capture_output=True, text=True, cwd=cwd or ctx.home, stdin=subprocess.DEVNULL)
            return sysexec.Result(r.returncode, r.stdout, r.stderr)
        if prog == "/usr/bin/python3":
            r = subprocess.run([sys.executable] + argv[1:], capture_output=True, text=True, cwd=cwd or ctx.home,
                               stdin=subprocess.DEVNULL)
            return sysexec.Result(r.returncode, r.stdout, r.stderr)
        if prog == "systemctl":
            verb = argv[2]
            if verb == "show-environment":
                return sysexec.Result(0, self.session_env, "") if self.session_env is not None else \
                    sysexec.Result(1, "", "Failed to connect to bus")
            if verb == "show":
                return sysexec.Result(0, self.units, "") if self.units is not None else sysexec.Result(1, "", "no")
            if verb in self.systemctl:
                return self.systemctl[verb](argv)
            return sysexec.Result(0, "", "")
        if prog == "bash":
            fn = self.scripts.get(argv[1]) or self.scripts.get(os.path.basename(argv[1]))
            end = None
            if on_start is not None and self.spawn is not None:
                pid, end = self.spawn(argv)
                on_start(pid)
            try:
                r = fn(argv, cwd) if fn else sysexec.Result(0, f"ran {argv[1]}\n", "")
            finally:
                if end is not None:
                    end()
            if on_line:
                for line in r.out.splitlines():
                    on_line(line)
            return r
        return sysexec.Result(127, "", f"{prog}: not faked")

    def commands(self):
        """Short labels: 'mkdir PATH', 'bash SCRIPT ARGS', 'systemctl stop', 'python3 unpack'..."""
        out = []
        for c in self.calls:
            a = c["argv"]
            if a[0] == "/usr/bin/python3":
                out.append("python3 unpack")
            elif a[0] == "systemctl":
                out.append("systemctl " + a[2])
            elif a[0] == "bash":
                out.append(" ".join(["bash"] + a[1:]))
            else:
                out.append(" ".join(x for x in a if x not in ("--", "-p", "-rf", "-a", "-T")))
        return out


def patch_user(test, fake=None):
    fake = fake or FakeUser()
    p = mock.patch.object(userexec, "run", fake)
    p.start()
    test.addCleanup(p.stop)
    return fake


class VdfTest(unittest.TestCase):
    def test_parse(self):
        text = r'''
// a comment
"root"
{
    "quoted"   "a \"b\" c\\d"   // trailing comment
    unquoted   value
    "nested" { "x" "1" "y" { "z" "2" } }
    "cond"     "win"   [$WIN32]
    "Mixed"    { "Key" "v" }
    "dup" "1"
    "dup" "2"
    "path" "C:\\Games"
}
'''
        d = steamlib.vdf_parse(text)
        r = d["root"]
        self.assertEqual(r["quoted"], 'a "b" c\\d')
        self.assertEqual(r["unquoted"], "value")
        self.assertEqual(r["nested"]["y"]["z"], "2")
        self.assertEqual(r["cond"], "win")
        self.assertEqual(r["dup"], "2")
        self.assertEqual(r["path"], "C:\\Games")
        self.assertEqual(steamlib.vdf_get(d, "ROOT", "mixed", "key"), "v")
        self.assertIsNone(steamlib.vdf_get(d, "root", "nope", "x"))
        self.assertEqual(steamlib.vdf_get(d, "root", "quoted", "x", default=5), 5)

    def test_lenient(self):
        self.assertEqual(steamlib.vdf_parse(""), {})
        self.assertEqual(steamlib.vdf_parse('"a" { "b" "c"'), {"a": {"b": "c"}})       # unclosed
        self.assertEqual(steamlib.vdf_parse('}}} "a" "b" "c"'), {"a": "b"})          # stray braces, a dangling key
        self.assertEqual(steamlib.vdf_parse('"unterminated'), {})

    def test_fixture_config(self):
        d = steamlib.vdf_parse(fixture("config.vdf").decode())
        m = steamlib.vdf_get(d, "InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping")
        self.assertEqual(m["292030"]["name"], "proton-cachyos-slr")
        self.assertEqual(m["0"]["name"], "proton_experimental")


class LibrariesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.w = World(self.tmp.name)

    def test_no_steam(self):
        self.assertIsNone(steamlib.steam_root(self.w.ctx))
        self.assertEqual(steamlib.libraries(self.w.ctx), [])
        self.assertFalse(steamlib.flatpak_steam(self.w.ctx))
        os.makedirs(os.path.join(self.w.home, ".var/app/com.valvesoftware.Steam"))
        self.assertTrue(steamlib.flatpak_steam(self.w.ctx))

    def test_steam_root_fallback(self):
        alt = os.path.join(self.w.home, ".steam", "debian-installation")
        os.makedirs(alt)
        os.symlink(alt, os.path.join(self.w.home, ".steam", "steam"))
        self.assertEqual(steamlib.steam_root(self.w.ctx), alt)

    def test_libraries(self):
        self.w.make_steam()
        sd = self.w.add_library(os.path.join(self.tmp.name, "media", "SD"))
        link = os.path.join(self.tmp.name, "sdlink")
        os.symlink(sd, link)
        self.w.libs.append(link)                                      # the same card twice, through a link
        self.w.libs.append(os.path.join(self.tmp.name, "gone"))       # no longer mounted
        self.w._write_libraryfolders()
        self.assertEqual(steamlib.libraries(self.w.ctx), [self.w.steam, sd])

    def test_config_libraryfolders_fallback(self):
        self.w.make_steam()
        sd = self.w.add_library(os.path.join(self.tmp.name, "SD"))
        vdf = os.path.join(self.w.steam, "steamapps", "libraryfolders.vdf")
        os.rename(vdf, os.path.join(self.w.steam, "config", "libraryfolders.vdf"))
        self.assertEqual(steamlib.libraries(self.w.ctx), [self.w.steam, sd])

    def test_app_and_prefix(self):
        self.w.make_steam()
        sd = self.w.add_library(os.path.join(self.tmp.name, "SD"))
        game, pfx = self.w.app("1091500", "Cyberpunk 2077", lib=sd)
        self.assertEqual(steamlib.app_install_dir(self.w.ctx, "1091500"), game)
        self.assertEqual(steamlib.prefix_dir(self.w.ctx, "1091500"), pfx)
        self.assertIsNone(steamlib.app_install_dir(self.w.ctx, "1"))
        self.assertIsNone(steamlib.prefix_dir(self.w.ctx, "1"))
        self.assertIsNone(steamlib.app_install_dir(self.w.ctx, "../x"))
        write(os.path.join(sd, "steamapps", "appmanifest_7.acf"), '"AppState" { "installdir" "../../etc" }')
        self.assertIsNone(steamlib.app_install_dir(self.w.ctx, "7"))


class CompatToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.w = World(self.tmp.name)
        self.w.make_steam()

    def test_mapping(self):
        write(os.path.join(self.w.steam, "config", "config.vdf"), fixture("config.vdf"))
        c = self.w.ctx
        self.assertEqual(steamlib.compat_tool(c, "292030"), "proton-cachyos-slr")
        self.assertEqual(steamlib.compat_tool(c, "1091500"), "GE-Proton10-3")
        self.assertIsNone(steamlib.compat_tool(c, "570"))          # empty name: Steam's default
        self.assertIsNone(steamlib.compat_tool(c, "12345"))        # no entry
        self.assertIsNone(steamlib.compat_tool(c, "0"))
        self.assertEqual(steamlib.default_compat_tool(c), "proton_experimental")

    def test_lowercase_valve_key(self):
        self.w.compat({"10": "proton_9"}, valve_key="valve")
        self.assertEqual(steamlib.compat_tool(self.w.ctx, "10"), "proton_9")
        self.assertIsNone(steamlib.default_compat_tool(self.w.ctx))

    def test_no_config(self):
        self.assertIsNone(steamlib.compat_tool(self.w.ctx, "10"))
        self.assertIsNone(steamlib.default_compat_tool(self.w.ctx))

    def test_find_tool_dir(self):
        c = self.w.ctx
        ge = self.w.proton("GE-Proton-latest", "tools", internal="GE-Proton10-3")
        slr = self.w.proton("proton-cachyos-slr", "system")
        local = self.w.proton("my-proton", "local")
        sd = self.w.add_library(os.path.join(self.tmp.name, "SD"))
        exp = self.w.proton("Proton - Experimental", "common", lib=sd)
        nine = self.w.proton("Proton 9.0 (Beta)", "common")
        self.assertEqual(steamlib.find_tool_dir(c, "GE-Proton10-3"), ge)        # by compatibilitytool.vdf
        self.assertEqual(steamlib.find_tool_dir(c, "ge-proton10-3"), ge)        # case-insensitive second
        self.assertEqual(steamlib.find_tool_dir(c, "GE-Proton-latest"), ge)     # by folder name
        self.assertEqual(steamlib.find_tool_dir(c, "proton-cachyos-slr"), slr)
        self.assertEqual(steamlib.find_tool_dir(c, "my-proton"), local)
        self.assertEqual(steamlib.find_tool_dir(c, "proton_experimental"), exp)
        self.assertEqual(steamlib.find_tool_dir(c, "proton_9"), nine)
        self.assertTrue(steamlib.is_system_tool(c, slr))
        self.assertTrue(steamlib.is_system_tool(c, local))
        self.assertFalse(steamlib.is_system_tool(c, ge))
        for unsure in ("proton_8", "proton_hotfix", "steamlinuxruntime_sniper", None, "", "../x", "a/b"):
            self.assertIsNone(steamlib.find_tool_dir(c, unsure), unsure)
        self.w.proton("Proton 9.0", "common", lib=sd)      # two candidates: unsure
        self.assertIsNone(steamlib.find_tool_dir(c, "proton_9"))

    def test_install_path_outside_is_not_followed(self):
        p = self.w.proton("evil", "tools", internal="Evil")
        write(os.path.join(p, "compatibilitytool.vdf"),
              '"compatibilitytools" { "compat_tools" { "Evil" { "install_path" "../../../../.." } } }')
        self.assertEqual(steamlib.find_tool_dir(self.w.ctx, "Evil"), p)


class ProcessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.w = World(self.tmp.name)
        self.w.make_steam()
        os.makedirs(self.w.sys("/proc"))

    def test_no_game(self):
        self.w.proc(["/usr/bin/gamescope", "--", "steam"])
        self.assertIsNone(steamlib.running_game(self.w.ctx))

    def test_reaper(self):
        self.w.proc([os.path.join(self.w.steam, "ubuntu12_32", "reaper"), "SteamLaunch", "AppId=0", "--", "x"])
        self.assertIsNone(steamlib.running_game(self.w.ctx))
        self.w.reaper("1091500")
        self.assertEqual(steamlib.running_game(self.w.ctx), "1091500")

    @unittest.skipUnless(ROOT, "needs root to fake another user's process")
    def test_other_users_reaper_is_ignored(self):
        self.w.proc([os.path.join(self.w.steam, "ubuntu12_32", "reaper"), "SteamLaunch", "AppId=42", "--", "x"],
                    uid=1234)
        self.assertIsNone(steamlib.running_game(self.w.ctx))

    def test_not_a_reaper(self):
        self.w.proc(["/usr/bin/python3", "SteamLaunch", "AppId=42"])
        self.assertIsNone(steamlib.running_game(self.w.ctx))

    def test_steam_environ(self):
        self.assertIsNone(steamlib.steam_environ(self.w.ctx))
        self.w.proc([os.path.join(self.w.steam, "ubuntu12_32", "steamwebhelper")], comm="steamwebhelper",
                    environ={"VK_DRIVER_FILES": "web"})
        self.w.proc(["/bin/bash", "/usr/bin/steam"], comm="steam", environ={"VK_DRIVER_FILES": "launcher"})
        self.assertIsNone(steamlib.steam_environ(self.w.ctx))
        self.w.proc(["/bin/bash", os.path.join(self.w.steam, "steam.sh")], comm="steam",
                    environ={"VK_DRIVER_FILES": "script"})
        self.assertEqual(steamlib.steam_environ(self.w.ctx)["VK_DRIVER_FILES"], "script")
        self.w.steam_client({"VK_DRIVER_FILES": "/pkg/run/intel_icd.json:/usr/share/vulkan/icd.d/x.json",
                             "HOME": self.w.home, "ODD": "a=b"})
        env = steamlib.steam_environ(self.w.ctx)
        self.assertEqual(env["VK_DRIVER_FILES"], "/pkg/run/intel_icd.json:/usr/share/vulkan/icd.d/x.json")
        self.assertEqual(env["ODD"], "a=b")

    def test_steam_by_argv(self):
        self.w.proc([os.path.join(self.w.steam, "ubuntu12_32", "steam")], comm="steam", environ={"A": "1"})
        self.assertEqual(steamlib.steam_environ(self.w.ctx), {"A": "1"})


if __name__ == "__main__":
    unittest.main()
