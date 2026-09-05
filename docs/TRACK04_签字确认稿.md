# TRACK 04 Session 边界参数 · 签字确认稿

> 用途：分工会上与 TRACK 04（Session 组）当面核对四个边界参数。
> 我方（TRACK 05A/05B）已全部做成**可切换配置**，04 确认任何口径都只需改
> `config/session.json`，不返工。当前默认假设值可独立运行、可先行实验。
>
> 日期：2026-09-02 ｜ 我方负责人：韩浩然

## 一、待确认参数（确认后在签字栏打勾/改值）

| # | 参数 | 配置项 | 我方默认假设值 | TRACK 04 确认值 | 签字 |
|---|---|---|---|---|---|
| 1 | 会话键粒度 | `key_granularity` / `key_fields` | `single_task`（task-id 单键） | ＿＿＿＿ | ＿＿ |
| 2 | 重放起点 | `replay_from` | `full`（全量重放） | ＿＿＿＿ | ＿＿ |
| 3 | 会话结束策略 | `end_policy` / `on_end` / `ttl_seconds` | `ttl` + `archive`（30 min） | ＿＿＿＿ | ＿＿ |
| 4 | 单会话记忆注入上限 | `session_memory_cap`（全局）/ `meta.memory_cap`（会话级） | 0（不限制） | ＿＿＿＿ | ＿＿ |

## 二、本地验证证据（均可复跑）

| 参数 | 证据 | 位置 |
|---|---|---|
| 重放起点三模式（full / last_breakpoint / sliding_window） | 单测 `test_build_prefix_strategies` | `tests/test_offline.py` |
| TTL 淘汰 + archive 保留可查 | 单测 `test_ttl_eviction` | 同上 |
| 记忆上限三级生效（会话 meta > 全局 > 0 不限）+ 幂等去重 | 单测 `test_memory_injection_dedup_and_cap` / `test_memory_cap_fallback_chain` | 同上 |
| 会话与 meta 落库重启不丢 | 单测 `test_sqlite_persistence_roundtrip` | 同上 |
| 端到端：管理口写 meta → 转换注入 2 条（cap=2）→ 预热冲突 400 | 2026-09-02 冒烟记录 | 网关 `server.py` |

## 三、耦合声明（为什么这些参数影响 05 组实验）

缓存前缀取决于重放起点与注入内容：Anthropic 缓存是**前缀匹配**——
重放起点或记忆注入口径一变，前缀即变，命中率结论随之改变。
因此实验报告将把 `config/session.json` 的完整快照与命中率数据**绑死**
（方案 3.9.1 纪律①），结论采用条件写法（纪律②）：

> 「在 TRACK 04 确认的〈重放起点/结束策略/记忆上限〉口径下，命中率为 X；
> 若口径变为 Y，预期影响为 Z（定性），需复测确认。」

## 四、签字栏

- TRACK 04 负责人：＿＿＿＿＿＿  日期：＿＿＿＿
- TRACK 05A/05B 负责人：韩浩然  日期：2026-09-＿＿
- 导师（如需）：＿＿＿＿＿＿  日期：＿＿＿＿
