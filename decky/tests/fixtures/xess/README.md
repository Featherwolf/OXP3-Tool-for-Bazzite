# Test fixtures for the xess-xmx-linux code

- `20261031/tools/`, `20261104/tools/`: `get-igc.sh`, `get-xess.sh` (and `get-ffx.sh`) unchanged from the
  xess-xmx-linux releases 20261031 and 20261104 (github.com/HHHHanasak1/xess-xmx-linux, MIT License, Copyright (c)
  2026 HHHHanasak1). The tests only compare their bytes (an update copies `xess/` when `get-xess.sh` is unchanged,
  `igc/` when `get-igc.sh` is) and never run them.
- `config.vdf`: a Steam `config/config.vdf` with a `CompatToolMapping`.
