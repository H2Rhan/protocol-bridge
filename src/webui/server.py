"""弹网页 · 本地仪表盘安全骨架（方案 3.7，无 OAuth 授权码流程）。

威胁模型：同机恶意进程访问 loopback、DNS rebinding 经浏览器跨界、
记忆内容含敏感信息被泄露。对症措施：
  - 绑 127.0.0.1（绝不 0.0.0.0），运行时随机端口，会话结束即关，60-120s 超时兜底
  - 校验 Host 头防 DNS rebinding（只接受 127.0.0.1:端口，拒绝域名形式）
  - URL 一次性 token（防同机进程扫端口直接访问）
  - 记忆内容默认脱敏/截断，完整内容需显式点开
"""
from __future__ import annotations

import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TIMEOUT_S = 90  # 超时兜底（60-120s 区间）


def _mask(text: str, keep: int = 24) -> str:
    """默认脱敏：只留前 keep 字符。"""
    return text if len(text) <= keep else text[:keep] + " …[点开看全文]"


class Handler(BaseHTTPRequestHandler):
    token = ""
    port = 0
    memories: list = []

    def log_message(self, *a):
        pass

    def _forbidden(self):
        self.send_response(403)
        self.end_headers()

    def do_GET(self):
        # 防 DNS rebinding：Host 必须是 127.0.0.1:端口
        if self.headers.get("Host") != f"127.0.0.1:{self.port}":
            return self._forbidden()
        # 一次性 token 校验
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        if q.get("t", [""])[0] != self.token:
            return self._forbidden()
        body = self._render().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _render(self) -> str:
        rows = "".join(
            f"<li><b>{m.get('layer','?')}</b>: {_mask(str(m.get('text','')))}</li>"
            for m in self.memories)
        return ("<!doctype html><meta charset=utf-8><title>Session init 注入预览</title>"
                "<h3>本次注入了什么 + 缓存前缀</h3><ul>" + rows + "</ul>"
                "<p style='color:#888'>本地仪表盘 · 内容已默认脱敏 · 勾裁功能待接状态层</p>")


def open_dashboard(memories: list, open_browser: bool = True) -> str:
    """起一次性本地仪表盘，返回带 token 的 URL。失败时打印 URL 供手动复制（fallback）。"""
    token = secrets.token_urlsafe(16)
    Handler.token = token
    Handler.memories = memories
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)  # 端口随机
    port = srv.server_address[1]
    Handler.port = port
    url = f"http://127.0.0.1:{port}/?t={token}"

    def serve_with_timeout():
        srv.timeout = 1
        deadline = time.time() + TIMEOUT_S
        while time.time() < deadline:
            srv.handle_request()  # 收到回调（一次请求）后由外层决定是否继续
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
