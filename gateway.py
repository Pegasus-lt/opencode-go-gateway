#!/usr/bin/env python3
"""
ogo-gw — OpenCode Go 本地网关

解决: Request is missing x-opencode-session (400 MissingSessionID)

原理: OpenCode Go 网关把请求按 x-opencode-session 做一致性路由 + prompt cache 亲和。
      客户端不发这头 -> 400。官方 opencode CLI 一直发, 所以第三方客户端炸。
      本地网关做反向代理, 把客户端自带的会话头(x-session-affinity 等)
      翻译成 x-opencode-session 再转发。

      网关不编造会话身份: 客户端一个会话头都没带 -> 直接 400 并提示要带哪个头。
      原因: 随机/哈希派生都能过 400, 但拿不到稳定缓存亲和, 会按全价烧钱。

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
import http.client
import json
import os
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

# 客户端自己的会话头 —— 谁给了就用谁的(优先级从高到低), 原样透传。
# 这样能保住"同一对话 → 同一 session → 缓存命中"的本意,
# 而不是我们在网关里瞎编一个把对话切开。
#
# 刻意不含 x-client-request-id —— 那是"每请求唯一"的关联 id, 拿它当
# session 会让每次请求换一次 session, 缓存永远打不中, 而且是静默失效
# (过了 400, 但按全价烧钱)。
#
# 不做形态整形: 2026-10-10 实测(deepseek-v4-flash) 上游缓存与 session 形态无关
# —— 7 种形态(官方/大写/裸 hex/短串/uuid/非hex)全部命中。网关是透明代理,
# 改写客户端的会话身份既没必要也不让人意外。
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


def pick_session(headers) -> str:
    """命中客户端会话头就原样用(按 NATIVE_SESSION_HEADERS 优先级), 都没有返回 None。

    网关不做任何内容派生 —— 会话身份必须由客户端自己给(见 _forward 的 400 拒绝)。
    """
    for h in NATIVE_SESSION_HEADERS:
        v = (headers.get(h) or "").strip()
        if v:
            return v
    return None


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
        # /models 这类: 走同一条转发逻辑(不补 session, 上游不需要)
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
        # 会话头只在推理请求(POST)上处理: 上游只对 POST 校验, GET /v1/models
        # 这类不带也 200。处理方式是「翻译」而不是「编造」——
        # 客户端必须自带 NATIVE_SESSION_HEADERS 里的任一头, 否则 400。
        inject_session = self.command == "POST"
        out = {}
        for k, v in self.headers.items():
            lk = k.lower()
            # authorization 后面统一设置, 避免重复
            if lk in DROP_REQ or lk == "authorization":
                continue
            out[k] = v

        if inject_session:
            sid = pick_session(self.headers)
            if not sid:
                if self.verbose:
                    log(f"→ {self.command} {self.path}  400 missing session")
                return self._err(
                    400,
                    "missing session header: send x-session-affinity / "
                    "x-opencode-session (the gateway does not derive one)")
            # 客户端已有 x-opencode-session 就原样用; 只带别的会话头则翻译一份
            if not any(k.lower() == SESSION_HEADER for k in out):
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
            if inject_session:
                log(f"→ {self.command} {path}  session={sid[:14]}…")
            else:
                log(f"→ {self.command} {path}")

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
  会话头     只透传/翻译客户端自带的(x-session-affinity 等); 缺失返回 400
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