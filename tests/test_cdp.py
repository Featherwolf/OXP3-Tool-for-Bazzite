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
        if path == "/json":
            body = json.dumps(self.targets if self.targets is not None else [
                {"title": "Steam Big Picture Mode", "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/page/BPM"},
                {"title": self.title, "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/page/SJC"},
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
        self.assertEqual(steam.path, "/devtools/page/SJC")
        self.assertIsNone(steam.origin)
        self.assertEqual(steam.requests, [{"id": 1, "method": "Runtime.evaluate",
                                           "params": {"expression": "1+1", "returnByValue": True}}])

    def test_events_pings_fragments_and_large_answers(self):
        big = "x" * 70000

        def reply(request):
            text = json.dumps({"id": 1, "result": {"result": {"value": big}}}).encode()
            return [frame(0x1, json.dumps({"method": "Runtime.consoleAPICalled"}).encode()),
                    frame(0x9, b"hi"),
                    frame(0x1, text[:10], fin=False), frame(0x0, text[10:200], fin=False),
                    frame(0x0, text[200:], fin=True)]
        steam = self.fake(reply)
        self.assertEqual(cdp.evaluate("big", port=steam.port), big)

    def test_masked_server_frames_are_read(self):
        def reply(request):
            return [frame(0x1, json.dumps({"id": 1, "result": {"result": {"value": 7}}}).encode(), mask=True)]
        self.assertEqual(cdp.evaluate("7", port=self.fake(reply).port), 7)

    def test_failures(self):
        def threw(request):
            return [frame(0x1, json.dumps({"id": 1, "result": {"result": {"type": "object"},
                                                                "exceptionDetails": {"text": "Uncaught"}}}).encode())]

        def closes(request):
            return [frame(0x8, b"")]

        def error(request):
            return [frame(0x1, json.dumps({"id": 1, "error": {"code": -32000, "message": "nope"}}).encode())]
        for reply, text in ((threw, "the script failed in Steam: Uncaught"), (closes, "closed the connection"),
                            (error, "DevTools error")):
            with self.assertRaisesRegex(cdp.CdpError, text):
                cdp.evaluate("x", port=self.fake(reply).port)
        with self.assertRaisesRegex(cdp.CdpError, "SharedJSContext page was not found"):
            cdp.evaluate("x", port=self.fake(answer(1), title="Other").port)
        with self.assertRaisesRegex(cdp.CdpError, "does not match"):
            cdp.evaluate("x", port=self.fake(answer(1), accept="bad").port)

    def test_only_steams_own_host_and_port(self):
        steam = self.fake(answer(1))
        elsewhere = [{"title": "SharedJSContext", "webSocketDebuggerUrl": "ws://192.0.2.1:8080/devtools/page/X"},
                     {"title": "SharedJSContext", "webSocketDebuggerUrl": f"wss://127.0.0.1:{steam.port}/x"},
                     {"title": "SharedJSContext", "webSocketDebuggerUrl": "ws://127.0.0.1:1/x"}]
        with self.assertRaisesRegex(cdp.CdpError, "not found"):
            cdp.shared_context_url(elsewhere, port=steam.port)

    def test_remote_debugging_off(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()  # nothing listens there
        with self.assertRaisesRegex(cdp.CdpError, "not reachable"):
            cdp.evaluate("x", port=port)

    def test_silent_steam_times_out(self):
        def silent(request):
            return []
        with self.assertRaisesRegex(cdp.CdpError, "in time"):
            cdp.evaluate("x", port=self.fake(silent).port, timeout=0.5)


if __name__ == "__main__":
    unittest.main()
