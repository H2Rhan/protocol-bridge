/** 网关/代理主进程：HTTP 转发 + SSE 流式（TS 移植自 src/gateway/server.py，对齐 v1.7）。
 *
 * 落点（方案 3.2）：独立进程承载状态层，是被「必须支持 previous_response_id」锁定的形态。
 *
 * 请求流程：
 *   入口按 target 选 adapter -> to_ir -> (有状态则状态层重放历史) ->
 *   from_ir 渲染目标协议（Anthropic 侧按断点布局打 cache_control）->
 *   转发到 backend（默认 mock，可离线跑通）-> 归一 usage + 命中率埋点 ->
 *   非流式 JSON / 流式 SSE 回客户端。
 *
 * 与 Python 版的已知差异（见 ts/README.md「差异清单」）：
 *   - 不读系统代理环境变量（Node 的 http.request 默认直连）；PB_DIRECT 保留兼容但为 no-op。
 *   - SQLite 用 Node 内置 node:sqlite（experimental warning 属正常）。
 */

import http from "node:http";
import { once } from "node:events";
import { TextDecoder } from "node:util";
import { randomUUID } from "node:crypto";
import * as ir from "../ir/model.ts";
import { Dropped, assistantFromUpstream } from "../adapters/base.ts";
import { ChatAdapter, usageFromChat } from "../adapters/chat.ts";
import { ResponseAdapter, usageFromResponse } from "../adapters/response.ts";
import { AnthropicAdapter, usageFromAnthropic } from "../adapters/anthropic.ts";
import { load as loadSessionConfig } from "../state/session_config.ts";
import { SessionStore, Session } from "../state/store.ts";
import { MetricsLog, NORMAL, WARMUP } from "../observability/metrics.ts";
import { AnthropicToChatStream, ChatStreamCollector, ChatToAnthropicStream,
         ResponseStreamCollector, ResponseToAnthropicStream, ResponseToChatStream,
         AnthropicToResponseStream, ChatToResponseStream,
         SseEventParser } from "./sse.ts";

/** 模块级可变配置（对齐 Python 的模块全局，测试可整体替换）。 */
export const gw = {
  BACKEND_URL: (process.env.PB_BACKEND ?? "http://127.0.0.1:9100").replace(/\/$/, ""),
  BACKEND_KEY: process.env.PB_API_KEY ?? "",      // 真实后端 Bearer key（仅环境变量，不落盘）
  DIRECT: process.env.PB_DIRECT === "1",          // TS 版 no-op：Node 不读系统代理
  BIND_HOST: "127.0.0.1",
  BIND_PORT: Number(process.env.PB_PORT ?? "8080"),
  METRICS_PATH: process.env.PB_METRICS ?? "metrics.jsonl",
  BACKEND_PATH: {
    openai_chat: process.env.PB_CHAT_PATH ?? "/chat/completions",
    openai_response: process.env.PB_RESPONSE_PATH ?? "/responses",
    anthropic: process.env.PB_ANTHROPIC_PATH ?? "/v1/messages",
  } as Record<string, string>,
  CFG: loadSessionConfig(),
  STORE: null as unknown as SessionStore,
  METRICS: null as unknown as MetricsLog,
  CONCURRENCY: Number(process.env.PB_CONCURRENCY ?? "2"),
};
gw.STORE = new SessionStore(gw.CFG);
gw.METRICS = new MetricsLog(gw.METRICS_PATH);

const ADAPTERS: Record<string, ChatAdapter | ResponseAdapter | AnthropicAdapter> = {
  openai_chat: new ChatAdapter(),
  openai_response: new ResponseAdapter(),
  anthropic: new AnthropicAdapter(),
};

/** 并发限流（替代 Python 的 threading.Semaphore）。 */
class Semaphore {
  private count: number;
  private queue: (() => void)[] = [];

  constructor(n: number) { this.count = n; }

  async acquire(): Promise<void> {
    if (this.count > 0) { this.count--; return; }
    await new Promise<void>((resolve) => this.queue.push(resolve));
  }

  release(): void {
    const next = this.queue.shift();
    if (next) next();
    else this.count++;
  }
}

const GATE = new Semaphore(gw.CONCURRENCY);

// ---------------------------------------------------------------------------
// 预热请求（max_tokens:0）：单独语义 + 拒绝条件校验（方案 3.7，v3 已核实官方行为）
// ---------------------------------------------------------------------------

export function isWarmup(payload: ir.Json): boolean {
  const mt = payload.max_tokens ?? payload.max_output_tokens ?? 1;
  try {
    return Number.parseInt(String(mt), 10) === 0;
  } catch {
    return false;
  }
}

/** 预热请求的拒绝条件：冲突则返回错误消息（应回 invalid_request_error）。
 *
 * 必须校验**转换后、即将发给上游的 rendered**（目标协议形态），不能用源协议
 * payload——拒绝条件是上游（target）的行为，且字段只存在于目标协议里
 * （如 thinking 属 Anthropic、response_format 属 OpenAI）。
 */
export function validateWarmup(rendered: ir.Json, target: string): string | null {
  if (rendered.stream) {
    return "warmup(max_tokens:0) 不能同时带 stream:true";
  }
  if (target === "anthropic") {
    const thinking = rendered.thinking;
    if (thinking !== null && typeof thinking === "object" && thinking.type === "enabled") {
      return "warmup(max_tokens:0) 不能同时带 extended thinking";
    }
  } else {
    // OpenAI 系：structured outputs（Chat 用 response_format；Responses 用 text.format）
    if (rendered.response_format || rendered.output_config?.format || rendered.text?.format) {
      return "warmup(max_tokens:0) 不能同时带 structured outputs";
    }
  }
  const tc = rendered.tool_choice;
  if (tc !== null && typeof tc === "object" && ["tool", "any"].includes(tc.type)) {
    return "warmup(max_tokens:0) 不能同时带 tool_choice={type:tool/any}";
  }
  return null;
}

// ---------------------------------------------------------------------------
// 记忆注入（方案 3.9 / TRACK 01）：session meta → IR system 尾部，文本幂等去重
// ---------------------------------------------------------------------------

/** 把 session.meta 的记忆块注入 IR system 尾部。返回注入条数。
 *
 * 幂等去重：按文本比对——外部请求若已注入相同文本则跳过，
 * 避免同一段记忆重复注入、重复计费（方案 3.9 耦合点 2）。
 * 记忆上限（TRACK 04 第三参数）：会话 meta.memory_cap > 全局
 * cfg.session_memory_cap > 0（不限制）；达到上限即停，保留已注入的稳定前缀。
 */
export function injectMemories(req: ir.IRRequest, session: Session | null): number {
  const memories = session?.meta?.memories;
  if (!memories || !Array.isArray(memories)) return 0;
  const cap = session?.meta?.memory_cap || gw.CFG.session_memory_cap;
  const seen = new Set(req.system.filter((b) => b.kind === ir.TEXT && b.text)
                                 .map((b) => b.text));
  // cap 语义 = 本会话「由记忆注入产生的块」总数上限。
  // 用 extra 标记识别已注入块：原始 system 里恰好同名的文本不算注入、不占名额；
  // 已注入块占名额——保证跨轮幂等（下轮同一批记忆不会突破 cap 继续加）。
  let injectedCount = req.system.filter((b) => b.extra.injected_memory).length;
  let added = 0;
  for (const m of memories) {
    if (m === null || typeof m !== "object") continue;
    const text = String(m.content ?? m.preview ?? "").trim();
    if (!text || seen.has(text)) continue; // 空文本 / 已在 system 中 → 幂等跳过
    if (cap && injectedCount >= cap) break;
    req.system.push(new ir.Block({ kind: ir.TEXT, text,
                                   extra: { injected_memory: true } }));
    seen.add(text);
    injectedCount++;
    added++;
  }
  return added;
}

export interface ConvertResult {
  out: ir.Json;
  req: ir.IRRequest;
  dropped: Dropped;
  session: Session | null;
  injected: number;
  replayed: ir.Message[];
}

/** source 协议 payload -> target 协议 payload。
 *
 * `replayed` 是本轮**真正重放进来的历史切片**（不含本轮新消息），
 * 供埋点区分「重放代价」与「本轮输入」——两者混在一起会让重放指标系统性偏高。
 */
export function convert(source: string, target: string, payload: ir.Json,
                        headers: Record<string, string>): ConvertResult {
  const dropped = new Dropped();
  const src = ADAPTERS[source];
  const dst = ADAPTERS[target];

  const req = src.to_ir(payload, dropped);

  // 有状态：Response 的 previous_response_id -> 状态层重放历史
  let session: Session | null = null;
  let replayed: ir.Message[] = [];
  const prevId = req.extra.previous_response_id;
  if (prevId) {
    session = gw.STORE.resolvePrevious(prevId);
    if (session !== null) {
      replayed = gw.STORE.replay(session);
      req.messages = [...replayed, ...req.messages];
    } else {
      dropped.add("previous_response_id", "未找到对应会话，按无状态处理", "explicit");
    }
  }

  // 需要会话的四种情况，缺一个多轮链路就起不来：
  //   target=anthropic       —— 记忆注入 + 断点布局都要挂会话
  //   target=openai_response —— Responses 是带状态协议，网关必须回一个自己能解析的
  //                            response_id 给客户端，否则第一轮之后无从续接
  //   source=openai_response —— 客户端说 Responses 协议，下轮会带
  //                            previous_response_id 回来（第五轮自查：原条件只看
  //                            target，response→chat 方向无会话可挂，流式铸造的
  //                            responseId 无人登记，多轮链静默退化成无状态）
  //   previous_response_id 已解析出会话
  if (session === null && (target === "anthropic" || target === "openai_response" ||
                           source === "openai_response")) {
    // 空键隔离（第四轮自查）：key_fields 拼出的状态键为空（客户端没带指定
    // 请求头）时，若直接用 "" 作键，所有匿名请求会共用同一个会话——历史、
    // 记忆注入、工具 ID 映射全部跨客户端串味。退化为一次性 ephemeral 键：
    // 本轮功能完整（断点布局/记忆注入/response_id 登记均可用），会话随 TTL
    // 自然淘汰，且不与任何其他请求共享状态。
    const configuredKey = gw.CFG.sessionKey(headers);
    const key = configuredKey !== "" ? configuredKey : `ephemeral|${randomUUID()}`;
    session = gw.STORE.getOrCreate(key);
  }

  // v1.4 工具 ID 双向映射（问题清单组4#3，需会话作用域；无会话则直通）
  if (session !== null) {
    const idm = gw.STORE.idmap;
    for (const m of req.messages) {
      for (const b of m.blocks) {
        if ((b.kind === ir.TOOL_USE || b.kind === ir.TOOL_RESULT) && b.tool_id) {
          b.tool_id = idm.incoming(session.key, b.tool_id, source);
        }
      }
    }
  }

  // Anthropic 目标：按 L2 断点布局打 cache_control
  const ctx = target === "anthropic"
    ? new ir.SessionContext({ session_key: session?.key ?? "", history: replayed })
    : null;

  // 记忆注入：重放之后、渲染之前（注入内容进 system 尾部，吃 system 后断点）
  const injected = session ? injectMemories(req, session) : 0;

  if (session !== null) {
    const idm = gw.STORE.idmap;
    for (const m of req.messages) {
      for (const b of m.blocks) {
        if ((b.kind === ir.TOOL_USE || b.kind === ir.TOOL_RESULT) && b.tool_id) {
          b.tool_id = idm.outgoing(session.key, b.tool_id, target);
        }
      }
    }
  }

  const out = target === "anthropic"
    ? dst.from_ir(req, dropped, ctx)
    : dst.from_ir(req, dropped);
  return { out, req, dropped, session, injected, replayed };
}

// ---------------------------------------------------------------------------
// 上游转发（Node 不读系统代理，恒直连——与 Python 版 PB_DIRECT=1 行为一致）
// ---------------------------------------------------------------------------

function backendHeaders(target = ""): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    // 部分真实网关（如 Cloudflare 前置）按 UA 拦 bot，带浏览器 UA 更稳
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
                  "AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
  };
  if (gw.BACKEND_KEY) {
    headers.Authorization = `Bearer ${gw.BACKEND_KEY}`;
    // Anthropic 官方协议用 x-api-key；多数兼容端点两种都认，两个都带无害
    headers["x-api-key"] = gw.BACKEND_KEY;
  }
  if (target === "anthropic") {
    // Anthropic 原生端点常校验该头
    headers["anthropic-version"] = process.env.PB_ANTHROPIC_VERSION ?? "2023-06-01";
  }
  return headers;
}

/** 上游非 2xx（对齐 Python urllib.error.HTTPError：code + 可读 body）。 */
export class UpstreamHttpError extends Error {
  statusCode: number;
  body: string;

  constructor(statusCode: number, body: string) {
    super(`upstream HTTP ${statusCode}`);
    this.statusCode = statusCode;
    this.body = body;
  }
}

function requestUpstream(url: string, payload: ir.Json, target: string): Promise<http.IncomingMessage> {
  const data = Buffer.from(JSON.stringify(payload), "utf-8");
  const u = new URL(url);
  return new Promise((resolve, reject) => {
    const req = http.request({
      hostname: u.hostname,
      port: u.port || (u.protocol === "https:" ? 443 : 80),
      path: u.pathname + u.search,
      method: "POST",
      headers: { ...backendHeaders(target), "Content-Length": String(data.length) },
      timeout: 60000,
    }, (resp) => {
      if ((resp.statusCode ?? 500) >= 400) {
        const chunks: Buffer[] = [];
        resp.on("data", (c) => chunks.push(c));
        resp.on("end", () => reject(new UpstreamHttpError(
          resp.statusCode ?? 500,
          Buffer.concat(chunks).toString("utf-8").slice(0, 500))));
        return;
      }
      resolve(resp);
    });
    req.on("timeout", () => req.destroy(new Error("upstream timeout")));
    req.on("error", reject);
    req.end(data);
  });
}

export async function postJson(url: string, payload: ir.Json, target = ""): Promise<ir.Json> {
  const resp = await requestUpstream(url, payload, target);
  const chunks: Buffer[] = [];
  resp.on("data", (c) => chunks.push(c));
  await once(resp, "end");
  return JSON.parse(Buffer.concat(chunks).toString("utf-8"));
}

function openStream(url: string, payload: ir.Json, target = ""): Promise<http.IncomingMessage> {
  return requestUpstream(url, payload, target);
}

// ---------------------------------------------------------------------------
// 流尾共用逻辑（非流式响应与 SSE 流式同一条路径）
// ---------------------------------------------------------------------------

function usageFor(target: string, resp: ir.Json): ir.IRUsage {
  const u = resp.usage ?? {};
  if (target === "anthropic") return usageFromAnthropic(u);
  if (target === "openai_response") return usageFromResponse(u);
  return usageFromChat(u);
}

/** usage 归一 + 命中率埋点 + 本轮历史落库 + 登记响应 id。
 *
 * 流式场景传入的 backendResp 是 AnthropicToChatStream.syntheticResponse()
 * 拼出的非流式形状——两条路径语义一致，不另造埋点口径。
 *
 * registerResponseId（v2.2）：流式 responses 客户端时，转换器已在事件流里
 * 铸好 response id 并下发——这里登记的必须是**同一个 id**，否则客户端下轮
 * 拿它做 previous_response_id 会 404。非流式路径不传（网关在 body 里改 id）。
 */
export function finalizeTurn(target: string, backendResp: ir.Json, req: ir.IRRequest,
                             dropped: Dropped, session: Session | null,
                             replayedHist: ir.Message[], warmup: boolean,
                             registerResponseId?: string): string | null {
  const usage = usageFor(target, backendResp);
  // 重放代价只算真正重放的历史切片，不含本轮新消息
  const replayedChars = replayedHist.reduce(
    (n, m) => n + m.blocks.reduce((k, b) => k + (b.text ?? "").length, 0), 0);
  // 按 extra 标记统计（而非按尾部切片），注入块位置变化时依然准确
  const injectedChars = req.system.filter((b) => b.extra.injected_memory)
    .reduce((n, b) => n + (b.text ?? "").length, 0);
  gw.METRICS.recordTurn(usage, injectedChars, replayedChars,
                        dropped.items as unknown as ir.Json[],
                        dropped.length ? "explicit" : "none",
                        warmup ? WARMUP : NORMAL);

  // 有状态：登记响应供下一轮 previous_response_id 引用。
  // 预热轮不登记——它不产出可引用的对话轮，登记会污染响应链。
  let rid: string | null = null;
  if (session !== null && !warmup) {
    // 本轮对话必须落进会话历史，否则下一轮 previous_response_id 重放出的是空列表，
    // 整个状态层等于空转（历史永远是空 -> 重放永远是空 -> 多轮链路第一轮就断）。
    // 只追加本轮新消息：重放切片已在历史里，再加一次会自我复制。
    const newTurn = req.messages.slice(replayedHist.length);
    const reply = assistantFromUpstream(target, backendResp);
    if (reply !== null) newTurn.push(reply);
    if (newTurn.length) {
      // 落库前把 ID 翻回 canonical：newTurn 里的 ID 刚被 outgoing
      // 翻成目标协议形式，reply 里的是上游新生成的——历史必须存
      // canonical，否则跨协议续轮时映射链会退化。
      const idm = gw.STORE.idmap;
      for (const m of newTurn) {
        for (const b of m.blocks) {
          if ((b.kind === ir.TOOL_USE || b.kind === ir.TOOL_RESULT) && b.tool_id) {
            b.tool_id = idm.incoming(session.key, b.tool_id, target);
          }
        }
      }
      gw.STORE.append(session, newTurn);
    }
    rid = registerResponseId ?? gw.STORE.recordResponse(session);
    if (registerResponseId) {
      // 转换器铸造的 id：补登记进 resp_index（recordResponse 的等价物，
      // 但 id 外部给定——responses 流式帧里已经下发给客户端了）
      gw.STORE.registerResponseId(session, registerResponseId);
      rid = registerResponseId;
    }
  }
  return rid;
}

// ---------------------------------------------------------------------------
// HTTP 入口
// ---------------------------------------------------------------------------

function jsonResponse(res: http.ServerResponse, code: number, obj: ir.Json): void {
  const body = Buffer.from(JSON.stringify(obj), "utf-8");
  res.writeHead(code, { "Content-Type": "application/json; charset=utf-8",
                        "Content-Length": String(body.length) });
  res.end(body);
}

async function readBody(req: http.IncomingMessage): Promise<ir.Json> {
  const chunks: Buffer[] = [];
  req.on("data", (c) => chunks.push(c));
  await once(req, "end");
  const text = Buffer.concat(chunks).toString("utf-8");
  return text ? JSON.parse(text) : {};
}

export async function handleRequest(req: http.IncomingMessage,
                                    res: http.ServerResponse): Promise<void> {
  if (req.method !== "POST") {
    return jsonResponse(res, 405, { error: "method not allowed" });
  }
  const payload = await readBody(req);
  // 路由：/v1/{source}/to/{target}
  const parts = (req.url ?? "").split("/").filter(Boolean);

  // 管理口：POST /v1/admin/session/meta  {"key": "...", "meta": {...}}
  // 供实验脚本/未来的弹网页 Session Init 写入记忆与 memory_cap
  if (parts[0] === "v1" && parts[1] === "admin" && parts.length === 4 &&
      parts[2] === "session" && parts[3] === "meta") {
    const key = payload.key;
    const meta = payload.meta;
    if (!key || meta === null || typeof meta !== "object") {
      return jsonResponse(res, 400, { error: "need {key, meta}" });
    }
    gw.STORE.updateMeta(key, meta);
    return jsonResponse(res, 200, { ok: true, key });
  }

  if (parts.length < 4 || parts[0] !== "v1" || parts[2] !== "to") {
    return jsonResponse(res, 400, { error: "path must be /v1/{source}/to/{target}" });
  }
  const source = parts[1];
  const target = parts[3];
  // 未知协议必须回 400：不拦的话 ADAPTERS[target] 取到 undefined，
  // 转换管线直接崩、连接被掐断，客户端只看到连接重置而非错误信息。
  if (!(source in ADAPTERS) || !(target in ADAPTERS)) {
    return jsonResponse(res, 400, { error: {
      message: `unknown protocol: ${source} -> ${target}`,
      type: "invalid_request_error",
      supported: Object.keys(ADAPTERS).sort() } });
  }

  const headers: Record<string, string> = {};
  for (const [k, v] of Object.entries(req.headers)) {
    if (typeof v === "string") headers[k.toLowerCase()] = v;
  }

  const { out, req: irReq, dropped, session, injected, replayed } =
    convert(source, target, payload, headers);

  // 预热请求：单独语义，用**转换后发给上游的报文**过拒绝条件校验（方案 3.7）
  const warmup = isWarmup(payload);
  if (warmup) {
    const werr = validateWarmup(out, target);
    if (werr) {
      return jsonResponse(res, 400, { error: { message: werr,
                                               type: "invalid_request_error" } });
    }
  }

  // SSE 流式（LIMITATIONS #1，v2.2 起 9 方向全闭环）：
  //   chat 客户端 ← anthropic 上游 —— 逐块转换（v1.6）
  //   anthropic 客户端 ← chat 上游 —— 逐块转换（v2.1）
  //   同协议直通（chat / anthropic / responses）—— 字节透传 + 旁路收集（v1.7/v2.2）
  //   responses 相关四方向 —— 三维寻址归一逐块转换（v2.2）
  // warmup+stream 已在上方被 validateWarmup 拦截，走到这里的一定不是预热轮。
  if (out.stream) {
    if (source === "openai_chat" && target === "anthropic") {
      return streamFromAnthropic(res, out, irReq, dropped, session, replayed);
    }
    if (source === "anthropic" && target === "openai_chat") {
      return streamFromChatUpstream(res, out, irReq, dropped, session, replayed);
    }
    if (source === "openai_response" && target === "anthropic") {
      return streamFromAnthropicToResponse(res, out, irReq, dropped, session,
                                           replayed);
    }
    if (source === "openai_response" && target === "openai_chat") {
      return streamFromChatToResponse(res, out, irReq, dropped, session,
                                      replayed);
    }
    if (source === "anthropic" && target === "openai_response") {
      return streamFromResponses(res, out, irReq, dropped, session, replayed,
                                 "anthropic");
    }
    if (source === "openai_chat" && target === "openai_response") {
      return streamFromResponses(res, out, irReq, dropped, session, replayed,
                                 "openai_chat");
    }
    if (source === target) {
      return streamPassthrough(res, out, irReq, dropped, session, replayed,
                               target);
    }
    return jsonResponse(res, 501, { error: {
      message: `暂不支持 ${source} -> ${target} 方向的流式；请改用 stream:false`,
      type: "invalid_request_error" } });
  }

  // mock 与真实后端走同一套端点映射。tools/mock_backend 已按端点路径
  // 返回对应协议形状，不再为 mock 特判——否则离线跑的和上线跑的不是同一条路径，
  // 上线时才暴露的问题离线永远测不出来。
  const path = gw.BACKEND_PATH[target] ?? req.url ?? "";
  let backendResp: ir.Json;
  await GATE.acquire();
  try {
    backendResp = await postJson(gw.BACKEND_URL + path, out, target);
  } catch (e) {
    if (e instanceof UpstreamHttpError) {
      // 上游错误原样透出状态码，避免把 4xx/5xx 伪装成网关 500
      return jsonResponse(res, e.statusCode, {
        error: { message: "backend error", type: "upstream_error",
                 status: e.statusCode, detail: e.body } });
    }
    return jsonResponse(res, 502, {
      error: { message: `backend unreachable: ${(e as Error).message}`,
               type: "upstream_error" } });
  } finally {
    GATE.release();
  }

  const rid = finalizeTurn(target, backendResp, irReq, dropped, session,
                           replayed, warmup);

  const body: ir.Json = { ...backendResp };
  // 客户端说 Responses 协议时，下一轮会带 previous_response_id 回来。
  // 那个 id 必须是网关自己能解析的，否则多轮链路在第一轮之后就断了
  // （上游真实 id 我们解析不了，上游同样解析不了我们的 id）。
  // 上游原始 id 保留在 _bridge_upstream_id 便于对账。
  if (rid && source === "openai_response") {
    body._bridge_upstream_id = backendResp.id ?? "";
    body.id = rid;
  }
  body._bridge = { dropped: dropped.items, session: Boolean(session),
                   injected_memories: injected,
                   warmup,
                   replayed_messages: replayed.length };
  jsonResponse(res, 200, body);
}

// ---------------------------------------------------------------------------
// SSE 流式两条路径
// ---------------------------------------------------------------------------

function sseHead(res: http.ServerResponse): void {
  res.writeHead(200, { "Content-Type": "text/event-stream; charset=utf-8",
                       "Cache-Control": "no-cache",
                       "Connection": "close" });
}

/** SSE 逐块流式：Anthropic 上游事件 → Chat chunk 帧即时下发（v1.6）。
 *
 * 与非流式的差别只在传输段：转换（to_ir/from_ir）、限流、usage 归一、
 * 埋点、历史落库全部复用同一条路径（finalizeTurn + syntheticResponse）。
 */
async function streamFromAnthropic(res: http.ServerResponse, out: ir.Json,
                                   irReq: ir.IRRequest, dropped: Dropped,
                                   session: Session | null,
                                   replayed: ir.Message[]): Promise<void> {
  const path = gw.BACKEND_PATH.anthropic;
  await GATE.acquire(); // 限流：流式请求同样占并发名额
  let upstream: http.IncomingMessage;
  try {
    upstream = await openStream(gw.BACKEND_URL + path, out, "anthropic");
  } catch (e) {
    GATE.release();
    if (e instanceof UpstreamHttpError) {
      return jsonResponse(res, e.statusCode, {
        error: { message: "backend error", type: "upstream_error",
                 status: e.statusCode, detail: e.body } });
    }
    return jsonResponse(res, 502, {
      error: { message: `backend unreachable: ${(e as Error).message}`,
               type: "upstream_error" } });
  }

  sseHead(res);
  const conv = new AnthropicToChatStream();
  const parser = new SseEventParser();
  const decoder = new TextDecoder("utf-8", { fatal: false });
  try {
    for await (const chunk of upstream) {
      const text = decoder.decode(chunk as Buffer, { stream: true });
      for (const [event, data] of parser.feed(text)) {
        for (const frame of conv.feed(event, data)) {
          if (!res.write(frame)) await once(res, "drain");
        }
      }
    }
    const tail = decoder.decode();
    for (const [event, data] of parser.feed(tail)) {
      for (const frame of conv.feed(event, data)) res.write(frame);
    }
  } catch {
    // 客户端中途断开：流尾落库/埋点照常（这轮 API 费用已实际发生）
  } finally {
    upstream.destroy();
    res.end();
    GATE.release();
  }

  // 流尾：与非流式同一套 usage 归一 + 埋点 + 历史落库。
  // dropped 清单对流式客户端不可见（SSE 帧里没有它的位置）——
  // 已写进 LIMITATIONS #1 的范围说明，埋点里 degradation 仍如实记录。
  finalizeTurn("anthropic", conv.syntheticResponse(), irReq, dropped,
               session, replayed, false);
}

/** SSE 逐块流式（v2.1 新增方向）：Chat 上游 chunk → Anthropic 事件即时下发。
 *
 * 与 streamFromAnthropic 互为逆方向，结构对称：
 * 转换器（ChatToAnthropicStream）产客户端帧，收集器（ChatStreamCollector）
 * 旁路攒流尾汇总——转换、限流、usage 归一、埋点、历史落库全部复用同一条
 * 路径（finalizeTurn + syntheticResponse），不另造语义。
 */
async function streamFromChatUpstream(res: http.ServerResponse, out: ir.Json,
                                      irReq: ir.IRRequest, dropped: Dropped,
                                      session: Session | null,
                                      replayed: ir.Message[]): Promise<void> {
  const path = gw.BACKEND_PATH.openai_chat;
  // 让上游在流尾带 usage 块（OpenAI 系默认不带）：客户端收到的是转换后的
  // Anthropic 事件、看不到原始 chunk，注入该参数对客户端不可见、零副作用，
  // 但流尾的 usage 归一与命中率埋点就有了真实数据（不编造）。
  out.stream_options = { ...(out.stream_options ?? {}), include_usage: true };
  await GATE.acquire(); // 限流：流式请求同样占并发名额
  let upstream: http.IncomingMessage;
  try {
    upstream = await openStream(gw.BACKEND_URL + path, out, "openai_chat");
  } catch (e) {
    GATE.release();
    if (e instanceof UpstreamHttpError) {
      return jsonResponse(res, e.statusCode, {
        error: { message: "backend error", type: "upstream_error",
                 status: e.statusCode, detail: e.body } });
    }
    return jsonResponse(res, 502, {
      error: { message: `backend unreachable: ${(e as Error).message}`,
               type: "upstream_error" } });
  }

  sseHead(res);
  const conv = new ChatToAnthropicStream();
  const collector = new ChatStreamCollector(); // 旁路汇总，供 finalizeTurn 复用
  const parser = new SseEventParser();
  const decoder = new TextDecoder("utf-8", { fatal: false });
  const feed = (text: string): string[] => {
    const frames: string[] = [];
    for (const [, data] of parser.feed(text)) {
      const s = typeof data === "string" ? data : JSON.stringify(data);
      frames.push(...conv.feedData(s));
      collector.feedData(s);
    }
    return frames;
  };
  try {
    for await (const chunk of upstream) {
      const text = decoder.decode(chunk as Buffer, { stream: true });
      for (const frame of feed(text)) {
        if (!res.write(frame)) await once(res, "drain");
      }
    }
    for (const frame of feed(decoder.decode())) res.write(frame);
  } catch {
    // 客户端中途断开：流尾落库/埋点照常（这轮 API 费用已实际发生）
  } finally {
    upstream.destroy();
    res.end();
    GATE.release();
  }

  // 流尾：与非流式同一套 usage 归一 + 埋点 + 历史落库（target 是上游协议 chat）
  finalizeTurn("openai_chat", collector.syntheticResponse(), irReq, dropped,
               session, replayed, false);
}

/** SSE 逐块流式（v2.2）：responses 客户端 ← anthropic 上游。
 * 转换器铸造的 responseId 随帧下发，finalizeTurn 登记同一个 id 维持多轮链。 */
async function streamFromAnthropicToResponse(
    res: http.ServerResponse, out: ir.Json, irReq: ir.IRRequest,
    dropped: Dropped, session: Session | null,
    replayed: ir.Message[]): Promise<void> {
  const path = gw.BACKEND_PATH.anthropic;
  await GATE.acquire();
  let upstream: http.IncomingMessage;
  try {
    upstream = await openStream(gw.BACKEND_URL + path, out, "anthropic");
  } catch (e) {
    GATE.release();
    return upstreamErrorResponse(res, e);
  }

  sseHead(res);
  const conv = new AnthropicToResponseStream();
  const collector = new AnthropicToChatStream(); // 旁路汇总（帧丢弃）
  const parser = new SseEventParser();
  const decoder = new TextDecoder("utf-8", { fatal: false });
  try {
    for await (const chunk of upstream) {
      const text = decoder.decode(chunk as Buffer, { stream: true });
      for (const [event, data] of parser.feed(text)) {
        collector.feed(event, data);
        for (const frame of conv.feedEvent(event, data)) {
          if (!res.write(frame)) await once(res, "drain");
        }
      }
    }
    const tail = decoder.decode();
    for (const [event, data] of parser.feed(tail)) {
      collector.feed(event, data);
      for (const frame of conv.feedEvent(event, data)) res.write(frame);
    }
  } catch {
    // 客户端中途断开：流尾落库/埋点照常（这轮 API 费用已实际发生）
  } finally {
    upstream.destroy();
    res.end();
    GATE.release();
  }
  finalizeTurn("anthropic", collector.syntheticResponse(), irReq, dropped,
               session, replayed, false, conv.responseId);
}

/** SSE 逐块流式（v2.2）：responses 客户端 ← chat 上游。
 * 注入 stream_options.include_usage（客户端只见转换后的 Responses 帧，不可见）。 */
async function streamFromChatToResponse(
    res: http.ServerResponse, out: ir.Json, irReq: ir.IRRequest,
    dropped: Dropped, session: Session | null,
    replayed: ir.Message[]): Promise<void> {
  const path = gw.BACKEND_PATH.openai_chat;
  out.stream_options = { ...(out.stream_options ?? {}), include_usage: true };
  await GATE.acquire();
  let upstream: http.IncomingMessage;
  try {
    upstream = await openStream(gw.BACKEND_URL + path, out, "openai_chat");
  } catch (e) {
    GATE.release();
    return upstreamErrorResponse(res, e);
  }

  sseHead(res);
  const conv = new ChatToResponseStream();
  const collector = new ChatStreamCollector(); // 旁路汇总（帧丢弃）
  const parser = new SseEventParser();
  const decoder = new TextDecoder("utf-8", { fatal: false });
  try {
    for await (const chunk of upstream) {
      const text = decoder.decode(chunk as Buffer, { stream: true });
      for (const [, data] of parser.feed(text)) {
        const s = typeof data === "string" ? data : JSON.stringify(data);
        collector.feedData(s);
        for (const frame of conv.feedData(s)) {
          if (!res.write(frame)) await once(res, "drain");
        }
      }
    }
    const tail = decoder.decode();
    for (const [, data] of parser.feed(tail)) {
      const s = typeof data === "string" ? data : JSON.stringify(data);
      collector.feedData(s);
      for (const frame of conv.feedData(s)) res.write(frame);
    }
  } catch {
    // 客户端中途断开：流尾落库/埋点照常
  } finally {
    upstream.destroy();
    res.end();
    GATE.release();
  }
  finalizeTurn("openai_chat", collector.syntheticResponse(), irReq, dropped,
               session, replayed, false, conv.responseId);
}

/** SSE 逐块流式（v2.2）：anthropic/chat 客户端 ← responses 上游。
 * clientProto 决定下游帧形状；上游 response.completed 自带完整响应，
 * ResponseStreamCollector 直接取作流尾汇总。 */
async function streamFromResponses(
    res: http.ServerResponse, out: ir.Json, irReq: ir.IRRequest,
    dropped: Dropped, session: Session | null,
    replayed: ir.Message[], clientProto: string): Promise<void> {
  const path = gw.BACKEND_PATH.openai_response;
  await GATE.acquire();
  let upstream: http.IncomingMessage;
  try {
    upstream = await openStream(gw.BACKEND_URL + path, out, "openai_response");
  } catch (e) {
    GATE.release();
    return upstreamErrorResponse(res, e);
  }

  sseHead(res);
  const conv = clientProto === "anthropic"
    ? new ResponseToAnthropicStream() : new ResponseToChatStream();
  const collector = new ResponseStreamCollector();
  const parser = new SseEventParser();
  const decoder = new TextDecoder("utf-8", { fatal: false });
  try {
    for await (const chunk of upstream) {
      const text = decoder.decode(chunk as Buffer, { stream: true });
      for (const [event, data] of parser.feed(text)) {
        collector.feedEvent(event, data);
        for (const frame of conv.feedEvent(event, data)) {
          if (!res.write(frame)) await once(res, "drain");
        }
      }
    }
    const tail = decoder.decode();
    for (const [event, data] of parser.feed(tail)) {
      collector.feedEvent(event, data);
      for (const frame of conv.feedEvent(event, data)) res.write(frame);
    }
  } catch {
    // 客户端中途断开：流尾落库/埋点照常
  } finally {
    upstream.destroy();
    res.end();
    GATE.release();
  }
  finalizeTurn("openai_response", collector.syntheticResponse(), irReq,
               dropped, session, replayed, false);
}

/** 上游错误的统一响应（v2.2 抽出：四个流式入口共用）。 */
function upstreamErrorResponse(res: http.ServerResponse, e: unknown): void {
  if (e instanceof UpstreamHttpError) {
    jsonResponse(res, e.statusCode, {
      error: { message: "backend error", type: "upstream_error",
               status: e.statusCode, detail: e.body } });
    return;
  }
  jsonResponse(res, 502, {
    error: { message: `backend unreachable: ${(e as Error).message}`,
             type: "upstream_error" } });
}

/** 同协议直通流式（v1.7）：字节原样透传，旁路收集流尾汇总。
 *
 * 与转换流式的差别：转发的是上游**原始字节**（一个比特都不动），
 * 收集器只负责把 usage / 文本攒下来供 finalizeTurn 复用。
 */
async function streamPassthrough(res: http.ServerResponse, out: ir.Json,
                                 irReq: ir.IRRequest, dropped: Dropped,
                                 session: Session | null,
                                 replayed: ir.Message[], target: string): Promise<void> {
  const path = gw.BACKEND_PATH[target] ?? "";
  await GATE.acquire();
  let upstream: http.IncomingMessage;
  try {
    upstream = await openStream(gw.BACKEND_URL + path, out, target);
  } catch (e) {
    GATE.release();
    if (e instanceof UpstreamHttpError) {
      return jsonResponse(res, e.statusCode, {
        error: { message: "backend error", type: "upstream_error",
                 status: e.statusCode, detail: e.body } });
    }
    return jsonResponse(res, 502, {
      error: { message: `backend unreachable: ${(e as Error).message}`,
               type: "upstream_error" } });
  }

  sseHead(res);
  // 复用转换器/收集器做旁路汇总（其产出帧丢弃，只要 usage/文本）
  const anthropicCollector = target === "anthropic" ? new AnthropicToChatStream() : null;
  const responseCollector = target === "openai_response" ? new ResponseStreamCollector() : null;
  const chatCollector = (anthropicCollector || responseCollector) ? null : new ChatStreamCollector();
  const parser = new SseEventParser();
  const decoder = new TextDecoder("utf-8", { fatal: false });
  try {
    for await (const chunk of upstream) {
      // 字节原样透传（背压感知）
      if (!res.write(chunk)) await once(res, "drain");
      const text = decoder.decode(chunk as Buffer, { stream: true });
      if (anthropicCollector) {
        for (const [event, data] of parser.feed(text)) {
          anthropicCollector.feed(event, data);
        }
      } else if (responseCollector) {
        for (const [event, data] of parser.feed(text)) {
          responseCollector.feedEvent(event, data);
        }
      } else {
        for (const [event, data] of parser.feed(text)) {
          void event;
          // Chat 流是裸 data: 帧：parseSseLines 语义下 data 可能是字符串
          if (typeof data === "string") chatCollector!.feedData(data);
          else chatCollector!.feedData(JSON.stringify(data));
        }
      }
    }
    const tail = decoder.decode();
    if (anthropicCollector) {
      for (const [event, data] of parser.feed(tail)) anthropicCollector.feed(event, data);
    } else if (responseCollector) {
      for (const [event, data] of parser.feed(tail)) responseCollector.feedEvent(event, data);
    } else {
      for (const [, data] of parser.feed(tail)) {
        if (typeof data === "string") chatCollector!.feedData(data);
        else chatCollector!.feedData(JSON.stringify(data));
      }
    }
  } catch {
    // 客户端中途断开：用已收到的部分照常落账（费用已实际发生）
  } finally {
    upstream.destroy();
    res.end();
    GATE.release();
  }

  const synthetic = anthropicCollector
    ? anthropicCollector.syntheticResponse()
    : responseCollector
      ? responseCollector.syntheticResponse()
      : chatCollector!.syntheticResponse();
  finalizeTurn(target, synthetic, irReq, dropped, session, replayed, false);
}

// ---------------------------------------------------------------------------

export function createServer(): http.Server {
  return http.createServer((req, res) => {
    handleRequest(req, res).catch((e) => {
      // 兜底：任何未预期异常回 500 JSON，不能让连接被裸掐断
      try {
        jsonResponse(res, 500, { error: { message: String(e), type: "internal_error" } });
      } catch { /* 连接已断则放弃 */ }
    });
  });
}

// 直接运行（node src/gateway/server.ts）时启动网关；被测试 import 时不启动
if (process.argv[1] && /server\.ts$/.test(process.argv[1])) {
  const srv = createServer();
  srv.listen(gw.BIND_PORT, gw.BIND_HOST, () => {
    console.log(`protocol-bridge gateway(ts) on http://${gw.BIND_HOST}:${gw.BIND_PORT} -> backend ${gw.BACKEND_URL}`);
  });
}
