"""Regenerate tests/fixtures/oxp3/*.json: real `oxp3-tool ... --json` output of this repository's CLI, run in-process
against the fake ONEXPLAYER 3 systems of the repository's own tests (tests/fakeroot.py).

Run from anywhere: python3 decky/tests/make_fixtures.py
The fake root's temporary path is replaced with "" so the files do not change with it; everything else is the CLI's
own output (timestamps included).
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
OUT = HERE / "fixtures" / "oxp3"
KERNELS = {"linux-cachyos-deckify": "7.2.11-1-cachyos-deckify",
           "linux-cachyos-deckify-vsw": "7.2.11-1-cachyos-deckify-vsw"}


def load_fakeroot():
    """The repository's tests/fakeroot.py, by path: decky/tests is also a package called `tests`."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    spec = importlib.util.spec_from_file_location("oxp3_repo_fakeroot", REPO / "tests" / "fakeroot.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def run(fake, *argv) -> tuple[int, str]:
    from oxp3_tool.cli import main
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = main(list(argv), system=fake.system())
        except SystemExit as e:   # a usage error: still one JSON object on stdout
            rc = e.code
    if err.getvalue():
        raise RuntimeError(f"{argv}: printed to stderr: {err.getvalue()!r}")
    return rc, out.getvalue()


def save(name: str, text: str, root: Path) -> None:
    obj = json.loads(text.replace(str(root), ""))   # exactly one object, like the plugin expects
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(obj, indent=1, sort_keys=True) + "\n")
    print(f"{name}: rc {obj.get('rc')}, ok {obj.get('ok')}")


def main() -> int:
    fr = load_fakeroot()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp, "root")
        cachy = fr.FakeCachyOS(root, kernels=dict(KERNELS))
        cachy.power_on()
        for name, argv in (
            ("status-cachyos.json", ["status", "--json"]),
            ("list.json", ["list", "--json"]),
            ("kernel.json", ["kernel", "--json"]),
            ("apply-dry-run.json", ["apply", "buttons", "volume-keys", "--home", "guide", "--fps-limit", "60",
                                    "--dry-run", "--json"]),
            ("apply-unknown-fix.json", ["apply", "no-such-fix", "--yes", "--json"]),
            ("usage-error.json", ["apply", "buttons", "--home", "nope", "--json"]),
            ("apply-nvme.json", ["apply", "nvme-resume", "--any-ssd", "--yes", "--json"]),
            ("status-reboot-pending.json", ["status", "--json"]),
            ("revert-nvme.json", ["revert", "nvme-resume", "--yes", "--json"]),
            ("kernel-try.json", ["kernel", "try", "linux-cachyos-deckify-vsw", "--yes", "--json"]),
            ("kernel-trying.json", ["kernel", "--json"]),
            ("kernel-keep.json", ["kernel", "keep", "--yes", "--json"]),
            ("diagnose.json", ["diagnose", "-o", str(root / "run/oxp3deck/oxp3-diagnose-20261104-120000.tar.gz"),
                               "--json"]),
        ):
            if name == "diagnose.json":
                os.makedirs(root / "run/oxp3deck", exist_ok=True)
            save(name, run(cachy, *argv)[1], root)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp, "root")
        bazzite = fr.FakeOxp3(root)
        bazzite.build()
        save("status-bazzite.json", run(bazzite, "status", "--json")[1], root)
        save("kernel-bazzite.json", run(bazzite, "kernel", "--json")[1], root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
