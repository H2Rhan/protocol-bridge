"""adapter 基类与降级路径记录。

每个协议实现两个方向：to_ir / from_ir。
不可映射的字段/参数必须进降级路径，绝不静默丢弃（方案 3.3 第④⑤项暴露）。
"""
from __future__ import annotations

from typing import Any


class Dropped:
    """被丢弃/降级的参数清单（配合告警）。"""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, field: str, reason: str, path: str = "explicit") -> None:
        # path: "explicit" 显式降级 / "silent" 静默通过（应尽量避免）
        self.items.append({"field": field, "reason": reason, "path": path})

    def __bool__(self) -> bool:
        return bool(self.items)


def record_unknown(payload: dict, known: set, dropped: Dropped,
                   reason: str = "该协议参数暂无 IR 映射") -> None:
    """把 adapter 未消费的顶层字段显式记进降级路径。

    三个 adapter 的 to_ir 末尾都必须调它。漏调 = 字段被静默丢弃 —— 而
    「被丢弃的参数清单」是命中率实验第④项暴露，静默丢弃会让这份清单不可信，
    也会让「三方互转无信息损失」的结论站不住。
    """
    for k in payload:
        if k not in known:
            dropped.add(k, reason, "explicit")


class Adapter:
    name = "base"

    def to_ir(self, payload: dict, dropped: Dropped):
        raise NotImplementedError

    def from_ir(self, ir, dropped: Dropped) -> dict:
        raise NotImplementedError
