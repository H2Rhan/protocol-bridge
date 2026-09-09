# -*- coding: utf-8 -*-
"""
协议转换课题 · 覆盖补充实验（E30–E34）
在 rich_experiments.py（E20–E29）基础上补充：

  E30 并发冷启动写入竞争        N 个独立会话首次同时到达同一前缀，是否重复写入
  E31 前缀稳定性边界            前缀改 1 字符 → 整段失效；前缀不变、尾部变 → 仍命中
  E32 并发冷启动·多轮统计        N=3/6 各 4 轮，量化重复写入的常态性
  E33 缓存命中延迟收益           命中 cache_read vs 冷启动 cache_creation 的响应延迟差
  E34 opus 悬案闭合 + 长度上限    opus-5 大 token 请求返回 input=0；haiku 超长前缀无长度上限

端点与密钥不落盘：OR_BASE_URL（Anthropic 兼容端点 base）+ OR_KEY 环境变量。
模型走环境变量：OR_MODEL（主模型，低档省钱）、OR_OPUS_MODEL（E34 上探用高档模型）。
OR_ONLY=exp30,exp31,... 可单独跑。
输出：实验结果/rich2_<时间戳>.jsonl
"""
import os, sys, json, time, random, threading, statistics, urllib.request, urllib.error
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")
KEY = os.environ.get("OR_KEY", "")
_BASE = os.environ.get("OR_BASE_URL", "").rstrip("/")   # Anthropic 兼容端点 base，如 https://<host>/api/v1
URL = _BASE + "/messages" if _BASE else ""
OUT_DIR = os.environ.get("OR_OUT", "./实验结果")
MODEL = os.environ.get("OR_MODEL", "")                  # 主模型（低档省钱）
OPUS = os.environ.get("OR_OPUS_MODEL", "")              # 高档模型（E34 上探）

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


def post(body, retries=4):
    h = {"Authorization": "Bearer " + KEY, "Content-Type": "application/json",
         "anthropic-beta": "prompt-caching-2024-07-31"}
    last = None
    for a in range(retries):
        try:
            req = urllib.request.Request(URL, json.dumps(body).encode("utf-8"), h, method="POST")
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r).get("usage", {}) or {}
        except urllib.error.HTTPError as e:
            last = RuntimeError("HTTP %d: %s" % (e.code, e.read().decode("utf-8", "ignore")[:150]))
            if e.code in (408, 429, 500, 502, 503, 504):
                time.sleep(2 ** a * 2); continue
            break
        except Exception as e:
            last = e; time.sleep(2 ** a * 2); continue
    raise last


def hit(u):
    cr = u.get("cache_read_input_tokens") or 0
    cc = u.get("cache_creation_input_tokens") or 0
    ti = u.get("input_tokens") or 0
    tot = cr + cc + ti
    return cr, cc, ti, (round(cr / tot, 4) if tot else 0.0)


def log(rows, jf):
    with open(jf, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def sys_mem(s, n=5000):
    return [{"type": "text", "text": "You are a helpful assistant."},
            {"type": "text", "text": f"[MEM {s}]\n" + mem(n), "cache_control": {"type": "ephemeral"}}]


# ---------------- E30 并发冷启动写入竞争 ----------------
def exp30(rows):
    print("\n▶ E30 并发冷启动写入竞争")
    for N in [1, 3, 6]:
        s = salt(); system = sys_mem(s)
        barrier = threading.Barrier(N)
        results = {}

        def worker(i):
            body = {"model": MODEL, "session_id": f"e30-{N}-{s}-{i}", "max_tokens": 16,
                    "system": system, "messages": [{"role": "user", "content": f"Reply {i}."}]}
            try:
                barrier.wait(timeout=5)
            except Exception:
                pass
            try:
                results[i] = hit(post(body))
            except Exception as e:
                results[i] = ("ERR", str(e))

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in ts: t.start()
        for t in ts: t.join()
        cc_cnt = sum(1 for v in results.values() if isinstance(v, tuple) and len(v) == 4 and v[1] > 0)
        cr_cnt = sum(1 for v in results.values() if isinstance(v, tuple) and len(v) == 4 and v[0] > 0)
        for i in range(N):
            v = results[i]
            if isinstance(v, tuple) and len(v) == 4:
                rows.append({"exp": "E30", "case": f"N{N}_req{i}", "cr": v[0], "cc": v[1], "ti": v[2], "hit": v[3]})
        print(f"  N={N}: 写入={cc_cnt} 命中={cr_cnt}")


# ---------------- E31 前缀稳定性边界 ----------------
def exp31(rows):
    print("\n▶ E31 前缀稳定性边界")
    s = salt()
    memA = f"[MEM {s}]\n" + mem(5000)
    sid = f"e31-{s}"

    def sys_with(text):
        return [{"type": "text", "text": "You are a helpful assistant."},
                {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]

    def post_sys(text, sid_):
        return post({"model": MODEL, "session_id": sid_, "max_tokens": 16, "system": sys_with(text),
                     "messages": [{"role": "user", "content": "hi"}]})

    u = post_sys(memA, sid); cr, cc, ti, _ = hit(u)
    rows.append({"exp": "E31", "case": "A_build", "cr": cr, "cc": cc, "ti": ti})
    u = post_sys(memA, sid); cr, cc, ti, h = hit(u)
    rows.append({"exp": "E31", "case": "A_same", "cr": cr, "cc": cc, "ti": ti, "hit": h})
    mut = memA[:8] + ("X" if memA[8] != "X" else "Y") + memA[9:]
    u = post_sys(mut, f"e31b-{s}"); cr, cc, ti, h = hit(u)
    rows.append({"exp": "E31", "case": "B_mutate1", "cr": cr, "cc": cc, "ti": ti, "hit": h})
    u = post({"model": MODEL, "session_id": sid, "max_tokens": 16, "system": sys_with(memA),
              "messages": [{"role": "user", "content": "different tail"}]})
    cr, cc, ti, h = hit(u)
    rows.append({"exp": "E31", "case": "C_tail_change", "cr": cr, "cc": cc, "ti": ti, "hit": h})
    print("  A 同前缀 / B 改1字符 / C 尾部变 → 看 jsonl")


# ---------------- E32 并发冷启动·多轮统计 ----------------
def exp32(rows):
    print("\n▶ E32 并发冷启动·多轮统计")
    for N in [3, 6]:
        repeat = 0; total_cc = 0
        for rnd in range(4):
            s = salt(); system = sys_mem(s)
            barrier = threading.Barrier(N); results = {}
            def worker(i):
                body = {"model": MODEL, "session_id": f"e32-{N}-{rnd}-{s}-{i}", "max_tokens": 16,
                        "system": system, "messages": [{"role": "user", "content": f"Reply {i}."}]}
                try:
                    barrier.wait(timeout=5)
                except Exception:
                    pass
                try:
                    results[i] = hit(post(body))
                except Exception:
                    results[i] = None
            ts = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
            for t in ts: t.start()
            for t in ts: t.join()
            cc_cnt = sum(1 for v in results.values() if v and v[1] > 0)
            cr_cnt = sum(1 for v in results.values() if v and v[0] > 0)
            rows.append({"exp": "E32", "case": f"N{N}_r{rnd+1}", "cc": cc_cnt, "cr": cr_cnt})
            total_cc += cc_cnt
            if cc_cnt > 1: repeat += 1
            print(f"  N={N} 第{rnd+1}轮: 写入={cc_cnt} 命中={cr_cnt}")
        print(f"  → N={N}: {repeat}/4 轮重复，平均 {total_cc/4:.2f} 次写入/轮")


# ---------------- E33 缓存命中延迟收益 ----------------
def exp33(rows):
    print("\n▶ E33 缓存命中延迟收益")
    s = salt(); system = sys_mem(s); sid = f"e33-{s}"
    post({"model": MODEL, "session_id": sid, "max_tokens": 1, "system": system,
          "messages": [{"role": "user", "content": "warm"}]})
    hit_t, cold_t = [], []
    for i in range(5):
        t0 = time.time()
        post({"model": MODEL, "session_id": sid, "max_tokens": 16, "system": system,
              "messages": [{"role": "user", "content": f"q{i}"}]})
        hit_t.append(time.time() - t0)
    for i in range(5):
        s2 = salt(); sys2 = sys_mem(s2)
        t0 = time.time()
        post({"model": MODEL, "session_id": f"e33c-{s2}", "max_tokens": 16, "system": sys2,
              "messages": [{"role": "user", "content": f"q{i}"}]})
        cold_t.append(time.time() - t0)
    hm, cm = statistics.median(hit_t), statistics.median(cold_t)
    print(f"  命中 {hm:.2f}s vs 冷启动 {cm:.2f}s（中位数），差 {cm-hm:+.2f}s")
    rows.append({"exp": "E33", "case": "summary", "hit_median": round(hm, 3), "cold_median": round(cm, 3)})


# ---------------- E34 opus 悬案闭合 + haiku 长度上限 ----------------
def exp34(rows):
    print("\n▶ E34 opus 悬案闭合 + 长度上限")
    def probe(model, n, tag):
        s = salt(); system = sys_mem(s, n)
        sid = f"{tag}-{n}-{s}"
        u = post({"model": model, "session_id": sid, "max_tokens": 4, "system": system,
                  "messages": [{"role": "user", "content": "hi"}]})
        cr, cc, ti = hit(u)
        u2 = post({"model": model, "session_id": sid, "max_tokens": 4, "system": system,
                   "messages": [{"role": "user", "content": "hi again"}]})
        cr2, cc2, ti2 = hit(u2)
        rows.append({"exp": tag, "case": f"tok{n}", "model": model, "actual_tokens": ti, "build_cc": cc, "reread_cr": cr2})
        return ti, cc, cr2
    if OPUS:
        for n in [8000, 12000, 16000, 20000]:
            ti, cc, cr = probe(OPUS, n, "E34_opus")
            print(f"  opus 目标{n}token(实际{ti}): cc={cc} cr={cr} {'缓存✅' if (cc or cr) else '异常/不缓存'}")
            if cc or cr:
                break
            time.sleep(1)
    for n in [10000, 20000]:
        ti, cc, cr = probe(MODEL, n, "E34_haiku")
        print(f"  haiku 目标{n}token(实际缓存{cc or cr}): {'缓存✅' if (cc or cr) else '不缓存'}")
        time.sleep(1)


EXPERIMENTS = [
    ("exp30", "并发冷启动写入", exp30),
    ("exp31", "前缀字节边界", exp31),
    ("exp32", "并发冷启动多轮", exp32),
    ("exp33", "缓存命中延迟", exp33),
    ("exp34", "opus闭合+长度上限", exp34),
]


def main():
    if not KEY or not _BASE or not MODEL:
        print("ERROR: 请先 export OR_KEY / OR_BASE_URL / OR_MODEL"); return
    only = set((os.environ.get("OR_ONLY") or "").split(",")) if os.environ.get("OR_ONLY") else None
    os.makedirs(OUT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    jf = os.path.join(OUT_DIR, f"rich2_{ts}.jsonl")
    print(f"模型={MODEL}  输出={jf}")
    rows = []
    for eid, desc, fn in EXPERIMENTS:
        if only and eid not in only:
            continue
        try:
            r = []; fn(r); rows += r; log(r, jf)
        except Exception as e:
            print(f"  !! {eid} 失败: {e}")
    print("\n完成。原始数据：" + jf)


if __name__ == "__main__":
    main()
