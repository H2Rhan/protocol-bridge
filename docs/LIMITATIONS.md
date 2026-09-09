# 已知限制（LIMITATIONS）

> 2026-09-09 · 本文档是本组「边界自觉」的执行：凡未闭环项，一律写清**影响半径 / 触发条件 / 绕过方式 / 后续路线**四要素。
> 原则（问题清单 05 节）：关键不是少做，而是把「没做」写成显式的已知限制。
> 凡本文档与 README / 能力矩阵的状态表述不一致处，**以本文档为准**。

## 一、协议转换链路

### #1 SSE 逐块流式转换（v1.6 部分闭环：chat 客户端 ← anthropic 上游）

> ◐ **v1.6 部分闭环**（2026-09-09）：`openai_chat` 客户端 ← `anthropic` 上游方向已实现**逐块流式**——`src/gateway/sse.py` 把 Anthropic 事件逐块翻成 Chat chunk 即时下发（冒烟实测首尾帧间隔 30.7ms，证明非整读补吐）；text_delta 即时下发、工具参数按既定折损缓冲到 `content_block_stop`、usage 流尾合并后照常进 5 项埋点、历史落库复用 `assistant_from_upstream`（流式与非流式同一路径）。回归测试 `TestSseConversion` 7 项 + 冒烟第 6 组 7 项。**v1.7 补充**（2026-09-09）：同协议直通流式（chat←chat / anthropic←anthropic）已闭环——字节原样透传 + 旁路收集流尾汇总（`tee_lines` / `ChatStreamCollector`）；**未实现的流式方向（response 源三维寻址、anthropic←chat 等）改为显式 501**（此前是 `post_json` 把 SSE 当 JSON 解析炸成语焉不详的 502）。**仍未闭环**：response 源、anthropic←chat 跨协议方向的流式转换；流式轮的 dropped 清单对客户端不可见（埋点中 degradation 仍如实记录）。以下原文留档，未闭环范围以四要素为准。

- **影响半径**：流式体验与首 token 延迟（TTFB）。功能正确性不受影响——响应内容完整、usage 归一与埋点照常工作。
- **触发条件**：客户端请求 `stream:true` 经过网关。当前网关 `post_json` 整读响应后一次性返回；跨协议的事件模型转换（Chat `delta` / Responses 三维寻址 / Anthropic 块生命周期）未实现。
- **绕过方式**：① 非流式调用网关（缓存命中率不受影响——E21 已实测流式与非流式命中率一致、缓存跨传输模式共享）；② 需要流式时直连端点。
- **后续路线**：按 IR 块生命周期定义统一事件模型，先做 Anthropic→Chat 单方向逐块转换；`input_json_delta` 非完整 JSON 片段需缓冲到完整再发（长工具参数退化为非流式，属无法回避的折损，将写进限制）。

### #2 工具调用 ID 无双向持久映射表（透传）

> ✅ **v1.4 已修复**（2026-09-09）：新增 `src/state/idmap.py`（会话级 canonical ↔ 各协议外部形式，SQLite 持久、铸造稳定、并发分叉隔离），网关 `convert()` 在 to_ir 后归一、from_ir 前翻译，落库前翻回 canonical。回归测试 `TestToolIdMap`（5 项，含 Anthropic→Chat→Anthropic 还原）。以下原文留档。

- **影响半径**：跨协议多轮工具链的 ID 一致性。当前 `tool_id` 经 IR 原样透传（`toolu_*` / `call_*` / `call_id` 直通），单轮与回放自身历史的场景工作正常。
- **触发条件**：一轮协议 A 产生的工具调用 ID，在后续轮次以协议 B 回传给生成方 A 时；以及并行调用重名、分叉重放场景。
- **绕过方式**：同协议往返（如 Anthropic→Anthropic）ID 原样有效；跨协议场景下多数 OpenAI 兼容端点接受任意字符串 ID。
- **后续路线**：状态层增加 `id_map`（会话内双向映射 + TTL 失效），处理并行重名；配回归测试后从本清单划掉。

### #3 状态层重放只保留文本块

> ✅ **v1.4 已修复**（2026-09-09）：`assistant_from_upstream()`（`src/adapters/base.py`）保留 thinking（含 signature / redacted data）与 tool_use（含解析后 tool_input）入历史；同批修复 anthropic adapter 的 signature 往返与跨协议工具参数丢失（第三轮自查 2 个 bug）。回归测试 `TestAssistantFromUpstream` / `TestThinkingSignature` / `TestCrossProtocolToolArgs`。以下原文留档。

- **影响半径**：多轮工具链 / 多轮 thinking 链。`_assistant_message` 只提取文本写入会话历史——thinking 块（含 signature）与 tool_use/tool_result 块不进重放。
- **触发条件**：开启 thinking 的多轮对话第二轮起（Anthropic 要求原样回传 thinking + signature，缺失会 400）；工具结果需跨轮引用的场景。
- **绕过方式**：无状态全量重放（客户端自带完整历史）不经状态层，不受影响；单轮工具链不受影响。
- **后续路线**：`_assistant_message` 保留结构化块（thinking/tool_use 原样入史），replay 路径按 IR 块渲染；与 #2 的 id_map 同日落地。

### #4 adaptive thinking / `display:"omitted"` 未处理

- **影响半径**：新模型（adaptive thinking + `output_config.effort`）的预热校验与块处理。`validate_warmup` 只识别旧版 `thinking.type=="enabled"`。
- **触发条件**：对新模型发送 `max_tokens:0` 预热且带新版推理配置——校验漏判，上游返回 400 而非网关提前拒绝；`thinking.display:"omitted"` 块（文本为空但 signature 必须保留）可能被误判为无效块。
- **绕过方式**：预热请求不带 thinking（官方本就禁止）；正式请求经 `extra` 透传，同协议无损。
- **后续路线**：校验逻辑按模型代次分派；omitted 块显式保留进 IR。

### #5 角色交替规整（合并同角色 / 占位 user）未实现

- **影响半径**：Anthropic 要求 user/assistant 严格交替且首轮为 user；外部请求若含连续同角色消息，网关不做规整。
- **触发条件**：客户端连发两条同角色消息、或以 assistant 开头。状态层重放的历史自然交替，不触发。
- **绕过方式**：多数兼容端点对交替违规容错；客户端自行规整。
- **后续路线**：adapter 层加合并策略（注意：插入占位 user turn 本身是语义污染，将显式进降级记录）。

### #6 错误语义未归一（状态码透出但 code↔type 未映射）

- **影响半径**：OpenAI `error.code` 与 Anthropic `error.type` 的语义体系不同（如 `context_length_exceeded` vs `invalid_request_error`），当前网关透出原始状态码与错误详情（截断 500 字符），但不做语义映射。
- **触发条件**：上游 4xx/5xx。客户端拿到的是上游原生错误体，不会是网关 500（v1.2 已修复吞错）。
- **绕过方式**：客户端按上游协议解析；`_bridge` 元信息保留上游状态码。
- **后续路线**：错误映射表（含重试语义对齐：`overloaded_error` ↔ `rate_limit_exceeded`）。

## 二、度量与实验

### #7 replayed / injected 按字符数统计（未接 count_tokens）

- **影响半径**：埋点中「注入 token / 重放 token」两项的绝对数值偏大（字符 ≠ token），**趋势与组间对比有效**。
- **触发条件**：读 metrics.jsonl 的 `injected` / `replayed` 字段。
- **绕过方式**：按实测系数折算（sonnet-4.6 约 5.1 字符/token）；命中率、cache_read/creation 等核心字段来自上游真实 usage，不受影响（E29 已对账逐字段一致）。
- **后续路线**：接 `/v1/messages/count_tokens` 校准（代价：每轮多一次 RTT，需评估）。

### #8 缓存断点往返衰减未量化

- **影响半径**：Anthropic→Chat→Anthropic 往返后断点信息丢失（Chat 无断点概念），命中率随往返次数衰减——存在但未量化。
- **触发条件**：跨协议多跳往返。
- **绕过方式**：单向链路（生产主形态）不受影响；IR 的 `cache_breakpoint` 标记在同协议往返保留。
- **后续路线**：往返衰减实验（N 次往返后命中率曲线）。

### #9 opus-5 缓存行为未决（端点异常，非「不缓存」）

- **影响半径**：最小可缓存长度表中 opus-5 = 512 一格只有官方文档依据，无实测。
- **触发条件**：对 opus-5 发大 token 请求——端点返回 `input_tokens=0`（请求未正常处理，E34 四档复测均如此）。
- **绕过方式**：无。结论表述必须为「端点异常、待复测」，**禁止写成「opus-5 不缓存」**（E34 教训：先核对 `input_tokens` 是否正常，再下缓存结论）。
- **后续路线**：换可用端点复测；或等端点修复。

## 三、工程与运维

### #10 网关与管理口无鉴权

- **影响半径**：`/v1/*` 与 `/v1/admin/session/meta` 均无鉴权。绑定 `127.0.0.1` 是唯一防线——本地使用安全，**暴露到网络即为事故**（管理口可任意写入会话记忆）。
- **触发条件**：`PB_PORT` 绑定到非回环地址，或同机恶意进程。
- **绕过方式**：不要改绑定地址；webui 侧已做 Host 头校验 + 一次性 URL token + 脱敏（记忆内容属敏感信息，loopback ≠ 天然安全）。
- **后续路线**：需要对外时加 Bearer token（网关侧已预留 `PB_API_KEY` 转发，鉴权逻辑是对称的）。

### #11 TTL 淘汰无后台触发器

> ✅ **v1.5 已修复**（2026-09-09）：新增 `SessionStore.maybe_evict()` 惰性淘汰——读写路径（`get_or_create`）顺手触发，按 `_EVICT_INTERVAL`（60s）节流避免每请求全表扫描，无需后台线程；在锁外调用规避非可重入锁死锁。回归测试 `test_lazy_eviction_throttled`（节流窗口）/ `test_get_or_create_triggers_lazy_eviction`（流量驱动）。以下原文留档。

- **影响半径**：`evict_expired` 已实现且测试覆盖，但网关主进程没有定时调用——会话表随运行时间无界增长（`replay_from=full` 时每轮全量重放，长对话重放代价线性增长）。
- **触发条件**：长时间运行的网关进程。
- **绕过方式**：重启进程不丢会话（SQLite 持久化），可定期人工调用淘汰；实验场景会话数受控。
- **后续路线**：网关加定时清扫（或惰性淘汰：读写时顺手淘汰）；生产形态需配合 `sliding_window` 重放策略 cap 历史。

### #12 往返一致性为例举式测试（非 property-based），有损清单待自动生成

> ✅ **已闭环**（2026-09-09，PR #8）：`TestPropertyRoundTrip` 以 stdlib 实现 property-based 往返测试（440 随机用例、固定种子可复现，断言断点恒在 [3,4] / 未知字段必降级 / 工具参数守恒 / signature 守恒四条性质）；`docs/lossy-fields.md` 已从三个 adapter 实码推导生成（含两处诚实标注的清单盲区）。以下原文留档。

- **影响半径**：`TestAdapterRoundTrip` 覆盖录制样例的代表性路径，不做随机化性质测试；已知有损字段清单当前依赖运行时 dropped 记录，无独立文档。
- **触发条件**：边缘字段组合（罕见的块类型/参数搭配）。
- **绕过方式**：铁律兜底——未知字段一律进 `extra` + dropped 降级记录，绝不静默丢弃；`_bridge.dropped` 每轮可见。
- **后续路线**：从 dropped 记录生成 `docs/lossy-fields.md`；引入 hypothesis 做 IR 层 property-based 往返测试。

---

## 结论隔离证明（为什么 34 组实验结论不依赖上述未闭环项）

1. **主数字 7.3×（LOCKED vs DYNAMIC）**：实验脚本**直连端点**（`experiments/rich_experiments*.py`），不经过网关的 SSE 路径（#1）、不经状态层重放（#3）、不含工具调用 ID（#2）。命中率字段为上游真实返回。
2. **阈值表（haiku=4096 / sonnet≈1024）**：同样直连端点；判定依据 `cache_creation/cache_read` 字段，与字符统计口径（#7）无关。
3. **05B 救援（E27：透传 0% vs 转 Messages 99.85%）**：比较的是两种**后端路由**，两端点均不经过 #1/#2/#3 涉及的代码路径。
4. **E29 埋点对账**：验证的是 metrics 忠实记录上游 usage——它证明的是「观测可信」，而 #7 的字符口径问题已在该节明示，不影响命中率北极星（分母分子均来自上游 usage）。
5. 未闭环项中唯一触及实验解读的是 #9（opus-5），其处理方式是**不下结论**而非下错结论。
