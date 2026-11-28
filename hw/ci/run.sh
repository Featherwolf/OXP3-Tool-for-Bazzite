#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Featherwolf
# SPDX-License-Identifier: MIT
#
# oxp3-hw CI: everything that can be checked without the device. Needs cargo, dbus-daemon,
# python3 (3.11+), and for the packaging checks systemd-analyze, systemd-tmpfiles and (optional)
# udevadm, makepkg, shellcheck. Nothing here needs root; a root run additionally runs the bus tests
# under several uids (setpriv) and applies grants.conf to a scratch root with systemd-tmpfiles.
#
#   hw/ci/run.sh            all checks
#   hw/ci/run.sh packaging  the packaging checks only (needs a release build)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here"
what="${1:-all}"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

step() { printf '\n== %s\n' "$*"; }
skip() { printf 'skipped: %s\n' "$*"; }

if [ "$what" = all ]; then
  step "rustfmt, clippy (release and test configurations)"
  cargo fmt --check
  cargo clippy --locked --all-targets -- -D warnings
  cargo clippy --locked --all-targets --features test-sysfs -- -D warnings

  step "unit tests, ported fake-sysfs tests, private-bus tests"
  cargo test --locked --features test-sysfs
fi

step "release build (without the test feature)"
cargo build --locked --release --bin oxp3-hwd
bin="$here/target/release/oxp3-hwd"
# exit 2 (unknown option) exactly: any other status means the option was parsed (as root a parsed
# option would end in the root refusal, exit 1, which a plain `if` would miss)
for opt in --sysfs-root --bus --time-scale --no-emulation; do
  st=0
  "$bin" "$opt" x >/dev/null 2>&1 || st=$?
  if [ "$st" != 2 ]; then
    echo "the release build accepts $opt (exit $st)" >&2
    exit 1
  fi
done
"$bin" --version

step "grant lint (and its self-test)"
python3 ci/lint_grants.py --self-test

step "steamos-manager files against steamos-manager 26.4.1's types"
python3 ci/check_steamos_manager.py packaging

step "PKGBUILD: syntax and lint"
bash -n packaging/PKGBUILD
if command -v shellcheck >/dev/null; then
  # PKGBUILD variables are read by makepkg; srcdir and pkgdir are makepkg's
  shellcheck -s bash -e SC2034,SC2154,SC2164 packaging/PKGBUILD ci/run.sh
else
  skip "shellcheck not installed"
fi

step "PKGBUILD: package() into a scratch pkgdir"
if command -v makepkg >/dev/null; then
  (cd packaging && makepkg --verifysource --nodeps) || echo "makepkg --verifysource failed (no network or no hw-v tag yet)"
fi
# without makepkg: run the PKGBUILD's package() as makepkg would, against this checkout
mkdir -p "$scratch/src" "$scratch/pkg"
ln -s "$(cd .. && pwd)" "$scratch/src/oxp3-hw"
(
  set -euo pipefail
  # shellcheck source=/dev/null
  source packaging/PKGBUILD
  srcdir="$scratch/src" pkgdir="$scratch/pkg"
  export srcdir pkgdir
  package
)
pkg="$scratch/pkg"
expect=(
  "755 usr/lib/oxp3-hw/oxp3-hwd"
  "644 usr/lib/oxp3-hw/grants.conf"
  "644 usr/lib/systemd/system/oxp3-hw.service"
  "644 usr/lib/sysusers.d/oxp3-hw.conf"
  "644 usr/lib/udev/rules.d/70-oxp3-hw.rules"
  "644 usr/share/dbus-1/system.d/io.github.featherwolf.Bathysphere.Hw1.conf"
  "644 usr/share/dbus-1/system-services/io.github.featherwolf.Bathysphere.Hw1.service"
  "644 usr/share/dbus-1/interfaces/io.github.featherwolf.Bathysphere.Hw1.xml"
  "644 usr/share/steamos-manager/devices/onexplayer-3.toml"
  "644 usr/share/steamos-manager/remotes.d/50-oxp3-hw.toml"
  "644 usr/share/doc/oxp3-hw/limits.toml.example"
  "644 usr/share/doc/oxp3-hw/README.md"
  "644 usr/share/licenses/oxp3-hw/LICENSE"
)
for e in "${expect[@]}"; do
  mode="${e%% *}" path="${e#* }"
  got="$(stat -c %a "$pkg/$path")"
  [ "$got" = "$mode" ] || { echo "$path: mode $got, want $mode" >&2; exit 1; }
done
[ "$(readlink "$pkg/usr/lib/systemd/system/multi-user.target.wants/oxp3-hw.service")" = ../oxp3-hw.service ]
files="$(cd "$pkg" && find . -type f -o -type l | sort | wc -l)"
[ "$files" -eq $((${#expect[@]} + 1)) ] || { (cd "$pkg" && find . -type f -o -type l | sort); echo "unexpected files in the package" >&2; exit 1; }
if [ -n "$(find "$pkg" -perm /6000)" ]; then
  echo "setuid or setgid file in the package" >&2
  exit 1
fi
echo "package layout: ${#expect[@]} files and the static enablement link, no setuid or setgid file"

step "systemd-analyze verify and security (offline)"
if command -v systemd-analyze >/dev/null; then
  # against this host's systemd, with the daemon's path pointed at the release build
  u="$scratch/unit/oxp3-hw.service"
  mkdir -p "$scratch/unit"
  sed "s|/usr/lib/oxp3-hw/oxp3-hwd|$bin|g" "$pkg/usr/lib/systemd/system/oxp3-hw.service" >"$u"
  systemd-analyze verify --man=no "$u"
  systemd-analyze security --offline=true --threshold=20 "$u" | tail -n 2
else
  skip "systemd-analyze not installed"
fi

step "udevadm verify"
# UDEVADM may point at an unpacked udevadm (for example from `apt-get download udev`)
udevadm="${UDEVADM:-$(command -v udevadm || true)}"
if [ -n "$udevadm" ] && "$udevadm" verify --help >/dev/null 2>&1; then
  "$udevadm" verify --resolve-names=never packaging/70-oxp3-hw.rules
else
  skip "udevadm verify not available (systemd >= 254 with udev installed)"
fi

step "D-Bus policy and activation file, loaded by dbus-daemon"
if command -v dbus-daemon >/dev/null; then
  sd="$scratch/services"
  mkdir -p "$sd"
  cp packaging/io.github.featherwolf.Bathysphere.Hw1.service "$sd/"
  cat >"$scratch/bus.conf" <<EOF
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN" "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>system</type>
  <auth>EXTERNAL</auth>
  <listen>unix:path=$scratch/bus</listen>
  <servicedir>$sd</servicedir>
  <policy context="default">
    <allow user="*"/>
    <deny own="*"/>
    <deny send_type="method_call"/>
    <allow send_type="signal"/>
    <allow send_requested_reply="true" send_type="method_return"/>
    <allow send_requested_reply="true" send_type="error"/>
    <allow receive_type="method_call"/>
    <allow receive_type="method_return"/>
    <allow receive_type="error"/>
    <allow receive_type="signal"/>
    <allow send_destination="org.freedesktop.DBus" send_interface="org.freedesktop.DBus"/>
  </policy>
  <include>$here/packaging/io.github.featherwolf.Bathysphere.Hw1.conf</include>
</busconfig>
EOF
  dbus-daemon --config-file="$scratch/bus.conf" --nofork --print-address=1 >"$scratch/addr" 2>"$scratch/bus.err" &
  bpid=$!
  for _ in $(seq 50); do [ -s "$scratch/addr" ] && break; sleep 0.1; done
  names="$(dbus-send --bus="$(head -n1 "$scratch/addr")" --print-reply --dest=org.freedesktop.DBus \
    /org/freedesktop/DBus org.freedesktop.DBus.ListActivatableNames)"
  kill "$bpid"
  grep -q '"io.github.featherwolf.Bathysphere.Hw1"' <<<"$names" || { cat "$scratch/bus.err"; exit 1; }
  echo "dbus-daemon loads the policy and lists the activatable name"
else
  skip "dbus-daemon not installed"
fi

step "grants.conf applied by systemd-tmpfiles to a scratch root"
if [ "$(id -u)" -eq 0 ] && command -v systemd-tmpfiles >/dev/null; then
  r="$scratch/root"
  mkdir -p "$r/etc"
  printf 'root:x:0:0::/root:/bin/sh\noxp3-hw:x:970:970::/:/usr/bin/nologin\n' >"$r/etc/passwd"
  printf 'root:x:0:\noxp3-hw:x:970:\n' >"$r/etc/group"
  # real sysfs shape: /sys/class/drm/card1 is a symlink into /sys/devices
  d="$r/sys/devices/pci0000:00/0000:00:02.0/drm/card1/device/tile0/gt0/freq0"
  mkdir -p "$d" "$r/sys/class/drm" "$r/sys/devices/system/cpu/cpufreq/policy0" "$r/sys/devices/system/cpu/cpufreq/policy1" \
    "$r/sys/devices/system/cpu/intel_pstate" "$r/sys/module/processor_thermal_soc_slider/parameters"
  ln -s ../../devices/pci0000:00/0000:00:02.0/drm/card1 "$r/sys/class/drm/card1"
  for f in "$d"/{min_freq,max_freq,power_profile,rp0_freq} \
    "$r"/sys/devices/system/cpu/cpufreq/policy{0,1}/{energy_performance_preference,scaling_max_freq,scaling_min_freq} \
    "$r"/sys/devices/system/cpu/intel_pstate/{max_perf_pct,min_perf_pct,no_turbo} \
    "$r"/sys/module/processor_thermal_soc_slider/parameters/{slider_balance,slider_offset}; do
    echo 0 >"$f"
    chmod 0644 "$f"
  done
  systemd-tmpfiles --root="$r" --create "$here/packaging/grants.conf"
  for f in "$d"/{min_freq,max_freq,power_profile} \
    "$r"/sys/devices/system/cpu/cpufreq/policy{0,1}/{energy_performance_preference,scaling_max_freq} \
    "$r"/sys/devices/system/cpu/intel_pstate/{max_perf_pct,min_perf_pct} \
    "$r"/sys/module/processor_thermal_soc_slider/parameters/{slider_balance,slider_offset}; do
    [ "$(stat -c '%a %g' "$f")" = "664 970" ] || { echo "not granted: $f ($(stat -c '%a %g' "$f"))" >&2; exit 1; }
  done
  for f in "$d/rp0_freq" "$r"/sys/devices/system/cpu/cpufreq/policy0/scaling_min_freq "$r"/sys/devices/system/cpu/intel_pstate/no_turbo; do
    [ "$(stat -c '%a %g' "$f")" = "644 0" ] || { echo "granted by mistake: $f" >&2; exit 1; }
  done
  echo "every grant line matched exactly its attributes (also through the /sys/class/drm symlink), nothing else"
else
  skip "needs root to chgrp (systemd-tmpfiles --root)"
fi

step "done"
