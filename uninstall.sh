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

# Let the tool settle its records first (a revert finishes at the reboot).
if [[ -x "$BIN" ]]; then
    "$BIN" status >/dev/null 2>&1 || true
fi

if [[ -f "$STATE" && "${1:-}" != "--keep-fixes" ]]; then
    echo "Some fixes are still applied (see: oxp3-tool status)." >&2
    echo "Revert them first with: sudo oxp3-tool revert --all" >&2
    echo "or run '$0 --keep-fixes' to remove only the program." >&2
    exit 1
fi

rm -f "$BIN"
rm -rf "$DEST"

# Keep anything revert left for the user (backups of their own files,
# copies of files they had edited); remove the directory only when empty.
SDIR=/var/lib/oxp3-tool
if [[ ! -f "$STATE" && -d "$SDIR" ]]; then
    find "$SDIR" -mindepth 1 -depth -type d -empty -delete
    if [[ -n "$(find "$SDIR" -mindepth 1 -not -type d -print -quit)" ]]; then
        echo "Kept $SDIR, which still holds copies of your files:"
        find "$SDIR" -mindepth 1 -not -type d
        echo "Delete it once you no longer need them: sudo rm -rf $SDIR"
    else
        rmdir "$SDIR" 2>/dev/null || true
    fi
fi
echo "oxp3-tool removed."
