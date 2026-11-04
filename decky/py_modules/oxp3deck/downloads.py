# Adapted from ClawDeck (MIT, Copyright (c) 2026 IHulkSmashI and ClawDeck contributors).
"""GitHub release lookup and downloads, fetched when the user taps Install or Update (nothing is redistributed).

The newest tag comes from the redirect of github.com/<repo>/releases/latest, which has no rate limit; the API is
the fallback (60 requests an hour per address, then 403). Downloads go into a folder only root can write, as a
.part file created exclusively, and are renamed into place once complete and checked.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from . import tls

log = logging.getLogger("oxp3deck")

USER_AGENT = "OXP3Deck"

_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,99}$")
_NET_ERRORS = (OSError, http.client.HTTPException, ValueError)   # URLError and ssl errors are OSErrors


class DownloadError(Exception):
    pass


def _request(url: str, accept: str | None = None, method: str | None = None) -> urllib.request.Request:
    if not url.startswith("https://"):
        raise DownloadError(f"{url}: only https:// URLs are fetched")
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    return urllib.request.Request(url, headers=headers, method=method)


def _reason(e: BaseException) -> str:
    if isinstance(e, urllib.error.HTTPError):
        e.close()
        if e.code in (403, 429):
            return f"HTTP {e.code} (GitHub's rate limit? try again in an hour)"
        return f"HTTP {e.code} {e.reason}"
    if isinstance(e, urllib.error.URLError):
        return str(e.reason)
    return str(e) or type(e).__name__


def _get(url: str, timeout: float, max_bytes: int, accept: str | None = None) -> str:
    req = _request(url, accept)
    try:
        with tls.urlopen(req, timeout) as r:
            data = r.read(max_bytes + 1)
    except _NET_ERRORS as e:
        raise DownloadError(f"{url}: {_reason(e)}") from e
    if len(data) > max_bytes:
        raise DownloadError(f"{url}: more than {max_bytes} bytes")
    return data.decode("utf-8", errors="replace")


def get_text(url: str, timeout: float = 30, max_bytes: int = 2_000_000) -> str:
    """The body of an https URL as text. Raises DownloadError."""
    return _get(url, timeout, max_bytes)


def _tag_in(url: str) -> str | None:
    m = re.search(r"/releases/tag/([^/?#]+)/?$", urllib.parse.urlsplit(url).path)
    tag = urllib.parse.unquote(m.group(1)) if m else None
    return tag if tag and _TAG.match(tag) else None


def latest_release_tag(repo: str) -> str:
    """The tag of the newest release of owner/name. Raises DownloadError."""
    if not _REPO.match(repo) or ".." in repo:
        raise DownloadError(f"latest release: bad repository name {repo!r}")
    web = f"https://github.com/{repo}/releases/latest"
    try:
        with tls.urlopen(_request(web, method="HEAD"), 30) as r:
            final = r.geturl()
        tag = _tag_in(final)
        if tag:
            return tag
        why = f"{web} led to {final}, not to a release"
    except _NET_ERRORS as e:
        why = f"{web}: {_reason(e)}"
    log.info("latest release of %s: %s; asking the API", repo, why)
    api = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        data = json.loads(_get(api, 30, 2_000_000, "application/vnd.github+json"))
    except DownloadError as e:
        raise DownloadError(f"latest release of {repo}: {why}; {e}") from e
    except ValueError as e:
        raise DownloadError(f"latest release of {repo}: {why}; {api}: not JSON") from e
    tag = data.get("tag_name") if isinstance(data, dict) else None
    if isinstance(tag, str) and _TAG.match(tag):
        return tag
    raise DownloadError(f"latest release of {repo}: {why}; {api} named no usable tag")


def _check_private(folder: str) -> None:
    try:
        st = os.lstat(folder)
    except OSError as e:
        raise DownloadError(f"download: {folder}: {e.strerror}") from e
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o022:
        raise DownloadError(f"download: {folder} must be a folder only the backend can write (a mkdtemp)")


def download(url: str, dest: str, *, expected_sha256: str | None = None, max_bytes: int,
             on_progress=None) -> str:
    """Download url to dest and return its sha256 (hex). dest's folder must be one only the backend can write.
    on_progress(done_bytes, total_bytes_or_None) is called about four times a second and once at the end.
    Raises DownloadError; nothing is left at dest or dest.part on failure."""
    name = os.path.basename(urllib.parse.urlsplit(url).path) or url
    dest = os.path.abspath(dest)
    _check_private(os.path.dirname(dest))
    req = _request(url, "application/octet-stream")
    part = dest + ".part"
    progress = [on_progress]

    def report(done: int, total: int | None) -> None:
        if progress[0] is not None:
            try:
                progress[0](done, total)
            except Exception:
                log.exception("download %s: on_progress failed", name)
                progress[0] = None

    try:
        try:
            os.unlink(part)
        except FileNotFoundError:
            pass
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    except OSError as e:
        raise DownloadError(f"download {name}: {part}: {e.strerror}") from e
    try:
        h = hashlib.sha256()
        done = 0
        total = None
        with os.fdopen(fd, "wb") as f:
            try:
                with tls.urlopen(req, 60) as r:
                    length = r.headers.get("Content-Length")
                    total = int(length) if length and length.isdigit() else None
                    if total is not None and total > max_bytes:
                        raise DownloadError(f"download {name}: {total} bytes, more than the {max_bytes} allowed")
                    last = time.monotonic()
                    while True:
                        chunk = r.read(1 << 16)
                        if not chunk:
                            break
                        done += len(chunk)
                        if done > max_bytes:
                            raise DownloadError(f"download {name}: more than the {max_bytes} bytes allowed")
                        h.update(chunk)
                        f.write(chunk)
                        if time.monotonic() - last >= 0.25:
                            last = time.monotonic()
                            report(done, total)
            except _NET_ERRORS as e:
                raise DownloadError(f"download {name}: {_reason(e)}") from e
        if total is not None and done != total:
            raise DownloadError(f"download {name}: cut off after {done} of {total} bytes")
        digest = h.hexdigest()
        if expected_sha256 and digest != expected_sha256.strip().lower():
            raise DownloadError(f"download {name}: sha256 is {digest}, expected {expected_sha256.strip().lower()}")
        report(done, total)
        try:
            os.replace(part, dest)
        except OSError as e:
            raise DownloadError(f"download {name}: {dest}: {e.strerror}") from e
        return digest
    except BaseException:
        try:
            os.unlink(part)
        except OSError:
            pass
        raise


def safe_zip_members(zip_path: str) -> list[str]:
    """The member names of a zip whose members all sit in one top folder, as plain files and folders. Raises
    DownloadError on absolute names, '..', backslashes, symlinks or other special entries, or more than one top
    entry, before anything is unpacked."""
    base = os.path.basename(zip_path)
    try:
        with zipfile.ZipFile(zip_path) as z:
            infos = z.infolist()
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, ValueError) as e:
        raise DownloadError(f"{base}: not a readable zip: {e}") from e
    names = []
    tops = set()
    for i in infos:
        n = i.filename

        def bad(why: str, n=n) -> DownloadError:
            return DownloadError(f"{base}: unsafe entry {n!r}: {why}")

        if "\\" in n:
            raise bad("backslash in the name")
        if n.startswith("/"):
            raise bad("absolute path")
        parts = n.rstrip("/").split("/")
        if any(p in ("", ".", "..") for p in parts):
            raise bad("'..' or an empty part in the path")
        kind = stat.S_IFMT(i.external_attr >> 16)
        if kind == stat.S_IFLNK:
            raise bad("symlink")
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise bad("not a plain file or folder")
        if len(parts) == 1 and not i.is_dir():
            raise bad("a file outside the top folder")
        tops.add(parts[0])
        names.append(n)
    if not names:
        raise DownloadError(f"{base}: empty zip")
    if len(tops) > 1:
        raise DownloadError(f"{base}: more than one top folder: {', '.join(sorted(tops)[:5])}")
    return names
