#!/usr/bin/env bash
# Remove oxp3-tool from /usr/local. Revert its fixes first:
#   sudo oxp3-tool revert --all
#   curl -fsSL https://raw.githubusercontent.com/Featherwolf/OXP3-Tool-for-Bazzite/HEAD/uninstall.sh | sudo bash
# (or `sudo ./uninstall.sh` from the source tree). --keep-fixes removes only
# the program and leaves applied fixes in place.
set -euo pipefail

PREFIX="${PREFIX:-/usr/local}"
DEST="$PREFIX/lib/oxp3-tool"
BIN="$PREFIX/bin/oxp3-tool"
DESKTOP="$PREFIX/share/applications/oxp3-tool.desktop"
STATE=/var/lib/oxp3-tool/state.json

if [[ $EUID -ne 0 ]]; then
    echo "Run this as root (with sudo)." >&2
    exit 1
fi

# Let the tool settle its records first (a revert finishes at the reboot).
if [[ -x "$BIN" ]]; then
    "$BIN" status >/dev/null 2>&1 || true
fi

if [[ -f "$STATE" && "${1:-}" != "--keep-fixes" ]]; then
    echo "oxp3-tool still tracks fixes (see: sudo oxp3-tool status)." >&2
    echo "Run: sudo oxp3-tool revert --all, and reboot if it or status says 'reboot to finish'; then run this again." >&2
    echo "or pass --keep-fixes to remove only the program." >&2
    exit 1
fi

# `oxp3-tool kernel try` turned Limine's remember_last_entry off (a comment above the line says so).
if [[ "${1:-}" != "--keep-fixes" ]]; then
    for conf in /boot/limine.conf /efi/limine.conf /boot/efi/limine.conf; do
        if [[ -f "$conf" ]] && grep -q '^[[:space:]]*# oxp3-tool kernel try:' "$conf"; then
            echo "$conf has remember_last_entry: no from \`oxp3-tool kernel try\`." >&2
            echo "Run: sudo oxp3-tool kernel keep (on the kernel to keep booting); then run this again," >&2
            echo "or pass --keep-fixes to leave it as it is." >&2
            exit 1
        fi
    done
fi

rm -f "$BIN" "$DESKTOP"
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
