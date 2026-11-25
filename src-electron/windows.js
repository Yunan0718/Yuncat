/**
 * windows.js —— 多窗口管理（对标 VS Code / Cursor 的窗口模型）
 *
 * 为什么这样设计：
 * - **一个后端进程，多个窗口**：Python sidecar 与窗口数无关（VS Code 同样是单一
 *   extension host 配多窗口）。每个窗口自成一份数据库/向量库连接，代价与风险都更高。
 * - **窗口间完全独立**：每个渲染进程有自己的 store、WebSocket 连接与任务视图，
 *   互相干扰最小；窗口数量不设硬上限，但禁止无限增殖（见 MAX_WINDOWS）。
 * - **几何状态只记一份**：主流做法是记住「最后关闭的那个窗口」的大小与位置，而不是
 *   给每个窗口记一份身份。这里在窗口关闭时落盘，并在恢复时校验显示器边界
 *   （外接屏拔掉后旧坐标会落在不存在的显示器上）。
 * - **关闭语义**：开启「关闭时隐藏到托盘」时，任何窗口关闭都只是隐藏；否则关掉
 *   最后一个窗口即退出。系统关机 / 安装器关闭场景始终放行。
 * - **主窗口指针**：`primaryWindow` = 最近聚焦的窗口，供「需要单一目标」的操作使用
 *   （文件选择框、通知点击、崩溃对话框）。窗口级广播走 `broadcast()`。
 */
"use strict";

const DEFAULT_WIDTH = 1280;
const DEFAULT_HEIGHT = 800;
const MIN_WIDTH = 1024;
const MIN_HEIGHT = 360;
const MAX_ZOOM = 5;
/** 人为上限：正常使用不会开到这么多窗口，超出多半是快捷键卡住或脚本循环。 */
const MAX_WINDOWS = 12;

/** 与某个 workArea 的重叠面积（Electron screen.getDisplayMatching 的纯函数版本）。 */
function overlapArea(bounds, area) {
  const w = Math.min(bounds.x + bounds.width, area.x + area.width) - Math.max(bounds.x, area.x);
  const h = Math.min(bounds.y + bounds.height, area.y + area.height) - Math.max(bounds.y, area.y);
  return Math.max(0, w) * Math.max(0, h);
}

/** 中心点落在某个 workArea 内，或与其有重叠。 */
function isBoundsVisible(bounds, areas) {
  for (const area of areas) {
    const cx = bounds.x + bounds.width / 2;
    const cy = bounds.y + bounds.height / 2;
    if (cx >= area.x && cx <= area.x + area.width && cy >= area.y && cy <= area.y + area.height) {
      return true;
    }
    if (overlapArea(bounds, area) > 0) return true;
  }
  return false;
}

function clampInt(value, min, max, fallback) {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(Math.max(Math.round(n), min), max);
}

/**
 * 校验并规整窗口状态（纯函数，便于单测）。
 * @param {object} state  上次落盘的窗口状态（可能缺字段或损坏）
 * @param {Array<{x:number,y:number,width:number,height:number}>} areas 显示器工作区，主屏在前
 */
function sanitizeBounds(state, areas) {
  const primary = (areas && areas[0]) || { x: 0, y: 0, width: DEFAULT_WIDTH, height: DEFAULT_HEIGHT };
  const src = state || {};
  const width = clampInt(src.width, 400, primary.width, Math.min(DEFAULT_WIDTH, primary.width));
  const height = clampInt(src.height, MIN_HEIGHT, primary.height, Math.min(DEFAULT_HEIGHT, primary.height));
  let x = Number.isFinite(src.x) ? Math.round(src.x) : primary.x;
  let y = Number.isFinite(src.y) ? Math.round(src.y) : primary.y;
  if (!isBoundsVisible({ x, y, width, height }, areas || [primary])) {
    x = Math.round(primary.x + (primary.width - width) / 2);
    y = Math.round(primary.y + (primary.height - height) / 2);
  }
  return { x, y, width, height };
}

function sanitizeZoom(value) {
  return Number.isFinite(value) && value > 0 && value <= MAX_ZOOM ? value : null;
}

/**
 * @param {object} deps
 * @param {object} deps.electron        { BrowserWindow, Menu, screen, dialog, shell, nativeTheme, app }
 * @param {string} deps.appDataDir      数据目录（window-state.json 落在这里）
 * @param {object} deps.fs              node:fs
 * @param {object} deps.path            node:path
 * @param {Function} deps.buildMenu     返回应用菜单（含「新建窗口」等）
 * @param {Function} deps.getUserPreference
 * @param {Function} deps.isQuitting
 * @param {Function} deps.isForcedClose 系统关机 / 安装器关闭
 * @param {Function} deps.isAllowedLocalUrl
 * @param {Array<string>} deps.externalLinkWhitelist
 * @param {Function} deps.crashHandler
 * @param {Function} [deps.log]
 */
function createWindowManager(deps) {
  const {
    electron, appDataDir, fs, path, buildMenu, getUserPreference, isQuitting,
    isForcedClose, isAllowedLocalUrl, externalLinkWhitelist, crashHandler,
    log = console,
  } = deps;
  const { BrowserWindow, screen, dialog, shell, nativeTheme, app } = electron;

  /** @type {Set<import('electron').BrowserWindow>} */
  const windows = new Set();
  let primary = null;

  const stateFile = path.join(appDataDir, 'window-state.json');
  const workAreas = () => screen.getAllDisplays().map((d) => d.workArea);

  function readState() {
    try {
      return JSON.parse(fs.readFileSync(stateFile, 'utf-8'));
    } catch (e) {
      return null; // 首次启动 / 状态文件损坏，按默认值开窗
    }
  }

  function saveState(win) {
    if (!win || win.isDestroyed()) return;
    try {
      const bounds = win.getBounds();
      const zoomFactor = win.webContents.getZoomFactor();
      fs.writeFileSync(stateFile, JSON.stringify({ ...bounds, zoomFactor }, null, 2));
    } catch (e) {
      log.warn('[windows] window state save failed:', e.message);
    }
  }

  function list() {
    return Array.from(windows);
  }

  function alive() {
    return list().filter((w) => !w.isDestroyed());
  }

  function focused() {
    const f = BrowserWindow.getFocusedWindow();
    if (f && windows.has(f)) return f;
    if (primary && !primary.isDestroyed()) return primary;
    return alive()[0] || null;
  }

  function broadcast(channel, payload) {
    for (const win of alive()) {
      try {
        win.webContents.send(channel, payload);
      } catch (e) {
        log.warn('[windows] broadcast failed:', e.message);
      }
    }
  }

  function sendWhenReady(win, channel, payload) {
    if (!win || win.isDestroyed()) return;
    const wc = win.webContents;
    if (wc.isLoading()) wc.once('did-finish-load', () => {
      if (!win.isDestroyed()) win.webContents.send(channel, payload);
    });
    else wc.send(channel, payload);
  }

  function applyTitleBarTheme(theme) {
    const dark = theme === 'dark' || (theme === 'system' && nativeTheme.shouldUseDarkColors);
    for (const win of alive()) {
      try {
        win.setTitleBarOverlay(dark
          ? { color: '#131415', symbolColor: '#9B9EA8', height: 32 }
          : { color: '#FCFCFD', symbolColor: '#5F6B7C', height: 32 });
      } catch (e) {
        log.warn('[windows] title bar theme failed:', e.message);
      }
    }
  }

  /** 后端端口确定后重建 CSP：CSP 在文档加载时固定，需 reload 才能带上实际端口。 */
  function reloadForCsp(port) {
    for (const win of alive()) {
      try {
        if (win.webContents.isLoading()) continue;
        win.reload();
      } catch (e) {
        log.warn('[windows] csp reload failed:', e.message);
      }
    }
  }

  function confirmOpenExternal(url) {
    let hostname = '';
    try {
      hostname = new URL(url).hostname.replace(/^www\./, '');
    } catch (e) {
      log.error('[openExternal] URL parse failed:', e.message);
      return;
    }
    if (externalLinkWhitelist.includes(hostname)) {
      shell.openExternal(url);
      return;
    }
    dialog.showMessageBox(focused() || undefined, {
      type: 'question', buttons: ['取消', '打开'], defaultId: 0, cancelId: 0,
      title: '打开外部链接', message: '即将打开外部链接',
      detail: `域名 ${hostname} 不在白名单中，确认打开？\n${url}`,
    }).then((r) => { if (r.response === 1) shell.openExternal(url); }).catch(() => {});
  }

  function wireWindow(win) {
    const wc = win.webContents;

    win.on('focus', () => { primary = win; });

    wc.on('render-process-gone', (_event, details) => {
      log.error('[Yuncat] renderer crashed — exitCode:', details.exitCode, 'reason:', details.reason);
      try { crashHandler.recordRendererCrash(app, details); } catch (e) { /* crash log 自身不能抛 */ }
      dialog.showMessageBox(win, {
        type: 'error',
        title: 'Yuncat',
        message: '界面进程已崩溃',
        detail: `原因: ${details.reason}（exit code ${details.exitCode}）。\n重新加载通常可恢复，后端与数据不受影响。`,
        buttons: ['重新加载', '退出'],
        defaultId: 0, cancelId: 1,
      }).then(({ response }) => {
        if (response === 0) { if (!win.isDestroyed()) win.reload(); }
        else app.quit(1);
      }).catch(() => {});
    });

    wc.on('unresponsive', () => log.warn('[Yuncat] renderer unresponsive'));

    wc.on('will-navigate', (event, url) => {
      if (!isAllowedLocalUrl(url)) {
        log.warn('[Yuncat] blocked navigation to', url);
        event.preventDefault();
      }
    });

    wc.setWindowOpenHandler(({ url }) => {
      if (isAllowedLocalUrl(url)) return { action: 'allow' };
      if (/^https?:/i.test(url)) confirmOpenExternal(url);
      return { action: 'deny' };
    });

    win.on('close', (e) => {
      // 系统关机 / 安装器关闭：永远放行，不隐藏、不拦
      if (isForcedClose()) return;
      const minimizeToTray = getUserPreference('minimizeToTray', false);
      if (!isQuitting() && minimizeToTray) {
        e.preventDefault();
        win.hide();
        return;
      }
      saveState(win);
    });

    win.on('closed', () => {
      windows.delete(win);
      if (primary === win) primary = alive()[0] || null;
    });
  }

  function loadEntry(win) {
    if (app.isPackaged) {
      win.loadFile(path.join(process.resourcesPath, 'dist', 'index.html'));
    } else {
      win.loadURL('http://localhost:1420');
    }
  }

  /**
   * 打开一个新窗口。
   * @param {{url?: string}} [options] url 用于「在新窗口打开某个路由」等场景
   */
  function create(options = {}) {
    if (alive().length >= MAX_WINDOWS) {
      // 明确的、可见的上限：不静默失败，也不无限开窗
      dialog.showMessageBox(focused() || undefined, {
        type: 'info', buttons: ['好'], defaultId: 0,
        title: 'Yuncat', message: `最多同时打开 ${MAX_WINDOWS} 个窗口`,
        detail: '请先关闭部分窗口再继续。',
      }).catch(() => {});
      return null;
    }

    const isDark = nativeTheme.shouldUseDarkColors;
    const saved = readState();
    const bounds = sanitizeBounds(saved, workAreas());

    const win = new BrowserWindow({
      width: bounds.width, height: bounds.height,
      center: true, minWidth: MIN_WIDTH, minHeight: MIN_HEIGHT,
      title: 'Yuncat', titleBarStyle: 'hidden',
      titleBarOverlay: { color: isDark ? '#131415' : '#FCFCFD', symbolColor: isDark ? '#9B9EA8' : '#5F6B7C', height: 32 },
      autoHideMenuBar: true, show: false,
      webPreferences: {
        preload: path.join(__dirname, 'preload.js'),
        nodeIntegration: false, contextIsolation: true, sandbox: true,
      },
    });
    windows.add(win);
    primary = win;
    wireWindow(win);

    if (BrowserWindow.getAllWindows().length === 1) {
      try { electron.Menu?.setApplicationMenu(buildMenu()); } catch (e) { /* 菜单失败不影响开窗 */ }
    }

    // 只有首个窗口继承上次的位置；后续窗口交给系统级联排布，避免完全重叠
    if (alive().length === 1 && Number.isFinite(bounds.x) && Number.isFinite(bounds.y)) {
      win.setBounds(bounds);
    }
    const zoom = sanitizeZoom(saved && saved.zoomFactor);
    if (zoom) {
      win.webContents.once('did-finish-load', () => {
        try { win.webContents.setZoomFactor(zoom); } catch (e) { /* zoom 失败忽略 */ }
      });
    }

    loadEntry(win);
    if (options.url) win.webContents.once('did-finish-load', () => {
      if (!win.isDestroyed()) win.webContents.send('deep-link', { url: options.url });
    });

    win.once('ready-to-show', () => {
      if (!win.isDestroyed()) win.show();
    });
    return win;
  }

  /** 聚焦已有窗口；一个都没有时新开一个（托盘点击、二次启动都走这里）。 */
  function focusOrCreate() {
    const win = focused();
    if (win) {
      if (win.isMinimized()) win.restore();
      win.show();
      win.focus();
      return win;
    }
    return create();
  }

  function byWebContentsId(id) {
    return alive().find((w) => w.webContents.id === id) || null;
  }

  function each(fn) {
    for (const win of alive()) fn(win);
  }

  return {
    create, list, alive, focused, broadcast, sendWhenReady, applyTitleBarTheme,
    reloadForCsp, focusOrCreate, byWebContentsId, each, saveState,
  };
}

/**
 * 安装生产环境的 CSP（2026-09-15 从 main.js 迁入）。
 *
 * 放在这里的理由：CSP 与 will-navigate / setWindowOpenHandler 属于同一类「窗口安全装配」，
 * 集中在一处才不容易在多窗口重构时被顺手删掉（这正是它曾经丢失的原因）。
 * connect-src 精确到后端实际端口；img/media/frame 允许 blob:（工作区预览的 object URL，
 * 字节走带鉴权 fetch，token 不进 URL）。
 */
function installContentSecurityPolicy({ app, getBackendPort, log = console }) {
  if (!app.isPackaged) return;  // 开发模式由 Vite 处理
  const { session } = require("electron");
  session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
    const port = getBackendPort ? getBackendPort() : null;
    const connectSrc = port
      ? `connect-src 'self' http://127.0.0.1:${port} ws://127.0.0.1:${port}`
      : `connect-src 'self'`;
    callback({
      responseHeaders: {
        ...details.responseHeaders,
        "Content-Security-Policy": [
          `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; ` +
            `img-src 'self' data: blob: https:; media-src 'self' blob:; frame-src 'self' blob:; ` +
            `${connectSrc};`,
        ],
      },
    });
  });
  log.log("[Yuncat] Content-Security-Policy installed");
}

module.exports = {
  installContentSecurityPolicy,
  createWindowManager, sanitizeBounds, isBoundsVisible, overlapArea, sanitizeZoom,
  DEFAULT_WIDTH, DEFAULT_HEIGHT, MIN_WIDTH, MIN_HEIGHT, MAX_ZOOM, MAX_WINDOWS,
};
