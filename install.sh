#!/usr/bin/env bash
# Install oxp3-tool from this source tree (builds the single-file program
# and installs it the same way get.sh does):
#
#   sudo ./install.sh [--no-menu]
#
# Running from the source tree without installing works too:
#   sudo ./bin/oxp3-tool status
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "Run this with sudo: sudo $0" >&2
    exit 1
fi
if [[ ! -f "$SRC/tools/build_zipapp.py" || ! -f "$SRC/get.sh" ]]; then
    echo "Run install.sh from the oxp3-tool source directory." >&2
    exit 1
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
python3 -I "$SRC/tools/build_zipapp.py" --output "$tmp/oxp3-tool" >/dev/null
bash "$SRC/get.sh" --file "$tmp/oxp3-tool" "$@"
