#!/usr/bin/env python3
"""
ogo-gw — OpenCode Go 本地网关

解决: Request is missing x-opencode-session (400 MissingSessionID)

原理: OpenCode Go 网关把请求按 x-opencode-session 做一致性路由 + prompt cache 亲和。
      客户端不发这头 -> 400。官方 opencode CLI 一直发, 所以只有第三方客户端炸。
      本地网关做反向代理, 在转发前补上这头。

只用标准库, 不需要 pip install。

启动:
    python gateway.py
    # 或指定端口/上游
    python gateway.py --port 8787 --upstream https://opencode.ai/zen/go

然后把客户端的 Base URL 从
    https://opencode.ai/zen/go/v1
改成
    http://127.0.0.1:8787/v1

API Key 不变。
"""

import argparse
import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- config

DEFAULT_UPSTREAM = "https://opencode.ai/zen/go"
DEFAULT_PORT = int(os.environ.get("OGO_GW_PORT", "8787"))
SESSION_HEADER = "x-opencode-session"
CLIENT_HEADER = "x-opencode-client"
# 默认不发 x-opencode-client: 实测它对 400 MissingSessionID 没有任何作用,
# 强制校验的只有 x-opencode-session。想要标识时用 --tag 显式开启。
DEFAULT_CLIENT_TAG = ""

# 客户端自己的会话头 —— 谁给了就用谁的(优先级从高到低)
# 这样能保住"同一对话 → 同一 session → 缓存命中"的本意,
# 而不是我们在网关里瞎编一个把对话切开。
#
# 注意 1: 刻意不含 x-client-request-id —— 那是"每请求唯一"的关联 id,
#          拿它当 session 会让每次请求换一次 session, 缓存永远打不中,
#          而且是静默失效(过了 400, 但按全价烧钱)。
# 注意 2: 也不是所有名字带 session 的头都可靠, 所以命中后还要经
#          normalize_session() 规整成官方形态(见下)。
NATIVE_SESSION_HEADERS = [
    "x-opencode-session",
    "x-claude-code-session-id",
    "x-deepseek-harness-session-id",
    "x-session-affinity",
    "x-session-id",
    "session-id",
    "session_id",
    "thread-id",
]

# 这些必须原样传给上游, 不能被我们改
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}
# 本地代理自己处理, 不转发
DROP_REQ = HOP_BY_HOP | {"host", "content-length", "accept-encoding"}

# 上游 Cloudflare 实测封禁的 User-Agent (Error 1010 浏览器签名校验):
# 缺 UA / python-requests / httpx / aiohttp / mimocode / node 都放行,
# 只有 python 标准库的 Python-urllib/3.x 被拒。命中的换成本网关的 UA。
BLOCKED_UA_PREFIX = "python-urllib/"
GATEWAY_UA = "ogo-gw/1.0"


# ---------------------------------------------------------------- helpers

def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


# 官方工具的形态: ses_ + 32 位小写 hex(实测只有这个形态能命中 prompt cache)
OFFICIAL_SESSION_RE = re.compile(r"^ses_[0-9a-f]{32}$", re.IGNORECASE)


def normalize_session(value: str) -> str:
    """
    把客户端给的 session 值整形成官方形态 ses_<32hex>。

    - 已经是官方形态 -> 原样带走(仅统一成小写), 客户端的真实会话 id 不被改写。
    - 其他形态(裸 hex / 短串 / UUID / 带横线) -> 确定性散列成官方形态。

    为什么要整形: 2026-10-07 实测, 裸 hex 在部分模型上不被识别为缓存键,
    等于过了 400 但缓存全废, 按全价烧钱。整形是确定性的(同一个原值永远
    得到同一个新 id), 所以"同一对话 → 同一 session → 缓存命中"不受影响,
    只是这个 id 现在能被上游认成缓存键了。
    """
    if OFFICIAL_SESSION_RE.match(value):
        return value.lower()
    return "ses_" + hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:32]


def content_derived_session(body: bytes) -> str:
    """
    客户端一个会话头都没给时的兜底。

    关键 1: 只取 model + system prompt + 第一条 user 消息做哈希。
    后续轮次这三段不变, 所以哈希稳定 -> 同一对话拿到同一个 id,
    缓存亲和照样成立。不取全量 body, 否则每轮 prompt 变长都会换 id, 缓存全废。

    关键 2: 必须加 model —— 同一句 "hi" 在两个模型上是两份缓存,
    共用一个 session 只会互相颠簸。也顺带降低不同对话撞车的概率
    (不同模型 → 不同 id)。

    已知局限: 两条对话如果 model + system + 首条 user 完全一样(都以
    "hi" 开头), 仍会撞到同一个 id —— 单个无状态请求里没有别的信号能
    区分对话。要真正避免, 只能让客户端发会话头(见 NATIVE_SESSION_HEADERS)。

    关键 3: 必须加 ses_ 前缀(官方工具的形态是 ses_ + 32 位小写 hex)。
    2026-10-07 实测: 同一 447 token prompt, 裸 hex -> cached_tokens 恒为 0;
    ses_<32hex> -> cached_tokens 384。即裸 hex 在部分模型上不被识别为缓存键,
    等于过了 400 但缓存全废, 按全价烧钱。
    """
    try:
        data = json.loads(body or b"")
    except Exception:
        return "ses_" + hashlib.sha256(body or b"ogo").hexdigest()[:32]

    if not isinstance(data, dict):
        return "ses_" + hashlib.sha256(body or b"ogo").hexdigest()[:32]

    parts = []

    # model: 同一对话在不同模型上是两份缓存, 不应共用一个 session
    model = data.get("model")
    if isinstance(model, str):
        parts.append(model)

    sys_msg = data.get("system")
    if isinstance(sys_msg, str):
        parts.append(sys_msg)
    elif isinstance(sys_msg, list):
        parts.extend(
            m.get("text", "") for m in sys_msg
            if isinstance(m, dict) and m.get("type") in (None, "text")
        )

    for m in data.get("messages") or []:
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, list):
                parts.extend(
                    b.get("text", "") for b in c
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            break  # 只要第一条 user

    seed = "\x00".join(parts) or "ogo-default"
    return "ses_" + hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:32]


def pick_session(headers, body: bytes) -> str:
    """决定这一跳用哪个 session id。恒返回官方形态 ses_<32hex>。"""
    for h in NATIVE_SESSION_HEADERS:
        v = (headers.get(h) or "").strip()
        if v:
            # 客户端给的值也可能不是官方形态(裸 hex / UUID / 短串),
            # 原样带过去会过 400 但缓存打不中, 所以先整形成官方形态。
            return normalize_session(v)
    # content_derived_session 内部已加 ses_ 前缀(官方格式), 不要再包一层
    return content_derived_session(body)


# ---------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ogo-gw"
    upstream = DEFAULT_UPSTREAM
    client_tag = DEFAULT_CLIENT_TAG
    verbose = False

    # ---- util

    def log_message(self, fmt, *args):
        if self.verbose:
            log("  ", fmt % args)

    def handle(self):
        # 客户端提前断开(keep-alive 超时/主动关)时 socketserver 会把
        # 异常当未处理异常打印整段 traceback 刷屏。
        # 对 HTTP 代理这属正常事件, 吞掉即可。
        #
        # 必须用 ConnectionError 而不是 ConnectionResetError:
        # Windows 上客户端断开抛的是 ConnectionAbortedError (WinError 10053),
        # 它是 ConnectionResetError 的兄弟类, 单独捕 ConnectionResetError 抓不到
        # (日志里 8 次 traceback 全是它)。ConnectionError 一次性覆盖
        # Reset / Aborted / Refused / BrokenPipe。
        try:
            super().handle()
        except (ConnectionError, TimeoutError):
            self.close_connection = True

    def _read_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while True:
                raw = self.rfile.readline()
                if not raw:
                    break
                line = raw.strip()
                if not line:
                    break
                try:
                    n = int(line.split(b";")[0], 16)
                except ValueError:
                    break
                if n == 0:
                    # 吃掉尾部 trailer 行直到空行; 否则 keep-alive 下
                    # 下一个请求的解析会从 trailer 处错位
                    while True:
                        t = self.rfile.readline()
                        if not t or t in (b"\r\n", b"\n"):
                            break
                    break
                chunks.append(self.rfile.read(n))
                self.rfile.readline()
            return b"".join(chunks)
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _client_addr(self) -> tuple:
        # 只用于日志/审计: X-Forwarded-For 第一跳是 ip[:port], 不要拿去 int()。
        # (旧实现 int("127.0.0.1") 会直接 ValueError, 且从未被调用过)
        fwd = self.headers.get("X-Forwarded-For")
        if fwd:
            first = fwd.split(",")[0].strip()
            host = first.rsplit(":", 1)[0] if first.count(":") == 1 else first
            return (host or "0.0.0.0", 0)
        return self.client_address

    # ---- routes

    def do_GET(self):
        if self.path.split("?")[0] == "/_health":
            return self._health()
        # /models 这类: 走同一条转发逻辑(也注入头)
        return self._forward()

    def do_POST(self):
        return self._forward()

    # 别的方法也只做转发(旧版只实现了 GET/POST, 其余一律 501)
    def do_PUT(self):
        return self._forward()

    def do_PATCH(self):
        return self._forward()

    def do_DELETE(self):
        return self._forward()

    def do_OPTIONS(self):
        return self._forward()

    def _health(self):
        body = json.dumps({
            "ok": True,
            "upstream": self.upstream,
            "session_header": SESSION_HEADER,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- core

    def _forward(self):
        body = self._read_body()
        u = urllib.parse.urlsplit(self.upstream)
        target = self.path
        if not target.startswith("/"):
            target = "/" + target

        # ---- 组装发往上游的 header
        out = {}
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in DROP_REQ or lk in (SESSION_HEADER, "authorization"):
                continue
            out[k] = v

        sid = pick_session(self.headers, body)
        out[SESSION_HEADER] = sid
        # 仅当显式开了 --tag 才发; 客户端自带的则原样透传
        if self.client_tag and not any(k.lower() == CLIENT_HEADER for k in out):
            out[CLIENT_HEADER] = self.client_tag

        # 被上游封禁的 UA 换掉, 否则整条请求 403 (Error 1010)
        ua_key = next((k for k in out if k.lower() == "user-agent"), None)
        ua = out.get(ua_key, "") if ua_key else ""
        if ua.lower().startswith(BLOCKED_UA_PREFIX):
            if ua_key:
                del out[ua_key]
            out["User-Agent"] = GATEWAY_UA

        # 保留 Authorization / base url 里的 key 原样
        auth = self.headers.get("Authorization")
        if auth:
            out["Authorization"] = auth

        path = (u.path.rstrip("/") + target) or "/"
        if self.verbose:
            log(f"→ {self.command} {path}  session={sid[:14]}…")

        try:
            conn = (http.client.HTTPSConnection(u.hostname, u.port or 443,
                                                timeout=600, context=ssl.create_default_context())
                    if u.scheme == "https"
                    else http.client.HTTPConnection(u.hostname, u.port or 80, timeout=600))
        except Exception as e:
            return self._err(502, f"upstream connect failed: {e}")

        try:
            conn.request(self.command, path, body=body or None, headers=out)
            resp = conn.getresponse()
        except Exception as e:
            try:
                conn.close()
            except Exception:
                pass
            return self._err(502, f"upstream request failed: {e}")

        ctype = resp.getheader("Content-Type", "")
        streaming = "event-stream" in ctype.lower()

        # 缓冲路径先把 body 读完, 读挂了还能回一个干净的 502;
        # 若先 send_response 再读, 异常时客户端只会拿到断连。
        data = None
        if not streaming:
            try:
                data = resp.read()
            except Exception as e:
                try:
                    conn.close()
                except Exception:
                    pass
                return self._err(502, f"upstream read failed: {e}")

        self.send_response(resp.status)
        # content-encoding 不剥: 网关从不转码, 剥了头又留压缩字节会把 body 弄坏。
        # date/server 也不透传: send_response 已经发了一份, 再转发会重复。
        hop = HOP_BY_HOP | {"content-length", "date", "server"}
        for k, v in resp.getheaders():
            if k.lower() in hop:
                continue
            self.send_header(k, v)
        # 自己管 body 长度/编码
        if streaming:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(len(data)))
        self.end_headers()

        try:
            if streaming:
                # SSE: read1() 单次底层读, 有多少转发多少。
                # 千万不能用 read(4096): 它会阻塞攒满 4096 字节才返回,
                # 实测把上游分 20 次到达的流憋成末尾一次性爆发。
                while True:
                    chunk = resp.read1(4096)
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            else:
                self.wfile.write(data)
        except ConnectionError:
            pass  # 客户端自己断了(含 Windows 的 ConnectionAbortedError/10053), 正常
        except Exception as e:
            if self.verbose:
                log("stream error:", e)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _err(self, code, msg):
        body = json.dumps({"error": {"message": msg, "type": "ogo_gateway_error"}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="OpenCode Go 本地网关(注入 x-opencode-session)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    ap.add_argument("--tag", default=DEFAULT_CLIENT_TAG,
                    help="x-opencode-client 的值(默认空=不发送该头; 非必需)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印每跳日志")
    args = ap.parse_args()

    Handler.upstream = args.upstream.rstrip("/")
    Handler.client_tag = args.tag
    Handler.verbose = args.verbose

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True

    print(f"""
  ogo-gw  已启动
  ────────────────────────────────────────────
  本地入口   http://{args.host}:{args.port}/v1
  上游       {Handler.upstream}
  注入头     {SESSION_HEADER} (缺失时按对话内容派生)
  ────────────────────────────────────────────
  客户端里把 Base URL 换成  http://{args.host}:{args.port}/v1
  API Key 不用改。Ctrl+C 停止。
""")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()