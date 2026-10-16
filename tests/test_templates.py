import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from oxp3_tool import templates

from .fakeroot import OXP3_MODALIAS

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

try:
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None

# InputPlumber's capability_map_v1.json; CI downloads it from the v0.82.0 tag.
IP_SCHEMA = os.environ.get("OXP3_IP_CAPABILITY_MAP_SCHEMA")


@unittest.skipIf(yaml is None, "PyYAML not installed")
class CapabilityMapTest(unittest.TestCase):
    def render(self, **actions):
        merged = dict(templates.DEFAULT_BUTTON_ACTIONS, **actions)
        return yaml.safe_load(templates.capability_map_yaml("oxp9", merged, "/usr/x.yaml", "abc"))

    def test_defaults(self):
        doc = self.render()
        self.assertEqual(doc["id"], "oxp9")
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["kind"], "CapabilityMap")
        self.assertEqual(doc["filtered_events"], [])
        by_name = {m["name"]: m for m in doc["mapping"]}
        self.assertEqual(set(by_name), {"ONEX", "Keyboard", "Home"})
        self.assertEqual(
            [e["keyboard"] for e in by_name["ONEX"]["source_events"]],
            ["KeyLeftCtrl", "KeyLeftAlt", "KeyLeftMeta"],
        )
        self.assertEqual(by_name["ONEX"]["target_event"], {"gamepad": {"button": "QuickAccess"}})
        self.assertEqual(
            [e["keyboard"] for e in by_name["Keyboard"]["source_events"]],
            ["KeyLeftCtrl", "KeyLeftMeta", "KeyO"],
        )
        self.assertEqual(by_name["Keyboard"]["target_event"], {"gamepad": {"button": "Keyboard"}})
        self.assertEqual(by_name["Home"]["source_events"], [{"gamepad": {"button": "Keyboard"}}])
        self.assertEqual(by_name["Home"]["target_event"], {"gamepad": {"button": "Guide"}})

    def test_stock_actions_reproduce_upstream(self):
        doc = self.render(**templates.STOCK_BUTTON_ACTIONS)
        by_name = {m["name"]: m for m in doc["mapping"]}
        # Home stays the native gamepad Keyboard event: no mapping needed.
        self.assertEqual(set(by_name), {"ONEX", "Keyboard"})
        self.assertEqual(by_name["Keyboard"]["target_event"], {"gamepad": {"button": "QuickAccess2"}})

    def test_rejects_unknown_action(self):
        with self.assertRaises(ValueError):
            templates.capability_map_yaml("oxp9", dict(templates.DEFAULT_BUTTON_ACTIONS, home="reboot"), None, None)

    @unittest.skipUnless(IP_SCHEMA and jsonschema, "InputPlumber schema or jsonschema not available")
    def test_matches_inputplumber_schema(self):
        schema = json.loads(Path(IP_SCHEMA).read_text())
        for home in templates.BUTTON_ACTIONS:
            for key in templates.BUTTON_ACTIONS:
                jsonschema.validate(self.render(home=home, keyboard=key), schema)
                jsonschema.validate(self.render(xbox=home, onex=key), schema)
        bad = self.render()
        bad["mapping"][0]["target_event"]["gamepad"]["button"] = "Home"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(bad, schema)


class RefreshTableTest(unittest.TestCase):
    def test_formula_matches_gamescope_table(self):
        # The formula only drives rates below 48 Hz; check it against the
        # curated table everywhere they overlap (67 Hz differs by rounding).
        for hz in range(48, 145):
            formula = round(380_160_000 / (2000 * hz)) - 1264
            table = templates.panel_vfp(hz)
            if hz == 67:
                self.assertEqual(abs(formula - table), 1)
            else:
                self.assertEqual(formula, table, hz)

    def test_native_modes(self):
        self.assertEqual(templates.panel_vfp(60), 1904)
        self.assertEqual(templates.panel_vfp(144), 56)
        self.assertEqual(templates.panel_vfp(30), 5072)

    def test_range(self):
        with self.assertRaises(ValueError):
            templates.panel_vfp(29)
        with self.assertRaises(ValueError):
            templates.oled_lua(49)


LUA = shutil.which("lua5.4") or shutil.which("lua")

LUA_HARNESS = r"""
local registered = {}
gamescope = {
  eotf = { gamma22 = 0, pq = 1 },
  config = { known_displays = {} },
  modegen = {
    adjust_front_porch = function(mode, vfp)
      mode.vfp = vfp
      mode.vtotal = mode.vdisplay + vfp + 64
    end,
    calc_vrefresh = function(mode)
      return math.floor(mode.clock * 1000 / (mode.htotal * mode.vtotal) + 0.5)
    end,
  },
}
function debug(_) end
function warn(_) end
dofile(arg[1])
local d = gamescope.config.known_displays.oxp3_tool_sdc_oled
assert(d, "profile not registered")
local out = {}
out[#out + 1] = "match_sdc=" .. d.matches({ vendor = "SDC", product = 0x4301, model = "AMS881KB01-0" })
out[#out + 1] = "match_other=" .. d.matches({ vendor = "SDC", product = 0x4302, model = "x" })
out[#out + 1] = "eotf=" .. d.hdr.eotf
out[#out + 1] = "rates=" .. #d.dynamic_refresh_rates
for _, hz in ipairs(d.dynamic_refresh_rates) do
  local mode = d.dynamic_modegen({ clock = 380160, htotal = 2000, vdisplay = 1200, vtotal = 1264 + 56 }, hz)
  out[#out + 1] = "mode=" .. hz .. ":" .. mode.vfp .. ":" .. mode.vrefresh
end
print(table.concat(out, "\n"))
"""


@unittest.skipIf(LUA is None, "no Lua interpreter")
class OledLuaTest(unittest.TestCase):
    def run_lua(self, min_refresh):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp, "profile.lua")
            harness = Path(tmp, "harness.lua")
            profile.write_text(templates.oled_lua(min_refresh))
            harness.write_text(LUA_HARNESS)
            res = subprocess.run([LUA, str(harness), str(profile)], capture_output=True, text=True, check=True)
        return res.stdout.splitlines()

    def check(self, min_refresh):
        lines = self.run_lua(min_refresh)
        self.assertIn("match_sdc=5100", lines)
        self.assertIn("match_other=-1", lines)
        self.assertIn("eotf=0", lines)  # gamma22, never PQ
        self.assertIn(f"rates={144 - min_refresh + 1}", lines)
        modes = [line for line in lines if line.startswith("mode=")]
        self.assertEqual(len(modes), 144 - min_refresh + 1)
        for line in modes:
            hz, vfp, vrefresh = (int(x) for x in line[5:].split(":"))
            self.assertEqual(vfp, templates.panel_vfp(hz))
            self.assertEqual(vrefresh, hz, line)

    def test_default_range(self):
        self.check(48)

    def test_extended_range(self):
        self.check(30)


SYSTEMD_HWDB = shutil.which("systemd-hwdb")


@unittest.skipIf(SYSTEMD_HWDB is None, "systemd-hwdb not available")
class VolumeHwdbTest(unittest.TestCase):
    def query(self, modalias):
        with tempfile.TemporaryDirectory() as tmp:
            hwdb_d = Path(tmp, "etc/udev/hwdb.d")
            hwdb_d.mkdir(parents=True)
            (hwdb_d / "61-oxp3-tool-volume-keys.hwdb").write_text(templates.VOLUME_HWDB)
            subprocess.run([SYSTEMD_HWDB, f"--root={tmp}", "update", "--strict"], check=True,
                           capture_output=True)
            res = subprocess.run([SYSTEMD_HWDB, f"--root={tmp}", "query", modalias],
                                 capture_output=True, text=True, check=False)
        return res.stdout

    def test_matches_oxp3_keyboard(self):
        out = self.query("evdev:atkbd:" + OXP3_MODALIAS)
        self.assertIn("KEYBOARD_KEY_ae=!volumedown", out)
        self.assertIn("KEYBOARD_KEY_b0=!volumeup", out)

    def test_ignores_other_devices(self):
        other = OXP3_MODALIAS.replace("pnONEXPLAYER3", "pnONEXPLAYERX1A")
        self.assertEqual(self.query("evdev:atkbd:" + other).strip(), "")
        self.assertEqual(self.query("evdev:name:HID 1a86:fe00:" + OXP3_MODALIAS).strip(), "")


if __name__ == "__main__":
    unittest.main()
