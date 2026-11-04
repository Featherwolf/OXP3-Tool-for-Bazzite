# Adapted from ClawDeck (MIT, Copyright (c) 2026 IHulkSmashI and ClawDeck contributors).
"""One SSL context for every HTTPS request the backend makes.

Decky's bundled OpenSSL looks for certificates in /usr/lib/ssl (Ubuntu's layout, where it was built), which
Bazzite and CachyOS do not have: the default context trusts nothing and every download fails with
CERTIFICATE_VERIFY_FAILED. Decky bundles certifi, so that comes first, then the system bundles. Verification is
never turned off.
"""
from __future__ import annotations

import os
import ssl
import threading
import urllib.request

CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",   # Arch / CachyOS, Debian; Fedora links it into /etc/pki
    "/etc/pki/tls/certs/ca-bundle.crt",     # Fedora / Bazzite
    "/etc/ssl/certs/ca-bundle.crt",
    "/etc/ssl/cert.pem",
)

_ctx: ssl.SSLContext | None = None
_lock = threading.Lock()


def _cafile() -> str | None:
    try:
        import certifi
        path = certifi.where()
        if path and os.path.isfile(path):
            return path
    except Exception:   # not importable, or a broken copy found on the system path
        pass
    for path in CA_BUNDLES:
        if os.path.isfile(path):
            return path
    return None   # the default paths, still verified


def context() -> ssl.SSLContext:
    global _ctx
    with _lock:
        if _ctx is None:
            _ctx = ssl.create_default_context(cafile=_cafile())
        return _ctx


def urlopen(url_or_request, timeout: float = 30):
    """urllib.request.urlopen with the context above."""
    return urllib.request.urlopen(url_or_request, timeout=timeout, context=context())
