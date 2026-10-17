"""Run one JavaScript expression in Steam's UI over its CEF remote debugging.

Decky Loader turns on Steam's remote debugging (~/.steam/steam/.cef-enable-remote-debugging),
and Steam then serves the Chrome DevTools protocol on 127.0.0.1:8080 (it passes
--remote-allow-origins=*, and a request without an Origin header is never checked). Steam's
UI stores (window.SystemPerfStore and others) live in its SharedJSContext page. This is a
minimal client for that one exchange, standard library only: list the pages over HTTP, open a
WebSocket to the shared context, send Runtime.evaluate, read the answer. Nothing but the
expression is sent, and the answer is treated as untrusted data.

Every CdpError says whether the expression may have run (``maybe_ran``): only failures after
the request was sent can have changed anything, so only those must not be retried another way.

oxp3-tool bundles this file into its helper scripts.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import socket
import struct
import time
from typing import Any, Optional

CDP_HOST = "127.0.0.1"
CDP_PORT = 8080
CDP_TIMEOUT = 4.0
MAX_MESSAGE = 1 << 20
MAX_HEADERS = 16384
# Page titles Decky Loader looks for, in its order (decky_loader/injector.py).
SHARED_CONTEXT_TITLES = ("SharedJSContext", "Steam Shared Context presented by Valve™", "Steam", "SP")
STEAM_PAGE_URL = "https://steamloopback.host/"
_PAGE_ID_RE = re.compile(r"[0-9A-F]{32}")
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class CdpError(Exception):
    def __init__(self, message: str, maybe_ran: bool = False) -> None:
        super().__init__(message)
        self.maybe_ran = maybe_ran


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-JSON number {name}")


def _loads(data: Any) -> Any:
    return json.loads(data, parse_constant=_reject_constant)


class _Deadline:
    def __init__(self, timeout: float) -> None:
        self.end = time.monotonic() + timeout

    def left(self, maybe_ran: bool = False) -> float:
        left = self.end - time.monotonic()
        if left <= 0:
            raise CdpError("Steam did not answer in time", maybe_ran)
        return left


def list_targets(host: str, port: int, deadline: _Deadline) -> list[dict]:
    conn = http.client.HTTPConnection(host, port, timeout=deadline.left())
    try:
        conn.request("GET", "/json/list", headers={"Accept": "application/json", "Accept-Encoding": "identity"})
        res = conn.getresponse()
        if res.status != 200:
            raise CdpError(f"the page list answered {res.status}")
        data = res.read(MAX_MESSAGE + 1)
    except (OSError, http.client.HTTPException) as e:
        raise CdpError(f"Steam's remote debugging is not reachable ({type(e).__name__})") from e
    finally:
        conn.close()
    if len(data) > MAX_MESSAGE:
        raise CdpError("Steam's page list is too large")
    try:
        targets = _loads(data)
    except (ValueError, RecursionError) as e:
        raise CdpError("Steam's page list is not JSON") from e
    if not isinstance(targets, list):
        raise CdpError("Steam's page list is not a list")
    return [t for t in targets if isinstance(t, dict)]


def shared_context_path(targets: list[dict]) -> str:
    """The DevTools path of Steam's shared JS context page. The advertised host is never used."""
    for title in SHARED_CONTEXT_TITLES:
        for t in targets:
            page_id = t.get("id")
            ws = t.get("webSocketDebuggerUrl")
            if (t.get("title") != title or t.get("type") != "page" or not isinstance(page_id, str)
                    or not _PAGE_ID_RE.fullmatch(page_id) or not str(t.get("url", "")).startswith(STEAM_PAGE_URL)):
                continue
            path = f"/devtools/page/{page_id}"
            if ws is None or (isinstance(ws, str) and ws.endswith(path)):
                return path
    raise CdpError("Steam's SharedJSContext page was not found")


class WebSocket:
    """Just enough of RFC 6455 for one request and its answer."""

    def __init__(self, host: str, port: int, path: str, deadline: _Deadline) -> None:
        self.deadline = deadline
        self.sent = False  # once the request is out, a failure may follow a change
        try:
            self.sock = socket.create_connection((host, port), timeout=deadline.left())
        except OSError as e:
            raise CdpError(f"Steam's remote debugging is not reachable ({type(e).__name__})") from e
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            # No Origin header: CEF only checks the origin of requests that send one.
            self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                               "Sec-WebSocket-Version: 13\r\n\r\n").encode())
            head = b""
            while b"\r\n\r\n" not in head:
                if len(head) > MAX_HEADERS:
                    raise CdpError("the WebSocket handshake answer is too large")
                head += self._recv(4096)
            head, _, self.buf = head.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            status = lines[0].split()
            if len(status) < 2 or status[1] != "101":
                raise CdpError(f"WebSocket handshake refused: {lines[0][:80]}")
            headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
            accept = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
            if (headers.get("upgrade", "").lower() != "websocket"
                    or "upgrade" not in [t.strip().lower() for t in headers.get("connection", "").split(",")]
                    or headers.get("sec-websocket-accept") != accept):
                raise CdpError("the WebSocket handshake answer does not match")
        except OSError as e:
            self.sock.close()
            raise CdpError(f"WebSocket handshake failed ({type(e).__name__})") from e
        except BaseException:
            self.sock.close()
            raise

    def _recv(self, n: int) -> bytes:
        self.sock.settimeout(self.deadline.left(self.sent))
        try:
            data = self.sock.recv(n)
        except socket.timeout as e:
            raise CdpError("Steam did not answer in time", self.sent) from e
        except OSError as e:
            raise CdpError(f"the connection to Steam failed ({type(e).__name__})", self.sent) from e
        if not data:
            raise CdpError("Steam closed the connection", self.sent)
        return data

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            self.buf += self._recv(65536)
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 1 << 16:
            header += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        self.sock.settimeout(self.deadline.left(self.sent))
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send_text(self, text: str) -> None:
        try:
            self._send_frame(0x1, text.encode())
        except OSError as e:
            # A partly sent request cannot run: the browser needs the whole frame.
            raise CdpError(f"sending to Steam failed ({type(e).__name__})") from e
        self.sent = True

    def recv_message(self) -> str:
        parts: list[bytes] = []
        size = 0
        while True:
            b0, b1 = self._read(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            if b0 & 0x70 or b1 & 0x80:
                raise CdpError("Steam sent a WebSocket frame this client does not accept", self.sent)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            if n > MAX_MESSAGE or size + n > MAX_MESSAGE:
                raise CdpError("Steam's answer is too large", self.sent)
            payload = self._read(n)
            if opcode == 0x8:
                raise CdpError("Steam closed the connection", self.sent)
            if opcode == 0x9:
                self._send_frame(0xA, payload)  # ping: answer and keep reading
                continue
            if opcode == 0xA:
                continue
            if opcode not in (0x0, 0x1):
                raise CdpError(f"unexpected WebSocket opcode {opcode}", self.sent)
            parts.append(payload)
            size += n
            if fin:
                try:
                    return b"".join(parts).decode()
                except UnicodeDecodeError as e:
                    raise CdpError("Steam's answer is not UTF-8", self.sent) from e

    def close(self) -> None:
        try:
            self.sock.settimeout(0.2)
            self._send_frame(0x8, struct.pack("!H", 1000))
            end = time.monotonic() + 0.5
            while time.monotonic() < end and self.sock.recv(4096):
                pass
        except (OSError, CdpError):
            pass
        self.sock.close()


def evaluate(expression: str, host: str = CDP_HOST, port: int = CDP_PORT, timeout: float = CDP_TIMEOUT,
             await_promise: bool = False, eval_timeout_ms: Optional[int] = None) -> Any:
    """The JSON value of ``expression`` evaluated in Steam's SharedJSContext (awaited when asked)."""
    deadline = _Deadline(timeout)
    path = shared_context_path(list_targets(host, port, deadline))
    ws = WebSocket(host, port, path, deadline)
    params: dict[str, Any] = {"expression": expression, "returnByValue": True, "silent": True}
    if await_promise:
        params["awaitPromise"] = True
    if eval_timeout_ms:
        params["timeout"] = int(eval_timeout_ms)
    try:
        ws.send_text(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": params}))
        while True:
            try:
                msg = _loads(ws.recv_message())
            except (ValueError, RecursionError) as e:
                raise CdpError("Steam's answer is not JSON", True) from e
            if not isinstance(msg, dict):
                continue
            if msg.get("method") == "Inspector.detached":
                raise CdpError("Steam detached the debugger", True)
            if msg.get("id") == 1:
                break  # anything else is an event
    finally:
        ws.close()
    if "error" in msg:
        text = str(msg["error"].get("message") if isinstance(msg["error"], dict) else msg["error"])[:200]
        # The expression only runs in an execution context; losing it mid-run is the one error after a start.
        raise CdpError(f"DevTools error: {text}", "context was destroyed" in text)
    result = msg.get("result")
    if not isinstance(result, dict):
        raise CdpError("Steam's answer has no result", True)
    details = result.get("exceptionDetails")
    if details:
        exc = details.get("exception") if isinstance(details, dict) else None
        text = (exc.get("description") if isinstance(exc, dict) else None) or \
            (details.get("text") if isinstance(details, dict) else None)
        raise CdpError(f"the script failed in Steam: {str(text)[:200]}")
    value = result.get("result")
    return value.get("value") if isinstance(value, dict) else None
