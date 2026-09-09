/** OpenAI Chat Completions ↔ IR（TS 移植自 src/adapters/chat.py）。
 *
 * 关键点：
 *   - messages[].role: system 上提到 L1.system；user/assistant 进 L1.messages。
 *   - tool_calls -> tool_use；role=tool -> tool_result。
 *   - usage: prompt_tokens 含缓存，cached_tokens 为子集（归一到 IRUsage）。
 */

import * as ir from "../ir/model.ts";
import { Adapter, Dropped, parseArguments, recordUnknown } from "./base.ts";

// Chat Completions 顶层字段中，本 adapter 真正消费的部分。
// 其余顶层字段一律进降级记录：命中率实验的第④项暴露依赖这份清单准确，
// 静默吞掉一个字段（如误发的 top-level system）会让埋点失真。
const KNOWN_TOP = new Set([
  "model", "messages", "tools",
  "max_tokens", "max_completion_tokens", "temperature", "stream", "system",
]);

export class ChatAdapter extends Adapter {
  name = "openai_chat";

  to_ir(payload: ir.Json, dropped: Dropped): ir.IRRequest {
    const req = new ir.IRRequest({
      model: payload.model ?? "",
      max_tokens: payload.max_completion_tokens ?? payload.max_tokens ?? 1024,
      temperature: payload.temperature ?? null,
      stream: Boolean(payload.stream ?? false),
    });
    for (const m of payload.messages ?? []) {
      const role = m.role ?? "user";
      if (role === "system") {
        req.system.push(new ir.Block({ kind: ir.TEXT, text: m.content ?? "" }));
        continue;
      }
      const msg = new ir.Message({ role });
      const content = m.content;
      if (typeof content === "string") {
        msg.blocks.push(new ir.Block({ kind: ir.TEXT, text: content }));
      } else if (Array.isArray(content)) {
        for (const part of content) {
          const t = part?.type;
          if (t === "text") {
            msg.blocks.push(new ir.Block({ kind: ir.TEXT, text: part.text ?? "" }));
          } else {
            dropped.add(`content.${t}`, "Chat 多模态内容块，方案 3.8 不做", "explicit");
          }
        }
      }
      for (const tc of m.tool_calls ?? []) {
        const fn = tc.function ?? {};
        const rawArgs = fn.arguments ?? "";
        msg.blocks.push(new ir.Block({
          kind: ir.TOOL_USE, tool_name: fn.name, tool_id: tc.id,
          // v1.4：参数解析成 dict 进 tool_input——不解析的话跨协议到
          // Anthropic 会渲染成空 input {}（第三轮自查发现的参数丢失 bug）。
          // 原始字符串留 extra，chat→chat 往返无损。
          tool_input: parseArguments(rawArgs),
          extra: { arguments: rawArgs },
        }));
      }
      if (role === "tool") {
        msg.role = "user";
        msg.blocks.push(new ir.Block({
          kind: ir.TOOL_RESULT, tool_id: m.tool_call_id,
          text: m.content ?? "",
        }));
      }
      req.messages.push(msg);
    }
    for (const t of payload.tools ?? []) {
      const fn = t.function ?? t;
      req.tools.push(new ir.Tool({
        name: fn.name ?? "", description: fn.description ?? "",
        input_schema: fn.parameters ?? {},
      }));
    }
    // 顶层 system 非 Chat 规范字段（Responses 用 instructions），但实践中
    // 常被误发到 Chat 路由：按语义上提到 IR.system，同时留一条降级记录，
    // 既不丢语义也不掩盖"这不是规范写法"这个事实。
    if ("system" in payload && typeof payload.system === "string") {
      req.system.push(new ir.Block({ kind: ir.TEXT, text: payload.system }));
      dropped.add("system", "Chat 规范无顶层 system 字段，已按语义上提至 IR.system",
                  "explicit");
    }
    // 其余未消费的顶层字段：显式进降级记录（方案 3.3 第④项）
    recordUnknown(payload, KNOWN_TOP, dropped, "Chat 参数暂无 IR 映射");
    return req;
  }

  from_ir(req: ir.IRRequest, dropped: Dropped): ir.Json {
    const messages: ir.Json[] = [];
    if (req.system.length) {
      messages.push({ role: "system",
                      content: req.system.map((b) => b.text ?? "").join("") });
    }
    for (const msg of req.messages) {
      const textParts: string[] = [];
      const toolCalls: ir.Json[] = [];
      const toolResults: ir.Json[] = [];
      for (const b of msg.blocks) {
        if (b.kind === ir.TEXT) {
          textParts.push(b.text ?? "");
        } else if (b.kind === ir.TOOL_USE) {
          toolCalls.push({ id: b.tool_id, type: "function",
                           function: { name: b.tool_name,
                                       arguments: argumentsStr(b) } });
        } else if (b.kind === ir.TOOL_RESULT) {
          toolResults.push({ role: "tool", tool_call_id: b.tool_id,
                             content: b.text ?? "" });
        }
      }
      if (toolResults.length) {
        messages.push(...toolResults);
        continue;
      }
      const m: ir.Json = { role: msg.role, content: textParts.join("") };
      if (toolCalls.length) m.tool_calls = toolCalls;
      messages.push(m);
    }
    const out: ir.Json = { model: req.model, messages,
                           max_completion_tokens: req.max_tokens, stream: req.stream };
    if (req.temperature !== null) out.temperature = req.temperature;
    if (req.tools.length) {
      out.tools = req.tools.map((t) => ({ type: "function", function: {
        name: t.name, description: t.description, parameters: t.input_schema } }));
    }
    return out;
  }
}

/** Chat/Responses 侧的 arguments 是 JSON 字符串。
 *
 * 优先用 extra 里的原始字符串（同协议往返字节无损）；没有则从 tool_input
 * 序列化——anthropic→chat 方向靠这条路把 dict 参数带过去（v1.4 修复）。
 */
function argumentsStr(b: ir.Block): string {
  const raw = b.extra.arguments;
  if (typeof raw === "string" && raw.trim()) return raw;
  return JSON.stringify(b.tool_input ?? {});
}

export function usageFromChat(usage: ir.Json): ir.IRUsage {
  const details = usage.prompt_tokens_details ?? {};
  return new ir.IRUsage({
    input_tokens: usage.prompt_tokens ?? 0,
    output_tokens: usage.completion_tokens ?? 0,
    cache_read_input_tokens: details.cached_tokens ?? 0,
  });
}
