# Third-party notices

oxp3-tool's own code is under the MIT License, in [LICENSE](LICENSE). It contains the third-party material listed here, which keeps its own licence. Each section says where the material comes from, where the tool uses it, and which licence covers it, with that licence's full text. This file is also packed into the single-file build `dist/oxp3-tool`; on an installed copy, `unzip -p /usr/local/bin/oxp3-tool THIRD_PARTY_NOTICES.md` shows it.

Projects whose findings, documentation or behaviour the tool builds on, without copying their code, are credited in the README under [Background and credits](README.md#background-and-credits).

## gamescope

- Project: [gamescope](https://github.com/ValveSoftware/gamescope)
- Source: `scripts/00-gamescope/displays/lenovo.legiongo2.oled.lua`, as of master commit `2b5cf191` (2026-10-13). The file is the same in the gamescope 3.16.31-ogc4 (Bazzite) and 3.17.0 (CachyOS) packages. It has no header of its own; gamescope's `LICENSE` covers it.
- Used in: `oxp3_tool/templates.py`. `LEGIONGO2_VFPS` is that file's front-porch table for 48–144 Hz (97 values), unchanged; the Legion Go 2 has the same panel as the OXP3. The display profile that `oled_lua()` writes (the `oled-profile` fix) follows the same file: the list of refresh rates, `dynamic_modegen` with `adjust_front_porch` and `calc_vrefresh`, the `SDC` / `0x4301` match, and its log messages.
- Licence: BSD 2-Clause License. The text below is lines 1–26 of gamescope's `LICENSE`; the rest of that file covers other parts of gamescope.

```text
BSD 2-Clause License

Copyright (c) 2013-2022, Valve Corporation
Copyright (c) 2022, NVIDIA CORPORATION
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## onexplayer3-steamos-setup (ONEXPLAYER 3 SteamOS fix pack)

- Project: [onexplayer3-steamos-setup](https://github.com/HHHHanasak1/onexplayer3-steamos-setup) by HHHHanasak1 (HANA)
- Source: the gamescope profile in `oxp3-apply-fixes.sh` (`LUA_HDR_CONTENT`, lines 173–253), fix pack 20261015, commit `8e4d2fb3`.
- Used in: `oxp3_tool/templates.py`. Parts of the profile that `oled_lua()` writes are adapted from it: the gamma 2.2 `hdr` block and its luminance values, the front-porch table keyed by refresh rate and read with `vfps[refresh]`, and the "Couldn't do refresh" warning. `panel_vfp()` uses its formula (vtotal = clock / (htotal × refresh)) for 30–47 Hz.
- Licence: MIT License, from the project's `LICENSE`:

```text
MIT License

Copyright (c) 2026 HANA & Claude

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Limine

- Project: [Limine](https://github.com/Limine-Bootloader/Limine)
- Source: `common/menu.c` and `common/lib/config.c` of Limine 12.9, checked against v12.9.6 (commit `89a9512c`).
- Used in: `oxp3_tool/limine_menu.py`, which reads `limine.conf` and Limine's UEFI variables the way Limine does, so that `oxp3-tool kernel try` and `keep` know what Limine will boot. No C code is copied. A few short routines, such as the boot entry id that `bli_entry_base_id` builds, are written again in Python to give the same results; Limine's notice is included here for them.
- Licence: BSD 2-Clause License, from Limine's `COPYING` (the same text in v12.9.6 and in CachyOS's limine 12.9.4-1 package):

```text
Copyright (C) 2019-2026 Mintsuki, 2026 Kamila Szewczyk, and contributors.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## InputPlumber (test data only)

- Project: [InputPlumber](https://github.com/ShadowBlip/InputPlumber), GPL-3.0-or-later ([LICENSE](https://github.com/ShadowBlip/InputPlumber/blob/main/LICENSE)). Its ONEXPLAYER 3 support is by Andrei Aldea (Grippy98).
- Used in: `tests/fixtures/inputplumber/` (read by `tests/fakeroot.py`). `onexplayer_type9.yaml` is InputPlumber v0.82.0's `rootfs/usr/share/inputplumber/capability_maps/onexplayer_type9.yaml` without its comments, and `50-onexplayer_3.yaml` is a shortened `rootfs/usr/share/inputplumber/devices/50-onexplayer_3.yaml`. The folder keeps InputPlumber's licence; its full text is in `tests/fixtures/inputplumber/COPYING`. Both are configuration data (device names, key codes and the format's keys) that the tests need as InputPlumber ships them, because the `buttons` fix only replaces a stock map it recognises. They are not part of `dist/oxp3-tool`.
