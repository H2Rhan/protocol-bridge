"""命中率埋点 —— 5 项必须暴露（方案 3.3），第一天就搭好而不是最后补。

没有这 5 个数字，「记忆注入对 KV Cache 的影响」无法给出可信结论。
真实命中率字段（cache_creation/cache_read）只有真实 API 才返回；离线阶段
框架先就绪，字段在 mock/录制样例中以 0 或样例值占位。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field, asdict

from ..ir.model import IRUsage

NORMAL = "normal"
WARMUP = "warmup"


@dataclass
class TurnMetrics:
    """单轮必须暴露的 5 项。"""
    ts: float = field(default_factory=time.time)
    # 轮次类型：预热轮（max_tokens:0）恒 cache_read=0，必须与正常轮分开，
    # 否则会稀释北极星分母（修复：预热轮不进 hit_rate 统计）。
    kind: str = NORMAL
    # ① 每轮缓存命中率（北极星）
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    input_tokens: int = 0
    # ② 注入 token 数（记忆注入成本）
    injected_tokens: int = 0
    # ③ 重放 token 数（无状态重放代价）
    replayed_tokens: int = 0
    # ④ 被丢弃的参数清单
    dropped_params: list = field(default_factory=list)
    # ⑤ 走了哪条降级路径（explicit / silent）
    degradation_path: str = "none"

    @property
    def cache_hit(self) -> bool:
        return self.cache_read_input_tokens > 0

    @property
    def cache_used(self) -> bool:
        """creation 与 read 同时为 0 = 完全没用上缓存（多半没够最小阈值）。"""
        return not (self.cache_creation_input_tokens == 0 and self.cache_read_input_tokens == 0)


class MetricsLog:
    """追加写 JSONL，供实验脚本汇总命中率。"""

    def __init__(self, path: str):
        self.path = path
        # 网关是 ThreadingHTTPServer，并发上限 PB_CONCURRENCY 默认是 2；
        # 无锁追加 JSONL 在 Windows 上可能写坏行，导致整份实验数据解析失败。
        self._lock = threading.Lock()

    def record(self, m: TurnMetrics) -> None:
        line = json.dumps(asdict(m), ensure_ascii=False) + "\n"
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)

    def record_turn(self, usage: IRUsage, injected: int, replayed: int,
                    dropped: list, degradation: str,
                    kind: str = NORMAL) -> TurnMetrics:
        m = TurnMetrics(
            kind=kind,
            cache_creation_input_tokens=usage.cache_creation_input_tokens,
            cache_read_input_tokens=usage.cache_read_input_tokens,
            input_tokens=usage.input_tokens,
            injected_tokens=injected,
            replayed_tokens=replayed,
            dropped_params=list(dropped),
            degradation_path=degradation,
        )
        self.record(m)
        return m


def hit_rate(metrics: list[TurnMetrics], include_warmup: bool = False) -> float:
    """cache_read 命中率 = 命中轮数 / 总轮数（北极星）。

    默认剔除预热轮：预热（max_tokens:0）的目的是写缓存，其 cache_read 恒为 0，
    把它算进分母会系统性拉低命中率，且拉低幅度取决于预热轮占比，
    让不同批次之间的命中率不可比。
    """
    ms = metrics if include_warmup else [m for m in metrics if m.kind != WARMUP]
    if not ms:
        return 0.0
    return sum(1 for m in ms if m.cache_hit) / len(ms)
