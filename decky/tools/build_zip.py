#!/usr/bin/env python3
"""Build release/OXP3Deck.zip, the file Decky installs with "Install Plugin from URL" or "from ZIP File".

    pnpm build && pnpm zip                   # writes decky/release/OXP3Deck.zip
    python3 tools/build_zip.py --out FILE    # writes FILE instead

The zip holds one folder named after plugin.json's "name": the frontend (dist/), the backend (main.py and
py_modules/oxp3deck/*.py), a copy of oxp3-tool (the repository's dist/oxp3-tool, checked against its .sha256)
and the docs. The same inputs always give the same bytes: sorted entries, fixed times and modes.

Keep the file name <name>.zip: Decky takes the plugin's name from the URL's or the file's name, and only finds
the installed copy to replace (and removes its old files) when that matches plugin.json.

Runs on the developer's machine with the standard library only, not inside Decky.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import re
import stat
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent   # decky/
REPO = PLUGIN.parent

# The earliest time a zip can hold; any fixed value keeps two builds identical.
STAMP = (1980, 1, 1, 0, 0, 0)
FILE_MODE = 0o644
EXEC_MODE = 0o755
DIR_MODE = 0o755
EXECUTABLES = {"bin/oxp3-tool"}
DOCS = ("LICENSE", "README.md", "CREDITS.md")
TOOL = "oxp3-tool"
# The name is also the plugin's folder and the zip's file name, and Decky builds paths from it.
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class BuildError(Exception):
    pass


@dataclass
class Plan:
    top: str
    version: str
    tool_version: str | None
    files: list[tuple[str, Path]] = field(default_factory=list)   # (path inside the top folder, source)
    warnings: list[str] = field(default_factory=list)


def _load_json(path: Path, label: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise BuildError(f"{label} is missing") from e
    except (OSError, ValueError) as e:
        raise BuildError(f"{label}: {e}") from e
    if not isinstance(data, dict):
        raise BuildError(f"{label}: not a JSON object")
    return data


def _regular(path: Path, label: str, hint: str = "") -> Path:
    # A link could pull in a file from anywhere on the build machine.
    if path.is_symlink():
        raise BuildError(f"{label} is a symbolic link")
    if not path.is_file():
        raise BuildError(f"{label} is missing" + (f": {hint}" if hint else ""))
    return path


def _tool_version(data: bytes) -> str | None:
    """The __version__ inside the oxp3-tool zipapp (a zip with a shebang line in front)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            text = zf.read("oxp3_tool/__init__.py").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError, OSError):
        return None
    m = re.search(r"""^__version__\s*=\s*["']([^"']+)["']""", text, re.M)
    return m.group(1) if m else None


def _tool_is_stale(repo: Path, data: bytes) -> bool | None:
    """Compare dist/oxp3-tool with a fresh build of oxp3_tool/ (None: the builder is not there)."""
    builder = repo / "tools" / "build_zipapp.py"
    if not builder.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("_oxp3_build_zipapp", builder)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.build() != data
    except Exception:   # only a warning depends on it
        return None


def _dist_files(plugin: Path) -> list[tuple[str, Path]]:
    dist = plugin / "dist"
    _regular(dist / "index.js", "dist/index.js", "run pnpm build first")
    out = []
    # rollup empties dist/ before each build, so everything in it belongs to the current build.
    for dirpath, dirnames, filenames in os.walk(dist):
        dirnames.sort()
        for name in sorted(filenames):
            src = Path(dirpath) / name
            rel = src.relative_to(plugin).as_posix()
            if src.is_symlink() or not src.is_file():
                raise BuildError(f"{rel}: not a regular file")
            out.append((rel, src))
    return out


def _backend_files(plugin: Path, warnings: list[str]) -> list[tuple[str, Path]]:
    pkg = plugin / "py_modules" / "oxp3deck"
    _regular(pkg / "__init__.py", "py_modules/oxp3deck/__init__.py")
    out = [("main.py", _regular(plugin / "main.py", "main.py"))]
    for entry in sorted(pkg.iterdir()):
        if entry.name == "__pycache__":
            continue
        if entry.is_dir():
            warnings.append(f"py_modules/oxp3deck/{entry.name}/ is not packed (only the package's own .py files are)")
        elif entry.suffix == ".py" and not entry.name.startswith("."):
            out.append((f"py_modules/oxp3deck/{entry.name}", _regular(entry, entry.name)))
    return out


def _tool_files(repo: Path, warnings: list[str]) -> tuple[list[tuple[str, Path]], str | None]:
    hint = "run python3 tools/build_zipapp.py in the repository root"
    tool = _regular(repo / "dist" / TOOL, f"../dist/{TOOL}", hint)
    sums = _regular(repo / "dist" / f"{TOOL}.sha256", f"../dist/{TOOL}.sha256", hint)
    data = tool.read_bytes()
    words = sums.read_text(encoding="utf-8", errors="replace").split()
    if len(words) != 2 or words[1] != TOOL or not re.fullmatch(r"[0-9a-f]{64}", words[0]):
        raise BuildError(f"../dist/{TOOL}.sha256 is not '<sha256>  {TOOL}': {hint}")
    if hashlib.sha256(data).hexdigest() != words[0]:
        raise BuildError(f"../dist/{TOOL} does not match ../dist/{TOOL}.sha256: {hint}")
    if _tool_is_stale(repo, data):
        warnings.append(f"../dist/{TOOL} is older than oxp3_tool/: {hint}")
    return [(f"bin/{TOOL}", tool), (f"bin/{TOOL}.sha256", sums)], _tool_version(data)


def _newest(folder: Path) -> Path | None:
    newest = None
    for dirpath, _dirs, filenames in os.walk(folder):
        for name in filenames:
            p = Path(dirpath) / name
            if newest is None or p.stat().st_mtime > newest.stat().st_mtime:
                newest = p
    return newest


def collect(plugin: Path = PLUGIN, repo: Path = REPO) -> Plan:
    manifest = _load_json(plugin / "plugin.json", "plugin.json")
    name = manifest.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise BuildError(f"plugin.json: name {name!r} must be letters, digits, '.', '_' or '-'")
    if "root" not in (manifest.get("flags") or []):
        raise BuildError('plugin.json: flags must include "root" (the backend runs oxp3-tool as root)')
    package = _load_json(plugin / "package.json", "package.json")
    version = package.get("version")
    if not isinstance(version, str) or not version:
        raise BuildError("package.json: no version (Decky shows it as the plugin's version)")

    warnings: list[str] = []
    files = [("plugin.json", plugin / "plugin.json"), ("package.json", plugin / "package.json")]
    files += _dist_files(plugin)
    index = (plugin / "dist" / "index.js").read_text(encoding="utf-8", errors="replace")
    if f'"name":{json.dumps(name)}' not in index:
        warnings.append(f"dist/index.js was not built for plugin {name!r}: run pnpm build")
    newest = _newest(plugin / "src")
    if newest and newest.stat().st_mtime > (plugin / "dist" / "index.js").stat().st_mtime:
        warnings.append(f"dist/index.js is older than {newest.relative_to(plugin).as_posix()}: run pnpm build")
    files += _backend_files(plugin, warnings)
    tool_files, tool_version = _tool_files(repo, warnings)
    files += tool_files
    files += [(doc, _regular(plugin / doc, doc)) for doc in DOCS]
    return Plan(top=name, version=version, tool_version=tool_version, files=files, warnings=warnings)


def _info(name: str, mode: int, is_dir: bool) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(name, date_time=STAMP)
    zi.create_system = 3   # Unix, so unzip and Python read the mode bits below
    zi.external_attr = ((stat.S_IFDIR if is_dir else stat.S_IFREG) | mode) << 16
    if is_dir:
        zi.external_attr |= 0x10   # the MS-DOS directory bit
        zi.compress_type = zipfile.ZIP_STORED
    else:
        zi.compress_type = zipfile.ZIP_DEFLATED
    return zi


def pack(plan: Plan) -> bytes:
    entries: dict[str, Path | None] = {f"{plan.top}/": None}
    for rel, src in plan.files:
        parts = rel.split("/")
        for i in range(1, len(parts)):
            entries[f"{plan.top}/{'/'.join(parts[:i])}/"] = None
        entries[f"{plan.top}/{rel}"] = src
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in sorted(entries):
            src = entries[name]
            if src is None:
                zf.writestr(_info(name, DIR_MODE, True), b"")
                continue
            rel = name[len(plan.top) + 1:]
            mode = EXEC_MODE if rel in EXECUTABLES else FILE_MODE
            zf.writestr(_info(name, mode, False), src.read_bytes(), compresslevel=9)
    return buf.getvalue()


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def build(out: Path | None = None, plugin: Path = PLUGIN, repo: Path = REPO) -> tuple[Path, str, Plan]:
    """Write the zip; returns its path, its sha256 and what went into it."""
    plan = collect(plugin, repo)
    if out is None:
        out = plugin / "release" / f"{plan.top}.zip"
    elif out.name != f"{plan.top}.zip":
        plan.warnings.append(f"{out.name}: Decky only replaces an installed copy from a file named {plan.top}.zip")
    data = pack(plan)
    write_atomic(out, data)
    return out, hashlib.sha256(data).hexdigest(), plan


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", type=Path, help="where to write the zip (default: release/<name>.zip)")
    args = p.parse_args(argv)
    try:
        out, digest, plan = build(args.out.resolve() if args.out else None)
    except (BuildError, OSError) as e:
        print(f"build_zip: {e}", file=sys.stderr)
        return 1
    for w in plan.warnings:
        print(f"warning: {w}", file=sys.stderr)
    print(f"{plan.top} {plan.version} with oxp3-tool {plan.tool_version or 'unknown'}")
    print(out)
    print(f"sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
