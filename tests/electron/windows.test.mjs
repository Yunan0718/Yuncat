import { test } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const { createWindowManager, sanitizeBounds, MAX_WINDOWS } = require(
  path.join(here, "..", "..", "src-electron", "windows.js"),
);

// ── Electron 假对象（只实现 windows.js 用到的那部分接口）────────────────
let wcSeq = 0;

class FakeWebContents extends EventEmitter {
  constructor() {
    super();
    this.id = ++wcSeq;
    this.loading = false;
    this.sent = [];
    this.zoomFactor = 1;
    this.destroyed = false;
  }
  send(channel, payload) { this.sent.push([channel, payload]); }
  isLoading() { return this.loading; }
  getZoomFactor() { return this.zoomFactor; }
  setZoomFactor(v) { this.zoomFactor = v; }
  openDevTools() {}
  setWindowOpenHandler(handler) { this.windowOpenHandler = handler; return handler; }
}

class FakeWindow extends EventEmitter {
  static instances = [];
  static focused = null;
  constructor(options = {}) {
    super();
    FakeWindow.instances.push(this);
    this.options = options;
    this.webContents = new FakeWebContents();
    this.bounds = {
      x: Number.isFinite(options.x) ? options.x : 0,
      y: Number.isFinite(options.y) ? options.y : 0,
      width: options.width ?? 1280,
      height: options.height ?? 800,
    };
    this.destroyed = false;
    this.minimized = false;
    this.visible = false;
    this.loaded = null;
    this.closedEvents = [];
  }
  static getAllWindows() { return FakeWindow.instances.filter((w) => !w.destroyed); }
  static getFocusedWindow() { return FakeWindow.focused; }
  isDestroyed() { return this.destroyed; }
  isMinimized() { return this.minimized; }
  restore() { this.minimized = false; }
  show() { this.visible = true; }
  hide() { this.visible = false; }
  focus() { FakeWindow.focused = this; this.emit("focus"); }
  reload() { this.reloaded = (this.reloaded || 0) + 1; }
  loadURL(url) { this.loaded = { url }; }
  loadFile(file) { this.loaded = { file }; }
  setBounds(b) { this.bounds = { ...this.bounds, ...b }; }
  getBounds() { return { ...this.bounds }; }
  setTitleBarOverlay(o) { this.overlay = o; }
}

function makeDeps(overrides = {}) {
  const written = [];
  const dialogs = [];
  return {
    written,
    dialogs,
    deps: {
      electron: {
        BrowserWindow: FakeWindow,
        Menu: { setApplicationMenu() {} },
        screen: { getAllDisplays: () => [{ workArea: { x: 0, y: 0, width: 1920, height: 1080 } }] },
        dialog: {
          showMessageBox: async (...args) => { dialogs.push(args); return { response: 0 }; },
        },
        shell: { openExternal() {} },
        nativeTheme: { shouldUseDarkColors: false },
        app: {
          isPackaged: false,
          quit() {},
          getPath: () => "/tmp",
        },
      },
      appDataDir: "/tmp/yuncat-test",
      fs: {
        readFileSync: () => { throw new Error("no state file"); },
        writeFileSync: (file, data) => written.push([file, data]),
      },
      path,
      buildMenu: () => ({ template: true }),
      getUserPreference: () => false,
      isQuitting: () => false,
      isForcedClose: () => false,
      isAllowedLocalUrl: () => true,
      externalLinkWhitelist: [],
      crashHandler: { recordRendererCrash() {} },
      log: { warn() {}, error() {}, log() {} },
      ...overrides,
    },
  };
}

function reset() {
  FakeWindow.instances = [];
  FakeWindow.focused = null;
}

// ── 纯函数：几何状态校验 ────────────────────────────────────────────────
test("sanitizeBounds: 尺寸被夹到显示器范围内，越界坐标回退主屏居中", () => {
  const areas = [{ x: 0, y: 0, width: 1920, height: 1080 }];
  const out = sanitizeBounds({ x: 99999, y: 99999, width: 5000, height: 20 }, areas);
  assert.equal(out.width, 1920);
  assert.equal(out.height, 360); // 下限对齐窗口最小高度
  assert.ok(out.x >= 0 && out.x + out.width <= 1920);
  assert.ok(out.y >= 0 && out.y + out.height <= 1080);
});

test("sanitizeBounds: 外接屏拔掉后（窗口落在不存在的坐标）回退主屏", () => {
  const areas = [{ x: 0, y: 0, width: 1920, height: 1080 }];
  const out = sanitizeBounds({ x: 2500, y: 200, width: 1280, height: 800 }, areas);
  assert.ok(out.x < 1920 && out.y < 1080);
});

test("sanitizeBounds: 正常坐标原样保留", () => {
  const areas = [{ x: 0, y: 0, width: 1920, height: 1080 }];
  const out = sanitizeBounds({ x: 100, y: 80, width: 1280, height: 800 }, areas);
  assert.deepEqual(out, { x: 100, y: 80, width: 1280, height: 800 });
});

// ── 窗口管理器：多窗口行为 ──────────────────────────────────────────────
test("create: 每个窗口独立注册且加载入口", () => {
  reset();
  const { deps } = makeDeps();
  const wins = createWindowManager(deps);
  const a = wins.create();
  const b = wins.create();
  assert.equal(wins.alive().length, 2);
  assert.notEqual(a.webContents.id, b.webContents.id);
  assert.equal(a.loaded.url, "http://localhost:1420");
  assert.equal(b.loaded.url, "http://localhost:1420");
});

test("broadcast: 事件发给所有窗口（后端就绪/更新提示不能只到主窗口）", () => {
  reset();
  const { deps } = makeDeps();
  const wins = createWindowManager(deps);
  const a = wins.create();
  const b = wins.create();
  wins.broadcast("backend-ready", [1234, "tok"]);
  assert.deepEqual(a.webContents.sent, [["backend-ready", [1234, "tok"]]]);
  assert.deepEqual(b.webContents.sent, [["backend-ready", [1234, "tok"]]]);
});

test("focusOrCreate: 已有窗口时聚焦，没有时新开", () => {
  reset();
  const { deps } = makeDeps();
  const wins = createWindowManager(deps);
  assert.equal(wins.focused(), null);
  const created = wins.focusOrCreate();
  assert.equal(wins.alive().length, 1);
  const again = wins.focusOrCreate();
  assert.equal(again, created);
  assert.equal(wins.alive().length, 1);
});

test("关闭窗口：minimizeToTray 开启时隐藏而非关闭", () => {
  reset();
  const { deps } = makeDeps({ getUserPreference: () => true });
  const wins = createWindowManager(deps);
  const win = wins.create();
  let prevented = false;
  win.emit("close", { preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(win.visible, false);
  assert.equal(wins.alive().length, 1);
});

test("关闭窗口：未开启托盘时落盘窗口状态", () => {
  reset();
  const { deps, written } = makeDeps();
  const wins = createWindowManager(deps);
  const win = wins.create();
  win.emit("close", { preventDefault() {} });
  assert.equal(written.length, 1);
  assert.match(written[0][0], /window-state\.json$/);
  const saved = JSON.parse(written[0][1]);
  assert.equal(typeof saved.width, "number");
  win.destroyed = true;
  win.emit("closed");
  assert.equal(wins.alive().length, 0);
});

test(`窗口数量上限：超过 ${MAX_WINDOWS} 个时拒绝并提示`, () => {
  reset();
  const { deps, dialogs } = makeDeps();
  const wins = createWindowManager(deps);
  for (let i = 0; i < MAX_WINDOWS; i++) assert.ok(wins.create());
  assert.equal(wins.create(), null);
  assert.equal(dialogs.length, 1);
  assert.match(String(dialogs[0][1].message), /最多同时打开/);
});

test("首个窗口应用上次的位置，后续窗口交给系统排布（避免完全重叠）", () => {
  reset();
  const { deps } = makeDeps({
    fs: {
      readFileSync: () => JSON.stringify({ x: 120, y: 90, width: 1400, height: 900 }),
      writeFileSync() {},
    },
  });
  const wins = createWindowManager(deps);
  const first = wins.create();
  assert.deepEqual(first.bounds, { x: 120, y: 90, width: 1400, height: 900 });
  const second = wins.create();
  assert.notDeepEqual(second.bounds, first.bounds);
});
