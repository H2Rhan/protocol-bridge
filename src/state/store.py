"""状态层：SQLite 持久化会话表 + TTL 淘汰 + previous_response_id 状态键。

为什么必须有它（方案 1.2）：OpenAI Response 的 previous_response_id 是
「上一轮已存服务端，下一轮只传 ID」的有状态设计；而 Chat / Anthropic 要求每轮
重发完整历史。转换层必须自己变成带存储的服务，把 previous_response_id 解析回
完整历史再重放给无状态协议。

持久化（v1.1）：进程重启会话不丢，TTL 归档（archive）语义可落地查证。
history / meta 以 JSON 落库；接口与内存版保持一致，上层无感。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict

from ..ir.model import Block, Message
from .session_config import SessionConfig, build_prefix, TTL

_DEFAULT_DB = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "sessions.db"
)


def _msg_to_dict(m) -> dict:
    """Message 对象 -> 可落库的 dict（已是 dict 则原样透传）。"""
    if isinstance(m, dict):
        return m
    return {"role": m.role, "blocks": [asdict(b) for b in m.blocks]}


def _msg_from_dict(d: dict) -> Message:
    return Message(role=d["role"],
                   blocks=[Block(**b) for b in d.get("blocks", [])])


@dataclass
class Session:
    key: str
    history: list = field(default_factory=list)  # list[Message]（内存形态；落库为 dict）
    cursor: int = 0                              # last_breakpoint 游标
    created_at: float = field(default_factory=time.time)
    touched_at: float = field(default_factory=time.time)
    closed: bool = False
    meta: dict = field(default_factory=dict)     # memories / memory_cap 等会话级配置
    # response_id -> 此 session 中已生成的响应序号（处理并发分叉）
    responses: dict = field(default_factory=dict)


class SessionStore:
    """线程安全的 SQLite 会话表。previous_response_id 的状态键复用 Session 标识体系。

    db_path: None → 环境变量 PB_DB → 项目 data/sessions.db；测试可传 ":memory:"。
    """

    def __init__(self, cfg: SessionConfig, db_path: str | None = None):
        self.cfg = cfg
        path = db_path or os.environ.get("PB_DB") or _DEFAULT_DB
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(
            """
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
            """
        )
        self._db.commit()

    # -- 序列化辅助 ----------------------------------------------------------
    @staticmethod
    def _row_to_session(row) -> Session:
        key, history, cursor, created, touched, closed, meta, responses = row
        return Session(
            key=key,
            history=[_msg_from_dict(d) for d in json.loads(history)],
            cursor=cursor,
            created_at=created,
            touched_at=touched,
            closed=bool(closed),
            meta=json.loads(meta),
            responses=json.loads(responses),
        )

    def _load(self, key: str) -> Session | None:
        row = self._db.execute(
            "SELECT key,history,cursor,created_at,touched_at,closed,meta,responses "
            "FROM sessions WHERE key=?", (key,)).fetchone()
        return self._row_to_session(row) if row else None

    def _save(self, s: Session) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO sessions "
            "(key,history,cursor,created_at,touched_at,closed,meta,responses) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (s.key, json.dumps([_msg_to_dict(m) for m in s.history],
                               ensure_ascii=False), s.cursor,
             s.created_at, s.touched_at, int(s.closed),
             json.dumps(s.meta, ensure_ascii=False),
             json.dumps(s.responses, ensure_ascii=False)))
        self._db.commit()

    # -- session 生命周期 --------------------------------------------------
    def get_or_create(self, key: str) -> Session:
        with self._lock:
            s = self._load(key)
            if s is None or s.closed:
                s = Session(key=key)
            s.touched_at = time.time()
            self._save(s)
            return s

    def resolve_previous(self, previous_response_id: str | None) -> Session | None:
        """把 previous_response_id 解析回它所属的 session。"""
        if not previous_response_id:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT session_key FROM resp_index WHERE rid=?",
                (previous_response_id,)).fetchone()
            return self._load(row[0]) if row else None

    def record_response(self, session: Session) -> str:
        """登记一个新响应，返回 response_id（供下一轮 previous_response_id 引用）。

        注意：调用方持有的 session 可能是旧快照（如中途 update_meta 过），
        必须先读最新行再改，避免用旧 meta 覆盖新值。
        """
        rid = "resp_" + uuid.uuid4().hex[:16]
        with self._lock:
            fresh = self._load(session.key) or session
            fresh.responses[rid] = len(fresh.responses)
            fresh.touched_at = time.time()
            self._save(fresh)
            # 同步调用方对象的 meta/responses，防止其后续再 _save 旧值
            session.meta = fresh.meta
            session.responses = fresh.responses
            session.history = fresh.history
            session.cursor = fresh.cursor
            self._db.execute(
                "INSERT OR REPLACE INTO resp_index (rid, session_key) VALUES (?,?)",
                (rid, session.key))
            self._db.commit()
        return rid

    def update_meta(self, key: str, **kw) -> None:
        """合并写入会话 meta（如 memories / memory_cap），保留既有键。"""
        with self._lock:
            s = self._load(key)
            if s is None:
                s = Session(key=key)
            s.meta.update(kw)
            s.touched_at = time.time()
            self._save(s)

    def close(self, key: str) -> None:
        with self._lock:
            s = self._load(key)
            if s:
                s.closed = True
                self._save(s)

    def shutdown(self) -> None:
        """关闭底层 SQLite 连接（进程退出/测试清理用）。"""
        with self._lock:
            self._db.close()

    # -- 重放 ---------------------------------------------------------------
    def replay(self, session: Session) -> list:
        """按当前配置的重放起点，返回本轮要拼进前缀的历史。"""
        return build_prefix(session.history, session.cursor, self.cfg)

    def append(self, session: Session, messages: list) -> None:
        with self._lock:
            session.history.extend(messages)
            session.cursor = len(session.history)
            session.touched_at = time.time()
            self._save(session)

    # -- TTL 淘汰 -----------------------------------------------------------
    def evict_expired(self, now: float | None = None) -> int:
        """TTL 结束策略下淘汰超时会话。返回淘汰数量。"""
        if self.cfg.end_policy != TTL:
            return 0
        now = now or time.time()
        cutoff = now - self.cfg.ttl_seconds
        removed = 0
        with self._lock:
            rows = self._db.execute(
                "SELECT key FROM sessions WHERE closed=0 AND touched_at<?",
                (cutoff,)).fetchall()
            for (key,) in rows:
                if self.cfg.on_end == "drop":
                    self._db.execute("DELETE FROM sessions WHERE key=?", (key,))
                else:  # archive：标记关闭但保留可查
                    self._db.execute(
                        "UPDATE sessions SET closed=1 WHERE key=?", (key,))
                removed += 1
            self._db.commit()
        return removed
