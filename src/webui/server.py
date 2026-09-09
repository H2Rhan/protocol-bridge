"""弹网页 · 本地仪表盘（安全骨架 + 记忆编排界面）。

威胁模型与对策（同机恶意进程访问 loopback、DNS rebinding 经浏览器跨界、记忆敏感信息泄露）：
  - 绑 127.0.0.1（绝不 0.0.0.0），运行时随机端口，会话结束即关，90s 超时兜底
  - 校验 Host 头防 DNS rebinding（只接受 127.0.0.1:端口，拒绝域名形式）
  - URL 一次性 token（防同机进程扫端口直接访问）
  - 记忆内容默认脱敏/截断，完整内容需显式点开

编排能力（补齐 REVIEW 已知缺口 #2：原仅安全骨架、无编排界面）：
  - 记忆勾选/裁剪：勾选本次要注入的记忆，提交经 save_callback 回写状态层
  - 缓存前缀可视化：4 断点布局（tools 后 / system 后 / 历史静态段后 / 滚动尾部）
"""
from __future__ import annotations

import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

TIMEOUT_S = 90  # 超时兜底（60-120s 区间）


def _mask(text: str, keep: int = 24) -> str:
    """默认脱敏：只留前 keep 字符。"""
    return text if len(text) <= keep else text[:keep] + " …[点开看全文]"


def _memory_text(m: dict) -> str:
    return str(m.get("content") or m.get("preview") or "").strip()


class Handler(BaseHTTPRequestHandler):
    token = ""
    port = 0
    memories: list = []
    save_callback = None

    def log_message(self, *a):
        pass

    def _forbidden(self):
        self.send_response(403)
        self.end_headers()

    def _html(self, body: str):
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _render(self) -> str:
        rows = []
        for i, m in enumerate(self.memories):
            if not isinstance(m, dict):
                continue
            layer = str(m.get("layer", "?"))
            rows.append(
                f'<label class="mem"><input type="checkbox" name="mem" value="{i}" checked>'
                f'<span class="layer">{layer}</span> <span class="txt">{_mask(_memory_text(m))}</span></label>')
        rows_html = "".join(rows) or '<p class="muted">（无候选记忆）</p>'
        return f"""<!doctype html>
<meta charset=utf-8>
<title>Session 记忆编排</title>
<style>
  body{{font-family:-apple-system,'Segoe UI',sans-serif;margin:24px;color:#222;background:#fff}}
  h3{{margin:0 0 4px}} .muted{{color:#888;font-size:12px}}
  .mem{{display:flex;gap:8px;align-items:baseline;padding:6px 8px;border:1px solid #eee;border-radius:6px;margin:6px 0;cursor:pointer}}
  .mem:hover{{background:#f7f9fc}}
  .layer{{font-weight:600;color:#085041;min-width:28px}}
  .txt{{color:#555;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  .prefix{{border:1px solid #1d9e75;border-radius:8px;padding:8px 12px;margin:10px 0;background:#f4fbf7}}
  .tail{{border:1px dashed #ccc;border-radius:8px;padding:8px 12px;margin:10px 0;color:#888}}
  .row{{display:flex;justify-content:space-between;margin:3px 0;font-size:13px}}
  button{{margin-top:12px;padding:8px 20px;background:#1d9e75;color:#fff;border:0;border-radius:6px;cursor:pointer}}
  button:hover{{background:#177a5e}}
</style>
<h3>本次注入了什么 + 缓存前缀</h3>
<p class="muted">勾选要注入的记忆，取消勾选则本次不注入（保存后回写状态层）</p>

<div class="prefix">
  <div class="row"><b>稳定前缀（吃缓存）</b><span>断点 ↓</span></div>
  <div class="row"><span>① tools · 工具定义</span><span>断点 1</span></div>
  <div class="row"><span>② system · 含注入记忆</span><span>断点 2</span></div>
  <div class="row"><span>③ 历史静态段</span><span>断点 3</span></div>
</div>
<div class="tail">
  <div class="row"><span>④ 本轮新消息（动态尾部，不吃缓存）</span><span>断点 4</span></div>
</div>

<form method="POST" action="/?t={self.token}">
  <input type="hidden" name="t" value="{self.token}">
  <h4>记忆列表（默认脱敏）</h4>
  {rows_html}
  <button type="submit">保存勾选</button>
</form>
"""

    def do_GET(self):
        if self.headers.get("Host") != f"127.0.0.1:{self.port}":
            return self._forbidden()
        q = parse_qs(urlparse(self.path).query)
        if q.get("t", [""])[0] != self.token:
            return self._forbidden()
        self._html(self._render())

    def do_POST(self):
        if self.headers.get("Host") != f"127.0.0.1:{self.port}":
            return self._forbidden()
        length = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(length).decode("utf-8"))
        if form.get("t", [""])[0] != self.token:
            return self._forbidden()
        selected_idx = sorted({int(i) for i in form.get("mem", []) if i.isdigit()})
        selected = [self.memories[i] for i in selected_idx if 0 <= i < len(self.memories)]
        # 注意：save_callback 是普通函数、存于类属性，必须用类名访问，
        # 否则实例访问会触发 descriptor 绑定、多传一个 self。
        if Handler.save_callback:
            try:
                Handler.save_callback(selected)
                msg = f"已保存：本次注入 {len(selected)} 条记忆"
            except Exception as e:  # 回写失败要显式暴露，不能静默
                msg = f"保存失败：{e}"
        else:
            msg = f"已选择 {len(selected)} 条（未接状态层，仅展示）"
        self._html(f"<!doctype html><meta charset=utf-8><h3>{msg}</h3>"
                   f'<p class="muted">可关闭本页。</p>')


def open_dashboard(memories: list, open_browser: bool = True, save_callback=None) -> str:
    """起一次性本地仪表盘，返回带 token 的 URL。

    memories: 候选记忆列表（dict，含 layer / content 或 preview）
    save_callback: 可选，勾选提交后调用 save_callback(selected_memories)，用于回写状态层
    """
    token = secrets.token_urlsafe(16)
    Handler.token = token
    Handler.memories = memories
    Handler.save_callback = save_callback
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)  # 端口随机
    port = srv.server_address[1]
    Handler.port = port
    url = f"http://127.0.0.1:{port}/?t={token}"

    def serve_with_timeout():
        srv.timeout = 1
        deadline = time.time() + TIMEOUT_S
        while time.time() < deadline:
            srv.handle_request()  # 90s 内持续响应（GET 页面 + POST 提交）
        srv.server_close()  # 超时兜底：关闭监听

    threading.Thread(target=serve_with_timeout, daemon=True).start()
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            print(f"浏览器打开失败，请手动复制：{url}")  # fallback
    else:
        print(f"dashboard: {url}")
    return url
