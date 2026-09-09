"""OpenAI Chat Completions ↔ IR。

关键点：
  - messages[].role: system 上提到 L1.system；user/assistant 进 L1.messages。
  - tool_calls -> tool_use；role=tool -> tool_result。
  - usage: prompt_tokens 含缓存，cached_tokens 为子集（归一到 IRUsage）。
"""
from __future__ import annotations

import json

from ..ir import model as ir
from .base import Adapter, Dropped, parse_arguments, record_unknown

# Chat Completions 顶层字段中，本 adapter 真正消费的部分。
# 其余顶层字段一律进降级记录：命中率实验的第④项暴露依赖这份清单准确，
# 静默吞掉一个字段（如误发的 top-level system）会让埋点失真。
_KNOWN_TOP = {
    "model", "messages", "tools",
    "max_tokens", "max_completion_tokens", "temperature", "stream", "system",
}


class ChatAdapter(Adapter):
    name = "openai_chat"

    def to_ir(self, payload: dict, dropped: Dropped) -> ir.IRRequest:
        req = ir.IRRequest(model=payload.get("model", ""),
                           max_tokens=payload.get("max_completion_tokens",
                                                  payload.get("max_tokens", 1024)),
                           temperature=payload.get("temperature"),
                           stream=bool(payload.get("stream", False)))
        for m in payload.get("messages", []):
            role = m.get("role", "user")
            if role == "system":
                req.system.append(ir.Block(kind=ir.TEXT, text=m.get("content", "")))
                continue
            msg = ir.Message(role=role)
            content = m.get("content")
            if isinstance(content, str):
                msg.blocks.append(ir.Block(kind=ir.TEXT, text=content))
            elif isinstance(content, list):
                for part in content:
                    t = part.get("type")
                    if t == "text":
                        msg.blocks.append(ir.Block(kind=ir.TEXT, text=part.get("text", "")))
                    else:
                        dropped.add(f"content.{t}", "Chat 多模态内容块，方案 3.8 不做", "explicit")
            for tc in m.get("tool_calls", []) or []:
                fn = tc.get("function", {})
                raw_args = fn.get("arguments", "")
                msg.blocks.append(ir.Block(
                    kind=ir.TOOL_USE, tool_name=fn.get("name"),
                    tool_id=tc.get("id"),
                    # v1.4：参数解析成 dict 进 tool_input——不解析的话跨协议到
                    # Anthropic 会渲染成空 input {}（第三轮自查发现的参数丢失 bug）。
                    # 原始字符串留 extra，chat→chat 往返无损。
                    tool_input=parse_arguments(raw_args),
                    extra={"arguments": raw_args}))
            if role == "tool":
                msg.role = "user"
                msg.blocks.append(ir.Block(
                    kind=ir.TOOL_RESULT, tool_id=m.get("tool_call_id"),
                    text=m.get("content", "")))
            req.messages.append(msg)
        for t in payload.get("tools", []) or []:
            fn = t.get("function", t)
            req.tools.append(ir.Tool(name=fn.get("name", ""),
                                     description=fn.get("description", ""),
                                     input_schema=fn.get("parameters", {})))
        # 顶层 system 非 Chat 规范字段（Responses 用 instructions），但实践中
        # 常被误发到 Chat 路由：按语义上提到 IR.system，同时留一条降级记录，
        # 既不丢语义也不掩盖"这不是规范写法"这个事实。
        if "system" in payload and isinstance(payload["system"], str):
            req.system.append(ir.Block(kind=ir.TEXT, text=payload["system"]))
            dropped.add("system", "Chat 规范无顶层 system 字段，已按语义上提至 IR.system",
                        "explicit")
        # 其余未消费的顶层字段：显式进降级记录（方案 3.3 第④项）
        record_unknown(payload, _KNOWN_TOP, dropped, "Chat 参数暂无 IR 映射")
        return req

    def from_ir(self, req: ir.IRRequest, dropped: Dropped) -> dict:
        messages = []
        if req.system:
            messages.append({"role": "system",
                             "content": "".join(b.text or "" for b in req.system)})
        for msg in req.messages:
            text_parts, tool_calls, tool_results = [], [], []
            for b in msg.blocks:
                if b.kind == ir.TEXT:
                    text_parts.append(b.text or "")
                elif b.kind == ir.TOOL_USE:
                    tool_calls.append({"id": b.tool_id, "type": "function",
                                       "function": {"name": b.tool_name,
                                                    "arguments": _arguments_str(b)}})
                elif b.kind == ir.TOOL_RESULT:
                    tool_results.append({"role": "tool", "tool_call_id": b.tool_id,
                                         "content": b.text or ""})
            if tool_results:
                messages.extend(tool_results)
                continue
            m = {"role": msg.role, "content": "".join(text_parts)}
            if tool_calls:
                m["tool_calls"] = tool_calls
            messages.append(m)
        out = {"model": req.model, "messages": messages,
               "max_completion_tokens": req.max_tokens, "stream": req.stream}
        if req.temperature is not None:
            out["temperature"] = req.temperature
        if req.tools:
            out["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description,
                "parameters": t.input_schema}} for t in req.tools]
        return out


def _arguments_str(b) -> str:
    """Chat/Responses 侧的 arguments 是 JSON 字符串。

    优先用 extra 里的原始字符串（同协议往返字节无损）；没有则从 tool_input
    序列化——anthropic→chat 方向靠这条路把 dict 参数带过去（v1.4 修复）。
    """
    raw = b.extra.get("arguments")
    if isinstance(raw, str) and raw.strip():
        return raw
    return json.dumps(b.tool_input or {}, ensure_ascii=False)


def usage_from_chat(usage: dict) -> ir.IRUsage:
    details = usage.get("prompt_tokens_details", {}) or {}
    return ir.IRUsage(
        input_tokens=usage.get("prompt_tokens", 0),
        output_tokens=usage.get("completion_tokens", 0),
        cache_read_input_tokens=details.get("cached_tokens", 0),
    )
