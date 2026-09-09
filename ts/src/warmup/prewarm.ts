/** max_tokens:0 预热路径 —— 已核实为 Anthropic 官方支持用法（方案 3.7）。
 * （TS 移植自 src/warmup/prewarm.py）
 *
 * 官方行为（核对日期 2026-09-01）：
 *   - 读入提示并在 cache_control 断点处写缓存，不生成输出即返回。
 *   - 响应是"畸形"的：content 空数组、stop_reason="max_tokens"、usage 完整填充
 *     （output_tokens=0，零输出计费）。
 *   - 拒绝条件（invalid_request_error）：带 stream / extended thinking /
 *     structured outputs(output_config.format) / tool_choice 为 tool 或 any；
 *     Message Batches 内也不支持。
 *   - 断点必须打在与后续请求共享的前缀末尾（如 system），不能打在占位 user 消息上，
 *     否则缓存条目以占位消息为键、后续永不命中。自动缓存会把断点放在最后一块（占位消息）。
 *
 * 转换层必须为它开单独路径：若按正常响应处理，会把预热响应当错误抛掉。
 */

import * as ir from "../ir/model.ts";

// 占位 user 消息：需非空白字符串（官方示例用 "warmup"），会被读入但不会被回答
export const PLACEHOLDER = "warmup";

/** 构造一个预热请求。
 *
 * 断点打在 system（与后续请求共享的前缀末尾），不打在占位消息上。
 */
export function buildWarmupRequest(systemText: string, model: string): ir.Json {
  return {
    model,
    max_tokens: 0,
    system: [{ type: "text", text: systemText,
               cache_control: { type: "ephemeral" } }],
    messages: [{ role: "user", content: PLACEHOLDER }],
  };
}

/** 返回会导致预热被拒的冲突参数清单（空 = 可发）。 */
export function validateWarmup(payload: ir.Json): string[] {
  const conflicts: string[] = [];
  if (payload.stream) conflicts.push("stream");
  const thinking = payload.thinking;
  if (thinking !== null && typeof thinking === "object" && thinking.type === "enabled") {
    conflicts.push("thinking");
  }
  if (payload.output_config?.format) conflicts.push("output_config.format");
  const tc = payload.tool_choice;
  if (tc !== null && typeof tc === "object" && ["tool", "any"].includes(tc.type)) {
    conflicts.push("tool_choice");
  }
  if ((payload.max_tokens ?? 1) !== 0) conflicts.push("max_tokens!=0");
  return conflicts;
}

/** 把"畸形"预热响应解析成 IRResponse，标记 is_warmup，避免被当错误。 */
export function parseWarmupResponse(body: ir.Json): ir.IRResponse {
  const usage = body.usage ?? {};
  return new ir.IRResponse({
    id: body.id ?? "",
    model: body.model ?? "",
    blocks: [], // content 为空数组，属正常
    stop_reason: body.stop_reason ?? null, // 恒为 "max_tokens"
    usage: new ir.IRUsage({
      input_tokens: usage.input_tokens ?? 0,
      output_tokens: usage.output_tokens ?? 0,
      cache_creation_input_tokens: usage.cache_creation_input_tokens ?? 0,
      cache_read_input_tokens: usage.cache_read_input_tokens ?? 0,
    }),
    is_warmup: true,
  });
}

/** 判定一个响应是否为预热响应（空 content + stop_reason=max_tokens）。 */
export function isWarmupResponse(body: ir.Json): boolean {
  return Array.isArray(body.content) && body.content.length === 0 &&
         body.stop_reason === "max_tokens";
}
