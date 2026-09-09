"""工具调用 ID 双向持久映射（v1.4，问题清单组4#3）。

三家工具 ID 命名不同：Anthropic `toolu_*`、Chat `call_*`、Responses 同时有
`call_id` 与 `item.id`。ID 由服务端生成、每次都变，跨协议往返必须维护一张
映射表——否则 Anthropic→Chat→Anthropic 一圈回来，最初那个 `toolu_*` 就丢了，
上游校验 tool_use_id 对应关系时直接 400。

设计：
- canonical id = 该 ID **首次出现时的原始形式**（含其来源协议）。
- `incoming(session, ext_id, proto)`：把某协议的外部 ID 归一到 canonical。
- `outgoing(session, canon, proto)`：取 canonical 在某协议下的外部形式；
  目标协议就是来源协议时返回 canonical 本身（字节无损），否则铸造新 ID
  （按目标协议习惯前缀）并持久化——同一会话内稳定，前缀序列化不被破坏。
- 映射随会话存 SQLite，TTL/归档与会话表同生命周期（不单独维护过期）。
- 冲突策略：first-wins。同一 (session, proto, ext) 永不改绑；
  同一 canon 在不同 proto 下各有独立外部形式。
"""
from __future__ import annotations

import time
import uuid

# 各协议的 ID 习惯前缀（铸造新 ID 时用；仅约定，不做严格校验）
PROTO_PREFIX = {
    "anthropic": "toolu_",
    "openai_chat": "call_",
    "openai_response": "call_",
}


class ToolIdMap:
    """会话级工具 ID 映射表。与 SessionStore 共用一个 SQLite 连接。"""

    def __init__(self, db):
        self._db = db
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tool_id_map (
                session_key TEXT NOT NULL,
                proto       TEXT NOT NULL,
                ext_id      TEXT NOT NULL,
                canon       TEXT NOT NULL,
                origin_proto TEXT NOT NULL,
                created_at  REAL NOT NULL,
                PRIMARY KEY (session_key, proto, ext_id)
            );
            CREATE INDEX IF NOT EXISTS idx_tool_id_canon
                ON tool_id_map (session_key, canon);
            """
        )
        self._db.commit()

    def incoming(self, session_key: str, ext_id: str | None, proto: str) -> str | None:
        """外部 ID → canonical。未见过的 ID 以其本身为 canonical 登记。"""
        if not ext_id:
            return ext_id
        row = self._db.execute(
            "SELECT canon FROM tool_id_map WHERE session_key=? AND proto=? AND ext_id=?",
            (session_key, proto, ext_id)).fetchone()
        if row:
            return row[0]
        self._db.execute(
            "INSERT OR IGNORE INTO tool_id_map "
            "(session_key, proto, ext_id, canon, origin_proto, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (session_key, proto, ext_id, ext_id, proto, time.time()))
        self._db.commit()
        return ext_id

    def outgoing(self, session_key: str, canon: str | None, proto: str) -> str | None:
        """canonical → 目标协议外部形式（稳定、可持久、按目标协议前缀铸造）。"""
        if not canon:
            return canon
        # 已铸造过 → 直接用（跨轮稳定，前缀不抖动）
        row = self._db.execute(
            "SELECT ext_id FROM tool_id_map WHERE session_key=? AND proto=? AND canon=?",
            (session_key, proto, canon)).fetchone()
        if row:
            return row[0]
        # 来源协议就是目标协议 → canonical 原样返回（同协议字节无损）
        origin = self._db.execute(
            "SELECT origin_proto FROM tool_id_map WHERE session_key=? AND canon=? "
            "LIMIT 1", (session_key, canon)).fetchone()
        if origin and origin[0] == proto:
            return canon
        # 铸造新 ID 并持久化
        ext = PROTO_PREFIX.get(proto, "call_") + uuid.uuid4().hex[:24]
        self._db.execute(
            "INSERT OR IGNORE INTO tool_id_map "
            "(session_key, proto, ext_id, canon, origin_proto, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (session_key, proto, ext, canon,
             origin[0] if origin else proto, time.time()))
        self._db.commit()
        return ext

    def size(self, session_key: str) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) FROM tool_id_map WHERE session_key=?",
            (session_key,)).fetchone()
        return row[0]
