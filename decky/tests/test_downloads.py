import hashlib
import io
import json
import os
import shutil
import ssl
import stat
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from unittest import mock

from oxp3deck import downloads, tls
from oxp3deck.downloads import DownloadError

REPO = "HHHHanasak1/xess-xmx-linux"


class FakeResponse:
    def __init__(self, body=b"", url="", length=True, fail_after=None):
        self.body = io.BytesIO(body)
        self.url = url
        self.headers = {"Content-Length": str(len(body))} if length is True else (
            {"Content-Length": str(length)} if length else {})
        self.fail_after = fail_after
        self.sent = 0

    def read(self, n=-1):
        if self.fail_after is not None and self.sent >= self.fail_after:
            raise ConnectionResetError("connection reset by peer")
        chunk = self.body.read(n)
        self.sent += len(chunk)
        return chunk

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(url, code):
    return urllib.error.HTTPError(url, code, "Forbidden" if code == 403 else "Not Found", {}, io.BytesIO(b""))


class FakeNet:
    """tls.urlopen stand-in: url -> FakeResponse, or an exception to raise. Records the requests."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, req, timeout=30):
        self.requests.append(req)
        what = self.routes.get(req.full_url)
        if what is None:
            raise http_error(req.full_url, 404)
        if isinstance(what, BaseException):
            raise what
        return what() if callable(what) else what


class NetTestCase(unittest.TestCase):
    def net(self, routes):
        fake = FakeNet(routes)
        p = mock.patch.object(tls, "urlopen", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake


class GetTextTest(NetTestCase):
    def test_ok_and_user_agent(self):
        net = self.net({"https://x.test/a": FakeResponse("héllo".encode())})
        self.assertEqual(downloads.get_text("https://x.test/a"), "héllo")
        self.assertEqual(net.requests[0].get_header("User-agent"), "OXP3Deck")

    def test_errors(self):
        self.net({"https://x.test/big": FakeResponse(b"x" * 101),
                  "https://x.test/down": urllib.error.URLError("Name or service not known")})
        with self.assertRaisesRegex(DownloadError, "HTTP 404"):
            downloads.get_text("https://x.test/missing")
        with self.assertRaisesRegex(DownloadError, "more than 100 bytes"):
            downloads.get_text("https://x.test/big", max_bytes=100)
        with self.assertRaisesRegex(DownloadError, "Name or service"):
            downloads.get_text("https://x.test/down")
        with self.assertRaisesRegex(DownloadError, "only https"):
            downloads.get_text("http://x.test/a")


class LatestReleaseTest(NetTestCase):
    WEB = f"https://github.com/{REPO}/releases/latest"
    API = f"https://api.github.com/repos/{REPO}/releases/latest"

    def test_from_the_redirect(self):
        net = self.net({self.WEB: FakeResponse(url=f"https://github.com/{REPO}/releases/tag/20261104")})
        self.assertEqual(downloads.latest_release_tag(REPO), "20261104")
        self.assertEqual(net.requests[0].get_method(), "HEAD")
        self.assertEqual(len(net.requests), 1)

    def test_api_fallback(self):
        api = FakeResponse(json.dumps({"tag_name": "20261102-2"}).encode())
        net = self.net({self.WEB: FakeResponse(url=f"https://github.com/{REPO}/releases"), self.API: api})
        with self.assertLogs("oxp3deck", "INFO"):
            self.assertEqual(downloads.latest_release_tag(REPO), "20261102-2")
        self.assertEqual(net.requests[1].get_header("Accept"), "application/vnd.github+json")

    def test_both_fail(self):
        self.net({self.WEB: urllib.error.URLError("timed out"), self.API: http_error(self.API, 403)})
        with self.assertLogs("oxp3deck", "INFO"), self.assertRaises(DownloadError) as cm:
            downloads.latest_release_tag(REPO)
        self.assertIn("timed out", str(cm.exception))
        self.assertIn("403", str(cm.exception))
        self.assertIn("rate limit", str(cm.exception))

    def test_bad_names_and_tags(self):
        self.net({})
        for repo in ("x", "a/b/c", "../x", "a/..", "a b/c"):
            with self.assertRaises(DownloadError, msg=repo):
                downloads.latest_release_tag(repo)
        self.net({self.WEB: FakeResponse(url=f"https://github.com/{REPO}/releases/tag/..%2F..%2Fetc"),
                  self.API: FakeResponse(json.dumps({"tag_name": "../etc"}).encode())})
        with self.assertLogs("oxp3deck", "INFO"), self.assertRaisesRegex(DownloadError, "no usable tag"):
            downloads.latest_release_tag(REPO)

    def test_encoded_tag(self):
        self.net({self.WEB: FakeResponse(url=f"https://github.com/{REPO}/releases/tag/v1.3%2B2")})
        self.assertEqual(downloads.latest_release_tag(REPO), "v1.3+2")


class DownloadTest(NetTestCase):
    URL = f"https://github.com/{REPO}/releases/download/20261104/xess-xmx-linux-20261104.zip"
    BODY = os.urandom(300_000)

    def setUp(self):
        self.dir = tempfile.mkdtemp()       # 0700, ours: what the callers make
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.dest = os.path.join(self.dir, "x.zip")

    def assertNothingLeft(self):
        self.assertEqual([n for n in os.listdir(self.dir) if n.startswith("x.zip")], [])

    def test_ok(self):
        net = self.net({self.URL: lambda: FakeResponse(self.BODY)})
        progress = []
        sha = hashlib.sha256(self.BODY).hexdigest()
        got = downloads.download(self.URL, self.dest, expected_sha256=sha.upper(), max_bytes=1 << 20,
                                 on_progress=lambda d, t: progress.append((d, t)))
        self.assertEqual(got, sha)
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), self.BODY)
        self.assertFalse(os.path.exists(self.dest + ".part"))
        self.assertEqual(progress[-1], (len(self.BODY), len(self.BODY)))
        self.assertEqual(net.requests[0].get_header("Accept"), "application/octet-stream")

    def test_sha_mismatch(self):
        self.net({self.URL: lambda: FakeResponse(self.BODY)})
        with self.assertRaisesRegex(DownloadError, "sha256"):
            downloads.download(self.URL, self.dest, expected_sha256="0" * 64, max_bytes=1 << 20)
        self.assertNothingLeft()

    def test_stale_part_and_planted_link(self):
        outside = os.path.join(self.dir, "victim")
        with open(outside, "wb") as f:
            f.write(b"keep")
        os.symlink(outside, self.dest + ".part")
        self.net({self.URL: lambda: FakeResponse(self.BODY)})
        downloads.download(self.URL, self.dest, max_bytes=1 << 20)
        with open(outside, "rb") as f:
            self.assertEqual(f.read(), b"keep")
        self.assertFalse(os.path.lexists(self.dest + ".part"))

    def test_too_big(self):
        self.net({self.URL: lambda: FakeResponse(self.BODY)})
        with self.assertRaisesRegex(DownloadError, "more than"):
            downloads.download(self.URL, self.dest, max_bytes=1000)
        self.assertNothingLeft()
        self.net({self.URL: lambda: FakeResponse(self.BODY, length=None)})    # no Content-Length
        with self.assertRaisesRegex(DownloadError, "more than"):
            downloads.download(self.URL, self.dest, max_bytes=1000)
        self.assertNothingLeft()

    def test_cut_off_and_reset(self):
        self.net({self.URL: lambda: FakeResponse(self.BODY, length=len(self.BODY) + 10)})
        with self.assertRaisesRegex(DownloadError, "cut off"):
            downloads.download(self.URL, self.dest, max_bytes=1 << 20)
        self.assertNothingLeft()
        self.net({self.URL: lambda: FakeResponse(self.BODY, fail_after=65536)})
        with self.assertRaisesRegex(DownloadError, "reset"):
            downloads.download(self.URL, self.dest, max_bytes=1 << 20)
        self.assertNothingLeft()

    def test_http_error_and_scheme(self):
        self.net({})
        with self.assertRaisesRegex(DownloadError, "404"):
            downloads.download(self.URL, self.dest, max_bytes=1 << 20)
        self.assertNothingLeft()
        with self.assertRaisesRegex(DownloadError, "only https"):
            downloads.download("http://example.test/x.zip", self.dest, max_bytes=1 << 20)

    def test_folder_must_be_private(self):
        self.net({self.URL: lambda: FakeResponse(self.BODY)})
        os.chmod(self.dir, 0o777)
        with self.assertRaisesRegex(DownloadError, "only the backend"):
            downloads.download(self.URL, self.dest, max_bytes=1 << 20)
        os.chmod(self.dir, 0o700)
        link = self.dir + "-link"
        os.symlink(self.dir, link)
        self.addCleanup(os.unlink, link)
        with self.assertRaisesRegex(DownloadError, "only the backend"):
            downloads.download(self.URL, os.path.join(link, "x.zip"), max_bytes=1 << 20)
        with self.assertRaises(DownloadError):
            downloads.download(self.URL, os.path.join(self.dir, "missing", "x.zip"), max_bytes=1 << 20)
        if os.geteuid() == 0:
            os.chown(self.dir, 4242, 4242)
            with self.assertRaisesRegex(DownloadError, "only the backend"):
                downloads.download(self.URL, self.dest, max_bytes=1 << 20)

    def test_failing_progress_callback(self):
        self.net({self.URL: lambda: FakeResponse(self.BODY)})

        def boom(done, total):
            raise RuntimeError("ui gone")

        with self.assertLogs("oxp3deck", "ERROR"):
            downloads.download(self.URL, self.dest, max_bytes=1 << 20, on_progress=boom)
        self.assertTrue(os.path.isfile(self.dest))


class SafeZipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def make(self, entries):
        """entries: names (files), names ending in '/' (folders) or (name, mode) for raw unix modes."""
        p = os.path.join(self.tmp.name, f"z{len(os.listdir(self.tmp.name))}.zip")
        with zipfile.ZipFile(p, "w") as z:
            for e in entries:
                name, mode = e if isinstance(e, tuple) else (e, None)
                info = zipfile.ZipInfo(name)
                if mode is not None:
                    info.external_attr = mode << 16
                elif name.endswith("/"):
                    info.external_attr = (stat.S_IFDIR | 0o755) << 16
                else:
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                z.writestr(info, b"" if name.endswith("/") else b"data")
        return p

    def test_release_layout(self):
        top = "xess-xmx-linux-20261104/"
        names = [top, top + "install.sh", top + "lib/", top + "lib/mesa-26.3.0-rc3/libvulkan_intel.so",
                 top + "lib/active", top + "tools/session-check.sh"]
        self.assertEqual(downloads.safe_zip_members(self.make(names)), names)
        self.assertEqual(downloads.safe_zip_members(self.make([top + "a", top + "b"])), [top + "a", top + "b"])
        self.assertEqual(downloads.safe_zip_members(self.make([(top + "x", 0)])), [top + "x"])   # made on Windows

    def test_unsafe(self):
        top = "pkg/"
        cases = {
            "absolute": [top + "a", "/etc/cron.d/x"],
            "dotdot": [top + "../../.bashrc"],
            "dotdot-end": [top + "a/.."],
            "backslash": [top + "a\\..\\..\\x"],
            "empty part": [top + "a//b"],
            "dot": [top + "./a"],
            "symlink": [top + "a", (top + "lib/link", stat.S_IFLNK | 0o777)],
            "device": [(top + "dev", stat.S_IFCHR | 0o666)],
            "fifo": [(top + "p", stat.S_IFIFO | 0o644)],
            "two tops": [top + "a", "other/b"],
            "top file": [top + "a", "README"],
            "only a file": ["install.sh"],
        }
        for why, entries in cases.items():
            with self.assertRaises(DownloadError, msg=why):
                downloads.safe_zip_members(self.make(entries))

    def test_not_a_zip(self):
        p = os.path.join(self.tmp.name, "x.zip")
        with open(p, "wb") as f:
            f.write(b"<html>rate limited</html>")
        with self.assertRaisesRegex(DownloadError, "not a readable zip"):
            downloads.safe_zip_members(p)
        with self.assertRaises(DownloadError):
            downloads.safe_zip_members(os.path.join(self.tmp.name, "missing.zip"))
        with self.assertRaisesRegex(DownloadError, "empty"):
            downloads.safe_zip_members(self.make([]))


class TlsTest(unittest.TestCase):
    def setUp(self):
        tls._ctx = None
        self.addCleanup(setattr, tls, "_ctx", None)

    def test_verifies(self):
        c = tls.context()
        self.assertIs(tls.context(), c)
        self.assertEqual(c.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(c.check_hostname)

    def test_without_certifi_uses_a_system_bundle(self):
        bundle = next((p for p in tls.CA_BUNDLES if os.path.isfile(p)), None)
        if bundle is None:
            self.skipTest("no system CA bundle here")
        with mock.patch.dict(sys.modules, {"certifi": None}), \
                mock.patch.object(tls, "CA_BUNDLES", ("/nonexistent/ca.crt", bundle)):
            self.assertEqual(tls._cafile(), bundle)
            self.assertGreater(len(tls.context().get_ca_certs()), 0)

    def test_nothing_found_still_verifies(self):
        with mock.patch.dict(sys.modules, {"certifi": None}), \
                mock.patch.object(tls, "CA_BUNDLES", ("/nonexistent/ca.crt",)):
            self.assertIsNone(tls._cafile())
            self.assertEqual(tls.context().verify_mode, ssl.CERT_REQUIRED)

    def test_urlopen_passes_the_context(self):
        with mock.patch("urllib.request.urlopen") as uo:
            tls.urlopen("https://x.test/", 7)
        uo.assert_called_once_with("https://x.test/", timeout=7, context=tls.context())


if __name__ == "__main__":
    unittest.main()
