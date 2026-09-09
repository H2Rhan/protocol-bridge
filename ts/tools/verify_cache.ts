/** 验站脚本：验证 Anthropic 端点是否真正透传 prompt caching。
 * （TS 移植自 tools/verify_cache.py）
 *
 * 两步验证（对应方案 G1 预热链路）：
 *   1) 带唯一随机前缀 + cache_control 预热（max_tokens:0，被拒则降级 max_tokens:1）
 *      -> 期望 usage.cache_creation_input_tokens > 0 且 cache_read_input_tokens == 0
 *   2) 立刻原样重发同一前缀
 *      -> 期望 usage.cache_read_input_tokens > 0
 *
 * 判定：
 *   PASS    写读都正常 —— 可用于链路调试（exit 0）
 *   PARTIAL 有写无读 —— 缓存可能被转格式吞掉一半，只能慎用（exit 1）
 *   FAIL    写都没有 —— cache_control 被剥，不能用于任何缓存实验（exit 2）
 *
 * 用法：
 *   set PB_API_KEY=xxx
 *   node tools/verify_cache.ts --base https://<端点域名> --model claude-sonnet-5
 *   # 离线自测（先起 tools/mock_backend.ts）：
 *   node tools/verify_cache.ts --base http://127.0.0.1:9100 --model mock
 *
 * 注：TS 版恒直连（Node 不读系统代理）；--use-proxy 保留兼容但为 no-op。
 */

import { randomInt } from "node:crypto";
import type { Json } from "../src/ir/model.ts";

const WORDS = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo " +
               "lima mike november oscar papa quebec romeo sierra tango uniform " +
               "victor whiskey xray yankee zulu").split(" ");

/** 约 nWords 个英文词（≈nWords+ tokens），开头嵌入唯一 runId。
 *
 * 唯一性保证：共享账号下别人不会撞上同一前缀（防污染），
 * 也不会命中本站历史残留缓存（防假阳性）。mulberry32 定种子，同 runId 可复现。
 */
export function buildPrefix(nWords: number, runId: string): string {
  let a = 0;
  for (const ch of runId) a = (a * 31 + ch.charCodeAt(0)) >>> 0;
  const next = () => {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const body = Array.from({ length: nWords },
    () => WORDS[Math.floor(next() * WORDS.length)]).join(" ");
  return `[run:${runId}] cache-passthrough probe. ${body}`;
}

export function makePayload(model: string, prefix: string, maxTokens: number): Json {
  return {
    model,
    max_tokens: maxTokens,
    messages: [{
      role: "user",
      content: [
        { type: "text", text: prefix, cache_control: { type: "ephemeral" } },
        { type: "text", text: "Reply with OK." },
      ],
    }],
  };
}

function usageOf(resp: Json): Json {
  const u = resp.usage ?? {};
  const out: Json = {};
  for (const k of ["input_tokens", "output_tokens",
                   "cache_creation_input_tokens", "cache_read_input_tokens"]) {
    out[k] = u[k] ?? 0;
  }
  return out;
}

async function post(base: string, key: string, payload: Json): Promise<[number, Json]> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "anthropic-version": process.env.PB_ANTHROPIC_VERSION ?? "2023-06-01",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
                  "AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
  };
  if (key) {
    headers["x-api-key"] = key;
    headers.Authorization = `Bearer ${key}`;
  }
  try {
    const resp = await fetch(base.replace(/\/$/, "") + "/v1/messages", {
      method: "POST", headers, body: JSON.stringify(payload),
      signal: AbortSignal.timeout(120000),
    });
    const body = await resp.json().catch(() => ({}));
    return [resp.status, body as Json];
  } catch (e) {
    return [0, { error: String(e) }];
  }
}

export interface VerifyResult {
  verdict: "PASS" | "PARTIAL" | "FAIL";
  u1: Json;
  u2: Json;
  forgedWarning: boolean;
}

export async function verifyCache(base: string, model: string, key: string,
                                  words: number,
                                  log: (s: string) => void = console.log,
                                  sleepMs = 2000): Promise<VerifyResult | null> {
  const runId = Array.from({ length: 10 },
    () => "abcdefghijklmnopqrstuvwxyz0123456789"[randomInt(36)]).join("");
  const prefix = buildPrefix(words, runId);
  log(`run_id=${runId}  前缀≈${words} words  base=${base}  model=${model}`);

  // ---- 第 1 步：预热（期望 creation>0, read==0）----
  let mt = 0;
  let [code, resp] = await post(base, key, makePayload(model, prefix, mt));
  if (code === 400) {
    log("max_tokens:0 被拒（400），降级 max_tokens:1 重试预热…");
    mt = 1;
    [code, resp] = await post(base, key, makePayload(model, prefix, mt));
  }
  if (code !== 200) {
    log(`FAIL: 预热 HTTP ${code}: ${JSON.stringify(resp).slice(0, 300)}`);
    return null;
  }
  const u1 = usageOf(resp);
  log(`[1] 预热(max_tokens=${mt}) usage: ${JSON.stringify(u1)}`);

  // ---- 第 2 步：原样重发（期望 read>0）----
  await new Promise((r) => setTimeout(r, sleepMs));
  [code, resp] = await post(base, key, makePayload(model, prefix, mt));
  if (code !== 200) {
    log(`FAIL: 重发 HTTP ${code}: ${JSON.stringify(resp).slice(0, 300)}`);
    return null;
  }
  const u2 = usageOf(resp);
  log(`[2] 重发 usage: ${JSON.stringify(u2)}`);

  const created = u1.cache_creation_input_tokens > 0;
  const hit = u2.cache_read_input_tokens > 0;
  log("-".repeat(60));
  if (created && hit) {
    log(`PASS ✅ 写 ${u1.cache_creation_input_tokens} / 读 ` +
        `${u2.cache_read_input_tokens} tokens —— 透传正常，可用于链路调试`);
    const forged = u1.cache_read_input_tokens > 0;
    if (forged) {
      log("注意：随机新前缀首call即 read>0，站点可能在伪造缓存字段，数据存疑");
    }
    return { verdict: "PASS", u1, u2, forgedWarning: forged };
  }
  if (created && !hit) {
    log("PARTIAL ⚠️ 有写无读 —— 缓存可能被转格式吞掉一半，慎用");
    return { verdict: "PARTIAL", u1, u2, forgedWarning: false };
  }
  log("FAIL ❌ cache_control 疑似被剥离（无 creation）—— 不能用于缓存实验");
  return { verdict: "FAIL", u1, u2, forgedWarning: false };
}

function parseArgs(argv: string[]): Record<string, string | boolean> {
  const out: Record<string, string | boolean> = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--use-proxy") out["use-proxy"] = true;
    else if (a.startsWith("--")) out[a.slice(2)] = argv[++i] ?? "";
  }
  return out;
}

async function main(): Promise<number> {
  const args = parseArgs(process.argv.slice(2));
  const base = String(args.base ?? "");
  const model = String(args.model ?? "");
  const key = String(args.key ?? process.env.PB_API_KEY ?? "");
  const words = Number(args.words ?? 4500);
  if (!base || !model) {
    console.error("用法: node tools/verify_cache.ts --base <端点根地址> --model <模型名> [--key xxx] [--words 4500]");
    return 2;
  }
  const result = await verifyCache(base, model, key, words);
  if (result === null) return 2;
  return result.verdict === "PASS" ? 0 : result.verdict === "PARTIAL" ? 1 : 2;
}

if (process.argv[1] && /verify_cache\.ts$/.test(process.argv[1])) {
  process.exitCode = await main();
}
