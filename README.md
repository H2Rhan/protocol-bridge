# protocol-bridge

犀牛鸟开源实战 · TRACK 05A/05B 协议转换组

OpenAI Chat / OpenAI Response ↔ Anthropic 的协议转换层（网关/代理 + 独立状态层）。
本仓库按「可移植标准」编写，主线在自仓库迭代，之后可拆出提上游。

> 交付节奏：截止 **2026-09-14**。当前为**离线骨架阶段**——
> 所有依赖真实 API（Anthropic / OpenAI）的实验**挂起等额度**，先用录制样例 + mock backend 把链路打通。

## 当前状态（API 挂起，先跑骨架）

| 模块 | 状态 | 说明 |
|---|---|---|
| IR v0 契约 | ✅ 离线 | `docs/ir-schema.md` + `src/ir/model.py` |
| 状态层（SQLite 持久化）+ 可切换 Session 配置 | ✅ v1.1 | `src/state/` + `config/session.json`，重启不丢 |
| 三对 adapter（字段映射） | ✅ 离线 | `src/adapters/`，录制样例可测 |
| 网关转发 + SSE 透传 | ✅ 离线（mock）/ Groq 真实链路已验证 | `src/gateway/` |
| 记忆注入（幂等去重 + memory_cap） | ✅ v1.1 新增 | `inject_memories`，管理口 `/v1/admin/session/meta` |
| 预热拒绝条件校验 | ✅ v1.1 新增 | stream/thinking/structured outputs/tool_choice 四类冲突 400 |
| 限流 | ✅ v1.1 新增 | `PB_CONCURRENCY`（默认 2） |
| 命中率埋点（5 项暴露） | ✅ 框架 | injected 字段 v1.1 起真实统计；v1.2 起预热轮单列、多线程写加锁 |
| max_tokens:0 预热路径 | ✅ 字段处理 | 用录制响应样例测 |
| **两轮代码自查（9 bug 修复 + 回归测试）** | ✅ **v1.2 新增** | 含 3 个严重项（状态层空转 / 多轮链断 / prev_id 回传），见 `docs/REVIEW.md` 第五节 |
| 三组对照实验 | ⏸ 等 Anthropic 端点通道恢复 | `experiments/`，框架已就绪；验站脚本 `tools/verify_cache.py` |
| TRACK04 签字确认稿 | ✅ v1.1 新增 | `docs/TRACK04_签字确认稿.md`，分工会直接签 |
| 弹网页（本地仪表盘） | 🔜 骨架 | `src/webui/` 安全骨架 |

## 快速开始（离线）

```bash
# 零外部依赖（仅标准库），Python 3.11+
python -m unittest tests.test_offline -v   # 25 项离线单测：IR 往返 / adapter / 状态层 / 预热 / 自查回归 / 多轮链 E2E
python tools/smoke_e2e.py                  # 11 项端到端冒烟：真起 mock+网关子进程，5 组链路断言
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

- 网关按 target 映射真实端点：`openai_chat→/chat/completions`、`openai_response→/responses`、`anthropic→/v1/messages`。
- 已用 Groq 验证：`openai_chat→openai_chat` 直通、`openai_response→openai_chat` 跨协议转换，
  真实 usage（prompt/completion/reasoning tokens）正常归一并写入 metrics jsonl。
- ⚠️ 本机代理 `127.0.0.1:50403` 对 POST 隧道不稳，必须 `PB_DIRECT=1`；Cloudflare 按 UA 拦 bot，网关已带浏览器 UA。
- Anthropic 目标需 Anthropic 协议端点（Groq 无 `/v1/messages`）；网关自动带 `anthropic-version` 头（`PB_ANTHROPIC_VERSION` 可覆盖）。
- 接 Anthropic 端点调试缓存链路前，先跑验站脚本确认透传（PASS 才可用）：
  `python tools/verify_cache.py --base https://<端点域名> --model <模型> --key <key>`
  正式实验数据口径（共享账号缓存污染与缓解）见方案 v3.1 §3.6.1。

## 设计要点（对应执行方案 v3）

- **IR 星型结构**：三个协议各写「转 IR / 从 IR 出」两个方向，3 对 adapter 替代 6 方向两两互转。
- **有状态服务**：`previous_response_id` 要求转换层带存储（会话表 + TTL + 并发分叉处理）。
- **Session 边界可切换**：`config/session.json` 三个开关（`key_granularity` / `replay_from` / `end_policy`），
  改配置即可换重放策略，不动 adapter 与实验脚本（见 `docs/ir-schema.md` L2）。
- **缓存断点布局**：3 个固定分层断点（tools 后 / system 后 / 历史静态段后）+ 1 个滚动尾部断点，
  用满官方 4 个 `cache_control` 上限。
- **命中率北极星**：每轮暴露 5 项（命中率 / 注入 token / 重放 token / 丢弃参数 / 降级路径）。

## 实验 API 与预算（见方案 3.6.1）

- 三组实验都必须真实 **Anthropic key**（命中率字段 mock 给不出）；**OpenAI key** 用于跨协议成本对比（用量少）。
- 优先级：粒度、更新频率必做；位置组可降级（D2 视 Session 04 口径挂起）。
- 省钱：低价档预演 → `max_tokens:0` 预热（零输出计费）→ `count_tokens` 免费端点校准。

## 目录

```
src/
  ir/            IR 数据模型（L0/L1/L2）
  adapters/      chat / response / anthropic 三对 adapter
  state/         会话表 + TTL + session 配置 + build_prefix
  gateway/       HTTP 转发 + SSE 透传
  observability/ 命中率埋点（5 项暴露）
  warmup/        max_tokens:0 预热
  webui/         弹网页本地仪表盘（安全骨架）
config/          session.json（Session 边界可切换配置）
experiments/     三组对照实验（dry-run 可跑）
tests/           离线单测（录制样例 + 自查回归 + 多轮链 E2E）
tools/           mock_backend + verify_cache + smoke_e2e + recordings
docs/            ir-schema.md / capability-matrix.md / REVIEW.md / TRACK04_签字确认稿.md
```
