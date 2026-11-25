import { test } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const { createBrowserHost, isAllowedPageUrl, MAX_TABS } = require(
  path.join(here, "..", "..", "src-electron", "browser.js"),
);
const { startHostBridge } = require(path.join(here, "..", "..", "src-electron", "browser-bridge.js"));

/**
 * 内置浏览器宿主的单测（2026-09-16）。
 *
 * 用假 BrowserWindow / WebContentsView / debugger 驱动真实代码路径，重点验证三件事：
 *   1. 窗口与标签的状态机（开、切、关、上限）；
 *   2. AI 动作分发只认白名单，且真的走到了 CDP 命令；
 *   3. 宿主桥的鉴权边界（无 token / 带 Origin / 超大结果）不会被绕过。
 * 真机上的渲染效果由人工在应用里验收，这里不假装能替代它。
 */

let wcSeq = 0;

class FakeDebugger extends EventEmitter {
  constructor() {
    super();
    this.attached = false;
    this.calls = [];
    // 默认应答：readyState=complete 让 waitForReady 立刻返回，避免单测里空转 20 秒
    this.handlers = {
      "Runtime.evaluate": () => ({ result: { value: "complete" } }),
      "Page.captureScreenshot": () => ({ data: "" }),
    };
  }
  isAttached() { return this.attached; }
  attach(version) { this.attached = true; this.version = version; }
  detach() { this.attached = false; }
  sendCommand(method, params) {
    this.calls.push([method, params]);
    // 真实场景：某些 CDP 命令可以永远不回应（实测 Page.enable 如此）。
    // 用 neverResolve 集合把它复现出来——否则「挂死」永远不会被单测覆盖。
    if (this.neverResolve && this.neverResolve.has(method)) return new Promise(() => {});
    const handler = this.handlers[method];
    return Promise.resolve(handler ? handler(params) : {});
  }
  callsOf(method) { return this.calls.filter((c) => c[0] === method).map((c) => c[1]); }
}

class FakePageContents extends EventEmitter {
  constructor() { super(); this.id = ++wcSeq; this.destroyed = false; this.debugger = new FakeDebugger(); this.scripts = []; }
  isDestroyed() { return this.destroyed; }
  setWindowOpenHandler(handler) { this.windowOpenHandler = handler; return handler; }
  executeJavaScript(script) {
    this.scripts.push(script);
    // 按脚本内容给默认应答，未显式设置 scriptResult 的路径也能走到确定分支
    if (script.includes("location.href") && !script.includes("document.title")) {
      return Promise.resolve("https://example.com/");
    }
    // readPage 的脚本会同时取 location.href / document.title / body.innerText，
    // 必须返回页面对象，不能落进上面的 URL 分支（否则会被当成 readyState）。
    if (script.includes("body.innerText")) {
      const r = this.scriptResult;
      return Promise.resolve(r && typeof r === "object" ? r : { url: "", title: "", text: "" });
    }
    return Promise.resolve(this.scriptResult);
  }
  loadURL() { return Promise.resolve(); }
  reload() { this.reloaded = (this.reloaded || 0) + 1; }
  stop() { this.stopped = true; }
  close() { this.destroyed = true; this.emit("destroyed"); }
  getURL() { return "about:blank"; }
}

class FakeWebContentsView {
  constructor(options) { this.options = options; this.webContents = new FakePageContents(); }
  setBounds(bounds) { this.bounds = bounds; }
}

class FakeContentView {
  constructor() { this.children = []; }
  addChildView(view) { if (!this.children.includes(view)) this.children.push(view); }
  removeChildView(view) { this.children = this.children.filter((v) => v !== view); }
}

class FakeWindow extends EventEmitter {
  static instances = [];
  constructor(options = {}) {
    super();
    FakeWindow.instances.push(this);
    this.options = options;
    this.contentView = new FakeContentView();
    this.webContents = new EventEmitter();
    this.webContents.sent = [];
    this.webContents.send = (channel, payload) => this.webContents.sent.push([channel, payload]);
    this.destroyed = false;
    this.minimized = false;
    this.focused = false;
    this.shown = 0;
  }
  isDestroyed() { return this.destroyed; }
  isMinimized() { return this.minimized; }
  restore() { this.minimized = false; }
  show() { this.shown += 1; }
  showInactive() { this.shown += 1; }
  focus() { this.focused = true; }
  destroy() { this.destroyed = true; this.emit("closed"); }
  loadFile(file) { this.loaded = file; }
  setMenuBarVisibility() {}
  getContentBounds() { return { x: 0, y: 0, width: 1200, height: 820 }; }
  getNormalBounds() { return { x: 10, y: 20, width: 1200, height: 820 }; }
  setTitleBarOverlay() {}
}

function makeIpcMain() {
  const handlers = new Map();
  return {
    handlers,
    handle(channel, fn) { handlers.set(channel, fn); },
    invoke(channel, ...args) { return handlers.get(channel)({}, ...args); },
  };
}

function makeHost() {
  const ipcMain = makeIpcMain();
  const appDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "yuncat-browser-test-"));
  const host = createBrowserHost({
    electron: {
      BrowserWindow: FakeWindow,
      WebContentsView: FakeWebContentsView,
      ipcMain,
      nativeTheme: { shouldUseDarkColors: false },
      shell: { openExternal() {} },
      screen: { getAllDisplays: () => [{ workArea: { x: 0, y: 0, width: 1920, height: 1080 } }] },
    },
    appDataDir,
    fs,
    log: { log() {}, warn() {}, error() {} },
  });
  host.wireIpc();
  return { host, ipcMain, appDataDir };
}

test("协议白名单：只放行 http/https/about", () => {
  assert.equal(isAllowedPageUrl("https://example.com/a"), true);
  assert.equal(isAllowedPageUrl("http://127.0.0.1:8000"), true);
  assert.equal(isAllowedPageUrl("about:blank"), true);
  assert.equal(isAllowedPageUrl("file:///C:/Windows/win.ini"), false);
  assert.equal(isAllowedPageUrl("javascript:alert(1)"), false);
  assert.equal(isAllowedPageUrl("data:text/html,<h1>x"), false);
  assert.equal(isAllowedPageUrl("not a url"), false);
});

test("open 建立窗口与首个标签页，并落盘窗口状态", async () => {
  const { host, appDataDir } = makeHost();
  const state = await host.open("");
  assert.equal(state.open, true);
  assert.equal(state.tabs.length, 1);
  assert.equal(state.tabs[0].active, true);
  host.closeAll();
  assert.equal(fs.existsSync(path.join(appDataDir, "browser-window-state.json")), true);
});

test("新建/切换/关闭标签页，关掉最后一个会关窗", async () => {
  const { host } = makeHost();
  await host.open("");
  const first = host.listTabs()[0].id;
  host.newTab("https://example.com/");
  const tabs = host.listTabs();
  assert.equal(tabs.length, 2);
  assert.equal(tabs[1].active, true, "新标签页应当自动激活");

  assert.equal(host.activate(first), true);
  assert.equal(host.getState().active_id, first);

  assert.equal(host.closeTab(first), true);
  assert.equal(host.getState().tabs.length, 1);
  assert.equal(host.getState().tabs[0].active, true, "关闭当前标签页后应由剩下的接管");

  host.closeTab(host.listTabs()[0].id);
  assert.equal(host.getState().open, false, "最后一个标签页关闭时整窗关闭");
});

test("标签页数量上限是显式上限，不是静默失败", async () => {
  const { host } = makeHost();
  await host.open("");
  for (let i = 1; i < MAX_TABS; i++) host.newTab("");
  assert.equal(host.listTabs().length, MAX_TABS);
  assert.throws(() => host.newTab(""), /最多同时打开/);
});

test("navigate 走 CDP 的真实导航命令，并规范化裸域名", async () => {
  const { host } = makeHost();
  await host.open("");
  const view = FakeWindow.instances.at(-1).contentView.children.at(-1);
  view.webContents.debugger.handlers["Runtime.evaluate"] = () => ({ result: { value: "complete" } });
  const result = await host.invoke("navigate", { url: "example.com/path" });
  const navigations = view.webContents.debugger.callsOf("Page.navigate");
  assert.equal(navigations.length, 1);
  assert.equal(navigations[0].url, "https://example.com/path");
  assert.equal(result.loaded, true);
});

test("click 派发真实鼠标事件序列，ref 失效时报可读错误", async () => {
  const { host } = makeHost();
  await host.open("");
  const view = FakeWindow.instances.at(-1).contentView.children.at(-1);
  view.webContents.scriptResult = { tag: "button", type: "submit", x: 10, y: 20, w: 100, h: 40 };
  await host.invoke("click", { ref: 3, selector: "" });
  const mouse = view.webContents.debugger.calls.filter((c) => c[0] === "Input.dispatchMouseEvent").map((c) => c[1].type);
  assert.deepEqual(mouse, ["mouseMoved", "mousePressed", "mouseReleased"]);

  view.webContents.scriptResult = null;
  await assert.rejects(() => host.invoke("click", { ref: 99 }), /重新调用 browser_snapshot/);
});

test("screenshot 返回 base64 与字节数，unknown 动作被拒绝", async () => {
  const { host } = makeHost();
  await host.open("");
  const view = FakeWindow.instances.at(-1).contentView.children.at(-1);
  const png = Buffer.from("fake-png-bytes");
  view.webContents.debugger.handlers["Page.captureScreenshot"] = () => ({ data: png.toString("base64") });
  const shot = await host.invoke("screenshot", {});
  assert.equal(shot.mime, "image/png");
  assert.equal(shot.bytes, png.length);
  assert.equal(Buffer.from(shot.base64, "base64").toString(), "fake-png-bytes");

  await assert.rejects(() => host.invoke("rm -rf /", {}), /未知的浏览器动作/);
});

test("scroll to=bottom 用脚本跳到底，而不是发一堆滚轮事件", async () => {
  const { host } = makeHost();
  await host.open("");
  const view = FakeWindow.instances.at(-1).contentView.children.at(-1);
  view.webContents.debugger.handlers["Runtime.evaluate"] = () => ({ result: { value: 4321 } });
  const result = await host.invoke("scroll", { to: "bottom" });
  const evaluations = view.webContents.debugger.callsOf("Runtime.evaluate");
  assert.equal(evaluations.length, 2, "一次滚动到底应当只做两次求值（跳转 + 读回位置）");
  assert.match(evaluations[0].expression, /scrollTo/);
  assert.match(evaluations[0].expression, /scrollHeight/);
  assert.equal(view.webContents.debugger.callsOf("Input.dispatchMouseEvent").length, 0);
  assert.equal(result.scroll_y, 4321);
});

test("应用侧 IPC 白名单：工具栏命令只认已声明的命令", async () => {
  const { host, ipcMain } = makeHost();
  await host.open("");
  const state = await ipcMain.invoke("browser:app-state");
  assert.equal(state.open, true);
  const opened = await ipcMain.invoke("browser:app-open", "https://example.com/");
  assert.equal(opened.ok, true);
  await assert.rejects(
    async () => ipcMain.invoke("browser:toolbar-command", { cmd: "execShell" }),
    /未知的工具栏命令/,
  );
});

test("宿主桥：无 token 401、带 Origin 403、正常调用透传结果", async () => {
  const seen = [];
  const bridge = await startHostBridge({
    invoke: async (action, params) => {
      seen.push([action, params]);
      if (action === "boom") throw new Error("模拟页面失败");
      return { ok: true, url: "https://example.com/" };
    },
    log: { log() {}, warn() {}, error() {} },
  });
  const url = `http://127.0.0.1:${bridge.port}/host/invoke`;
  const post = (headers, body) => fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(body),
  });

  const health = await fetch(`http://127.0.0.1:${bridge.port}/host/health`);
  assert.equal(health.status, 200);

  const noToken = await post({}, { action: "state" });
  assert.equal(noToken.status, 401, "缺 token 必须拒绝");

  const withOrigin = await post({ Authorization: `Bearer ${bridge.token}`, Origin: "https://evil.example" }, { action: "state" });
  assert.equal(withOrigin.status, 403, "带 Origin 的请求必须拒绝（网页脚本拿不到这条路）");

  const ok = await post({ Authorization: `Bearer ${bridge.token}` }, { action: "navigate", params: { url: "example.com" } });
  assert.equal(ok.status, 200);
  const payload = await ok.json();
  assert.equal(payload.ok, true);
  assert.deepEqual(seen.at(-1), ["navigate", { url: "example.com" }]);

  const failed = await post({ Authorization: `Bearer ${bridge.token}` }, { action: "boom" });
  const failedBody = await failed.json();
  assert.equal(failedBody.ok, false);
  assert.match(failedBody.error, /模拟页面失败/);

  const badJson = await fetch(url, {
    method: "POST", headers: { Authorization: `Bearer ${bridge.token}`, "Content-Type": "application/json" }, body: "{oops",
  });
  assert.equal(badJson.status, 400);

  await bridge.close();
});

/**
 * 2026-10-07 回归：CDP 事件域 enable 不回应时，动作不能被它卡死。
 *
 * 真实故障：应用内实测 Page.enable 每次都 15s 超时，而 attach() 把它当成前置门，
 * 导致 open / read / snapshot / click / screenshot 全部失败——内置浏览器完全不可用。
 * 旧的 FakeDebugger 对任何未声明的命令都返回 {}，所以这个坑一直没被单测发现。
 */
test("Page.enable 永不回应时，read 仍然成功（不再被事件域卡死）", async () => {
  const { host } = makeHost();
  await host.open("");
  const view = FakeWindow.instances.at(-1).contentView.children.at(-1);
  view.webContents.debugger.neverResolve = new Set(["Page.enable"]);
  view.webContents.scriptResult = { url: "https://example.com/", title: "T", text: "hello" };

  const result = await host.invoke("read", { max_chars: 1000 });
  assert.equal(result.text, "hello", "Page.enable 不回应不应影响读取页面");
});

test("Page.enable 永不回应时，snapshot / click 仍然走通", async () => {
  const { host } = makeHost();
  await host.open("");
  const view = FakeWindow.instances.at(-1).contentView.children.at(-1);
  view.webContents.debugger.neverResolve = new Set(["Page.enable"]);
  view.webContents.scriptResult = { tag: "button", type: "submit", x: 1, y: 2, w: 10, h: 10 };

  const box = await host.invoke("click", { ref: 1 });
  assert.equal(box.clicked.tag, "button");
  const mouse = view.webContents.debugger.calls
    .filter((c) => c[0] === "Input.dispatchMouseEvent").map((c) => c[1].type);
  assert.deepEqual(mouse, ["mouseMoved", "mousePressed", "mouseReleased"]);
});

test("事件域 enable 只在首次附着时尝试，成功后被记住", async () => {
  const { host } = makeHost();
  await host.open("");
  const view = FakeWindow.instances.at(-1).contentView.children.at(-1);
  const dbg = view.webContents.debugger;
  view.webContents.scriptResult = { url: "u", title: "t", text: "x" };

  await host.invoke("read", {});
  const firstCount = dbg.callsOf("Runtime.enable").length;
  assert.ok(firstCount >= 1, "首次动作应尝试开启事件域");

  await host.invoke("read", {});
  assert.equal(dbg.callsOf("Runtime.enable").length, firstCount,
    "已成功开启过就不该每个动作都重发 enable");
});
