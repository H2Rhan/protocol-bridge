"""Anthropic Messages ↔ IR。

关键点：
  - system 独立成字段（list[Block]），渲染顺序 tools → system → messages。
  - cache_control 断点：只在 L2 断点布局指定的块上打（3 固定 + 1 滚动，用满 4 个上限）。
  - usage: input_tokens 不含缓存读写；cache_creation/cache_read 单列。
"""
from __future__ import annotations

from ..ir import model as ir
from .base import Adapter, Dropped, record_unknown

_BP = {"type": "ephemeral"}

# to_ir 真正消费的顶层字段；其余一律进降级记录（见 base.record_unknown）
_KNOWN_TOP = {"model", "max_tokens", "temperature", "stream",
              "system", "messages", "tools", "thinking", "tool_choice"}


class AnthropicAdapter(Adapter):
    name = "anthropic"

    @staticmethod
    def _render_block(b: ir.Block, dropped: Dropped) -> dict | None:
        """IR 块 → Anthropic content 块。无法映射的一律进降级记录，绝不静默丢弃。"""
        if b.kind == ir.TEXT:
            return {"type": "text", "text": b.text or ""}
        if b.kind == ir.TOOL_USE:
            return {"type": "tool_use", "id": b.tool_id, "name": b.tool_name,
                    "input": b.tool_input or {}}
        if b.kind == ir.TOOL_RESULT:
            return {"type": "tool_result", "tool_use_id": b.tool_id,
                    "content": b.text or ""}
        if b.kind == ir.THINKING:
            return {"type": "thinking", "thinking": b.text or ""}
        dropped.add(f"block.{b.kind}", "IR 块无 Anthropic 对应类型（方案 3.8 不做）",
                    "explicit")
        return None

    def to_ir(self, payload: dict, dropped: Dropped) -> ir.IRRequest:
        req = ir.IRRequest(model=payload.get("model", ""),
                           max_tokens=payload.get("max_tokens", 1024),
                           temperature=payload.get("temperature"),
                           stream=bool(payload.get("stream", False)))
        system = payload.get("system")
        if isinstance(system, str):
            req.system.append(ir.Block(kind=ir.TEXT, text=system))
        elif isinstance(system, list):
            for b in system:
                req.system.append(ir.Block(
                    kind=ir.TEXT, text=b.get("text", ""),
                    cache_breakpoint="cache_control" in b))
        for m in payload.get("messages", []):
            msg = ir.Message(role=m.get("role", "user"))
            content = m.get("content")
            if isinstance(content, str):
                msg.blocks.append(ir.Block(kind=ir.TEXT, text=content))
            elif isinstance(content, list):
                for b in content:
                    t = b.get("type")
                    bp = "cache_control" in b
                    if t == "text":
                        msg.blocks.append(ir.Block(kind=ir.TEXT, text=b.get("text", ""), cache_breakpoint=bp))
                    elif t == "tool_use":
                        msg.blocks.append(ir.Block(kind=ir.TOOL_USE, tool_name=b.get("name"),
                                                   tool_id=b.get("id"), tool_input=b.get("input", {}),
                                                   cache_breakpoint=bp))
                    elif t == "tool_result":
                        msg.blocks.append(ir.Block(kind=ir.TOOL_RESULT, tool_id=b.get("tool_use_id"),
                                                   text=_result_text(b), cache_breakpoint=bp))
                    elif t == "thinking":
                        msg.blocks.append(ir.Block(kind=ir.THINKING, text=b.get("thinking", "")))
                    else:
                        dropped.add(f"content.{t}", "Anthropic 内容块，方案 3.8 不做", "explicit")
            req.messages.append(msg)
        for t in payload.get("tools", []) or []:
            req.tools.append(ir.Tool(name=t.get("name", ""),
                                     description=t.get("description", ""),
                                     input_schema=t.get("input_schema", {}),
                                     cache_breakpoint="cache_control" in t))
        # IR v0 没有 extended thinking / tool_choice 的字段，但不能静默丢弃：
        # 存进 extra，from_ir 原样渲染回去 —— 既保证 anthropic→anthropic 无损，
        # 也让 validate_warmup 的 thinking 冲突检查真正可达。
        for k in ("thinking", "tool_choice"):
            if payload.get(k) is not None:
                req.extra[k] = payload[k]
        record_unknown(payload, _KNOWN_TOP, dropped, "Anthropic 参数暂无 IR 映射")
        return req

    def from_ir(self, req: ir.IRRequest, dropped: Dropped,
                ctx: "object | None" = None) -> dict:
        """ctx 为 SessionContext 时按断点布局打 cache_control；否则不打。"""
        tools = []
        for i, t in enumerate(req.tools):
            td = {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            if ctx is not None and getattr(ctx, "bp_after_tools", False) and i == len(req.tools) - 1:
                td["cache_control"] = dict(_BP)
            tools.append(td)

        system = []
        for i, b in enumerate(req.system):
            sb = {"type": "text", "text": b.text or ""}
            if ctx is not None and getattr(ctx, "bp_after_system", False) and i == len(req.system) - 1:
                sb["cache_control"] = dict(_BP)
            system.append(sb)

        messages = []
        n = len(req.messages)
        # 历史静态段末尾：重放进来的历史最后一条（ctx.history 即本轮重放切片）
        hist_end = len(getattr(ctx, "history", None) or []) - 1
        for i, msg in enumerate(req.messages):
            blocks = []
            for b in msg.blocks:
                bd = self._render_block(b, dropped)
                if bd is not None:
                    blocks.append(bd)
            # 固定断点③：历史静态段末尾（若与尾部断点重合，自然只落一个）
            if (ctx is not None and getattr(ctx, "bp_after_history_static", False)
                    and 0 <= hist_end == i and blocks):
                blocks[-1]["cache_control"] = dict(_BP)
            # 滚动尾部断点：打在最后一条消息的最后一个块
            if (ctx is not None and getattr(ctx, "bp_rolling_tail", False)
                    and i == n - 1 and blocks):
                blocks[-1]["cache_control"] = dict(_BP)
            messages.append({"role": msg.role, "content": blocks or [{"type": "text", "text": ""}]})

        out = {"model": req.model, "messages": messages, "max_tokens": req.max_tokens,
               "stream": req.stream}
        if system:
            out["system"] = system
        if tools:
            out["tools"] = tools
        if req.temperature is not None:
            out["temperature"] = req.temperature
        # to_ir 存进 extra 的 Anthropic 专属参数，原样渲染回去（anthropic→anthropic 无损）
        for k in ("thinking", "tool_choice"):
            if req.extra.get(k) is not None:
                out[k] = req.extra[k]
        return out


def _result_text(b: dict) -> str:
    c = b.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(p.get("text", "") for p in c if isinstance(p, dict))
    return ""


def usage_from_anthropic(usage: dict) -> ir.IRUsage:
    return ir.IRUsage(
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
        cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
    )
