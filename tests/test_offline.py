"""离线单测：IR 往返 / adapter 映射 / 状态层 / 预热字段处理。

全部用内联录制样例，不依赖真实 API。运行：
  python -m unittest tests.test_offline -v   （或 pytest tests/ -q）
"""
import json
import os
import sys
import threading
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.adapters.base import Dropped
from src.adapters.chat import ChatAdapter, usage_from_chat
from src.adapters.anthropic import AnthropicAdapter, usage_from_anthropic
from src.adapters.response import ResponseAdapter
from src.ir import model as ir
from src.state.session_config import (SessionConfig, build_prefix, load,
                                      FULL, LAST_BREAKPOINT, SLIDING_WINDOW)
from src.state.store import SessionStore
from src.warmup import prewarm


class TestAdapterRoundTrip(unittest.TestCase):
    def test_chat_to_anthropic_breakpoint_cap(self):
        payload = {
            "model": "gpt-x",
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
                {"role": "user", "content": "again"},
            ],
            "tools": [{"type": "function", "function": {
                "name": "search", "description": "d", "parameters": {}}}],
        }
        req = ChatAdapter().to_ir(payload, Dropped())
        self.assertEqual(len(req.system), 1)
        self.assertEqual(len(req.messages), 3)
        self.assertEqual(len(req.tools), 1)

        ctx = ir.SessionContext()  # 默认 3 固定 + 1 滚动
        out = AnthropicAdapter().from_ir(req, Dropped(), ctx)
        bps = _count_breakpoints(out)
        self.assertLessEqual(bps, 4, "cache_control 断点不得超过官方 4 个上限")
        self.assertGreaterEqual(bps, 3, "3 固定 + 1 滚动应至少 3 个")

    def test_usage_normalize_three_flavors(self):
        a = usage_from_anthropic({"input_tokens": 8, "output_tokens": 0,
                                  "cache_creation_input_tokens": 5120,
                                  "cache_read_input_tokens": 0})
        self.assertEqual(a.total_input, 5128)
        c = usage_from_chat({"prompt_tokens": 2006, "completion_tokens": 300,
                             "prompt_tokens_details": {"cached_tokens": 1920}})
        self.assertEqual(c.cache_read_input_tokens, 1920)


class TestStateLayer(unittest.TestCase):
    def test_previous_response_id_replay(self):
        store = SessionStore(SessionConfig(), db_path=":memory:")
        self.addCleanup(store.shutdown)
        s = store.get_or_create("task-1")
        store.append(s, [ir.Message.text("user", "u1"), ir.Message.text("assistant", "a1")])
        rid = store.record_response(s)
        # 下一轮带 previous_response_id，应解析回同一 session 并重放历史
        s2 = store.resolve_previous(rid)
        self.assertIsNotNone(s2)
        self.assertEqual(len(store.replay(s2)), 2)

    def test_build_prefix_strategies(self):
        hist = [ir.Message.text("user", f"m{i}") for i in range(5)]
        cfg = SessionConfig(replay_from=FULL)
        self.assertEqual(len(build_prefix(hist, 0, cfg)), 5)
        cfg.replay_from = LAST_BREAKPOINT
        self.assertEqual(len(build_prefix(hist, 3, cfg)), 2)
        cfg.replay_from = SLIDING_WINDOW
        cfg.sliding_window_n = 2
        self.assertEqual(len(build_prefix(hist, 0, cfg)), 2)

    def test_ttl_eviction(self):
        cfg = SessionConfig(end_policy="ttl", ttl_seconds=1, on_end="archive")
        store = SessionStore(cfg, db_path=":memory:")
        self.addCleanup(store.shutdown)
        s = store.get_or_create("task-x")
        s.touched_at -= 10  # 假装已超时（持久化层需写回才生效）
        store._save(s)
        self.assertEqual(store.evict_expired(), 1)

    def test_sqlite_persistence_roundtrip(self):
        """重启（换连接）后会话与 meta 不丢。"""
        import tempfile, os as _os
        with tempfile.TemporaryDirectory() as td:
            db = _os.path.join(td, "s.db")
            cfg = SessionConfig()
            st1 = SessionStore(cfg, db_path=db)
            s = st1.get_or_create("task-p")
            st1.append(s, [ir.Message.text("user", "u1")])
            st1.update_meta("task-p", memories=[{"content": "记住：偏好简洁"}], memory_cap=3)
            rid = st1.record_response(s)
            # 模拟进程重启：新实例读同一文件
            st2 = SessionStore(cfg, db_path=db)
            s2 = st2.resolve_previous(rid)
            self.assertIsNotNone(s2)
            self.assertEqual(len(st2.replay(s2)), 1)
            self.assertEqual(s2.meta["memory_cap"], 3)
            self.assertEqual(s2.meta["memories"][0]["content"], "记住：偏好简洁")
            st1.shutdown()
            st2.shutdown()


class TestWarmup(unittest.TestCase):
    def test_build_and_validate(self):
        w = prewarm.build_warmup_request("system prompt", "claude-opus-4-8")
        self.assertEqual(w["max_tokens"], 0)
        self.assertEqual(w["messages"][0]["content"], prewarm.PLACEHOLDER)
        self.assertEqual(prewarm.validate_warmup(w), [])
        # 断点打在 system 而非占位消息
        self.assertIn("cache_control", w["system"][0])
        self.assertNotIn("cache_control", w["messages"][0])

    def test_forbidden_conflicts(self):
        bad = {"max_tokens": 0, "stream": True, "tool_choice": {"type": "any"}}
        self.assertIn("stream", prewarm.validate_warmup(bad))
        self.assertIn("tool_choice", prewarm.validate_warmup(bad))

    def test_parse_malformed_warmup_response(self):
        body = {"id": "msg_1", "model": "m", "content": [],
                "stop_reason": "max_tokens",
                "usage": {"input_tokens": 8, "output_tokens": 0,
                          "cache_creation_input_tokens": 5120}}
        self.assertTrue(prewarm.is_warmup_response(body))
        r = prewarm.parse_warmup_response(body)
        self.assertTrue(r.is_warmup)
        self.assertEqual(r.blocks, [])
        self.assertEqual(r.usage.cache_creation_input_tokens, 5120)


class TestSessionConfig(unittest.TestCase):
    def test_load_defaults_and_switch(self):
        cfg = load()  # 从 config/session.json
        self.assertEqual(cfg.replay_from, "full")
        snap = cfg.snapshot()
        self.assertIn("replay_from", snap)
        # 热切换重放策略，不动其余配置
        cfg.replay_from = "sliding_window"
        self.assertEqual(cfg.replay_from, "sliding_window")
        self.assertEqual(cfg.key_granularity, "single_task")


class TestGatewayPolicies(unittest.TestCase):
    """v1.1 取长补短：预热拒绝校验 + 记忆注入（幂等去重 + memory_cap）。"""

    def test_warmup_rejection_conditions(self):
        from src.gateway.server import is_warmup, validate_warmup
        self.assertTrue(is_warmup({"max_tokens": 0}))
        self.assertTrue(is_warmup({"max_output_tokens": 0}))
        self.assertFalse(is_warmup({"max_tokens": 100}))
        # 四类冲突（Anthropic 侧查 thinking；OpenAI 侧查 structured outputs）
        self.assertIn("stream", validate_warmup(
            {"stream": True}, "anthropic"))
        self.assertIn("thinking", validate_warmup(
            {"thinking": {"type": "enabled"}}, "anthropic"))
        self.assertIn("structured", validate_warmup(
            {"response_format": {"type": "json_schema"}}, "openai_chat"))
        self.assertIn("tool_choice", validate_warmup(
            {"tool_choice": {"type": "any"}}, "anthropic"))
        self.assertIsNone(validate_warmup({"max_tokens": 0}, "anthropic"))

    def test_memory_injection_dedup_and_cap(self):
        from src.gateway.server import inject_memories
        from src.state.store import Session
        req = ir.IRRequest(system=[ir.Block(kind=ir.TEXT, text="已有系统提示")])
        s = Session(key="t", meta={"memories": [
            {"content": "记忆A"},
            {"content": "已有系统提示"},   # 与 system 已有文本重复 → 跳过
            {"content": "记忆B"},
            {"content": "记忆C"},
        ], "memory_cap": 2})
        n = inject_memories(req, s)
        self.assertEqual(n, 2, "去重后剩 3 条候选，cap=2 只注入 2 条")
        texts = [b.text for b in req.system]
        self.assertEqual(texts, ["已有系统提示", "记忆A", "记忆B"])
        # 幂等：重复注入不再加
        self.assertEqual(inject_memories(req, s), 0)

    def test_memory_cap_fallback_chain(self):
        from src.gateway import server
        from src.state.store import Session
        req = ir.IRRequest()
        s = Session(key="t", meta={"memories": [{"content": f"m{i}"} for i in range(5)]})
        # 会话无 memory_cap → 用全局 cfg（0=不限）
        old = server.CFG.session_memory_cap
        server.CFG.session_memory_cap = 3
        try:
            self.assertEqual(server.inject_memories(req, s), 3)
        finally:
            server.CFG.session_memory_cap = old


class TestAuditFindings(unittest.TestCase):
    """2026-09-02 代码自查发现的问题，各补一条回归测试（修复前均会失败）。

    第一批（adapter/网关）：4 断点未落地、静默丢块、预热校验对象错、上游错误被吞。
    第二批（埋点与有状态链路）：重放指标口径、预热污染北极星、滑动窗口 n=0、
    顶层字段静默丢弃、previous_response_id 回传上游、Anthropic 专属参数丢失。
    """

    def test_four_breakpoints_when_history_present(self):
        """有重放历史时应打满 4 个断点（tools后 / system后 / 历史静态段后 / 滚动尾部）。
        修复前：bp_after_history_static 从未落地，实际只打 3 个。"""
        # Chat 协议的 system 是 messages 里的 role:"system"，不是顶层 system 字段
        payload = {
            "model": "m",
            "messages": [{"role": "system", "content": "sys"},
                         {"role": "user", "content": "u1"},
                         {"role": "assistant", "content": "a1"},
                         {"role": "user", "content": "u2"}],
            "tools": [{"type": "function", "function": {
                "name": "search", "description": "d", "parameters": {}}}],
        }
        req = ChatAdapter().to_ir(payload, Dropped())
        ctx = ir.SessionContext(history=[ir.Message.text("user", "u0"),
                                         ir.Message.text("assistant", "a0")])
        req.messages = ctx.history + req.messages  # 模拟重放 2 条历史
        out = AnthropicAdapter().from_ir(req, Dropped(), ctx)
        self.assertEqual(_count_breakpoints(out), 4)

    def test_silent_drop_is_recorded(self):
        """无对应类型的块（如 image）必须进降级记录，不能静默丢弃。"""
        req = ir.IRRequest(messages=[ir.Message(
            role="user", blocks=[ir.Block(kind=ir.IMAGE, text="[img]")])])
        d = Dropped()
        AnthropicAdapter().from_ir(req, d)
        self.assertTrue(any("image" in str(i) for i in d.items),
                        f"image 块被静默丢弃且无记录：{d.items}")

    def test_warmup_validates_target_protocol_fields(self):
        """校验对象是转换后的目标协议报文，不是源协议 payload。"""
        from src.gateway.server import validate_warmup
        self.assertIn("structured", validate_warmup(
            {"response_format": {"type": "json_object"}}, "openai_chat"))
        self.assertIsNone(validate_warmup(
            {"response_format": {"type": "json_object"}}, "anthropic"),
            "Anthropic 上游不看 response_format，不应误报")

    def test_backend_http_error_propagates(self):
        """上游 4xx/5xx 必须抛 HTTPError（网关原样透出状态码），不能被吞掉。"""
        import threading
        import urllib.error
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from src.gateway.server import post_json

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(529)
                self.end_headers()
                self.wfile.write(b'{"error":"overloaded"}')

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                post_json(f"http://127.0.0.1:{srv.server_port}/x", {}, "openai_chat")
            self.assertEqual(cm.exception.code, 529)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_replayed_slice_excludes_current_turn(self):
        """重放指标只算真正重放的历史，不能把本轮新消息也算进去。

        修复前埋点统计的是 req.messages 全部（= 重放历史 + 本轮新消息），
        「重放代价」被系统性高估，且高估幅度随对话轮次增长。
        """
        from src.gateway import server

        old_store = server.STORE
        st = SessionStore(SessionConfig(), db_path=":memory:")
        server.STORE = st
        try:
            s = st.get_or_create("k-replay")
            st.append(s, [ir.Message.text("user", "历史" * 20)])
            rid = st.record_response(s)
            payload = {"model": "m",
                       "input": [{"type": "message", "role": "user",
                                  "content": "本轮" * 20}],
                       "previous_response_id": rid}
            out, req, dropped, session, injected, replayed = server.convert(
                "openai_response", "openai_chat", payload, {})
            self.assertEqual(len(replayed), 1, "重放切片应只含 1 条历史")
            self.assertEqual(len(req.messages), 2, "本轮 1 条 + 重放 1 条")
            self.assertEqual(
                "".join(b.text or "" for m in replayed for b in m.blocks),
                "历史" * 20)
        finally:
            st.shutdown()
            server.STORE = old_store

    def test_hit_rate_excludes_warmup_turns(self):
        """预热轮 cache_read 恒为 0，计入分母会按预热占比稀释北极星。"""
        from src.observability.metrics import (TurnMetrics, hit_rate,
                                               NORMAL, WARMUP)
        ms = [
            TurnMetrics(kind=NORMAL, cache_read_input_tokens=100),
            TurnMetrics(kind=NORMAL, cache_read_input_tokens=0),
            TurnMetrics(kind=WARMUP, cache_read_input_tokens=0),
        ]
        self.assertAlmostEqual(hit_rate(ms), 0.5, msg="2 个正常轮命中 1 个")
        self.assertAlmostEqual(hit_rate(ms, include_warmup=True), 1 / 3)

    def test_build_prefix_sliding_window_zero_returns_empty(self):
        """sliding_window_n<=0 必须是空窗口。

        修复前直接写 history[-n:]，n==0 时等价于 history[0:] → 静默退化成
        full，实验组间差异消失且极难察觉。
        """
        hist = [ir.Message.text("user", f"m{i}") for i in range(5)]
        cfg = SessionConfig(replay_from=SLIDING_WINDOW, sliding_window_n=0)
        self.assertEqual(build_prefix(hist, 0, cfg), [])

    def test_chat_unknown_top_level_field_is_recorded(self):
        """非规范/无映射的顶层字段必须显式接管或记录，不能静默丢弃。"""
        payload = {"model": "m",
                   "messages": [{"role": "user", "content": "hi"}],
                   "system": "顶层系统提示",   # 非 Chat 规范字段
                   "top_p": 0.9}               # IR 无映射字段
        d = Dropped()
        req = ChatAdapter().to_ir(payload, d)
        self.assertEqual([b.text for b in req.system], ["顶层系统提示"],
                         "顶层 system 应按语义上提，语义不能丢")
        fields = {i["field"] for i in d.items}
        self.assertIn("system", fields)
        self.assertIn("top_p", fields, "IR 无映射的顶层参数被静默丢弃")

    def test_previous_response_id_not_forwarded_upstream(self):
        """历史已由网关重放，不能再把 previous_response_id 透给上游：

        传上游真实 id → 上游二次拼接历史，上下文重复、token 翻倍；
        传网关自生成的 resp_xxx → 上游不认识，直接 404。
        """
        req = ir.IRRequest(extra={"previous_response_id": "resp_abc"})
        out = ResponseAdapter().from_ir(req, Dropped())
        self.assertNotIn("previous_response_id", out)

    def test_anthropic_thinking_roundtrip(self):
        """extended thinking / tool_choice 属 Anthropic 专属参数，IR 无字段但不可丢。

        anthropic→anthropic 必须无损；同时这决定了 validate_warmup 的
        thinking 冲突检查是否真的可达。
        """
        d = Dropped()
        req = AnthropicAdapter().to_ir(
            {"model": "m", "max_tokens": 10,
             "thinking": {"type": "enabled", "budget_tokens": 1024},
             "tool_choice": {"type": "auto"},
             "messages": [{"role": "user", "content": "hi"}]}, d)
        out = AnthropicAdapter().from_ir(req, Dropped())
        self.assertEqual(out["thinking"], {"type": "enabled", "budget_tokens": 1024})
        self.assertEqual(out["tool_choice"], {"type": "auto"})


class TestStatefulChainE2E(unittest.TestCase):
    """端到端：真起网关 + mock 后端，验多轮 previous_response_id 链路。

    这条链路是「为什么必须有独立状态层」的论据所在，而单测覆盖不到它：
    单测里的 history 都是测试自己 append 的，网关自己从未写过历史。
    修复前网关只 record_response 不 append，会话历史恒为空 —— 第二轮
    重放出 0 条，多轮链路在第一轮之后就断了。
    """

    @classmethod
    def setUpClass(cls):
        import tempfile
        from http.server import ThreadingHTTPServer
        from src.gateway import server as gw
        from src.observability.metrics import MetricsLog
        from tools import mock_backend

        cls.td = tempfile.TemporaryDirectory()
        cls.gw = gw
        cls.orig_store = gw.STORE  # 模块导入时建的那个（data/sessions.db），收尾要关
        gw.STORE = SessionStore(gw.CFG, db_path=os.path.join(cls.td.name, "s.db"))
        gw.METRICS = MetricsLog(os.path.join(cls.td.name, "m.jsonl"))
        gw.DIRECT = True  # 绕过系统代理

        cls.mock = ThreadingHTTPServer(("127.0.0.1", 0), mock_backend.Handler)
        threading.Thread(target=cls.mock.serve_forever, daemon=True).start()
        gw.BACKEND_URL = f"http://127.0.0.1:{cls.mock.server_address[1]}"

        cls.gwsrv = ThreadingHTTPServer(("127.0.0.1", 0), gw.Handler)
        cls.url = f"http://127.0.0.1:{cls.gwsrv.server_address[1]}"
        threading.Thread(target=cls.gwsrv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.gwsrv.shutdown()
        cls.gwsrv.server_close()
        cls.mock.shutdown()
        cls.mock.server_close()
        cls.gw.STORE.shutdown()
        cls.orig_store.shutdown()
        cls.td.cleanup()

    def _post(self, path: str, payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url + path, data=data,
                                     headers={"Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_multiturn_previous_response_id_chain(self):
        r1 = self._post("/v1/openai_response/to/openai_response", {
            "model": "mock",
            "input": [{"type": "message", "role": "user", "content": "第一轮问题"}],
        })
        rid = r1.get("id")
        # 不能只断言前缀 —— mock 的 id 恰好也是 resp_ 开头，会假通过。
        # 真正的性质是：这个 id 网关自己解得回会话。
        self.assertIsNotNone(self.gw.STORE.resolve_previous(rid),
                             f"网关回的 response_id 必须自己能解析，实际 {rid!r}")
        self.assertEqual(r1["_bridge"]["replayed_messages"], 0, "首轮无历史可重放")

        r2 = self._post("/v1/openai_response/to/openai_response", {
            "model": "mock",
            "previous_response_id": rid,
            "input": [{"type": "message", "role": "user", "content": "第二轮问题"}],
        })
        self.assertEqual(r2["_bridge"]["replayed_messages"], 2,
                         "第 2 轮应重放 2 条（上轮 user + assistant）")
        self.assertTrue(r2["_bridge"]["session"])

        r3 = self._post("/v1/openai_response/to/openai_response", {
            "model": "mock",
            "previous_response_id": r2.get("id"),
            "input": [{"type": "message", "role": "user", "content": "第三轮问题"}],
        })
        self.assertEqual(r3["_bridge"]["replayed_messages"], 4,
                         "第 3 轮应重放 4 条，历史必须持续累积而非恒为空")

    def test_warmup_not_registered_as_turn(self):
        """预热轮不进响应链，也不该被当成一轮对话重放。"""
        w = prewarm.build_warmup_request("sys", "mock")
        rw = self._post("/v1/anthropic/to/anthropic", w)
        self.assertTrue(rw["_bridge"]["warmup"])
        self.assertEqual(rw["usage"]["output_tokens"], 0)
        self.assertFalse(rw.get("id", "").startswith("resp_"),
                         "预热轮不应登记进 previous_response_id 响应链")


def _count_breakpoints(payload: dict) -> int:
    n = 0
    for t in payload.get("tools", []):
        n += "cache_control" in t
    for b in payload.get("system", []):
        n += "cache_control" in b
    for m in payload.get("messages", []):
        for b in m.get("content", []):
            n += "cache_control" in b
    return n


if __name__ == "__main__":
    unittest.main()
