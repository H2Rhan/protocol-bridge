# 实验原始数据（34 组实测 · 逐轮记录）

> 本目录是 `docs/experiment-results.md` 全部数字的**原始出处**：每一次真实 API 调用的逐轮 jsonl 记录 + 汇总 csv。
> 数据日期 2026-09-08 / 09-09，Anthropic Messages API 兼容端点真实调用。
> 口径与防污染方法（唯一 run_id 前缀隔离等）见实验文档第七节「方法学与踩坑」。

## 文件 → 实验编号 溯源表

| 文件 | 内容 | 对应实验文档结论 |
| --- | --- | --- |
| `experiment_20260908_213818.jsonl` / `summary_*.csv` | 三组对照主实验批次 1：A_baseline / B_locked / G1_300 / G2_4x512 / G3_2048 / G4_30frag | exp1–exp5 |
| `experiment_20260908_214354.jsonl` / `summary_*.csv` | 批次 2：增加 C_dynamic / D1_head / D2_tail | exp1–exp8 |
| `experiment_20260908_214628.jsonl` / `summary_*.csv` | 批次 3（8 轮 × 9 配置全量） | exp1–exp19 主数据 |
| `supplement_20260908_220106.jsonl` | 补充实验一：E1/E2 协议横向 token 对照（99.3% 少算）+ H_10/30/60blk 块数证伪 | 20-block 回看窗口证伪 |
| `supplement_20260908_220353.jsonl` | 补充实验一续：H_5/10/30/60blk | 同上 |
| `batch3_20260908_221208.jsonl` | 三协议横向（R1_anthropic / R2_chat / R3_responses）+ 逐模型阈值探测（T_haiku-4.5 / T_sonnet-4.6 / T_opus-5）+ N1–N5 更新频率公式 | 05B 悬案 / 阈值表 / (N−1)/N |
| `batch4_20260908_223443.jsonl` | 断点数量（P1–P4，第 5 个 400）+ 边际效应（M_500–8000）+ 顺序对照（O1_fixed / O2_shuffled）+ 长前缀（OP_10000/20000） | 4 断点上限 / 阈值边际 / 确定性序列化 |
| `batch5_20260908_223934.jsonl` | LONG 20 轮长对话 + WARMUP 预热 + GAIN_LOCKED/DYNAMIC 成本收益 | 7.3× 主数字 / 首轮 11.6× |
| `ttl_20260908_224911.jsonl` / `ttl_20260908_230014.jsonl` | TTL 双档（T1_5min / T2_1h），两次运行互相印证 | 5min 档 400s 失效 |
| `rich_20260908_232421.jsonl` / `rich_20260908_232832.jsonl` / `rich_20260908_233922.jsonl` | 覆盖实验 E20–E29（跨模型隔离 / 流式无损 / schema 变更 / 阈值复测 / 断点位置 / 05B 救援 E27 / E29 埋点对账） | E20–E29 全部结论 |
| `补充2_20260909_142421.jsonl` | E30 并发冷启动 / E31 前缀字节边界 | E30–E31 |
| `补充3_20260909_142829.jsonl` | E32 single-flight / E33 命中延迟差 ~1.65s | E32–E33 |
| `补充4_20260909_143614.jsonl` | E34 opus-5 四档复测（端点异常定性）+ haiku 对照 | E34 / LIMITATIONS #9 |

> 说明：采集当日另有两个 0 字节空文件（`补充2_20260909_141208/141913.jsonl`，为前两次未完成运行），无数据，未入库。

## 字段口径

- `cr` / `cache_read` = `cache_read_input_tokens`；`cc` / `cache_creation` = `cache_creation_input_tokens`；`ti` = `input_tokens`（均为上游真实返回）。
- `hit_rate` = cache_read / (cache_read + input_tokens)，与实验文档定义一致。
- `batch5` 的 `cost` 为按当时价目计算的美元成本；实验文档中的成本列统一为**相对倍数**，绝对金额以本目录原始记录为准。
