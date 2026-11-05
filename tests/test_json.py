"""--json: one JSON object per run, for front ends such as the Decky plugin."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from oxp3_tool.cli import main

from .fakeroot import FakeCachyOS, FakeOxp3

KERNELS = {"linux-cachyos-deckify": "7.2.11-1-cachyos-deckify",
           "linux-cachyos-deckify-vsw": "7.2.11-1-cachyos-deckify-vsw"}


class JsonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name, "root")

    def run_json(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(list(argv), system=self.f.system())
        self.assertEqual(err.getvalue(), "")  # everything is in the object
        data = json.loads(out.getvalue())  # exactly one object
        self.assertEqual(data["rc"], rc)
        self.assertEqual(data["ok"], rc == 0)
        return data

    def test_status_and_list(self):
        self.f = FakeOxp3(self.root)
        self.f.build()
        data = self.run_json("status", "--json")
        st = data["status"]
        self.assertEqual(data["command"], "status")
        self.assertTrue(st["device"]["supported"] and st["can_change"])
        self.assertEqual(st["os"]["platform"], "bazzite")
        ids = [f["id"] for f in st["fixes"]]
        self.assertIn("buttons", ids)
        buttons = st["fixes"][ids.index("buttons")]
        self.assertEqual(buttons["state"], "recommended")
        self.assertIn("oxp3-tool", st["text"])
        cat = self.run_json("list", "--json")["catalog"]
        self.assertIn("fps-limit", cat["button_actions"])
        self.assertEqual(len(cat["fixes"]), len(st["fixes"]))

    def test_apply_needs_yes_and_reports_the_outcome(self):
        self.f = FakeOxp3(self.root)
        self.f.build()
        data = self.run_json("apply", "volume-keys", "--json")
        self.assertFalse(data["ok"])
        self.assertIn("pass --yes", " ".join(line["text"] for line in data["log"]))
        data = self.run_json("apply", "volume-keys", "--yes", "--json")
        self.assertTrue(data["ok"])
        self.assertTrue(data["outcome"]["changed"])
        self.assertTrue(data["plan"][0].startswith("volume-keys:"))
        st = self.run_json("status", "--json")["status"]
        self.assertEqual(next(f for f in st["fixes"] if f["id"] == "volume-keys")["state"], "applied")
        data = self.run_json("revert", "volume-keys", "--yes", "--json")
        self.assertTrue(data["ok"] and data["outcome"]["changed"])

    def test_pending_reboot_and_notes(self):
        self.f = FakeCachyOS(self.root, kernels=dict(KERNELS))
        self.f.power_on()
        res = self.run_json("apply", "nvme-resume", "--yes", "--any-ssd", "--json")
        self.assertTrue(res["outcome"]["reboot_needed"])
        st = self.run_json("status", "--json")["status"]
        nvme = next(f for f in st["fixes"] if f["id"] == "nvme-resume")
        self.assertEqual((nvme["state"], nvme["pending"]), ("applied", "reboot"))
        self.assertTrue(st["reboot_pending"])
        self.assertIsNone(st["rebuild_unfinished"])
        self.f.boot()
        st = self.run_json("status", "--json")["status"]
        self.assertFalse(st["reboot_pending"])
        # status-level notes are the status's own, not the last fix's
        self.run_json("kernel", "try", "linux-cachyos-deckify-vsw", "--yes", "--json")
        st = self.run_json("status", "--json")["status"]
        self.assertTrue(any("once at the next boot" in n for n in st["notes"]), st["notes"])
        self.assertFalse(any("DSB" in n for n in st["notes"]))

    def test_usage_error_is_json(self):
        self.f = FakeOxp3(self.root)
        self.f.build()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as e:
            main(["apply", "buttons", "--home", "nope", "--json"], system=self.f.system())
        self.assertEqual(e.exception.code, 2)
        data = json.loads(out.getvalue())
        self.assertEqual((data["rc"], data["ok"]), (2, False))
        self.assertIn("invalid choice", data["log"][0]["text"])

    def test_catalog_has_the_keys(self):
        self.f = FakeOxp3(self.root)
        self.f.build()
        cat = self.run_json("list", "--json")["catalog"]
        self.assertEqual(cat["button_keys"]["xbox"]["flag"], "--xbox-key")
        self.assertEqual(cat["button_keys"]["home"]["default"], "guide")
        self.assertEqual(cat["min_refresh"]["default"], 48)

    def test_errors_are_in_the_object(self):
        self.f = FakeOxp3(self.root)
        self.f.build()
        data = self.run_json("apply", "no-such-fix", "--yes", "--json")
        self.assertFalse(data["ok"])
        self.assertEqual(data["log"][-1]["stream"], "err")
        self.assertIn("unknown fix", data["log"][-1]["text"])

    def test_kernel(self):
        self.f = FakeCachyOS(self.root, kernels=dict(KERNELS))
        self.f.power_on()
        k = self.run_json("kernel", "--json")["kernel"]
        self.assertEqual([x["package"] for x in k["kernels"]], list(KERNELS))
        self.assertTrue(k["kernels"][0]["running"] and k["kernels"][0]["default"])
        self.assertEqual(k["next_boot"], {"package": "linux-cachyos-deckify", "why": "remembered", "then": None})
        self.assertTrue(self.run_json("kernel", "try", "linux-cachyos-deckify-vsw", "--yes", "--json")["ok"])
        k = self.run_json("kernel", "--json")["kernel"]
        self.assertTrue(k["trying"])
        self.assertEqual(k["next_boot"], {"package": "linux-cachyos-deckify-vsw", "why": "one-shot",
                                          "then": "linux-cachyos-deckify"})


if __name__ == "__main__":
    unittest.main()
