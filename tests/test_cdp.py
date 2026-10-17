import base64
import hashlib
import json
import socket
import struct
import threading
import unittest

from oxp3_tool import cdp

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def frame(opcode, payload, fin=True, mask=False):
    header = bytes([(0x80 if fin else 0) | opcode])
    n = len(payload)
    m = 0x80 if mask else 0
    if n < 126:
        header += bytes([m | n])
    elif n < 1 << 16:
        header += bytes([m | 126]) + struct.pack("!H", n)
    else:
        header += bytes([m | 127]) + struct.pack("!Q", n)
    if mask:
        key = b"\x01\x02\x03\x04"
        return header + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return header + payload


def read_frame(conn):
    def read(n):
        data = b""
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data
    b0, b1 = read(2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack("!H", read(2))[0]
    elif n == 127:
        n = struct.unpack("!Q", read(8))[0]
    assert b1 & 0x80, "client frames must be masked"
    key = read(4)
    payload = bytes(b ^ key[i % 4] for i, b in enumerate(read(n)))
    return b0 & 0x0F, payload


SJC = "0123456789ABCDEF0123456789ABCDEF"
BPM = "FEDCBA9876543210FEDCBA9876543210"


def page(title, page_id, port, **extra):
    target = {"id": page_id, "type": "page", "title": title, "url": "https://steamloopback.host/index.html",
              "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/page/{page_id}"}
    target.update(extra)
    return target


class FakeSteam:
    """/json plus a WebSocket page that answers Runtime.evaluate as ``reply`` says."""

    def __init__(self, reply, title="SharedJSContext", targets=None, accept=None):
        self.reply = reply
        self.title = title
        self.accept = accept
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        self.targets = targets
        self.requests = []
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                try:
                    self.handle(conn)
                except (EOFError, OSError):
                    pass

    def handle(self, conn):
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = conn.recv(4096)
            if not chunk:
                return
            head += chunk
        lines = head.decode().split("\r\n")
        path = lines[0].split()[1]
        headers = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
        if path == "/json/list":
            body = json.dumps(self.targets if self.targets is not None else [
                page("Steam Big Picture Mode", BPM, self.port),
                page(self.title, SJC, self.port),
            ]).encode()
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(body)
                         + body)
            return
        self.path = path
        self.origin = headers.get("origin")
        key = headers["sec-websocket-key"]
        accept = self.accept or base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        opcode, payload = read_frame(conn)
        request = json.loads(payload)
        self.requests.append(request)
        for chunk in self.reply(request):
            conn.sendall(chunk)
        try:
            read_frame(conn)  # the client's close frame
        except (EOFError, OSError, AssertionError):
            pass

    def close(self):
        self.server.close()


def answer(value):
    def reply(request):
        return [frame(0x1, json.dumps({"id": request["id"], "result": {"result": {"type": "object", "value": value}}})
                      .encode())]
    return reply


class CdpTest(unittest.TestCase):
    def fake(self, reply, **kw):
        steam = FakeSteam(reply, **kw)
        self.addCleanup(steam.close)
        return steam

    def test_evaluates_in_the_shared_context(self):
        steam = self.fake(answer({"ok": True, "enabled": True}))
        self.assertEqual(cdp.evaluate("1+1", port=steam.port), {"ok": True, "enabled": True})
        self.assertEqual(steam.path, f"/devtools/page/{SJC}")
        self.assertIsNone(steam.origin)
        self.assertEqual(steam.requests, [{"id": 1, "method": "Runtime.evaluate",
                                           "params": {"expression": "1+1", "returnByValue": True, "silent": True}}])
        cdp.evaluate("x", port=steam.port, await_promise=True, eval_timeout_ms=1500)
        self.assertEqual(steam.requests[-1]["params"],
                         {"expression": "x", "returnByValue": True, "silent": True, "awaitPromise": True,
                          "timeout": 1500})

    def test_events_pings_fragments_and_large_answers(self):
        big = "x" * 70000

        def reply(request):
            text = json.dumps({"id": 1, "result": {"result": {"value": big}}}).encode()
            return [frame(0x1, json.dumps({"method": "Runtime.consoleAPICalled"}).encode()),
                    frame(0x9, b"hi"),
                    frame(0x1, text[:10], fin=False), frame(0x0, text[10:200], fin=False),
                    frame(0x0, text[200:], fin=True)]
        self.assertEqual(cdp.evaluate("big", port=self.fake(reply).port), big)

    def test_failures_after_the_request_may_have_run(self):
        def masked(request):
            return [frame(0x1, json.dumps({"id": 1, "result": {"result": {"value": 7}}}).encode(), mask=True)]

        def binary(request):
            return [frame(0x2, b"\x00\x01")]

        def not_utf8(request):
            return [frame(0x1, b"\xff\xfe")]

        def detached(request):
            return [frame(0x1, json.dumps({"method": "Inspector.detached", "params": {}}).encode())]

        def destroyed(request):
            error = {"code": -32000, "message": "Execution context was destroyed."}
            return [frame(0x1, json.dumps({"id": 1, "error": error}).encode())]

        def closes(request):
            return [frame(0x8, b"")]

        def not_json(request):
            return [frame(0x1, b"{nope")]
        for reply in (masked, binary, not_utf8, detached, destroyed, closes, not_json):
            with self.assertRaises(cdp.CdpError) as caught:
                cdp.evaluate("x", port=self.fake(reply).port)
            self.assertTrue(caught.exception.maybe_ran, reply.__name__)

    def test_failures_before_anything_ran(self):
        # The script never started (an exception is a syntax or top-level error of an
        # expression that never rejects), or Steam refused the request.
        def threw(request):
            return [frame(0x1, json.dumps({"id": 1, "result": {"result": {"type": "object"}, "exceptionDetails": {
                "text": "Uncaught", "exception": {"description": "SyntaxError: x"}}}}).encode())]

        def error(request):
            return [frame(0x1, json.dumps({"id": 1, "error": {"code": -32603, "message": "Internal error"}}).encode())]
        for steam, text in ((self.fake(threw), "the script failed in Steam: SyntaxError: x"),
                            (self.fake(error), "DevTools error: Internal error"),
                            (self.fake(answer(1), title="Other"), "SharedJSContext page was not found"),
                            (self.fake(answer(1), accept="bad"), "does not match")):
            with self.assertRaisesRegex(cdp.CdpError, text) as caught:
                cdp.evaluate("x", port=steam.port)
            self.assertFalse(caught.exception.maybe_ran, text)

    def test_only_steams_own_pages(self):
        good = page("SharedJSContext", SJC, 8080)
        self.assertEqual(cdp.shared_context_path([good]), f"/devtools/page/{SJC}")
        # The advertised host is never used: only the page id goes into the path.
        self.assertEqual(cdp.shared_context_path([dict(good, webSocketDebuggerUrl=None)]), f"/devtools/page/{SJC}")
        for bad in (dict(good, type="service_worker"), dict(good, url="https://example.com/"),
                    dict(good, id="../../x"), dict(good, webSocketDebuggerUrl="ws://192.0.2.1/devtools/page/OTHER")):
            with self.assertRaisesRegex(cdp.CdpError, "not found"):
                cdp.shared_context_path([bad])

    def test_remote_debugging_off(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()  # nothing listens there
        with self.assertRaisesRegex(cdp.CdpError, "not reachable") as caught:
            cdp.evaluate("x", port=port)
        self.assertFalse(caught.exception.maybe_ran)

    def test_silent_steam_times_out(self):
        def silent(request):
            return []
        with self.assertRaisesRegex(cdp.CdpError, "in time") as caught:
            cdp.evaluate("x", port=self.fake(silent).port, timeout=0.5)
        self.assertTrue(caught.exception.maybe_ran)  # the request was out: it may have run


if __name__ == "__main__":
    unittest.main()
