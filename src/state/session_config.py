"""Session 边界配置（可切换）—— 读 config/session.json，驱动重放策略。

落地纪律（对应执行方案 3.9.1）：
  - key_granularity：启动时读配置，运行中途切换会致已存会话键值错乱。
  - replay_from / end_policy：可热切换（只在读历史时生效）。
  - 实验脚本每次跑之前，把本配置完整快照写进报告头部，与命中率数据绑死。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

# key_granularity
SINGLE_TASK = "single_task"
THREE_LEVEL = "three_level"
# replay_from
FULL = "full"
LAST_BREAKPOINT = "last_breakpoint"
SLIDING_WINDOW = "sliding_window"
# end_policy
TTL = "ttl"
EXPLICIT_CLOSE = "explicit_close"


@dataclass
class SessionConfig:
    key_granularity: str = SINGLE_TASK
    key_fields: list = field(default_factory=lambda: ["task-id"])
    replay_from: str = FULL
    sliding_window_n: int = 20
    end_policy: str = TTL
    ttl_seconds: int = 1800
    on_end: str = "archive"
    # 记忆注入上限（TRACK 04 第三参数）：0=不限制；会话 meta.memory_cap 可覆盖
    session_memory_cap: int = 0

    def session_key(self, headers: dict) -> str:
        """由 key_fields 从请求头拼状态键。"""
        parts = [str(headers.get(k, "")) for k in self.key_fields]
        return "|".join(parts)

    def snapshot(self) -> dict:
        """实验报告头部要绑定的配置快照。"""
        return {
            "key_granularity": self.key_granularity,
            "key_fields": list(self.key_fields),
            "replay_from": self.replay_from,
            "sliding_window_n": self.sliding_window_n,
            "end_policy": self.end_policy,
            "ttl_seconds": self.ttl_seconds,
            "on_end": self.on_end,
            "session_memory_cap": self.session_memory_cap,
        }


_DEFAULT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "config", "session.json"
)


def load(path: str | None = None) -> SessionConfig:
    """从 JSON 加载；文件缺失或缺字段时回落到默认假设值。"""
    p = path or _DEFAULT_PATH
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f).get("session", {})
    except (FileNotFoundError, json.JSONDecodeError):
        raw = {}
    cfg = SessionConfig()
    for k, v in raw.items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)
    return cfg


def build_prefix(history: list, cursor: int, cfg: SessionConfig) -> list:
    """重放起点策略接口：配置决定用哪个实现。

    history: 该 session 已存的完整历史（list[Message]）
    cursor:  last_breakpoint 的游标
    返回：本轮要拼进前缀的消息
    """
    if cfg.replay_from == FULL:
        return list(history)
    if cfg.replay_from == LAST_BREAKPOINT:
        return list(history[cursor:])
    # sliding_window
    # n<=0 必须返回空窗口。直接写 history[-n:] 在 n==0 时等价于 history[0:]，
    # 会静默退化成 full —— 实验组间差异就此消失且极难察觉。
    n = cfg.sliding_window_n
    return list(history[-n:]) if n > 0 else []
