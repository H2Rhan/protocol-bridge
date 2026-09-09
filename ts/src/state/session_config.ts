/** Session 边界配置（可切换）—— 读 config/session.json，驱动重放策略。
 * （TS 移植自 src/state/session_config.py）
 *
 * 落地纪律（对应执行方案 3.9.1）：
 *   - key_granularity：启动时读配置，运行中途切换会致已存会话键值错乱。
 *   - replay_from / end_policy：可热切换（只在读历史时生效）。
 *   - 实验脚本每次跑之前，把本配置完整快照写进报告头部，与命中率数据绑死。
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { Message } from "../ir/model.ts";

// key_granularity
export const SINGLE_TASK = "single_task";
export const THREE_LEVEL = "three_level";
// replay_from
export const FULL = "full";
export const LAST_BREAKPOINT = "last_breakpoint";
export const SLIDING_WINDOW = "sliding_window";
// end_policy
export const TTL = "ttl";
export const EXPLICIT_CLOSE = "explicit_close";

export class SessionConfig {
  key_granularity: string;
  key_fields: string[];
  replay_from: string;
  sliding_window_n: number;
  end_policy: string;
  ttl_seconds: number;
  on_end: string;
  /** 记忆注入上限（TRACK 04 第三参数）：0=不限制；会话 meta.memory_cap 可覆盖 */
  session_memory_cap: number;

  constructor(init: Partial<SessionConfig> = {}) {
    this.key_granularity = init.key_granularity ?? SINGLE_TASK;
    this.key_fields = init.key_fields ?? ["task-id"];
    this.replay_from = init.replay_from ?? FULL;
    this.sliding_window_n = init.sliding_window_n ?? 20;
    this.end_policy = init.end_policy ?? TTL;
    this.ttl_seconds = init.ttl_seconds ?? 1800;
    this.on_end = init.on_end ?? "archive";
    this.session_memory_cap = init.session_memory_cap ?? 0;
  }

  /** 由 key_fields 从请求头拼状态键。 */
  sessionKey(headers: Record<string, string>): string {
    return this.key_fields.map((k) => String(headers[k] ?? "")).join("|");
  }

  /** 实验报告头部要绑定的配置快照。 */
  snapshot(): Record<string, unknown> {
    return {
      key_granularity: this.key_granularity,
      key_fields: [...this.key_fields],
      replay_from: this.replay_from,
      sliding_window_n: this.sliding_window_n,
      end_policy: this.end_policy,
      ttl_seconds: this.ttl_seconds,
      on_end: this.on_end,
      session_memory_cap: this.session_memory_cap,
    };
  }
}

const DEFAULT_PATH = join(
  dirname(fileURLToPath(import.meta.url)), "..", "..", "config", "session.json");

/** 从 JSON 加载；文件缺失或缺字段时回落到默认假设值。 */
export function load(path?: string): SessionConfig {
  const p = path ?? DEFAULT_PATH;
  let raw: Record<string, unknown> = {};
  try {
    raw = (JSON.parse(readFileSync(p, "utf-8")).session ?? {}) as Record<string, unknown>;
  } catch {
    raw = {};
  }
  const cfg = new SessionConfig();
  for (const [k, v] of Object.entries(raw)) {
    if (k in cfg) {
      (cfg as unknown as Record<string, unknown>)[k] = v;
    }
  }
  return cfg;
}

/** 重放起点策略接口：配置决定用哪个实现。
 *
 * history: 该 session 已存的完整历史
 * cursor:  last_breakpoint 的游标
 * 返回：本轮要拼进前缀的消息
 */
export function buildPrefix(history: Message[], cursor: number, cfg: SessionConfig): Message[] {
  if (cfg.replay_from === FULL) return [...history];
  if (cfg.replay_from === LAST_BREAKPOINT) return history.slice(cursor);
  // sliding_window
  // n<=0 必须返回空窗口。直接写 history.slice(-n) 在 n==0 时等价于 history.slice(0)，
  // 会静默退化成 full —— 实验组间差异就此消失且极难察觉。
  const n = cfg.sliding_window_n;
  return n > 0 ? history.slice(-n) : [];
}
