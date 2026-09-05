"""OpenAI Responses ↔ IR。

关键点：
  - input 为结构化条目列表（message / function_call / function_call_output / reasoning）。
  - previous_response_id 是有状态字段，由网关/状态层在调用前解析、在响应后登记；
    adapter 只负责把它透传到 extra，真正重放在状态层完成。
  - usage: input_tokens + input_tokens_details.cached_tokens。
"""
from __future__ import annotations

from ..ir import model as ir
from .base import Adapter, Dropped, record_unknown

# to_ir 真正消费的顶层字段；其余一律进降级记录（见 base.record_unknown）
_KNOWN_TOP = {"model", "input", "instructions", "tools", "max_output_tokens",
              "temperature", "stream", "previous_response_id"}


class ResponseAdapter(Adapter):
    name = "openai_response"

    def to_ir(self, payload: dict, dropped: Dropped) -> ir.IRRequest:
        req = ir.IRRequest(model=payload.get("model", ""),
                           max_tokens=payload.get("max_output_tokens", 1024),
                           temperature=payload.get("temperature"),
                           stream=bool(payload.get("stream", False)))
        if payload.get("instructions"):
            req.system.append(ir.Block(kind=ir.TEXT, text=payload["instructions"]))
        for item in payload.get("input", []) or []:
            t = item.get("type")
            if t == "message":
                msg = ir.Message(role=item.get("role", "user"))
                content = item.get("content")
                if isinstance(content, str):
                    msg.blocks.append(ir.Block(kind=ir.TEXT, text=content))
                elif isinstance(content, list):
                    for part in content:
                        if part.get("type") in ("input_text", "output_text", "text"):
                            msg.blocks.append(ir.Block(kind=ir.TEXT, text=part.get("text", "")))
                        else:
                            dropped.add(f"input.{part.get('type')}", "Responses 内容块，方案 3.8 不做", "explicit")
                req.messages.append(msg)
            elif t == "function_call":
                req.messages.append(ir.Message(role="assistant", blocks=[ir.Block(
                    kind=ir.TOOL_USE, tool_name=item.get("name"),
                    tool_id=item.get("call_id"),
                    extra={"arguments": item.get("arguments", "")})]))
            elif t == "function_call_output":
                req.messages.append(ir.Message(role="user", blocks=[ir.Block(
                    kind=ir.TOOL_RESULT, tool_id=item.get("call_id"),
                    text=item.get("output", ""))]))
            elif t == "reasoning":
                req.messages.append(ir.Message(role="assistant", blocks=[ir.Block(
                    kind=ir.THINKING, text=item.get("summary", "") if isinstance(item.get("summary"), str) else "")]))
            else:
                dropped.add(f"input.{t}", "Responses 条目类型暂无映射", "explicit")
        for t in payload.get("tools", []) or []:
            req.tools.append(ir.Tool(name=t.get("name", ""),
                                     description=t.get("description", ""),
                                     input_schema=t.get("parameters", {})))
        # 有状态字段：交给状态层处理，这里透传
        if "previous_response_id" in payload:
            req.extra["previous_response_id"] = payload["previous_response_id"]
        record_unknown(payload, _KNOWN_TOP, dropped, "Responses 参数暂无 IR 映射")
        return req

    def from_ir(self, req: ir.IRRequest, dropped: Dropped) -> dict:
        items = []
        for msg in req.messages:
            for b in msg.blocks:
                if b.kind == ir.TEXT:
                    items.append({"type": "message", "role": msg.role,
                                  "content": [{"type": "input_text" if msg.role == "user" else "output_text",
                                               "text": b.text or ""}]})
                elif b.kind == ir.TOOL_USE:
                    items.append({"type": "function_call", "name": b.tool_name,
                                  "call_id": b.tool_id,
                                  "arguments": b.extra.get("arguments", "{}")})
                elif b.kind == ir.TOOL_RESULT:
                    items.append({"type": "function_call_output",
                                  "call_id": b.tool_id, "output": b.text or ""})
        out = {"model": req.model, "input": items,
               "max_output_tokens": req.max_tokens, "stream": req.stream}
        if req.system:
            out["instructions"] = "".join(b.text or "" for b in req.system)
        if req.temperature is not None:
            out["temperature"] = req.temperature
        if req.tools:
            out["tools"] = [{"type": "function", "name": t.name,
                             "description": t.description,
                             "parameters": t.input_schema} for t in req.tools]
        # 不再向上游回传 previous_response_id：历史已由本网关状态层重放进 input，
        # 再传一次会让上游（真 OpenAI）二次拼接历史 → 上下文重复且 token 翻倍；
        # 若传的是网关自己生成的 resp_xxx，上游根本不认识 → 直接 404。
        # 给客户端的 response_id 由网关在响应里注入（见 gateway/server.py）。
        return out


def usage_from_response(usage: dict) -> ir.IRUsage:
    details = usage.get("input_tokens_details", {}) or {}
    return ir.IRUsage(
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_read_input_tokens=details.get("cached_tokens", 0),
    )
