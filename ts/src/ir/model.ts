/** IR（中间表示）数据模型 —— 三方唯一契约（v0 冻结，TS 移植自 src/ir/model.py）。
 *
 * 三层：
 *   L0 内容块模型  ：消息内部的最小组成（text / tool_use / tool_result / thinking）
 *   L1 规范请求    ：一次调用的完整规范化请求（model / system / messages / tools / 采样参数）
 *   L2 会话与缓存  ：跨轮上下文（session 标识、重放起点、缓存断点布局）
 *
 * 设计约束（对应执行方案 v3）：
 *   - 每个协议只写 to_ir / from_ir 两个方向，3 对 adapter 替代 6 方向两两互转。
 *   - L2 的断点布局 = 3 个固定分层断点 + 1 个滚动尾部断点（用满官方 4 个 cache_control 上限）。
 *   - 未知/不可映射字段一律进 `extra`，绝不静默丢弃（丢弃要进降级路径埋点）。
 */

export type Json = Record<string, any>;

// ---------------------------------------------------------------------------
// L0 · 内容块模型
// ---------------------------------------------------------------------------

export const TEXT = "text";
export const TOOL_USE = "tool_use";
export const TOOL_RESULT = "tool_result";
export const THINKING = "thinking";
export const IMAGE = "image"; // 多模态：方案 3.8 明确不做，仅占位以便显式降级
export const DOCUMENT = "document";

/** L0 内容块。`kind` 决定其余字段的语义。 */
export class Block {
  kind: string;
  text: string | null;
  tool_name: string | null;
  tool_id: string | null;
  tool_input: Json | null;
  /** 缓存断点标记（Anthropic cache_control）；仅允许出现在断点布局指定的块上 */
  cache_breakpoint: boolean;
  /** 未被规范化的原始字段（显式降级的证据，不静默丢弃） */
  extra: Json;

  constructor(init: { kind: string } & Partial<Block>) {
    this.kind = init.kind;
    this.text = init.text ?? null;
    this.tool_name = init.tool_name ?? null;
    this.tool_id = init.tool_id ?? null;
    this.tool_input = init.tool_input ?? null;
    this.cache_breakpoint = init.cache_breakpoint ?? false;
    this.extra = init.extra ?? {};
  }
}

/** L0 消息。role 规范化为 user / assistant / system（system 也可上提到 L1.system）。 */
export class Message {
  role: string;
  blocks: Block[];

  constructor(init: { role: string; blocks?: Block[] }) {
    this.role = init.role;
    this.blocks = init.blocks ?? [];
  }

  static text(role: string, text: string): Message {
    return new Message({ role, blocks: [new Block({ kind: TEXT, text })] });
  }
}

// ---------------------------------------------------------------------------
// L1 · 规范请求
// ---------------------------------------------------------------------------

export class Tool {
  name: string;
  description: string;
  input_schema: Json;
  cache_breakpoint: boolean;
  extra: Json;

  constructor(init: { name: string } & Partial<Tool>) {
    this.name = init.name;
    this.description = init.description ?? "";
    this.input_schema = init.input_schema ?? {};
    this.cache_breakpoint = init.cache_breakpoint ?? false;
    this.extra = init.extra ?? {};
  }
}

/** 一次调用的规范化请求。 */
export class IRRequest {
  model: string;
  system: Block[];    // 渲染顺序固定在 tools 之后
  messages: Message[];
  tools: Tool[];
  max_tokens: number;
  temperature: number | null;
  stream: boolean;
  /** 采样/生成等其他参数（不可映射时显式降级） */
  extra: Json;

  constructor(init: Partial<IRRequest> = {}) {
    this.model = init.model ?? "";
    this.system = init.system ?? [];
    this.messages = init.messages ?? [];
    this.tools = init.tools ?? [];
    this.max_tokens = init.max_tokens ?? 1024;
    this.temperature = init.temperature ?? null;
    this.stream = init.stream ?? false;
    this.extra = init.extra ?? {};
  }
}

/** 归一化 usage。三家口径不同，统一拆出缓存读写，避免对账全错。 */
export class IRUsage {
  input_tokens: number;
  output_tokens: number;
  cache_creation_input_tokens: number;
  cache_read_input_tokens: number;

  constructor(init: Partial<IRUsage> = {}) {
    this.input_tokens = init.input_tokens ?? 0;
    this.output_tokens = init.output_tokens ?? 0;
    this.cache_creation_input_tokens = init.cache_creation_input_tokens ?? 0;
    this.cache_read_input_tokens = init.cache_read_input_tokens ?? 0;
  }

  /** Anthropic 口径：input_tokens 不含缓存读写 */
  get total_input(): number {
    return this.input_tokens + this.cache_creation_input_tokens + this.cache_read_input_tokens;
  }
}

export class IRResponse {
  id: string;
  model: string;
  blocks: Block[];
  stop_reason: string | null;
  usage: IRUsage;
  /** max_tokens:0 预热响应是"畸形"的（空 content），用此标记避免被当错误 */
  is_warmup: boolean;
  extra: Json;

  constructor(init: Partial<IRResponse> = {}) {
    this.id = init.id ?? "";
    this.model = init.model ?? "";
    this.blocks = init.blocks ?? [];
    this.stop_reason = init.stop_reason ?? null;
    this.usage = init.usage ?? new IRUsage();
    this.is_warmup = init.is_warmup ?? false;
    this.extra = init.extra ?? {};
  }
}

// ---------------------------------------------------------------------------
// L2 · 会话与缓存上下文
// ---------------------------------------------------------------------------

/** 跨轮上下文。重放起点/缓存断点布局由 config/session.json 驱动。 */
export class SessionContext {
  session_key: string;        // 由 key_fields 拼出的状态键
  history: Message[];         // 已重放的历史
  cursor: number;             // last_breakpoint 重放起点游标
  // 缓存断点布局：固定分层断点的位置标记（tools 后 / system 后 / 历史静态段后）
  bp_after_tools: boolean;
  bp_after_system: boolean;
  bp_after_history_static: boolean;
  bp_rolling_tail: boolean;   // 第 4 个（滚动尾部）

  constructor(init: Partial<SessionContext> = {}) {
    this.session_key = init.session_key ?? "";
    this.history = init.history ?? [];
    this.cursor = init.cursor ?? 0;
    this.bp_after_tools = init.bp_after_tools ?? true;
    this.bp_after_system = init.bp_after_system ?? true;
    this.bp_after_history_static = init.bp_after_history_static ?? true;
    this.bp_rolling_tail = init.bp_rolling_tail ?? true;
  }
}

/** 序列化（冻结契约的 wire 形态） */
export function dumps(obj: unknown): string {
  return JSON.stringify(obj);
}
