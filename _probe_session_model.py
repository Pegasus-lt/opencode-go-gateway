#!/usr/bin/env python3
"""
_probe_session_model.py — 实测: 派生 session 该不该把 model 放进 seed?

要回答的问题
------------
gateway.py 里我加了一条: 派生 session 时把 model 也放进 seed, 理由写着
"同一对话在不同模型上是两份缓存, 不该共用一个 session"。

这条只有原则, 没有依据。两种可能:

  - 缓存是 model-aware 的(按 model+prompt 寻址) -> model 已经在缓存键里,
    session 只影响路由。放不放 model 进 seed 无所谓, 那条改动是多余的。
  - 缓存不是 model-aware 的(按 session+prompt 寻址) -> 不同模型共用同一个
    session 会串答案(上游 bug), 那 model 就必须进 seed。

这个脚本用真实上游定这件事。直连上游, 不经过网关。

实验 1: 缓存是不是 model-aware?
  同一段长 prompt P, 同一个 session S1, 两个模型 A / B:
    1. (P, S1, A) 第 1 次  -> 冷, cached = 0
    2. (P, S1, A) 第 2 次  -> 命中, cached > 0
    3. (P, S1, B)          -> 0 = model-aware;  > 0 = 不是(会串答案!)
    4. (P, S2, B)          -> 0 = 对照组(换个 session 确实冷)
  第 3 步是关键: 同 prompt 同 session 换模型, 如果还能命中, 说明上游缓存
  根本不分模型 —— 那 A 的答案会被当成 B 的返回, 是严重 bug。

实验 2: 共用 session 跨模型会不会互相踩(颠簸)?
  两段长 prompt P_A / P_B, 两个模型, 交错打 5 轮:
    - 共用 S    : (P_A, S, A) 和 (P_B, S, B) 交错
    - 分开 S_A/S_B : 同样交错
  比较命中率。共用明显更差 -> 说明 session 该带 model; 一样 -> 无所谓。

用法:
    set OGO_GW_KEY=sk-你的key
    python _probe_session_model.py
    python _probe_session_model.py --model-a deepseek-v4-flash \
                                   --model-b glm-5.3-flash
"""

import argparse
import json
import os
import secrets
import sys
import time

from _probe_session_form import Upstream, make_prompt

# 同一家供应商的两个模型最有意义: 同一批用户会来回换, 最可能共用缓存路径
DEFAULT_A = "deepseek-v4-flash"
DEFAULT_B = "deepseek-v4-pro"


def main():
    ap = argparse.ArgumentParser(description="实测派生 session 该不该带 model")
    ap.add_argument("key", nargs="?", default="",
                    help="API key(不给就读环境变量 OGO_GW_KEY)")
    ap.add_argument("--url", default="https://opencode.ai/zen/go/v1/chat/completions")
    ap.add_argument("--model-a", default=DEFAULT_A)
    ap.add_argument("--model-b", default=DEFAULT_B)
    ap.add_argument("--rounds", type=int, default=5, help="实验 2 每种配置打几轮")
    ap.add_argument("--max-tokens", type=int, default=1)
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--retries", type=int, default=12)
    args = ap.parse_args()

    key = args.key or os.environ.get("OGO_GW_KEY", "")
    if not key:
        print("!! 缺 API key: python _probe_session_model.py <sk-...> "
              "或 set OGO_GW_KEY=sk-...", file=sys.stderr)
        return 2

    up = Upstream(args.url, key, args.timeout, args.retries)
    print(f"""
  session 该不该带 model —— 探测
  ────────────────────────────────────────────
  上游       {args.url}
  模型 A     {args.model_a}
  模型 B     {args.model_b}
  ────────────────────────────────────────────
""")

    # ---------------------------------------------------------- 实验 1
    print("  实验 1: 缓存是不是 model-aware?")
    print("  同一段 prompt, 同一个 session, 换模型\n")
    p = make_prompt(f"exp1-{secrets.token_hex(8)}")
    s1 = "ses_" + secrets.token_hex(16)
    s2 = "ses_" + secrets.token_hex(16)

    r1 = up.post(s1, p, args.model_a, args.max_tokens)
    time.sleep(args.sleep)
    r2 = up.post(s1, p, args.model_a, args.max_tokens)
    time.sleep(args.sleep)
    r3 = up.post(s1, p, args.model_b, args.max_tokens)      # 关键
    time.sleep(args.sleep)
    r4 = up.post(s2, p, args.model_b, args.max_tokens)      # 对照

    rows = [
        ("1. (P, S1, A) 第1次  冷",        r1),
        ("2. (P, S1, A) 第2次  应命中",     r2),
        ("3. (P, S1, B)  同prompt同session", r3),
        ("4. (P, S2, B)  对照组 应冷",      r4),
    ]
    for label, (status, cached, ptok, err) in rows:
        if status != 200:
            print(f"    {label:34s} HTTP {status}: {err}")
        else:
            print(f"    {label:34s} cached_tokens={cached}  (prompt_tokens={ptok})")

    # ---------------------------------------------------------- 实验 2
    # 两组必须用完全不同的 prompt: 上游缓存跨请求持续, 如果共用/分开两组
    # 复用同一批 prompt, 先跑的那组会把缓存预热, 后跑的那组"继承"热度,
    # 比较就失去意义(假上游上实测踩过这个坑)。
    print(f"\n  实验 2: 共用 session 跨模型会不会互相踩? 各 {args.rounds} 轮交错")
    print("  (两组用不同 prompt, 保证冷启动一致)\n")
    p_a_sh = make_prompt(f"exp2a-{secrets.token_hex(8)}")
    p_b_sh = make_prompt(f"exp2b-{secrets.token_hex(8)}")
    p_a_sp = make_prompt(f"exp2c-{secrets.token_hex(8)}")
    p_b_sp = make_prompt(f"exp2d-{secrets.token_hex(8)}")
    shared = "ses_" + secrets.token_hex(16)
    sep_a = "ses_" + secrets.token_hex(16)
    sep_b = "ses_" + secrets.token_hex(16)

    def run_config(pa, pb, sa, sb, tag):
        """交错打 rounds 轮, 返回 (a_hits, b_hits)。"""
        a_hits = b_hits = 0
        a_rounds, b_rounds = [], []
        for i in range(args.rounds):
            if i:
                time.sleep(args.sleep)
            st_a, c_a, _, e_a = up.post(sa, pa, args.model_a, args.max_tokens)
            if i:
                time.sleep(args.sleep)
            st_b, c_b, _, e_b = up.post(sb, pb, args.model_b, args.max_tokens)
            if st_a == 200:
                a_rounds.append(c_a)
                a_hits += 1 if (c_a or 0) > 0 else 0
            else:
                a_rounds.append(f"ERR:{e_a}")
            if st_b == 200:
                b_rounds.append(c_b)
                b_hits += 1 if (c_b or 0) > 0 else 0
            else:
                b_rounds.append(f"ERR:{e_b}")
        print(f"    [{tag}]")
        print(f"      A {args.model_a:20s} {a_hits}/{args.rounds} 命中  {a_rounds}")
        print(f"      B {args.model_b:20s} {b_hits}/{args.rounds} 命中  {b_rounds}")
        return a_hits, b_hits

    sh_a, sh_b = run_config(p_a_sh, p_b_sh, shared, shared, "共用 session")
    sp_a, sp_b = run_config(p_a_sp, p_b_sp, sep_a, sep_b, "分开 session")

    up.close()

    # ---------------------------------------------------------- 结论
    print("\n" + "─" * 60)

    # 实验 1 判定
    c1 = r1[1]
    c2 = r2[1]
    c3 = r3[1]
    c4 = r4[1]
    model_aware = None
    if all(c is not None for c in (c1, c2, c3, c4)):
        if c1 == 0 and c2 and c3 == 0 and c4 == 0:
            model_aware = True
        elif c3:
            model_aware = False

    if model_aware is None:
        print("  实验 1 数据不完整(有请求失败), 先修错误。")
    elif model_aware:
        print("  实验 1: 缓存是 model-aware 的 —— 同 prompt 同 session 换模型是冷的。")
        print("           -> model 已经在缓存键里, session 只影响路由。")
    else:
        print("  实验 1: 缓存【不是】model-aware 的 —— 同 prompt 同 session 换模型还能命中!")
        print("           -> 上游会把 A 的答案当 B 的返回, 这是上游的严重 bug。")
        print("           -> 这种情况下 model 必须进 seed, 否则网关派生 session 会串答案。")

    # 实验 2 判定
    def rate(hits):
        return hits / args.rounds if args.rounds else 0

    sh_rate = (rate(sh_a) + rate(sh_b)) / 2
    sp_rate = (rate(sp_a) + rate(sp_b)) / 2

    print(f"\n  实验 2: 共用 session 平均命中率 {sh_rate:.0%}, "
          f"分开 session {sp_rate:.0%}")
    if model_aware is False:
        print("\n  结论: 上游缓存不分模型, 且共用 session 没造成额外颠簸。")
        print("        -> model 必须进 seed(否则串答案), 这条改动是【必要的】。")
    elif sh_rate >= sp_rate - 0.1:
        print("\n  结论: 缓存 model-aware, 且共用 session 跨模型没有变差。")
        print("        -> model 放不放进 seed 都无所谓(路由层面的事),")
        print("           那条改动【无害但多余】, 可以 revert 也可以留着。")
    else:
        print("\n  结论: 共用 session 跨模型确实更差 —— session 该带 model。")
        print("        -> 那条改动是【有依据的】。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
