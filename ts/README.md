# protocol-bridge · TypeScript 移植版

> 与上游 TencentDB-Agent-Memory（Node 22 / TypeScript 技术栈）对齐的全量移植。
> 功能对齐 Python 版 v1.7：**59 项离线单测 + 25 项端到端冒烟全过**（与 Python 版用例一一镜像）。
> Python 版保留在仓库根目录作参考实现；docs/ 下的契约、实验与取舍文档对两个实现同样适用。

## 快速开始

```bash
cd ts
npm install            # 仅开发依赖（typescript + @types/node）；运行零第三方依赖
npm test               # 59 项离线单测（node:test + Node 22 类型擦除，无需编译）
npm run smoke          # 25 项端到端冒烟：真起 mock + 网关两个子进程
npm run typecheck      # tsc --noEmit（strict + erasableSyntaxOnly）
npm run gateway        # 起网关（127.0.0.1:8080，转发到 PB_BACKEND，默认 mock）
npm run mock           # 起 mock backend（127.0.0.1:9100）
```

环境要求：**Node.js ≥ 22.18**（type stripping 默认可用 + `node:sqlite` 无 flag）。
环境变量与 Python 版一致：`PB_BACKEND` / `PB_API_KEY` / `PB_PORT` / `PB_METRICS` / `PB_DB` / `PB_CONCURRENCY` / `PB_ANTHROPIC_VERSION` / `PB_CHAT_PATH` / `PB_RESPONSE_PATH` / `PB_ANTHROPIC_PATH`。

## 目录映射（Python → TS）

| Python | TypeScript | 说明 |
| --- | --- | --- |
| `src/ir/model.py` | `src/ir/model.ts` | IR 三层契约（Block/Message/IRRequest/IRUsage/SessionContext） |
| `src/adapters/*.py` | `src/adapters/*.ts` | 三对 adapter + 降级记录 + `assistantFromUpstream` |
| `src/state/*.py` | `src/state/*.ts` | SessionStore（node:sqlite）/ ToolIdMap / SessionConfig |
| `src/gateway/server.py` | `src/gateway/server.ts` | 网关主进程（convert / finalizeTurn / 两条 SSE 路径 / 501） |
| `src/gateway/sse.py` | `src/gateway/sse.ts` | SSE 分帧 + Anthropic→Chat 转换 + Chat 旁路收集 |
| `src/observability/metrics.py` | `src/observability/metrics.ts` | 5 项埋点 + hitRate |
| `src/warmup/prewarm.py` | `src/warmup/prewarm.ts` | max_tokens:0 预热 |
| `tools/mock_backend.py` | `tools/mock_backend.ts` | 三协议形状 + 有状态缓存模拟 + SSE |
| `tools/smoke_e2e.py` | `tools/smoke_e2e.ts` | 7 组 25 项断言 |
| `src/webui/server.py` | `src/webui/server.ts` | 弹网页仪表盘（v2.1） |
| `tools/verify_cache.py` | `tools/verify_cache.ts` | 验站脚本（v2.1，恒直连） |
| `tests/test_offline.py`（56 项） | `tests/offline.test.ts`（59 项） | 用例一一镜像 + TS 版新增 webui/verify_cache 3 项 |

## 差异清单（有意为之，均不影响语义）

1. **不读系统代理**：Node 的 `http.request` 不读 `http_proxy` 环境变量，恒直连——与 Python 版 `PB_DIRECT=1` 的行为一致；`PB_DIRECT` 保留兼容但为 no-op。
2. **SQLite 用 `node:sqlite`**（Node 内置，experimental warning 属正常）：表结构、SQL、惰性淘汰语义与 Python 版完全一致；单线程同步驱动，Python 版 `threading.Lock` 与「锁外调用防死锁」的注释在 TS 版天然不适用。
3. **property 测试 PRNG**：Python 用 `random.Random`（Mersenne Twister），TS 用 mulberry32——种子固定、序列不同，断言的四条性质（断点 [3,4] / 未知字段必降级 / 工具参数守恒 / signature 守恒）完全一致。
4. **未移植**：仅 `experiments/`（真实 API 实验脚本——研究代码，跟随 Python 版即可）；webui 与 verify_cache 已于 v2.1 补齐。

## 与上游 MemoryProxy 的关系

本目录按上游技术栈（Node 22 + TypeScript + 零重型依赖）组织，模块边界与上游 `MemoryProxy/src/` 的 handler/adapter 结构对应——断点布局（3 固定 + 1 滚动）、usage 归一、命中率埋点、状态层重放均可整体或按模块迁入。34 组实验的量化结论见根目录 `docs/experiment-results.md`。
