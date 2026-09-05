"""IR（中间表示）数据模型 —— 三方唯一契约（v0 冻结）。

三层：
  L0 内容块模型  ：消息内部的最小组成（text / tool_use / tool_result / thinking）
  L1 规范请求    ：一次调用的完整规范化请求（model / system / messages / tools / 采样参数）
  L2 会话与缓存  ：跨轮上下文（session 标识、重放起点、缓存断点布局）

设计约束（对应执行方案 v3）：
  - 每个协议只写 to_ir / from_ir 两个方向，3 对 adapter 替代 6 方向两两互转。
  - L2 的断点布局 = 3 个固定分层断点 + 1 个滚动尾部断点（用满官方 4 个 cache_control 上限）。
  - 未知/不可映射字段一律进 `extra`，绝不静默丢弃（丢弃要进降级路径埋点）。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional
import json


# ---------------------------------------------------------------------------
# L0 · 内容块模型
# ---------------------------------------------------------------------------

# 规范化内容块类型
TEXT = "text"
TOOL_USE = "tool_use"
TOOL_RESULT = "tool_result"
THINKING = "thinking"
IMAGE = "image"      # 多模态：方案 3.8 明确不做，仅占位以便显式降级
DOCUMENT = "document"


@dataclass
class Block:
    """L0 内容块。`kind` 决定其余字段的语义。"""
    kind: str
    text: Optional[str] = None
    # tool_use / tool_result
    tool_name: Optional[str] = None
    tool_id: Optional[str] = None
    tool_input: Optional[dict] = None
    # 缓存断点标记（Anthropic cache_control）；仅允许出现在断点布局指定的块上
    cache_breakpoint: bool = False
    # 未被规范化的原始字段（显式降级的证据，不静默丢弃）
    extra: dict = field(default_factory=dict)


@dataclass
class Message:
    """L0 消息。role 规范化为 user / assistant / system（system 也可上提到 L1.system）。"""
    role: str
    blocks: list = field(default_factory=list)  # list[Block]

    @staticmethod
    def text(role: str, text: str) -> "Message":
        return Message(role=role, blocks=[Block(kind=TEXT, text=text)])


# ---------------------------------------------------------------------------
# L1 · 规范请求
# ---------------------------------------------------------------------------

@dataclass
class Tool:
    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)
    cache_breakpoint: bool = False
    extra: dict = field(default_factory=dict)


@dataclass
class IRRequest:
    """一次调用的规范化请求。"""
    model: str = ""
    system: list = field(default_factory=list)      # list[Block]，渲染顺序固定在 tools 之后
    messages: list = field(default_factory=list)    # list[Message]
    tools: list = field(default_factory=list)       # list[Tool]
    max_tokens: int = 1024
    temperature: Optional[float] = None
    stream: bool = False
    # 采样/生成等其他参数（不可映射时显式降级）
    extra: dict = field(default_factory=dict)


@dataclass
class IRUsage:
    """归一化 usage。三家口径不同，统一拆出缓存读写，避免对账全错。"""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total_input(self) -> int:
        # Anthropic 口径：input_tokens 不含缓存读写
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens


@dataclass
class IRResponse:
    id: str = ""
    model: str = ""
    blocks: list = field(default_factory=list)  # list[Block]
    stop_reason: Optional[str] = None
    usage: IRUsage = field(default_factory=IRUsage)
    # max_tokens:0 预热响应是"畸形"的（空 content），用此标记避免被当错误
    is_warmup: bool = False
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# L2 · 会话与缓存上下文
# ---------------------------------------------------------------------------

@dataclass
class SessionContext:
    """跨轮上下文。重放起点/缓存断点布局由 config/session.json 驱动。"""
    session_key: str = ""                 # 由 key_fields 拼出的状态键
    history: list = field(default_factory=list)  # list[Message]，已重放的历史
    cursor: int = 0                       # last_breakpoint 重放起点游标
    # 缓存断点布局：固定分层断点的位置标记（tools 后 / system 后 / 历史静态段后）
    bp_after_tools: bool = True
    bp_after_system: bool = True
    bp_after_history_static: bool = True
    bp_rolling_tail: bool = True          # 第 4 个（滚动尾部）


# ---------------------------------------------------------------------------
# 序列化（冻结契约的 wire 形态）
# ---------------------------------------------------------------------------

def to_dict(obj: Any) -> Any:
    return asdict(obj)


def dumps(obj: Any) -> str:
    return json.dumps(to_dict(obj), ensure_ascii=False)
