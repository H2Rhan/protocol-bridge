# CHANGELOG

按日期复述演进（问题清单冻结于 09-01，仓库 09-05 建立；v1.2 为首个全量快照，其后一律走 PR 流）。

## 2026-09-01 · 契约冻结（v0）

- 问题全景分析定稿：31 条问题清单 + A/B/C 分级 + 范围切割（主线 / 非目标）
- **IR v0 三层契约冻结**（L0 内容块 / L1 规范请求 / L2 会话缓存上下文），05A/05B 据此并行
- 缓存最小 token 阈值、计费与 TTL 不对称逐模型核对官方文档（docs.anthropic.com 与 platform.claude.com 一致）

## 2026-09-05 · v1.2 全量实现（PR #1）

- IR 契约 / 3 对 adapter / 状态层（SQLite + TTL + prev_id 反查）/ 网关（预热校验 + 记忆注入 + 限流 + usage 归一 + 历史落库）/ 5 项埋点 / 实验框架 / 工具链
- **两轮代码自查修复 9 项缺陷**（3 严重：状态层空转 / 多轮链断 / prev_id 回传上游），各配回归测试 → 抗漂移机制实战验证
- 25 项单测 + 11 项端到端冒烟全过

## 2026-09-08 · 实验交付（PR #2）

- 缓存命中率全量实测 **exp1–19 + E20–E28**（Anthropic 兼容端点真实调用）
- 主数字：LOCKED vs DYNAMIC ≈ **7.3×**（跨模型一致）；E27「05B 救援」：Responses 透传 0% → 转 Messages 99.85%
- 「20-block 回看窗口」**决定性证伪**（60 块 100% 命中），从执行方案删除

## 2026-09-09 · 覆盖补全 + 可观测对账 + 弹网页（PR #3 / #4 / #5）

- E30–E34 进阶实验：并发冷启动重复写入（E32，single-flight 必要性）/ 前缀字节边界 / 命中快 ~1.65s（E33）/ opus-5 悬案定性为「端点异常」（E34）/ 23711 token 仍全缓存
- **E29 埋点对账**：起网关连真实端点，埋点与 API usage 逐字段一致
- 弹网页编排界面：记忆勾选/裁剪 + 缓存前缀可视化 + 回写状态层
- 修复网关后端路径映射（`PB_CHAT_PATH` / `PB_RESPONSE_PATH` / `PB_ANTHROPIC_PATH` 可覆盖）

## 2026-09-09 · 文档一致性修订（本提交）

- 全库清除「20-block 回看窗口」残留表述（ir-schema / capability-matrix / 实验脚本注释），统一指向证伪结论
- capability-matrix v0.2：状态列按实码校准（prev_id / 断点布局已实现；SSE 更正为「整读透传」）；补「手动断点 vs 自动缓存」决策记录
- ir-schema 补「变更记录」（冻结 = L0/L1 语义层 + 修订必配回归测试的抗漂移机制）
- experiment-results 补「第〇节 · 证伪条件预登记」（实验编号 = 假设编号，回链问题清单条目）
- 新增 `docs/LIMITATIONS.md`：12 项未闭环/边界项四要素（影响半径 / 触发条件 / 绕过方式 / 后续路线）+ 结论隔离证明
- README：状态表加「证据」列；SSE 措辞更正；新增「与上游 TencentDB-Agent-Memory 的关系」

## 2026-09-09 · v1.4 代码补强：第三轮自查 + 工具 ID 映射 + 结构化重放（本提交）

- **第三轮自查（评审前逐文件复查）再发现 2 个真实 bug，各配回归测试**：
  - 跨协议工具参数丢失：OpenAI 系 `arguments` 是 JSON 字符串，to_ir 只塞 `extra` 不解析，`tool_input` 恒为 None → 转到 Anthropic 全部渲染成空 `input {}`（`TestCrossProtocolToolArgs` 4 项）
  - thinking `signature` 与 `redacted_thinking` 在 to_ir 被丢 → 多轮思考链下一轮必 400（`TestThinkingSignature` 2 项）
- **工具调用 ID 双向持久映射**（问题清单组4#3，原 LIMITATIONS #2）：`src/state/idmap.py` 会话级 canonical ↔ 各协议外部形式，SQLite 持久、铸造稳定（前缀不抖动）、会话间隔离（并发分叉不串号）；网关 to_ir 后归一、from_ir 前翻译、落库前翻回 canonical（`TestToolIdMap` 5 项）
- **状态层重放保留结构化块**（原 LIMITATIONS #3）：`assistant_from_upstream()` 保留 thinking（signature / redacted）与 tool_use（含解析后 tool_input）入历史，多轮思考链/工具链不再断（`TestAssistantFromUpstream` 4 项）
- 测试 25 → **40 项全过** + 11 项端到端冒烟全过（Python 3.13）

## 2026-09-09 · property-based 测试 + 有损清单 + CI（本提交）

- **`TestPropertyRoundTrip`**（问题清单组5#10）：stdlib 实现、固定种子可复现，440 个随机用例断言四条性质——断点恒在 [3,4] / 未知顶层字段必降级 / 工具参数跨协议守恒 / thinking signature 往返守恒（参数丢失 bug 正是这类性质被抓出的）。测试 40 → **44 项全过**
- **新增 `docs/lossy-fields.md`**（问题清单主线交付物）：从三个 adapter 实码推导的有损字段清单（块级 / 参数级 / usage 归一级），含两处诚实标注的清单盲区（工具定义内部字段无 dropped 记录、多模态/流式未覆盖）
- **GitHub Actions CI**：`.github/workflows/test.yml`，Python 3.11/3.12/3.13 矩阵跑单测 + 冒烟 + dry-run（零依赖，无需装包）；README 加 badge 行
- README 修正一处悬空引用（原指向方案 v3.1 已不存在的节号，改指实验文档方法学节）
- 文档口径：实验文档成本列改为**相对倍数**（7.35× / 10.2× / 7.9×），不再出现绝对金额；ir-schema 移除签字栏（冻结约束以「双方确认 + 变更记录」为准）

## 2026-09-09 · v1.5 运维闭环：TTL 惰性淘汰 + 实验数据入库（本提交）

- **TTL 惰性淘汰**（LIMITATIONS #11 闭环）：`SessionStore.maybe_evict()`——读写路径（`get_or_create`）顺手触发淘汰，60s 节流避免每请求全表扫描；在锁外调用规避非可重入锁死锁。回归测试 2 项（节流窗口 / 流量驱动），测试 44 → **46 项全过** + 11 项冒烟全过
- **实验原始数据入库**：`data/experiments/` 19 个文件（34 组实测逐轮 jsonl + 汇总 csv）+ `data/README.md` 溯源表——实验文档每个数字可回查到具体文件具体行；`.gitignore` 改为只忽略运行时产物、放行精选数据目录
- LIMITATIONS #12 补标已闭环（property-based + lossy-fields 已于 PR #8 落地，原标记滞后）
- README：目录补 data/、已知限制行标注 3 项已闭环、单测数 40 → 46；REVIEW.md 测试口径统一为 46 项

## 2026-09-09 · v1.6 SSE 逐块流式（LIMITATIONS #1 部分闭环，本提交）

- **`src/gateway/sse.py`**：`openai_chat` 客户端 ← `anthropic` 上游方向的逐块流式转换。text_delta 即时翻成 Chat chunk 下发（首 token 延迟与直连一致）；`input_json_delta` 按既定折损缓冲到 `content_block_stop` 一次性发（非完整 JSON 片段无法流式透传）；usage 在 message_start/message_delta 分次到达、流尾合并照常进 5 项埋点；`synthetic_response()` 复用 `assistant_from_upstream`——流式与非流式落库/埋点同一条代码路径，不另造语义
- 网关：抽 `_backend_headers` / `_backend_opener` / `open_stream`，do_POST 尾部逻辑收敛为 `finalize_turn()`（非流式与流式共用）；流式请求同样占 `_GATE` 并发名额
- 设计验证：urllib 响应对象按行增量吐数据（实测逐行到达），无需引入第三方 SSE 库，保持零依赖
- mock：Anthropic 路径发完整官方事件序列（message_start 带 usage 与缓存字段 → content_block_* → message_delta → message_stop）
- 测试：`TestSseConversion` 7 项单测（分帧容错 / 即时下发 / 参数缓冲 / stop_reason 映射 / usage 合并 / 错误帧）；冒烟第 6 组 7 项（**实测首尾帧间隔 30.7ms，证明逐块到达非整读补吐** + 流式轮照常进埋点）。53 项单测 + 18 项冒烟全过
- 文档：LIMITATIONS #1 标「部分闭环」（其余方向与 dropped 可见性写明）；README SSE 行与测试口径同步；REVIEW 网关文件清单补 sse.py

## 2026-09-09 · v1.7 同协议直通流式 + 未实现方向显式 501（本提交）

- **直通流式**（chat←chat / anthropic←anthropic）：`tee_lines` 三通逐行透传（字节不动、即时 flush），旁路收集器攒流尾汇总——anthropic 方向复用 `AnthropicToChatStream`（帧丢弃只取 usage/文本），chat 方向新增 `ChatStreamCollector`（工具槽位拼接、脏数据容错）；落库/埋点仍走 `finalize_turn` 单一路径
- **隐藏 bug 修复**：此前**任何** stream:true 请求落到未实现方向，`post_json` 会把 SSE 当 JSON 解析、炸成语焉不详的 502；现改为显式 **501**（附已支持方向清单，提示改用 stream:false）
- 客户端中途断开：已收到的部分照常落账（费用已实际发生）
- mock：`/chat/completions` 路径发 Chat 形状 chunk（role 首帧 + finish + usage + [DONE]）
- 测试：`TestChatStreamCollector` 3 项单测；冒烟第 7 组 7 项（两个透传方向文本拼合 / 逐块到达 ~31ms / [DONE] / 501）。56 项单测 + 25 项冒烟全过
