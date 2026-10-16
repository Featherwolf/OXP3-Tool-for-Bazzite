"""The committed single-file build (dist/oxp3-tool) must match the sources."""

import hashlib
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path

from oxp3_tool import __version__

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import build_zipapp  # noqa: E402

DIST = ROOT / "dist" / "oxp3-tool"


class DistTest(unittest.TestCase):
    def test_dist_matches_sources(self):
        self.assertTrue(DIST.exists(), "run: python3 tools/build_zipapp.py")
        data = DIST.read_bytes()
        self.assertTrue(data.startswith(build_zipapp.SHEBANG))
        with zipfile.ZipFile(DIST) as zf:
            committed = {name: zf.read(name) for name in zf.namelist()}
        expected = dict(build_zipapp.members())
        self.assertEqual(committed, expected, "dist/oxp3-tool is stale: run python3 tools/build_zipapp.py")

    def test_checksum_file(self):
        line = (ROOT / "dist" / "oxp3-tool.sha256").read_text().split()
        self.assertEqual(line, [hashlib.sha256(DIST.read_bytes()).hexdigest(), "oxp3-tool"])

    def test_runs(self):
        res = subprocess.run([sys.executable, "-I", str(DIST), "--version"], capture_output=True, text=True,
                             check=True)
        self.assertEqual(res.stdout.strip(), f"oxp3-tool {__version__}")

    def test_build_is_reproducible(self):
        self.assertEqual(build_zipapp.build(), build_zipapp.build())


if __name__ == "__main__":
    unittest.main()
