# 能力矩阵 v0（可配置 · 不硬编码 · 数据日期 2026-09-01）

> 缓存阈值/折扣**逐模型查表、注明数据日期**，不外推（阈值非单调）。
> 本表为 v0，拿到真实 API 后按 `count_tokens` 与真实响应逐格校准。

## Anthropic 最小可缓存长度（按断点处累计前缀判定）

| 模型 | 最小可缓存 token |
|---|---|
| Claude Opus 5 / Fable 5 / Mythos 5 | 512 |
| Claude Mythos Preview / Opus 4.7 | 2048 |
| Claude Opus 4.6 / 4.5 | 4096 |
| Claude Opus 4.8 / Sonnet 5 / Sonnet 4.6 / 4.5 | 1024 |
| Claude Haiku 4.5 | 4096 |

## 缓存计价与折扣

| 维度 | Anthropic | OpenAI |
|---|---|---|
| 写入 | 5 分钟档 1.25x / 1 小时档 2x | 无写入溢价 |
| 读取 | 0.1x（省 90%） | GPT-4o/o 系省 50% / GPT-4.1 系省 75% / GPT-5 系省 90% |
| TTL | 5 分钟 / 1 小时 | 默认 5–10 分钟，可选扩展 24 小时 |
| 触发 | 显式 cache_control（每请求 ≤4 断点） | 自动（prompt ≥ 1024 token） |
| 回看 | 20 content block | — |

## 硬约束（实验要复现/规避）

- 4 断点上限：第 5 个 cache_control 返回 400。
- 20-block 回看：单轮新增 >20 块，尾部断点找不到上次写入 → 全重算不报错。
- 缓存条目在首个响应开始后才可用：对照实验须先串行预热再放量。
- max_tokens:0 预热：官方支持；禁带 stream/thinking/structured outputs/tool_choice(tool,any)；Batches 不支持。

## 字段级支持矩阵（v0）

| 能力 | Chat→Anthropic | Response→Anthropic | Anthropic→Chat/Response | 状态 |
|---|---|---|---|---|
| 文本消息 | ✅ | ✅ | ✅ | 离线已测 |
| system/instructions | ✅ | ✅ | ✅ | 离线已测 |
| 工具调用/结果 | ✅ | ✅ | ✅ | 离线已测 |
| usage 归一 | ✅ | ✅ | ✅ | 离线已测 |
| previous_response_id | — | ✅（状态层重放） | — | 骨架 |
| 缓存断点布局 | ✅ | ✅ | — | 骨架 |
| SSE 流式透传 | ✅ | ✅ | ✅ | mock 已测 |
| max_tokens:0 预热 | — | — | ✅ | 字段处理已测 |
| 多模态 / Realtime / Batches | ❌ | ❌ | ❌ | 方案 3.8 明确不做 |
