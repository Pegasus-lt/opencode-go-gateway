#!/usr/bin/env python3
"""
_probe_session_form.py — 实测: 上游认不认非官方形态的 session id 当缓存键

要回答的问题
------------
gateway.py 里有条 2026-10-07 的实测记录:

    同一 447 token prompt, 裸 hex -> cached_tokens 恒为 0;
    ses_<32hex> -> cached_tokens 384

但那次测的是**网关自己派生的 id**, 不是**客户端明确指定的值**。
于是就出现分歧: 网关要不要把客户端给的非官方形态整形掉?

  - 认为要整形: 上游按格式认缓存键, 不整形 = 每个对话都按全价烧钱
  - 认为该透传: 网关是透明代理, 没资格替客户端改名; 透传的最坏情况只是
    缓存不命中, 整形的最坏情况是把客户端的身份换掉了

这个脚本用数据定这件事, 不靠推理。

实测结果(2026-10-10, deepseek-v4-flash, 7 形态 x 3 轮)
------------------------------------------------
  official ses_+32hex   0  256  256   HIT
  upper    SES_+32HEX   0  256  256   HIT
  bare     32hex        -  512  512   HIT
  bare     64hex        0  256  256   HIT
  short    'abc-123456' 0  256  256   HIT
  uuid                  0  256  256   HIT
  ses_+非hex             0  256  256   HIT

7/7 全部命中 —— 上游缓存与 session 形态无关。所以 gateway.py 不做整形,
客户端给什么就发什么。

(顺带: 本机到 opencode.ai 的 TLS 握手会间歇性超时/证书失败, 12 次里只成
3 次。所以这里复用一条已校验的连接, 21 个请求只付一次握手。证书校验
不通过时不会发 Authorization 头, 密钥不会流到没校验过的对端。)

怎么测的
--------
对每种形态:
  1. 随机生成一个该形态的 session 值, 整组固定复用
  2. 造一段**只属于该形态**的长 prompt(嵌随机 nonce), 保证它此前从没被缓存过
     -- 不共用 prompt 是关键: 否则第一个形态把缓存焐热了, 后面的形态
        就算完全不认 session 也会命中, 得出全绿的假阳性
  3. 同一个 session + 同一个 prompt 连打 N 次, 记录每次的 cached_tokens
     第 1 次冷是正常的; **第 2 次起 > 0 才叫命中**

直连上游, 不经过网关(要控制的正是这个头本身)。每次 max_tokens=1,
21 次请求的量级, 消耗极小。

用法
----
    set OGO_GW_KEY=sk-你的key          (Windows)
    export OGO_GW_KEY=sk-你的key       (macOS/Linux)

    python _probe_session_form.py
    python _probe_session_form.py --model deepseek-v4-flash --rounds 3

输出末尾会直接给结论: 该整形还是该原样透传。
"""

import argparse
import http.client
import json
import os
import random
import secrets
import ssl
import sys
import time
import urllib.parse
import uuid

# Windows 控制台是 GBK, 输出里有中文/制表符, 管道或部分终端下会
# UnicodeEncodeError 直接炸。保留本地编码, 只把无法表示的字符换掉。
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

DEFAULT_URL = "https://opencode.ai/zen/go/v1/chat/completions"
# 必须显式给 UA: 上游 Cloudflare 封 python 标准库的 Python-urllib/3.x (Error 1010)
PROBE_UA = "mimocode/1.15.0"


def make_forms():
    """每种形态一个名字 + 是否算官方形态 + 取值函数。

    值必须随机, 免得撞上以前跑过的缓存。
    official 标记很关键: 大小写变体本质上还是官方形态, 不该和裸 hex
    这种真异形混在同一组里统计, 否则结论会被搅成"部分命中"。
    """
    return [
        ("official  ses_+32hex",   True,  lambda: "ses_" + secrets.token_hex(16)),
        ("upper     SES_+32HEX",   True,  lambda: "SES_" + secrets.token_hex(16).upper()),
        ("bare      32hex",        False, lambda: secrets.token_hex(16)),
        ("bare      64hex",        False, lambda: secrets.token_hex(32)),
        ("short     'abc-123456'", False, lambda: "abc-%06d" % random.randint(0, 999999)),
        ("uuid      8-4-4-4-12",   False, lambda: str(uuid.uuid4())),
        ("ses_+31   non-hex",      False, lambda: "ses_" + "".join(
            random.choice("ghijklmnopqrstuvwxyz") for _ in range(31))),
    ]


def make_prompt(nonce: str) -> str:
    """够长(超过上游最小缓存前缀) + 嵌 nonce 保证全局唯一。"""
    base = ("You are a careful testing assistant. " * 60).strip()
    return f"{base}\n\n[probe nonce: {nonce}]\n\nReply with the single word: pong"


class Upstream:
    """复用一条经过证书校验的连接, 避免每个请求都重新 TLS 握手。

    本机实测 opencode.ai 的 TLS 握手间歇性超时/证书失败。HTTPSConnection
    在证书校验成功前不会发送 HTTP 请求头, 包括 Authorization; 不可信证书
    会在 connect() 阶段失败并被丢弃。握手成功后 21 次探测共用同一条连接。
    """
    def __init__(self, url, key, timeout, retries):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"不支持的上游 URL: {url}")
        self.scheme = u.scheme
        self.host = u.hostname
        self.port = u.port or (443 if u.scheme == "https" else 80)
        self.path = urllib.parse.urlunsplit(("", "", u.path or "/", u.query, ""))
        self.key = key
        self.timeout = timeout
        self.retries = max(1, retries)
        self.conn = None

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def _connect(self):
        if self.scheme == "https":
            conn = http.client.HTTPSConnection(
                self.host, self.port, timeout=self.timeout,
                context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(self.host, self.port,
                                               timeout=self.timeout)
        # 先独立握手+校验证书; 不通过时绝不发送含 key 的 HTTP 请求。
        conn.connect()
        self.conn = conn

    def post(self, sid, prompt, model, max_tokens):
        body = json.dumps({
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
        headers = {
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "User-Agent": PROBE_UA,
            "x-opencode-session": sid,
        }
        last = None
        for attempt in range(self.retries):
            try:
                if self.conn is None:
                    self._connect()
                self.conn.request("POST", self.path, body=body, headers=headers)
                resp = self.conn.getresponse()
                raw = resp.read()
                status = resp.status
                if status != 200:
                    try:
                        msg = json.loads(raw).get("error", {}).get("message", "")
                    except Exception:
                        msg = raw[:200].decode("utf-8", "replace")
                    self.close()
                    if status == 429 or 500 <= status < 600:
                        last = f"HTTP {status}: {msg[:120]}"
                        time.sleep(min(0.5 * (attempt + 1), 3.0))
                        continue
                    return status, None, None, msg[:120]

                data = json.loads(raw)
                usage = data.get("usage") or {}
                details = usage.get("prompt_tokens_details") or {}
                return 200, details.get("cached_tokens"), usage.get("prompt_tokens"), None
            except Exception as e:
                last = repr(e)
                self.close()
                time.sleep(min(0.5 * (attempt + 1), 3.0))
        return None, None, None, f"重试 {self.retries} 次仍失败: {last}"


def main():
    ap = argparse.ArgumentParser(description="实测上游认不认非官方形态的 session 当缓存键")
    ap.add_argument("key", nargs="?", default="",
                    help="API key(不给就读环境变量 OGO_GW_KEY)")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--model", default="deepseek-v4-flash",
                    help="挑缓存命中最好的模型(默认 deepseek-v4-flash)")
    ap.add_argument("--rounds", type=int, default=3, help="每种形态打几次(默认 3)")
    ap.add_argument("--max-tokens", type=int, default=1)
    ap.add_argument("--sleep", type=float, default=1.0, help="每轮之间歇几秒")
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--retries", type=int, default=6,
                    help="TLS/限流失败重试几次(默认 6)")
    args = ap.parse_args()

    key = args.key or os.environ.get("OGO_GW_KEY", "")
    if not key:
        print("!! 缺 API key: python _probe_session_form.py <sk-...> "
              "或 set OGO_GW_KEY=sk-...", file=sys.stderr)
        return 2

    forms = make_forms()
    n = args.rounds
    print(f"""
  session 形态缓存探测
  ────────────────────────────────────────────
  上游       {args.url}
  模型       {args.model}
  每形态     {n} 次 (max_tokens={args.max_tokens}, 间隔 {args.sleep}s)
  预计消耗   {len(forms) * n} 个极小请求
  ────────────────────────────────────────────
""")

    upstream = Upstream(args.url, key, args.timeout, args.retries)
    results = []
    for idx, (name, is_official, gen) in enumerate(forms):
        sid = gen()
        # 每种形态配一段只属于它的 prompt, 保证缓存从冷开始
        prompt = make_prompt(f"{idx}-{secrets.token_hex(8)}")
        rounds = []
        err = None
        for r in range(n):
            if r:
                time.sleep(args.sleep)
            status, cached, ptok, e = upstream.post(
                sid, prompt, args.model, args.max_tokens)
            if status != 200:
                err = f"HTTP {status}: {e}"
                break
            rounds.append((cached, ptok))

        hit = None
        if len(rounds) == n:
            # 第 1 次冷是正常的, 看第 2 次起
            later = [c for c, _ in rounds[1:]]
            hit = bool(later) and all(c is not None and c > 0 for c in later)

        results.append({"name": name, "sid": sid, "rounds": rounds,
                        "hit": hit, "err": err, "official": is_official})

        vals = "  ".join("-" if c is None else str(c) for c, _ in rounds) or "-"
        mark = "  ?" if err else ("HIT" if hit else ("MISS" if rounds else "ERR"))
        print(f"  [{mark}] {name:26s} cached_tokens: {vals:14s} "
              f"sid={sid[:20]}{'…' if len(sid) > 20 else ''}")
        if err:
            print(f"         -> {err}")

    upstream.close()

    # -------------------------------------------------------------- 结论
    print("\n" + "─" * 60)
    official_rows = [r for r in results if r["official"]]
    other_rows = [r for r in results if not r["official"]]

    if all(r["err"] for r in results):
        print("  每个请求都失败了, 先修错误再看结论(多半是 key / 模型 / 配额):")
        print(f"      {results[0]['err']}")
        return 1

    official_ok = any(r["hit"] for r in official_rows)
    other_bad = [r for r in other_rows if r["hit"] is False]
    other_ok = [r for r in other_rows if r["hit"]]

    print(f"  官方形态 ses_+32hex (含大写)  : "
          f"{'命中' if official_ok else '未命中'}")
    print(f"  真异形 (裸hex/短串/uuid/非hex): {len(other_ok)}/{len(other_rows)} 命中")
    for r in other_bad:
        print(f"      未命中 -> {r['name']}")

    print()
    if not official_ok:
        print("  注意: 官方形态本身都没命中 —— 这轮数据不可信, 换个模型重跑。")
    elif not other_bad:
        print("  结论: 非官方形态一样能命中缓存。")
        print("        -> 上游不按格式认缓存键, 网关应该【原样透传】,")
        print("           normalize_session() 属于过度设计, 建议 revert。")
    elif len(other_bad) == len(other_rows):
        print("  结论: 只有官方形态命中, 真异形一律缓存为 0。")
        print("        -> 上游确实按格式认缓存键, 网关【整形是必要的】,")
        print("           否则客户端传个裸 hex 就等于全价烧钱。")
    else:
        print("  结论: 真异形里有的命中、有的不命中 —— 看上面逐行结果。")
        print("        -> 建议网关【只整形确实不命中的那几种】, 或默认透传 +")
        print("           开关整形, 把选择权交给用户。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
