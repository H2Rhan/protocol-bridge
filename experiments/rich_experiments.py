# -*- coding: utf-8 -*-
"""
协议转换课题 · 丰富实验批次（E20–E28）· 补齐覆盖空洞
复用现有框架：mem() 字符系数 6 / post() 指数退避 / session_id 黏性 / 独立 salt 前缀隔离。
主模型走 OR_MODEL（低档省钱），E20 跨模型对照用 OR_SONNET_MODEL。
端点与密钥不落盘：OR_BASE_URL（Anthropic 兼容端点 base）+ OR_KEY 环境变量；OR_ONLY=exp20,exp21,... 可单独跑。

覆盖：
  E20 跨模型缓存复用      haiku 建 → sonnet 读 / 反向
  E21 流式 vs 非流式一致   stream=true 命中率是否变化
  E22 工具 schema 缓存失效 改 tool 参数名 → 前缀失效
  E23 haiku 精确阈值扫描   1024→6000 钉死 haiku 最小可缓存长度
  E24 断点位置 system/user  cache_control 放哪
  E25 同 session 并发       3 并发同前缀是否冲垮缓存
  E26 动态 tool_result 混合  锁记忆+每轮变工具结果
  E27 05B 救援             Responses 后端 0% → Messages 后端能否命中（决定 05B 死活）
  E28 haiku 收益主数字      LOCKED vs DYNAMIC 20 轮，用推荐便宜模型重算倍数
  E29 埋点可观测性对账      需网关运行时，本脚本只输出校验方法（不联网）

输出：实验结果/rich_<时间戳>.jsonl + 控制台汇总
"""
import os, sys, json, time, random, threading, urllib.request, urllib.error
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")
KEY = os.environ.get("OR_KEY", "")
_BASE = os.environ.get("OR_BASE_URL", "").rstrip("/")   # Anthropic 兼容端点 base，如 https://<host>/api/v1
URL_M = _BASE + "/messages" if _BASE else ""
URL_R = _BASE + "/responses" if _BASE else ""
OUT_DIR = os.environ.get("OR_OUT", "./实验结果")
MODEL = os.environ.get("OR_MODEL", "")              # 主模型（低档省钱）
SONNET = os.environ.get("OR_SONNET_MODEL", "")      # 高档模型（E20 跨模型对照）
PRICE_IN = float(os.environ.get("OR_PRICE_IN", "1e-6"))    # haiku
PRICE_OUT = float(os.environ.get("OR_PRICE_OUT", "5e-6"))
W_MUL, R_MUL = 1.25, 0.1
BETA = {"anthropic-beta": "prompt-caching-2024-07-31"}

BASE = ("The user is building an open source protocol translation layer that converts "
        "OpenAI Chat Completions and OpenAI Responses API traffic into the Anthropic "
        "Messages format. Long term memory is injected into every request so the assistant "
        "remembers project conventions, coding style and past decisions. Stable content must "
        "be placed at the front of the prompt while dynamic retrieved memory is appended at "
        "the end, because prompt caching matches prefixes byte for byte. ")


def mem(n):
    need = int(n * 6)
    return (BASE * (need // len(BASE) + 1))[:need]


def salt():
    return "%08x" % random.randrange(16 ** 8)


def post(body, retries=4, stream=False, url=URL_M):
    """Anthropic Messages 格式调用；stream=True 时解析 SSE 取 cache 字段。"""
    h = {"Authorization": "Bearer " + KEY, "Content-Type": "application/json", **BETA}
    if stream:
        h["Accept"] = "text/event-stream"
    last = None
    for a in range(retries):
        try:
            req = urllib.request.Request(url, json.dumps(body).encode("utf-8"), h, method="POST")
            with urllib.request.urlopen(req, timeout=120) as r:
                if stream:
                    usage = None
                    for raw in r:
                        line = raw.decode("utf-8", "ignore").strip()
                        if not line or line.startswith("event:"):
                            continue
                        if line.startswith("data:"):
                            p = line[5:].strip()
                            if p == "[DONE]":
                                break
                            try:
                                obj = json.loads(p)
                            except Exception:
                                continue
                            if obj.get("type") == "message_start":
                                usage = dict(obj["message"].get("usage", {}))
                            elif obj.get("type") == "message_delta":
                                d = obj.get("usage", {}) or {}
                                if usage is None:
                                    usage = {}
                                for k in ("output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                                    if k in d:
                                        usage[k] = d[k]
                    return usage or {}
                return json.load(r).get("usage", {}) or {}
        except urllib.error.HTTPError as e:
            last = RuntimeError("HTTP %d: %s" % (e.code, e.read().decode("utf-8", "ignore")[:150]))
            if e.code in (408, 429, 500, 502, 503, 504):
                time.sleep(2 ** a * 2); continue
            break
        except Exception as e:
            last = e; time.sleep(2 ** a * 2); continue
    raise last


def post_responses(body, retries=4):
    """OpenAI Responses 格式调用；usage 形态不同（cached_tokens 即命中）。"""
    h = {"Authorization": "Bearer " + KEY, "Content-Type": "application/json"}
    last = None
    for a in range(retries):
        try:
            req = urllib.request.Request(URL_R, json.dumps(body).encode("utf-8"), h, method="POST")
            with urllib.request.urlopen(req, timeout=120) as r:
                obj = json.load(r)
                u = obj.get("usage", {}) or {}
                cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                return {"cache_read": cached, "input_tokens": u.get("prompt_tokens", 0),
                        "output_tokens": u.get("completion_tokens", 0),
                        "cache_creation": 0}
        except urllib.error.HTTPError as e:
            last = RuntimeError("HTTP %d: %s" % (e.code, e.read().decode("utf-8", "ignore")[:150]))
            if e.code in (408, 429, 500, 502, 503, 504):
                time.sleep(2 ** a * 2); continue
            break
        except Exception as e:
            last = e; time.sleep(2 ** a * 2); continue
    raise last


def cost(u, pin=PRICE_IN, pout=PRICE_OUT):
    cr = _iv(u, "cache_read_input_tokens")
    cc = _iv(u, "cache_creation_input_tokens")
    ti = _iv(u, "input_tokens")
    ot = _iv(u, "output_tokens")
    return ti * pin + ot * pout + cc * pin * W_MUL + cr * pin * R_MUL


def _iv(u, *keys):
    """只认整数 token 字段，忽略 dict/None 兜底，防止 int+dict 崩溃。"""
    if not isinstance(u, dict):
        return 0
    for k in keys:
        v = u.get(k)
        if isinstance(v, int):
            return v
    return 0


def hit(u):
    cr = _iv(u, "cache_read_input_tokens")
    cc = _iv(u, "cache_creation_input_tokens")
    ti = _iv(u, "input_tokens")
    tot = cr + cc + ti
    return cr, cc, ti, (round(cr / tot, 4) if tot else 0.0)


def log(rows, jf):
    with open(jf, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ---------------- E20 跨模型缓存复用 ----------------
def exp20(rows):
    print("\n▶ E20 跨模型缓存复用（haiku ↔ sonnet，provider 隔离验证）")
    # haiku 建，sonnet 读
    s = salt(); system = [{"type": "text", "text": "You are a helpful assistant."},
        {"type": "text", "text": f"[MEM {s}]\n" + mem(5000), "cache_control": {"type": "ephemeral"}}]
    b_h = {"model": MODEL, "session_id": f"e20h-{s}", "max_tokens": 16, "system": system,
           "messages": [{"role": "user", "content": "Reply OK."}]}
    u = post(b_h); cr, cc, ti, _ = hit(u)
    print(f"  haiku 建立: write={cc} read={cr}")
    rows.append({"exp": "E20", "case": "haiku_build", "cr": cr, "cc": cc, "ti": ti})
    b_s = dict(b_h); b_s["model"] = SONNET; b_s["session_id"] = f"e20hs-{s}"
    u = post(b_s); cr, cc, ti, _ = hit(u)
    print(f"  → sonnet 读同前缀: read={cr} （0=provider 隔离，缓存不跨模型）")
    rows.append({"exp": "E20", "case": "sonnet_read", "cr": cr, "cc": cc, "ti": ti})
    # 反向
    s2 = salt(); system2 = [{"type": "text", "text": "You are a helpful assistant."},
        {"type": "text", "text": f"[MEM {s2}]\n" + mem(5000), "cache_control": {"type": "ephemeral"}}]
    b_s2 = {"model": SONNET, "session_id": f"e20s-{s2}", "max_tokens": 16, "system": system2,
            "messages": [{"role": "user", "content": "Reply OK."}]}
    u = post(b_s2); cr, cc, ti, _ = hit(u)
    print(f"  sonnet 建立: write={cc} read={cr}")
    rows.append({"exp": "E20", "case": "sonnet_build", "cr": cr, "cc": cc, "ti": ti})
    b_h2 = dict(b_s2); b_h2["model"] = MODEL; b_h2["session_id"] = f"e20sh-{s2}"
    u = post(b_h2); cr, cc, ti, _ = hit(u)
    print(f"  → haiku 读同前缀: read={cr} （0=provider 隔离）")
    rows.append({"exp": "E20", "case": "haiku_read", "cr": cr, "cc": cc, "ti": ti})


# ---------------- E21 流式 vs 非流式 ----------------
def exp21(rows):
    print("\n▶ E21 流式 vs 非流式一致性")
    s = salt(); system = [{"type": "text", "text": "You are a helpful assistant."},
        {"type": "text", "text": f"[MEM {s}]\n" + mem(5000), "cache_control": {"type": "ephemeral"}}]
    # 非流式建立
    u = post({"model": MODEL, "session_id": f"e21n-{s}", "max_tokens": 16, "system": system,
              "messages": [{"role": "user", "content": "Reply OK."}]})
    cr, cc, ti, _ = hit(u); rows.append({"exp": "E21", "case": "nonstream_build", "cr": cr, "cc": cc, "ti": ti})
    u = post({"model": MODEL, "session_id": f"e21n-{s}", "max_tokens": 16, "system": system,
              "messages": [{"role": "user", "content": "Reply yes."}]})
    cr, cc, ti, h1 = hit(u); rows.append({"exp": "E21", "case": "nonstream_read", "cr": cr, "cc": cc, "ti": ti, "hit": h1})
    # 流式建立+读
    u = post({"model": MODEL, "session_id": f"e21s-{s}", "max_tokens": 16, "system": system,
              "messages": [{"role": "user", "content": "Reply OK."}], "stream": True}, stream=True)
    cr, cc, ti, _ = hit(u); rows.append({"exp": "E21", "case": "stream_build", "cr": cr, "cc": cc, "ti": ti})
    u = post({"model": MODEL, "session_id": f"e21s-{s}", "max_tokens": 16, "system": system,
              "messages": [{"role": "user", "content": "Reply yes."}], "stream": True}, stream=True)
    cr, cc, ti, h2 = hit(u); rows.append({"exp": "E21", "case": "stream_read", "cr": cr, "cc": cc, "ti": ti, "hit": h2})
    print(f"  非流式命中={h1:.1%}  流式命中={h2:.1%}  → {'一致 ✅' if abs(h1-h2) < 0.01 else '不一致 ⚠'}")


# ---------------- E22 工具 schema 缓存失效 ----------------
def exp22(rows):
    print("\n▶ E22 工具 schema 缓存失效（TRACK 05 工具方向）")
    s = salt()
    def build(tool_name):
        return [{"type": "text", "text": "You are a helpful assistant."},
                {"type": "text", "text": f"[MEM {s}]\n" + mem(4000), "cache_control": {"type": "ephemeral"}}], \
               [{"name": "search", "description": "search docs",
                 "input_schema": {"type": "object", "properties": {"q": {"type": "string", "description": tool_name}},
                                  "required": ["q"]}}]
    system, tools = build("old_param")
    body = {"model": MODEL, "session_id": f"e22-{s}", "max_tokens": 16, "system": system, "tools": tools,
            "messages": [{"role": "user", "content": "search hi"}]}
    u = post(body); cr, cc, ti, _ = hit(u); rows.append({"exp": "E22", "case": "build", "cr": cr, "cc": cc, "ti": ti})
    u = post(body); cr, cc, ti, h = hit(u); rows.append({"exp": "E22", "case": "same_tool_read", "cr": cr, "cc": cc, "ti": ti, "hit": h})
    print(f"  工具未变，重发命中={h:.1%}")
    # 改 tool 参数名 → 前缀失效
    system2, tools2 = build("new_param_renamed")
    body2 = {"model": MODEL, "session_id": f"e22-{s}", "max_tokens": 16, "system": system2, "tools": tools2,
             "messages": [{"role": "user", "content": "search hi"}]}
    u = post(body2); cr, cc, ti, h2 = hit(u); rows.append({"exp": "E22", "case": "renamed_tool", "cr": cr, "cc": cc, "ti": ti, "hit": h2})
    print(f"  改 tool 参数名后重发: read={cr} write={cc} → {'前缀失效 ❌' if cc else '仍命中 ✅'}")


# ---------------- E23 haiku 精确阈值扫描 ----------------
def exp23(rows):
    print("\n▶ E23 haiku-4.5 精确阈值扫描")
    for n in [1024, 2048, 3000, 3500, 4096, 4500, 5000, 6000]:
        s = salt(); system = [{"type": "text", "text": "You are a helpful assistant."},
            {"type": "text", "text": f"[MEM {s}]\n" + mem(n), "cache_control": {"type": "ephemeral"}}]
        sid = f"e23-{n}-{s}"
        u = post({"model": MODEL, "session_id": sid, "max_tokens": 16, "system": system,
                  "messages": [{"role": "user", "content": "Reply OK."}]})
        cr, cc, ti, _ = hit(u)
        u = post({"model": MODEL, "session_id": sid, "max_tokens": 16, "system": system,
                  "messages": [{"role": "user", "content": "Reply yes."}]})
        cr, cc, ti, h = hit(u)
        rows.append({"exp": "E23", "case": f"tok{n}", "cr": cr, "cc": cc, "ti": ti, "hit": h})
        print(f"  {n:>5} token: 2轮命中={h:.1%} {'（过阈值）' if h else '（未过阈值）'}")


# ---------------- E24 断点位置 system vs user ----------------
def exp24(rows):
    print("\n▶ E24 断点位置：system 块 vs 首条 user 块")
    # A: system 带断点
    s = salt(); sysA = [{"type": "text", "text": "You are a helpful assistant."},
        {"type": "text", "text": f"[MEM {s}]\n" + mem(5000), "cache_control": {"type": "ephemeral"}}]
    sidA = f"e24a-{s}"
    for q in ["Reply OK.", "Reply yes."]:
        u = post({"model": MODEL, "session_id": sidA, "max_tokens": 16, "system": sysA,
                  "messages": [{"role": "user", "content": q}]})
        cr, cc, ti, h = hit(u); rows.append({"exp": "E24", "case": "sys_bp", "cr": cr, "cc": cc, "ti": ti, "hit": h})
    # B: 首条 user 带断点，system 无缓存
    s2 = salt(); sysB = [{"type": "text", "text": "You are a helpful assistant."}]
    sidB = f"e24b-{s2}"
    for q in ["Reply OK.", "Reply yes."]:
        msgs = [{"role": "user", "content": [{"type": "text", "text": f"[MEM {s2}]\n" + mem(5000),
                "cache_control": {"type": "ephemeral"}}]}]
        u = post({"model": MODEL, "session_id": sidB, "max_tokens": 16, "system": sysB, "messages": msgs})
        cr, cc, ti, h = hit(u); rows.append({"exp": "E24", "case": "user_bp", "cr": cr, "cc": cc, "ti": ti, "hit": h})
    print(f"  system 断点末轮命中、user 断点末轮命中 → 比较两者")


# ---------------- E25 同 session 并发 ----------------
def exp25(rows):
    print("\n▶ E25 同 session 并发（3 并发同前缀）")
    s = salt(); system = [{"type": "text", "text": "You are a helpful assistant."},
        {"type": "text", "text": f"[MEM {s}]\n" + mem(5000), "cache_control": {"type": "ephemeral"}}]
    sid = f"e25-{s}"
    # 先建立
    u = post({"model": MODEL, "session_id": sid, "max_tokens": 16, "system": system,
              "messages": [{"role": "user", "content": "Reply OK."}]})
    cr, cc, ti, _ = hit(u); rows.append({"exp": "E25", "case": "build", "cr": cr, "cc": cc, "ti": ti})
    # 3 并发读
    res = {}
    def worker(i):
        try:
            r = post({"model": MODEL, "session_id": sid, "max_tokens": 16, "system": system,
                      "messages": [{"role": "user", "content": f"Reply {i}."}]})
            res[i] = hit(r)
        except Exception as e:
            res[i] = ("ERR", str(e))
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for t in ts: t.start()
    for t in ts: t.join()
    for i in range(3):
        v = res[i]
        if isinstance(v, tuple) and len(v) == 4:
            cr, cc, ti, h = v
            rows.append({"exp": "E25", "case": f"concurrent{i}", "cr": cr, "cc": cc, "ti": ti, "hit": h})
            print(f"  并发{i}: read={cr} write={cc} hit={h:.1%}")
        else:
            print(f"  并发{i}: 异常 {v}")


# ---------------- E26 动态 tool_result 混合 ----------------
def exp26(rows):
    print("\n▶ E26 动态 tool_result 混合（锁记忆 + 每轮变工具结果）")
    s = salt(); system = [{"type": "text", "text": "You are a helpful assistant."},
        {"type": "text", "text": f"[MEM {s}]\n" + mem(5000), "cache_control": {"type": "ephemeral"}}]
    sid = f"e26-{s}"
    for i in range(6):
        msgs = [{"role": "user", "content": "query"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "search",
                                                   "input": {"q": "x"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                 "content": f"result-{i}-{salt()}"}]}]  # 每轮变化，置于缓存断点之后
        u = post({"model": MODEL, "session_id": sid, "max_tokens": 16, "system": system, "messages": msgs})
        cr, cc, ti, h = hit(u); rows.append({"exp": "E26", "case": f"round{i+1}", "cr": cr, "cc": cc, "ti": ti, "hit": h})
        print(f"  轮{i+1}: read={cr} write={cc} hit={h:.1%}")


# ---------------- E27 05B 救援 ----------------
def exp27(rows):
    print("\n▶ E27 05B 救援：Responses 后端 0% → Messages 后端能否命中")
    s = salt(); memtext = f"[MEM {s}]\n" + mem(5000)
    # (a) Responses 后端
    body_r = {"model": MODEL, "input": [
        {"role": "system", "content": memtext},
        {"role": "user", "content": "Reply OK."}], "max_output_tokens": 16}
    try:
        u = post_responses(body_r); cr, cc, ti = u["cache_read"], u["cache_creation"], u["input_tokens"]
        rows.append({"exp": "E27", "case": "responses_backend", "cr": cr, "cc": cc, "ti": ti})
        print(f"  (a) /v1/responses 后端: cached={cr} → {'无缓存 ❌' if cr == 0 else '有缓存 ✅'}")
    except Exception as e:
        print(f"  (a) /v1/responses 调用失败: {e}")
    # (b) Messages 后端（转换器把 Responses 内容转成 Anthropic 格式，后端走 /v1/messages）
    system = [{"type": "text", "text": "You are a helpful assistant."},
              {"type": "text", "text": memtext, "cache_control": {"type": "ephemeral"}}]
    sid = f"e27m-{s}"
    u = post({"model": MODEL, "session_id": sid, "max_tokens": 16, "system": system,
              "messages": [{"role": "user", "content": "Reply OK."}]})
    cr, cc, ti, _ = hit(u); rows.append({"exp": "E27", "case": "messages_backend_build", "cr": cr, "cc": cc, "ti": ti})
    u = post({"model": MODEL, "session_id": sid, "max_tokens": 16, "system": system,
              "messages": [{"role": "user", "content": "Reply yes."}]})
    cr, cc, ti, h = hit(u); rows.append({"exp": "E27", "case": "messages_backend_read", "cr": cr, "cc": cc, "ti": ti, "hit": h})
    print(f"  (b) /v1/messages 后端(转换器产出): 2轮命中={h:.1%} → {'05B 缓存可达 ✅' if h else '仍不可达 ❌'}")


# ---------------- E28 haiku 收益主数字 ----------------
def exp28(rows):
    print("\n▶ E28 haiku 收益主数字（LOCKED vs DYNAMIC，20 轮）")
    # LOCKED
    s = salt(); sysL = [{"type": "text", "text": "You are a helpful assistant."},
        {"type": "text", "text": f"[MEM {s}]\n" + mem(5000), "cache_control": {"type": "ephemeral"}}]
    sidL = f"e28L-{s}"; costL = 0.0; hitL = []
    for i in range(20):
        u = post({"model": MODEL, "session_id": sidL, "max_tokens": 16, "system": sysL,
                  "messages": [{"role": "user", "content": f"q{i}"}]})
        cr, cc, ti, h = hit(u); costL += cost(u); hitL.append(h)
        if i in (0, 19): rows.append({"exp": "E28", "case": f"LOCKED_r{i+1}", "cr": cr, "cc": cc, "ti": ti, "hit": h})
    # DYNAMIC
    sidD = f"e28D-{s}"; costD = 0.0; hitD = []
    for i in range(20):
        sysD = [{"type": "text", "text": "You are a helpful assistant."},
            {"type": "text", "text": f"[MEM {s} v{i}-{salt()}]\n" + mem(5000), "cache_control": {"type": "ephemeral"}}]
        u = post({"model": MODEL, "session_id": sidD, "max_tokens": 16, "system": sysD,
                  "messages": [{"role": "user", "content": f"q{i}"}]})
        cr, cc, ti, h = hit(u); costD += cost(u); hitD.append(h)
        if i in (0, 19): rows.append({"exp": "E28", "case": f"DYNAMIC_r{i+1}", "cr": cr, "cc": cc, "ti": ti, "hit": h})
    avgL = sum(hitL) / len(hitL); avgD = sum(hitD) / len(hitD)
    mul = costD / costL if costL else 0
    print(f"  LOCKED: 均命中={avgL:.1%} 成本=${costL:.5f}")
    print(f"  DYNAMIC: 均命中={avgD:.1%} 成本=${costD:.5f}")
    print(f"  → haiku 下贵 {mul:.2f} 倍（对比 sonnet 版 7.30 倍）")
    rows.append({"exp": "E28", "case": "summary", "locked_hit": round(avgL,4), "dynamic_hit": round(avgD,4),
                 "locked_cost": round(costL,6), "dynamic_cost": round(costD,6), "multiplier": round(mul,3)})


# ---------------- E29 埋点可观测性对账（方法，不联网） ----------------
def exp29(rows):
    print("\n▶ E29 埋点可观测性对账（需网关运行时，输出校验方法）")
    print("  网关北极星5项埋点已完成代码，但未与 API 原始 usage 对账。校验方法：")
    print("   1) 网关记录的 cache_read 应 = usage.cache_read_input_tokens；")
    print("   2) 网关命中率 = cache_read/(cache_read+cache_creation+input)；")
    print("   3) 对一组真实流量采样，比对网关埋点值与原始 jsonl，差异>0 即埋点字段取错。")
    print("  → 本实验需网关进程联调，不在本脚本联网范围；标记为『待网关联调』。")
    rows.append({"exp": "E29", "case": "method", "status": "待网关联调"})


EXPERIMENTS = [
    ("exp20", "跨模型缓存复用", exp20),
    ("exp21", "流式vs非流式", exp21),
    ("exp22", "工具schema失效", exp22),
    ("exp23", "haiku精确阈值", exp23),
    ("exp24", "断点位置", exp24),
    ("exp25", "同session并发", exp25),
    ("exp26", "动态tool_result", exp26),
    ("exp27", "05B救援", exp27),
    ("exp28", "haiku收益主数字", exp28),
    ("exp29", "埋点对账(方法)", exp29),
]


def main():
    if not KEY or not _BASE or not MODEL:
        print("ERROR: 请先 export OR_KEY / OR_BASE_URL / OR_MODEL"); return
    only = set((os.environ.get("OR_ONLY") or "").split(",")) if os.environ.get("OR_ONLY") else None
    os.makedirs(OUT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    jf = os.path.join(OUT_DIR, f"rich_{ts}.jsonl")
    print(f"模型默认={MODEL}  输出={jf}")
    all_rows = []
    for eid, desc, fn in EXPERIMENTS:
        if only and eid not in only:
            continue
        try:
            r = []
            fn(r)
            all_rows += r
            log(r, jf)
        except Exception as e:
            print(f"  !! {eid} 失败: {e}")
    print("\n" + "=" * 60)
    print(f"完成。原始数据：{jf}")
    print("=" * 60)


if __name__ == "__main__":
    main()
