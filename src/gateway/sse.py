"""SSE 逐块流式转换（LIMITATIONS #1 最小闭环：openai_chat 客户端 ← Anthropic 上游）。

只有 source=openai_chat / target=anthropic 且 stream:true 的请求走这里，
其余方向保持整读透传（见 docs/LIMITATIONS.md #1 的范围说明）。

转换语义：
- Anthropic 事件按行增量解析（空行分帧），text_delta 立即翻成 Chat chunk 下发——
  首 token 延迟与直连上游一致，这是本模块存在的意义；
- 工具参数（input_json_delta）是非完整 JSON 片段，缓冲到 content_block_stop
  一次性发出——长工具调用退化为非流式，是无法回避的折损（LIMITATIONS 已声明）；
- thinking_delta / signature_delta 在 Chat 协议里无对应概念，不落帧（客户端
  只能看到正式文本——这是降级，不是丢失：thinking 本就不该出现在 Chat 响应里）；
- usage 在 message_start（input 侧 + 缓存字段）与 message_delta（output 侧）
  分次到达，流尾合并后照常进 5 项埋点——命中率北极星不因流式缺数据。
"""
from __future__ import annotations

import json
import time

# Anthropic stop_reason → Chat finish_reason
_STOP_MAP = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length",
             "tool_use": "tool_calls", "refusal": "stop"}


def parse_sse_lines(line_iter):
    """把 SSE 行流解析成 (event, data) 事件对，逐事件 yield。

    line_iter 是任何按行迭代的字节/字符串流（urllib 响应对象即可）。
    data 按 SSE 规范可多行拼接；能解析成 JSON 就给 dict，否则给原字符串。
    流尾残留的半帧容错丢弃（正常结束的上游最后一定是 message_stop 完整帧）。
    """
    event, data_lines = None, []
    for raw in line_iter:
        line = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
        line = line.rstrip("\r\n")
        if not line:
            if data_lines:
                data = "\n".join(data_lines)
                try:
                    data = json.loads(data)
                except ValueError:
                    pass
                yield event, data
            event, data_lines = None, []
            continue
        if line.startswith(":"):
            continue  # 注释 / 心跳行
        field, _, value = line.partition(":")
        value = value.lstrip(" ")
        if field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)


class AnthropicToChatStream:
    """Anthropic SSE 事件 → Chat completion.chunk 帧（逐块、可即时下发）。

    用法：每个上游事件 feed() 一次，把返回的帧立即写给客户端；
    流结束后用 usage() / synthetic_response() 做埋点与历史落库。
    """

    def __init__(self) -> None:
        self.msg_id = "chatcmpl_stream"
        self.model = ""
        self.created = int(time.time())
        self._usage_in: dict = {}
        self._output_tokens = 0
        self._text_parts: list[str] = []
        # index -> {"id","name","args":[str]}；input_json_delta 逐段累积
        self._tool_open: dict[int, dict] = {}
        self._tools_done: list[dict] = []
        self._sent_role = False

    # -- 帧构造 ------------------------------------------------------------
    def _chunk(self, delta: dict, finish: str | None = None) -> str:
        frame = {"id": self.msg_id, "object": "chat.completion.chunk",
                 "created": self.created, "model": self.model,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return "data: " + json.dumps(frame, ensure_ascii=False) + "\n\n"

    def _role_frame(self) -> str:
        self._sent_role = True
        return self._chunk({"role": "assistant"})

    # -- 事件入口 -----------------------------------------------------------
    def feed(self, event: str | None, data) -> list[str]:
        """喂一个 Anthropic 事件，返回要立即下发客户端的 SSE 帧（可为空列表）。"""
        if not isinstance(data, dict):
            return []
        etype = data.get("type", event or "")
        out: list[str] = []
        if etype == "message_start":
            msg = data.get("message", {}) or {}
            self.msg_id = msg.get("id", self.msg_id)
            self.model = msg.get("model", self.model)
            self._usage_in = msg.get("usage", {}) or {}
            out.append(self._role_frame())
        elif etype == "content_block_start":
            idx = data.get("index", 0)
            block = data.get("content_block", {}) or {}
            if block.get("type") == "tool_use":
                self._tool_open[idx] = {"id": block.get("id", ""),
                                        "name": block.get("name", ""), "args": []}
        elif etype == "content_block_delta":
            idx = data.get("index", 0)
            delta = data.get("delta", {}) or {}
            dt = delta.get("type")
            if dt == "text_delta":
                text = delta.get("text", "")
                self._text_parts.append(text)
                if not self._sent_role:
                    out.append(self._role_frame())
                out.append(self._chunk({"content": text}))
            elif dt == "input_json_delta":
                tb = self._tool_open.get(idx)
                if tb is not None:
                    tb["args"].append(delta.get("partial_json", ""))
            # thinking_delta / signature_delta：Chat 无对应概念，不落帧（见模块 docstring）
        elif etype == "content_block_stop":
            idx = data.get("index", 0)
            tb = self._tool_open.pop(idx, None)
            if tb is not None:
                self._tools_done.append(tb)
                # 工具参数缓冲到块结束一次性发出（input_json_delta 非完整 JSON）
                out.append(self._chunk({"tool_calls": [{
                    "index": 0, "id": tb["id"], "type": "function",
                    "function": {"name": tb["name"],
                                 "arguments": "".join(tb["args"])}}]}))
        elif etype == "message_delta":
            delta = data.get("delta", {}) or {}
            usage = data.get("usage", {}) or {}
            self._output_tokens = usage.get("output_tokens", self._output_tokens)
            sr = delta.get("stop_reason")
            if sr:
                out.append(self._chunk({}, _STOP_MAP.get(sr, "stop")))
        elif etype == "message_stop":
            out.append("data: [DONE]\n\n")
        elif etype == "error":
            err = data.get("error", {}) or {}
            out.append(self._chunk(
                {"content": f"[上游错误] {err.get('message', 'unknown')}"}))
        # ping 等控制事件：不落帧
        return out

    # -- 流尾汇总（埋点 + 历史落库的原料） ------------------------------------
    def usage(self) -> dict:
        """合并 message_start / message_delta 的 usage（保持 Anthropic 口径）。"""
        u = dict(self._usage_in)
        u["output_tokens"] = self._output_tokens
        return u

    def synthetic_response(self) -> dict:
        """把累计内容拼成 Anthropic 非流式响应形状——直接复用
        `assistant_from_upstream("anthropic", ...)` 与 `_usage_for`，
        流式与非流式的落库/埋点走同一条代码路径，不另造语义。"""
        content: list[dict] = []
        text = "".join(self._text_parts)
        if text:
            content.append({"type": "text", "text": text})
        done = list(self._tools_done)
        # 容错：流被截断时未正常 content_block_stop 的工具块也入史
        done += [self._tool_open[i] for i in sorted(self._tool_open)]
        for tb in done:
            try:
                tool_input = json.loads("".join(tb["args"]) or "{}")
            except ValueError:
                tool_input = {}
            content.append({"type": "tool_use", "id": tb["id"],
                            "name": tb["name"], "input": tool_input})
        return {"content": content, "usage": self.usage()}
