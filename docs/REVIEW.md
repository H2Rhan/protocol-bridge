# 代码评审材料（Code Review Pack）

> 版本 v1.2（2026-09-02）｜ src 1493 行 + tools/experiments/tests 1065 行（纯标准库，零第三方依赖，Python 3.11+）
> 测试：**53 项单测全过 + 18 项端到端冒烟全过**（2026-09-09，含第三轮自查回归 + property-based 性质测试 + 惰性淘汰回归 + SSE 逐块转换）+ Groq 真实链路验证通过（2026-09-01）
> 本版差异：**两轮代码自查发现 9 个真实 bug，全部修复并各配回归测试**（见第六节）

## 一、整体架构（IR 星型）

```
OpenAI Chat ──adapter──┐                       ┌── 状态层（SQLite 会话表 + TTL）
OpenAI Response ─adapter┼──▶ IR 唯一契约 ──adapter──▶ Anthropic（cache_control 断点）
                        │                       ├── 记忆注入（幂等去重 + cap）
                        └──▶ 网关：预热校验 → 转换 → 限流 → 后端 → usage 归一 → 埋点
```

**为什么是这个形态**：`previous_response_id` 要求转换层必须带存储（有状态网关），
这是被协议特性锁定的，不是过度设计。3 对 adapter 替代 6 方向两两互转。

## 二、请求生命周期（一条转换请求经过的代码路径）

```
do_POST（路由 /v1/{source}/to/{target}，未知协议 → 400）
  → is_warmup? → validate_warmup（对**转换后**报文，四类冲突 → 400）
  → convert()
      → src.to_ir(payload)                     # 源协议 → IR（未知顶层字段进降级记录）
      → STORE.resolve_previous(prev_id)?       # 有状态：重放历史
      → STORE.get_or_create(session_key)       # anthropic / openai_response 目标：建会话
      → inject_memories(req, session)          # 记忆注入（重放后、渲染前）
      → dst.from_ir(req, ctx)                  # IR → 目标协议（Anthropic 侧打 cache_control）
  → _GATE 限流（Semaphore，默认并发 2）
  → post_json()                                # 按 BACKEND_PATH 转发（mock 与真实同路径）
  → _usage_for()                               # usage 三口径归一
  → METRICS.record_turn(kind=normal|warmup)    # 5 项埋点落 JSONL（预热轮单列）
  → STORE.append(本轮新消息 + assistant 回复)   # 历史落库（多轮链路的地基）
  → STORE.record_response()                    # 登记响应 id
  → openai_response 源：响应 id 覆写为网关可解析的 resp_xxx
  → 响应 + _bridge 元信息返回
```

## 三、逐文件功能清单

### src/ir/ —— IR 契约层（冻结 v0）

| 文件 | 行数 | 功能 |
| ---- | --- | ---- |
| `model.py` | 138 | 三层数据模型。**L0**：`Block`（text/tool_use/tool_result/thinking + `cache_breakpoint` 标记 + `extra` 降级证据位）、`Message`；**L1**：`IRRequest`、`IRUsage`（`total_input` 按 Anthropic 口径求和）、`IRResponse`（`is_warmup` 标记）；**L2**：`SessionContext`（4 断点布局开关）。铁律：未知字段进 `extra`，绝不静默丢弃 |

### src/adapters/ —— 协议适配层（3 对 = 6 方向）

| 文件 | 行数 | 功能 |
| ---- | --- | ---- |
| `base.py` | 45 | `Dropped` 降级记录；**`record_unknown()`：三个 adapter 统一的顶层字段收口**——凡未消费的顶层字段一律进降级清单（实验第④项暴露的数据源） |
| `chat.py` | 116 | OpenAI Chat ↔ IR。system 消息上提 L1.system；顶层 `system` 等非规范字段显式接管 + 记录；`usage_from_chat()`：`prompt_tokens` 含缓存、`cached_tokens` 子集 → 归一 |
| `response.py` | 105 | OpenAI Responses ↔ IR。`previous_response_id` 透传到 `extra`（状态层取用）；**`from_ir` 不再回传 prev_id 给上游**（历史已由网关重放，再传会二次拼历史/404）；`usage_from_response()` 归一 |
| `anthropic.py` | 157 | Anthropic ↔ IR。**`from_ir` 按断点布局打 cache_control**：tools 末 / system 末 / **历史静态段末（v1.2 修复落地）** 3 固定 + 滚动尾部 1 个（用满官方 4 上限）；`_render_block` 无映射块显式进降级记录；thinking / tool_choice 经 `extra` 无损透传（anthropic→anthropic 同协议无损）；`usage_from_anthropic()` 归一 |

### src/state/ —— 状态层

| 文件 | 行数 | 功能 |
| ---- | --- | ---- |
| `session_config.py` | 92 | 可切换 Session 边界配置（对齐方案 3.9.1）。3 开关 + `session_memory_cap`；`load()` 读 `config/session.json`；`build_prefix()` 三策略（**v1.2 修复：sliding_window n≤0 必须返回空窗口**）；`snapshot()` 供实验报告绑死 |
| `store.py` | 217 | **SQLite 持久化会话表**。`get_or_create` / `resolve_previous` / `record_response`（先读最新行防旧快照覆盖）/ `update_meta` / `append` / `replay` / `evict_expired`。`PB_DB` 可换路径，`:memory:` 供测试 |

### src/gateway/ —— 网关主进程

| 文件 | 行数 | 功能 |
| ---- | --- | ---- |
| `server.py` | 363 | 见下方「网关关键函数」 |
| `sse.py` | 150 | **v1.6 新增**：SSE 逐块流式转换（chat 客户端 ← anthropic 上游）。`parse_sse_lines` 行流分帧；`AnthropicToChatStream` 事件→Chat chunk：text_delta 即时下发、工具参数缓冲到 content_block_stop（既定折损）、usage 流尾合并、`synthetic_response()` 复用 `assistant_from_upstream` 落库 |

**网关关键函数**：

| 函数 | 功能 |
| ---- | ---- |
| `is_warmup` / `validate_warmup` | 识别 `max_tokens:0` 预热；对**转换后的目标协议报文**做四类冲突拒绝 → 400（方案 3.7） |
| `inject_memories` | 记忆注入：文本幂等去重；cap 三级生效（会话 meta > 全局 > 0 不限）；`extra.injected_memory` 标记保证跨轮幂等 |
| `convert` | 转换主管线，返回 6 元组（含 `replayed` 重放切片，供埋点区分重放代价与本轮输入） |
| `_assistant_message` | **v1.2 新增**：上游响应 → IR assistant 文本消息（状态层重放的原料） |
| `post_json` | 后端转发：Bearer + x-api-key 双头、`anthropic-version` 头、浏览器 UA、`PB_DIRECT=1` 绕系统代理 |
| `do_POST` | 路由（未知协议 → 400）+ 管理口 `/v1/admin/session/meta` + 预热拦截 + 限流 + 埋点 + **历史落库** + 响应 id 覆写（Responses 源）+ `_bridge` 元信息 |

**环境变量**：`PB_BACKEND` / `PB_API_KEY` / `PB_DIRECT` / `PB_PORT` / `PB_METRICS` / `PB_DB` / `PB_CONCURRENCY` / `PB_ANTHROPIC_VERSION`

### src/observability/ —— 埋点

| 文件 | 行数 | 功能 |
| ---- | --- | ---- |
| `metrics.py` | 92 | `TurnMetrics`：**方案 3.3 五项暴露** + `kind`（normal/warmup 单列）；`hit_rate()` **默认剔除预热轮**（修复北极星分母稀释）；`MetricsLog` 加锁追加 JSONL（多线程写不坏行） |

### src/warmup/ —— 预热

| 文件 | 行数 | 功能 |
| ---- | --- | ---- |
| `prewarm.py` | 80 | `build_warmup_request()`：断点打 system 不打占位消息；`validate_warmup()`（类型防御）；`parse_warmup_response()` / `is_warmup_response()`："畸形"响应单独路径 |

### src/webui/ —— 弹网页（安全骨架 + 记忆编排界面）

| 文件 | 行数 | 功能 |
| ---- | --- | ---- |
| `server.py` | 180 | 本地仪表盘：绑 127.0.0.1 / 随机端口 / Host 头校验 / 一次性 URL token / `_mask()` 脱敏 / 90s 超时兜底。**v1.3 补齐编排界面**：记忆勾选/裁剪（`save_callback` 回写状态层）+ 缓存前缀可视化（4 断点布局） |

### experiments/ —— 实验层

| 文件 | 行数 | 功能 |
| ---- | --- | ---- |
| `run_experiments.py` | 85 | 三组对照（更新频率/位置/粒度）框架；配置快照与数据绑死（纪律①）；`--dry-run` 离线跑。**等真实 API 填数据** |

### tools/ —— 工具

| 文件 | 行数 | 功能 |
| ---- | --- | ---- |
| `mock_backend.py` | 194 | 离线顶替真实端点。**v1.2 按端点路径返回三种协议各自形状**（/v1/messages、/responses、/chat/completions），mock 与真实后端走同一条转发路径；有状态缓存模拟（前缀哈希记账）；支持 `max_tokens:0` 预热形态与 SSE |
| `verify_cache.py` | 152 | **验站脚本**：唯一随机 run_id 前缀两步验证（预热看 creation → 复发看 read）；PASS/PARTIAL/FAIL；`max_tokens:0` 被拒自动降级 1 |
| `smoke_e2e.py` | 220 | 端到端冒烟——真起 mock + 网关两个子进程，跑 6 组 18 项断言（直通/跨协议/多轮链/预热/非法路由/**SSE 逐块流式**），退出码 0/1 |

### tests/ —— 测试（53 项全过）

| 测试类 | 覆盖 |
| ------ | ---- |
| `TestAdapterRoundTrip` | Chat→IR→Anthropic 往返、断点数 ≤4 且 ≥3、usage 三口径归一 |
| `TestStateLayer` | prev_id 重放、重放三策略、TTL 淘汰、惰性淘汰节流（v1.5）、SQLite 重启不丢 |
| `TestWarmup` | 预热构造、冲突校验、"畸形"响应解析 |
| `TestSessionConfig` | 配置加载/热切换/快照 |
| `TestGatewayPolicies` | 预热四类拒绝、记忆注入去重+cap+cap 三级生效 |
| `TestAuditFindings` | **v1.2 新增**：9 条自查回归测试（见第六节），每条对应一个修过的 bug |
| `TestStatefulChainE2E` | **v1.2 新增**：真起网关+mock 的多轮 prev_id 链路（0→2→4 条重放累积）、预热轮不进响应链 |
| `TestCrossProtocolToolArgs` | **v1.4 新增**：跨协议工具参数守恒（bug #10 回归） |
| `TestThinkingSignature` | **v1.4 新增**：thinking signature / redacted 往返（bug #11 回归） |
| `TestToolIdMap` | **v1.4 新增**：工具 ID 双向映射（含 Anthropic→Chat→Anthropic 还原、并发分叉不串号） |
| `TestAssistantFromUpstream` | **v1.4 新增**：状态层重放保留 thinking / tool_use 结构化块 |
| `TestPropertyRoundTrip` | **v1.4 新增**：property-based 往返（440 随机用例、固定种子可复现）：断点恒在 [3,4] / 未知字段必降级 / 工具参数守恒 / signature 守恒 |
| `TestSseConversion` | **v1.6 新增**（7 项）：SSE 分帧（注释行/半帧容错）、text_delta 即时下发、工具参数缓冲到块结束、stop_reason 映射、usage 流尾合并 + 落库复用、上游错误帧 |

## 四、已验证记录

| 验证 | 结果 | 日期 |
| ---- | ---- | ---- |
| 53 项单测（`-W error::ResourceWarning` 下零警告，含第三轮自查回归 + property-based + 惰性淘汰 + SSE 转换） | ✅ 全过 | 2026-09-09 |
| 18 项端到端冒烟（子进程级，`tools/smoke_e2e.py`，含 SSE 逐块到达实测 30.7ms） | ✅ 全过 | 2026-09-09 |
| 多轮 previous_response_id 链路（0→2→4 条重放） | ✅ 修复后通过 | 2026-09-02 |
| Groq 真实链路（Chat 直通 + Responses→Chat + usage 归一 + 埋点） | ✅ | 2026-09-01 |
| 实验框架 dry-run | ✅ | 2026-09-02 |
| Anthropic 端点缓存透传 + 命中率埋点对账（E29） | ✅ 两轮 cc=5978→cr=5978，埋点与 usage 逐字段一致 | 2026-09-09 |

## 五、2026-09-02 两轮代码自查（9 个真实 bug，全部修复）

> 起因：进度很快，担心有暗坑，主动做了两轮逐文件复查。
> 结论：担心是对的——其中 3 个是**会让实验结论站不住**的严重 bug。
> 每个 bug 都配了回归测试（`TestAuditFindings` / `TestStatefulChainE2E`）。

### 第一批：adapter 与转换正确性

| # | 严重度 | 问题 | 修复 |
| - | ------ | ---- | ---- |
| 1 | 中 | **第 4 个固定断点 `bp_after_history_static` 是死代码**：定义了但从未落地，历史静态段实际不缓存，断点只用 3 个 | `anthropic.from_ir` 补落地逻辑（历史切片末条末块） |
| 2 | 中 | 无映射内容块（image 等）**静默丢弃**，不进降级记录——违背「绝不静默丢弃」铁律 | 抽 `_render_block()`，无映射一律 `dropped.add` |
| 3 | 中 | 预热拒绝校验对的是**源协议** payload：thinking/response_format 是目标专属字段，源 payload 里永远没有 → 校验形同虚设 | 移到 `convert()` 之后，校验渲染后的目标报文 |
| 4 | 中 | 上游 4xx/5xx 被吞，客户端拿到网关 500，无法区分「网关挂了」还是「上游拒了」 | `HTTPError` 捕获，状态码原样透出 + 错误详情截断带回 |

### 第二批：埋点与有状态链路（3 个严重）

| # | 严重度 | 问题 | 修复 |
| - | ------ | ---- | ---- |
| 5 | **严重** | **网关从不写历史**：只有 `record_response` 没有 `append` → 会话历史恒为空 → 多轮重放永远是空，整个状态层空转。「为什么必须有状态层」这个论据在代码里不成立 | `do_POST` 补 `STORE.append(本轮新消息 + assistant 回复)` |
| 6 | **严重** | **openai_response 首轮不建会话**：只有 anthropic 目标或已带 prev_id 才建 → 首轮 response_id 根本没登记 → 多轮链路第一轮就断 | 会话创建条件扩为「anthropic / openai_response 目标 / 已解析会话」 |
| 7 | **严重** | **previous_response_id 回传上游**：历史已由网关重放，再把 id 透给上游 → 真 OpenAI 会二次拼历史（上下文重复、token 翻倍）；传网关自生成的 resp_xxx 则上游直接 404 | `from_ir` 不再回传；响应 id 由网关覆写为自己可解析的 resp_xxx（上游原始 id 留在 `_bridge_upstream_id`） |
| 8 | 高 | 重放指标口径错：`replayed` 统计了 `req.messages` 全部（含本轮新消息），「重放代价」被系统性高估且随轮次增长——**这是实验 5 项暴露之一** | `convert()` 返回重放切片，只统计切片 |
| 9 | 高 | 预热轮混进命中率分母：预热轮 cache_read 恒为 0，算进 `hit_rate()` 分母会按预热占比稀释北极星，且不同批次不可比 | `TurnMetrics` 加 `kind`，`hit_rate()` 默认剔除预热轮；预热轮不登记响应链 |

### 顺带修复（不配单独测试，各一处）

- 滑动窗口 `n=0` 时 `history[-0:]` 静默退化成 full（实验组间差异消失且极难察觉）→ 显式返回空窗口（有测试）
- 三个 adapter 各自静默吞顶层字段 → 统一 `base.record_unknown()` 收口（有测试）
- Anthropic thinking / tool_choice 无 IR 字段被丢 → 经 `extra` 透传，anthropic→anthropic 无损（有测试）
- 未知协议路由抛 `KeyError` → 请求线程崩、连接被掐断 → 显式 400
- mock 特判转发路径（离线跑的和上线跑的不是同一条路径）→ 删除特判，mock 按端点返回三种协议形状
- `MetricsLog` 无锁追加 JSONL（ThreadingHTTPServer 并发写可能坏行）→ 加锁
- `injected_chars` 按尾部切片假设 → 改按 `extra` 标记统计
- `prewarm.validate_warmup` 对非 dict 的 thinking 字段会崩 → 类型防御

### 第三批：2026-09-09 评审前复查（v1.4，2 个真实 bug + 2 项能力补齐）

> 起因：评审前对三个 adapter 与状态层做了第三轮逐文件复查。
> 结论：前两轮的自查范围（网关/埋点/状态链）之外，**adapter 的跨协议路径**还藏着两个会让「工具全链路」结论站不住的 bug。

| # | 严重度 | 问题 | 修复 |
| - | ------ | ---- | ---- |
| 10 | **严重** | **跨协议工具参数丢失**：OpenAI 系 `arguments` 是 JSON 字符串，chat/response 的 to_ir 只塞 `extra["arguments"]`、`tool_input` 恒为 None → 转到 Anthropic 全部渲染成空 `input {}`。反向 anthropic→chat 的 `arguments` 也退化成 `"{}"`——工具全链路跨协议双向断 | `base.parse_arguments()` 解析进 `tool_input`；from_ir 优先用 extra 原始串（同协议字节无损）、否则从 `tool_input` 序列化。`TestCrossProtocolToolArgs` 4 项 |
| 11 | **严重** | **thinking `signature` / `redacted_thinking` 在 to_ir 被丢**：Anthropic 要求下一轮原样回传 signature、redacted 逐字节透传，丢则多轮思考链第二轮必 400——恰好命中问题清单组4#4/#5 两条 A 级 | signature / redacted data 进 `extra` 保留，`_render_block` 原样渲染回。`TestThinkingSignature` 2 项 |

同批能力补齐（原 LIMITATIONS #2/#3，均配回归测试）：

- **工具调用 ID 双向持久映射**（问题清单组4#3）：`src/state/idmap.py`——会话级 canonical ↔ 各协议外部形式，SQLite 持久、铸造稳定、会话间隔离；网关 to_ir 后归一、from_ir 前翻译、落库前翻回 canonical。`TestToolIdMap` 5 项（含 Anthropic→Chat→Anthropic 还原、并发分叉不串号）
- **状态层重放保留结构化块**：`base.assistant_from_upstream()` 保留 thinking（signature/redacted）与 tool_use（含解析后 tool_input）入历史，取代只取文本的 `_assistant_message`。`TestAssistantFromUpstream` 4 项

测试 25 → **40 项全过** + 11 项端到端冒烟全过。（其后的 property-based 提交再增至 **44 项**，见 CHANGELOG 2026-09-09 末节。）

## 六、已知缺口（修复后剩余）

> 2026-09-09 起，缺口的最新状态、四要素（影响半径/触发条件/绕过方式/后续路线）与结论隔离证明统一维护在 **`docs/LIMITATIONS.md`**；下表为评审时点快照。

| # | 缺口 | 影响 | 计划 |
| - | ---- | ---- | ---- |
| 1 | **SSE 是假透传**：整个响应读完再处理，不是逐块流式 | 流式体验与首 token 延迟 | P2 |
| 2 | ~~webui 只有安全骨架，无实际编排界面~~ | 方案 3.7 弹网页承诺未兑现 | ✅ v1.3 已补：记忆勾选/裁剪 + 缓存前缀可视化 + `save_callback` 回写 |
| 3 | replayed/injected 按**字符数**统计，非 token | 埋点数值偏大但趋势可用 | 接 `count_tokens` 校准 |
| 4 | 网关无鉴权（绑 127.0.0.1 兜底）；管理口 `/v1/admin/*` 同样无鉴权 | 仅本地用安全；暴露到网络即风险 | 交付文档声明 + 需要时加 token |
| 5 | 三组实验无真实数据 | ✅ 已补：exp1–19 + 覆盖实验 E20–E28 全量实测，量化取舍文档见 `docs/experiment-results.md` | 已闭环 |
| 6 | 会话历史无上限增长（replay_from=full 时每轮全量重放） | 长对话下重放代价线性增长——本身是实验变量，但生产形态需 cap | 实验里用 sliding_window 组对比，文档说明 |
| 7 | mock 的缓存模拟比真实粗糙（无 TTL、无最小长度门槛、只认文本块） | 离线验证只能证明「链路通」，不能证明「真实 API 会缓存」 | 已有 `verify_cache.py` 验真实站点 |
