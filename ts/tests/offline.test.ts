/** 离线单测：IR 往返 / adapter 映射 / 状态层 / 预热 / SSE —— 镜像 Python 版 56 项。
 * （TS 移植自 tests/test_offline.py；运行：npm test）
 *
 * 与 Python 版的已知差异：property 测试的随机数生成器是 mulberry32（Node 无内置
 * Mersenne Twister），种子固定但序列与 Python 不同——断言的四条性质不变。
 */

import { describe, test, before, after } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { mkdtempSync, rmSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import * as ir from "../src/ir/model.ts";
import { Dropped, assistantFromUpstream } from "../src/adapters/base.ts";
import { ChatAdapter, usageFromChat } from "../src/adapters/chat.ts";
import { AnthropicAdapter, usageFromAnthropic } from "../src/adapters/anthropic.ts";
import { ResponseAdapter } from "../src/adapters/response.ts";
import { SessionConfig, buildPrefix, load,
         FULL, LAST_BREAKPOINT, SLIDING_WINDOW } from "../src/state/session_config.ts";
import { SessionStore, Session } from "../src/state/store.ts";
import * as prewarm from "../src/warmup/prewarm.ts";
import { makeTurnMetrics, hitRate, MetricsLog,
         NORMAL, WARMUP } from "../src/observability/metrics.ts";
import { AnthropicToChatStream, ChatStreamCollector, parseSseLines } from "../src/gateway/sse.ts";
import * as gwmod from "../src/gateway/server.ts";
import { mockHandler } from "../tools/mock_backend.ts";

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------

function countBreakpoints(payload: ir.Json): number {
  let n = 0;
  for (const t of payload.tools ?? []) n += "cache_control" in t ? 1 : 0;
  for (const b of payload.system ?? []) n += "cache_control" in b ? 1 : 0;
  for (const m of payload.messages ?? []) {
    for (const b of m.content ?? []) n += "cache_control" in b ? 1 : 0;
  }
  return n;
}

/** mulberry32：定种子 PRNG（Node 无内置 MT；性质测试只要求确定性）。 */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

class Rng {
  private next: () => number;
  constructor(seed: number) { this.next = mulberry32(seed); }
  random(): number { return this.next(); }
  randint(lo: number, hi: number): number {
    return lo + Math.floor(this.next() * (hi - lo + 1));
  }
  choice<T>(arr: T[]): T { return arr[Math.floor(this.next() * arr.length)]; }
}

// ---------------------------------------------------------------------------
// TestAdapterRoundTrip
// ---------------------------------------------------------------------------

describe("TestAdapterRoundTrip", () => {
  test("chat→anthropic 断点数 ≤4 且 ≥3", () => {
    const payload = {
      model: "gpt-x",
      messages: [
        { role: "system", content: "sys" },
        { role: "user", content: "hi" },
        { role: "assistant", content: "hello" },
        { role: "user", content: "again" },
      ],
      tools: [{ type: "function", function: { name: "search", description: "d", parameters: {} } }],
    };
    const req = new ChatAdapter().to_ir(payload, new Dropped());
    assert.equal(req.system.length, 1);
    assert.equal(req.messages.length, 3);
    assert.equal(req.tools.length, 1);

    const ctx = new ir.SessionContext(); // 默认 3 固定 + 1 滚动
    const out = new AnthropicAdapter().from_ir(req, new Dropped(), ctx);
    const bps = countBreakpoints(out);
    assert.ok(bps <= 4, "cache_control 断点不得超过官方 4 个上限");
    assert.ok(bps >= 3, "3 固定 + 1 滚动应至少 3 个");
  });

  test("usage 三口径归一", () => {
    const a = usageFromAnthropic({ input_tokens: 8, output_tokens: 0,
                                   cache_creation_input_tokens: 5120,
                                   cache_read_input_tokens: 0 });
    assert.equal(a.total_input, 5128);
    const c = usageFromChat({ prompt_tokens: 2006, completion_tokens: 300,
                              prompt_tokens_details: { cached_tokens: 1920 } });
    assert.equal(c.cache_read_input_tokens, 1920);
  });
});

// ---------------------------------------------------------------------------
// TestStateLayer
// ---------------------------------------------------------------------------

describe("TestStateLayer", () => {
  test("previous_response_id 重放", () => {
    const store = new SessionStore(new SessionConfig(), ":memory:");
    const s = store.getOrCreate("task-1");
    store.append(s, [ir.Message.text("user", "u1"), ir.Message.text("assistant", "a1")]);
    const rid = store.recordResponse(s);
    // 下一轮带 previous_response_id，应解析回同一 session 并重放历史
    const s2 = store.resolvePrevious(rid);
    assert.ok(s2 !== null);
    assert.equal(store.replay(s2).length, 2);
    store.shutdown();
  });

  test("build_prefix 三策略", () => {
    const hist = Array.from({ length: 5 }, (_, i) => ir.Message.text("user", `m${i}`));
    let cfg = new SessionConfig({ replay_from: FULL });
    assert.equal(buildPrefix(hist, 0, cfg).length, 5);
    cfg = new SessionConfig({ replay_from: LAST_BREAKPOINT });
    assert.equal(buildPrefix(hist, 3, cfg).length, 2);
    cfg = new SessionConfig({ replay_from: SLIDING_WINDOW, sliding_window_n: 2 });
    assert.equal(buildPrefix(hist, 0, cfg).length, 2);
  });

  test("TTL 淘汰", () => {
    const cfg = new SessionConfig({ end_policy: "ttl", ttl_seconds: 1, on_end: "archive" });
    const store = new SessionStore(cfg, ":memory:");
    const s = store.getOrCreate("task-x");
    s.touched_at -= 10; // 假装已超时（持久化层需写回才生效）
    store._save(s);
    assert.equal(store.evictExpired(), 1);
    store.shutdown();
  });

  test("惰性淘汰按间隔节流（LIMITATIONS #11 回归）", () => {
    const cfg = new SessionConfig({ end_policy: "ttl", ttl_seconds: 1, on_end: "archive" });
    const store = new SessionStore(cfg, ":memory:");
    const t0 = Date.now() / 1000;
    const s = store.getOrCreate("task-old");
    s.touched_at = t0 - 100;
    store._save(s);
    // getOrCreate 本身已触发过一次实时惰性淘汰（节流窗由此刻起算），
    // 重置 _lastEvict 以便用显式 now 做确定性验证
    store._lastEvict = 0;
    // 首次触发：淘汰 1 个超时会话
    assert.equal(store.maybeEvict(t0), 1);
    // 节流窗口内（< 60s）：不再扫描，即使又有新超时会话
    const s2 = store.getOrCreate("task-old2");
    s2.touched_at = t0 - 100;
    store._save(s2);
    assert.equal(store.maybeEvict(t0 + 30), 0);
    // 窗口过后：恢复淘汰
    assert.equal(store.maybeEvict(t0 + 61), 1);
    store.shutdown();
  });

  test("正常读写路径顺手淘汰超时会话（LIMITATIONS #11 回归）", () => {
    const cfg = new SessionConfig({ end_policy: "ttl", ttl_seconds: 1, on_end: "archive" });
    const store = new SessionStore(cfg, ":memory:");
    const s = store.getOrCreate("task-expired");
    s.touched_at -= 100;
    store._save(s);
    store._lastEvict = 0; // 绕过节流，模拟「距上次淘汰已很久」
    store.getOrCreate("task-new"); // 任意流量即触发
    assert.equal(store._load("task-expired")!.closed, true);
    assert.equal(store._load("task-new")!.closed, false);
    store.shutdown();
  });

  test("SQLite 重启不丢（换连接后会话与 meta 仍在）", () => {
    const td = mkdtempSync(join(tmpdir(), "pb-ts-"));
    try {
      const db = join(td, "s.db");
      const cfg = new SessionConfig();
      const st1 = new SessionStore(cfg, db);
      const s = st1.getOrCreate("task-p");
      st1.append(s, [ir.Message.text("user", "u1")]);
      st1.updateMeta("task-p", { memories: [{ content: "记住：偏好简洁" }], memory_cap: 3 });
      const rid = st1.recordResponse(s);
      // 模拟进程重启：新实例读同一文件
      const st2 = new SessionStore(cfg, db);
      const s2 = st2.resolvePrevious(rid);
      assert.ok(s2 !== null);
      assert.equal(st2.replay(s2).length, 1);
      assert.equal(s2.meta.memory_cap, 3);
      assert.equal(s2.meta.memories[0].content, "记住：偏好简洁");
      st1.shutdown();
      st2.shutdown();
    } finally {
      rmSync(td, { recursive: true, force: true });
    }
  });
});

// ---------------------------------------------------------------------------
// TestWarmup
// ---------------------------------------------------------------------------

describe("TestWarmup", () => {
  test("预热构造与校验", () => {
    const w = prewarm.buildWarmupRequest("system prompt", "claude-opus-4-8");
    assert.equal(w.max_tokens, 0);
    assert.equal(w.messages[0].content, prewarm.PLACEHOLDER);
    assert.deepEqual(prewarm.validateWarmup(w), []);
    // 断点打在 system 而非占位消息
    assert.ok("cache_control" in w.system[0]);
    assert.ok(!("cache_control" in w.messages[0]));
  });

  test("冲突参数被拒", () => {
    const bad = { max_tokens: 0, stream: true, tool_choice: { type: "any" } };
    assert.ok(prewarm.validateWarmup(bad).includes("stream"));
    assert.ok(prewarm.validateWarmup(bad).includes("tool_choice"));
  });

  test("畸形预热响应解析", () => {
    const body = { id: "msg_1", model: "m", content: [],
                   stop_reason: "max_tokens",
                   usage: { input_tokens: 8, output_tokens: 0,
                            cache_creation_input_tokens: 5120 } };
    assert.ok(prewarm.isWarmupResponse(body));
    const r = prewarm.parseWarmupResponse(body);
    assert.ok(r.is_warmup);
    assert.deepEqual(r.blocks, []);
    assert.equal(r.usage.cache_creation_input_tokens, 5120);
  });
});

// ---------------------------------------------------------------------------
// TestSessionConfig
// ---------------------------------------------------------------------------

describe("TestSessionConfig", () => {
  test("加载默认值与热切换", () => {
    const cfg = load(); // 从 config/session.json
    assert.equal(cfg.replay_from, "full");
    const snap = cfg.snapshot();
    assert.ok("replay_from" in snap);
    // 热切换重放策略，不动其余配置
    cfg.replay_from = "sliding_window";
    assert.equal(cfg.replay_from, "sliding_window");
    assert.equal(cfg.key_granularity, "single_task");
  });
});

// ---------------------------------------------------------------------------
// TestGatewayPolicies
// ---------------------------------------------------------------------------

describe("TestGatewayPolicies", () => {
  test("预热拒绝条件（校验转换后报文）", () => {
    assert.ok(gwmod.isWarmup({ max_tokens: 0 }));
    assert.ok(gwmod.isWarmup({ max_output_tokens: 0 }));
    assert.ok(!gwmod.isWarmup({ max_tokens: 100 }));
    // 四类冲突（Anthropic 侧查 thinking；OpenAI 侧查 structured outputs）
    assert.ok(gwmod.validateWarmup({ stream: true }, "anthropic")!.includes("stream"));
    assert.ok(gwmod.validateWarmup({ thinking: { type: "enabled" } }, "anthropic")!.includes("thinking"));
    assert.ok(gwmod.validateWarmup({ response_format: { type: "json_schema" } }, "openai_chat")!.includes("structured"));
    assert.ok(gwmod.validateWarmup({ tool_choice: { type: "any" } }, "anthropic")!.includes("tool_choice"));
    assert.equal(gwmod.validateWarmup({ max_tokens: 0 }, "anthropic"), null);
  });

  test("记忆注入：幂等去重 + memory_cap", () => {
    const req = new ir.IRRequest({ system: [new ir.Block({ kind: ir.TEXT, text: "已有系统提示" })] });
    const s = new Session({ key: "t", meta: { memories: [
      { content: "记忆A" },
      { content: "已有系统提示" }, // 与 system 已有文本重复 → 跳过
      { content: "记忆B" },
      { content: "记忆C" },
    ], memory_cap: 2 } });
    const n = gwmod.injectMemories(req, s);
    assert.equal(n, 2, "去重后剩 3 条候选，cap=2 只注入 2 条");
    const texts = req.system.map((b) => b.text);
    assert.deepEqual(texts, ["已有系统提示", "记忆A", "记忆B"]);
    // 幂等：重复注入不再加
    assert.equal(gwmod.injectMemories(req, s), 0);
  });

  test("memory_cap 回退链：会话 meta > 全局 cfg > 不限", () => {
    const req = new ir.IRRequest();
    const s = new Session({ key: "t", meta: { memories:
      Array.from({ length: 5 }, (_, i) => ({ content: `m${i}` })) } });
    // 会话无 memory_cap → 用全局 cfg（0=不限）
    const old = gwmod.gw.CFG.session_memory_cap;
    gwmod.gw.CFG.session_memory_cap = 3;
    try {
      assert.equal(gwmod.injectMemories(req, s), 3);
    } finally {
      gwmod.gw.CFG.session_memory_cap = old;
    }
  });
});

// ---------------------------------------------------------------------------
// TestAuditFindings（两批自查 + 顺带修复的回归测试）
// ---------------------------------------------------------------------------

describe("TestAuditFindings", () => {
  test("有重放历史时打满 4 个断点", () => {
    // 修复前：bp_after_history_static 从未落地，实际只打 3 个
    const payload = {
      model: "m",
      messages: [{ role: "system", content: "sys" },
                 { role: "user", content: "u1" },
                 { role: "assistant", content: "a1" },
                 { role: "user", content: "u2" }],
      tools: [{ type: "function", function: { name: "search", description: "d", parameters: {} } }],
    };
    const req = new ChatAdapter().to_ir(payload, new Dropped());
    const ctx = new ir.SessionContext({ history: [ir.Message.text("user", "u0"),
                                                  ir.Message.text("assistant", "a0")] });
    req.messages = [...ctx.history, ...req.messages]; // 模拟重放 2 条历史
    const out = new AnthropicAdapter().from_ir(req, new Dropped(), ctx);
    assert.equal(countBreakpoints(out), 4);
  });

  test("无映射块必须进降级记录（不静默丢弃）", () => {
    const req = new ir.IRRequest({ messages: [new ir.Message({
      role: "user", blocks: [new ir.Block({ kind: ir.IMAGE, text: "[img]" })] })] });
    const d = new Dropped();
    new AnthropicAdapter().from_ir(req, d);
    assert.ok(d.items.some((i) => JSON.stringify(i).includes("image")),
              `image 块被静默丢弃且无记录：${JSON.stringify(d.items)}`);
  });

  test("预热校验对象是目标协议报文", () => {
    assert.ok(gwmod.validateWarmup({ response_format: { type: "json_object" } }, "openai_chat")!.includes("structured"));
    assert.equal(gwmod.validateWarmup({ response_format: { type: "json_object" } }, "anthropic"), null,
                 "Anthropic 上游不看 response_format，不应误报");
  });

  test("上游 4xx/5xx 抛 UpstreamHttpError（状态码不吞）", async () => {
    const srv = http.createServer((req, res) => {
      req.resume();
      req.on("end", () => {
        res.writeHead(529, { "Content-Type": "application/json" });
        res.end('{"error":"overloaded"}');
      });
    });
    await new Promise<void>((r) => srv.listen(0, "127.0.0.1", r));
    const port = (srv.address() as { port: number }).port;
    try {
      await assert.rejects(
        gwmod.postJson(`http://127.0.0.1:${port}/x`, {}, "openai_chat"),
        (e: unknown) => e instanceof gwmod.UpstreamHttpError &&
                        (e as gwmod.UpstreamHttpError).statusCode === 529);
    } finally {
      srv.close();
    }
  });

  test("重放指标只算真正重放的历史（不含本轮新消息）", () => {
    const oldStore = gwmod.gw.STORE;
    const st = new SessionStore(new SessionConfig(), ":memory:");
    gwmod.gw.STORE = st;
    try {
      const s = st.getOrCreate("k-replay");
      st.append(s, [ir.Message.text("user", "历史".repeat(20))]);
      const rid = st.recordResponse(s);
      const payload = { model: "m",
                        input: [{ type: "message", role: "user",
                                  content: "本轮".repeat(20) }],
                        previous_response_id: rid };
      const { req, replayed } = gwmod.convert(
        "openai_response", "openai_chat", payload, {});
      assert.equal(replayed.length, 1, "重放切片应只含 1 条历史");
      assert.equal(req.messages.length, 2, "本轮 1 条 + 重放 1 条");
      assert.equal(
        replayed.map((m) => m.blocks.map((b) => b.text ?? "").join("")).join(""),
        "历史".repeat(20));
    } finally {
      st.shutdown();
      gwmod.gw.STORE = oldStore;
    }
  });

  test("预热轮不进命中率分母", () => {
    const ms = [
      makeTurnMetrics({ kind: NORMAL, cache_read_input_tokens: 100 }),
      makeTurnMetrics({ kind: NORMAL, cache_read_input_tokens: 0 }),
      makeTurnMetrics({ kind: WARMUP, cache_read_input_tokens: 0 }),
    ];
    assert.ok(Math.abs(hitRate(ms) - 0.5) < 1e-9, "2 个正常轮命中 1 个");
    assert.ok(Math.abs(hitRate(ms, true) - 1 / 3) < 1e-9);
  });

  test("sliding_window n<=0 必须空窗口", () => {
    // 修复前 history[-n:] 在 n==0 时静默退化成 full
    const hist = Array.from({ length: 5 }, (_, i) => ir.Message.text("user", `m${i}`));
    const cfg = new SessionConfig({ replay_from: SLIDING_WINDOW, sliding_window_n: 0 });
    assert.deepEqual(buildPrefix(hist, 0, cfg), []);
  });

  test("Chat 未知顶层字段必进降级记录", () => {
    const payload = { model: "m",
                      messages: [{ role: "user", content: "hi" }],
                      system: "顶层系统提示",   // 非 Chat 规范字段
                      top_p: 0.9 };             // IR 无映射字段
    const d = new Dropped();
    const req = new ChatAdapter().to_ir(payload, d);
    assert.deepEqual(req.system.map((b) => b.text), ["顶层系统提示"],
                     "顶层 system 应按语义上提，语义不能丢");
    const fields = new Set(d.items.map((i) => i.field));
    assert.ok(fields.has("system"));
    assert.ok(fields.has("top_p"), "IR 无映射的顶层参数被静默丢弃");
  });

  test("previous_response_id 不回传上游", () => {
    const req = new ir.IRRequest({ extra: { previous_response_id: "resp_abc" } });
    const out = new ResponseAdapter().from_ir(req, new Dropped());
    assert.ok(!("previous_response_id" in out));
  });

  test("Anthropic thinking/tool_choice 经 extra 无损往返", () => {
    const d = new Dropped();
    const req = new AnthropicAdapter().to_ir(
      { model: "m", max_tokens: 10,
        thinking: { type: "enabled", budget_tokens: 1024 },
        tool_choice: { type: "auto" },
        messages: [{ role: "user", content: "hi" }] }, d);
    const out = new AnthropicAdapter().from_ir(req, new Dropped());
    assert.deepEqual(out.thinking, { type: "enabled", budget_tokens: 1024 });
    assert.deepEqual(out.tool_choice, { type: "auto" });
  });
});

// ---------------------------------------------------------------------------
// TestStatefulChainE2E：真起网关 + mock，验多轮 previous_response_id 链路
// ---------------------------------------------------------------------------

describe("TestStatefulChainE2E", () => {
  let td: string;
  let mockSrv: http.Server;
  let gwSrv: http.Server;
  let url: string;
  let origStore: SessionStore;
  let origMetrics: MetricsLog;
  let origBackend: string;

  before(async () => {
    td = mkdtempSync(join(tmpdir(), "pb-ts-e2e-"));
    origStore = gwmod.gw.STORE;
    origMetrics = gwmod.gw.METRICS;
    origBackend = gwmod.gw.BACKEND_URL;
    gwmod.gw.STORE = new SessionStore(gwmod.gw.CFG, join(td, "s.db"));
    gwmod.gw.METRICS = new MetricsLog(join(td, "m.jsonl"));

    mockSrv = http.createServer((req, res) => { void mockHandler(req, res); });
    await new Promise<void>((r) => mockSrv.listen(0, "127.0.0.1", r));
    const mockPort = (mockSrv.address() as { port: number }).port;
    gwmod.gw.BACKEND_URL = `http://127.0.0.1:${mockPort}`;

    gwSrv = gwmod.createServer();
    await new Promise<void>((r) => gwSrv.listen(0, "127.0.0.1", r));
    url = `http://127.0.0.1:${(gwSrv.address() as { port: number }).port}`;
  });

  after(async () => {
    gwSrv.close();
    mockSrv.close();
    gwmod.gw.STORE.shutdown();
    gwmod.gw.STORE = origStore;
    gwmod.gw.METRICS = origMetrics;
    gwmod.gw.BACKEND_URL = origBackend;
    origStore.shutdown();
    rmSync(td, { recursive: true, force: true });
  });

  async function post(path: string, payload: ir.Json): Promise<ir.Json> {
    const resp = await fetch(url + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return resp.json() as Promise<ir.Json>;
  }

  test("多轮 previous_response_id 链：历史持续累积", async () => {
    const r1 = await post("/v1/openai_response/to/openai_response", {
      model: "mock",
      input: [{ type: "message", role: "user", content: "第一轮问题" }],
    });
    const rid = r1.id;
    // 不能只断言前缀 —— mock 的 id 恰好也是 resp_ 开头，会假通过。
    // 真正的性质是：这个 id 网关自己解得回会话。
    assert.ok(gwmod.gw.STORE.resolvePrevious(rid) !== null,
              `网关回的 response_id 必须自己能解析，实际 ${rid}`);
    assert.equal(r1._bridge.replayed_messages, 0, "首轮无历史可重放");

    const r2 = await post("/v1/openai_response/to/openai_response", {
      model: "mock",
      previous_response_id: rid,
      input: [{ type: "message", role: "user", content: "第二轮问题" }],
    });
    assert.equal(r2._bridge.replayed_messages, 2,
                 "第 2 轮应重放 2 条（上轮 user + assistant）");
    assert.ok(r2._bridge.session);

    const r3 = await post("/v1/openai_response/to/openai_response", {
      model: "mock",
      previous_response_id: r2.id,
      input: [{ type: "message", role: "user", content: "第三轮问题" }],
    });
    assert.equal(r3._bridge.replayed_messages, 4,
                 "第 3 轮应重放 4 条，历史必须持续累积而非恒为空");
  });

  test("预热轮不进响应链", async () => {
    const w = prewarm.buildWarmupRequest("sys", "mock");
    const rw = await post("/v1/anthropic/to/anthropic", w);
    assert.ok(rw._bridge.warmup);
    assert.equal(rw.usage.output_tokens, 0);
    assert.ok(!String(rw.id ?? "").startsWith("resp_"),
              "预热轮不应登记进 previous_response_id 响应链");
  });
});

// ---------------------------------------------------------------------------
// TestCrossProtocolToolArgs（第三轮自查回归 #1：跨协议工具参数丢失）
// ---------------------------------------------------------------------------

describe("TestCrossProtocolToolArgs", () => {
  test("Chat→Anthropic 保留 tool_input", () => {
    const payload = { model: "gpt-x", messages: [
      { role: "assistant", content: null, tool_calls: [
        { id: "call_1", type: "function", function: {
          name: "search", arguments: '{"city": "天津", "n": 3}' } }] },
    ] };
    const req = new ChatAdapter().to_ir(payload, new Dropped());
    const b = req.messages[0].blocks[0];
    assert.deepEqual(b.tool_input, { city: "天津", n: 3 },
                     "arguments 字符串必须解析成 dict 进 tool_input");
    const out = new AnthropicAdapter().from_ir(req, new Dropped());
    const tu = out.messages[0].content[0];
    assert.deepEqual(tu.input, { city: "天津", n: 3 },
                     "Chat→Anthropic 的 tool_use.input 不得为空 {}");
  });

  test("Anthropic→Chat 保留 arguments", () => {
    const payload = { model: "claude-x", max_tokens: 100, messages: [
      { role: "assistant", content: [
        { type: "tool_use", id: "toolu_1", name: "search",
          input: { q: "犀牛鸟" } }] },
    ] };
    const req = new AnthropicAdapter().to_ir(payload, new Dropped());
    const out = new ChatAdapter().from_ir(req, new Dropped());
    const args = out.messages[0].tool_calls[0].function.arguments;
    assert.deepEqual(JSON.parse(args), { q: "犀牛鸟" },
                     "Anthropic→Chat 的 arguments 不得退化成 '{}'");
  });

  test("Responses→Anthropic 保留 tool_input", () => {
    const payload = { model: "gpt-x", input: [
      { type: "function_call", name: "calc", call_id: "call_9",
        arguments: '{"x": 1}' }] };
    const req = new ResponseAdapter().to_ir(payload, new Dropped());
    const out = new AnthropicAdapter().from_ir(req, new Dropped());
    assert.deepEqual(out.messages[0].content[0].input, { x: 1 });
  });

  test("同协议往返字节无损", () => {
    const raw = '{"a":  1, "b": [1,2]}'; // 带非常规空格
    const payload = { model: "gpt-x", messages: [
      { role: "assistant", content: null, tool_calls: [
        { id: "call_1", type: "function", function: { name: "f", arguments: raw } }] },
    ] };
    const req = new ChatAdapter().to_ir(payload, new Dropped());
    const out = new ChatAdapter().from_ir(req, new Dropped());
    assert.equal(out.messages[0].tool_calls[0].function.arguments, raw);
  });
});

// ---------------------------------------------------------------------------
// TestThinkingSignature（第三轮自查回归 #2：signature/redacted 往返）
// ---------------------------------------------------------------------------

describe("TestThinkingSignature", () => {
  test("thinking signature 往返保留", () => {
    const payload = { model: "claude-x", max_tokens: 100, messages: [
      { role: "assistant", content: [
        { type: "thinking", thinking: "推理过程", signature: "sig_abc123" },
        { type: "text", text: "答案" }] },
    ] };
    const req = new AnthropicAdapter().to_ir(payload, new Dropped());
    const out = new AnthropicAdapter().from_ir(req, new Dropped());
    const blk = out.messages[0].content[0];
    assert.equal(blk.signature, "sig_abc123", "thinking 的 signature 往返必须保留");
  });

  test("redacted_thinking 逐字节透传", () => {
    const payload = { model: "claude-x", max_tokens: 100, messages: [
      { role: "assistant", content: [
        { type: "redacted_thinking", data: "enc_逐字节数据==" }] },
    ] };
    const req = new AnthropicAdapter().to_ir(payload, new Dropped());
    assert.equal(req.messages[0].blocks.length, 1,
                 "redacted_thinking 不得被判成无效块丢弃");
    const out = new AnthropicAdapter().from_ir(req, new Dropped());
    assert.deepEqual(out.messages[0].content[0],
                     { type: "redacted_thinking", data: "enc_逐字节数据==" });
  });
});

// ---------------------------------------------------------------------------
// TestAssistantFromUpstream（状态层重放原料保留结构化块）
// ---------------------------------------------------------------------------

describe("TestAssistantFromUpstream", () => {
  test("anthropic 结构化块（thinking/text/tool_use）", () => {
    const resp = { content: [
      { type: "thinking", thinking: "想", signature: "s1" },
      { type: "text", text: "答" },
      { type: "tool_use", id: "toolu_7", name: "search", input: { q: "x" } }] };
    const msg = assistantFromUpstream("anthropic", resp)!;
    assert.deepEqual(msg.blocks.map((b) => b.kind), [ir.THINKING, ir.TEXT, ir.TOOL_USE]);
    assert.equal(msg.blocks[0].extra.signature, "s1");
    assert.deepEqual(msg.blocks[2].tool_input, { q: "x" });
  });

  test("chat tool_calls", () => {
    const resp = { choices: [{ message: {
      content: null,
      tool_calls: [{ id: "call_3", type: "function", function: {
        name: "f", arguments: '{"k": 2}' } }] } }] };
    const msg = assistantFromUpstream("openai_chat", resp)!;
    assert.equal(msg.blocks.length, 1);
    assert.equal(msg.blocks[0].kind, ir.TOOL_USE);
    assert.deepEqual(msg.blocks[0].tool_input, { k: 2 });
  });

  test("response function_call + reasoning", () => {
    const resp = { output: [
      { type: "reasoning", summary: "想了一下", encrypted_content: "enc1" },
      { type: "function_call", name: "f", call_id: "call_5", arguments: '{"y": true}' },
      { type: "message", content: [{ type: "output_text", text: "好" }] }] };
    const msg = assistantFromUpstream("openai_response", resp)!;
    assert.deepEqual(msg.blocks.map((b) => b.kind), [ir.THINKING, ir.TOOL_USE, ir.TEXT]);
    assert.deepEqual(msg.blocks[1].tool_input, { y: true });
  });

  test("空响应返回 null", () => {
    assert.equal(assistantFromUpstream("anthropic", { content: [] }), null);
    assert.equal(assistantFromUpstream("openai_chat", {}), null);
  });
});

// ---------------------------------------------------------------------------
// TestToolIdMap（工具 ID 双向持久映射）
// ---------------------------------------------------------------------------

describe("TestToolIdMap", () => {
  function makeStore() {
    const store = new SessionStore(new SessionConfig(), ":memory:");
    return { store, idm: store.idmap };
  }

  test("同协议 outgoing 字节无损", () => {
    const { store, idm } = makeStore();
    const canon = idm.incoming("s1", "toolu_abc", "anthropic");
    assert.equal(canon, "toolu_abc");
    assert.equal(idm.outgoing("s1", canon, "anthropic"), "toolu_abc",
                 "同协议 outgoing 必须原样返回 canonical");
    store.shutdown();
  });

  test("跨协议铸造：call_ 前缀 + 稳定", () => {
    const { store, idm } = makeStore();
    idm.incoming("s1", "toolu_abc", "anthropic");
    const e1 = idm.outgoing("s1", "toolu_abc", "openai_chat")!;
    assert.ok(e1.startsWith("call_"), "Chat 侧铸造 ID 应用 call_ 前缀");
    const e2 = idm.outgoing("s1", "toolu_abc", "openai_chat");
    assert.equal(e1, e2, "同一会话内铸造结果必须稳定（前缀序列化不抖动）");
    store.shutdown();
  });

  test("Anthropic→Chat→Anthropic 还原", () => {
    const { store, idm } = makeStore();
    const canon = idm.incoming("s1", "toolu_orig", "anthropic");
    const chatExt = idm.outgoing("s1", canon, "openai_chat");
    const back = idm.incoming("s1", chatExt, "openai_chat");
    assert.equal(back, canon, "外部形式必须能反解回 canonical");
    assert.equal(idm.outgoing("s1", back, "anthropic"), "toolu_orig");
    store.shutdown();
  });

  test("会话间隔离（并发分叉不串号）", () => {
    const { store, idm } = makeStore();
    idm.incoming("s1", "toolu_abc", "anthropic");
    assert.equal(idm.incoming("s2", "toolu_abc", "anthropic"), "toolu_abc");
    const e1 = idm.outgoing("s1", "toolu_abc", "openai_chat");
    const e2 = idm.outgoing("s2", "toolu_abc", "openai_chat");
    assert.notEqual(e1, e2, "不同会话的铸造互不影响");
    store.shutdown();
  });

  test("共享 db 与 size", () => {
    const { store, idm } = makeStore();
    idm.incoming("s1", "call_1", "openai_chat");
    idm.outgoing("s1", "call_1", "anthropic");
    assert.equal(idm.size("s1"), 2);
    store.shutdown();
  });
});

// ---------------------------------------------------------------------------
// TestPropertyRoundTrip（property-based 往返，440 随机用例、固定种子可复现）
// ---------------------------------------------------------------------------

describe("TestPropertyRoundTrip", () => {
  const SEED = 20260909;
  const LETTERS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ中文测试字  ";

  function randText(rng: Rng, n = 40): string {
    const len = rng.randint(1, n);
    let s = "";
    for (let i = 0; i < len; i++) s += rng.choice(LETTERS.split(""));
    return s;
  }

  function randChatPayload(rng: Rng): ir.Json {
    const msgs: ir.Json[] = [{ role: "system", content: randText(rng) }];
    const count = rng.randint(1, 6);
    for (let i = 0; i < count; i++) {
      const role = i % 2 === 0 ? "user" : "assistant";
      const m: ir.Json = { role, content: randText(rng) };
      if (role === "assistant" && rng.random() < 0.5) {
        m.tool_calls = [{
          id: `call_${rng.randint(0, 999)}`, type: "function",
          function: { name: "f", arguments: JSON.stringify(
            { k: rng.randint(0, 100), s: randText(rng, 8) }) } }];
      }
      msgs.push(m);
    }
    return { model: "gpt-x", messages: msgs,
             tools: [{ type: "function", function: {
               name: "f", description: "d", parameters: { type: "object" } } }],
             [`x_rand_${rng.randint(0, 999)}`]: rng.random(),
             logit_bias: { "1": 1 } };
  }

  function randAnthropicPayload(rng: Rng): ir.Json {
    const msgs: ir.Json[] = [];
    const count = rng.randint(1, 5);
    for (let i = 0; i < count; i++) {
      const role = i % 2 === 0 ? "user" : "assistant";
      const content: ir.Json[] = [{ type: "text", text: randText(rng) }];
      if (role === "assistant") {
        if (rng.random() < 0.6) {
          content.unshift({ type: "thinking", thinking: randText(rng),
                            signature: `sig_${rng.randint(0, 9999)}` });
        }
        if (rng.random() < 0.5) {
          content.push({ type: "tool_use", id: `toolu_${rng.randint(0, 999)}`,
                         name: "f", input: { v: rng.randint(0, 50) } });
        }
      }
      msgs.push({ role, content });
    }
    return { model: "claude-x", max_tokens: 128, messages: msgs,
             tools: [{ name: "f", description: "d",
                       input_schema: { type: "object" } }],
             [`x_rand_${rng.randint(0, 999)}`]: 1, top_p: 0.9 };
  }

  function randResponsePayload(rng: Rng): ir.Json {
    const items: ir.Json[] = [];
    const count = rng.randint(1, 5);
    for (let i = 0; i < count; i++) {
      if (i % 2 === 0) {
        items.push({ type: "message", role: "user", content: randText(rng) });
      } else {
        items.push({ type: "message", role: "assistant",
                     content: [{ type: "output_text", text: randText(rng) }] });
        if (rng.random() < 0.5) {
          items.push({ type: "function_call", name: "f",
                       call_id: `call_${rng.randint(0, 999)}`,
                       arguments: JSON.stringify({ v: rng.randint(0, 50) }) });
        }
      }
    }
    return { model: "gpt-x", input: items,
             [`x_rand_${rng.randint(0, 999)}`]: "x" };
  }

  test("性质：chat→anthropic（未知字段必降级 / 断点恒 [3,4] / 参数守恒）", () => {
    const rng = new Rng(SEED);
    const known = new Set(["model", "messages", "tools", "max_tokens",
                           "max_completion_tokens", "temperature", "stream", "system"]);
    for (let c = 0; c < 120; c++) {
      const p = randChatPayload(rng);
      const d1 = new Dropped();
      const req = new ChatAdapter().to_ir(p, d1);
      const droppedFields = new Set(d1.items.map((d) => d.field));
      for (const k of Object.keys(p)) {
        if (!known.has(k)) {
          assert.ok(droppedFields.has(k), `case ${c}: 未知字段 ${k} 被静默丢弃`);
        }
      }
      const out = new AnthropicAdapter().from_ir(req, new Dropped(), new ir.SessionContext());
      const bps = countBreakpoints(out);
      assert.ok(bps >= 3 && bps <= 4, `case ${c}: 断点数 ${bps} 越界`);
      const src = p.messages.flatMap((m: ir.Json) => m.tool_calls ?? [])
        .map((tc: ir.Json) => JSON.parse(tc.function.arguments));
      const got = out.messages.flatMap((m: ir.Json) => m.content)
        .filter((b: ir.Json) => b.type === "tool_use")
        .map((b: ir.Json) => b.input);
      assert.deepEqual(src, got, `case ${c}: 工具参数跨协议变形`);
    }
  });

  test("性质：anthropic→chat（未知字段必降级 / 参数守恒）", () => {
    const rng = new Rng(SEED + 1);
    const known = new Set(["model", "max_tokens", "temperature", "stream",
                           "system", "messages", "tools", "thinking", "tool_choice"]);
    for (let c = 0; c < 120; c++) {
      const p = randAnthropicPayload(rng);
      const d1 = new Dropped();
      const req = new AnthropicAdapter().to_ir(p, d1);
      const droppedFields = new Set(d1.items.map((d) => d.field));
      for (const k of Object.keys(p)) {
        if (!known.has(k)) {
          assert.ok(droppedFields.has(k), `case ${c}: 未知字段 ${k} 被静默丢弃`);
        }
      }
      const out = new ChatAdapter().from_ir(req, new Dropped());
      const srcInputs = p.messages.flatMap((m: ir.Json) => m.content)
        .filter((b: ir.Json) => b.type === "tool_use")
        .map((b: ir.Json) => b.input);
      const gotArgs = out.messages.flatMap((m: ir.Json) => m.tool_calls ?? [])
        .map((tc: ir.Json) => JSON.parse(tc.function.arguments));
      assert.deepEqual(srcInputs, gotArgs, `case ${c}: Anthropic→Chat 参数变形`);
    }
  });

  test("性质：thinking signature 有序守恒", () => {
    const rng = new Rng(SEED + 2);
    for (let c = 0; c < 80; c++) {
      const p = randAnthropicPayload(rng);
      const req = new AnthropicAdapter().to_ir(p, new Dropped());
      const out = new AnthropicAdapter().from_ir(req, new Dropped());
      const srcSigs = p.messages.flatMap((m: ir.Json) => m.content)
        .filter((b: ir.Json) => b.type === "thinking")
        .map((b: ir.Json) => b.signature);
      const gotSigs = out.messages.flatMap((m: ir.Json) => m.content)
        .filter((b: ir.Json) => b.type === "thinking")
        .map((b: ir.Json) => b.signature);
      assert.deepEqual(srcSigs, gotSigs, `case ${c}: thinking signature 往返丢失`);
    }
  });

  test("性质：response→anthropic（未知字段必降级 / 参数守恒）", () => {
    const rng = new Rng(SEED + 3);
    for (let c = 0; c < 100; c++) {
      const p = randResponsePayload(rng);
      const d1 = new Dropped();
      const req = new ResponseAdapter().to_ir(p, d1);
      const droppedFields = new Set(d1.items.map((d) => d.field));
      for (const k of Object.keys(p)) {
        if (k !== "model" && k !== "input") {
          assert.ok(droppedFields.has(k), `case ${c}: 未知字段 ${k} 被静默丢弃`);
        }
      }
      const out = new AnthropicAdapter().from_ir(req, new Dropped(), new ir.SessionContext());
      const src = p.input.filter((it: ir.Json) => it.type === "function_call")
        .map((it: ir.Json) => JSON.parse(it.arguments));
      const got = out.messages.flatMap((m: ir.Json) => m.content)
        .filter((b: ir.Json) => b.type === "tool_use")
        .map((b: ir.Json) => b.input);
      assert.deepEqual(src, got, `case ${c}: Responses→Anthropic 参数变形`);
    }
  });
});

// ---------------------------------------------------------------------------
// TestSseConversion（Anthropic SSE → Chat chunk 逐块转换）
// ---------------------------------------------------------------------------

describe("TestSseConversion", () => {
  function feedSeq(conv: AnthropicToChatStream, events: ir.Json[]): string[] {
    const frames: string[] = [];
    for (const ev of events) {
      frames.push(...conv.feed(ev.type ?? null, ev));
    }
    return frames;
  }

  function parseFrame(frame: string): ir.Json {
    assert.ok(frame.startsWith("data: ") && frame.endsWith("\n\n"));
    return JSON.parse(frame.slice("data: ".length).trim());
  }

  test("SSE 分帧：注释行跳过 / 非 JSON 透传", () => {
    const raw = [
      "event: message_start\n",
      'data: {"type":"message_start","message":{"id":"msg_1"}}\n',
      "\n",
      ": heartbeat-comment\n",
      'data: {"type":"ping"}\n',
      "\n",
      "data: 非JSON行\n",
      "\n",
    ];
    const events = [...parseSseLines(raw)];
    assert.equal(events.length, 3);
    assert.deepEqual(events[0], ["message_start",
                                 { type: "message_start", message: { id: "msg_1" } }]);
    assert.deepEqual(events[1][1], { type: "ping" }); // 注释行被跳过
    assert.equal(events[2][1], "非JSON行");           // 非 JSON 原样透传
  });

  test("SSE 分帧：尾部半帧容错丢弃", () => {
    const raw = ['data: {"type":"message_stop"}\n', "\n", 'data: {"type":"truncat'];
    const events = [...parseSseLines(raw)];
    assert.equal(events.length, 1); // 尾部半帧容错丢弃，不崩
  });

  test("text_delta 即时下发", () => {
    const conv = new AnthropicToChatStream();
    const frames = feedSeq(conv, [
      { type: "message_start", message: {
        id: "msg_x", model: "claude-mock",
        usage: { input_tokens: 100, cache_read_input_tokens: 80 } } },
      { type: "content_block_delta", index: 0,
        delta: { type: "text_delta", text: "你" } },
      { type: "content_block_delta", index: 0,
        delta: { type: "text_delta", text: "好" } },
      { type: "message_delta", delta: { stop_reason: "end_turn" },
        usage: { output_tokens: 7 } },
      { type: "message_stop" },
    ]);
    // role + 2 文本帧 + finish + DONE：文本逐块即时下发，不等流尾
    assert.equal(frames.length, 5);
    const first = parseFrame(frames[0]);
    assert.deepEqual(first.choices[0].delta, { role: "assistant" });
    assert.equal(first.id, "msg_x");
    assert.equal(first.model, "claude-mock");
    assert.equal(first.object, "chat.completion.chunk");
    const texts = frames.slice(1, 3).map((f) => parseFrame(f).choices[0].delta.content ?? "");
    assert.equal(texts.join(""), "你好");
    const fin = parseFrame(frames[3]);
    assert.equal(fin.choices[0].finish_reason, "stop");
    assert.equal(frames[4], "data: [DONE]\n\n");
  });

  test("工具参数缓冲到块结束一次性发", () => {
    const conv = new AnthropicToChatStream();
    const f1 = conv.feed("content_block_start",
                         { type: "content_block_start", index: 1,
                           content_block: { type: "tool_use",
                                            id: "toolu_1", name: "get_weather" } });
    assert.deepEqual(f1, []); // 工具块开始不落帧
    const f2 = conv.feed("content_block_delta",
                         { type: "content_block_delta", index: 1,
                           delta: { type: "input_json_delta",
                                    partial_json: '{"city": "天' } });
    const f3 = conv.feed("content_block_delta",
                         { type: "content_block_delta", index: 1,
                           delta: { type: "input_json_delta",
                                    partial_json: '津"}' } });
    assert.deepEqual([...f2, ...f3], []); // 参数片段期间不落帧
    const f4 = conv.feed("content_block_stop",
                         { type: "content_block_stop", index: 1 });
    assert.equal(f4.length, 1);
    const chunk = parseFrame(f4[0]);
    const tc = chunk.choices[0].delta.tool_calls[0];
    assert.equal(tc.id, "toolu_1");
    assert.equal(tc.function.name, "get_weather");
    assert.equal(tc.function.arguments, '{"city": "天津"}');
    // 落库原料：工具参数被解析回 dict
    const synth = conv.syntheticResponse();
    const toolBlocks = synth.content.filter((b: ir.Json) => b.type === "tool_use");
    assert.deepEqual(toolBlocks[0].input, { city: "天津" });
  });

  test("stop_reason 映射", () => {
    for (const [anthropicReason, chatReason] of [
      ["end_turn", "stop"], ["max_tokens", "length"], ["tool_use", "tool_calls"],
    ] as const) {
      const conv = new AnthropicToChatStream();
      const frames = conv.feed("message_delta",
                               { type: "message_delta",
                                 delta: { stop_reason: anthropicReason },
                                 usage: { output_tokens: 3 } });
      const chunk = parseFrame(frames[0]);
      assert.equal(chunk.choices[0].finish_reason, chatReason);
    }
  });

  test("usage 流尾合并 + 落库复用", () => {
    const conv = new AnthropicToChatStream();
    feedSeq(conv, [
      { type: "message_start", message: {
        id: "msg_u", model: "m",
        usage: { input_tokens: 5978,
                 cache_creation_input_tokens: 5978,
                 cache_read_input_tokens: 0 } } },
      { type: "content_block_delta", index: 0,
        delta: { type: "text_delta", text: "答复" } },
      { type: "message_delta", delta: { stop_reason: "end_turn" },
        usage: { output_tokens: 12 } },
    ]);
    const u = usageFromAnthropic(conv.usage());
    assert.equal(u.input_tokens, 5978);
    assert.equal(u.cache_creation_input_tokens, 5978);
    assert.equal(u.output_tokens, 12);
    const reply = assistantFromUpstream("anthropic", conv.syntheticResponse());
    assert.ok(reply !== null);
    assert.equal(reply.blocks[0].text, "答复");
  });

  test("上游错误帧", () => {
    const conv = new AnthropicToChatStream();
    const frames = conv.feed("error", { type: "error", error: {
      type: "overloaded_error", message: "Overloaded" } });
    const chunk = parseFrame(frames[0]);
    assert.ok(String(chunk.choices[0].delta.content ?? "").includes("Overloaded"));
  });
});

// ---------------------------------------------------------------------------
// TestChatStreamCollector（chat←chat 直通流式旁路收集）
// ---------------------------------------------------------------------------

describe("TestChatStreamCollector", () => {
  test("文本与 usage 汇总", () => {
    const c = new ChatStreamCollector();
    c.feedData(JSON.stringify({ choices: [{ delta: { role: "assistant", content: "你" } }] }));
    c.feedData(JSON.stringify({ choices: [{ delta: { content: "好" } }] }));
    c.feedData(JSON.stringify({ choices: [{ delta: {}, finish_reason: "stop" }],
                                usage: { prompt_tokens: 100, completion_tokens: 7 } }));
    c.feedData("[DONE]");
    assert.equal(c.usage().prompt_tokens, 100);
    const synth = c.syntheticResponse();
    assert.equal(synth.choices[0].message.content, "你好");
    // 复用既有落库/归一路径
    const reply = assistantFromUpstream("openai_chat", synth)!;
    assert.equal(reply.blocks[0].text, "你好");
    const u = usageFromChat(synth.usage);
    assert.equal(u.output_tokens, 7);
  });

  test("工具槽位拼接（Chat 流参数同样分段）", () => {
    const c = new ChatStreamCollector();
    c.feedData(JSON.stringify({ choices: [{ delta: { tool_calls: [
      { index: 0, id: "call_1", type: "function",
        function: { name: "get_weather", arguments: '{"ci' } }] } }] }));
    c.feedData(JSON.stringify({ choices: [{ delta: { tool_calls: [
      { index: 0, function: { arguments: 'ty":"天津"}' } }] } }] }));
    const synth = c.syntheticResponse();
    const tc = synth.choices[0].message.tool_calls[0];
    assert.equal(tc.id, "call_1");
    assert.equal(tc.function.arguments, '{"city":"天津"}');
  });

  test("脏数据与空输入容错", () => {
    const c = new ChatStreamCollector();
    c.feedData("");        // 空行
    c.feedData("非JSON");   // 容错跳过
    c.feedData("[DONE]");
    assert.deepEqual(c.usage(), {});
    const synth = c.syntheticResponse();
    assert.equal(synth.choices[0].message.content, "");
  });
});

// ---------------------------------------------------------------------------
// TestWebuiDashboard（TS 版新增：弹网页安全骨架 + 编排回写）
// ---------------------------------------------------------------------------

describe("TestWebuiDashboard", () => {
  test("Host 头与一次性 token 校验（防 DNS rebinding / 扫端口）", async () => {
    const { openDashboard, TIMEOUT_S } = await import("../src/webui/server.ts");
    assert.equal(TIMEOUT_S, 90); // 超时兜底区间 60-120s
    const d = await openDashboard(
      [{ layer: "L3", content: "偏好简洁明了的回答风格，不喜欢任何冗余解释与无关铺陈" }],
      { openBrowser: false });
    try {
      // 无 token → 403
      let resp = await fetch(`http://127.0.0.1:${d.port}/`);
      assert.equal(resp.status, 403);
      // 错误 Host（域名形式）→ 403（undici 禁改 Host，用裸 http.request）
      const status = await new Promise<number>((resolve, reject) => {
        const r = http.request({ host: "127.0.0.1", port: d.port, path: "/?t=x",
                                 headers: { Host: `localhost:${d.port}` } },
                                (res) => { res.resume(); resolve(res.statusCode ?? 0); });
        r.on("error", reject);
        r.end();
      });
      assert.equal(status, 403);
      // 正确 token + Host → 200，且页面含缓存前缀可视化与脱敏
      resp = await fetch(d.url);
      assert.equal(resp.status, 200);
      const html = await resp.text();
      assert.ok(html.includes("稳定前缀（吃缓存）"), "缺缓存前缀可视化");
      assert.ok(html.includes("断点 4"), "缺滚动尾部断点说明");
      assert.ok(html.includes("…[点开看全文]"), "长记忆未脱敏截断");
    } finally {
      d.close();
    }
  });

  test("勾选提交经 saveCallback 回写；mask 脱敏", async () => {
    const { openDashboard, mask } = await import("../src/webui/server.ts");
    assert.equal(mask("短"), "短");
    assert.ok(mask("x".repeat(30)).endsWith("…[点开看全文]"));
    const mems = [{ layer: "L1", content: "记忆甲" }, { layer: "L3", content: "记忆乙" }];
    const saved: ir.Json[][] = [];
    const d = await openDashboard(mems, { openBrowser: false,
                                          saveCallback: (sel) => saved.push(sel) });
    try {
      const resp = await fetch(`http://127.0.0.1:${d.port}/`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: `t=${encodeURIComponent(d.url.split("t=")[1])}&mem=1`,
      });
      assert.equal(resp.status, 200);
      assert.equal(saved.length, 1);
      assert.deepEqual(saved[0], [mems[1]], "只应回写勾选的 mem=1");
    } finally {
      d.close();
    }
  });
});

// ---------------------------------------------------------------------------
// TestVerifyCacheOffline（TS 版新增：验站脚本对 mock 应 PASS）
// ---------------------------------------------------------------------------

describe("TestVerifyCacheOffline", () => {
  test("对 mock 两步验证：写 creation>0 → 读 read>0 = PASS", async () => {
    const { verifyCache } = await import("../tools/verify_cache.ts");
    const srv = http.createServer((req, res) => { void mockHandler(req, res); });
    await new Promise<void>((r) => srv.listen(0, "127.0.0.1", r));
    const port = (srv.address() as { port: number }).port;
    try {
      const result = await verifyCache(`http://127.0.0.1:${port}`, "mock", "",
                                       4500, () => {}, 10);
      assert.ok(result !== null);
      assert.equal(result.verdict, "PASS");
      assert.ok(result.u1.cache_creation_input_tokens > 0);
      assert.ok(result.u2.cache_read_input_tokens > 0);
      assert.equal(result.forgedWarning, false);
    } finally {
      srv.close();
    }
  });
});
