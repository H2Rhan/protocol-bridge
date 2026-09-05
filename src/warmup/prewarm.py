"""max_tokens:0 预热路径 —— 已核实为 Anthropic 官方支持用法（方案 3.7）。

官方行为（核对日期 2026-09-01）：
  - 读入提示并在 cache_control 断点处写缓存，不生成输出即返回。
  - 响应是"畸形"的：content 空数组、stop_reason="max_tokens"、usage 完整填充
    （output_tokens=0，零输出计费）。
  - 拒绝条件（invalid_request_error）：带 stream / extended thinking /
    structured outputs(output_config.format) / tool_choice 为 tool 或 any；
    Message Batches 内也不支持。
  - 断点必须打在与后续请求共享的前缀末尾（如 system），不能打在占位 user 消息上，
    否则缓存条目以占位消息为键、后续永不命中。自动缓存会把断点放在最后一块（占位消息）。

转换层必须为它开单独路径：若按正常响应处理，会把预热响应当错误抛掉。
"""
from __future__ import annotations

from ..ir import model as ir

# 占位 user 消息：需非空白字符串（官方示例用 "warmup"），会被读入但不会被回答
PLACEHOLDER = "warmup"

# 与预热冲突、会被 invalid_request_error 拒绝的参数
FORBIDDEN_WITH_WARMUP = ("stream", "thinking", "output_config", "tool_choice")


def build_warmup_request(system_text: str, model: str) -> dict:
    """构造一个预热请求。

    断点打在 system（与后续请求共享的前缀末尾），不打在占位消息上。
    """
    return {
        "model": model,
        "max_tokens": 0,
        "system": [{"type": "text", "text": system_text,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": PLACEHOLDER}],
    }


def validate_warmup(payload: dict) -> list[str]:
    """返回会导致预热被拒的冲突参数清单（空 = 可发）。"""
    conflicts = []
    if payload.get("stream"):
        conflicts.append("stream")
    thinking = payload.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        conflicts.append("thinking")
    if payload.get("output_config", {}).get("format"):
        conflicts.append("output_config.format")
    tc = payload.get("tool_choice", {})
    if isinstance(tc, dict) and tc.get("type") in ("tool", "any"):
        conflicts.append("tool_choice")
    if payload.get("max_tokens", 1) != 0:
        conflicts.append("max_tokens!=0")
    return conflicts


def parse_warmup_response(body: dict) -> ir.IRResponse:
    """把"畸形"预热响应解析成 IRResponse，标记 is_warmup，避免被当错误。"""
    usage = body.get("usage", {}) or {}
    return ir.IRResponse(
        id=body.get("id", ""),
        model=body.get("model", ""),
        blocks=[],  # content 为空数组，属正常
        stop_reason=body.get("stop_reason"),  # 恒为 "max_tokens"
        usage=ir.IRUsage(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
            cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
        ),
        is_warmup=True,
    )


def is_warmup_response(body: dict) -> bool:
    """判定一个响应是否为预热响应（空 content + stop_reason=max_tokens）。"""
    return (isinstance(body.get("content"), list)
            and len(body["content"]) == 0
            and body.get("stop_reason") == "max_tokens")
