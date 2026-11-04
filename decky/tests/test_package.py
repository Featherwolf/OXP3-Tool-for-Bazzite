"""The manifest files and tools/build_zip.py (the OXP3Deck.zip users install with Decky)."""
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
REPO = PLUGIN.parent
BUILDER = PLUGIN / "tools" / "build_zip.py"
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$")


def load_builder():
    name = "oxp3deck_build_zip"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, BUILDER)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod   # dataclasses look the module up there
        spec.loader.exec_module(mod)
    return sys.modules[name]


def mode_of(info: zipfile.ZipInfo) -> int:
    return info.external_attr >> 16


class ManifestTest(unittest.TestCase):
    def test_plugin_json(self):
        manifest = json.loads((PLUGIN / "plugin.json").read_text())
        self.assertEqual(manifest["name"], "OXP3Deck")
        self.assertEqual(manifest["flags"], ["root"])   # "_root" or no flag: every root write fails
        self.assertEqual(manifest["api_version"], 1)
        image = manifest.get("publish", {}).get("image")
        if image:   # only an asset that exists in the repository
            rel = image.split("/HEAD/", 1)[-1]
            self.assertTrue((REPO / rel).is_file(), image)

    def test_package_json(self):
        package = json.loads((PLUGIN / "package.json").read_text())
        self.assertRegex(package["version"], SEMVER)
        self.assertEqual(package["type"], "module")
        self.assertEqual(package["scripts"]["zip"], "python3 tools/build_zip.py")


class FakeTreeTest(unittest.TestCase):
    """build_zip against a fake plugin folder and repository, so it runs without pnpm build."""

    def setUp(self):
        self.bz = load_builder()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.plugin, self.repo = base / "repo" / "decky", base / "repo"
        files = {
            "plugin.json": json.dumps({"name": "OXP3Deck", "flags": ["root"], "api_version": 1}),
            "package.json": json.dumps({"name": "oxp3deck", "version": "1.2.3", "type": "module"}),
            "main.py": "# main\n",
            "dist/index.js": 'const manifest = {"name":"OXP3Deck"};\n',
            "py_modules/oxp3deck/__init__.py": "",
            "py_modules/oxp3deck/env.py": "X = 1\n",
            "py_modules/oxp3deck/notes.txt": "not packed\n",
            "py_modules/oxp3deck/__pycache__/env.cpython-313.pyc": "junk",
            "LICENSE": "MIT\n", "README.md": "# r\n", "CREDITS.md": "# c\n",
        }
        for rel, text in files.items():
            p = self.plugin / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        self.tool = b"#!/usr/bin/python3 -I\nfake"
        (self.repo / "dist").mkdir()
        (self.repo / "dist" / "oxp3-tool").write_bytes(self.tool)
        self.write_sums(hashlib.sha256(self.tool).hexdigest())
        self.out = base / "out" / "OXP3Deck.zip"

    def write_sums(self, digest):
        (self.repo / "dist" / "oxp3-tool.sha256").write_text(f"{digest}  oxp3-tool\n")

    def build(self, out=None):
        return self.bz.build(out or self.out, plugin=self.plugin, repo=self.repo)

    def test_contents(self):
        out, digest, plan = self.build()
        self.assertEqual(hashlib.sha256(out.read_bytes()).hexdigest(), digest)
        self.assertEqual(plan.version, "1.2.3")
        with zipfile.ZipFile(out) as zf:
            files = [i.filename for i in zf.infolist() if not i.is_dir()]
            self.assertEqual(zf.read("OXP3Deck/bin/oxp3-tool"), self.tool)
        self.assertEqual(sorted(files), sorted(f"OXP3Deck/{n}" for n in [
            "CREDITS.md", "LICENSE", "README.md", "bin/oxp3-tool", "bin/oxp3-tool.sha256", "dist/index.js",
            "main.py", "package.json", "plugin.json", "py_modules/oxp3deck/__init__.py",
            "py_modules/oxp3deck/env.py"]))

    def test_needs_the_frontend_build(self):
        (self.plugin / "dist" / "index.js").unlink()
        with self.assertRaisesRegex(self.bz.BuildError, "dist/index.js is missing: run pnpm build first"):
            self.build()
        self.assertFalse(self.out.exists())

    def test_checksum_mismatch(self):
        self.write_sums("0" * 64)
        with self.assertRaisesRegex(self.bz.BuildError, "does not match"):
            self.build()

    def test_missing_backend_or_docs(self):
        for rel in ("main.py", "CREDITS.md"):
            (self.plugin / rel).rename(self.plugin / (rel + ".x"))
            with self.assertRaisesRegex(self.bz.BuildError, f"{re.escape(rel)} is missing"):
                self.build()
            (self.plugin / (rel + ".x")).rename(self.plugin / rel)

    def test_refuses_a_symlink(self):
        target = Path(self.tmp.name) / "elsewhere.py"
        target.write_text("SECRET = 1\n")
        os.symlink(target, self.plugin / "py_modules" / "oxp3deck" / "link.py")
        with self.assertRaisesRegex(self.bz.BuildError, "symbolic link"):
            self.build()

    def test_not_root(self):
        (self.plugin / "plugin.json").write_text(json.dumps({"name": "OXP3Deck", "flags": ["_root"]}))
        with self.assertRaisesRegex(self.bz.BuildError, "root"):
            self.build()

    def test_warnings(self):
        _out, _digest, plan = self.build(Path(self.tmp.name) / "other.zip")
        self.assertTrue(any("OXP3Deck.zip" in w for w in plan.warnings), plan.warnings)
        (self.plugin / "dist" / "index.js").write_text("// built for another name\n")
        os.utime(self.plugin / "dist" / "index.js", (1, 1))   # older than src/
        (self.plugin / "src").mkdir()
        (self.plugin / "src" / "index.tsx").write_text("")
        _out, _digest, plan = self.build()
        self.assertTrue(any("not built for plugin" in w for w in plan.warnings), plan.warnings)
        self.assertTrue(any("older than src/index.tsx" in w for w in plan.warnings), plan.warnings)


@unittest.skipUnless((PLUGIN / "dist" / "index.js").is_file(), "dist/index.js is missing: run pnpm build")
class RealBuildTest(unittest.TestCase):
    """The zip from this tree, built twice with the real command."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.zips, cls.stdout = [], []
        for i in (1, 2):
            out = base / str(i) / "OXP3Deck.zip"
            res = subprocess.run([sys.executable, "-I", str(BUILDER), "--out", str(out)], capture_output=True,
                                 text=True, cwd=str(PLUGIN), stdin=subprocess.DEVNULL)
            if res.returncode != 0:
                cls.tmp.cleanup()
                raise AssertionError(f"build_zip failed: {res.stderr.strip()}")
            cls.zips.append(out)
            cls.stdout.append(res.stdout)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.zf = zipfile.ZipFile(self.zips[0])
        self.addCleanup(self.zf.close)
        self.infos = self.zf.infolist()

    def test_reproducible(self):
        self.assertEqual(self.zips[0].read_bytes(), self.zips[1].read_bytes())

    def test_prints_path_and_sha256(self):
        digest = hashlib.sha256(self.zips[0].read_bytes()).hexdigest()
        lines = self.stdout[0].splitlines()
        self.assertIn(str(self.zips[0]), lines)
        self.assertIn(f"sha256 {digest}", lines)

    def test_one_top_folder(self):
        self.assertEqual({i.filename.split("/", 1)[0] for i in self.infos}, {"OXP3Deck"})
        # Decky accepts a zip only with exactly one <folder>/plugin.json
        self.assertEqual([n for n in self.zf.namelist() if n.endswith("/plugin.json") and n.count("/") == 1],
                         ["OXP3Deck/plugin.json"])

    def test_file_list(self):
        files = {i.filename[len("OXP3Deck/"):] for i in self.infos if not i.is_dir()}
        dist = {p.relative_to(PLUGIN).as_posix() for p in (PLUGIN / "dist").rglob("*") if p.is_file()}
        backend = {f"py_modules/oxp3deck/{p.name}" for p in (PLUGIN / "py_modules" / "oxp3deck").glob("*.py")}
        expected = {"plugin.json", "package.json", "main.py", "bin/oxp3-tool", "bin/oxp3-tool.sha256",
                    "LICENSE", "README.md", "CREDITS.md"} | dist | backend
        self.assertEqual(files, expected)
        self.assertIn("dist/index.js", files)
        self.assertIn("py_modules/oxp3deck/__init__.py", files)
        for i in self.infos:
            if not i.is_dir():
                self.assertEqual(self.zf.read(i), (self.source(i.filename)).read_bytes(), i.filename)

    def source(self, name):
        rel = name[len("OXP3Deck/"):]
        return REPO / "dist" / rel[len("bin/"):] if rel.startswith("bin/") else PLUGIN / rel

    def test_no_bytecode(self):
        for name in self.zf.namelist():
            self.assertNotIn("__pycache__", name)
            self.assertFalse(name.endswith((".pyc", ".pyo")), name)

    def test_fixed_metadata(self):
        names = [i.filename for i in self.infos]
        self.assertEqual(names, sorted(names))
        for i in self.infos:
            self.assertEqual(i.date_time, (1980, 1, 1, 0, 0, 0), i.filename)
            mode = mode_of(i)
            if i.is_dir():
                self.assertTrue(stat.S_ISDIR(mode), i.filename)
                self.assertEqual(stat.S_IMODE(mode), 0o755, i.filename)
            else:
                self.assertTrue(stat.S_ISREG(mode), i.filename)
                self.assertEqual(i.compress_type, zipfile.ZIP_DEFLATED, i.filename)
                want = 0o755 if i.filename == "OXP3Deck/bin/oxp3-tool" else 0o644
                self.assertEqual(stat.S_IMODE(mode), want, i.filename)

    def test_bundled_tool_matches_its_checksum(self):
        tool = self.zf.read("OXP3Deck/bin/oxp3-tool")
        self.assertEqual(self.zf.read("OXP3Deck/bin/oxp3-tool.sha256").decode().split(),
                         [hashlib.sha256(tool).hexdigest(), "oxp3-tool"])

    def test_bundled_tool_is_new_enough(self):
        """OXP-1: the plugin offers its own copy when the installed one is older than MIN_VERSION; a copy below it
        would be offered and then refused by the install's own version gate."""
        from oxp3deck import oxp3tool
        version = load_builder()._tool_version(self.zf.read("OXP3Deck/bin/oxp3-tool"))
        self.assertGreaterEqual(oxp3tool.parse_version(version), oxp3tool.MIN_VERSION, version)
        self.assertIn(f"with oxp3-tool {version}", self.stdout[0])


if __name__ == "__main__":
    unittest.main()
