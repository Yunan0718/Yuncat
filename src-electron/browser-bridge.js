"use strict";
/**
 * browser-bridge.js —— 桌面宿主 ↔ Python 后端的反向通道（loopback HTTP）
 *
 * 为什么需要它：内置浏览器住在 Electron 主进程（Chromium 是 Electron 自带的），
 * 而 Agent 工具跑在 Python 进程里。现有的通道只有「前端 → Python」，没有反过来的路。
 * 与其让 Python 去连 Electron 的远程调试端口（那会把整个应用 UI 也暴露成可编程目标），
 * 不如由主进程开一个**只监听 127.0.0.1**、**必须带随机 token**的小 HTTP 端点，
 * 把「浏览器动作」当作受限白名单命令暴露出去。
 *
 * 安全边界（三条同时成立才放行）：
 *   1. 只绑 127.0.0.1，外部网络不可达；
 *   2. `Authorization: Bearer <token>`，token 每次启动随机生成、只通过环境变量交给自己的
 *      Python 子进程，不落盘；
 *   3. 带 `Origin` 头的请求一律拒绝——网页里的脚本能发出跨站请求，但浏览器一定会附 Origin，
 *      这条直接把它挡在门外（同时也不返回 CORS 头，脚本读不到任何响应）。
 *
 * 命令白名单由调用方（browser.js）的 invoke 决定，本模块不认识任何具体动作。
 */

const http = require("http");
const crypto = require("crypto");

/** 请求体上限：浏览器动作的参数都很小，超过就是异常调用（截图走响应体，不走请求体） */
const MAX_BODY_BYTES = 1024 * 1024;

function safeEqual(a, b) {
  const left = Buffer.from(String(a || ""), "utf8");
  const right = Buffer.from(String(b || ""), "utf8");
  if (left.length !== right.length || left.length === 0) return false;
  return crypto.timingSafeEqual(left, right);
}

function readBody(req, limit) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > limit) {
        reject(new Error("request body too large"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
    "Cache-Control": "no-store",
  });
  res.end(body);
}

function authorized(req, token) {
  if (req.headers.origin || req.headers["sec-fetch-site"]) return { ok: false, status: 403, error: "browser-origin requests are rejected" };
  const header = req.headers.authorization || "";
  const prefix = "Bearer ";
  if (!header.startsWith(prefix)) return { ok: false, status: 401, error: "missing bearer token" };
  if (!safeEqual(header.slice(prefix.length), token)) return { ok: false, status: 401, error: "invalid token" };
  return { ok: true };
}

function createRequestHandler({ invoke, token, log }) {
  return async (req, res) => {
    const url = (req.url || "").split("?")[0];
    if (req.method === "GET" && url === "/host/health") {
      sendJson(res, 200, { ok: true, service: "yuncat-host-bridge" });
      return;
    }
    if (req.method !== "POST" || url !== "/host/invoke") {
      sendJson(res, 404, { ok: false, error: "not found" });
      return;
    }
    const auth = authorized(req, token);
    if (!auth.ok) {
      log.warn(`[host-bridge] rejected request: ${auth.error}`);
      sendJson(res, auth.status, { ok: false, error: auth.error });
      return;
    }
    let payload;
    try {
      const raw = await readBody(req, MAX_BODY_BYTES);
      payload = JSON.parse(raw || "{}");
    } catch (e) {
      sendJson(res, 400, { ok: false, error: `invalid request body: ${e.message}` });
      return;
    }
    const action = String(payload.action || "");
    if (!action) {
      sendJson(res, 400, { ok: false, error: "action is required" });
      return;
    }
    try {
      const data = await invoke(action, payload.params || {});
      sendJson(res, 200, { ok: true, data: data === undefined ? null : data });
    } catch (e) {
      // 桥的职责是把失败如实回传：这里绝不吞异常，Python 侧要能把原因讲给用户
      log.warn(`[host-bridge] action ${action} failed: ${e && e.message}`);
      sendJson(res, 200, { ok: false, error: (e && e.message) || String(e) });
    }
  };
}

/** 启动桥，返回 {port, token, close()}；端口由系统分配（0），避免固定端口冲突 */
function startHostBridge(options) {
  const opts = options || {};
  const log = opts.log || console;
  const token = opts.token || crypto.randomBytes(32).toString("hex");
  const server = http.createServer();
  server.on("request", createRequestHandler({ invoke: opts.invoke, token, log }));
  server.on("clientError", (_err, socket) => { try { socket.destroy(); } catch (e) { /* 降级：连接已断开，忽略 */ } });
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      log.log(`[host-bridge] listening on 127.0.0.1:${address.port}`);
      resolve({
        port: address.port,
        token,
        close: () => new Promise((done) => server.close(() => done())),
      });
    });
  });
}

module.exports = { startHostBridge, MAX_BODY_BYTES };