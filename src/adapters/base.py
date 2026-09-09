"""adapter 基类与降级路径记录。

每个协议实现两个方向：to_ir / from_ir。
不可映射的字段/参数必须进降级路径，绝不静默丢弃（方案 3.3 第④⑤项暴露）。
"""
from __future__ import annotations

import json
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


def parse_arguments(raw: Any) -> dict:
    """OpenAI 系的工具参数是 JSON **字符串**，Anthropic 是 dict。

    跨协议转换必须解析成 dict 进 IR.tool_input，否则到 Anthropic 侧会渲染成
    空 input {}（第三轮自查发现的参数丢失 bug）。解析失败返回 {}，原始字符串
    由调用方留在 extra["arguments"] 兜底（同协议往返无损）。
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def assistant_from_upstream(target: str, resp: dict):
    """上游响应 → IR assistant 消息（状态层重放的原料）。

    v1.4 起保留**结构化块**：thinking（含 signature / redacted data）与
    tool_use（含解析后的 tool_input）必须进历史——Anthropic 要求下一轮原样
    回传 thinking + signature，缺失直接 400；tool_use 丢失则多轮工具链断链。
    返回 None 表示本轮没有任何可重放内容（如空响应）。
    延迟 import ir，避免与 adapter 模块产生循环依赖。
    """
    from ..ir import model as ir

    blocks: list = []
    if target == "anthropic":
        for b in resp.get("content", []) or []:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                blocks.append(ir.Block(kind=ir.TEXT, text=b.get("text", "")))
            elif t == "thinking":
                blocks.append(ir.Block(kind=ir.THINKING, text=b.get("thinking", ""),
                                       extra={"signature": b.get("signature", "")}))
            elif t == "redacted_thinking":
                blocks.append(ir.Block(kind=ir.THINKING, text="",
                                       extra={"redacted": True,
                                              "data": b.get("data", "")}))
            elif t == "tool_use":
                blocks.append(ir.Block(kind=ir.TOOL_USE, tool_name=b.get("name"),
                                       tool_id=b.get("id"),
                                       tool_input=b.get("input", {})))
    elif target == "openai_response":
        for item in resp.get("output", []) or []:
            if not isinstance(item, dict):
                continue
            t = item.get("type")
            if t == "message":
                for c in item.get("content", []) or []:
                    if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                        blocks.append(ir.Block(kind=ir.TEXT, text=c.get("text", "")))
            elif t == "function_call":
                raw = item.get("arguments", "")
                blocks.append(ir.Block(kind=ir.TOOL_USE, tool_name=item.get("name"),
                                       tool_id=item.get("call_id"),
                                       tool_input=parse_arguments(raw),
                                       extra={"arguments": raw}))
            elif t == "reasoning":
                summary = item.get("summary")
                text = summary if isinstance(summary, str) else ""
                blocks.append(ir.Block(kind=ir.THINKING, text=text,
                                       extra={"encrypted_content":
                                              item.get("encrypted_content", "")}))
    else:  # openai_chat
        try:
            m = resp["choices"][0]["message"]
        except (KeyError, IndexError, TypeError, AttributeError):
            m = {}
        if m.get("content"):
            blocks.append(ir.Block(kind=ir.TEXT, text=m["content"]))
        for tc in m.get("tool_calls", []) or []:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            raw = fn.get("arguments", "")
            blocks.append(ir.Block(kind=ir.TOOL_USE, tool_name=fn.get("name"),
                                   tool_id=tc.get("id"),
                                   tool_input=parse_arguments(raw),
                                   extra={"arguments": raw}))
    # 纯空文本（如异常响应）不进历史
    blocks = [b for b in blocks
              if b.kind != ir.TEXT or (b.text or "").strip()]
    if not blocks:
        return None
    return ir.Message(role="assistant", blocks=blocks)
