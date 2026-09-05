"""mock backend：离线顶替真实 Anthropic/OpenAI 端点，供网关跑通链路。

按**请求路径**返回对应协议的响应形状：
  /v1/messages        -> Anthropic Messages
  /responses          -> OpenAI Responses
  /chat/completions   -> OpenAI Chat Completions

缓存行为为**有状态模拟**：按带 cache_control 的前缀内容哈希记账——首次见到某
前缀报 cache_creation，再次见到同一前缀报 cache_read，供离线验证
「预热 -> 命中」两步链路。不代表真实缓存行为（无 TTL、无最小长度门槛）。

注意：前缀哈希只认 system / messages / input 里的文本块，且只有带
cache_control 的块才算前缀末尾——与 Anthropic 的语义一致，但比真实实现粗糙。
"""
from __future__ import annotations

import hashlib
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIND = ("127.0.0.1", 9100)

# 已建缓存的前缀哈希集合（模拟 cache 命名空间）
_SEEN_PREFIXES: set[str] = set()


def _prefix_hash_upto_breakpoint(payload: dict) -> tuple[str | None, int]:
    """找到最后一个带 cache_control 的块，返回 (前缀哈希, 前缀字符数)。"""
    parts: list[str] = []
    last_marked = -1

    def walk(blocks):
        nonlocal last_marked
        for b in blocks:
            parts.append(b.get("text", "") if isinstance(b, dict) else str(b))
            if isinstance(b, dict) and b.get("cache_control"):
                last_marked = len(parts) - 1

    sys_ = payload.get("system")
    if isinstance(sys_, str):
        parts.append(sys_)
    elif isinstance(sys_, list):
        walk(sys_)
    for m in payload.get("messages", []) or []:
        c = m.get("content")
        walk(c if isinstance(c, list) else [{"text": str(c)}])
    # Responses 协议：条目在 input 里
    for item in payload.get("input", []) or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        c = item.get("content")
        walk(c if isinstance(c, list) else [{"text": str(c)}])

    if last_marked < 0:
        return None, 0
    prefix = "".join(parts[: last_marked + 1])
    return hashlib.sha256(prefix.encode("utf-8")).hexdigest(), len(prefix)


def _cache_tick(payload: dict) -> tuple[int, int]:
    """按前缀哈希记账，返回 (cache_creation, cache_read)。"""
    ph, plen = _prefix_hash_upto_breakpoint(payload)
    if ph is None:
        return 0, 0
    if ph in _SEEN_PREFIXES:
        return 0, plen          # 再次见到同一前缀 -> 读命中
    _SEEN_PREFIXES.add(ph)
    return plen, 0              # 首次 -> 写缓存


def _input_chars(payload: dict) -> int:
    """粗略的输入量（字符数），只为让 usage 各字段非零可看。"""
    n = 512
    sys_ = payload.get("system")
    if isinstance(sys_, str):
        n += len(sys_)
    elif isinstance(sys_, list):
        n += sum(len(b.get("text", "")) for b in sys_)
    for m in payload.get("messages", []) or []:
        c = m.get("content")
        n += len(c) if isinstance(c, str) else sum(
            len(b.get("text", "")) for b in c if isinstance(b, dict))
    for item in payload.get("input", []) or []:
        c = item.get("content") if isinstance(item, dict) else None
        n += len(c) if isinstance(c, str) else sum(
            len(b.get("text", "")) for b in (c or []) if isinstance(b, dict))
    return n


def _is_warmup(payload: dict) -> bool:
    for k in ("max_tokens", "max_output_tokens", "max_completion_tokens"):
        try:
            if int(payload.get(k, 1)) == 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


# ---------------------------------------------------------------------------
# 三种协议各自的响应形状
# ---------------------------------------------------------------------------

def anthropic_response(payload: dict) -> dict:
    creation, read = _cache_tick(payload)
    usage = {"input_tokens": _input_chars(payload), "output_tokens": 0 if _is_warmup(payload) else 12,
             "cache_creation_input_tokens": creation, "cache_read_input_tokens": read}
    if _is_warmup(payload):
        # max_tokens:0 预热：空 content + stop_reason=max_tokens（官方行为）
        return {"id": "msg_mock_warmup", "type": "message", "role": "assistant",
                "content": [], "model": payload.get("model", "mock"),
                "stop_reason": "max_tokens", "usage": usage}
    return {"id": "msg_mock01", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "mock reply"}],
            "model": payload.get("model", "mock"), "stop_reason": "end_turn",
            "usage": usage}


def openai_response_response(payload: dict) -> dict:
    _, read = _cache_tick(payload)
    usage = {"input_tokens": _input_chars(payload),
             "output_tokens": 0 if _is_warmup(payload) else 12,
             "input_tokens_details": {"cached_tokens": read},
             "output_tokens_details": {"reasoning_tokens": 0}}
    if _is_warmup(payload):
        return {"id": "resp_mock_warmup", "object": "response", "output": [],
                "model": payload.get("model", "mock"), "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "usage": usage}
    return {"id": "resp_mock01", "object": "response",
            "output": [{"type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "mock reply",
                                     "annotations": []}]}],
            "model": payload.get("model", "mock"), "status": "completed",
            "usage": usage}


def chat_response(payload: dict) -> dict:
    _, read = _cache_tick(payload)
    usage = {"prompt_tokens": _input_chars(payload),
             "completion_tokens": 0 if _is_warmup(payload) else 12,
             "prompt_tokens_details": {"cached_tokens": read}}
    content = "" if _is_warmup(payload) else "mock reply"
    return {"id": "chatcmpl_mock01", "object": "chat.completion",
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": content},
                         "finish_reason": "length" if _is_warmup(payload) else "stop"}],
            "model": payload.get("model", "mock"), "usage": usage}


def payload_response(payload: dict, path: str) -> dict:
    """按请求路径挑响应形状（网关转发真实后端时路径是协议专属端点）。"""
    if path.rstrip("/").endswith("/responses"):
        return openai_response_response(payload)
    if "chat/completions" in path:
        return chat_response(payload)
    return anthropic_response(payload)


def sse_lines(payload: dict):
    chunks = ["mock ", "stream ", "reply"]
    for c in chunks:
        yield f"data: {json.dumps({'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': c}})}\n\n"
        time.sleep(0.01)
    yield 'data: {"type": "message_stop"}\n\n'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for line in sse_lines(payload):
                self.wfile.write(line.encode("utf-8"))
            return
        body = json.dumps(payload_response(payload, self.path),
                          ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    print(f"mock backend on http://{BIND[0]}:{BIND[1]}")
    ThreadingHTTPServer(BIND, Handler).serve_forever()
