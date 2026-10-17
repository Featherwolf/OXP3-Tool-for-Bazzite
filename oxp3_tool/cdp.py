"""Run one JavaScript expression in Steam's UI over its CEF remote debugging.

Decky Loader turns on Steam's remote debugging (~/.steam/steam/.cef-enable-remote-debugging),
and Steam then serves the Chrome DevTools protocol on 127.0.0.1:8080. Steam's UI stores
(window.SystemPerfStore and others) live in its SharedJSContext page. This is a minimal
client for that one exchange, standard library only: list the pages over HTTP, open a
WebSocket to the shared context, send Runtime.evaluate, read the answer. Nothing but the
expression is sent, and the answer is treated as untrusted data.

oxp3-tool bundles this file into its helper scripts.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import socket
import struct
import time
from typing import Any
from urllib.parse import urlsplit

CDP_HOST = "127.0.0.1"
CDP_PORT = 8080
CDP_TIMEOUT = 3.0
MAX_MESSAGE = 1 << 20
MAX_HEADERS = 16384
# Page titles Decky Loader looks for, in its order (decky_loader/injector.py).
SHARED_CONTEXT_TITLES = ("SharedJSContext", "Steam Shared Context presented by Valve™", "Steam", "SP")
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class CdpError(Exception):
    pass


def list_targets(host: str = CDP_HOST, port: int = CDP_PORT, timeout: float = CDP_TIMEOUT) -> list[dict]:
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", "/json")
        res = conn.getresponse()
        if res.status != 200:
            raise CdpError(f"/json answered {res.status}")
        data = res.read(MAX_MESSAGE + 1)
    except (OSError, http.client.HTTPException) as e:
        raise CdpError(f"Steam's remote debugging is not reachable ({type(e).__name__})") from e
    finally:
        conn.close()
    if len(data) > MAX_MESSAGE:
        raise CdpError("Steam's page list is too large")
    try:
        targets = json.loads(data)
    except ValueError as e:
        raise CdpError("Steam's page list is not JSON") from e
    if not isinstance(targets, list):
        raise CdpError("Steam's page list is not a list")
    return [t for t in targets if isinstance(t, dict)]


def shared_context_url(targets: list[dict], host: str = CDP_HOST, port: int = CDP_PORT) -> str:
    """The WebSocket URL of Steam's shared JS context; only one on the same host and port."""
    for title in SHARED_CONTEXT_TITLES:
        for target in targets:
            url = target.get("webSocketDebuggerUrl")
            if target.get("title") != title or not isinstance(url, str):
                continue
            parts = urlsplit(url)
            if parts.scheme == "ws" and parts.hostname in (host, "localhost") and parts.port == port:
                return url
    raise CdpError("Steam's SharedJSContext page was not found")


class WebSocket:
    """Just enough of RFC 6455 for one request and its answer."""

    def __init__(self, url: str, deadline: float, host: str = CDP_HOST, port: int = CDP_PORT) -> None:
        self.deadline = deadline
        parts = urlsplit(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        self.sock = socket.create_connection((host, port), timeout=self._left())
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            # No Origin header: CEF only checks the origin of requests that send one.
            self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                               "Sec-WebSocket-Version: 13\r\n\r\n").encode())
            head = b""
            while b"\r\n\r\n" not in head:
                if len(head) > MAX_HEADERS:
                    raise CdpError("WebSocket handshake answer too large")
                head += self._recv(4096)
            head, _, self.buf = head.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            if len(lines[0].split()) < 2 or lines[0].split()[1] != "101":
                raise CdpError(f"WebSocket handshake refused: {lines[0][:80]}")
            headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
            accept = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
            if headers.get("sec-websocket-accept") != accept:
                raise CdpError("WebSocket handshake answer does not match")
        except BaseException:
            self.sock.close()
            raise

    def _left(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise CdpError("Steam did not answer in time")
        return left

    def _recv(self, n: int) -> bytes:
        self.sock.settimeout(self._left())
        try:
            data = self.sock.recv(n)
        except socket.timeout as e:
            raise CdpError("Steam did not answer in time") from e
        if not data:
            raise CdpError("Steam closed the connection")
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
        self.sock.settimeout(self._left())
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode())

    def recv_message(self) -> str:
        parts: list[bytes] = []
        size = 0
        while True:
            b0, b1 = self._read(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else b""
            if n > MAX_MESSAGE or size + n > MAX_MESSAGE:
                raise CdpError("Steam's answer is too large")
            payload = self._read(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise CdpError("Steam closed the connection")
            if opcode == 0x9:
                self._send_frame(0xA, payload)  # ping: answer and keep reading
                continue
            if opcode == 0xA:
                continue
            if opcode not in (0x0, 0x1, 0x2):
                raise CdpError(f"unexpected WebSocket opcode {opcode}")
            parts.append(payload)
            size += n
            if fin:
                return b"".join(parts).decode(errors="replace")

    def close(self) -> None:
        try:
            self.sock.settimeout(0.2)
            self._send_frame(0x8, b"")
        except (OSError, CdpError):
            pass
        self.sock.close()


def evaluate(expression: str, host: str = CDP_HOST, port: int = CDP_PORT, timeout: float = CDP_TIMEOUT) -> Any:
    """The JSON value of ``expression`` evaluated in Steam's SharedJSContext."""
    deadline = time.monotonic() + timeout
    url = shared_context_url(list_targets(host, port, timeout), host, port)
    ws = WebSocket(url, deadline, host, port)
    try:
        ws.send_text(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                 "params": {"expression": expression, "returnByValue": True}}))
        while True:
            try:
                msg = json.loads(ws.recv_message())
            except ValueError as e:
                raise CdpError("Steam's answer is not JSON") from e
            if isinstance(msg, dict) and msg.get("id") == 1:
                break  # anything else is an event
    finally:
        ws.close()
    if "error" in msg:
        raise CdpError(f"DevTools error: {str(msg['error'])[:200]}")
    result = msg.get("result")
    if not isinstance(result, dict):
        raise CdpError("Steam's answer has no result")
    if result.get("exceptionDetails"):
        details = result["exceptionDetails"]
        text = details.get("text") if isinstance(details, dict) else None
        raise CdpError(f"the script failed in Steam: {str(text)[:200]}")
    value = result.get("result")
    return value.get("value") if isinstance(value, dict) else None
