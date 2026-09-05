"""三组对照实验框架 —— dry-run 可跑，真实运行等 API 额度。

方法学（方案 3.6）：
  - 先串行预热（max_tokens:0）再放量：缓存条目在首个响应开始后才可用，
    并行打相同前缀的首批请求必然全 miss。
  - 每次跑之前把 session 配置快照写进报告头部，与命中率数据绑死。
  - 优先级：粒度、更新频率必做；位置组可降级（D2 视 Session 04 口径）。

三组实验：
  一 · 更新频率  A 不注入 / B 锁死注入 / C 每轮动态注入
  二 · 位置      D1 前缀头部 / D2 尾部 append（可降级）
  三 · 粒度      G1 300 / G2 4x512 / G3 2048 / G4 30 块（20-block 断崖）

真实运行（拿到 key 后）：python run_experiments.py --live --backend <url>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.state.session_config import load as load_session_config  # noqa: E402
from src.warmup import prewarm  # noqa: E402

GROUPS = {
    "frequency": ["A_baseline", "B_locked", "C_dynamic"],
    "position": ["D1_head", "D2_tail"],
    "granularity": ["G1_300", "G2_4x512", "G3_2048", "G4_30blocks"],
}
PRIORITY = {"frequency": "must", "granularity": "must", "position": "degradable"}


def make_memory_block(strategy: str, size: int = 512) -> str:
    """按策略生成记忆注入内容（真实实验时替换为 TRACK 01 的 L1-L3 记忆）。"""
    base = {"A_baseline": "", "B_locked": "persona: fixed",
            "C_dynamic": f"persona: varies {time.time()}"}.get(strategy, "memory")
    return (base + " " + "x" * max(0, size))[:size] if base else ""


def run_group(group: str, live: bool, backend: str, report: dict):
    cfg = load_session_config()
    report["config_snapshot"] = cfg.snapshot()  # 与命中率数据绑死
    report["groups"][group] = {"priority": PRIORITY[group], "arms": {}}
    for arm in GROUPS[group]:
        result = {"arm": arm, "status": "pending"}
        if not live:
            # dry-run：只验证编排与预热请求构造，不真实调 API
            warm = prewarm.build_warmup_request(make_memory_block(arm) or "sys", "mock")
            conflicts = prewarm.validate_warmup(warm)
            result.update(status="dry-run-ok", warmup_conflicts=conflicts,
                          note="预热请求构造合法" if not conflicts else "预热参数冲突")
        else:
            # 真实运行：先串行预热，再放量测命中率（需真实 Anthropic key）
            result.update(status="live-todo",
                          note="需真实 key；先 max_tokens:0 串行预热再放量")
        report["groups"][group]["arms"][arm] = result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="真实调 API（需 key）")
    ap.add_argument("--dry-run", action="store_true", help="只验证编排，不调 API")
    ap.add_argument("--backend", default=os.environ.get("PB_BACKEND", "http://127.0.0.1:9100"))
    ap.add_argument("--out", default="experiments/report.json")
    args = ap.parse_args()

    report = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "mode": "live" if args.live else "dry-run",
              "note": "命中率字段只有真实 API 返回；dry-run 只验证编排与预热合法性",
              "groups": {}}
    for group in ("frequency", "position", "granularity"):
        run_group(group, args.live, args.backend, report)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"report -> {args.out} (mode={report['mode']})")


if __name__ == "__main__":
    main()
