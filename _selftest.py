#!/usr/bin/env python3
"""
_selftest.py — ogo-gw 网关能力自测

用法:
    set OGO_GW_KEY=sk-你的key          (Windows: set OGO_GW_KEY=sk-...)
    python _selftest.py [base_url] [api_key]

默认打 http://127.0.0.1:8788/v1 (测试实例)。
key 从第二个参数或环境变量 OGO_GW_KEY 读取 —— 不要写死在源码里。
覆盖: 健康检查 / 模型列表 / 无会话头派生 / x-session-affinity 透传 /
      会话稳定性 / 缓存亲和 / SSE 流式增量 / 大请求体 / 查询串透传。
只发小请求, 消耗极小。
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8788/v1").rstrip("/")
KEY = (sys.argv[2] if len(sys.argv) > 2 else "") or os.environ.get("OGO_GW_KEY", "")
if not KEY:
    print("!! 未提供 API key: python _selftest.py <base_url> <sk-...> "
          "或设置环境变量 OGO_GW_KEY", file=sys.stderr)
    sys.exit(2)
MODEL = "deepseek-v4.1-flash"
# MiMoCode 真实请求头形态: User-Agent 是 mimocode/<version>
MIMO_UA = "mimocode/1.15.0"

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f"  -- {detail}" if detail else ""), flush=True)


def req(method, url, data=None, headers=None, timeout=60, ua=MIMO_UA):
    h = {"Authorization": f"Bearer {KEY}"}
    if ua is not None:
        h["User-Agent"] = ua
    h.update(headers or {})
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def chat(body, extra_headers=None, timeout=90):
    h = {"Content-Type": "application/json"}
    h.update(extra_headers or {})
    return req("POST", f"{BASE}/chat/completions",
               data=json.dumps(body).encode(), headers=h, timeout=timeout)


# ---------------------------------------------------------------- 1. health
def t_health():
    root = BASE.rsplit("/v1", 1)[0] + "/_health"
    try:
        with urllib.request.urlopen(root, timeout=10) as r:
            d = json.load(r)
        check("health: /_health 返回 ok", d.get("ok") is True, json.dumps(d, ensure_ascii=False))
    except Exception as e:
        check("health: /_health 返回 ok", False, repr(e))


# ---------------------------------------------------------------- 2. models
def t_models():
    try:
        st, hdrs, body = req("GET", f"{BASE}/models")
        data = json.loads(body).get("data", [])
        ids = [m.get("id") for m in data]
        check("models: GET /v1/models 200 且非空", st == 200 and len(data) > 0,
              f"status={st} count={len(data)}")
        check(f"models: 含 {MODEL}", MODEL in ids)
    except Exception as e:
        check("models: GET /v1/models 200 且非空", False, repr(e))


# ------------------------------------------- 3. 无会话头 -> 内容派生 (curl 场景)
def t_derived_session():
    body = {
        "model": MODEL, "max_tokens": 8,
        "messages": [{"role": "user", "content": "reply with the word pong only"}],
    }
    try:
        st, _, b = chat(body)
        d = json.loads(b)
        ok = st == 200 and d.get("choices")
        check("derived: 无会话头请求 200", ok,
              f"status={st} err={d.get('error', {}).get('message', '')[:80]}")
    except Exception as e:
        check("derived: 无会话头请求 200", False, repr(e))


# --------------------------- 4. x-session-affinity (MiMoCode 真实请求头形态)
def t_mimo_affinity():
    body = {
        "model": MODEL, "max_tokens": 8,
        "messages": [{"role": "user", "content": "reply with the word pong only"}],
    }
    # mimo.exe 构造: x-session-affinity: ses_<32+> (官方形态), 无 x-opencode-session
    affinity = "ses_mimoaffinity0000000000000000000"
    try:
        st, _, b = chat(body, {"x-session-affinity": affinity})
        d = json.loads(b)
        check("mimocode: x-session-affinity 请求 200",
              st == 200 and bool(d.get("choices")),
              f"status={st} err={d.get('error', {}).get('message', '')[:80]}")
    except Exception as e:
        check("mimocode: x-session-affinity 请求 200", False, repr(e))


# --------------------------------- 5. 稳定 session -> 缓存命中 (成本核心)
def t_cache_affinity():
    # >128 token 的长前缀, 才超过上游最小缓存前缀
    long_prefix = ("You are a careful testing assistant. " * 60).strip()
    body = {
        "model": MODEL, "max_tokens": 8,
        "messages": [{"role": "user", "content": long_prefix + "\n\nReply: pong"}],
    }
    cached = []
    try:
        for i in range(3):
            st, _, b = chat(body)
            d = json.loads(b)
            if st != 200 or not d.get("choices"):
                check("cache: 重复请求 200", False, f"round={i} status={st} "
                      f"err={d.get('error', {}).get('message', '')[:80]}")
                return
            u = d.get("usage", {}) or {}
            cached.append((u.get("prompt_tokens_details") or {}).get("cached_tokens"))
            time.sleep(0.6)
        check("cache: 重复请求 200", True, f"usage={cached}")
        # 第 2 次起应命中缓存; 第 1 次为 0 属正常
        hit = cached[1] is not None and cached[1] > 0
        check("cache: 第 2 次起 cached_tokens>0 (缓存亲和)", hit, f"cached={cached}")
    except Exception as e:
        check("cache: 重复请求 200", False, repr(e))


# ------------------------------------------------------ 6. SSE 流式增量透传
def t_streaming():
    body = {
        "model": MODEL, "max_tokens": 64, "stream": True,
        "messages": [{"role": "user", "content": "Count from 1 to 20, one number per line."}],
    }
    r = urllib.request.Request(
        f"{BASE}/chat/completions", data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json",
                 "User-Agent": MIMO_UA})
    t0 = time.time()
    first_chunk = None
    last_chunk = None
    n_chunks = 0
    text_len = 0
    try:
        with urllib.request.urlopen(r, timeout=90) as resp:
            if resp.headers.get("Transfer-Encoding", "").lower() != "chunked":
                check("stream: 响应为 chunked", False,
                      f"TE={resp.headers.get('Transfer-Encoding')}")
            else:
                check("stream: 响应为 chunked", True)
            for line in resp:
                now = time.time() - t0
                if first_chunk is None:
                    first_chunk = now
                last_chunk = now
                n_chunks += 1
                if line.startswith(b"data: "):
                    payload = line[6:].strip()
                    if payload and payload != b"[DONE]":
                        try:
                            d = json.loads(payload)
                            for ch in d.get("choices") or []:
                                text_len += len((ch.get("delta") or {}).get("content") or "")
                        except json.JSONDecodeError:
                            pass
        total = time.time() - t0
        check("stream: 有增量数据", text_len > 0, f"text_len={text_len} chunks={n_chunks}")
        # 缓冲化症状: 首块与末块同时到达(全部憋到流结束才转发)。
        # 实测缓冲 bug 时 spread≈0.00~0.01s; 正常增量 ≥0.05s。
        # 阈值不能设太高: 模型发得快时正常 spread 也只有 0.1s 出头。
        incremental = (first_chunk is not None and last_chunk is not None
                       and (last_chunk - first_chunk) > 0.05)
        check("stream: 增量到达(未被整体缓冲)", incremental,
              f"first={first_chunk:.2f}s last={last_chunk:.2f}s total={total:.2f}s")
        check("stream: 首字节不慢", first_chunk is not None and first_chunk < 5.0,
              f"ttfb={first_chunk:.2f}s")
    except Exception as e:
        check("stream: 有增量数据", False, repr(e))


# ---------------------------------------------------------- 7. 大请求体完整性
def t_big_body():
    big = "训练数据片段 abc123。" * 4000  # ~100KB+
    body = {
        "model": MODEL, "max_tokens": 8,
        "messages": [{"role": "user", "content": big + "\n\nReply: pong"}],
    }
    raw = json.dumps(body).encode()
    try:
        st, _, b = chat(body, timeout=120)
        d = json.loads(b)
        check("bigbody: ~100KB 请求 200", st == 200 and bool(d.get("choices")),
              f"req={len(raw)}B status={st} "
              f"err={d.get('error', {}).get('message', '')[:80]}")
    except Exception as e:
        check("bigbody: ~100KB 请求 200", False, repr(e))


# ---------------------------------------------------------- 8. 查询串透传
def t_query_string():
    try:
        st, _, body = req("GET", f"{BASE}/models?limit=5")
        d = json.loads(body)
        check("query: GET /v1/models?limit=5 200", st == 200,
              f"status={st} count={len(d.get('data', []))} "
              f"err={d.get('error', {}).get('message', '')[:80] if isinstance(d.get('error'), dict) else ''}")
    except Exception as e:
        check("query: GET /v1/models?limit=5 200", False, repr(e))


# ---------------------------------------------------------- 9. 直连对照(可选)
def t_direct_control():
    """对照组: 直连上游不带会话头, 应 400 MissingSessionID (证明网关在起作用)。"""
    body = {
        "model": MODEL, "max_tokens": 4,
        "messages": [{"role": "user", "content": "hi"}],
    }
    url = "https://opencode.ai/zen/go/v1/chat/completions"
    st, b = None, b""
    for attempt in range(3):
        r = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json",
                     "User-Agent": MIMO_UA})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                st, b = resp.status, resp.read()
            break
        except urllib.error.HTTPError as e:
            st, b = e.code, e.read()
            break
        except Exception:
            # 上游 Cloudflare 偶发 SSL EOF 限流, 退避重试
            if attempt == 2:
                check("control: 直连上游无会话头 -> 400", False, "network error after 3 tries")
                return
            time.sleep(4)
    msg = b[:200].decode("utf-8", "replace")
    check("control: 直连上游无会话头 -> 400 MissingSessionID",
          st == 400 and "session" in msg.lower(), f"status={st} body={msg[:120]}")


# --------------------- 10. Python-urllib UA: 网关必须替换 (上游封 1010)
def t_blocked_ua_rewritten():
    body = {
        "model": MODEL, "max_tokens": 8,
        "messages": [{"role": "user", "content": "reply with the word pong only"}],
    }
    st, _, b = req("POST", f"{BASE}/chat/completions",
                   data=json.dumps(body).encode(),
                   headers={"Content-Type": "application/json"},
                   ua="Python-urllib/3.13")
    d = json.loads(b) if b else {}
    check("ua: Python-urllib 被网关替换后 200", st == 200 and bool(d.get("choices")),
          f"status={st} err={d.get('error', {}).get('message', '')[:80]}")


# --------------------------- 11. 无 UA (http.client 默认不发) → 应 200
def t_no_ua():
    import http.client
    body = json.dumps({
        "model": MODEL, "max_tokens": 8,
        "messages": [{"role": "user", "content": "reply with the word pong only"}],
    }).encode()
    u = urllib.parse.urlsplit(BASE)
    c = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=60)
    try:
        c.request("POST", (u.path or "") + "/chat/completions", body=body,
                  headers={"Content-Type": "application/json",
                           "Authorization": f"Bearer {KEY}"})
        resp = c.getresponse()
        d = json.loads(resp.read())
        check("ua: 完全无 UA 请求 200", resp.status == 200 and bool(d.get("choices")),
              f"status={resp.status} err={d.get('error', {}).get('message', '')[:80]}")
    except Exception as e:
        check("ua: 完全无 UA 请求 200", False, repr(e))
    finally:
        c.close()


# --------------------- 12. chunked 请求体 (Node 客户端常见形态)
def t_chunked_request():
    import http.client
    payload = json.dumps({
        "model": MODEL, "max_tokens": 8,
        "messages": [{"role": "user", "content": "reply with the word pong only"}],
    }).encode()
    u = urllib.parse.urlsplit(BASE)
    c = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=60)
    try:
        c.putrequest("POST", (u.path or "") + "/chat/completions", skip_accept_encoding=True)
        c.putheader("Content-Type", "application/json")
        c.putheader("Transfer-Encoding", "chunked")
        c.putheader("Authorization", f"Bearer {KEY}")
        c.putheader("User-Agent", MIMO_UA)
        c.endheaders()
        # 分两段发, 模拟真实 chunked
        half = len(payload) // 2
        for part in (payload[:half], payload[half:]):
            c.send(b"%x\r\n%s\r\n" % (len(part), part))
        c.send(b"0\r\n\r\n")
        resp = c.getresponse()
        d = json.loads(resp.read())
        check("chunked: 分段 chunked 请求体 200", resp.status == 200 and bool(d.get("choices")),
              f"status={resp.status} err={d.get('error', {}).get('message', '')[:80]}")
    except Exception as e:
        check("chunked: 分段 chunked 请求体 200", False, repr(e))
    finally:
        c.close()


def main():
    print(f"=== ogo-gw selftest @ {BASE} ===", flush=True)
    t_health()
    t_models()
    t_query_string()
    t_derived_session()
    t_mimo_affinity()
    t_cache_affinity()
    t_streaming()
    t_big_body()
    t_direct_control()
    t_blocked_ua_rewritten()
    t_no_ua()
    t_chunked_request()

    n_ok = sum(1 for _, ok, _ in results if ok)
    print(f"\n=== {n_ok}/{len(results)} passed ===", flush=True)
    for name, ok, detail in results:
        if not ok:
            print(f"  FAIL  {name}  -- {detail}", flush=True)
    sys.exit(0 if n_ok == len(results) else 1)


if __name__ == "__main__":
    main()
