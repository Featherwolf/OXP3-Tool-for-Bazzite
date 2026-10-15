#!/usr/bin/env bash
# Remove oxp3-tool from /usr/local. Revert its fixes first:
#   sudo oxp3-tool revert --all
#   sudo ./uninstall.sh
set -euo pipefail

PREFIX="${PREFIX:-/usr/local}"
DEST="$PREFIX/lib/oxp3-tool"
BIN="$PREFIX/bin/oxp3-tool"
STATE=/var/lib/oxp3-tool/state.json

if [[ $EUID -ne 0 ]]; then
    echo "Run this with sudo: sudo $0" >&2
    exit 1
fi

if [[ -f "$STATE" && "${1:-}" != "--keep-fixes" ]]; then
    echo "Some fixes are still applied (see: oxp3-tool status)." >&2
    echo "Revert them first with: sudo oxp3-tool revert --all" >&2
    echo "or run '$0 --keep-fixes' to remove only the program." >&2
    exit 1
fi

rm -f "$BIN"
rm -rf "$DEST"
if [[ ! -f "$STATE" ]]; then
    rm -rf /var/lib/oxp3-tool
fi
echo "oxp3-tool removed."
