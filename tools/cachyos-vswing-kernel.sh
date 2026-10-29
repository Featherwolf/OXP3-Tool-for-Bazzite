#!/bin/bash
# Build CachyOS's Handheld Edition kernel, linux-cachyos-deckify 7.2.11-1, with Intel's
# "Vswing / Pre-emphasis Override" fix (mainline Linux 7.4) for the white dots on the OXP3's
# screen, as a second kernel next to the stock one.
#
# It uses CachyOS's own build recipe and sources, pinned to the 7.2.11-1 release, adds the
# eight patches from Linus Torvalds' tree, builds with makepkg and prints the install command.
# Nothing is installed or changed outside $WORK until you run that command yourself.
#
# Run it as your normal user (not with sudo), on AC power:
#   bash cachyos-vswing-kernel.sh
#   WORK=/path/on/disk bash cachyos-vswing-kernel.sh   # build somewhere else (default: ~/kbuild)
#
# Needs about 30 GB free in $WORK and 30-60+ minutes. makepkg asks for your password once to
# install the build tools (base-devel, rust, pahole, ...).
set -euo pipefail

LC_COMMIT=4a8d2ec8509d0da7d5fd5293f42251ea99d5ea3c  # CachyOS/linux-cachyos: "7.2.11-1"
# CachyOS/kernel-patches as of the 7.2.11-1 build (2026-10-18). The recipe's checksum for the handheld patch
# is the one before (17bb0bb), which no longer applies to the 7.2.11-1 source; the published package was
# built with this one (its hid-steam.ko has the functions this version adds and lacks the two it removes).
KP_COMMIT=333b9269015584d1b31f75af9e23d838d7b3d0fe
HANDHELD_OLD=68f4b4cfe9ef3ceffd252af8522d4fa2ce2f0c667a8d2f2427f9e8e31e9e4b3f84c16da8445f420a8411b80a6a74946c685713142f6c1040d1e5b355e2cc5574
HANDHELD_NEW=98239e9d111f4e08a87b872bd19ec41e52674354a969ac9119a9c7f91de43fa0a3f38eb4c8065a4d5a9e890c7f19ed73d9be4cdd1275e99572b79b1a16888987
# The series as merged into torvalds/linux master for 7.4 (merge 5b0764a53235).
VSWING=(
    3018008dc1c3ca1d8c3f0a2362e15a1f47e66616
    c76c03dab828709469558a6bdd55336ef9fd9420
    1b15117625045baa107970a9a1ffb5638ab05414
    a138a81d7805a025a778035c0850642381d1fdf9
    e084812de907c63b0b943b3955bf629df62f6745
    e8fe5008ae3da28c06ccdaf63d0621c0c50f39dc
    7fdf86e530c7f6655aad0612a8c90ab1559984bc
    4bda5543b3c6697ca435e89aeea655eea9087f96
)
SUFFIX=cachyos-deckify-vsw
WORK="${WORK:-$HOME/kbuild}"

die() { echo "error: $*" >&2; exit 1; }
step() { echo; echo "== $*"; }

[ "$(id -u)" -ne 0 ] || die "run this as your normal user, not with sudo (makepkg refuses to run as root)"
command -v pacman >/dev/null || die "this is for CachyOS (pacman not found)"

mkdir -p "$WORK"
WORK="$(cd "$WORK" && pwd)"
[ "$(findmnt -no FSTYPE -T "$WORK" 2>/dev/null || true)" != tmpfs ] ||
    die "$WORK is in RAM (tmpfs); set WORK= to a folder on disk"
free_gb=$(df -Pk "$WORK" | awk 'NR==2 {print int($4 / 1048576)}')
[ "$free_gb" -ge 30 ] || die "$WORK has ${free_gb} GB free; the build needs about 30 GB (set WORK= to another disk)"
on_ac=no
for p in /sys/class/power_supply/*; do
    [ "$(cat "$p/type" 2>/dev/null)" = Mains ] && [ "$(cat "$p/online" 2>/dev/null)" = 1 ] && on_ac=yes
done
[ "$on_ac" = yes ] || echo "warning: not on AC power; the build runs every CPU core flat out for up to an hour"

step "Build tools"
sudo pacman -S --needed base-devel git

step "CachyOS's recipe for linux-cachyos-deckify 7.2.11-1"
cd "$WORK"
[ -d linux-cachyos/.git ] || git clone --filter=blob:none --no-checkout https://github.com/CachyOS/linux-cachyos.git
git -C linux-cachyos cat-file -e "$LC_COMMIT^{commit}" 2>/dev/null || git -C linux-cachyos fetch origin
git -C linux-cachyos checkout -q --force "$LC_COMMIT"
cd linux-cachyos/linux-cachyos-deckify
if ! { grep -qx '_major=7.2' PKGBUILD && grep -qx '_minor=11' PKGBUILD && grep -qx 'pkgrel=1' PKGBUILD; }; then
    die "unexpected PKGBUILD (not 7.2.11-1)"
fi

# The recipe downloads its patches from kernel-patches' moving master branch; pin the ones the published
# 7.2.11-1 package was built with.
sed -i "s|^_patchsource=.*|_patchsource=\"https://raw.githubusercontent.com/cachyos/kernel-patches/$KP_COMMIT/\${_major}\"|" PKGBUILD
[ "$(grep -c "kernel-patches/$KP_COMMIT/" PKGBUILD)" = 1 ] || die "could not pin the patch source"
sed -i "s|'$HANDHELD_OLD'|'$HANDHELD_NEW'|" PKGBUILD
[ "$(grep -c "'$HANDHELD_NEW'" PKGBUILD)" = 1 ] || die "could not update the handheld patch checksum"
# A package of its own (linux-cachyos-deckify-vsw), so the stock kernel stays installed and bootable.
sed -i "s|^    _pkgsuffix=\"cachyos-deckify\"\$|    _pkgsuffix=\"$SUFFIX\"|" PKGBUILD
[ "$(grep -c "_pkgsuffix=\"$SUFFIX\"" PKGBUILD)" = 1 ] || die "could not rename the package"

step "The vswing/pre-emphasis patches from torvalds/linux"
rm -f vswing-*.patch
n=1
for h in "${VSWING[@]}"; do
    curl -fsSL --retry 3 -o "vswing-$n.patch" "https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/patch/?id=$h"
    [ "$(head -c 45 "vswing-$n.patch")" = "From $h" ] || die "vswing-$n.patch is not commit $h"
    n=$((n + 1))
done
cat vswing-[1-8].patch > vswing-override.patch
[ "$(grep -c '^Subject: ' vswing-override.patch)" = 8 ] || die "expected 8 patches in vswing-override.patch"
printf '\nsource+=("vswing-override.patch")\nb2sums+=("%s")\n' "$(b2sum vswing-override.patch | cut -d' ' -f1)" >> PKGBUILD

step "CachyOS's signing keys for the kernel source"
for key in E18447AC260021D31F3FF6C4C8A2A4774B8B63C4 E8B9AA39F054E30E8290D492C3C4820857F654FE; do
    gpg --list-keys "$key" >/dev/null 2>&1 || gpg --recv-keys "$key" ||
        curl -fsSL --retry 3 "https://keys.openpgp.org/vks/v1/by-fingerprint/$key" | gpg --import
    gpg --list-keys "$key" >/dev/null 2>&1 || die "could not get CachyOS's signing key $key"
done

step "Building (30-60+ minutes); the log must show 'Applying patch vswing-override.patch...'"
mkdir -p "$WORK/pkg"
BUILDDIR="$WORK/build" PKGDEST="$WORK/pkg" makepkg -s --cleanbuild
rm -rf "$WORK/build"

step "Done. Install it next to your stock kernel with:"
packages=("$WORK/pkg/linux-$SUFFIX-7.2.11-1-"*.pkg.tar.zst "$WORK/pkg/linux-$SUFFIX-headers-7.2.11-1-"*.pkg.tar.zst)
echo "  sudo pacman -U ${packages[*]}"
echo "Limine then gets a 'linux-$SUFFIX' entry; the stock linux-cachyos-deckify entry stays."
