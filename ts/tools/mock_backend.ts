/** mock backend：离线顶替真实 Anthropic/OpenAI 端点，供网关跑通链路。
 * （TS 移植自 tools/mock_backend.py）
 *
 * 按**请求路径**返回对应协议的响应形状：
 *   /v1/messages        -> Anthropic Messages
 *   /responses          -> OpenAI Responses
 *   /chat/completions   -> OpenAI Chat Completions
 *
 * 缓存行为为**有状态模拟**：按带 cache_control 的前缀内容哈希记账——首次见到某
 * 前缀报 cache_creation，再次见到同一前缀报 cache_read，供离线验证
 * 「预热 -> 命中」两步链路。不代表真实缓存行为（无 TTL、无最小长度门槛）。
 *
 * 注意：前缀哈希只认 system / messages / input 里的文本块，且只有带
 * cache_control 的块才算前缀末尾——与 Anthropic 的语义一致，但比真实实现粗糙。
 */

import http from "node:http";
import { createHash } from "node:crypto";
import { once } from "node:events";
import type { Json } from "../src/ir/model.ts";

const BIND = { host: "127.0.0.1", port: 9100 };

// 已建缓存的前缀哈希集合（模拟 cache 命名空间）
const SEEN_PREFIXES = new Set<string>();

function prefixHashUptoBreakpoint(payload: Json): [string | null, number] {
  /** 找到最后一个带 cache_control 的块，返回 [前缀哈希, 前缀字符数]。 */
  const parts: string[] = [];
  let lastMarked = -1;

  function walk(blocks: unknown[]): void {
    for (const b of blocks) {
      const isObj = b !== null && typeof b === "object";
      parts.push(isObj ? ((b as Json).text ?? "") : String(b));
      if (isObj && (b as Json).cache_control) lastMarked = parts.length - 1;
    }
  }

  const sys = payload.system;
  if (typeof sys === "string") parts.push(sys);
  else if (Array.isArray(sys)) walk(sys);
  for (const m of payload.messages ?? []) {
    const c = m?.content;
    walk(Array.isArray(c) ? c : [{ text: String(c ?? "") }]);
  }
  // Responses 协议：条目在 input 里
  for (const item of payload.input ?? []) {
    if (item?.type !== "message") continue;
    const c = item.content;
    walk(Array.isArray(c) ? c : [{ text: String(c ?? "") }]);
  }

  if (lastMarked < 0) return [null, 0];
  const prefix = parts.slice(0, lastMarked + 1).join("");
  return [createHash("sha256").update(prefix, "utf-8").digest("hex"), prefix.length];
}

/** 按前缀哈希记账，返回 [cache_creation, cache_read]。 */
function cacheTick(payload: Json): [number, number] {
  const [ph, plen] = prefixHashUptoBreakpoint(payload);
  if (ph === null) return [0, 0];
  if (SEEN_PREFIXES.has(ph)) return [0, plen]; // 再次见到同一前缀 -> 读命中
  SEEN_PREFIXES.add(ph);
  return [plen, 0]; // 首次 -> 写缓存
}

/** 粗略的输入量（字符数），只为让 usage 各字段非零可看。 */
function inputChars(payload: Json): number {
  let n = 512;
  const sys = payload.system;
  if (typeof sys === "string") n += sys.length;
  else if (Array.isArray(sys)) n += sys.reduce((k, b) => k + (b?.text?.length ?? 0), 0);
  for (const m of payload.messages ?? []) {
    const c = m?.content;
    n += typeof c === "string" ? c.length
       : (Array.isArray(c) ? c.reduce((k, b) => k + (b?.text?.length ?? 0), 0) : 0);
  }
  for (const item of payload.input ?? []) {
    const c = item?.content;
    n += typeof c === "string" ? c.length
       : (Array.isArray(c) ? c.reduce((k, b) => k + (b?.text?.length ?? 0), 0) : 0);
  }
  return n;
}

function isWarmupPayload(payload: Json): boolean {
  for (const k of ["max_tokens", "max_output_tokens", "max_completion_tokens"]) {
    try {
      if (Number.parseInt(String(payload[k] ?? 1), 10) === 0) return true;
    } catch { /* 继续看下一个字段 */ }
  }
  return false;
}

// ---------------------------------------------------------------------------
// 三种协议各自的响应形状
// ---------------------------------------------------------------------------

function anthropicResponse(payload: Json): Json {
  const [creation, read] = cacheTick(payload);
  const usage = { input_tokens: inputChars(payload),
                  output_tokens: isWarmupPayload(payload) ? 0 : 12,
                  cache_creation_input_tokens: creation,
                  cache_read_input_tokens: read };
  if (isWarmupPayload(payload)) {
    // max_tokens:0 预热：空 content + stop_reason=max_tokens（官方行为）
    return { id: "msg_mock_warmup", type: "message", role: "assistant",
             content: [], model: payload.model ?? "mock",
             stop_reason: "max_tokens", usage };
  }
  return { id: "msg_mock01", type: "message", role: "assistant",
           content: [{ type: "text", text: "mock reply" }],
           model: payload.model ?? "mock", stop_reason: "end_turn", usage };
}

function openaiResponseResponse(payload: Json): Json {
  const [, read] = cacheTick(payload);
  const usage = { input_tokens: inputChars(payload),
                  output_tokens: isWarmupPayload(payload) ? 0 : 12,
                  input_tokens_details: { cached_tokens: read },
                  output_tokens_details: { reasoning_tokens: 0 } };
  if (isWarmupPayload(payload)) {
    return { id: "resp_mock_warmup", object: "response", output: [],
             model: payload.model ?? "mock", status: "incomplete",
             incomplete_details: { reason: "max_output_tokens" }, usage };
  }
  return { id: "resp_mock01", object: "response",
           output: [{ type: "message", role: "assistant", status: "completed",
                      content: [{ type: "output_text", text: "mock reply",
                                  annotations: [] }] }],
           model: payload.model ?? "mock", status: "completed", usage };
}

function chatResponse(payload: Json): Json {
  const [, read] = cacheTick(payload);
  const usage = { prompt_tokens: inputChars(payload),
                  completion_tokens: isWarmupPayload(payload) ? 0 : 12,
                  prompt_tokens_details: { cached_tokens: read } };
  const content = isWarmupPayload(payload) ? "" : "mock reply";
  return { id: "chatcmpl_mock01", object: "chat.completion",
           choices: [{ index: 0,
                       message: { role: "assistant", content },
                       finish_reason: isWarmupPayload(payload) ? "length" : "stop" }],
           model: payload.model ?? "mock", usage };
}

function payloadResponse(payload: Json, path: string): Json {
  /** 按请求路径挑响应形状（网关转发真实后端时路径是协议专属端点）。 */
  if (path.replace(/\/$/, "").endsWith("/responses")) return openaiResponseResponse(payload);
  if (path.includes("chat/completions")) return chatResponse(payload);
  return anthropicResponse(payload);
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** 流式响应。Anthropic 路径发**完整官方事件序列**（message_start →
 * content_block_* → message_delta → message_stop，带 usage 与缓存字段），
 * 供网关 SSE 逐块转换的离线测试；chat 路径发 Chat 形状 chunk；
 * responses 路径发官方三维寻址事件序列（response.created →
 * output_item/content_part.added → output_text.delta* → *.done →
 * response.completed，completed 自带完整响应与 usage）。 */
async function* sseLines(payload: Json, path: string): AsyncGenerator<string> {
  if (path.replace(/\/$/, "").endsWith("/responses")) {
    const [, read] = cacheTick(payload);
    const chunks = ["mock ", "stream ", "reply"];
    const text = chunks.join("");
    const respId = "resp_mock_stream";
    const itemId = "item_mock_1";
    const ev = (name: string, obj: Json): string =>
      `event: ${name}\ndata: ${JSON.stringify({ type: name, ...obj })}\n\n`;
    yield ev("response.created", { response: {
      id: respId, object: "response", created_at: Math.floor(Date.now() / 1000),
      status: "in_progress", model: payload.model ?? "mock", output: [] } });
    yield ev("response.output_item.added", { output_index: 0, item: {
      id: itemId, type: "message", role: "assistant",
      status: "in_progress", content: [] } });
    yield ev("response.content_part.added", {
      item_id: itemId, output_index: 0, content_index: 0,
      part: { type: "output_text", text: "", annotations: [] } });
    for (const c of chunks) {
      yield ev("response.output_text.delta", {
        item_id: itemId, output_index: 0, content_index: 0, delta: c });
      await sleep(10);
    }
    yield ev("response.output_text.done", {
      item_id: itemId, output_index: 0, content_index: 0, text });
    yield ev("response.content_part.done", {
      item_id: itemId, output_index: 0, content_index: 0,
      part: { type: "output_text", text, annotations: [] } });
    yield ev("response.output_item.done", { output_index: 0, item: {
      id: itemId, type: "message", role: "assistant", status: "completed",
      content: [{ type: "output_text", text, annotations: [] }] } });
    yield ev("response.completed", { response: {
      id: respId, object: "response", created_at: Math.floor(Date.now() / 1000),
      status: "completed", model: payload.model ?? "mock",
      output: [{ id: itemId, type: "message", role: "assistant",
                 status: "completed",
                 content: [{ type: "output_text", text, annotations: [] }] }],
      usage: { input_tokens: inputChars(payload), output_tokens: 12,
               input_tokens_details: { cached_tokens: read },
               output_tokens_details: { reasoning_tokens: 0 } } } });
    return;
  }
  if (path.replace(/\/$/, "").endsWith("/v1/messages") || path.endsWith("/messages")) {
    const [creation, read] = cacheTick(payload);
    const chunks = ["mock ", "stream ", "reply"];
    const msgStart = { type: "message_start", message: {
      id: "msg_mock_stream", type: "message", role: "assistant",
      model: payload.model ?? "mock", content: [],
      usage: { input_tokens: inputChars(payload), output_tokens: 1,
               cache_creation_input_tokens: creation,
               cache_read_input_tokens: read } } };
    yield "event: message_start\n";
    yield `data: ${JSON.stringify(msgStart)}\n\n`;
    yield "event: content_block_start\n";
    yield 'data: {"type":"content_block_start","index":0,' +
          '"content_block":{"type":"text","text":""}}\n\n';
    for (const c of chunks) {
      const delta = { type: "content_block_delta", index: 0,
                      delta: { type: "text_delta", text: c } };
      yield "event: content_block_delta\n";
      yield `data: ${JSON.stringify(delta)}\n\n`;
      await sleep(10);
    }
    yield "event: content_block_stop\n";
    yield 'data: {"type":"content_block_stop","index":0}\n\n';
    yield "event: message_delta\n";
    yield 'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},' +
          '"usage":{"output_tokens":12}}\n\n';
    yield "event: message_stop\n";
    yield 'data: {"type":"message_stop"}\n\n';
    return;
  }
  if (path.includes("chat/completions")) {
    const [, read] = cacheTick(payload);
    const chunks = ["mock ", "stream ", "reply"];
    for (const [i, c] of chunks.entries()) {
      const delta = i === 0 ? { role: "assistant", content: c } : { content: c };
      const frame = { id: "chatcmpl_mock_stream", object: "chat.completion.chunk",
                      created: Math.floor(Date.now() / 1000),
                      model: payload.model ?? "mock",
                      choices: [{ index: 0, delta, finish_reason: null }] };
      yield `data: ${JSON.stringify(frame)}\n\n`;
      await sleep(10);
    }
    const fin = { id: "chatcmpl_mock_stream", object: "chat.completion.chunk",
                  created: Math.floor(Date.now() / 1000),
                  model: payload.model ?? "mock",
                  choices: [{ index: 0, delta: {}, finish_reason: "stop" }],
                  usage: { prompt_tokens: inputChars(payload),
                           completion_tokens: 12,
                           prompt_tokens_details: { cached_tokens: read } } };
    yield `data: ${JSON.stringify(fin)}\n\n`;
    yield "data: [DONE]\n\n";
    return;
  }
  for (const c of ["mock ", "stream ", "reply"]) {
    yield `data: ${JSON.stringify({ type: "content_block_delta",
                                    delta: { type: "text_delta", text: c } })}\n\n`;
    await sleep(10);
  }
  yield 'data: {"type": "message_stop"}\n\n';
}

export async function mockHandler(req: http.IncomingMessage,
                                  res: http.ServerResponse): Promise<void> {
  if (req.method !== "POST") {
    res.writeHead(405).end();
    return;
  }
  const chunks: Buffer[] = [];
  req.on("data", (c) => chunks.push(c));
  await once(req, "end");
  const payload: Json = JSON.parse(Buffer.concat(chunks).toString("utf-8") || "{}");
  if (payload.stream) {
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    for await (const line of sseLines(payload, req.url ?? "")) {
      res.write(line);
    }
    res.end();
    return;
  }
  const body = Buffer.from(JSON.stringify(payloadResponse(payload, req.url ?? "")),
                           "utf-8");
  res.writeHead(200, { "Content-Type": "application/json; charset=utf-8",
                       "Content-Length": String(body.length) });
  res.end(body);
}

if (process.argv[1] && /mock_backend\.ts$/.test(process.argv[1])) {
  http.createServer((req, res) => { void mockHandler(req, res); })
    .listen(BIND.port, BIND.host, () => {
      console.log(`mock backend(ts) on http://${BIND.host}:${BIND.port}`);
    });
}
