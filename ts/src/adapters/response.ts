/** OpenAI Responses ↔ IR（TS 移植自 src/adapters/response.py）。
 *
 * 关键点：
 *   - input 为结构化条目列表（message / function_call / function_call_output / reasoning）。
 *   - previous_response_id 是有状态字段，由网关/状态层在调用前解析、在响应后登记；
 *     adapter 只负责把它透传到 extra，真正重放在状态层完成。
 *   - usage: input_tokens + input_tokens_details.cached_tokens。
 */

import * as ir from "../ir/model.ts";
import { Adapter, Dropped, parseArguments, recordUnknown } from "./base.ts";

// to_ir 真正消费的顶层字段；其余一律进降级记录（见 base.recordUnknown）
const KNOWN_TOP = new Set(["model", "input", "instructions", "tools", "max_output_tokens",
                           "temperature", "stream", "previous_response_id"]);

export class ResponseAdapter extends Adapter {
  name = "openai_response";

  to_ir(payload: ir.Json, dropped: Dropped): ir.IRRequest {
    const req = new ir.IRRequest({
      model: payload.model ?? "",
      max_tokens: payload.max_output_tokens ?? 1024,
      temperature: payload.temperature ?? null,
      stream: Boolean(payload.stream ?? false),
    });
    if (payload.instructions) {
      req.system.push(new ir.Block({ kind: ir.TEXT, text: payload.instructions }));
    }
    for (const item of payload.input ?? []) {
      const t = item?.type;
      if (t === "message") {
        const msg = new ir.Message({ role: item.role ?? "user" });
        const content = item.content;
        if (typeof content === "string") {
          msg.blocks.push(new ir.Block({ kind: ir.TEXT, text: content }));
        } else if (Array.isArray(content)) {
          for (const part of content) {
            if (["input_text", "output_text", "text"].includes(part?.type)) {
              msg.blocks.push(new ir.Block({ kind: ir.TEXT, text: part.text ?? "" }));
            } else {
              dropped.add(`input.${part?.type}`, "Responses 内容块，方案 3.8 不做", "explicit");
            }
          }
        }
        req.messages.push(msg);
      } else if (t === "function_call") {
        const rawArgs = item.arguments ?? "";
        req.messages.push(new ir.Message({ role: "assistant", blocks: [new ir.Block({
          kind: ir.TOOL_USE, tool_name: item.name, tool_id: item.call_id,
          // v1.4：解析成 dict 进 tool_input，跨协议到 Anthropic 不再丢参数
          tool_input: parseArguments(rawArgs),
          extra: { arguments: rawArgs } })] }));
      } else if (t === "function_call_output") {
        req.messages.push(new ir.Message({ role: "user", blocks: [new ir.Block({
          kind: ir.TOOL_RESULT, tool_id: item.call_id,
          text: item.output ?? "" })] }));
      } else if (t === "reasoning") {
        req.messages.push(new ir.Message({ role: "assistant", blocks: [new ir.Block({
          kind: ir.THINKING,
          text: typeof item.summary === "string" ? item.summary : "" })] }));
      } else {
        dropped.add(`input.${t}`, "Responses 条目类型暂无映射", "explicit");
      }
    }
    for (const t of payload.tools ?? []) {
      req.tools.push(new ir.Tool({ name: t.name ?? "",
                                   description: t.description ?? "",
                                   input_schema: t.parameters ?? {} }));
    }
    // 有状态字段：交给状态层处理，这里透传
    if ("previous_response_id" in payload) {
      req.extra.previous_response_id = payload.previous_response_id;
    }
    recordUnknown(payload, KNOWN_TOP, dropped, "Responses 参数暂无 IR 映射");
    return req;
  }

  from_ir(req: ir.IRRequest, dropped: Dropped): ir.Json {
    const items: ir.Json[] = [];
    for (const msg of req.messages) {
      for (const b of msg.blocks) {
        if (b.kind === ir.TEXT) {
          items.push({ type: "message", role: msg.role,
                       content: [{ type: msg.role === "user" ? "input_text" : "output_text",
                                   text: b.text ?? "" }] });
        } else if (b.kind === ir.TOOL_USE) {
          items.push({ type: "function_call", name: b.tool_name,
                       call_id: b.tool_id, arguments: argumentsStr(b) });
        } else if (b.kind === ir.TOOL_RESULT) {
          items.push({ type: "function_call_output",
                       call_id: b.tool_id, output: b.text ?? "" });
        }
      }
    }
    const out: ir.Json = { model: req.model, input: items,
                           max_output_tokens: req.max_tokens, stream: req.stream };
    if (req.system.length) {
      out.instructions = req.system.map((b) => b.text ?? "").join("");
    }
    if (req.temperature !== null) out.temperature = req.temperature;
    if (req.tools.length) {
      out.tools = req.tools.map((t) => ({ type: "function", name: t.name,
                                           description: t.description,
                                           parameters: t.input_schema }));
    }
    // 不再向上游回传 previous_response_id：历史已由本网关状态层重放进 input，
    // 再传一次会让上游（真 OpenAI）二次拼接历史 → 上下文重复且 token 翻倍；
    // 若传的是网关自己生成的 resp_xxx，上游根本不认识 → 直接 404。
    // 给客户端的 response_id 由网关在响应里注入（见 gateway/server.ts）。
    return out;
  }
}

/** 优先用 extra 原始字符串（同协议无损）；否则从 tool_input 序列化（v1.4）。 */
function argumentsStr(b: ir.Block): string {
  const raw = b.extra.arguments;
  if (typeof raw === "string" && raw.trim()) return raw;
  return JSON.stringify(b.tool_input ?? {});
}

export function usageFromResponse(usage: ir.Json): ir.IRUsage {
  const details = usage.input_tokens_details ?? {};
  return new ir.IRUsage({
    input_tokens: usage.input_tokens ?? 0,
    output_tokens: usage.output_tokens ?? 0,
    cache_read_input_tokens: details.cached_tokens ?? 0,
  });
}
