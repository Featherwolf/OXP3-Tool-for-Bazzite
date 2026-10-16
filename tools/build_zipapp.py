#!/usr/bin/env python3
"""Build dist/oxp3-tool: the whole tool as one executable file (a Python zipapp).

    python3 tools/build_zipapp.py            # writes dist/oxp3-tool and dist/oxp3-tool.sha256
    python3 tools/build_zipapp.py -o FILE    # writes FILE only

Run it after changing anything in oxp3_tool/; the tests fail while the
committed dist/ file is out of date.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "oxp3_tool"
DIST = ROOT / "dist"
NAME = "oxp3-tool"

# -I: ignore PYTHON* variables, the user's site-packages and the current
# directory (the archive itself stays on sys.path, so the package imports).
SHEBANG = b"#!/usr/bin/python3 -I\n"

MAIN = b'''\
"""Entry point of the oxp3-tool single-file build."""

import sys

from oxp3_tool.cli import main

sys.exit(main())
'''

# Fixed timestamp so the same sources always give the same archive.
STAMP = (2026, 1, 1, 0, 0, 0)


def members() -> list[tuple[str, bytes]]:
    files = [("__main__.py", MAIN)]
    for path in sorted(PACKAGE.glob("*.py")):
        files.append((f"oxp3_tool/{path.name}", path.read_bytes()))
    return files


def build() -> bytes:
    buf = io.BytesIO()
    buf.write(SHEBANG)
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members():
            info = zipfile.ZipInfo(name, date_time=STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    return buf.getvalue()


def write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("-o", "--output", type=Path, help="write only this file")
    args = p.parse_args(argv)
    data = build()
    if args.output:
        write(args.output, data, 0o755)
        print(f"wrote {args.output}")
        return 0
    write(DIST / NAME, data, 0o755)
    digest = hashlib.sha256(data).hexdigest()
    write(DIST / f"{NAME}.sha256", f"{digest}  {NAME}\n".encode(), 0o644)
    print(f"wrote {DIST / NAME} ({len(data)} bytes, sha256 {digest})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
