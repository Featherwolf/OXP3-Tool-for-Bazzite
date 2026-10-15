#!/usr/bin/env bash
# Install oxp3-tool under /usr/local. On Bazzite /usr/local is writable (it
# lives in /var/usrlocal), so this survives OS updates without layering.
#
#   sudo ./install.sh
#
# Running from the source tree without installing works too:
#   sudo ./bin/oxp3-tool status
set -euo pipefail

PREFIX="${PREFIX:-/usr/local}"
DEST="$PREFIX/lib/oxp3-tool"
BIN="$PREFIX/bin/oxp3-tool"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "Run this with sudo: sudo $0" >&2
    exit 1
fi
if [[ ! -f "$SRC/oxp3_tool/cli.py" || ! -f "$SRC/bin/oxp3-tool" ]]; then
    echo "Run install.sh from the oxp3-tool source directory." >&2
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required." >&2
    exit 1
fi

mkdir -p "$PREFIX/lib" "$PREFIX/bin"
staging="$(mktemp -d "$PREFIX/lib/.oxp3-tool.XXXXXX")"
trap 'rm -rf "$staging"' EXIT

install -d -m 0755 "$staging/oxp3_tool" "$staging/bin"
install -m 0644 "$SRC"/oxp3_tool/*.py "$staging/oxp3_tool/"
install -m 0755 "$SRC/bin/oxp3-tool" "$staging/bin/oxp3-tool"
chmod 0755 "$staging"

rm -rf "$DEST"
mv "$staging" "$DEST"
trap - EXIT
ln -sfn "../lib/oxp3-tool/bin/oxp3-tool" "$BIN"
if command -v restorecon >/dev/null 2>&1; then
    restorecon -R "$DEST" "$BIN" >/dev/null 2>&1 || true
fi

echo "Installed $("$BIN" --version) to $DEST"
echo "Next: oxp3-tool status"
