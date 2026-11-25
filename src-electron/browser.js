"use strict";
/**
 * browser.js —— 内置浏览器宿主（一个窗口 + 多标签 + AI 可操控）
 *
 * 设计取舍（为什么是「Electron 自带 Chromium + CDP」而不是 Playwright / browser-use）：
 * - Electron 本身就带 Chromium，`webContents.debugger` 就是现成的 DevTools Protocol 通道，
 *   零新增依赖、零额外浏览器二进制，和项目「不再随包分发大运行时」的方向一致；
 * - Playwright / Puppeteer 要再带一份浏览器（数百 MB）；browser-use / Stagehand 本身是
 *   另一套 agent 循环，与 Yuncat 自己的工具循环重复；
 * - Skyvern / Lightpanda 是 AGPL-3.0，随安装包分发有法律约束。
 *
 * 安全边界：
 * - 页面跑在独立 session partition（persist:yuncat-browser），与应用 UI 的 cookie/存储隔离，
 *   应用自己的 CSP 也不会被注入到用户要访问的网站上；
 * - 页面 webPreferences 关闭 nodeIntegration、开启 contextIsolation/sandbox；
 * - 只允许 http/https/about:blank，file:/javascript: 一律拦截；
 * - AI 只能通过 browser-bridge 的固定动作白名单操作本窗口，碰不到应用 UI。
 */

const path = require("path");
const cdp = require("./browser-cdp");
const { startHostBridge } = require("./browser-bridge");

/** 顶部工具栏高度（标签条 + 地址栏），页面视图从这里往下排 */
const TOOLBAR_HEIGHT = 76;
/** 同时打开的标签页上限（显式人为上限，防止一次会话开成内存黑洞） */
const MAX_TABS = 12;
const DEFAULT_WIDTH = 1200;
const DEFAULT_HEIGHT = 820;
const MIN_WIDTH = 720;
const MIN_HEIGHT = 420;
/** 「AI 正在操作」指示灯的存活时长 */
const AGENT_ACTIVE_MS = 2500;
const STATE_FILE = "browser-window-state.json";

const PAGE_PARTITION = "persist:yuncat-browser";
const ALLOWED_PAGE_PROTOCOLS = new Set(["http:", "https:", "about:"]);

function isAllowedPageUrl(url) {
  try {
    return ALLOWED_PAGE_PROTOCOLS.has(new URL(url).protocol);
  } catch (e) {
    return false; // 解析不了的一律不放行（fail-closed）
  }
}

function clampInt(value, min, max, fallback) {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(Math.max(Math.round(n), min), max);
}

/**
 * @param {object} deps
 * @param {object} deps.electron  { BrowserWindow, WebContentsView, ipcMain, nativeTheme, shell, screen }
 * @param {string} deps.appDataDir
 * @param {object} deps.fs / deps.path
 * @param {object} [deps.log]
 */
function createBrowserHost(deps) {
  const { electron, appDataDir, fs, log = console } = deps;
  const { BrowserWindow, WebContentsView, ipcMain, nativeTheme, shell, screen } = electron;

  let win = null;
  const tabs = [];
  let activeTabId = null;
  let tabSeq = 0;
  let agentActiveUntil = 0;
  let ipcWired = false;

  const stateFile = path.join(appDataDir, STATE_FILE);

  function readState() {
    try {
      return JSON.parse(fs.readFileSync(stateFile, "utf-8"));
    } catch (e) {
      return null; // 首次启动或状态文件损坏：走默认尺寸
    }
  }

  function saveState() {
    if (!win || win.isDestroyed()) return;
    try {
      fs.writeFileSync(stateFile, JSON.stringify(win.getNormalBounds(), null, 2));
    } catch (e) {
      log.warn("[browser] window state save failed:", e.message);
    }
  }

  function activeTab() {
    return tabs.find((t) => t.id === activeTabId) || null;
  }

  function getState() {
    return {
      open: !!win && !win.isDestroyed(),
      agent_active: Date.now() < agentActiveUntil,
      active_id: activeTabId,
      tabs: tabs.map((t) => ({
        id: t.id, url: t.url, title: t.title, loading: t.loading, error: t.error,
        active: t.id === activeTabId,
      })),
      limits: { max_tabs: MAX_TABS },
    };
  }

  function emit() {
    if (!win || win.isDestroyed()) return;
    try {
      win.webContents.send("browser:state", getState());
    } catch (e) {
      // 降级：工具栏还没加载完时发状态失败是正常的，下一次状态变化会补上
    }
  }

  function markAgentActivity() {
    agentActiveUntil = Date.now() + AGENT_ACTIVE_MS;
    if (win && !win.isDestroyed()) {
      if (win.isMinimized()) win.restore();
      // showInactive：让用户看见「AI 正在操作」，但不抢走当前输入焦点
      win.showInactive();
    }
    emit();
    setTimeout(emit, AGENT_ACTIVE_MS + 50);
  }

  function pageView() {
    return new WebContentsView({
      webPreferences: {
        partition: PAGE_PARTITION,
        nodeIntegration: false,
        contextIsolation: true,
        sandbox: true,
        webSecurity: true,
        allowRunningInsecureContent: false,
        spellcheck: false,
        backgroundThrottling: false,
      },
    });
  }

  function layout() {
    if (!win || win.isDestroyed()) return;
    const content = win.getContentBounds();
    const pageBounds = {
      x: 0, y: TOOLBAR_HEIGHT,
      width: Math.max(0, content.width),
      height: Math.max(0, content.height - TOOLBAR_HEIGHT),
    };
    for (const tab of tabs) {
      const isActive = tab.id === activeTabId;
      if (isActive && !tab.attached) {
        win.contentView.addChildView(tab.view);
        tab.attached = true;
      } else if (!isActive && tab.attached) {
        win.contentView.removeChildView(tab.view);
        tab.attached = false;
      }
      if (isActive) tab.view.setBounds(pageBounds);
    }
  }

  function wirePage(tab) {
    const wc = tab.view.webContents;
    wc.setWindowOpenHandler(({ url }) => {
      if (isAllowedPageUrl(url) && url !== "about:blank") {
        try {
          newTab(url);
        } catch (e) {
          log.warn("[browser] popup tab rejected:", e.message);
        }
      }
      return { action: "deny" };
    });
    wc.on("will-navigate", (event, url) => {
      if (!isAllowedPageUrl(url)) {
        log.warn("[browser] blocked page navigation to", url);
        event.preventDefault();
      }
    });
    wc.on("did-start-loading", () => { tab.loading = true; tab.error = null; emit(); });
    wc.on("did-stop-loading", () => { tab.loading = false; emit(); });
    wc.on("page-title-updated", (_e, title) => { tab.title = title || tab.title; emit(); });
    wc.on("did-navigate", (_e, url) => { tab.url = url; emit(); });
    wc.on("did-navigate-in-page", (_e, url, isMainFrame) => {
      if (isMainFrame) { tab.url = url; emit(); }
    });
    wc.on("did-fail-load", (_e, code, description, url, isMainFrame) => {
      // -3 = ERR_ABORTED：用户或脚本取消了导航，不是错误，不要吓唬人
      if (!isMainFrame || code === -3) return;
      tab.error = `${description} (${code})`;
      tab.url = url || tab.url;
      log.warn(`[browser] load failed ${code} ${description} ${url}`);
      emit();
    });
    wc.on("render-process-gone", (_e, details) => {
      tab.error = `页面进程退出：${details.reason}`;
      emit();
    });
  }

  function slideTab(tab, url) {
    if (url && url !== "about:blank") {
      cdp.navigate(tab.view.webContents, url).catch((e) => {
        tab.error = e.message;
        emit();
      });
    } else {
      tab.view.webContents.loadURL("about:blank").catch((e) => log.warn("[browser] blank load failed:", e.message));
    }
  }

  function newTab(url) {
    const target = url ? cdp.normalizeUrl(url) : "";
    if (tabs.length >= MAX_TABS) throw new Error(`最多同时打开 ${MAX_TABS} 个标签页，请先关闭一些`);
    const win_ = ensureWindow();
    const view = pageView();
    const tab = {
      id: ++tabSeq, view, url: target || "about:blank", title: "新标签页",
      loading: false, error: null, attached: false,
    };
    tabs.push(tab);
    wirePage(tab);
    win_.contentView.addChildView(view);
    tab.attached = true;
    activate(tab.id);
    slideTab(tab, target);
    return tab;
  }

  function activate(id) {
    if (!tabs.some((t) => t.id === id)) return false;
    activeTabId = id;
    layout();
    emit();
    return true;
  }

  function closeTab(id) {
    const index = tabs.findIndex((t) => t.id === id);
    if (index < 0) return false;
    const [tab] = tabs.splice(index, 1);
    closeTabView(tab);
    if (tabs.length === 0) {
      activeTabId = null;
      closeWindow();
      return true;
    }
    if (activeTabId === id) activeTabId = tabs[Math.min(index, tabs.length - 1)].id;
    layout();
    emit();
    return true;
  }

  function closeTabView(tab) {
    try {
      if (tab.attached && win && !win.isDestroyed()) win.contentView.removeChildView(tab.view);
    } catch (e) {
      log.warn("[browser] detach tab view failed:", e.message);
    }
    tab.attached = false;
    const wc = tab.view.webContents;
    cdp.detach(wc);
    try {
      if (!wc.isDestroyed()) {
        if (typeof wc.close === "function") wc.close();
        else if (typeof wc.destroy === "function") wc.destroy();
      }
    } catch (e) {
      log.warn("[browser] tab teardown failed:", e.message);
    }
  }

  function ensureWindow() {
    if (win && !win.isDestroyed()) return win;
    const saved = readState() || {};
    const areas = screen.getAllDisplays().map((d) => d.workArea);
    const primary = areas[0] || { x: 0, y: 0, width: DEFAULT_WIDTH, height: DEFAULT_HEIGHT };
    const width = clampInt(saved.width, MIN_WIDTH, primary.width, Math.min(DEFAULT_WIDTH, primary.width));
    const height = clampInt(saved.height, MIN_HEIGHT, primary.height, Math.min(DEFAULT_HEIGHT, primary.height));
    win = new BrowserWindow({
      width, height,
      x: Number.isFinite(saved.x) ? saved.x : undefined,
      y: Number.isFinite(saved.y) ? saved.y : undefined,
      minWidth: MIN_WIDTH, minHeight: MIN_HEIGHT,
      title: "Yuncat 浏览器",
      autoHideMenuBar: true, show: false,
      backgroundColor: nativeTheme.shouldUseDarkColors ? "#1b1c1f" : "#ffffff",
      webPreferences: {
        preload: path.join(__dirname, "browser-preload.js"),
        nodeIntegration: false, contextIsolation: true, sandbox: true,
      },
    });
    win.setMenuBarVisibility(false);
    win.on("resize", layout);
    win.on("maximize", layout);
    win.on("unmaximize", layout);
    win.on("enter-full-screen", layout);
    win.on("leave-full-screen", layout);
    win.on("close", () => saveState());
    win.on("closed", () => {
      for (const tab of tabs.splice(0, tabs.length)) closeTabView(tab);
      activeTabId = null;
      win = null;
    });
    win.webContents.on("did-finish-load", emit);
    win.loadFile(path.join(__dirname, "browser-chrome.html"));
    win.once("ready-to-show", () => { if (win && !win.isDestroyed()) win.show(); });
    return win;
  }

  function closeWindow() {
    if (!win || win.isDestroyed()) return;
    const target = win;
    win = null;
    saveStateFor(target);
    try {
      target.destroy();
    } catch (e) {
      log.warn("[browser] window destroy failed:", e.message);
    }
  }

  function saveStateFor(target) {
    try {
      fs.writeFileSync(stateFile, JSON.stringify(target.getNormalBounds(), null, 2));
    } catch (e) {
      log.warn("[browser] window state save failed:", e.message);
    }
  }

  function ensureActiveTab(tabId) {
    const explicit = tabId === undefined || tabId === null ? null : tabs.find((t) => t.id === Number(tabId));
    if (explicit) return explicit;
    if (!activeTab()) {
      ensureWindow();
      return newTab("");
    }
    return activeTab();
  }

  function tabsAction(params) {
    const action = String(params.action || "list");
    if (action === "list") return { tabs: getState().tabs };
    if (action === "new") return { tab: describeTab(newTab(params.url ? String(params.url) : "")) };
    if (action === "activate") return { activated: activate(Number(params.tab_id)) };
    if (action === "close") return { closed: closeTab(Number(params.tab_id)) };
    throw new Error(`未知的标签动作：${action}（可用 list/new/activate/close）`);
  }

  function describeTab(tab) {
    return { id: tab.id, url: tab.url, title: tab.title };
  }

  async function open(url) {
    const target = url ? cdp.normalizeUrl(url) : "";
    const existing = win && !win.isDestroyed();
    const win_ = ensureWindow();
    if (!existing) {
      newTab(target);
    } else if (target) {
      const tab = ensureActiveTab();
      await cdp.navigate(tab.view.webContents, target);
    } else if (!tabs.length) {
      newTab("");
    }
    if (win_.isMinimized()) win_.restore();
    win_.focus();
    emit();
    return getState();
  }

  function closeAll() {
    for (const tab of tabs.splice(0, tabs.length)) closeTabView(tab);
    activeTabId = null;
    closeWindow();
  }

  async function withActiveTab(params, fn) {
    const tab = ensureActiveTab(params.tabId);
    const wc = tab.view.webContents;
    try {
      const data = await fn(wc, tab);
      tab.url = await cdp.executeJavaScript(wc, "location.href").catch(() => tab.url);
      tab.error = null;
      emit();
      return data;
    } catch (e) {
      tab.error = e.message;
      emit();
      throw e;
    }
  }

  async function screenshotData(params) {
    return withActiveTab(params, async (wc) => {
      const buffer = await cdp.screenshot(wc, { fullPage: !!params.full_page });
      return { mime: "image/png", base64: buffer.toString("base64"), bytes: buffer.length };
    });
  }

  /** 白名单动作分发：桥只能用这里列出的名字，别的一律报错 */
  /** 滚动：to=top/bottom 用脚本跳到页首/页尾（长列表要滚很多屏），其余走真实滚轮事件 */
  async function scrollTab(wc, params) {
    const to = String(params.to || "");
    if (to !== "top" && to !== "bottom") return cdp.scrollBy(wc, params.dx, params.dy);
    const target = to === "top" ? "0" : "document.documentElement.scrollHeight";
    await cdp.evaluate(wc, `window.scrollTo({ top: ${target}, behavior: 'auto' })`);
    const y = await cdp.evaluate(wc, "Math.round(window.scrollY)");
    return { scroll_y: typeof y === "number" ? y : null };
  }

  async function invoke(action, params) {
    const p = params || {};
    if (action !== "state") markAgentActivity();
    switch (action) {
      case "open": return await open(p.url ? String(p.url) : "");
      case "state": return getState();
      case "navigate": return withActiveTab(p, (wc) => cdp.navigate(wc, String(p.url || "")));
      case "back": return withActiveTab(p, (wc) => cdp.goBack(wc));
      case "forward": return withActiveTab(p, (wc) => cdp.goForward(wc));
      case "reload": return withActiveTab(p, (wc) => cdp.reload(wc));
      case "snapshot": return withActiveTab(p, (wc) => cdp.buildSnapshot(wc, p));
      case "click": return withActiveTab(p, (wc) => cdp.clickTarget(wc, { ref: p.ref, selector: p.selector }, p));
      case "type": return withActiveTab(p, (wc) => cdp.typeText(wc, p.text, { ref: p.ref, selector: p.selector }, p));
      case "press": return withActiveTab(p, (wc) => cdp.pressKey(wc, p.key, p.modifiers));
      case "scroll": return withActiveTab(p, (wc) => scrollTab(wc, p));
      case "read": return withActiveTab(p, (wc) => cdp.readPage(wc, p.max_chars));
      case "wait_for": return withActiveTab(p, (wc) => cdp.waitFor(wc, p));
      case "eval": return withActiveTab(p, (wc) => cdp.evaluate(wc, p.expression));
      case "screenshot": return screenshotData(p);
      case "tabs": return tabsAction(p);
      default: throw new Error(`未知的浏览器动作：${action}`);
    }
  }

  function toolbarCommand(message) {
    const cmd = String((message && message.cmd) || "");
    const payload = (message && message.payload) || {};
    if (cmd === "state") return getState();
    if (cmd === "navigate") {
      const tab = ensureActiveTab();
      return cdp.navigate(tab.view.webContents, String(payload.url || ""))
        .then(() => getState())
        .catch((e) => { tab.error = e.message; emit(); return getState(); });
    }
    if (cmd === "back") return cdp.goBack(ensureActiveTab().view.webContents).then(() => getState());
    if (cmd === "forward") return cdp.goForward(ensureActiveTab().view.webContents).then(() => getState());
    if (cmd === "reload") return cdp.reload(ensureActiveTab().view.webContents).then(() => getState());
    if (cmd === "stop") { ensureActiveTab().view.webContents.stop(); return getState(); }
    if (cmd === "newTab") { newTab(payload.url ? String(payload.url) : ""); return getState(); }
    if (cmd === "closeTab") { closeTab(Number(payload.id)); return getState(); }
    if (cmd === "activateTab") { activate(Number(payload.id)); return getState(); }
    if (cmd === "openExternal") {
      const url = String(payload.url || "");
      if (/^https?:/i.test(url)) shell.openExternal(url);
      return getState();
    }
    throw new Error(`未知的工具栏命令：${cmd}`);
  }

  function wireIpc() {
    if (ipcWired) return;
    ipcWired = true;
    ipcMain.handle("browser:toolbar-state", () => getState());
    ipcMain.handle("browser:toolbar-command", (_event, message) => toolbarCommand(message));
    ipcMain.handle("browser:app-open", async (_event, url) => {
      try {
        return { ok: true, state: await open(url ? String(url) : "") };
      } catch (e) {
        return { ok: false, error: e.message };
      }
    });
    ipcMain.handle("browser:app-state", () => getState());
  }

  return {
    invoke, open, getState, closeAll, wireIpc, toolbarCommand,
    newTab, closeTab, activate, listTabs: () => getState().tabs,
    hasWindow: () => !!win && !win.isDestroyed(),
  };
}

/**
 * 一次性装配：建宿主 + 起桥 + 挂 IPC，返回 main.js 需要的几样东西。
 *
 * 这里返回 `env` 而不是让 main.js 自己拼环境变量——端口和 token 是随机的，
 * 只应该有**一个**地方知道它们怎么传给 Python 子进程。
 */
async function startBrowserIntegration(deps) {
  const host = createBrowserHost(deps);
  host.wireIpc();
  const bridge = await startHostBridge({
    invoke: (action, params) => host.invoke(action, params),
    log: deps.log || console,
  });
  return {
    host,
    bridge,
    env: {
      YUNCAT_HOST_BRIDGE_PORT: String(bridge.port),
      YUNCAT_HOST_BRIDGE_TOKEN: bridge.token,
    },
    // 返回的是「可直接展开进现有子菜单」的片段，调用方负责决定挂在哪个菜单下
    menuItems: () => [
      { type: "separator" },
      {
        label: "打开内置浏览器",
        accelerator: "CmdOrCtrl+Shift+B",
        click: () => { host.open("").catch((e) => (deps.log || console).warn("[browser] open failed:", e.message)); },
      },
      {
        label: "浏览器新建标签页",
        click: () => {
          try { host.newTab(""); } catch (e) { (deps.log || console).warn("[browser] new tab failed:", e.message); }
        },
      },
    ],
    close: async () => {
      host.closeAll();
      await bridge.close();
    },
  };
}

module.exports = {
  createBrowserHost, startBrowserIntegration,
  isAllowedPageUrl, TOOLBAR_HEIGHT, MAX_TABS, PAGE_PARTITION,
};