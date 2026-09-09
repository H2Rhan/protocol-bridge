/** SSE 流式三件套（TS 移植自 src/gateway/sse.py，对应 v1.6/v1.7）：
 *
 * - parseSseLines：把 SSE 行流解析成 (event, data) 事件（单测用，同步可迭代输入）
 * - SseEventParser：增量分帧器（网关用，按 chunk 喂入，吐出完整事件）
 * - AnthropicToChatStream：Anthropic 事件 → Chat chunk 逐块转换
 * - ChatStreamCollector：chat←chat 直通流式的旁路收集器
 *
 * 范围（LIMITATIONS #1）：chat 客户端 ← anthropic 上游逐块转换 +
 * chat←chat / anthropic←anthropic 同协议透传；其余方向显式 501。
 */

import type { Json } from "../ir/model.ts";

// Anthropic stop_reason → Chat finish_reason
const STOP_MAP: Record<string, string> = {
  end_turn: "stop", stop_sequence: "stop", max_tokens: "length",
  tool_use: "tool_calls", refusal: "stop",
};

export type SseEvent = [string | null, unknown];

/** 把 SSE 行流解析成 (event, data) 事件对，逐事件 yield（单测/离线用）。
 *
 * data 按 SSE 规范可多行拼接；能解析成 JSON 就给对象，否则给原字符串。
 * 流尾残留的半帧容错丢弃（正常结束的上游最后一定是 message_stop 完整帧）。
 */
export function* parseSseLines(lineIter: Iterable<string>): Generator<SseEvent> {
  let event: string | null = null;
  let dataLines: string[] = [];
  for (const raw of lineIter) {
    const line = (typeof raw === "string" ? raw : String(raw)).replace(/\r?\n$/, "");
    if (!line) {
      if (dataLines.length) {
        const text = dataLines.join("\n");
        let data: unknown = text;
        try { data = JSON.parse(text); } catch { /* 原样透传 */ }
        yield [event, data];
      }
      event = null;
      dataLines = [];
      continue;
    }
    if (line.startsWith(":")) continue; // 注释 / 心跳行
    const idx = line.indexOf(":");
    const field = idx >= 0 ? line.slice(0, idx) : line;
    const value = (idx >= 0 ? line.slice(idx + 1) : "").replace(/^ /, "");
    if (field === "event") event = value;
    else if (field === "data") dataLines.push(value);
  }
}

/** 增量分帧器：按 chunk 喂入 UTF-8 文本，吐出完整 (event, data) 事件。
 * 跨 chunk 的半行、半帧都缓冲，适配上游任意切片。 */
export class SseEventParser {
  private buf = "";

  /** 喂入一段文本（可以是不完整行），返回本次凑齐的事件。 */
  feed(chunk: string): SseEvent[] {
    this.buf += chunk;
    const events: SseEvent[] = [];
    let idx: number;
    while ((idx = this.buf.indexOf("\n")) >= 0) {
      const line = this.buf.slice(0, idx).replace(/\r$/, "");
      this.buf = this.buf.slice(idx + 1);
      const ev = this.feedLine(line);
      if (ev) events.push(ev);
    }
    return events;
  }

  private event: string | null = null;
  private dataLines: string[] = [];

  private feedLine(line: string): SseEvent | null {
    if (!line) {
      if (this.dataLines.length) {
        const text = this.dataLines.join("\n");
        let data: unknown = text;
        try { data = JSON.parse(text); } catch { /* 原样透传 */ }
        const out: SseEvent = [this.event, data];
        this.event = null;
        this.dataLines = [];
        return out;
      }
      return null;
    }
    if (line.startsWith(":")) return null;
    const idx = line.indexOf(":");
    const field = idx >= 0 ? line.slice(0, idx) : line;
    const value = (idx >= 0 ? line.slice(idx + 1) : "").replace(/^ /, "");
    if (field === "event") this.event = value;
    else if (field === "data") this.dataLines.push(value);
    return null;
  }
}

/** Anthropic SSE 事件 → Chat completion.chunk 帧（逐块、可即时下发）。
 *
 * 用法：每个上游事件 feed() 一次，把返回的帧立即写给客户端；
 * 流结束后用 usage() / syntheticResponse() 做埋点与历史落库。
 */
export class AnthropicToChatStream {
  msgId = "chatcmpl_stream";
  model = "";
  created = Math.floor(Date.now() / 1000);
  private usageIn: Json = {};
  private outputTokens = 0;
  private textParts: string[] = [];
  // index -> {id,name,args[]}；input_json_delta 逐段累积
  private toolOpen = new Map<number, { id: string; name: string; args: string[] }>();
  private toolsDone: { id: string; name: string; args: string[] }[] = [];
  private sentRole = false;

  private chunk(delta: Json, finish: string | null = null): string {
    const frame = {
      id: this.msgId, object: "chat.completion.chunk",
      created: this.created, model: this.model,
      choices: [{ index: 0, delta, finish_reason: finish }],
    };
    return "data: " + JSON.stringify(frame) + "\n\n";
  }

  private roleFrame(): string {
    this.sentRole = true;
    return this.chunk({ role: "assistant" });
  }

  /** 喂一个 Anthropic 事件，返回要立即下发客户端的 SSE 帧（可为空数组）。 */
  feed(event: string | null, data: unknown): string[] {
    if (data === null || typeof data !== "object") return [];
    const d = data as Json;
    const etype: string = d.type ?? event ?? "";
    const out: string[] = [];
    if (etype === "message_start") {
      const msg = d.message ?? {};
      this.msgId = msg.id ?? this.msgId;
      this.model = msg.model ?? this.model;
      this.usageIn = msg.usage ?? {};
      out.push(this.roleFrame());
    } else if (etype === "content_block_start") {
      const idx = d.index ?? 0;
      const block = d.content_block ?? {};
      if (block.type === "tool_use") {
        this.toolOpen.set(idx, { id: block.id ?? "", name: block.name ?? "", args: [] });
      }
    } else if (etype === "content_block_delta") {
      const idx = d.index ?? 0;
      const delta = d.delta ?? {};
      const dt = delta.type;
      if (dt === "text_delta") {
        const text = delta.text ?? "";
        this.textParts.push(text);
        if (!this.sentRole) out.push(this.roleFrame());
        out.push(this.chunk({ content: text }));
      } else if (dt === "input_json_delta") {
        const tb = this.toolOpen.get(idx);
        if (tb) tb.args.push(delta.partial_json ?? "");
      }
      // thinking_delta / signature_delta：Chat 无对应概念，不落帧
    } else if (etype === "content_block_stop") {
      const idx = d.index ?? 0;
      const tb = this.toolOpen.get(idx);
      if (tb) {
        this.toolOpen.delete(idx);
        this.toolsDone.push(tb);
        // 工具参数缓冲到块结束一次性发出（input_json_delta 非完整 JSON）
        out.push(this.chunk({ tool_calls: [{
          index: 0, id: tb.id, type: "function",
          function: { name: tb.name, arguments: tb.args.join("") } }] }));
      }
    } else if (etype === "message_delta") {
      const delta = d.delta ?? {};
      const usage = d.usage ?? {};
      this.outputTokens = usage.output_tokens ?? this.outputTokens;
      const sr = delta.stop_reason;
      if (sr) out.push(this.chunk({}, STOP_MAP[sr] ?? "stop"));
    } else if (etype === "message_stop") {
      out.push("data: [DONE]\n\n");
    } else if (etype === "error") {
      const err = d.error ?? {};
      out.push(this.chunk({ content: `[上游错误] ${err.message ?? "unknown"}` }));
    }
    // ping 等控制事件：不落帧
    return out;
  }

  /** 合并 message_start / message_delta 的 usage（保持 Anthropic 口径）。 */
  usage(): Json {
    return { ...this.usageIn, output_tokens: this.outputTokens };
  }

  /** 把累计内容拼成 Anthropic 非流式响应形状——直接复用
   * assistantFromUpstream("anthropic", ...) 与 usageFor，
   * 流式与非流式的落库/埋点走同一条代码路径，不另造语义。 */
  syntheticResponse(): Json {
    const content: Json[] = [];
    const text = this.textParts.join("");
    if (text) content.push({ type: "text", text });
    const done = [...this.toolsDone];
    // 容错：流被截断时未正常 content_block_stop 的工具块也入史
    for (const i of [...this.toolOpen.keys()].sort((a, b) => a - b)) {
      done.push(this.toolOpen.get(i)!);
    }
    for (const tb of done) {
      let toolInput: Json = {};
      try { toolInput = JSON.parse(tb.args.join("") || "{}"); } catch { /* 容错 */ }
      content.push({ type: "tool_use", id: tb.id, name: tb.name, input: toolInput });
    }
    return { content, usage: this.usage() };
  }
}

/** chat←chat 直通流式的流尾收集器（不转换，只汇总）。
 *
 * Chat 流式响应默认不带 usage（除非客户端传 stream_options.include_usage），
 * 收不到就是 0——如实记录，不编造。
 */
export class ChatStreamCollector {
  private textParts: string[] = [];
  private usageData: Json = {};
  // index -> {id,name,args[]}（Chat 流的工具参数也是分段 delta）
  private toolSlots = new Map<number, { id: string; name: string; args: string[] }>();

  /** 喂一行 SSE data 内容（已去掉 'data:' 前缀）。 */
  feedData(dataStr: string): void {
    const s = dataStr.trim();
    if (!s || s === "[DONE]") return;
    let d: Json;
    try { d = JSON.parse(s); } catch { return; }
    if (d.usage !== null && typeof d.usage === "object") this.usageData = d.usage;
    for (const ch of d.choices ?? []) {
      const delta = ch.delta ?? {};
      if (delta.content) this.textParts.push(delta.content);
      for (const tc of delta.tool_calls ?? []) {
        const i = tc.index ?? 0;
        if (!this.toolSlots.has(i)) this.toolSlots.set(i, { id: "", name: "", args: [] });
        const slot = this.toolSlots.get(i)!;
        if (tc.id) slot.id = tc.id;
        const fn = tc.function ?? {};
        if (fn.name) slot.name = fn.name;
        if (fn.arguments) slot.args.push(fn.arguments);
      }
    }
  }

  usage(): Json {
    return { ...this.usageData };
  }

  /** 拼成 Chat 非流式响应形状，复用 usageFor / assistantFromUpstream。 */
  syntheticResponse(): Json {
    const msg: Json = { role: "assistant", content: this.textParts.join("") };
    if (this.toolSlots.size) {
      msg.tool_calls = [...this.toolSlots.keys()].sort((a, b) => a - b).map((i) => {
        const s = this.toolSlots.get(i)!;
        return { id: s.id, type: "function",
                 function: { name: s.name, arguments: s.args.join("") } };
      });
    }
    return { choices: [{ index: 0, message: msg }], usage: this.usageData };
  }
}
