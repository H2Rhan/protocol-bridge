"""端到端冒烟：起 mock 后端 + 网关两个子进程，跑四条链路，退出码 0/1。

单元测试跑在进程内（monkeypatch 掉 STORE / BACKEND_URL），验的是逻辑；
这个脚本验的是**入口能不能真的起来**：`python -m src.gateway.server`
和 `tools/mock_backend.py` 这两个真入口，以及跨进程 HTTP 链路。

用法：
  python tools/smoke_e2e.py
环境变量：PYTHON 指定解释器；PB_DB / PB_METRICS 由本脚本自动指向临时目录。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
GW_PORT = 8099
MOCK_PORT = 9100

PROCS: list = []


def _wait_port(port: int, timeout: float = 15.0) -> bool:
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def start():
    tmp = tempfile.mkdtemp(prefix="pb-smoke-")
    env = dict(os.environ,
               PYTHONIOENCODING="utf-8", PB_DIRECT="1", PYTHONUNBUFFERED="1",
               PB_DB=os.path.join(tmp, "s.db"),
               PB_METRICS=os.path.join(tmp, "m.jsonl"),
               PB_PORT=str(GW_PORT))
    mock = subprocess.Popen([PY, os.path.join(ROOT, "tools", "mock_backend.py")],
                            cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    PROCS.append(mock)
    if not _wait_port(MOCK_PORT):
        raise RuntimeError("mock 后端未能在 15s 内监听 9100")
    gw = subprocess.Popen([PY, "-m", "src.gateway.server"], cwd=ROOT, env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    PROCS.append(gw)
    if not _wait_port(GW_PORT):
        raise RuntimeError(f"网关未能在 15s 内监听 {GW_PORT}")
    return tmp


def stop(tmp: str):
    for p in PROCS:
        p.terminate()
    for p in PROCS:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
    shutil.rmtree(tmp, ignore_errors=True)


def post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{GW_PORT}{path}", data=data,
                                 headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


CHECKS: list = []


def check(name: str, ok: bool, detail: str = ""):
    CHECKS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


def main() -> int:
    print(f"smoke: mock=127.0.0.1:{MOCK_PORT}  gateway=127.0.0.1:{GW_PORT}")
    tmp = start()
    try:
        print("\n1) openai_chat -> openai_chat（同协议直通）")
        r = post("/v1/openai_chat/to/openai_chat",
                 {"model": "mock", "messages": [{"role": "user", "content": "hi"}]})
        check("usage 归一有值", (r.get("usage") or {}).get("prompt_tokens", 0) > 0,
              str(r.get("usage")))
        check("无静默丢弃", not r["_bridge"]["dropped"], str(r["_bridge"]["dropped"]))

        print("\n2) openai_response -> openai_chat（跨协议）")
        r = post("/v1/openai_response/to/openai_chat",
                 {"model": "mock", "input": [{"type": "message", "role": "user",
                                              "content": "hi"}]})
        check("跨协议转换成功", r.get("choices") is not None or r.get("id") is not None)

        print("\n3) openai_response -> openai_response（多轮 previous_response_id）")
        r1 = post("/v1/openai_response/to/openai_response",
                  {"model": "mock", "input": [{"type": "message", "role": "user",
                                               "content": "第一轮"}]})
        check("首轮无历史可重放", r1["_bridge"]["replayed_messages"] == 0,
              f"replayed={r1['_bridge']['replayed_messages']}")
        r2 = post("/v1/openai_response/to/openai_response",
                  {"model": "mock", "previous_response_id": r1.get("id"),
                   "input": [{"type": "message", "role": "user", "content": "第二轮"}]})
        check("第 2 轮重放 2 条", r2["_bridge"]["replayed_messages"] == 2,
              f"replayed={r2['_bridge']['replayed_messages']}")
        r3 = post("/v1/openai_response/to/openai_response",
                  {"model": "mock", "previous_response_id": r2.get("id"),
                   "input": [{"type": "message", "role": "user", "content": "第三轮"}]})
        check("第 3 轮重放 4 条（历史持续累积）",
              r3["_bridge"]["replayed_messages"] == 4,
              f"replayed={r3['_bridge']['replayed_messages']}")

        print("\n4) anthropic -> anthropic（预热 max_tokens:0 + 冲突拦截）")
        warm = {"model": "mock", "max_tokens": 0,
                "system": [{"type": "text", "text": "s" * 30,
                            "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user", "content": "warmup"}]}
        r = post("/v1/anthropic/to/anthropic", warm)
        check("预热零输出计费", (r.get("usage") or {}).get("output_tokens") == 0,
              str(r.get("usage")))
        check("预热标记为 warmup 轮", r["_bridge"]["warmup"] is True)
        check("预热写了缓存", (r.get("usage") or {}).get("cache_creation_input_tokens", 0) > 0,
              str(r.get("usage")))
        bad = dict(warm, thinking={"type": "enabled", "budget_tokens": 1024})
        try:
            post("/v1/anthropic/to/anthropic", bad)
            check("thinking 与预热冲突被拦截", False, "请求通过了，未拦截")
        except urllib.error.HTTPError as e:
            check("thinking 与预热冲突被拦截", e.code == 400, f"HTTP {e.code}")

        print("\n5) 非法路由 / 未知协议（必须回 400，不能掐断连接）")
        try:
            post("/v1/openai_chat/to/does_not_exist", {"model": "mock", "messages": []})
            check("未知 target 回 400", False, "请求通过了")
        except urllib.error.HTTPError as e:
            check("未知 target 回 400", e.code == 400, f"HTTP {e.code}")
        except Exception as e:  # RemoteDisconnected 等
            check("未知 target 回 400", False, f"{type(e).__name__}: {e}")
    finally:
        stop(tmp)

    failed = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} 通过")
    if failed:
        print("失败项：" + "、".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
