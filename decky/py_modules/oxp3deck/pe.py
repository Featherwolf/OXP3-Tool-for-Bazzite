"""The file version of a Windows DLL (a game's libxess.dll), read from its VS_FIXEDFILEINFO.

Same rule as xess-xmx-linux's tools/proton-hook.py version(), which decides what XESS=latest swaps: the signature
0xFEEF04BD, then dwStrucVersion, dwFileVersionMS and dwFileVersionLS. A hit whose dwStrucVersion is 0x00010000 is
preferred, so a stray copy of the four bytes earlier in the file is skipped. Game DLLs reach 50 MB and more, so the
file is read in 4 MB chunks, never loaded whole. It is not mapped: a page that cannot be read (a worn microSD card,
a card pulled out, a file that shrinks) would kill the whole root backend with SIGBUS, where read() gives OSError.
"""
from __future__ import annotations

import os
import stat

SIGNATURE = b"\xbd\x04\xef\xfe"
STRUC_VERSION = 0x00010000
CHUNK = 4 << 20
MAX_HITS = 64      # a file full of the signature bytes is not worth scanning to the end


def _fields(buf, i: int) -> tuple[int, int, int] | None:
    if i + 16 > len(buf):
        return None
    sv, ms, ls = (int.from_bytes(buf[i + o:i + o + 4], "little") for o in (4, 8, 12))
    return sv, ms, ls


def _pick(hits: list[tuple[int, int, int]]) -> str | None:
    if not hits:
        return None
    good = [h for h in hits if h[0] == STRUC_VERSION]
    _sv, ms, ls = (good or hits)[0]
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def _scan_chunks(fd: int) -> list[tuple[int, int, int]]:
    hits = []
    tail = b""
    while len(hits) < MAX_HITS:
        chunk = os.read(fd, CHUNK)
        if not chunk:
            break
        buf = tail + chunk
        i = buf.find(SIGNATURE)
        last = -1
        while i >= 0:
            f = _fields(buf, i)
            if f is None:
                break        # the fields continue in the next chunk: keep this hit in the tail
            hits.append(f)
            if f[0] == STRUC_VERSION or len(hits) >= MAX_HITS:   # one chunk can hold a million copies
                return hits
            last = i
            i = buf.find(SIGNATURE, i + 1)
        keep_from = max(len(buf) - 15, last + 1, 0) if i < 0 else i
        tail = buf[keep_from:]
    return hits


def file_version(path: str) -> str | None:
    """'a.b.c.d', or None when the file is missing, not a regular file or has no version resource. Never raises."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except (OSError, ValueError):
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size < 16:
            return None
        return _pick(_scan_chunks(fd))
    except (OSError, ValueError, OverflowError):
        return None
    finally:
        os.close(fd)


def version_tuple(v: str | None) -> tuple[int, ...] | None:
    """'2.0.1.41' -> (2, 0, 1, 41); None for anything else."""
    if not v:
        return None
    try:
        return tuple(int(p) for p in v.split("."))
    except ValueError:
        return None
