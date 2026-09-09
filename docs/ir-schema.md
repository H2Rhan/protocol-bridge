# IR v0 · 中间表示契约（冻结）

> 本文件是 05A（Chat）、05B（Response）、Anthropic 三方的**唯一契约**。
> IR 一冻结，05A 与 05B 即可并行开发。修改需双方共同签字。
> 数据日期：2026-09-01。

三个协议各写两个方向：`to_ir`（协议 → IR）与 `from_ir`（IR → 协议）。
3 对 adapter 替代 6 个方向的两两互转；Chat 与 Response 结构差异大、不能直接互转，必须经 IR。

---

## L0 · 内容块模型

消息内部的最小组成。`Block.kind` 决定其余字段语义：

| kind | 含义 | 关键字段 |
|---|---|---|
| `text` | 文本块 | `text` |
| `tool_use` | 模型发起工具调用 | `tool_name` / `tool_id` / `tool_input` |
| `tool_result` | 工具结果回填 | `tool_id`（关联 tool_use）/ `text` |
| `thinking` | 扩展思考块 | `text`（往返需保留，见硬约束） |
| `image` / `document` | 多模态 | 方案 3.8 明确不做，仅占位以便**显式降级** |

约定：
- `Block.cache_breakpoint` 标记缓存断点，**只允许**出现在 L2 断点布局指定的块上。
- 未规范化字段一律进 `Block.extra`，**绝不静默丢弃**（丢弃须进降级路径埋点）。

## L1 · 规范请求

一次调用的完整规范化请求（`IRRequest`）：

```
model, system[list<Block>], messages[list[Message>], tools[list[Tool]],
max_tokens, temperature, stream, extra
```

- **渲染顺序固定为 tools → system → messages**（Anthropic 缓存层级，前缀匹配按此顺序失效）。
- `system` 用 `list<Block>` 而非纯字符串，以便在尾部块打缓存断点。

## L2 · 会话与缓存上下文

跨轮上下文（`SessionContext`）：

```
session_key      由 config.key_fields 拼出的状态键
history          已重放的历史消息
cursor           last_breakpoint 重放起点游标
bp_after_tools / bp_after_system / bp_after_history_static / bp_rolling_tail
```

### 缓存断点布局（= IR 分层）

官方硬上限：**每请求最多 4 个 `cache_control` 断点，第 5 个返回 400**。
因此采用 **3 固定 + 1 滚动**，恰好用满：

1. tools 后（`bp_after_tools`）
2. system 后（`bp_after_system`）
3. 历史静态段后（`bp_after_history_static`）
4. 滚动尾部，跟随每轮最后一块（`bp_rolling_tail`）

> 不采用「历史每 N 块插断点」——长对话必然突破 4 个上限。
> 断点布局依据：断点按**前缀累积**生效（exp3/exp12 实测）+ 4 断点硬上限（exp7 实测第 5 个返回 400）。

### Session 边界（可切换，config/session.json 驱动）

| 开关 | 取值 | 说明 |
|---|---|---|
| `key_granularity` | `single_task` / `three_level` | **启动时读配置**，运行中途切换会致已存会话键值错乱 |
| `key_fields` | `["task-id"]` 或 `["x-team-id","x-agent-id","x-task-id"]` | 拼 `session_key` |
| `replay_from` | `full` / `last_breakpoint` / `sliding_window` | 重放起点（决定缓存前缀） |
| `sliding_window_n` | int | `sliding_window` 时生效 |
| `end_policy` | `ttl` / `explicit_close` | 结束策略 |
| `ttl_seconds` / `on_end` | int / `archive`/`drop` | TTL 与结束后处理 |

默认假设值（04 组未对齐时先跑）：`single_task` + `full` 全量重放（最保守、最易被 04 组兼容）。
交付用**条件结论**写法：在「边界 = X」前提下陈述命中率结论。

---

## usage 归一化（对账不错的前提）

| 协议 | input token 构成 |
|---|---|
| Anthropic | `input_tokens` 不含缓存读写；总数 = input + cache_read + cache_creation |
| OpenAI Chat | `prompt_tokens` 含缓存，`prompt_tokens_details.cached_tokens` 为子集 |
| OpenAI Responses | `input_tokens` + `input_tokens_details.cached_tokens` |

统一拆成 `IRUsage{input, output, cache_creation, cache_read}`，`total_input = input + cache_creation + cache_read`。

---

## 变更记录

「冻结」的准确含义：**L0/L1 语义层冻结**（块模型与规范请求的字段集合），实现层允许修订，
但任何修订必须配回归测试、契约外字段必须进 `extra` 降级记录（`record_unknown()` 统一收口）。
契约的稳定性不靠「不改」保证，靠「改了一定被发现」保证。

| 版本 | 日期 | 变更 | 性质 |
|---|---|---|---|
| v0 | 2026-09-01 | 三层契约定稿（本文件） | 语义层冻结 |
| v0.1 | 2026-09-05 | 实现层修订：落地第 4 个固定断点 `bp_after_history_static`；三 adapter 顶层字段统一 `record_unknown()` 收口；thinking/tool_choice 经 `extra` 透传。**L0/L1 语义未动** | 实现层（各配回归测试，见 REVIEW.md 第五节） |
| v0.2 | 2026-09-09 | 删除对「20-block 回看窗口」的引用——该约束已被本组实验决定性证伪（exp5/7/11：60 块 10872 token 仍 100% 命中，见 experiment-results.md 第三节） | 文档修订（契约字段未动） |

## 签字

- 05A（Chat）：＿＿＿
- 05B（Response）：＿＿＿
- 日期：2026-09-01
