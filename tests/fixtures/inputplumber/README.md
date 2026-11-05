# InputPlumber test data

These two files come from [InputPlumber](https://github.com/ShadowBlip/InputPlumber) v0.82.0 by ShadowBlip and
its contributors (ONEXPLAYER 3 support by Andrei Aldea, commit `d9718db`, merged in PR #672). The tests install
them in a fake root as the stock files the `buttons` fix recognises:

- `onexplayer_type9.yaml`: `rootfs/usr/share/inputplumber/capability_maps/onexplayer_type9.yaml`, without its
  comments. Keep it as InputPlumber ships it.
- `50-onexplayer_3.yaml`: `rootfs/usr/share/inputplumber/devices/50-onexplayer_3.yaml`, shortened.

Unlike the rest of this repository (MIT, see [LICENSE](../../../LICENSE)), they are under InputPlumber's licence,
the GNU General Public License v3.0 or later. Its text is in [COPYING](COPYING), copied from InputPlumber's
`LICENSE`. They are only used by the tests and are not part of `dist/oxp3-tool`.
