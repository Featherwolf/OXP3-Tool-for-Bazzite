import os
import tempfile
import unittest
from unittest import mock

from oxp3deck import context, games, xess
from tests.test_pe import fake_dll
from tests.test_steamlib import World, patch_user, write
from tests.test_xess import SHIM_OLD, STOCK, env_text, make_package

WITCHER = "292030"


class GameInfoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.w = World(self.tmp.name)
        self.ctx = self.w.ctx
        self.w.make_steam()
        os.makedirs(self.w.sys("/proc"))
        self.u = patch_user(self)
        self.settings = context.Settings(os.path.join(self.ctx.settings_dir, "settings.json"))
        xess._marker_cache.clear()
        self.pkg = make_package(os.path.join(self.ctx.home, "Documents", "xess-xmx-linux-20261031"), "20261031")
        write(os.path.join(self.ctx.home, xess.ENV_FILE), env_text(self.pkg))
        self.sd = self.w.add_library(os.path.join(self.tmp.name, "media", "SD"))
        self.game, self.pfx = self.w.app(WITCHER, "The Witcher 3", lib=self.sd)

    def info(self, appid=WITCHER):
        r = games.game_info(self.ctx, self.settings, appid)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.u.calls, [])      # nothing runs for a game's info
        return r["game"]

    def dll(self, rel, version):
        p = os.path.join(self.game, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return fake_dll(p, version)

    def test_witcher3_on_a_stock_system_proton(self):
        """The owner's case: XeLL 1.2.1.13 refuses the GPU, on proton-cachyos-slr without the shim."""
        self.w.compat({"0": "proton_experimental", WITCHER: "proton-cachyos-slr"})
        slr = self.w.proton("proton-cachyos-slr", "system", dll=STOCK)
        self.dll("bin/x64_dx12/libxess.dll", (1, 3, 1, 0))
        self.dll("bin/x64_dx12/libxess_fg.dll", (1, 1, 0, 19))
        self.dll("bin/x64_dx12/libxell.dll", (1, 2, 1, 13))
        deep = "/".join(f"d{i}" for i in range(1, 11))          # 10 folders down: found, as proton-hook.py does
        self.dll(deep + "/libxess_dx11.dll", (1, 3, 1, 0))
        self.dll(deep + "/d11/libxess.dll", (9, 9, 9, 9))       # 11 down: not searched
        write(os.path.join(self.game, "bin/x64_dx12/LIBXESS.DLL.txt"), "not a dll")
        c = os.path.join(self.pfx, "drive_c")
        write(os.path.join(c, "igdext_trace.log"), "igdext: GPU 0xb0a0\nfallback: unknown Intel device id\n"
                                                   "igdext: later\nfallback: self-test failed (no canary)\n")
        write(os.path.join(c, "igdext_xell.log"), "[XeLL] XeLL context creation failed - unsupported device\n")
        write(os.path.join(c, "igdext_kernels", "compile_failed.txt"), "k1\n")
        write(os.path.join(c, "igdext_kernels", "anv_canary"), "")
        g = self.info()
        self.assertEqual((g["appid"], g["install_dir"], g["prefix"]), (WITCHER, self.game, self.pfx))
        self.assertEqual((g["compat_tool"], g["default_compat_tool"]), ("proton-cachyos-slr", "proton_experimental"))
        self.assertEqual((g["compat_tool_dir"], g["compat_tool_system"], g["compat_tool_shimmed"]), (slr, True, False))
        found = {os.path.relpath(d["path"], self.game): d["version"] for d in g["dlls"]}
        self.assertEqual(found, {"bin/x64_dx12/libxess.dll": "1.3.1.0", "bin/x64_dx12/libxess_fg.dll": "1.1.0.19",
                                 "bin/x64_dx12/libxell.dll": "1.2.1.13", deep + "/libxess_dx11.dll": "1.3.1.0"})
        self.assertEqual(g["last_run"]["fallback"], "self-test failed (no canary)")
        self.assertEqual({k: g["last_run"][k] for k in ("trace", "compile_failed", "canary", "xell_unsupported")},
                         {"trace": True, "compile_failed": True, "canary": True, "xell_unsupported": True})
        self.assertIsNotNone(g["last_run"]["logged_at"])
        advice = "\n".join(g["advice"])
        self.assertIn("XeSS 1.3.1.0 is older than 2.0.2.68", advice)
        self.assertIn("needs the Proton hook, which proton-cachyos-slr (a system Proton) does not have", advice)
        self.assertIn("XeSS frame generation 1.1.0.19 is older than 1.2", advice)
        self.assertIn("XeLL 1.2 may refuse this GPU (unsupported device), so frame generation can stay off", advice)
        self.assertIn("The last run's log says it did", advice)
        self.assertNotIn("XeLL refused this GPU at the last run", advice)       # said once
        self.assertIn("falls back to DP4a until a retry", advice)
        self.assertIn("self-test", advice)
        self.assertIn("proton-cachyos-slr is a system Proton without the shim", advice)
        self.assertIn("shim it from the XeSS page", advice)

    def test_shimmed_user_proton_and_current_xess(self):
        self.w.compat({WITCHER: "GE-Proton10-3"})
        self.w.proton("GE-Proton10-3", "tools", dll=SHIM_OLD, hook=True)
        self.dll("bin/x64/libxess.dll", (2, 0, 2, 68))
        self.dll("bin/x64/libxell.dll", (1, 3, 2, 10))
        g = self.info()
        self.assertEqual((g["compat_tool_shimmed"], g["compat_tool_system"], g["compat_tool_hook"]),
                         (True, False, True))
        self.assertEqual(g["last_run"], {"trace": False, "fallback": None, "compile_failed": False, "canary": False,
                                         "xell_unsupported": None, "logged_at": None})
        self.assertEqual(g["advice"], [])

    def test_xell_refusal_seen_without_xell_12(self):
        self.w.compat({WITCHER: "GE-Proton10-3"})
        self.w.proton("GE-Proton10-3", "tools", dll=SHIM_OLD, hook=True)
        self.dll("libxell.dll", (1, 3, 0, 5))
        write(os.path.join(self.pfx, "drive_c", "igdext_trace.log"),
              "XeLL context creation failed - Unsupported device")
        g = self.info()
        self.assertTrue(g["last_run"]["xell_unsupported"])
        self.assertIn("XeLL refused this GPU at the last run", "\n".join(g["advice"]))
        write(os.path.join(self.pfx, "drive_c", "igdext_trace.log"), "all fine\n")
        self.assertFalse(self.info()["last_run"]["xell_unsupported"])

    def test_default_tool_unknown(self):
        g = self.info()
        self.assertIsNone(g["compat_tool"])
        self.assertIsNone(g["compat_tool_shimmed"])
        self.assertIn("Steam's config.vdf does not name", "\n".join(g["advice"]))
        self.assertIn("No XeSS or FidelityFX DLL", "\n".join(g["advice"]))
        self.w.compat({"0": "proton_experimental"})
        self.assertIn("Steam's default tool (proton_experimental), whose folder was not found",
                      "\n".join(self.info()["advice"]))
        exp = self.w.proton("Proton - Experimental", "common", dll=SHIM_OLD, hook=True)
        g = self.info()
        self.assertEqual((g["compat_tool"], g["compat_tool_dir"], g["compat_tool_shimmed"]), (None, exp, True))
        self.assertNotIn("default", "\n".join(g["advice"]))

    def test_mapped_tool_not_found(self):
        self.w.compat({WITCHER: "GE-Proton9-1"})
        self.assertIn("The folder of GE-Proton9-1 was not found", "\n".join(self.info()["advice"]))

    def test_proton_without_driver_store(self):
        self.w.compat({WITCHER: "proton_11"})
        self.w.proton("Proton 11.0", "common", dll=None, hook=True)
        g = self.info()
        self.assertIsNone(g["compat_tool_shimmed"])
        self.assertIn("XeSS loads the shim there only with xess-xmx-linux 20261104 or newer", "\n".join(g["advice"]))
        write(os.path.join(self.ctx.home, xess.ENV_FILE), env_text(self.pkg, alt=True))
        self.assertNotIn("20261104 or newer", "\n".join(self.info()["advice"]))

    def test_fsr_only_game(self):
        self.w.compat({WITCHER: "GE-Proton10-3"})
        self.w.proton("GE-Proton10-3", "tools", dll=SHIM_OLD, hook=True)
        self.dll("Game/Binaries/Win64/amd_fidelityfx_dx12.dll", (1, 0, 0, 0))
        g = self.info()
        self.assertEqual([d["kind"] for d in g["dlls"]], ["ffx"])
        advice = "\n".join(g["advice"])
        self.assertIn("FSR=xess runs its upscaling and frame generation on XeSS", advice)
        self.assertIn("needs xess-xmx-linux 20261104 or newer", advice)       # the package is 20261031
        self.dll("Game/Binaries/Win64/libxess.dll", (2, 0, 2, 68))
        self.assertNotIn("FSR=xess", "\n".join(self.info()["advice"]))           # a game with XeSS uses its XeSS

    def test_leftover_and_link(self):
        self.w.compat({WITCHER: "GE-Proton10-3"})
        self.w.proton("GE-Proton10-3", "tools", dll=SHIM_OLD, hook=True)
        os.makedirs(os.path.join(self.pkg, "xess"))
        sdk = fake_dll(os.path.join(self.pkg, "xess", "libxess.dll"), (2, 0, 2, 68))
        self.dll("bin/libxess.dll.xmx-orig", (2, 0, 1, 41))
        os.symlink(sdk, os.path.join(self.game, "bin", "libxess.dll"))
        g = self.info()
        self.assertEqual([(d["version"], d["link"]) for d in g["dlls"]], [("2.0.2.68", True)])
        self.assertIn("left the game's own libraries renamed (.xmx-orig)", "\n".join(g["advice"]))
        outside = fake_dll(os.path.join(self.tmp.name, "root-only.dll"), (6, 6, 6, 6))
        os.symlink(outside, os.path.join(self.game, "libxell.dll"))
        versions = {d["name"]: d["version"] for d in self.info()["dlls"]}
        self.assertEqual(versions, {"libxess.dll": "2.0.2.68", "libxell.dll": None})   # a link out is not read

    def test_entry_limit(self):
        for i in range(30):
            write(os.path.join(self.game, f"f{i}.pak"), "")
        with mock.patch.object(games, "MAX_ENTRIES", 10):
            self.assertIn("more than 10 entries", "\n".join(self.info()["advice"]))

    def test_no_prefix_no_folder_not_installed(self):
        os.unlink(os.path.join(self.ctx.home, xess.ENV_FILE))
        self.w.app("10", "Native", prefix=False)
        g = self.info("10")
        self.assertIsNone(g["prefix"])
        self.assertIsNone(g["last_run"])
        advice = "\n".join(g["advice"])
        self.assertTrue(g["advice"][0].startswith("xess-xmx-linux is not installed"))
        self.assertIn("No Proton prefix yet", advice)
        g = self.info("999")
        self.assertIn("The game's folder was not found", "\n".join(g["advice"]))

    def test_not_a_steam_game(self):
        for bad in ("", "abc", "12a", "../1", None, "-1"):
            r = games.game_info(self.ctx, self.settings, bad)
            self.assertEqual((r["ok"], r["error"], r["game"]), (False, "game info: not a Steam game", None), bad)

    def test_never_raises(self):
        with mock.patch.object(games.steamlib, "app_install_dir", side_effect=RuntimeError("boom")), \
                self.assertLogs("oxp3deck", "ERROR"):
            r = games.game_info(self.ctx, self.settings, WITCHER)
        self.assertEqual((r["ok"], r["error"]), (False, "game info: RuntimeError: boom"))


if __name__ == "__main__":
    unittest.main()
