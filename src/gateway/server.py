"""网关/代理主进程：HTTP 转发 + SSE 透传骨架。

落点（方案 3.2）：独立进程承载状态层，是被「必须支持 previous_response_id」锁定的形态。

请求流程：
  入口按 target 选 adapter -> to_ir -> (有状态则状态层重放历史) ->
  from_ir 渲染目标协议（Anthropic 侧按断点布局打 cache_control）->
  转发到 backend（默认 mock，可离线跑通）-> 归一 usage + 命中率埋点 -> SSE 透传回客户端。

真实 API 挂起：backend 默认指向 tools/mock_backend.py；拿到 key 后把
BACKEND_URL 换成真实端点即可，转换层代码不动。
"""
from __future__ import annotations

import json
import os
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..adapters.base import Dropped
from ..adapters.chat import ChatAdapter
from ..adapters.response import ResponseAdapter
from ..adapters.anthropic import AnthropicAdapter
from ..ir import model as ir
from ..state.session_config import load as load_session_config
from ..state.store import SessionStore
from ..observability.metrics import MetricsLog, NORMAL, WARMUP

BACKEND_URL = os.environ.get("PB_BACKEND", "http://127.0.0.1:9100").rstrip("/")
BACKEND_KEY = os.environ.get("PB_API_KEY", "")          # 真实后端 Bearer key（仅环境变量，不落盘）
DIRECT = os.environ.get("PB_DIRECT", "") == "1"         # 1 = 绕过系统代理直连
BIND = ("127.0.0.1", int(os.environ.get("PB_PORT", "8080")))
METRICS_PATH = os.environ.get("PB_METRICS", "metrics.jsonl")

# 真实后端的端点路径（mock_backend 同时兼容这几条路径 + 网关转发路径）
BACKEND_PATH = {
    "openai_chat": "/chat/completions",
    "openai_response": "/responses",
    "anthropic": "/v1/messages",
}

ADAPTERS = {
    "openai_chat": ChatAdapter(),
    "openai_response": ResponseAdapter(),
    "anthropic": AnthropicAdapter(),
}

CFG = load_session_config()
STORE = SessionStore(CFG)
METRICS = MetricsLog(METRICS_PATH)

# 限流：并发上限（默认 2，硬件友好 + token 成本不失控），PB_CONCURRENCY 可调
_GATE = threading.Semaphore(int(os.environ.get("PB_CONCURRENCY", "2")))


# ---------------------------------------------------------------------------
# 预热请求（max_tokens:0）：单独语义 + 拒绝条件校验（方案 3.7，v3 已核实官方行为）
# ---------------------------------------------------------------------------

def is_warmup(payload: dict) -> bool:
    mt = payload.get("max_tokens", payload.get("max_output_tokens", 1))
    try:
        return int(mt) == 0
    except (TypeError, ValueError):
        return False


def validate_warmup(rendered: dict, target: str) -> str | None:
    """预热请求的拒绝条件：冲突则返回错误消息（应回 invalid_request_error）。

    必须校验**转换后、即将发给上游的 rendered**（目标协议形态），不能用源协议
    payload——拒绝条件是上游（target）的行为，且字段只存在于目标协议里
    （如 thinking 属 Anthropic、response_format 属 OpenAI）。
    """
    if rendered.get("stream"):
        return "warmup(max_tokens:0) 不能同时带 stream:true"
    if target == "anthropic":
        thinking = rendered.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") == "enabled":
            return "warmup(max_tokens:0) 不能同时带 extended thinking"
    else:
        # OpenAI 系：structured outputs（Chat 用 response_format；Responses 用 text.format）
        if (rendered.get("response_format")
                or (rendered.get("output_config") or {}).get("format")
                or (rendered.get("text") or {}).get("format")):
            return "warmup(max_tokens:0) 不能同时带 structured outputs"
    tc = rendered.get("tool_choice")
    if isinstance(tc, dict) and tc.get("type") in ("tool", "any"):
        return "warmup(max_tokens:0) 不能同时带 tool_choice={type:tool/any}"
    return None


# ---------------------------------------------------------------------------
# 记忆注入（方案 3.9 / TRACK 01）：session meta → IR system 尾部，文本幂等去重
# ---------------------------------------------------------------------------

def inject_memories(req: ir.IRRequest, session) -> int:
    """把 session.meta 的记忆块注入 IR system 尾部。返回注入条数。

    幂等去重：按文本比对——外部请求若已注入相同文本则跳过，
    避免同一段记忆重复注入、重复计费（方案 3.9 耦合点 2）。
    记忆上限（TRACK 04 第三参数）：会话 meta.memory_cap > 全局
    cfg.session_memory_cap > 0（不限制）；达到上限即停，保留已注入的稳定前缀。
    """
    memories = (session.meta or {}).get("memories") if session else None
    if not memories:
        return 0
    cap = (session.meta or {}).get("memory_cap") or CFG.session_memory_cap
    seen = {b.text for b in req.system
            if b.kind == ir.TEXT and b.text}
    # cap 语义 = 本会话「由记忆注入产生的块」总数上限。
    # 用 extra 标记识别已注入块：原始 system 里恰好同名的文本不算注入、不占名额；
    # 已注入块占名额——保证跨轮幂等（下轮同一批记忆不会突破 cap 继续加）。
    injected_count = sum(1 for b in req.system
                         if b.extra.get("injected_memory"))
    added = 0
    for m in memories:
        if not isinstance(m, dict):
            continue
        text = (m.get("content") or m.get("preview") or "").strip()
        if not text or text in seen:
            continue  # 空文本 / 已在 system 中（无论来源）→ 幂等跳过
        if cap and injected_count >= cap:
            break
        req.system.append(ir.Block(kind=ir.TEXT, text=text,
                                   extra={"injected_memory": True}))
        seen.add(text)
        injected_count += 1
        added += 1
    return added


def convert(source: str, target: str, payload: dict, headers: dict):
    """source 协议 payload -> target 协议 payload。

    返回 6 元组 (out, req, dropped, session, injected, replayed)。
    `replayed` 是本轮**真正重放进来的历史切片**（不含本轮新消息），
    供埋点区分「重放代价」与「本轮输入」——两者混在一起会让重放指标系统性偏高。
    """
    dropped = Dropped()
    src = ADAPTERS[source]
    dst = ADAPTERS[target]

    req = src.to_ir(payload, dropped)

    # 有状态：Response 的 previous_response_id -> 状态层重放历史
    session = None
    replayed: list = []
    prev_id = req.extra.get("previous_response_id")
    if prev_id:
        session = STORE.resolve_previous(prev_id)
        if session is not None:
            replayed = STORE.replay(session)
            req.messages = replayed + req.messages
        else:
            dropped.add("previous_response_id", "未找到对应会话，按无状态处理", "explicit")

    # 需要会话的三种情况，缺一个多轮链路就起不来：
    #   anthropic       —— 记忆注入 + 断点布局都要挂会话
    #   openai_response —— Responses 是带状态协议，网关必须回一个自己能解析的
    #                      response_id 给客户端，否则第一轮之后无从续接
    #   previous_response_id 已解析出会话
    if session is None and target in ("anthropic", "openai_response"):
        session = STORE.get_or_create(CFG.session_key(headers))

    # Anthropic 目标：按 L2 断点布局打 cache_control
    ctx = None
    if target == "anthropic":
        ctx = ir.SessionContext(session_key=session.key, history=replayed)

    # 记忆注入：重放之后、渲染之前（注入内容进 system 尾部，吃 system 后断点）
    injected = inject_memories(req, session) if session else 0

    out = dst.from_ir(req, dropped, ctx) if target == "anthropic" else dst.from_ir(req, dropped)
    return out, req, dropped, session, injected, replayed


def post_json(url: str, payload: dict, target: str = "") -> dict:
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        # 部分真实网关（如 Cloudflare 前置）按 UA 拦 bot，带浏览器 UA 更稳
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
    }
    if BACKEND_KEY:
        headers["Authorization"] = f"Bearer {BACKEND_KEY}"
        # Anthropic 官方协议用 x-api-key；中转站两种 usually 都认，两个都带无害
        headers["x-api-key"] = BACKEND_KEY
    if target == "anthropic":
        # Anthropic 原生端点/中转站常校验该头
        headers["anthropic-version"] = os.environ.get("PB_ANTHROPIC_VERSION",
                                                      "2023-06-01")
    req = urllib.request.Request(url, data=data, headers=headers)
    # PB_DIRECT=1 时绕过系统代理（本地代理隧道对长 POST 可能 502）
    opener = (urllib.request.build_opener(urllib.request.ProxyHandler({}))
              if DIRECT else urllib.request.build_opener())
    with opener.open(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静音
        pass

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        # 路由：/v1/{source}/to/{target}
        parts = [p for p in self.path.split("/") if p]

        # 管理口：POST /v1/admin/session/meta  {"key": "...", "meta": {...}}
        # 供实验脚本/未来的弹网页 Session Init 写入记忆与 memory_cap
        if parts[:2] == ["v1", "admin"] and len(parts) == 4 \
                and parts[2] == "session" and parts[3] == "meta":
            key = payload.get("key")
            meta = payload.get("meta")
            if not key or not isinstance(meta, dict):
                return self._json(400, {"error": "need {key, meta}"})
            STORE.update_meta(key, **meta)
            return self._json(200, {"ok": True, "key": key})

        try:
            source, target = parts[1], parts[3]
        except IndexError:
            return self._json(400, {"error": "path must be /v1/{source}/to/{target}"})
        # 未知协议必须回 400：不拦的话 ADAPTERS[target] 抛 KeyError，
        # 请求线程直接崩、连接被掐断，客户端只看到 RemoteDisconnected 而非错误信息。
        if source not in ADAPTERS or target not in ADAPTERS:
            return self._json(400, {"error": {
                "message": f"unknown protocol: {source} -> {target}",
                "type": "invalid_request_error",
                "supported": sorted(ADAPTERS)}})

        headers = {k.lower(): v for k, v in self.headers.items()}

        out, req, dropped, session, injected, replayed_hist = convert(
            source, target, payload, headers)

        # 预热请求：单独语义，用**转换后发给上游的报文**过拒绝条件校验（方案 3.7）
        warmup = is_warmup(payload)
        if warmup:
            werr = validate_warmup(out, target)
            if werr:
                return self._json(400, {"error": {"message": werr,
                                                  "type": "invalid_request_error"}})

        # mock 与真实后端走同一套端点映射。tools/mock_backend.py 已按端点路径
        # 返回对应协议形状，不再为 mock 特判——否则离线跑的和上线跑的不是同一条路径，
        # 上线时才暴露的问题离线永远测不出来。
        path = BACKEND_PATH.get(target, self.path)
        with _GATE:  # 限流：并发上限保护上游与成本
            try:
                backend_resp = post_json(BACKEND_URL + path, out, target)
            except urllib.error.HTTPError as e:
                # 上游错误原样透出状态码，避免把 4xx/5xx 伪装成网关 500
                detail = e.read().decode("utf-8", "replace")[:500]
                return self._json(int(e.code), {
                    "error": {"message": "backend error", "type": "upstream_error",
                              "status": int(e.code), "detail": detail}})
            except Exception as e:  # 超时/连接失败等
                return self._json(502, {
                    "error": {"message": f"backend unreachable: {e}",
                              "type": "upstream_error"}})

        # 归一 usage + 命中率埋点（真实字段由真实 API 返回；mock 时为占位值）
        usage = _usage_for(target, backend_resp)
        # 重放代价只算真正重放的历史切片，不含本轮新消息
        replayed_chars = sum(len(b.text or "")
                             for m in replayed_hist for b in m.blocks)
        # 按 extra 标记统计（而非按尾部切片），注入块位置变化时依然准确
        injected_chars = sum(len(b.text or "") for b in req.system
                             if b.extra.get("injected_memory"))
        METRICS.record_turn(usage, injected=injected_chars, replayed=replayed_chars,
                            dropped=dropped.items,
                            degradation="explicit" if dropped else "none",
                            kind=WARMUP if warmup else NORMAL)

        # 有状态：登记响应供下一轮 previous_response_id 引用。
        # 预热轮不登记——它不产出可引用的对话轮，登记会污染响应链。
        rid = None
        if session is not None and not warmup:
            # 本轮对话必须落进会话历史，否则下一轮 previous_response_id 重放出的是空列表，
            # 整个状态层等于空转（历史永远是空 -> 重放永远是空 -> 多轮链路第一轮就断）。
            # 只追加本轮新消息：重放切片已在历史里，再加一次会自我复制。
            new_turn = list(req.messages[len(replayed_hist):])
            reply = _assistant_message(target, backend_resp)
            if reply is not None:
                new_turn.append(reply)
            if new_turn:
                STORE.append(session, new_turn)
            rid = STORE.record_response(session)

        body = dict(backend_resp)
        # 客户端说 Responses 协议时，下一轮会带 previous_response_id 回来。
        # 那个 id 必须是网关自己能解析的，否则多轮链路在第一轮之后就断了
        # （上游真实 id 我们解析不了，上游同样解析不了我们的 id）。
        # 上游原始 id 保留在 _bridge_upstream_id 便于对账。
        if rid and source == "openai_response":
            body["_bridge_upstream_id"] = backend_resp.get("id", "")
            body["id"] = rid
        body["_bridge"] = {"dropped": dropped.items, "session": bool(session),
                           "injected_memories": injected,
                           "warmup": warmup,
                           "replayed_messages": len(replayed_hist)}
        self._json(200, body)


def _assistant_message(target: str, resp: dict):
    """上游响应 -> IR assistant 消息（只取文本，够状态层重放用）。

    只取文本是有意为之：工具调用 / 思考块的重放在当前实验范围内不涉及
    （方案 3.8 不做复杂块重放）。但文本必须进历史，否则下一轮重放是无源之水。
    """
    if target == "anthropic":
        texts = [b.get("text", "") for b in resp.get("content", []) or []
                 if isinstance(b, dict) and b.get("type") == "text"]
    elif target == "openai_response":
        texts = []
        for item in resp.get("output", []) or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for c in item.get("content", []) or []:
                if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                    texts.append(c.get("text", ""))
    else:  # openai_chat
        try:
            texts = [resp["choices"][0]["message"].get("content") or ""]
        except (KeyError, IndexError, TypeError, AttributeError):
            texts = []
    text = "".join(texts).strip()
    return ir.Message.text("assistant", text) if text else None


def _usage_for(target: str, resp: dict) -> ir.IRUsage:
    u = resp.get("usage", {}) or {}
    if target == "anthropic":
        from ..adapters.anthropic import usage_from_anthropic
        return usage_from_anthropic(u)
    if target == "openai_response":
        from ..adapters.response import usage_from_response
        return usage_from_response(u)
    from ..adapters.chat import usage_from_chat
    return usage_from_chat(u)


def main():
    srv = ThreadingHTTPServer(BIND, Handler)
    print(f"protocol-bridge gateway on http://{BIND[0]}:{BIND[1]} -> backend {BACKEND_URL}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
