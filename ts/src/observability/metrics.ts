/** 命中率埋点 —— 5 项必须暴露（方案 3.3），第一天就搭好而不是最后补。
 * （TS 移植自 src/observability/metrics.py）
 *
 * 没有这 5 个数字，「记忆注入对 KV Cache 的影响」就无法给出可信结论。
 * 真实命中率字段（cache_creation/cache_read）只有真实 API 才返回；离线阶段
 * 框架先就绪，字段在 mock/录制样例中以 0 或样例值占位。
 */

import { appendFileSync } from "node:fs";
import type { IRUsage, Json } from "../ir/model.ts";

export const NORMAL = "normal";
export const WARMUP = "warmup";

/** 单轮必须暴露的 5 项。 */
export interface TurnMetrics {
  ts: number;
  /** 轮次类型：预热轮（max_tokens:0）恒 cache_read=0，必须与正常轮分开，
   * 否则会稀释北极星分母（修复：预热轮不进 hitRate 统计）。 */
  kind: string;
  // ① 每轮缓存命中率（北极星）
  cache_creation_input_tokens: number;
  cache_read_input_tokens: number;
  input_tokens: number;
  // ② 注入 token 数（记忆注入成本）
  injected_tokens: number;
  // ③ 重放 token 数（无状态重放代价）
  replayed_tokens: number;
  // ④ 被丢弃的参数清单
  dropped_params: Json[];
  // ⑤ 走了哪条降级路径（explicit / silent）
  degradation_path: string;
}

export function makeTurnMetrics(init: Partial<TurnMetrics> = {}): TurnMetrics {
  return {
    ts: init.ts ?? Date.now() / 1000,
    kind: init.kind ?? NORMAL,
    cache_creation_input_tokens: init.cache_creation_input_tokens ?? 0,
    cache_read_input_tokens: init.cache_read_input_tokens ?? 0,
    input_tokens: init.input_tokens ?? 0,
    injected_tokens: init.injected_tokens ?? 0,
    replayed_tokens: init.replayed_tokens ?? 0,
    dropped_params: init.dropped_params ?? [],
    degradation_path: init.degradation_path ?? "none",
  };
}

export function cacheHit(m: TurnMetrics): boolean {
  return m.cache_read_input_tokens > 0;
}

/** creation 与 read 同时为 0 = 完全没用上缓存（多半没够最小阈值）。 */
export function cacheUsed(m: TurnMetrics): boolean {
  return !(m.cache_creation_input_tokens === 0 && m.cache_read_input_tokens === 0);
}

/** 追加写 JSONL，供实验脚本汇总命中率。
 * Python 版需要锁（ThreadingHTTPServer 并发写可能坏行）；
 * Node 单线程 + appendFileSync 同步写，天然无竞态。
 */
export class MetricsLog {
  path: string;

  constructor(path: string) {
    this.path = path;
  }

  record(m: TurnMetrics): void {
    appendFileSync(this.path, JSON.stringify(m) + "\n", "utf-8");
  }

  recordTurn(usage: IRUsage, injected: number, replayed: number,
             dropped: Json[], degradation: string, kind: string = NORMAL): TurnMetrics {
    const m = makeTurnMetrics({
      kind,
      cache_creation_input_tokens: usage.cache_creation_input_tokens,
      cache_read_input_tokens: usage.cache_read_input_tokens,
      input_tokens: usage.input_tokens,
      injected_tokens: injected,
      replayed_tokens: replayed,
      dropped_params: [...dropped],
      degradation_path: degradation,
    });
    this.record(m);
    return m;
  }
}

/** cache_read 命中率 = 命中轮数 / 总轮数（北极星）。
 *
 * 默认剔除预热轮：预热（max_tokens:0）的目的是写缓存，其 cache_read 恒为 0，
 * 把它算进分母会系统性拉低命中率，且拉低幅度取决于预热轮占比，
 * 让不同批次之间的命中率不可比。
 */
export function hitRate(metrics: TurnMetrics[], includeWarmup = false): number {
  const ms = includeWarmup ? metrics : metrics.filter((m) => m.kind !== WARMUP);
  if (!ms.length) return 0;
  return ms.filter(cacheHit).length / ms.length;
}
