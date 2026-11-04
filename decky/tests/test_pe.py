import mmap
import os
import struct
import tempfile
import unittest
from unittest import mock

from oxp3deck import pe


def fixed_info(a, b, c, d, struc=0x00010000) -> bytes:
    """A VS_FIXEDFILEINFO head: signature, dwStrucVersion, dwFileVersionMS, dwFileVersionLS (+ product version)."""
    return struct.pack("<IIIIII", 0xFEEF04BD, struc, (a << 16) | b, (c << 16) | d, 0, 0)


def fake_dll(path, version=(2, 0, 1, 41), before=b"", pad=4096):
    with open(path, "wb") as f:
        f.write(b"MZ" + b"\0" * pad + before + b"VS_VERSION_INFO\0" + fixed_info(*version) + b"\0" * 64)
    return path


class FileVersionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name

    def test_version(self):
        p = fake_dll(os.path.join(self.d, "libxess.dll"))
        self.assertEqual(pe.file_version(p), "2.0.1.41")
        p = fake_dll(os.path.join(self.d, "libxell.dll"), (1, 2, 1, 13))
        self.assertEqual(pe.file_version(p), "1.2.1.13")
        self.assertEqual(pe.version_tuple("1.2.1.13"), (1, 2, 1, 13))
        self.assertIsNone(pe.version_tuple("1.x"))
        self.assertIsNone(pe.version_tuple(None))

    def test_decoy_signature_skipped(self):
        decoy = struct.pack("<IIII", 0xFEEF04BD, 0xDEAD, 0x00090009, 0x00090009)
        p = fake_dll(os.path.join(self.d, "a.dll"), (1, 3, 1, 78), before=decoy)
        self.assertEqual(pe.file_version(p), "1.3.1.78")

    def test_only_a_decoy(self):
        p = os.path.join(self.d, "b.dll")
        with open(p, "wb") as f:
            f.write(b"MZ" + b"\0" * 100 + fixed_info(4, 5, 6, 7, struc=0x1234))
        self.assertEqual(pe.file_version(p), "4.5.6.7")       # like proton-hook.py: the first hit

    def test_none(self):
        empty = os.path.join(self.d, "empty.dll")
        open(empty, "wb").close()
        plain = os.path.join(self.d, "plain.dll")
        with open(plain, "wb") as f:
            f.write(b"MZ" + b"\0" * 10000)
        cut = os.path.join(self.d, "cut.dll")
        with open(cut, "wb") as f:
            f.write(b"\0" * 100 + b"\xbd\x04\xef\xfe\0\0")      # signature without its fields
        os.mkfifo(os.path.join(self.d, "fifo.dll"))
        for p in (empty, plain, cut, os.path.join(self.d, "missing.dll"), self.d, os.path.join(self.d, "fifo.dll"),
                  "", "\0bad"):
            self.assertIsNone(pe.file_version(p), p)

    def test_big_sparse_file(self):
        p = os.path.join(self.d, "big.dll")
        with open(p, "wb") as f:
            f.truncate(60 << 20)
            f.seek((60 << 20) - 200)
            f.write(fixed_info(2, 0, 2, 68))
        self.assertEqual(pe.file_version(p), "2.0.2.68")

    def test_chunks_across_boundaries(self):
        with mock.patch.object(mmap, "mmap", side_effect=AssertionError("mapped")):   # never mapped (SIGBUS)
            for chunk in (16, 17, 23, 64, 4096):
                for offset in range(0, 40, 3):
                    with self.subTest(chunk=chunk, offset=offset), mock.patch.object(pe, "CHUNK", chunk):
                        p = os.path.join(self.d, f"c{chunk}-{offset}.dll")
                        with open(p, "wb") as f:
                            f.write(b"\xbd" * offset + struct.pack("<IIII", 0xFEEF04BD, 7, 0x00010001, 0x00010001)
                                    + b"x" * offset + fixed_info(1, 3, 2, 10) + b"\0" * 5)
                        self.assertEqual(pe.file_version(p), "1.3.2.10")

    def test_a_file_full_of_signatures_stops_at_max_hits(self):
        """RS-2: MAX_HITS bounds the hits of one chunk too (a 4 MB chunk of signatures held ~1M of them)."""
        p = os.path.join(self.d, "full.dll")
        with open(p, "wb") as f:
            f.write(fixed_info(9, 8, 7, 6, struc=7)[:16] * (1 << 16) + fixed_info(1, 2, 3, 4))   # 1 MB, then a good one
        fd = os.open(p, os.O_RDONLY)
        try:
            self.assertEqual(len(pe._scan_chunks(fd)), pe.MAX_HITS)
        finally:
            os.close(fd)
        self.assertEqual(pe.file_version(p), "9.8.7.6")     # the first hit, as before (proton-hook.py's rule)

    def test_read_error_is_no_version(self):
        """An unreadable sector (EIO) answers None; a mapped file would have killed the backend with SIGBUS."""
        p = fake_dll(os.path.join(self.d, "libxess.dll"))
        real = os.read

        def failing(fd, n):
            if n == pe.CHUNK:
                raise OSError(5, "Input/output error")
            return real(fd, n)

        with mock.patch.object(os, "read", failing):
            self.assertIsNone(pe.file_version(p))
        self.assertEqual(pe.file_version(p), "2.0.1.41")


if __name__ == "__main__":
    unittest.main()
