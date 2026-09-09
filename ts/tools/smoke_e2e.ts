/** 端到端冒烟：起 mock 后端 + 网关两个子进程，跑 7 组 25 项断言，退出码 0/1。
 * （TS 移植自 tools/smoke_e2e.py）
 *
 * 单元测试跑在进程内（替换 gw 单例），验的是逻辑；
 * 这个脚本验的是**入口能不能真的起来**：`node src/gateway/server.ts`
 * 和 `node tools/mock_backend.ts` 这两个真入口，以及跨进程 HTTP 链路。
 *
 * 用法：npm run smoke（或 node tools/smoke_e2e.ts）
 * 环境变量：NODE 指定解释器；PB_DB / PB_METRICS 由本脚本自动指向临时目录。
 */

import { spawn, type ChildProcess } from "node:child_process";
import { mkdtempSync, rmSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import net from "node:net";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const NODE = process.execPath;
const GW_PORT = 8099;
const MOCK_PORT = 9100;

const PROCS: ChildProcess[] = [];

function waitPort(port: number, timeout = 15000): Promise<boolean> {
  const deadline = Date.now() + timeout;
  return new Promise((resolve) => {
    const tryOnce = () => {
      const s = net.connect(port, "127.0.0.1");
      s.once("connect", () => { s.end(); resolve(true); });
      s.once("error", () => {
        if (Date.now() < deadline) setTimeout(tryOnce, 200);
        else resolve(false);
      });
    };
    tryOnce();
  });
}

async function start(): Promise<string> {
  const tmp = mkdtempSync(join(tmpdir(), "pb-smoke-ts-"));
  const env = {
    ...process.env,
    PB_DIRECT: "1",
    PB_DB: join(tmp, "s.db"),
    PB_METRICS: join(tmp, "m.jsonl"),
    PB_PORT: String(GW_PORT),
  };
  const mock = spawn(NODE, [join(ROOT, "tools", "mock_backend.ts")],
                     { cwd: ROOT, env, stdio: "ignore" });
  PROCS.push(mock);
  if (!(await waitPort(MOCK_PORT))) throw new Error("mock 后端未能在 15s 内监听 9100");
  const gw = spawn(NODE, [join(ROOT, "src", "gateway", "server.ts")],
                   { cwd: ROOT, env, stdio: "ignore" });
  PROCS.push(gw);
  if (!(await waitPort(GW_PORT))) throw new Error(`网关未能在 15s 内监听 ${GW_PORT}`);
  return tmp;
}

function stop(tmp: string): void {
  for (const p of PROCS) p.kill();
  // 给子进程一点退出时间再清目录（异步冒烟里允许宽松处理）
  setTimeout(() => rmSync(tmp, { recursive: true, force: true }), 300).unref();
}

async function post(path: string, payload: unknown): Promise<any> {
  const resp = await fetch(`http://127.0.0.1:${GW_PORT}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    const err = new Error(`HTTP ${resp.status}`) as Error & { status: number };
    err.status = resp.status;
    throw err;
  }
  return resp.json();
}

/** 流式 POST：逐帧收 SSE，返回 [data 帧列表, 各帧到达时间列表]。
 * 到达时间用来证明网关是**逐块下发**而非整读后再吐。 */
async function postStream(path: string, payload: unknown): Promise<[string[], number[]]> {
  const resp = await fetch(`http://127.0.0.1:${GW_PORT}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const frames: string[] = [];
  const times: number[] = [];
  const reader = resp.body!.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, idx).trim();
      buf = buf.slice(idx + 1);
      if (line.startsWith("data:")) {
        frames.push(line.slice("data:".length).trim());
        times.push(Date.now());
      }
    }
  }
  return [frames, times];
}

const CHECKS: [string, boolean, string][] = [];
function check(name: string, ok: boolean, detail = ""): void {
  CHECKS.push([name, ok, detail]);
  console.log(`  [${ok ? "PASS" : "FAIL"}] ${name}` + (detail ? `  ${detail}` : ""));
}

function tryJson(s: string): any | null {
  try { return JSON.parse(s); } catch { return null; }
}

async function main(): Promise<number> {
  console.log(`smoke(ts): mock=127.0.0.1:${MOCK_PORT}  gateway=127.0.0.1:${GW_PORT}`);
  const tmp = await start();
  try {
    console.log("\n1) openai_chat -> openai_chat（同协议直通）");
    let r = await post("/v1/openai_chat/to/openai_chat",
                       { model: "mock", messages: [{ role: "user", content: "hi" }] });
    check("usage 归一有值", (r.usage?.prompt_tokens ?? 0) > 0, JSON.stringify(r.usage));
    check("无静默丢弃", !r._bridge.dropped?.length, JSON.stringify(r._bridge.dropped));

    console.log("\n2) openai_response -> openai_chat（跨协议）");
    r = await post("/v1/openai_response/to/openai_chat",
                   { model: "mock", input: [{ type: "message", role: "user", content: "hi" }] });
    check("跨协议转换成功", r.choices !== undefined || r.id !== undefined);

    console.log("\n3) openai_response -> openai_response（多轮 previous_response_id）");
    const r1 = await post("/v1/openai_response/to/openai_response",
                          { model: "mock", input: [{ type: "message", role: "user", content: "第一轮" }] });
    check("首轮无历史可重放", r1._bridge.replayed_messages === 0,
          `replayed=${r1._bridge.replayed_messages}`);
    const r2 = await post("/v1/openai_response/to/openai_response",
                          { model: "mock", previous_response_id: r1.id,
                            input: [{ type: "message", role: "user", content: "第二轮" }] });
    check("第 2 轮重放 2 条", r2._bridge.replayed_messages === 2,
          `replayed=${r2._bridge.replayed_messages}`);
    const r3 = await post("/v1/openai_response/to/openai_response",
                          { model: "mock", previous_response_id: r2.id,
                            input: [{ type: "message", role: "user", content: "第三轮" }] });
    check("第 3 轮重放 4 条（历史持续累积）", r3._bridge.replayed_messages === 4,
          `replayed=${r3._bridge.replayed_messages}`);

    console.log("\n4) anthropic -> anthropic（预热 max_tokens:0 + 冲突拦截）");
    const warm = { model: "mock", max_tokens: 0,
                   system: [{ type: "text", text: "s".repeat(30),
                              cache_control: { type: "ephemeral" } }],
                   messages: [{ role: "user", content: "warmup" }] };
    r = await post("/v1/anthropic/to/anthropic", warm);
    check("预热零输出计费", (r.usage?.output_tokens ?? -1) === 0, JSON.stringify(r.usage));
    check("预热标记为 warmup 轮", r._bridge.warmup === true);
    check("预热写了缓存", (r.usage?.cache_creation_input_tokens ?? 0) > 0,
          JSON.stringify(r.usage));
    try {
      await post("/v1/anthropic/to/anthropic",
                 { ...warm, thinking: { type: "enabled", budget_tokens: 1024 } });
      check("thinking 与预热冲突被拦截", false, "请求通过了，未拦截");
    } catch (e) {
      check("thinking 与预热冲突被拦截", (e as { status?: number }).status === 400,
            `HTTP ${(e as { status?: number }).status}`);
    }

    console.log("\n5) 非法路由 / 未知协议（必须回 400，不能掐断连接）");
    try {
      await post("/v1/openai_chat/to/does_not_exist", { model: "mock", messages: [] });
      check("未知 target 回 400", false, "请求通过了");
    } catch (e) {
      check("未知 target 回 400", (e as { status?: number }).status === 400,
            `HTTP ${(e as { status?: number }).status}`);
    }

    console.log("\n6) SSE 逐块流式（openai_chat 客户端 ← anthropic 上游）");
    const [frames, times] = await postStream("/v1/openai_chat/to/anthropic",
                                             { model: "mock", stream: true,
                                               messages: [{ role: "user", content: "hi" }] });
    const chunks = frames.filter((f) => f !== "[DONE]").map(tryJson).filter(Boolean);
    check("收到多帧而非一整块", frames.length >= 5, `frames=${frames.length}`);
    check("首帧是 role 帧",
          chunks.length > 0 && chunks[0].choices[0].delta.role === "assistant",
          frames[0]?.slice(0, 80) ?? "无帧");
    const text = chunks.map((c) => c.choices[0].delta.content ?? "").join("");
    check("delta 文本拼合完整", text === "mock stream reply", JSON.stringify(text));
    check("有 finish_reason=stop 帧",
          chunks.some((c) => c.choices[0].finish_reason === "stop"));
    check("以 [DONE] 收尾", frames.length > 0 && frames[frames.length - 1] === "[DONE]",
          frames[frames.length - 1]?.slice(0, 40) ?? "无帧");
    // mock 每 chunk 间隔 10ms——若网关整读再吐，首尾帧会几乎同时到达
    const spread = times.length >= 2 ? (times[times.length - 1] - times[0]) / 1000 : 0;
    check("帧是逐块到达的（非整读后补吐）", spread >= 0.015,
          `首尾间隔 ${(spread * 1000).toFixed(1)}ms`);
    try {
      const lines = readFileSync(join(tmp, "m.jsonl"), "utf-8").trim().split("\n");
      const last = JSON.parse(lines[lines.length - 1]);
      check("流式轮照常进埋点",
            last.kind === "normal" && (last.input_tokens ?? 0) > 0,
            JSON.stringify(last).slice(0, 120));
    } catch (e) {
      check("流式轮照常进埋点", false, String(e));
    }

    console.log("\n7) SSE 直通流式（同协议透传）+ 未实现方向显式 501");
    // 7a anthropic -> anthropic：字节透传，客户端收到原样 Anthropic 事件
    const [fa, ta] = await postStream("/v1/anthropic/to/anthropic",
                                      { model: "mock", stream: true, max_tokens: 64,
                                        messages: [{ role: "user", content: "hi" }] });
    const deltas = fa.map(tryJson).filter(Boolean)
      .filter((d) => d.type === "content_block_delta")
      .map((d) => d.delta?.text ?? "");
    check("anthropic 透传文本拼合完整", deltas.join("") === "mock stream reply",
          JSON.stringify(deltas.join("")));
    check("anthropic 透传收到 message_stop", fa.some((f) => f.includes("message_stop")));
    const spreadA = ta.length >= 2 ? (ta[ta.length - 1] - ta[0]) / 1000 : 0;
    check("anthropic 透传逐块到达", spreadA >= 0.015,
          `首尾间隔 ${(spreadA * 1000).toFixed(1)}ms`);
    // 7b chat -> chat：字节透传，客户端收到原样 Chat chunk
    const [fb, tb] = await postStream("/v1/openai_chat/to/openai_chat",
                                      { model: "mock", stream: true,
                                        messages: [{ role: "user", content: "hi" }] });
    const chunksB = fb.filter((f) => f !== "[DONE]").map(tryJson).filter(Boolean);
    const textB = chunksB.map((c) => c.choices[0].delta.content ?? "").join("");
    check("chat 透传文本拼合完整", textB === "mock stream reply", JSON.stringify(textB));
    check("chat 透传以 [DONE] 收尾", fb.length > 0 && fb[fb.length - 1] === "[DONE]");
    const spreadB = tb.length >= 2 ? (tb[tb.length - 1] - tb[0]) / 1000 : 0;
    check("chat 透传逐块到达", spreadB >= 0.015,
          `首尾间隔 ${(spreadB * 1000).toFixed(1)}ms`);
    // 7c 未实现的流式方向：显式 501（此前是把 SSE 当 JSON 解析炸成 502）
    try {
      await post("/v1/openai_response/to/anthropic",
                 { model: "mock", stream: true,
                   input: [{ type: "message", role: "user", content: "hi" }] });
      check("未实现流式方向显式 501", false, "请求通过了");
    } catch (e) {
      check("未实现流式方向显式 501", (e as { status?: number }).status === 501,
            `HTTP ${(e as { status?: number }).status}`);
    }
  } finally {
    stop(tmp);
  }

  const failed = CHECKS.filter(([, ok]) => !ok).map(([n]) => n);
  console.log(`\n${CHECKS.length - failed.length}/${CHECKS.length} 通过`);
  if (failed.length) console.log("失败项：" + failed.join("、"));
  return failed.length ? 1 : 0;
}

process.exitCode = await main();
