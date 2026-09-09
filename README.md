# protocol-bridge

[![test](https://github.com/H2Rhan/protocol-bridge/actions/workflows/test.yml/badge.svg)](https://github.com/H2Rhan/protocol-bridge/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![deps](https://img.shields.io/badge/deps-stdlib%20only-brightgreen)

犀牛鸟开源实战 · TRACK 05A/05B 协议转换组

OpenAI Chat / OpenAI Response ↔ Anthropic 的协议转换层（网关/代理 + 独立状态层）。
本仓库按「可移植标准」编写，主线在自仓库迭代，之后可拆出提上游。

> 交付节奏：截止 **2026-09-14**。骨架与真实 API 实验均已闭环——
> 缓存命中率实测（19 组对照 + 覆盖实验 E20–E28）已完成，量化取舍结论见 `docs/experiment-results.md`。

## 当前状态

| 模块 | 状态 | 证据 |
|---|---|---|
| IR v0 契约（三层 + 变更记录） | ✅ 冻结（语义层） | `docs/ir-schema.md` + `src/ir/model.py`；抗漂移机制见变更记录节 |
| 状态层（SQLite 持久化）+ 可切换 Session 配置 | ✅ v1.1 | `src/state/` + `config/session.json`，重启不丢（`TestStateLayer`） |
| 三对 adapter（字段映射） | ✅ 离线 | `src/adapters/`，录制样例可测（`TestAdapterRoundTrip`） |
| 网关转发 | ✅ 离线（mock）/ Groq 真实链路已验证 | `src/gateway/`；E29 真实端点对账 |
| SSE 流式 | ◐ **v1.7 三方向逐块**（chat←anthropic 转换 + 同协议透传） | `src/gateway/sse.py`；未实现方向显式 501 → `docs/LIMITATIONS.md` #1；E21 证明流式无损命中率 |
| 记忆注入（幂等去重 + memory_cap） | ✅ v1.1 新增 | `inject_memories` + `TestGatewayPolicies`；管理口 `/v1/admin/session/meta` |
| 工具调用全链路（含 ID 双向映射） | ✅ **v1.4** | 跨协议参数/签名不丢（`TestCrossProtocolToolArgs`/`TestThinkingSignature`）；`src/state/idmap.py` 会话级映射（`TestToolIdMap`） |
| 状态层重放（结构化块） | ✅ **v1.4** | thinking（signature/redacted）与 tool_use 入历史（`TestAssistantFromUpstream`） |
| 预热拒绝条件校验 | ✅ v1.1 新增 | stream/thinking/structured outputs/tool_choice 四类冲突 400（`TestWarmup`） |
| 限流 | ✅ v1.1 新增 | `PB_CONCURRENCY`（默认 2） |
| 命中率埋点（5 项暴露） | ✅ 已对账 | **E29：埋点与真实端点 usage 逐字段一致** |
| max_tokens:0 预热路径 | ✅ 实测有效 | 录制样例测 + 实测预热后 100% 命中（exp17） |
| **两轮代码自查（9 bug 修复 + 回归测试）** | ✅ **v1.2 新增** | 含 3 个严重项，各配回归测试（`TestAuditFindings`），见 `docs/REVIEW.md` 第五节 |
| 三组对照实验 | ✅ 34 组全量实测 | `docs/experiment-results.md`（含第〇节证伪条件预登记） |
| TRACK04 边界参数对齐稿 | ✅ 已对齐 | `docs/TRACK04_签字确认稿.md`（single_task 等三参数已定稿） |
| 弹网页（本地仪表盘） | ✅ 记忆编排界面 | `src/webui/` 安全骨架 + 记忆勾选/裁剪 + 缓存前缀可视化 |
| 已知限制（边界自觉） | 📋 12 项四要素（3 项已闭环留档） | `docs/LIMITATIONS.md`（含结论隔离证明） |
| **TypeScript 移植版** | ✅ **v2.0**（对齐上游技术栈） | `ts/`：56 项单测 + 25 项冒烟 + strict 类型检查全过（用例与 Python 版一一镜像），差异清单见 `ts/README.md` |

## 快速开始（离线）

```bash
# 零外部依赖（仅标准库），Python 3.11+
python -m unittest tests.test_offline -v   # 56 项离线单测：IR 往返 / adapter / 状态层 / 预热 / 三轮自查回归 / 多轮链 E2E / ID 映射 / property-based / 惰性淘汰 / SSE 转换与透传
python tools/smoke_e2e.py                  # 25 项端到端冒烟：真起 mock+网关子进程，7 组链路断言（含 SSE 转换/透传/501）
python tools/mock_backend.py               # 起 mock backend（127.0.0.1:9100，按端点返回三种协议形状）
python -m src.gateway.server               # 起网关（转发到 mock）
python experiments/run_experiments.py --dry-run   # 实验框架 dry-run（不真实调 API）
```

## 挂接真实后端（OpenAI v1 兼容，如 Groq）

```bash
PB_BACKEND="https://api.groq.com/openai/v1" \
PB_API_KEY="<key，仅环境变量，不落盘>" \
PB_DIRECT=1 \                      # 绕过系统代理（本地代理隧道对长 POST 可能 502）
python -m src.gateway.server
# 然后 POST http://127.0.0.1:8080/v1/{source}/to/{target}
```

- 网关按 target 映射真实端点：`openai_chat→/chat/completions`、`openai_response→/responses`、`anthropic→/v1/messages`。路径可经 `PB_CHAT_PATH` / `PB_RESPONSE_PATH` / `PB_ANTHROPIC_PATH` 覆盖——接 Anthropic 兼容端点（base 含 `/api/v1`）时 anthropic 需设 `PB_ANTHROPIC_PATH=/messages`，否则会拼出多一个 `/v1` 而 404。
- 已用 Groq 验证：`openai_chat→openai_chat` 直通、`openai_response→openai_chat` 跨协议转换，
  真实 usage（prompt/completion/reasoning tokens）正常归一并写入 metrics jsonl。
- ⚠️ 本机代理 `127.0.0.1:50403` 对 POST 隧道不稳，必须 `PB_DIRECT=1`；Cloudflare 按 UA 拦 bot，网关已带浏览器 UA。
- Anthropic 目标需 Anthropic 协议端点（Groq 无 `/v1/messages`）；网关自动带 `anthropic-version` 头（`PB_ANTHROPIC_VERSION` 可覆盖）。
- 接 Anthropic 端点调试缓存链路前，先跑验站脚本确认透传（PASS 才可用）：
  `python tools/verify_cache.py --base https://<端点域名> --model <模型> --key <key>`
  正式实验数据口径（缓存污染与缓解：唯一 run_id 前缀隔离）见 `docs/experiment-results.md` 第七节「方法学与踩坑」第 1 条。

## 设计要点（对应执行方案 v3）

- **IR 星型结构**：三个协议各写「转 IR / 从 IR 出」两个方向，3 对 adapter 替代 6 方向两两互转。
- **有状态服务**：`previous_response_id` 要求转换层带存储（会话表 + TTL + 并发分叉处理）。
- **Session 边界可切换**：`config/session.json` 三个开关（`key_granularity` / `replay_from` / `end_policy`），
  改配置即可换重放策略，不动 adapter 与实验脚本（见 `docs/ir-schema.md` L2）。
- **缓存断点布局**：3 个固定分层断点（tools 后 / system 后 / 历史静态段后）+ 1 个滚动尾部断点，
  用满官方 4 个 `cache_control` 上限。
- **命中率北极星**：每轮暴露 5 项（命中率 / 注入 token / 重放 token / 丢弃参数 / 降级路径）。

## 实验（已完成，34 组）

- 缓存命中率实测 **34 组**（exp1–19 三组对照 + E20–E28 覆盖 + E30–E34 进阶），量化取舍结论见 `docs/experiment-results.md`。
- **实验编号 = 假设编号**：问题清单的 A 级条目在写代码前即登记为待判决假设（测试左移），回链表见实验文档第〇节。判决分布：证实 8 · 证伪/反向 4 · 反转 1（含「20-block 回看窗口」证伪、「05B 透传不可行 → 转 Messages 可达」反转）。
- 核心结论：LOCKED vs DYNAMIC ≈ **7.3×**（跨模型一致）；命中比冷启动快 **~1.65s**；缓存按 provider 隔离、不跨模型复用；haiku-4.5 阈值 4096 token。
- 配套脚本：`experiments/run_experiments.py`（三组对照）、`rich_experiments.py`（E20–E28）、`rich_experiments2.py`（E30–E34）。

## 与上游 TencentDB-Agent-Memory 的关系

[TencentDB-Agent-Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory) 的核心机制是「proxy 不改协议、每轮向上下文注入 L2/L3 记忆」——**本仓库的 34 组实验量化的正是这套机制的成本语义**：同一份记忆，锁死注入 vs 动态注入成本差 7.3×；低于模型阈值静默不缓存；断点布局直接决定前缀稳定性。

可拆出提上游的方向（已核对上游源码，表述以其现状为准）：上游 MemoryProxy 对缓存断点是**被动保留**的——透传客户端已有的 `cache_control`（`injection/adapters/anthropic.ts`），注入记忆时**特意不加**断点（`session/context-injector.ts` 注释申明理由），usage 管道也已采集 `cache_read_input_tokens`（`credit-reporter.ts`，用于计费上报）。**上游缺的不是埋点字段，而是「注入方式 × 命中率」的量化依据与主动策略**：本仓库的 34 组实验数据（锁死 vs 动态 7.3×、确定性序列化 74.8% vs 24.9%、逐模型阈值表）+ 主动断点布局（3 固定 + 1 滚动）+ 确定性序列化流程，可作为上游注入策略的参考实现与决策依据。

## 目录

```
src/
  ir/            IR 数据模型（L0/L1/L2）
  adapters/      chat / response / anthropic 三对 adapter
  state/         会话表 + TTL + session 配置 + build_prefix
  gateway/       HTTP 转发 + SSE 流式（chat←anthropic 逐块转换 + 同协议透传；未实现方向 501，见 docs/LIMITATIONS.md #1）
  observability/ 命中率埋点（5 项暴露）
  warmup/        max_tokens:0 预热
  webui/         弹网页本地仪表盘（安全骨架 + 记忆编排界面）
config/          session.json（Session 边界可切换配置）
experiments/     三组对照 + rich_experiments.py（E20–E28）+ rich_experiments2.py（E30–E34）
tests/           离线单测（录制样例 + 自查回归 + 多轮链 E2E）
tools/           mock_backend + verify_cache + smoke_e2e + recordings
data/            34 组实验原始数据（逐轮 jsonl + 汇总 csv，溯源表见 data/README.md）
ts/              TypeScript 全量移植（对齐上游 Node 22 + TS 技术栈，v2.0）
docs/            ir-schema.md / capability-matrix.md / experiment-results.md / LIMITATIONS.md / REVIEW.md / TRACK04_签字确认稿.md
CHANGELOG.md     按日期演进记录（v0 契约冻结 → v1.2 → 实验交付 → 覆盖补全）
```
