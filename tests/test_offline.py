"""离线单测：IR 往返 / adapter 映射 / 状态层 / 预热字段处理。

全部用内联录制样例，不依赖真实 API。运行：
  python -m unittest tests.test_offline -v   （或 pytest tests/ -q）
"""
import json
import os
import random
import string
import sys
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.adapters.base import Dropped, assistant_from_upstream
from src.adapters.chat import ChatAdapter, usage_from_chat
from src.adapters.anthropic import AnthropicAdapter, usage_from_anthropic
from src.adapters.response import ResponseAdapter
from src.gateway.sse import AnthropicToChatStream, parse_sse_lines
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

    def test_lazy_eviction_throttled(self):
        """LIMITATIONS #11 回归：惰性淘汰按间隔节流，不每请求全表扫描。"""
        cfg = SessionConfig(end_policy="ttl", ttl_seconds=1, on_end="archive")
        store = SessionStore(cfg, db_path=":memory:")
        self.addCleanup(store.shutdown)
        t0 = time.time()
        s = store.get_or_create("task-old")
        s.touched_at = t0 - 100
        store._save(s)
        # get_or_create 本身已触发过一次实时惰性淘汰（节流窗由此刻起算），
        # 重置 _last_evict 以便用显式 now 做确定性验证
        store._last_evict = 0
        # 首次触发：淘汰 1 个超时会话
        self.assertEqual(store.maybe_evict(now=t0), 1)
        # 节流窗口内（< 60s）：不再扫描，即使又有新超时会话
        s2 = store.get_or_create("task-old2")
        s2.touched_at = t0 - 100
        store._save(s2)
        self.assertEqual(store.maybe_evict(now=t0 + 30), 0)
        # 窗口过后：恢复淘汰
        self.assertEqual(store.maybe_evict(now=t0 + 61), 1)

    def test_get_or_create_triggers_lazy_eviction(self):
        """LIMITATIONS #11 回归：正常读写路径（get_or_create）会顺手淘汰超时会话。"""
        cfg = SessionConfig(end_policy="ttl", ttl_seconds=1, on_end="archive")
        store = SessionStore(cfg, db_path=":memory:")
        self.addCleanup(store.shutdown)
        s = store.get_or_create("task-expired")
        s.touched_at -= 100
        store._save(s)
        store._last_evict = 0  # 绕过节流，模拟「距上次淘汰已很久」
        store.get_or_create("task-new")  # 任意流量即触发
        self.assertTrue(store._load("task-expired").closed)
        self.assertFalse(store._load("task-new").closed)

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


class TestCrossProtocolToolArgs(unittest.TestCase):
    """第三轮自查回归 #1：跨协议工具调用参数丢失（v1.4 修复）。

    OpenAI 系 arguments 是 JSON 字符串、Anthropic input 是 dict。v1.3 之前
    chat/response 的 to_ir 只把字符串塞进 extra、tool_input 恒为 None，
    转到 Anthropic 全部渲染成空 input {}——工具全链路跨协议断。
    """

    def test_chat_to_anthropic_keeps_tool_input(self):
        payload = {"model": "gpt-x", "messages": [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {
                    "name": "search", "arguments": '{"city": "天津", "n": 3}'}}]},
        ]}
        req = ChatAdapter().to_ir(payload, Dropped())
        b = req.messages[0].blocks[0]
        self.assertEqual(b.tool_input, {"city": "天津", "n": 3},
                         "arguments 字符串必须解析成 dict 进 tool_input")
        out = AnthropicAdapter().from_ir(req, Dropped())
        tu = out["messages"][0]["content"][0]
        self.assertEqual(tu["input"], {"city": "天津", "n": 3},
                         "Chat→Anthropic 的 tool_use.input 不得为空 {}")

    def test_anthropic_to_chat_keeps_arguments(self):
        payload = {"model": "claude-x", "max_tokens": 100, "messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "search",
                 "input": {"q": "犀牛鸟"}}]},
        ]}
        req = AnthropicAdapter().to_ir(payload, Dropped())
        out = ChatAdapter().from_ir(req, Dropped())
        args = out["messages"][0]["tool_calls"][0]["function"]["arguments"]
        self.assertEqual(json.loads(args), {"q": "犀牛鸟"},
                         "Anthropic→Chat 的 arguments 不得退化成 '{}'")

    def test_response_to_anthropic_keeps_tool_input(self):
        payload = {"model": "gpt-x", "input": [
            {"type": "function_call", "name": "calc", "call_id": "call_9",
             "arguments": '{"x": 1}'}]}
        req = ResponseAdapter().to_ir(payload, Dropped())
        out = AnthropicAdapter().from_ir(req, Dropped())
        self.assertEqual(out["messages"][0]["content"][0]["input"], {"x": 1})

    def test_same_protocol_arguments_byte_lossless(self):
        """同协议往返仍走 extra 原始字符串，字节无损（不受 v1.4 解析影响）。"""
        raw = '{"a":  1, "b": [1,2]}'  # 带非常规空格
        payload = {"model": "gpt-x", "messages": [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {
                    "name": "f", "arguments": raw}}]},
        ]}
        req = ChatAdapter().to_ir(payload, Dropped())
        out = ChatAdapter().from_ir(req, Dropped())
        self.assertEqual(out["messages"][0]["tool_calls"][0]["function"]["arguments"], raw)


class TestThinkingSignature(unittest.TestCase):
    """第三轮自查回归 #2：thinking signature / redacted_thinking 往返（v1.4 修复）。

    Anthropic 开启 thinking 后下一轮必须原样回传 signature，redacted_thinking
    必须逐字节透传，否则直接 400。v1.3 之前 to_ir 把两者都丢了。
    """

    def test_thinking_signature_roundtrip(self):
        payload = {"model": "claude-x", "max_tokens": 100, "messages": [
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "推理过程",
                 "signature": "sig_abc123"},
                {"type": "text", "text": "答案"}]},
        ]}
        req = AnthropicAdapter().to_ir(payload, Dropped())
        out = AnthropicAdapter().from_ir(req, Dropped())
        blk = out["messages"][0]["content"][0]
        self.assertEqual(blk.get("signature"), "sig_abc123",
                         "thinking 的 signature 往返必须保留")

    def test_redacted_thinking_byte_exact(self):
        payload = {"model": "claude-x", "max_tokens": 100, "messages": [
            {"role": "assistant", "content": [
                {"type": "redacted_thinking", "data": "enc_逐字节数据=="}]},
        ]}
        req = AnthropicAdapter().to_ir(payload, Dropped())
        self.assertEqual(len(req.messages[0].blocks), 1,
                         "redacted_thinking 不得被判成无效块丢弃")
        out = AnthropicAdapter().from_ir(req, Dropped())
        blk = out["messages"][0]["content"][0]
        self.assertEqual(blk, {"type": "redacted_thinking", "data": "enc_逐字节数据=="})


class TestAssistantFromUpstream(unittest.TestCase):
    """v1.4：状态层重放原料保留结构化块（thinking / tool_use），不再只取文本。"""

    def test_anthropic_structured_blocks(self):
        resp = {"content": [
            {"type": "thinking", "thinking": "想", "signature": "s1"},
            {"type": "text", "text": "答"},
            {"type": "tool_use", "id": "toolu_7", "name": "search",
             "input": {"q": "x"}}]}
        msg = assistant_from_upstream("anthropic", resp)
        kinds = [b.kind for b in msg.blocks]
        self.assertEqual(kinds, [ir.THINKING, ir.TEXT, ir.TOOL_USE])
        self.assertEqual(msg.blocks[0].extra["signature"], "s1")
        self.assertEqual(msg.blocks[2].tool_input, {"q": "x"})

    def test_chat_tool_calls(self):
        resp = {"choices": [{"message": {
            "content": None,
            "tool_calls": [{"id": "call_3", "type": "function", "function": {
                "name": "f", "arguments": '{"k": 2}'}}]}}]}
        msg = assistant_from_upstream("openai_chat", resp)
        self.assertEqual(len(msg.blocks), 1)
        self.assertEqual(msg.blocks[0].kind, ir.TOOL_USE)
        self.assertEqual(msg.blocks[0].tool_input, {"k": 2})

    def test_response_function_call_and_reasoning(self):
        resp = {"output": [
            {"type": "reasoning", "summary": "想了一下",
             "encrypted_content": "enc1"},
            {"type": "function_call", "name": "f", "call_id": "call_5",
             "arguments": '{"y": true}'},
            {"type": "message", "content": [
                {"type": "output_text", "text": "好"}]}]}
        msg = assistant_from_upstream("openai_response", resp)
        kinds = [b.kind for b in msg.blocks]
        self.assertEqual(kinds, [ir.THINKING, ir.TOOL_USE, ir.TEXT])
        self.assertEqual(msg.blocks[1].tool_input, {"y": True})

    def test_empty_response_returns_none(self):
        self.assertIsNone(assistant_from_upstream("anthropic", {"content": []}))
        self.assertIsNone(assistant_from_upstream("openai_chat", {}))


class TestToolIdMap(unittest.TestCase):
    """v1.4：工具 ID 双向持久映射（问题清单组4#3）。"""

    def setUp(self):
        self.store = SessionStore(SessionConfig(), ":memory:")
        self.idm = self.store.idmap

    def test_same_proto_outgoing_is_byte_lossless(self):
        canon = self.idm.incoming("s1", "toolu_abc", "anthropic")
        self.assertEqual(canon, "toolu_abc")
        self.assertEqual(self.idm.outgoing("s1", canon, "anthropic"), "toolu_abc",
                         "同协议 outgoing 必须原样返回 canonical")

    def test_cross_proto_mint_stable_and_prefix(self):
        self.idm.incoming("s1", "toolu_abc", "anthropic")
        e1 = self.idm.outgoing("s1", "toolu_abc", "openai_chat")
        self.assertTrue(e1.startswith("call_"), "Chat 侧铸造 ID 应用 call_ 前缀")
        e2 = self.idm.outgoing("s1", "toolu_abc", "openai_chat")
        self.assertEqual(e1, e2, "同一会话内铸造结果必须稳定（前缀序列化不抖动）")

    def test_roundtrip_restores_original_id(self):
        """Anthropic→Chat→Anthropic 一圈，最初的 toolu_* 必须还原（否则上游 400）。"""
        canon = self.idm.incoming("s1", "toolu_orig", "anthropic")
        chat_ext = self.idm.outgoing("s1", canon, "openai_chat")
        back = self.idm.incoming("s1", chat_ext, "openai_chat")
        self.assertEqual(back, canon, "外部形式必须能反解回 canonical")
        self.assertEqual(self.idm.outgoing("s1", back, "anthropic"), "toolu_orig")

    def test_sessions_isolated(self):
        self.idm.incoming("s1", "toolu_abc", "anthropic")
        self.assertEqual(self.idm.incoming("s2", "toolu_abc", "anthropic"), "toolu_abc")
        e1 = self.idm.outgoing("s1", "toolu_abc", "openai_chat")
        e2 = self.idm.outgoing("s2", "toolu_abc", "openai_chat")
        self.assertNotEqual(e1, e2, "不同会话的铸造互不影响（并发分叉不串号）")

    def test_shared_db_and_size(self):
        self.idm.incoming("s1", "call_1", "openai_chat")
        self.idm.outgoing("s1", "call_1", "anthropic")
        self.assertEqual(self.idm.size("s1"), 2)


class TestPropertyRoundTrip(unittest.TestCase):
    """问题清单组5#10：property-based 往返测试（stdlib 实现，零三方依赖）。

    与录制样例的例举式测试互补：固定种子生成随机请求，断言**性质**而非样例——
    ① 断点数恒在 [3,4]；② 任何未知顶层字段必进降级记录（绝不静默丢弃）；
    ③ 工具参数跨协议守恒；④ thinking signature 往返守恒。
    种子固定 → 失败可用同一种子回放现场。
    """

    SEED = 20260909

    def _rand_text(self, rng, n=40):
        return "".join(rng.choice(string.ascii_letters + "中文测试字  ")
                       for _ in range(rng.randint(1, n)))

    # -- 随机 payload 生成 -------------------------------------------------
    def _rand_chat_payload(self, rng):
        msgs = [{"role": "system", "content": self._rand_text(rng)}]
        for i in range(rng.randint(1, 6)):
            role = "user" if i % 2 == 0 else "assistant"
            m = {"role": role, "content": self._rand_text(rng)}
            if role == "assistant" and rng.random() < 0.5:
                m["tool_calls"] = [{
                    "id": f"call_{rng.randint(0, 999)}", "type": "function",
                    "function": {"name": "f", "arguments": json.dumps(
                        {"k": rng.randint(0, 100), "s": self._rand_text(rng, 8)},
                        ensure_ascii=False)}}]
            msgs.append(m)
        return {"model": "gpt-x", "messages": msgs,
                "tools": [{"type": "function", "function": {
                    "name": "f", "description": "d",
                    "parameters": {"type": "object"}}}],
                f"x_rand_{rng.randint(0, 999)}": rng.random(),
                "logit_bias": {"1": 1}}

    def _rand_anthropic_payload(self, rng):
        msgs = []
        for i in range(rng.randint(1, 5)):
            role = "user" if i % 2 == 0 else "assistant"
            content = [{"type": "text", "text": self._rand_text(rng)}]
            if role == "assistant":
                if rng.random() < 0.6:
                    content.insert(0, {"type": "thinking",
                                       "thinking": self._rand_text(rng),
                                       "signature": f"sig_{rng.randint(0, 9999)}"})
                if rng.random() < 0.5:
                    content.append({"type": "tool_use",
                                    "id": f"toolu_{rng.randint(0, 999)}",
                                    "name": "f",
                                    "input": {"v": rng.randint(0, 50)}})
            msgs.append({"role": role, "content": content})
        return {"model": "claude-x", "max_tokens": 128, "messages": msgs,
                "tools": [{"name": "f", "description": "d",
                           "input_schema": {"type": "object"}}],
                f"x_rand_{rng.randint(0, 999)}": 1, "top_p": 0.9}

    def _rand_response_payload(self, rng):
        items = []
        for i in range(rng.randint(1, 5)):
            if i % 2 == 0:
                items.append({"type": "message", "role": "user",
                              "content": self._rand_text(rng)})
            else:
                items.append({"type": "message", "role": "assistant",
                              "content": [{"type": "output_text",
                                           "text": self._rand_text(rng)}]})
                if rng.random() < 0.5:
                    items.append({"type": "function_call", "name": "f",
                                  "call_id": f"call_{rng.randint(0, 999)}",
                                  "arguments": json.dumps(
                                      {"v": rng.randint(0, 50)})})
        return {"model": "gpt-x", "input": items,
                f"x_rand_{rng.randint(0, 999)}": "x"}

    # -- 性质断言 -----------------------------------------------------------
    def test_property_chat_to_anthropic(self):
        rng = random.Random(self.SEED)
        for case in range(120):
            p = self._rand_chat_payload(rng)
            d1 = Dropped()
            req = ChatAdapter().to_ir(p, d1)
            # 性质②：未知顶层字段必降级（chat._KNOWN_TOP 之外一律在案）
            known = {"model", "messages", "tools", "max_tokens",
                     "max_completion_tokens", "temperature", "stream", "system"}
            dropped_fields = {d["field"] for d in d1.items}
            for k in p:
                if k not in known:
                    self.assertIn(k, dropped_fields,
                                  f"case {case}: 未知字段 {k} 被静默丢弃")
            # 性质①：断点上限
            out = AnthropicAdapter().from_ir(req, Dropped(), ir.SessionContext())
            bps = _count_breakpoints(out)
            self.assertTrue(3 <= bps <= 4, f"case {case}: 断点数 {bps} 越界")
            # 性质③：工具参数守恒（v1.4 修复的参数丢失正是这条性质被抓出来的）
            src = [json.loads(tc["function"]["arguments"])
                   for m in p["messages"] for tc in m.get("tool_calls", []) or []]
            got = [b["input"] for m in out["messages"] for b in m["content"]
                   if b.get("type") == "tool_use"]
            self.assertEqual(src, got, f"case {case}: 工具参数跨协议变形")

    def test_property_anthropic_to_chat(self):
        rng = random.Random(self.SEED + 1)
        for case in range(120):
            p = self._rand_anthropic_payload(rng)
            d1 = Dropped()
            req = AnthropicAdapter().to_ir(p, d1)
            known = {"model", "max_tokens", "temperature", "stream",
                     "system", "messages", "tools", "thinking", "tool_choice"}
            dropped_fields = {d["field"] for d in d1.items}
            for k in p:
                if k not in known:
                    self.assertIn(k, dropped_fields,
                                  f"case {case}: 未知字段 {k} 被静默丢弃")
            out = ChatAdapter().from_ir(req, Dropped())
            # 性质③：tool_use.input → arguments JSON 守恒
            src_inputs = [b["input"] for m in p["messages"]
                          for b in m["content"] if b.get("type") == "tool_use"]
            got_args = [json.loads(tc["function"]["arguments"])
                        for m in out["messages"]
                        for tc in m.get("tool_calls", []) or []]
            self.assertEqual(src_inputs, got_args,
                             f"case {case}: Anthropic→Chat 参数变形")

    def test_property_anthropic_signature_roundtrip(self):
        rng = random.Random(self.SEED + 2)
        for case in range(80):
            p = self._rand_anthropic_payload(rng)
            req = AnthropicAdapter().to_ir(p, Dropped())
            out = AnthropicAdapter().from_ir(req, Dropped())
            # 性质④：signature 有序守恒（多轮思考链的硬约束）
            src_sigs = [b["signature"] for m in p["messages"]
                        for b in m["content"] if b.get("type") == "thinking"]
            got_sigs = [b.get("signature") for m in out["messages"]
                        for b in m["content"] if b.get("type") == "thinking"]
            self.assertEqual(src_sigs, got_sigs,
                             f"case {case}: thinking signature 往返丢失")

    def test_property_response_to_anthropic(self):
        rng = random.Random(self.SEED + 3)
        for case in range(100):
            p = self._rand_response_payload(rng)
            d1 = Dropped()
            req = ResponseAdapter().to_ir(p, d1)
            dropped_fields = {d["field"] for d in d1.items}
            for k in p:
                if k not in ("model", "input"):
                    self.assertIn(k, dropped_fields,
                                  f"case {case}: 未知字段 {k} 被静默丢弃")
            out = AnthropicAdapter().from_ir(req, Dropped(), ir.SessionContext())
            src = [json.loads(it["arguments"]) for it in p["input"]
                   if it.get("type") == "function_call"]
            got = [b["input"] for m in out["messages"] for b in m["content"]
                   if b.get("type") == "tool_use"]
            self.assertEqual(src, got, f"case {case}: Responses→Anthropic 参数变形")


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


class TestSseConversion(unittest.TestCase):
    """LIMITATIONS #1 最小闭环：Anthropic SSE → Chat chunk 逐块转换。"""

    @staticmethod
    def _feed_seq(conv, events):
        """按序喂事件，把每事件产出的帧全部收集。"""
        frames = []
        for ev in events:
            frames.extend(conv.feed(ev.get("type"), ev))
        return frames

    @staticmethod
    def _parse_frame(frame: str):
        assert frame.startswith("data: ") and frame.endswith("\n\n")
        return json.loads(frame[len("data:"):].strip())

    def test_parse_sse_lines_basic(self):
        raw = (
            'event: message_start\n'
            'data: {"type":"message_start","message":{"id":"msg_1"}}\n'
            '\n'
            ': heartbeat-comment\n'
            'data: {"type":"ping"}\n'
            '\n'
            'data: 非JSON行\n'
            '\n'
        ).encode("utf-8")
        events = list(parse_sse_lines(iter(raw.splitlines(keepends=True))))
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0], ("message_start",
                                     {"type": "message_start",
                                      "message": {"id": "msg_1"}}))
        self.assertEqual(events[1][1], {"type": "ping"})   # 注释行被跳过
        self.assertEqual(events[2][1], "非JSON行")          # 非 JSON 原样透传

    def test_parse_sse_lines_partial_tail_dropped(self):
        raw = b'data: {"type":"message_stop"}\n\ndata: {"type":"truncat'
        events = list(parse_sse_lines(iter(raw.splitlines(keepends=True))))
        self.assertEqual(len(events), 1)  # 尾部半帧容错丢弃，不崩

    def test_text_delta_streams_immediately(self):
        conv = AnthropicToChatStream()
        frames = self._feed_seq(conv, [
            {"type": "message_start", "message": {
                "id": "msg_x", "model": "claude-mock",
                "usage": {"input_tokens": 100, "cache_read_input_tokens": 80}}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "你"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "好"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
             "usage": {"output_tokens": 7}},
            {"type": "message_stop"},
        ])
        # role + 2 文本帧 + finish + DONE：文本逐块即时下发，不等流尾
        self.assertEqual(len(frames), 5)
        first = self._parse_frame(frames[0])
        self.assertEqual(first["choices"][0]["delta"], {"role": "assistant"})
        self.assertEqual(first["id"], "msg_x")
        self.assertEqual(first["model"], "claude-mock")
        self.assertEqual(first["object"], "chat.completion.chunk")
        texts = [self._parse_frame(f)["choices"][0]["delta"].get("content", "")
                 for f in frames[1:3]]
        self.assertEqual("".join(texts), "你好")
        fin = self._parse_frame(frames[3])
        self.assertEqual(fin["choices"][0]["finish_reason"], "stop")
        self.assertEqual(frames[4], "data: [DONE]\n\n")

    def test_tool_args_buffered_until_block_stop(self):
        """input_json_delta 是非完整 JSON 片段：缓冲到 content_block_stop 一次性发。"""
        conv = AnthropicToChatStream()
        f1 = conv.feed("content_block_start",
                       {"type": "content_block_start", "index": 1,
                        "content_block": {"type": "tool_use",
                                          "id": "toolu_1", "name": "get_weather"}})
        self.assertEqual(f1, [])  # 工具块开始不落帧
        f2 = conv.feed("content_block_delta",
                       {"type": "content_block_delta", "index": 1,
                        "delta": {"type": "input_json_delta",
                                  "partial_json": '{"city": "天'}})
        f3 = conv.feed("content_block_delta",
                       {"type": "content_block_delta", "index": 1,
                        "delta": {"type": "input_json_delta",
                                  "partial_json": '津"}'}})
        self.assertEqual(f2 + f3, [])  # 参数片段期间不落帧（无法回避的折损）
        f4 = conv.feed("content_block_stop",
                       {"type": "content_block_stop", "index": 1})
        self.assertEqual(len(f4), 1)
        chunk = self._parse_frame(f4[0])
        tc = chunk["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual(tc["id"], "toolu_1")
        self.assertEqual(tc["function"]["name"], "get_weather")
        self.assertEqual(tc["function"]["arguments"], '{"city": "天津"}')
        # 落库原料：工具参数被解析回 dict
        synth = conv.synthetic_response()
        tool_blocks = [b for b in synth["content"] if b["type"] == "tool_use"]
        self.assertEqual(tool_blocks[0]["input"], {"city": "天津"})

    def test_stop_reason_mapping(self):
        for anthropic_reason, chat_reason in (
                ("end_turn", "stop"), ("max_tokens", "length"),
                ("tool_use", "tool_calls")):
            conv = AnthropicToChatStream()
            frames = conv.feed("message_delta",
                               {"type": "message_delta",
                                "delta": {"stop_reason": anthropic_reason},
                                "usage": {"output_tokens": 3}})
            chunk = self._parse_frame(frames[0])
            self.assertEqual(chunk["choices"][0]["finish_reason"], chat_reason)

    def test_usage_merge_and_history_reuse(self):
        """usage 分次到达（start 带 input+缓存、delta 带 output），流尾合并；
        synthetic_response 直接复用 assistant_from_upstream 落库。"""
        conv = AnthropicToChatStream()
        self._feed_seq(conv, [
            {"type": "message_start", "message": {
                "id": "msg_u", "model": "m",
                "usage": {"input_tokens": 5978,
                          "cache_creation_input_tokens": 5978,
                          "cache_read_input_tokens": 0}}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "答复"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
             "usage": {"output_tokens": 12}},
        ])
        u = usage_from_anthropic(conv.usage())
        self.assertEqual(u.input_tokens, 5978)
        self.assertEqual(u.cache_creation_input_tokens, 5978)
        self.assertEqual(u.output_tokens, 12)
        reply = assistant_from_upstream("anthropic", conv.synthetic_response())
        self.assertIsNotNone(reply)
        self.assertEqual(reply.blocks[0].text, "答复")

    def test_upstream_error_frame(self):
        conv = AnthropicToChatStream()
        frames = conv.feed("error", {"type": "error", "error": {
            "type": "overloaded_error", "message": "Overloaded"}})
        chunk = self._parse_frame(frames[0])
        self.assertIn("Overloaded",
                      chunk["choices"][0]["delta"].get("content", ""))


if __name__ == "__main__":
    unittest.main()
