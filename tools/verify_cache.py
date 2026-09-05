"""验站脚本：验证 Anthropic 端点是否真正透传 prompt caching。

两步验证（对应方案 G1 预热链路）：
  1) 带唯一随机前缀 + cache_control 预热（max_tokens:0，被拒则降级 max_tokens:1）
     -> 期望 usage.cache_creation_input_tokens > 0 且 cache_read_input_tokens == 0
  2) 立刻原样重发同一前缀
     -> 期望 usage.cache_read_input_tokens > 0

判定：
  PASS    写读都正常 —— 可用于链路调试
  PARTIAL 有写无读 —— 缓存可能被转格式吞掉一半，只能慎用
  FAIL    写都没有 —— cache_control 被剥，不能用于任何缓存实验

用法：
  set PB_API_KEY=xxx
  python tools/verify_cache.py --base https://<端点域名> --model claude-sonnet-5
  # 离线自测（先起 tools/mock_backend.py）：
  python tools/verify_cache.py --base http://127.0.0.1:9100 --model mock
"""
from __future__ import annotations

import argparse
import json
import os
import random
import string
import sys
import time
import urllib.error
import urllib.request

WORDS = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo "
         "lima mike november oscar papa quebec romeo sierra tango uniform "
         "victor whiskey xray yankee zulu").split()


def build_prefix(n_words: int, run_id: str) -> str:
    """约 n_words 个英文词（≈n_words+ tokens），开头嵌入唯一 run_id。

    唯一性保证：共享账号下别人不会撞上同一前缀（防污染），
    也不会命中本站历史残留缓存（防假阳性）。
    """
    rng = random.Random(run_id)
    body = " ".join(rng.choice(WORDS) for _ in range(n_words))
    return f"[run:{run_id}] cache-passthrough probe. {body}"


def post(base: str, key: str, payload: dict, use_proxy: bool) -> tuple[int, dict]:
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": os.environ.get("PB_ANTHROPIC_VERSION", "2023-06-01"),
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
    }
    if key:
        headers["x-api-key"] = key
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(base.rstrip("/") + "/v1/messages",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers=headers)
    opener = (urllib.request.build_opener() if use_proxy else
              urllib.request.build_opener(urllib.request.ProxyHandler({})))
    try:
        with opener.open(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def make_payload(model: str, prefix: str, max_tokens: int) -> dict:
    return {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prefix,
                 "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": "Reply with OK."},
            ],
        }],
    }


def usage_of(resp: dict) -> dict:
    u = resp.get("usage", {}) or {}
    return {k: u.get(k, 0) for k in
            ("input_tokens", "output_tokens",
             "cache_creation_input_tokens", "cache_read_input_tokens")}


def main() -> int:
    ap = argparse.ArgumentParser(description="验证端点缓存透传")
    ap.add_argument("--base", required=True, help="Anthropic 端点根地址，如 https://xxx.com")
    ap.add_argument("--model", required=True, help="模型名，如 claude-sonnet-5")
    ap.add_argument("--key", default=os.environ.get("PB_API_KEY", ""),
                    help="默认读环境变量 PB_API_KEY")
    ap.add_argument("--words", type=int, default=4500,
                    help="前缀词数（须 ≥ 模型最小可缓存长度；Opus 4.5/4.6 要 4096+）")
    ap.add_argument("--use-proxy", action="store_true", help="走系统代理（默认直连）")
    args = ap.parse_args()

    run_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    prefix = build_prefix(args.words, run_id)
    print(f"run_id={run_id}  前缀≈{args.words} words  base={args.base}  model={args.model}")

    # ---- 第 1 步：预热（期望 creation>0, read==0）----
    mt = 0
    code, resp = post(args.base, args.key, make_payload(args.model, prefix, mt),
                      args.use_proxy)
    if code == 400:
        print("max_tokens:0 被拒（400），降级 max_tokens:1 重试预热…")
        mt = 1
        code, resp = post(args.base, args.key, make_payload(args.model, prefix, mt),
                          args.use_proxy)
    if code != 200:
        print(f"FAIL: 预热 HTTP {code}: {json.dumps(resp, ensure_ascii=False)[:300]}")
        return 2
    u1 = usage_of(resp)
    print(f"[1] 预热(max_tokens={mt}) usage: {json.dumps(u1)}")

    # ---- 第 2 步：原样重发（期望 read>0）----
    time.sleep(2)
    code, resp = post(args.base, args.key, make_payload(args.model, prefix, mt),
                      args.use_proxy)
    if code != 200:
        print(f"FAIL: 重发 HTTP {code}: {json.dumps(resp, ensure_ascii=False)[:300]}")
        return 2
    u2 = usage_of(resp)
    print(f"[2] 重发 usage: {json.dumps(u2)}")

    created = u1["cache_creation_input_tokens"] > 0
    hit = u2["cache_read_input_tokens"] > 0
    print("-" * 60)
    if created and hit:
        print(f"PASS ✅ 写 {u1['cache_creation_input_tokens']} / 读 "
              f"{u2['cache_read_input_tokens']} tokens —— 透传正常，可用于链路调试")
        if u1["cache_read_input_tokens"] > 0:
            print("注意：随机新前缀首call即 read>0，站点可能在伪造缓存字段，数据存疑")
        return 0
    if created and not hit:
        print("PARTIAL ⚠️ 有写无读 —— 缓存可能被转格式吞掉一半，慎用")
        return 1
    print("FAIL ❌ cache_control 疑似被剥离（无 creation）—— 不能用于缓存实验")
    return 2


if __name__ == "__main__":
    sys.exit(main())
