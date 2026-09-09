# 已知有损字段清单（Lossy Fields）

> 问题清单主线交付物（组5#10）。由三个 adapter 的实码推导（`record_unknown` / `dropped.add` 全覆盖点），
> 与运行时 `_bridge.dropped` 每轮暴露的记录同口径。**凡转换会丢语义的地方，这里必须有一行。**
> 数据日期 2026-09-09 · v1.4 实码核对。

## 一、内容块级（message content 内部）

| 字段/块 | 方向 | 损耗 | 去向 |
|---|---|---|---|
| `image` / `document` / `audio` 等多模态块 | 全方向 | 不转换（方案 3.8 明确不做） | `dropped: content.<type>` 显式记录 |
| Chat 多模态 content parts（`image_url` / `input_audio` / `file`） | Chat→* | 同上 | `dropped: content.<type>` |
| Responses `input_image` / `input_file` 等非文本条目 | Responses→* | 同上 | `dropped: input.<type>` |
| 缓存断点 `cache_control` | Anthropic→Chat/Responses | Chat/Responses 无断点概念，往返后丢失（衰减未量化 → LIMITATIONS #8） | IR `cache_breakpoint` 标记仅同协议保留 |
| Chat 消息 `name` / `refusal` / `audio` 字段 | Chat→* | 无 IR 字段，不回传 | 当前不进降级记录（见「四、已知缺口」） |
| Responses `reasoning` 的 `summary` 结构化数组 | Responses→* | 仅字符串 summary 进 IR，结构丢弃 | 文本保留，结构降级 |
| thinking `display:"omitted"` | Anthropic→* | 文本为空但 signature 保留（v1.4 修复后不再误判丢弃） | `extra.signature` 保留 |

## 二、请求参数级（payload 顶层）

| 字段 | 方向 | 损耗 | 去向 |
|---|---|---|---|
| `n` / `logprobs` / `top_logprobs` / `logit_bias` / `seed` / `frequency_penalty` / `presence_penalty` | *→Anthropic | Anthropic 全部不支持，不生效 | `record_unknown` → dropped 清单 + 告警 |
| `top_p` | *→Anthropic | 与 `temperature` 互斥，不进渲染 | 同上 |
| `response_format`（结构化输出） | *→Anthropic | Anthropic 无原生 JSON Schema 模式，约束保证静默失去（问题清单组2#4：显式降级，不默认通过） | 同上 |
| Chat 顶层 `system`（非规范误发） | Chat→* | 语义保留（上提 IR.system）但记录"这不是规范写法" | 上提 + dropped 记录 |
| `previous_response_id`（会话未找到） | Responses→* | 降级为无状态处理 | dropped 记录 + 客户端可见 |
| Responses `text.format` / `output_config` | Responses→Anthropic | 无 IR 字段 | `record_unknown` → dropped |
| TTL 24h 保留语义 | Responses→Anthropic | Anthropic 最长 1h，只能降级 | 文档标注（capability-matrix 计价表） |

## 三、usage 归一级

| 字段 | 损耗 | 说明 |
|---|---|---|
| `reasoning_tokens` / `*_tokens_details` 明细 | 归一后不单独保留 | 计入 `output_tokens` 总数，明细粒度丢失（组4#2 B 级：折算或显式标注不可比） |
| `service_tier` / `system_fingerprint` 等上游元数据 | 不归一 | 进 `_bridge` 元信息之外的原始响应，网关透传不消费 |

## 四、已知缺口（清单本身的盲区，诚实标注）

| 缺口 | 说明 | 后续 |
|---|---|---|
| 工具定义**内部**的非规范字段不进降级记录 | MCP 工具的 `server_url` / `server_label` / `require_approval`、内置工具（`web_search` 等）的专有参数——`to_ir` 只取 name/description/input_schema，其余字段丢失且**当前无 dropped 记录**（`record_unknown` 只覆盖顶层） | 候选修复：tools[] 逐项 record_unknown；评审被问到的答法：MCP 语义保真在方案 3.8 已声明非目标（降级为 function），但"降级无记录"确实待补 |
| property-based 覆盖范围 | `TestPropertyRoundTrip` 覆盖文本/工具/thinking 三族块的随机组合（440 用例），未覆盖多模态与流式 | 多模态属方案 3.8 非目标；流式见 LIMITATIONS #1 |

## 五、验证方式

- 静态：本清单每行可在 `src/adapters/*.py` 找到对应 `dropped.add` / `record_unknown` 调用点。
- 动态：任意请求经网关后读响应 `_bridge.dropped`——与 metrics.jsonl 的降级路径埋点（第④⑤项暴露）同一份数据。
- 回归：`TestPropertyRoundTrip` 性质②（任何未知顶层字段必进降级记录，440 随机用例）防"静默丢弃"回潮。
