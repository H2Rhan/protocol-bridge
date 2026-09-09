/** adapter 基类与降级路径记录（TS 移植自 src/adapters/base.py）。
 *
 * 每个协议实现两个方向：to_ir / from_ir。
 * 不可映射的字段/参数必须进降级路径，绝不静默丢弃（方案 3.3 第④⑤项暴露）。
 */

import * as ir from "../ir/model.ts";

export interface DroppedItem {
  field: string;
  reason: string;
  path: string; // "explicit" 显式降级 / "silent" 静默通过（应尽量避免）
}

/** 被丢弃/降级的参数清单（配合告警）。 */
export class Dropped {
  items: DroppedItem[] = [];

  add(field: string, reason: string, path = "explicit"): void {
    this.items.push({ field, reason, path });
  }

  get length(): number {
    return this.items.length;
  }
}

/** 把 adapter 未消费的顶层字段显式记进降级路径。
 *
 * 三个 adapter 的 to_ir 末尾都必须调它。漏调 = 字段被静默丢弃 —— 而
 * 「被丢弃的参数清单」是命中率实验第④项暴露，静默丢弃会让这份清单不可信，
 * 也会让「三方互转无信息损失」的结论站不住。
 */
export function recordUnknown(payload: ir.Json, known: Set<string>, dropped: Dropped,
                              reason = "该协议参数暂无 IR 映射"): void {
  for (const k of Object.keys(payload)) {
    if (!known.has(k)) {
      dropped.add(k, reason, "explicit");
    }
  }
}

export abstract class Adapter {
  abstract name: string;
  abstract to_ir(payload: ir.Json, dropped: Dropped): ir.IRRequest;
  abstract from_ir(req: ir.IRRequest, dropped: Dropped, ctx?: ir.SessionContext | null): ir.Json;
}

/** OpenAI 系的工具参数是 JSON **字符串**，Anthropic 是 dict。
 *
 * 跨协议转换必须解析成 dict 进 IR.tool_input，否则到 Anthropic 侧会渲染成
 * 空 input {}（第三轮自查发现的参数丢失 bug）。解析失败返回 {}，原始字符串
 * 由调用方留在 extra["arguments"] 兜底（同协议往返无损）。
 */
export function parseArguments(raw: unknown): ir.Json {
  if (raw !== null && typeof raw === "object" && !Array.isArray(raw)) {
    return raw as ir.Json;
  }
  if (typeof raw === "string" && raw.trim()) {
    try {
      const v = JSON.parse(raw);
      return (v !== null && typeof v === "object" && !Array.isArray(v)) ? v : {};
    } catch {
      return {};
    }
  }
  return {};
}

/** 上游响应 → IR assistant 消息（状态层重放的原料）。
 *
 * v1.4 起保留**结构化块**：thinking（含 signature / redacted data）与
 * tool_use（含解析后的 tool_input）必须进历史——Anthropic 要求下一轮原样
 * 回传 thinking + signature，缺失直接 400；tool_use 丢失则多轮工具链断链。
 * 返回 null 表示本轮没有任何可重放内容（如空响应）。
 */
export function assistantFromUpstream(target: string, resp: ir.Json): ir.Message | null {
  const blocks: ir.Block[] = [];
  if (target === "anthropic") {
    for (const b of resp.content ?? []) {
      if (b === null || typeof b !== "object") continue;
      const t = b.type;
      if (t === "text") {
        blocks.push(new ir.Block({ kind: ir.TEXT, text: b.text ?? "" }));
      } else if (t === "thinking") {
        blocks.push(new ir.Block({ kind: ir.THINKING, text: b.thinking ?? "",
                                   extra: { signature: b.signature ?? "" } }));
      } else if (t === "redacted_thinking") {
        blocks.push(new ir.Block({ kind: ir.THINKING, text: "",
                                   extra: { redacted: true, data: b.data ?? "" } }));
      } else if (t === "tool_use") {
        blocks.push(new ir.Block({ kind: ir.TOOL_USE, tool_name: b.name,
                                   tool_id: b.id, tool_input: b.input ?? {} }));
      }
    }
  } else if (target === "openai_response") {
    for (const item of resp.output ?? []) {
      if (item === null || typeof item !== "object") continue;
      const t = item.type;
      if (t === "message") {
        for (const c of item.content ?? []) {
          if (c !== null && typeof c === "object" && (c.type === "output_text" || c.type === "text")) {
            blocks.push(new ir.Block({ kind: ir.TEXT, text: c.text ?? "" }));
          }
        }
      } else if (t === "function_call") {
        const raw = item.arguments ?? "";
        blocks.push(new ir.Block({ kind: ir.TOOL_USE, tool_name: item.name,
                                   tool_id: item.call_id,
                                   tool_input: parseArguments(raw),
                                   extra: { arguments: raw } }));
      } else if (t === "reasoning") {
        const summary = item.summary;
        const text = typeof summary === "string" ? summary : "";
        blocks.push(new ir.Block({ kind: ir.THINKING, text,
                                   extra: { encrypted_content: item.encrypted_content ?? "" } }));
      }
    }
  } else { // openai_chat
    let m: ir.Json = {};
    try {
      m = resp.choices[0].message ?? {};
    } catch { m = {}; }
    if (m.content) {
      blocks.push(new ir.Block({ kind: ir.TEXT, text: m.content }));
    }
    for (const tc of m.tool_calls ?? []) {
      const fn = (tc !== null && typeof tc === "object") ? (tc.function ?? {}) : {};
      const raw = fn.arguments ?? "";
      blocks.push(new ir.Block({ kind: ir.TOOL_USE, tool_name: fn.name,
                                 tool_id: tc.id, tool_input: parseArguments(raw),
                                 extra: { arguments: raw } }));
    }
  }
  // 纯空文本（如异常响应）不进历史
  const kept = blocks.filter((b) => b.kind !== ir.TEXT || (b.text ?? "").trim());
  if (!kept.length) return null;
  return new ir.Message({ role: "assistant", blocks: kept });
}
