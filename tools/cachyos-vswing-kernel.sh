#!/bin/bash
# Build CachyOS's Handheld Edition kernel, linux-cachyos-deckify 7.2.11-1, with Intel's
# "Vswing / Pre-emphasis Override" fix (mainline Linux 7.4) for the white dots on the OXP3's
# screen, as a second kernel (linux-cachyos-deckify-vsw) next to the stock one.
#
# It uses CachyOS's own build recipe and sources, pinned to the 7.2.11-1 release, adds the
# eight patches from Linus Torvalds' tree, builds for x86-64-v3 like CachyOS's published
# package, and prints the install command. The kernel is not installed until you run that
# command yourself. Before that, the script installs base-devel and git, makepkg adds the build
# tools (rust, rust-src, rust-bindgen with clang, pahole, ...; about 1 GB, and they stay
# installed), and gpg adds CachyOS's two signing keys to ~/.gnupg.
#
# Run it as your normal user (not with sudo), plugged in, from Desktop Mode, in Konsole:
#   curl -fsSLO https://raw.githubusercontent.com/Featherwolf/OXP3-Tool-for-Bazzite/HEAD/tools/cachyos-vswing-kernel.sh
#   bash cachyos-vswing-kernel.sh
#   WORK=/path/on/disk bash cachyos-vswing-kernel.sh   # build somewhere else (default: ~/kbuild)
#   REBUILD=1 bash cachyos-vswing-kernel.sh            # build again although the packages exist
#
# Needs about 30 GB free in $WORK (ext4, btrfs, xfs or f2fs; not a FAT/exFAT microSD) and 1-2 hours.
# In the first minutes it asks for your password twice (sudo for base-devel and git, then
# makepkg's own sudo for rust, pahole, ...) and pacman asks you to confirm each install; after
# that it runs unattended. Returning to Gaming Mode or closing the terminal stops the build.
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
VERSION=7.2.11-1
WORK="${WORK:-$HOME/kbuild}"
RECIPE=linux-cachyos-vsw-recipe  # the script's own clone of CachyOS/linux-cachyos in $WORK

die() { echo "error: $*" >&2; exit 1; }
step() { echo; echo "== $*"; }

[ "$(id -u)" -ne 0 ] || die "run this as your normal user, not with sudo (makepkg refuses to run as root)"
command -v pacman >/dev/null || die "this is for CachyOS (pacman not found)"

mkdir -p "$WORK"
WORK="$(cd "$WORK" && pwd)"
case $WORK in
    *[[:space:]:]*) die "WORK ($WORK) must not contain spaces or colons: the kernel build refuses them" ;;
esac
fstype=$(findmnt -no FSTYPE -T "$WORK" 2>/dev/null || true)
case $fstype in
    ext4 | btrfs | xfs | f2fs) ;;
    *) die "$WORK is on ${fstype:-an unknown file system}; set WORK= to a folder on an ext4, btrfs, xfs or f2fs disk" ;;
esac
# A stopped or failed earlier build leaves its tree (makepkg --cleanbuild would delete it anyway).
rm -rf "$WORK/build/linux-$SUFFIX"
on_ac=no
for p in /sys/class/power_supply/*; do
    [ "$(cat "$p/type" 2>/dev/null)" = Mains ] && [ "$(cat "$p/online" 2>/dev/null)" = 1 ] && on_ac=yes
done
[ "$on_ac" = yes ] || echo "warning: no charger seen; the build runs every CPU core flat out for 1-2 hours"

step "Build tools"
sudo pacman -S --needed base-devel git

step "CachyOS's recipe for linux-cachyos-deckify $VERSION"
cd "$WORK"
[ -d "$RECIPE/.git" ] ||
    git clone --filter=blob:none --no-checkout https://github.com/CachyOS/linux-cachyos.git "$RECIPE"
git -C "$RECIPE" cat-file -e "$LC_COMMIT^{commit}" 2>/dev/null || git -C "$RECIPE" fetch origin
git -C "$RECIPE" checkout -q --force "$LC_COMMIT"
cd "$RECIPE/linux-cachyos-deckify"
if ! { grep -qx '_major=7.2' PKGBUILD && grep -qx '_minor=11' PKGBUILD && grep -qx 'pkgrel=1' PKGBUILD; }; then
    die "unexpected PKGBUILD (not $VERSION)"
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
# x86-64-v3, like the published package from the cachyos-v3 repository (the recipe's default is
# -march=native), so the two kernels differ only by the patches.
sed -i 's/_processor_opt:=}/_processor_opt:=GENERIC_V3}/' PKGBUILD
# shellcheck disable=SC2016  # the PKGBUILD's literal text
grep -qxF ': "${_processor_opt:=GENERIC_V3}"' PKGBUILD || die "could not set the CPU target to x86-64-v3"

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
    if ! gpg --list-keys "$key" >/dev/null 2>&1; then
        gpg --recv-keys "$key" ||
            { curl -fsSL --retry 3 "https://keys.openpgp.org/vks/v1/by-fingerprint/$key" | gpg --import; } || true
    fi
    gpg --list-keys "$key" >/dev/null 2>&1 || die "could not get CachyOS's signing key $key"
done

find_packages() {
    shopt -s nullglob
    kpkg=("$WORK/pkg/linux-$SUFFIX-$VERSION-"*.pkg.tar.zst)
    hpkg=("$WORK/pkg/linux-$SUFFIX-headers-$VERSION-"*.pkg.tar.zst)
    shopt -u nullglob
}
mkdir -p "$WORK/pkg" "$WORK/logs"
# The edited PKGBUILD pins every source by checksum, the patch series by its commits: packages built from
# another recipe are built again. (The patches' own bytes change with kernel.org's cgit version.)
stamp="$WORK/pkg/.linux-$SUFFIX.recipe"
recipe="$({ grep -v '^b2sums+=' PKGBUILD; printf '%s\n' "${VSWING[@]}"; } | b2sum | cut -d' ' -f1)"
find_packages
if [ -z "${REBUILD:-}" ] && [ ${#kpkg[@]} -eq 1 ] && [ ${#hpkg[@]} -eq 1 ] &&
    [ "$(cat "$stamp" 2>/dev/null)" = "$recipe" ]; then
    step "Already built from this recipe (REBUILD=1 bash $0 builds again)"
else
    rm -f "${kpkg[@]}" "${hpkg[@]}" "$stamp"
    free_gb=$(df -Pk "$WORK" | awk 'NR==2 {print int($4 / 1048576)}')
    [ "$free_gb" -ge 30 ] || die "$WORK has ${free_gb} GB free; the build needs about 30 GB (set WORK= to another disk)"
    step "Building (1-2 hours; logs in $WORK/logs). Stay in Desktop Mode and keep this window open until it says Done"
    inhibit=(systemd-inhibit --what=sleep --who=cachyos-vswing-kernel --why="Building a kernel")
    "${inhibit[@]}" true 2>/dev/null || { inhibit=(); echo "note: could not keep the device awake; do not let it sleep"; }
    # makepkg stops with "A failure occurred in prepare()" if any patch, vswing-override.patch included, does
    # not apply, so a finished build always has the series.
    if ! "${inhibit[@]}" env BUILDDIR="$WORK/build" PKGDEST="$WORK/pkg" LOGDEST="$WORK/logs" PKGEXT=.pkg.tar.zst \
        makepkg -s -f --cleanbuild --log; then
        rm -rf "$WORK/build/linux-$SUFFIX"
        die "the build failed: makepkg's error is in the lines just above (the output of prepare(), build() and
package() is also in $WORK/logs, if it got that far). Nothing was installed."
    fi
    echo "$recipe" > "$stamp"
    rm -rf "$WORK/build/linux-$SUFFIX"
    rmdir "$WORK/build" 2>/dev/null || true
    find_packages
    [ ${#kpkg[@]} -eq 1 ] && [ ${#hpkg[@]} -eq 1 ] ||
        die "expected one kernel and one headers package in $WORK/pkg, found: ${kpkg[*]} ${hpkg[*]}"
fi

step "Done. Install it next to your stock kernel with:"
printf '  sudo pacman -U'; printf ' %q' "${kpkg[0]}" "${hpkg[0]}"; echo
cat <<EOF

Limine then lists linux-$SUFFIX after the stock kernel and keeps booting the stock one. Do not
pick the new kernel in Limine's menu or with bootctl: CachyOS's limine.conf has
'remember_last_entry: yes', so whatever boots once boots at every power-on, also if it hangs.
Instead, with oxp3-tool 0.3.0 or later (update it with the get.sh line from its README):
  sudo oxp3-tool kernel try linux-$SUFFIX
  systemctl reboot
If it hangs or the screen stays dark, hold the power button about 10 seconds until the device is
off, then turn it on: Limine boots the stock kernel. On the new kernel, \`uname -r\` shows
$VERSION-$SUFFIX; to keep booting it: sudo oxp3-tool kernel keep
If it did not come up and you are back on the stock kernel: sudo oxp3-tool kernel keep (on the
stock kernel) puts Limine's settings back.
To remove it later: on it, sudo oxp3-tool kernel try linux-cachyos-deckify and reboot; on the
stock kernel, sudo oxp3-tool kernel keep, then
  sudo pacman -R linux-$SUFFIX linux-$SUFFIX-headers
EOF
