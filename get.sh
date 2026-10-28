#!/usr/bin/env bash
# Download and install oxp3-tool (one file) from GitHub:
#
#   curl -fsSL https://raw.githubusercontent.com/Featherwolf/OXP3-Tool-for-Bazzite/HEAD/get.sh | sudo bash
#
# Run it again to update. It installs /usr/local/bin/oxp3-tool (writable on
# Bazzite, survives OS updates on Bazzite and CachyOS) and an "OXP3 Tool" entry in the Desktop Mode
# app menu. Options: --file PATH installs a local build instead of
# downloading; --no-menu skips the app menu entry. OXP3_TOOL_REF picks a
# branch or tag (default: the repository's default branch); OXP3_TOOL_URL
# replaces the whole download location (a mirror or a fork's dist/ folder).
set -euo pipefail

REPO="${OXP3_TOOL_REPO:-Featherwolf/OXP3-Tool-for-Bazzite}"
REF="${OXP3_TOOL_REF:-HEAD}"
PREFIX="${PREFIX:-/usr/local}"
BIN="$PREFIX/bin/oxp3-tool"
DESKTOP="$PREFIX/share/applications/oxp3-tool.desktop"
URL="${OXP3_TOOL_URL:-https://raw.githubusercontent.com/$REPO/$REF/dist}"

file=""
menu=1
while [[ $# -gt 0 ]]; do
    case "$1" in
        --file) file="${2:?--file needs a path}"; shift 2 ;;
        --no-menu) menu=0; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "Run this as root, e.g.: curl -fsSL https://raw.githubusercontent.com/$REPO/HEAD/get.sh | sudo bash" >&2
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required (on CachyOS: sudo pacman -S python)." >&2
    exit 1
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if [[ -n "$file" ]]; then
    cp "$file" "$tmp/oxp3-tool"
else
    echo "Downloading oxp3-tool from $URL ..."
    curl -fsSL --retry 3 -o "$tmp/oxp3-tool" "$URL/oxp3-tool"
    curl -fsSL --retry 3 -o "$tmp/oxp3-tool.sha256" "$URL/oxp3-tool.sha256"
    # Catches a truncated or mangled download (both files come from the same place).
    (cd "$tmp" && sha256sum --check --quiet oxp3-tool.sha256)
fi

# Refuse anything that does not run as oxp3-tool.
version="$(python3 -I "$tmp/oxp3-tool" --version)"
case "$version" in
    oxp3-tool\ *) ;;
    *) echo "The downloaded file is not oxp3-tool: $version" >&2; exit 1 ;;
esac

# An older install.sh put the program in $PREFIX/lib/oxp3-tool with a symlink.
rm -rf "$PREFIX/lib/oxp3-tool"
rm -f "$BIN"
install -D -m 0755 "$tmp/oxp3-tool" "$BIN"

if [[ $menu -eq 1 ]] && command -v konsole >/dev/null 2>&1; then
    install -d -m 0755 "$(dirname "$DESKTOP")"
    cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=OXP3 Tool
Comment=Controller and wake-from-sleep fixes for the OneXPlayer 3
Icon=input-gaming
Exec=konsole --hold -e sh -c "sudo $BIN status; sudo $BIN apply"
TryExec=konsole
Terminal=false
Categories=System;Settings;
EOF
    chmod 0644 "$DESKTOP"
fi

if command -v restorecon >/dev/null 2>&1; then
    restorecon "$BIN" >/dev/null 2>&1 || true
    if [[ -f "$DESKTOP" ]]; then
        restorecon "$DESKTOP" >/dev/null 2>&1 || true
    fi
fi

echo "Installed $version to $BIN"
if [[ -f "$DESKTOP" ]]; then
    echo "Desktop Mode app menu: OXP3 Tool"
fi
echo "Next: sudo oxp3-tool status, then sudo oxp3-tool apply"
