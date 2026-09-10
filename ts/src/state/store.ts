/** 状态层：SQLite 持久化会话表 + TTL 淘汰 + previous_response_id 状态键。
 * （TS 移植自 src/state/store.py，SQLite 用 Node 内置 node:sqlite）
 *
 * 为什么必须有它（方案 1.2）：OpenAI Response 的 previous_response_id 是
 * 「上一轮已存服务端，下一轮只传 ID」的有状态设计；而 Chat / Anthropic 要求每轮
 * 重发完整历史。转换层必须自己变成带存储的服务，把 previous_response_id 解析回
 * 完整历史再重放给无状态协议。
 *
 * 持久化（v1.1）：进程重启会话不丢，TTL 归档（archive）语义可落地查证。
 * history / meta 以 JSON 落库；接口与 Python 版保持一致，上层无感。
 */

import { mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";
import { DatabaseSync } from "node:sqlite";
import * as ir from "../ir/model.ts";
import { ToolIdMap } from "./idmap.ts";
import { SessionConfig, buildPrefix, TTL } from "./session_config.ts";

const DEFAULT_DB = join(
  dirname(fileURLToPath(import.meta.url)), "..", "..", "data", "sessions.db");

/** Message/Block → 可落库 plain object；反向同理。 */
function msgToPlain(m: ir.Message | ir.Json): ir.Json {
  if (!(m instanceof ir.Message)) return m;
  return { role: m.role, blocks: m.blocks.map((b) => ({ ...b })) };
}

function msgFromPlain(d: ir.Json): ir.Message {
  return new ir.Message({
    role: d.role,
    blocks: (d.blocks ?? []).map((b: ir.Json) =>
      new ir.Block(b as { kind: string } & Partial<ir.Block>)),
  });
}

export class Session {
  key: string;
  history: ir.Message[] = [];   // 内存形态；落库为 plain object
  cursor = 0;                   // last_breakpoint 游标
  created_at: number;
  touched_at: number;
  closed = false;
  meta: ir.Json = {};           // memories / memory_cap 等会话级配置
  /** response_id -> 此 session 中已生成的响应序号（处理并发分叉） */
  responses: Record<string, number> = {};

  constructor(init: { key: string } & Partial<Session>) {
    const now = Date.now() / 1000;
    this.key = init.key;
    this.history = init.history ?? [];
    this.cursor = init.cursor ?? 0;
    this.created_at = init.created_at ?? now;
    this.touched_at = init.touched_at ?? now;
    this.closed = init.closed ?? false;
    this.meta = init.meta ?? {};
    this.responses = init.responses ?? {};
  }
}

interface SessionRow {
  key: string;
  history: string;
  cursor: number;
  created_at: number;
  touched_at: number;
  closed: number;
  meta: string;
  responses: string;
}

/** 线程安全的 SQLite 会话表（Node 单线程 + 同步驱动，无需 Python 的锁）。
 *
 * dbPath: undefined → 环境变量 PB_DB → 项目 data/sessions.db；测试可传 ":memory:"。
 */
export class SessionStore {
  cfg: SessionConfig;
  idmap: ToolIdMap;
  private db: DatabaseSync;
  /** 上次惰性淘汰时间戳（测试可写，用于绕过节流） */
  _lastEvict = 0;
  /** 惰性淘汰最小间隔（秒） */
  static readonly EVICT_INTERVAL = 60;

  constructor(cfg: SessionConfig, dbPath?: string) {
    this.cfg = cfg;
    const path = dbPath ?? process.env.PB_DB ?? DEFAULT_DB;
    if (path !== ":memory:") {
      mkdirSync(dirname(resolve(path)), { recursive: true });
    }
    this.db = new DatabaseSync(path);
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS sessions (
        key TEXT PRIMARY KEY,
        history TEXT NOT NULL DEFAULT '[]',
        cursor INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        touched_at REAL NOT NULL,
        closed INTEGER NOT NULL DEFAULT 0,
        meta TEXT NOT NULL DEFAULT '{}',
        responses TEXT NOT NULL DEFAULT '{}'
      );
      CREATE TABLE IF NOT EXISTS resp_index (
        rid TEXT PRIMARY KEY,
        session_key TEXT NOT NULL
      );
    `);
    // v1.4：工具调用 ID 双向映射表（与会话同库同生命周期）
    this.idmap = new ToolIdMap(this.db);
  }

  // -- 序列化辅助 ----------------------------------------------------------
  private static rowToSession(row: SessionRow): Session {
    return new Session({
      key: row.key,
      history: (JSON.parse(row.history) as ir.Json[]).map(msgFromPlain),
      cursor: row.cursor,
      created_at: row.created_at,
      touched_at: row.touched_at,
      closed: Boolean(row.closed),
      meta: JSON.parse(row.meta),
      responses: JSON.parse(row.responses),
    });
  }

  /** @internal 测试与 store 内部使用 */
  _load(key: string): Session | null {
    const row = this.db.prepare(
      "SELECT key,history,cursor,created_at,touched_at,closed,meta,responses " +
      "FROM sessions WHERE key=?").get(key) as SessionRow | undefined;
    return row ? SessionStore.rowToSession(row) : null;
  }

  /** @internal 测试与 store 内部使用 */
  _save(s: Session): void {
    this.db.prepare(
      "INSERT OR REPLACE INTO sessions " +
      "(key,history,cursor,created_at,touched_at,closed,meta,responses) " +
      "VALUES (?,?,?,?,?,?,?,?)")
      .run(s.key,
           JSON.stringify(s.history.map(msgToPlain)),
           s.cursor, s.created_at, s.touched_at, s.closed ? 1 : 0,
           JSON.stringify(s.meta), JSON.stringify(s.responses));
  }

  // -- session 生命周期 --------------------------------------------------
  getOrCreate(key: string): Session {
    // 惰性淘汰（LIMITATIONS #11）：由流量驱动，无后台线程；
    // 同步驱动无锁需求（Python 版需在锁外调用避免非可重入锁死锁）。
    this.maybeEvict();
    let s = this._load(key);
    if (s === null || s.closed) {
      s = new Session({ key });
    }
    s.touched_at = Date.now() / 1000;
    this._save(s);
    return s;
  }

  /** 把 previous_response_id 解析回它所属的 session。 */
  resolvePrevious(previousResponseId: string | null): Session | null {
    if (!previousResponseId) return null;
    const row = this.db.prepare(
      "SELECT session_key FROM resp_index WHERE rid=?")
      .get(previousResponseId) as { session_key: string } | undefined;
    return row ? this._load(row.session_key) : null;
  }

  /** 登记一个新响应，返回 response_id（供下一轮 previous_response_id 引用）。
   *
   * 注意：调用方持有的 session 可能是旧快照（如中途 updateMeta 过），
   * 必须先读最新行再改，避免用旧 meta 覆盖新值。
   */
  recordResponse(session: Session): string {
    const rid = "resp_" + randomUUID().replaceAll("-", "").slice(0, 16);
    const fresh = this._load(session.key) ?? session;
    fresh.responses[rid] = Object.keys(fresh.responses).length;
    fresh.touched_at = Date.now() / 1000;
    this._save(fresh);
    // 同步调用方对象的 meta/responses，防止其后续再 _save 旧值
    session.meta = fresh.meta;
    session.responses = fresh.responses;
    session.history = fresh.history;
    session.cursor = fresh.cursor;
    this.db.prepare(
      "INSERT OR REPLACE INTO resp_index (rid, session_key) VALUES (?,?)")
      .run(rid, session.key);
    return rid;
  }

  /** 合并写入会话 meta（如 memories / memory_cap），保留既有键。 */
  updateMeta(key: string, kw: ir.Json): void {
    let s = this._load(key);
    if (s === null) s = new Session({ key });
    Object.assign(s.meta, kw);
    s.touched_at = Date.now() / 1000;
    this._save(s);
  }

  close(key: string): void {
    const s = this._load(key);
    if (s) {
      s.closed = true;
      this._save(s);
    }
  }

  /** 关闭底层 SQLite 连接（进程退出/测试清理用）。 */
  shutdown(): void {
    this.db.close();
  }

  // -- 重放 ---------------------------------------------------------------
  /** 按当前配置的重放起点，返回本轮要拼进前缀的历史。 */
  replay(session: Session): ir.Message[] {
    return buildPrefix(session.history, session.cursor, this.cfg);
  }

  append(session: Session, messages: ir.Message[]): void {
    // 并发同键会话的丢更新修复（第四轮自查）：调用方持有的可能是旧快照——
    // convert() 读会话到 finalizeTurn() 落库之间隔着一次上游 RTT，另一个
    // 同键请求可能已 append 过；INSERT OR REPLACE 整行覆盖会把对方的轮次抹掉。
    // 先读最新行再合并（与 recordResponse 同一模式），并发下历史只多不少。
    const fresh = this._load(session.key) ?? session;
    fresh.history.push(...messages);
    fresh.cursor = fresh.history.length;
    fresh.touched_at = Date.now() / 1000;
    this._save(fresh);
    // 同步调用方对象，防止其后续再 _save 旧值造成二次覆盖
    session.history = fresh.history;
    session.cursor = fresh.cursor;
    session.touched_at = fresh.touched_at;
    session.meta = fresh.meta;
    session.responses = fresh.responses;
  }

  // -- TTL 淘汰 -----------------------------------------------------------
  /** 惰性 TTL 淘汰：读写路径顺手调用，按 EVICT_INTERVAL 节流。
   *
   * 返回实际淘汰数量；处于节流窗口内时返回 0（未执行扫描）。
   * _lastEvict 的竞写在 Python 里是良性的，Node 单线程下天然无竞态。
   */
  maybeEvict(now?: number): number {
    const t = now ?? Date.now() / 1000;
    if (t - this._lastEvict < SessionStore.EVICT_INTERVAL) return 0;
    this._lastEvict = t;
    return this.evictExpired(t);
  }

  /** TTL 结束策略下淘汰超时会话。返回淘汰数量。 */
  evictExpired(now?: number): number {
    if (this.cfg.end_policy !== TTL) return 0;
    const t = now ?? Date.now() / 1000;
    const cutoff = t - this.cfg.ttl_seconds;
    const rows = this.db.prepare(
      "SELECT key FROM sessions WHERE closed=0 AND touched_at<?")
      .all(cutoff) as { key: string }[];
    for (const { key } of rows) {
      if (this.cfg.on_end === "drop") {
        this.db.prepare("DELETE FROM sessions WHERE key=?").run(key);
        // 级联清理（第四轮自查）：resp_index / tool_id_map 以 session_key 为
        // 外键语义，只删主表会留下孤儿行，长运行网关上两张辅表无界增长。
        this.db.prepare("DELETE FROM resp_index WHERE session_key=?").run(key);
        this.db.prepare("DELETE FROM tool_id_map WHERE session_key=?").run(key);
      } else { // archive：标记关闭但保留可查（辅表随主表一并保留，语义一致）
        this.db.prepare("UPDATE sessions SET closed=1 WHERE key=?").run(key);
      }
    }
    return rows.length;
  }
}
