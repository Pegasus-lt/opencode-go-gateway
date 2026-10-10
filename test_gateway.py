#!/usr/bin/env python3
"""
test_gateway.py — ogo-gw 回归测试

不需要 API key, 也不打真实上游: 在本进程里起一个 mock 上游 + 一个真实网关,
然后打真实 HTTP 请求。跑:

    python test_gateway.py -v

覆盖本次修复:
  1. handle() 能吞掉 Windows 的 ConnectionAbortedError(WinError 10053)
  2. _client_addr 不再 int("127.0.0.1") 崩溃
  3. 派生 session 带 model, 不同模型不同 id; 同一对话跨轮稳定
  4. 非官方形态的 native session 头被整形成 ses_<32hex>
  5. x-client-request-id 不再被当成 session
  6. PUT/PATCH/DELETE/OPTIONS 不再 501
"""

import http.client
import http.server
import io
import json
import re
import socket
import struct
import sys
import threading
import time
import unittest
import urllib.request

import gateway

# ---------------------------------------------------------------- mock 上游

RECORDS = []
LOCK = threading.Lock()


def clear_records():
    with LOCK:
        RECORDS.clear()


def records():
    with LOCK:
        return list(RECORDS)


class MockUpstream(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    sse_delay = 0.3

    def log_message(self, *a):
        pass

    def _read_body(self) -> bytes:
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            out = []
            while True:
                line = self.rfile.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    break
                try:
                    n = int(line.split(b";")[0], 16)
                except ValueError:
                    break
                if n == 0:
                    while True:
                        t = self.rfile.readline()
                        if not t or t in (b"\r\n", b"\n"):
                            break
                    break
                out.append(self.rfile.read(n))
                self.rfile.readline()
            return b"".join(out)
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _record(self, body: bytes) -> dict:
        rec = {
            "method": self.command,
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            # 保留重复项, 用来断言"同一头没有发两次"
            "header_items": [(k.lower(), v) for k, v in self.headers.items()],
            "body": body,
        }
        with LOCK:
            RECORDS.append(rec)
        return rec

    def count_header(self, rec, name):
        return sum(1 for k, _ in rec["header_items"] if k == name)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        rec = self._record(b"")
        if self.path.split("?")[0].endswith("/models"):
            self._send_json({"data": [{"id": "deepseek-v4-flash"}]})
        else:
            self._send_json({"error": {"message": "no route"}}, 404)

    def do_POST(self):
        body = self._read_body()
        rec = self._record(body)
        try:
            data = json.loads(body or b"{}")
        except Exception:
            data = {}
        if data.get("stream"):
            return self._sse(rec)
        self._send_json({
            "choices": [{"message": {"role": "assistant", "content": "pong"}}],
            "echo_session": rec["headers"].get("x-opencode-session"),
            "echo_auth": rec["headers"].get("authorization"),
            "echo_ua": rec["headers"].get("user-agent"),
        })

    def do_DELETE(self):
        rec = self._record(self._read_body())
        self._send_json({"ok": True, "session": rec["headers"].get("x-opencode-session")})

    do_PUT = do_PATCH = do_DELETE

    def _sse(self, rec):
        sid = rec["headers"].get("x-opencode-session", "")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for i in range(3):
            chunk = f'data: {{"n":{i},"session":"{sid}"}}\n\n'.encode()
            self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.flush()
            time.sleep(self.sse_delay)
        done = b"data: [DONE]\n\n"
        self.wfile.write(b"%x\r\n%s\r\n" % (len(done), done))
        self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


# ---------------------------------------------------------------- helpers

def chat_body(text="hi", model="deepseek-v4-flash", messages=None, stream=False):
    msgs = messages or [{"role": "user", "content": text}]
    return json.dumps({
        "model": model, "max_tokens": 8, "messages": msgs, "stream": stream,
    }).encode()


def raw_request(port, payload, timeout=15):
    """裸 socket 发请求, 返回 (status, headers, body)。"""
    s = socket.create_connection(("127.0.0.1", port), timeout)
    try:
        s.sendall(payload)
        buf = b""
        while b"\r\n\r\n" not in buf:
            d = s.recv(4096)
            if not d:
                break
            buf += d
        head, _, rest = buf.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        if not lines or not lines[0]:
            return 0, {}, rest
        status = int(lines[0].split()[1])
        hdrs = {}
        for line in lines[1:]:
            k, _, v = line.partition(b":")
            hdrs[k.strip().lower().decode("latin-1")] = v.strip().decode("latin-1")
        clen = int(hdrs.get("content-length", 0) or 0)
        while len(rest) < clen:
            d = s.recv(4096)
            if not d:
                break
            rest += d
        return status, hdrs, rest[:clen]
    finally:
        s.close()


class GatewayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockUpstream)
        cls.mock.daemon_threads = True
        threading.Thread(target=cls.mock.serve_forever, daemon=True).start()

        gateway.Handler.upstream = f"http://127.0.0.1:{cls.mock.server_address[1]}"
        gateway.Handler.client_tag = ""
        gateway.Handler.verbose = False

        cls.gw = http.server.ThreadingHTTPServer(("127.0.0.1", 0), gateway.Handler)
        cls.gw.daemon_threads = True
        threading.Thread(target=cls.gw.serve_forever, daemon=True).start()
        cls.port = cls.gw.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.gw.shutdown()
        cls.gw.server_close()
        cls.mock.shutdown()
        cls.mock.server_close()

    def setUp(self):
        clear_records()

    def call(self, path, data=None, headers=None, method=None):
        req = urllib.request.Request(
            f"{self.base}{path}", data=data, headers=headers or {}, method=method)
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, dict(r.headers), r.read()

    # ------------------------------------------------- 1. ConnectionAbortedError

    def test_handle_swallows_windows_connection_aborted(self):
        """修复 #1: Windows 的 ConnectionAbortedError 必须被吞掉。"""
        import http.server as hs
        orig = hs.BaseHTTPRequestHandler.handle
        cases = [
            ConnectionAbortedError(10053, "aborted"),   # WinError 10053
            ConnectionResetError(104, "reset"),
            BrokenPipeError(32, "broken pipe"),
            TimeoutError("timed out"),
        ]
        try:
            for exc in cases:
                def fake(self, _e=exc):
                    raise _e
                hs.BaseHTTPRequestHandler.handle = fake
                h = gateway.Handler.__new__(gateway.Handler)
                h.close_connection = False
                h.handle()                      # 必须不抛
                self.assertTrue(h.close_connection,
                                f"未进 except 分支 (没处理 {type(exc).__name__})")
        finally:
            hs.BaseHTTPRequestHandler.handle = orig

    def test_handle_still_raises_unrelated_errors(self):
        """别把真正的 bug 也吞了。"""
        import http.server as hs
        orig = hs.BaseHTTPRequestHandler.handle
        def fake(self):
            raise ValueError("real bug")
        hs.BaseHTTPRequestHandler.handle = fake
        try:
            h = gateway.Handler.__new__(gateway.Handler)
            with self.assertRaises(ValueError):
                h.handle()
        finally:
            hs.BaseHTTPRequestHandler.handle = orig

    def test_abrupt_client_rst_produces_no_traceback(self):
        """真实 RST: socketserver 不该往 stderr 刷 traceback。"""
        captured = io.StringIO()
        old = sys.stderr
        sys.stderr = captured
        try:
            s = socket.create_connection(("127.0.0.1", self.port), 10)
            s.sendall(b"GET /_health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            time.sleep(0.3)
            # SO_LINGER=0 -> close() 直接发 RST
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            s.close()
            time.sleep(0.8)
        finally:
            sys.stderr = old
        out = captured.getvalue()
        self.assertNotIn("Traceback", out, f"RST 后仍打印了 traceback:\n{out}")

    # ------------------------------------------------- 2. _client_addr

    def test_client_addr_with_xff_does_not_crash(self):
        """修复 #2: 旧实现 int(\"127.0.0.1\") 会 ValueError。"""
        h = gateway.Handler.__new__(gateway.Handler)
        h.client_address = ("10.0.0.5", 1234)

        class H(dict):
            def get(self, k, default=None):
                return dict.get(self, k.lower(), default)
        h.headers = H({"x-forwarded-for": "203.0.113.9, 10.0.0.1"})

        host, port = h._client_addr()
        self.assertEqual(host, "203.0.113.9")
        self.assertIsInstance(port, int)

        h.headers = H({})
        self.assertEqual(h._client_addr(), ("10.0.0.5", 1234))

    # ------------------------------------------------- 3. 派生 session

    def test_derived_session_is_official_form_end_to_end(self):
        body = chat_body("hello gateway")
        self.call("/v1/chat/completions", data=body,
                  headers={"Content-Type": "application/json"})
        rec = records()[0]
        sid = rec["headers"]["x-opencode-session"]
        self.assertRegex(sid, r"^ses_[0-9a-f]{32}$")
        self.assertEqual(sid, gateway.content_derived_session(body))
        self.assertEqual(self.count(rec, "x-opencode-session"), 1)

    def count(self, rec, name):
        return sum(1 for k, _ in rec["header_items"] if k == name)

    def test_derived_session_stable_across_turns(self):
        turn1 = chat_body(messages=[
            {"role": "user", "content": "long system-ish preamble, please help"},
        ])
        turn2 = chat_body(messages=[
            {"role": "user", "content": "long system-ish preamble, please help"},
            {"role": "assistant", "content": "sure"},
            {"role": "user", "content": "and now the follow-up question?"},
        ])
        for body in (turn1, turn2):
            self.call("/v1/chat/completions", data=body,
                      headers={"Content-Type": "application/json"})
        recs = records()
        sids = [r["headers"]["x-opencode-session"] for r in recs]
        self.assertEqual(len(sids), 2)
        self.assertEqual(sids[0], sids[1], "同一对话跨轮 session 变了 -> 缓存废")

    def test_derived_session_differs_by_first_user(self):
        a = chat_body(messages=[{"role": "user", "content": "question A"}])
        b = chat_body(messages=[{"role": "user", "content": "question B"}])
        self.assertNotEqual(gateway.content_derived_session(a),
                            gateway.content_derived_session(b))

    def test_derived_session_differs_by_model(self):
        """修复 #3 的一部分: 同一句话不同模型 = 两份缓存, 不该共用 id。"""
        a = chat_body(model="deepseek-v4-flash")
        b = chat_body(model="deepseek-v4-pro")
        self.assertNotEqual(gateway.content_derived_session(a),
                            gateway.content_derived_session(b))

        for m in ("deepseek-v4-flash", "deepseek-v4-pro"):
            self.call("/v1/chat/completions", data=chat_body(model=m),
                      headers={"Content-Type": "application/json"})
        sids = [r["headers"]["x-opencode-session"] for r in records()]
        self.assertNotEqual(sids[0], sids[1])

    def test_derived_session_changes_when_system_prompt_changes(self):
        a = json.dumps({"model": "m", "system": "sys A",
                        "messages": [{"role": "user", "content": "q"}]}).encode()
        b = json.dumps({"model": "m", "system": "sys B",
                        "messages": [{"role": "user", "content": "q"}]}).encode()
        self.assertNotEqual(gateway.content_derived_session(a),
                            gateway.content_derived_session(b))

    # ------------------------------------------------- 4/5. native session 头

    def test_official_native_session_passes_through_verbatim(self):
        sid = "ses_" + "ab" * 16
        self.call("/v1/chat/completions", data=chat_body(),
                  headers={"Content-Type": "application/json",
                           "x-session-affinity": sid})
        rec = records()[0]
        self.assertEqual(rec["headers"]["x-opencode-session"], sid)
        self.assertEqual(self.count(rec, "x-opencode-session"), 1)

    def test_official_native_header_case_insensitive(self):
        sid = "ses_" + "cd" * 16
        self.call("/v1/chat/completions", data=chat_body(),
                  headers={"Content-Type": "application/json",
                           "X-OpenCode-Session": sid})
        rec = records()[0]
        self.assertEqual(rec["headers"]["x-opencode-session"], sid)
        self.assertEqual(self.count(rec, "x-opencode-session"), 1)

    def test_native_values_passed_through_verbatim(self):
        """客户端给的 session 必须原样透传 —— 网关没有资格替它改名。

        依据: _probe_session_form.py 实测(2026-10-10, deepseek-v4-flash)
        上游缓存与 session 形态无关, 7 种形态全部命中。所以整形既没必要,
        又会把客户端的会话身份换掉。
        """
        cases = [
            "ses_" + "ef" * 16,                        # 官方形态
            "SES_" + "EF" * 16,                        # 官方形态大写
            "1bda55fef85296e4aebe834442ee6254",          # 裸 32hex
            "af62ec49467f0f244a0c7dce04dab6ad9fbc782281",  # 裸 64hex
            "abc-123",                                  # 短串
            "550e8400-e29b-41d4-a716-446655440000",     # uuid
            "ses_" + "z" * 31,                         # ses_ 但不是 hex
            "a" * 200,                                 # 超长
        ]
        for value in cases:
            got = gateway.pick_session({"x-session-id": value}, b"")
            self.assertEqual(got, value, f"被改写了: {value!r} -> {got!r}")

    def test_native_value_whitespace_stripped(self):
        got = gateway.pick_session({"x-session-id": "  abc-123  "}, b"")
        self.assertEqual(got, "abc-123")

    def test_non_official_native_header_passed_through_end_to_end(self):
        self.call("/v1/chat/completions", data=chat_body(),
                  headers={"Content-Type": "application/json",
                           "x-session-id": "abc-123"})
        sid = records()[0]["headers"]["x-opencode-session"]
        self.assertEqual(sid, "abc-123", "非官方形态被改写了")

    def test_client_request_id_not_used_as_session(self):
        """修复 #5: x-client-request-id 是每请求唯一 id, 不该当 session。"""
        body = chat_body()
        self.call("/v1/chat/completions", data=body,
                  headers={"Content-Type": "application/json",
                           "x-client-request-id": "req-000001"})
        sid = records()[0]["headers"]["x-opencode-session"]
        self.assertNotEqual(sid, "req-000001")
        self.assertEqual(sid, gateway.content_derived_session(body),
                         "x-client-request-id 不该影响 session 选择")

    def test_real_session_header_wins_over_request_id(self):
        sid = "ses_" + "11" * 16
        self.call("/v1/chat/completions", data=chat_body(),
                  headers={"Content-Type": "application/json",
                           "x-session-affinity": sid,
                           "x-client-request-id": "req-000002"})
        rec = records()[0]
        self.assertEqual(rec["headers"]["x-opencode-session"], sid)
        self.assertEqual(rec["headers"].get("x-client-request-id"), "req-000002",
                         "客户端自己的 x-client-request-id 应该照常透传")

    # ------------------------------------------------- 转发本身

    def test_health(self):
        st, _, body = self.call("/_health")
        data = json.loads(body)
        self.assertEqual(st, 200)
        self.assertTrue(data["ok"])

    def test_query_string_preserved(self):
        st, _, body = self.call("/v1/models?limit=5")
        self.assertEqual(st, 200)
        self.assertEqual(records()[0]["path"], "/v1/models?limit=5")
        self.assertIn("deepseek-v4-flash", body.decode())

    def test_models_get_also_injects_session(self):
        self.call("/v1/models")
        sid = records()[0]["headers"]["x-opencode-session"]
        self.assertRegex(sid, r"^ses_[0-9a-f]{32}$")

    def test_authorization_forwarded(self):
        self.call("/v1/chat/completions", data=chat_body(),
                  headers={"Content-Type": "application/json",
                           "Authorization": "Bearer sk-test-123"})
        rec = records()[0]
        self.assertEqual(rec["headers"].get("authorization"), "Bearer sk-test-123")
        self.assertEqual(json.loads(rec["body"]).get("model"), "deepseek-v4-flash")

    def test_hop_by_hop_and_accept_encoding_not_forwarded(self):
        self.call("/v1/chat/completions", data=chat_body(),
                  headers={"Content-Type": "application/json",
                           "Connection": "keep-alive",
                           "Accept-Encoding": "gzip, deflate",
                           "Proxy-Authorization": "nope"})
        rec = records()[0]
        # 网关刻意丢掉客户端的 accept-encoding; http.client 随后会补一个
        # Accept-Encoding: identity(= 不压缩), 所以上游侧永远是未压缩响应,
        # 不存在"剥了 content-encoding 又留压缩字节"的坑。但客户端要的 gzip
        # 绝不能被原样带过去。
        self.assertNotIn("gzip", rec["headers"].get("accept-encoding", "").lower())
        self.assertNotIn("deflate", rec["headers"].get("accept-encoding", "").lower())
        self.assertNotIn("connection", rec["headers"])
        self.assertNotIn("proxy-authorization", rec["headers"])
        # content-length 必须由网关按真实 body 重新给出
        self.assertEqual(int(rec["headers"]["content-length"]), len(rec["body"]))

    def test_blocked_python_ua_is_replaced(self):
        self.call("/v1/chat/completions", data=chat_body(),
                  headers={"Content-Type": "application/json",
                           "User-Agent": "Python-urllib/3.13"})
        self.assertEqual(records()[0]["headers"].get("user-agent"),
                         gateway.GATEWAY_UA)

    def test_normal_ua_passes_through(self):
        self.call("/v1/chat/completions", data=chat_body(),
                  headers={"Content-Type": "application/json",
                           "User-Agent": "mimocode/1.15.0"})
        self.assertEqual(records()[0]["headers"].get("user-agent"),
                         "mimocode/1.15.0")

    def test_chunked_request_body_forwarded_intact(self):
        """客户端用 chunked 上传时, body 必须完整 + Content-Length 正确。"""
        payload = chat_body(text="chunked body 请原样转发 " * 30)
        raw = (b"POST /v1/chat/completions HTTP/1.1\r\n"
               b"Host: 127.0.0.1\r\n"
               b"Content-Type: application/json\r\n"
               b"Transfer-Encoding: chunked\r\n"
               b"\r\n")
        half = len(payload) // 2
        for piece in (payload[:half], payload[half:]):
            raw += b"%x\r\n%s\r\n" % (len(piece), piece)
        raw += b"0\r\n\r\n"

        st, _, _ = raw_request(self.port, raw)
        self.assertEqual(st, 200)
        rec = records()[0]
        self.assertEqual(rec["body"], payload, "chunked body 被截断/改写")
        self.assertEqual(int(rec["headers"]["content-length"]), len(payload))
        self.assertNotIn("transfer-encoding", rec["headers"])

    # ------------------------------------------------- 流式

    def test_sse_streaming_is_not_buffered(self):
        body = chat_body(text="stream please", stream=True)
        req = urllib.request.Request(
            f"{self.base}/v1/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "User-Agent": "mimocode/1.15.0"})
        t0 = time.time()
        times, lines = [], []
        with urllib.request.urlopen(req, timeout=30) as resp:
            te = resp.headers.get("Transfer-Encoding", "")
            self.assertEqual(te.lower(), "chunked", f"TE={te}")
            for line in resp:
                lines.append(line)
                times.append(time.time() - t0)

        text = b"".join(lines)
        self.assertIn(b'"n":0', text)
        self.assertIn(b'"n":2', text)
        self.assertIn(b"[DONE]", text)
        # 上游每 0.3s 一个事件; 若网关整体缓冲, first/last 会几乎同时到
        spread = times[-1] - times[0]
        self.assertGreater(spread, 0.2,
                           f"SSE 被整体缓冲了 spread={spread:.3f}s "
                           f"(first={times[0]:.3f} last={times[-1]:.3f})")

    def test_sse_event_carries_session_header(self):
        sid = "ses_" + "22" * 16
        req = urllib.request.Request(
            f"{self.base}/v1/chat/completions", data=chat_body(stream=True),
            headers={"Content-Type": "application/json",
                     "x-session-affinity": sid})
        with urllib.request.urlopen(req, timeout=30) as resp:
            self.assertIn("text/event-stream",
                          resp.headers.get("Content-Type", ""))
            text = resp.read().decode()
        self.assertIn(sid, text)

    # ------------------------------------------------- 方法覆盖

    def test_delete_put_patch_not_501(self):
        for m in ("DELETE", "PUT", "PATCH"):
            with self.subTest(method=m):
                st, _, body = self.call("/v1/resource/1", data=b"", method=m)
                self.assertEqual(st, 200, f"{m} -> {st}")
                data = json.loads(body)
                self.assertTrue(data.get("ok"))
                self.assertRegex(data["session"], r"^ses_[0-9a-f]{32}$",
                                 f"{m} 没注入 session")

if __name__ == "__main__":
    unittest.main(verbosity=2)
