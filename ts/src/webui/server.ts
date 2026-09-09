/** 弹网页 · 本地仪表盘（安全骨架 + 记忆编排界面；TS 移植自 src/webui/server.py）。
 *
 * 威胁模型与对策（同机恶意进程访问 loopback、DNS rebinding 经浏览器跨界、记忆敏感信息泄露）：
 *   - 绑 127.0.0.1（绝不 0.0.0.0），运行时随机端口，会话结束即关，90s 超时兜底
 *   - 校验 Host 头防 DNS rebinding（只接受 127.0.0.1:端口，拒绝域名形式）
 *   - URL 一次性 token（防同机进程扫端口直接访问）
 *   - 记忆内容默认脱敏/截断，完整内容需显式点开
 *
 * 编排能力（补齐 REVIEW 已知缺口 #2：原仅安全骨架、无编排界面）：
 *   - 记忆勾选/裁剪：勾选本次要注入的记忆，提交经 saveCallback 回写状态层
 *   - 缓存前缀可视化：4 断点布局（tools 后 / system 后 / 历史静态段后 / 滚动尾部）
 */

import http from "node:http";
import { randomBytes } from "node:crypto";
import { exec } from "node:child_process";
import type { Json } from "../ir/model.ts";

export const TIMEOUT_S = 90; // 超时兜底（60-120s 区间）

/** 默认脱敏：只留前 keep 字符。 */
export function mask(text: string, keep = 24): string {
  return text.length <= keep ? text : text.slice(0, keep) + " …[点开看全文]";
}

function memoryText(m: Json): string {
  return String(m.content ?? m.preview ?? "").trim();
}

export interface DashboardOptions {
  openBrowser?: boolean;
  saveCallback?: ((selected: Json[]) => void) | null;
  /** 测试用：指定超时秒数（默认 90） */
  timeoutS?: number;
}

function render(memories: Json[], token: string): string {
  const rows: string[] = [];
  memories.forEach((m, i) => {
    if (m === null || typeof m !== "object") return;
    const layer = String(m.layer ?? "?");
    rows.push(
      `<label class="mem"><input type="checkbox" name="mem" value="${i}" checked>` +
      `<span class="layer">${layer}</span> <span class="txt">${mask(memoryText(m))}</span></label>`);
  });
  const rowsHtml = rows.join("") || '<p class="muted">（无候选记忆）</p>';
  return `<!doctype html>
<meta charset=utf-8>
<title>Session 记忆编排</title>
<style>
  body{font-family:-apple-system,'Segoe UI',sans-serif;margin:24px;color:#222;background:#fff}
  h3{margin:0 0 4px} .muted{color:#888;font-size:12px}
  .mem{display:flex;gap:8px;align-items:baseline;padding:6px 8px;border:1px solid #eee;border-radius:6px;margin:6px 0;cursor:pointer}
  .mem:hover{background:#f7f9fc}
  .layer{font-weight:600;color:#085041;min-width:28px}
  .txt{color:#555;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .prefix{border:1px solid #1d9e75;border-radius:8px;padding:8px 12px;margin:10px 0;background:#f4fbf7}
  .tail{border:1px dashed #ccc;border-radius:8px;padding:8px 12px;margin:10px 0;color:#888}
  .row{display:flex;justify-content:space-between;margin:3px 0;font-size:13px}
  button{margin-top:12px;padding:8px 20px;background:#1d9e75;color:#fff;border:0;border-radius:6px;cursor:pointer}
  button:hover{background:#177a5e}
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

<form method="POST" action="/?t=${token}">
  <input type="hidden" name="t" value="${token}">
  <h4>记忆列表（默认脱敏）</h4>
  ${rowsHtml}
  <button type="submit">保存勾选</button>
</form>
`;
}

/** 起一次性本地仪表盘，resolve 出带 token 的 URL 与服务句柄（测试可提前关）。
 *
 * memories: 候选记忆列表（含 layer / content 或 preview）
 * saveCallback: 可选，勾选提交后调用 saveCallback(selectedMemories)，用于回写状态层
 *
 * 注意：listen 是异步的，必须 await 返回后再用 url/port（否则拿到 port 0）。
 */
export async function openDashboard(memories: Json[], opts: DashboardOptions = {}):
    Promise<{ url: string; close: () => void; port: number }> {
  const token = randomBytes(16).toString("base64url");
  const timeoutS = opts.timeoutS ?? TIMEOUT_S;

  const forbidden = (res: http.ServerResponse) => {
    res.writeHead(403);
    res.end();
  };

  const srv = http.createServer((req, res) => {
    const port = (srv.address() as { port: number }).port;
    // Host 头校验：防 DNS rebinding（只接受 127.0.0.1:端口，拒绝域名形式）
    if (req.headers.host !== `127.0.0.1:${port}`) return forbidden(res);
    const u = new URL(req.url ?? "/", `http://127.0.0.1:${port}`);
    if (req.method === "GET") {
      if (u.searchParams.get("t") !== token) return forbidden(res);
      const body = Buffer.from(render(memories, token), "utf-8");
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8",
                           "Content-Length": String(body.length) });
      res.end(body);
      return;
    }
    if (req.method === "POST") {
      const chunks: Buffer[] = [];
      req.on("data", (c) => chunks.push(c));
      req.on("end", () => {
        const form = new URLSearchParams(Buffer.concat(chunks).toString("utf-8"));
        if (form.get("t") !== token) return forbidden(res);
        const selectedIdx = [...new Set(
          form.getAll("mem").map(Number).filter((n) => Number.isInteger(n)))]
          .sort((a, b) => a - b);
        const selected = selectedIdx
          .filter((i) => i >= 0 && i < memories.length)
          .map((i) => memories[i]);
        let msg: string;
        if (opts.saveCallback) {
          try {
            opts.saveCallback(selected);
            msg = `已保存：本次注入 ${selected.length} 条记忆`;
          } catch (e) {
            // 回写失败要显式暴露，不能静默
            msg = `保存失败：${e}`;
          }
        } else {
          msg = `已选择 ${selected.length} 条（未接状态层，仅展示）`;
        }
        res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
        res.end(`<!doctype html><meta charset=utf-8><h3>${msg}</h3>` +
                '<p class="muted">可关闭本页。</p>');
      });
      return;
    }
    res.writeHead(405).end();
  });

  await new Promise<void>((resolve) => srv.listen(0, "127.0.0.1", resolve));
  const port = (srv.address() as { port: number }).port;
  const url = `http://127.0.0.1:${port}/?t=${token}`;

  if (opts.openBrowser !== false) {
    // 平台各自的默认浏览器打开方式；失败则退到打印 URL
    const cmd = process.platform === "win32" ? `start "" "${url}"`
      : process.platform === "darwin" ? `open "${url}"`
      : `xdg-open "${url}"`;
    exec(cmd, (err) => {
      if (err) console.log(`浏览器打开失败，请手动复制：${url}`);
    });
  } else {
    console.log(`dashboard: ${url}`);
  }

  // 超时兜底：到点关闭监听（90s 内持续响应 GET 页面 + POST 提交）
  const timer = setTimeout(() => srv.close(), timeoutS * 1000);
  timer.unref();

  return { url, port, close: () => { clearTimeout(timer); srv.close(); } };
}
