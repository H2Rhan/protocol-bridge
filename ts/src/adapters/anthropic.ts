/** Anthropic Messages ↔ IR（TS 移植自 src/adapters/anthropic.py）。
 *
 * 关键点：
 *   - system 独立成字段（Block[]），渲染顺序 tools → system → messages。
 *   - cache_control 断点：只在 L2 断点布局指定的块上打（3 固定 + 1 滚动，用满 4 个上限）。
 *   - usage: input_tokens 不含缓存读写；cache_creation/cache_read 单列。
 */

import * as ir from "../ir/model.ts";
import { Adapter, Dropped, recordUnknown } from "./base.ts";

const BP = { type: "ephemeral" };

// to_ir 真正消费的顶层字段；其余一律进降级记录（见 base.recordUnknown）
const KNOWN_TOP = new Set(["model", "max_tokens", "temperature", "stream",
                           "system", "messages", "tools", "thinking", "tool_choice"]);

export class AnthropicAdapter extends Adapter {
  name = "anthropic";

  /** IR 块 → Anthropic content 块。无法映射的一律进降级记录，绝不静默丢弃。 */
  private static renderBlock(b: ir.Block, dropped: Dropped): ir.Json | null {
    if (b.kind === ir.TEXT) {
      return { type: "text", text: b.text ?? "" };
    }
    if (b.kind === ir.TOOL_USE) {
      return { type: "tool_use", id: b.tool_id, name: b.tool_name,
               input: b.tool_input ?? {} };
    }
    if (b.kind === ir.TOOL_RESULT) {
      return { type: "tool_result", tool_use_id: b.tool_id,
               content: b.text ?? "" };
    }
    if (b.kind === ir.THINKING) {
      // v1.4：signature 必须原样回传（缺失下一轮直接 400）；
      // redacted_thinking 逐字节透传 data。
      if (b.extra.redacted) {
        return { type: "redacted_thinking", data: b.extra.data ?? "" };
      }
      const out: ir.Json = { type: "thinking", thinking: b.text ?? "" };
      if (b.extra.signature) out.signature = b.extra.signature;
      return out;
    }
    dropped.add(`block.${b.kind}`, "IR 块无 Anthropic 对应类型（方案 3.8 不做）",
                "explicit");
    return null;
  }

  to_ir(payload: ir.Json, dropped: Dropped): ir.IRRequest {
    const req = new ir.IRRequest({
      model: payload.model ?? "",
      max_tokens: payload.max_tokens ?? 1024,
      temperature: payload.temperature ?? null,
      stream: Boolean(payload.stream ?? false),
    });
    const system = payload.system;
    if (typeof system === "string") {
      req.system.push(new ir.Block({ kind: ir.TEXT, text: system }));
    } else if (Array.isArray(system)) {
      for (const b of system) {
        req.system.push(new ir.Block({
          kind: ir.TEXT, text: b.text ?? "",
          cache_breakpoint: "cache_control" in b }));
      }
    }
    for (const m of payload.messages ?? []) {
      const msg = new ir.Message({ role: m.role ?? "user" });
      const content = m.content;
      if (typeof content === "string") {
        msg.blocks.push(new ir.Block({ kind: ir.TEXT, text: content }));
      } else if (Array.isArray(content)) {
        for (const b of content) {
          const t = b?.type;
          const bp = b !== null && typeof b === "object" && "cache_control" in b;
          if (t === "text") {
            msg.blocks.push(new ir.Block({ kind: ir.TEXT, text: b.text ?? "", cache_breakpoint: bp }));
          } else if (t === "tool_use") {
            msg.blocks.push(new ir.Block({ kind: ir.TOOL_USE, tool_name: b.name,
                                           tool_id: b.id, tool_input: b.input ?? {},
                                           cache_breakpoint: bp }));
          } else if (t === "tool_result") {
            msg.blocks.push(new ir.Block({ kind: ir.TOOL_RESULT, tool_id: b.tool_use_id,
                                           text: resultText(b), cache_breakpoint: bp }));
          } else if (t === "thinking") {
            // v1.4：signature 进 extra 保留——下一轮必须原样回传，丢则 400
            msg.blocks.push(new ir.Block({
              kind: ir.THINKING, text: b.thinking ?? "",
              extra: { signature: b.signature ?? "" } }));
          } else if (t === "redacted_thinking") {
            // 逐字节透传 data，不能判成无效块丢掉（问题清单组4#5）
            msg.blocks.push(new ir.Block({
              kind: ir.THINKING, text: "",
              extra: { redacted: true, data: b.data ?? "" } }));
          } else {
            dropped.add(`content.${t}`, "Anthropic 内容块，方案 3.8 不做", "explicit");
          }
        }
      }
      req.messages.push(msg);
    }
    for (const t of payload.tools ?? []) {
      req.tools.push(new ir.Tool({ name: t.name ?? "",
                                   description: t.description ?? "",
                                   input_schema: t.input_schema ?? {},
                                   cache_breakpoint: "cache_control" in t }));
    }
    // IR v0 没有 extended thinking / tool_choice 的字段，但不能静默丢弃：
    // 存进 extra，from_ir 原样渲染回去 —— 既保证 anthropic→anthropic 无损，
    // 也让 validateWarmup 的 thinking 冲突检查真正可达。
    for (const k of ["thinking", "tool_choice"]) {
      if (payload[k] !== undefined && payload[k] !== null) {
        req.extra[k] = payload[k];
      }
    }
    recordUnknown(payload, KNOWN_TOP, dropped, "Anthropic 参数暂无 IR 映射");
    return req;
  }

  from_ir(req: ir.IRRequest, dropped: Dropped, ctx: ir.SessionContext | null = null): ir.Json {
    const tools: ir.Json[] = [];
    req.tools.forEach((t, i) => {
      const td: ir.Json = { name: t.name, description: t.description, input_schema: t.input_schema };
      if (ctx?.bp_after_tools && i === req.tools.length - 1) {
        td.cache_control = { ...BP };
      }
      tools.push(td);
    });

    const system: ir.Json[] = [];
    req.system.forEach((b, i) => {
      const sb: ir.Json = { type: "text", text: b.text ?? "" };
      if (ctx?.bp_after_system && i === req.system.length - 1) {
        sb.cache_control = { ...BP };
      }
      system.push(sb);
    });

    const messages: ir.Json[] = [];
    const n = req.messages.length;
    // 历史静态段末尾：重放进来的历史最后一条（ctx.history 即本轮重放切片）
    const histEnd = (ctx?.history?.length ?? 0) - 1;
    req.messages.forEach((msg, i) => {
      const blocks: ir.Json[] = [];
      for (const b of msg.blocks) {
        const bd = AnthropicAdapter.renderBlock(b, dropped);
        if (bd !== null) blocks.push(bd);
      }
      // 固定断点③：历史静态段末尾（若与尾部断点重合，自然只落一个）
      if (ctx?.bp_after_history_static && histEnd >= 0 && histEnd === i && blocks.length) {
        blocks[blocks.length - 1].cache_control = { ...BP };
      }
      // 滚动尾部断点：打在最后一条消息的最后一个块
      if (ctx?.bp_rolling_tail && i === n - 1 && blocks.length) {
        blocks[blocks.length - 1].cache_control = { ...BP };
      }
      messages.push({ role: msg.role,
                      content: blocks.length ? blocks : [{ type: "text", text: "" }] });
    });

    const out: ir.Json = { model: req.model, messages, max_tokens: req.max_tokens,
                           stream: req.stream };
    if (system.length) out.system = system;
    if (tools.length) out.tools = tools;
    if (req.temperature !== null) out.temperature = req.temperature;
    // to_ir 存进 extra 的 Anthropic 专属参数，原样渲染回去（anthropic→anthropic 无损）
    for (const k of ["thinking", "tool_choice"]) {
      if (req.extra[k] !== undefined && req.extra[k] !== null) {
        out[k] = req.extra[k];
      }
    }
    return out;
  }
}

function resultText(b: ir.Json): string {
  const c = b.content;
  if (typeof c === "string") return c;
  if (Array.isArray(c)) {
    return c.map((p) => (p !== null && typeof p === "object") ? (p.text ?? "") : "").join("");
  }
  return "";
}

export function usageFromAnthropic(usage: ir.Json): ir.IRUsage {
  return new ir.IRUsage({
    input_tokens: usage.input_tokens ?? 0,
    output_tokens: usage.output_tokens ?? 0,
    cache_creation_input_tokens: usage.cache_creation_input_tokens ?? 0,
    cache_read_input_tokens: usage.cache_read_input_tokens ?? 0,
  });
}
