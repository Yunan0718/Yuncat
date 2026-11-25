import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const read = (rel) => fs.readFileSync(path.join(here, "..", "..", rel), "utf-8");

/**
 * Electron 主进程安全基线的静态守卫（2026-09-15）。
 *
 * 起因：多窗口重构把窗口创建搬进 windows.js 时，**打包环境的 CSP 安装代码被一起删掉**，
 * 而 node --check 与业务单测都发现不了——CSP 是运行时行为，没有静态锚点就会静默丢失。
 * 这里把「必须存在的安全装配」钉死成断言。
 */
test("生产环境安装 CSP，且连接白名单精确到后端端口", () => {
  // CSP 装配在 windows.js（与 will-navigate / window-open 同一处安全装配），
  // main.js 负责在启动时调用——两边都断言，避免再次「搬家时丢掉」。
  const wins = read("src-electron/windows.js");
  const main = read("src-electron/main.js");
  assert.match(wins, /onHeadersReceived/, "windows.js 必须安装 CSP（webRequest.onHeadersReceived）");
  assert.match(wins, /default-src 'self'/, "CSP 缺少 default-src 'self'");
  assert.match(wins, /script-src 'self'/, "CSP 缺少 script-src 'self'（内联脚本必须被禁）");
  assert.match(wins, /img-src[^;]*blob:/, "CSP 的 img-src 需要 blob:（图片预览）");
  assert.match(wins, /media-src[^;]*blob:/, "CSP 的 media-src 需要 blob:（音视频预览）");
  assert.match(wins, /frame-src[^;]*blob:/, "CSP 的 frame-src 需要 blob:（PDF / HTML 预览）");
  assert.match(wins, /connect-src 'self' http:\/\/127\.0\.0\.1:\$\{port\}/,
    "connect-src 必须精确到后端端口，而不是 127.0.0.1:* 通配");
  assert.match(main, /installContentSecurityPolicy\(\{ app, getBackendPort/, "启动时必须调用 CSP 装配");
});

test("窗口 webPreferences 保持安全默认（无 nodeIntegration）", () => {
  const wins = read("src-electron/windows.js");
  assert.match(wins, /nodeIntegration:\s*false/, "nodeIntegration 必须为 false");
  assert.match(wins, /contextIsolation:\s*true/, "contextIsolation 必须为 true");
  assert.match(wins, /sandbox:\s*true/, "sandbox 必须为 true");
  const main = read("src-electron/main.js");
  assert.doesNotMatch(main, /nodeIntegration:\s*true/, "main.js 不得开启 nodeIntegration");
});

test("导航空白名单与 window.open 拦截都在", () => {
  const wins = read("src-electron/windows.js");
  assert.match(wins, /will-navigate/, "缺少 will-navigate 拦截");
  assert.match(wins, /setWindowOpenHandler/, "缺少 setWindowOpenHandler 拦截");
});

test("preload 只暴露白名单 API，不暴露 ipcRenderer 本体", () => {
  const preload = read("src-electron/preload.js");
  assert.match(preload, /contextBridge\.exposeInMainWorld\('yuncat'/);
  assert.doesNotMatch(preload, /exposeInMainWorld\([^)]*ipcRenderer[^)]*\)/,
    "不得把 ipcRenderer 本体暴露给渲染进程");
});

/**
 * 打包清单守卫（2026-09-16）。
 *
 * electron-builder.yml 的 files 是一个**显式白名单**：没写进去的文件不会进安装包。
 * 开发期一切正常，用户装上后才会在 `require` 处直接崩——而且是在启动阶段，
 * 连错误界面都出不来。上一轮把窗口逻辑拆到 windows.js 时就漏过一次。
 *
 * 这里从 main.js 出发递归收集 require('./x') 与 path.join(__dirname, 'x')，
 * 再和打包清单逐一对齐；两个方向都查（引用了但没打包 / 打包了但文件不存在）。
 */
function stripComments(source) {
  // 必须先去掉行注释再去块注释：这些文件里存在 `// ... /** ...` 这种写法，
  // 反过来会从那个假 `/**` 一直吃到很后面的真 `*/`，把中间的 require 一起吃掉。
  return source.replace(/^\s*\/\/.*$/gm, "").replace(/\/\*[\s\S]*?\*\//g, "");
}

function packagedFiles() {
  const yml = read("src-electron/electron-builder.yml");
  const block = yml.split(/\r?\n/);
  const start = block.findIndex((line) => /^files:\s*$/.test(line));
  assert.ok(start >= 0, "electron-builder.yml 缺少 files 段");
  const entries = [];
  for (let i = start + 1; i < block.length; i++) {
    const line = block[i];
    if (/^\S/.test(line)) break; // 回到顶层键，结束
    const m = line.match(/^\s+-\s+(\S+)\s*$/);
    if (m) entries.push(m[1]);
  }
  assert.ok(entries.length > 0, "files 段没有解析到任何条目");
  return entries;
}

function collectMainProcessFiles() {
  const dir = path.join(here, "..", "..", "src-electron");
  const seen = new Set();
  const queue = ["main.js"];
  while (queue.length) {
    const name = queue.shift();
    if (seen.has(name)) continue;
    seen.add(name);
    const full = path.join(dir, name);
    if (!fs.existsSync(full)) continue;
    const source = stripComments(fs.readFileSync(full, "utf-8"));
    const dirnameRefs = source.matchAll(/__dirname,\s*["']([^"']+)["']/g);
    for (const [, ref] of dirnameRefs) seen.add(ref);
    if (!name.endsWith(".js")) continue;
    const requires = source.matchAll(/require\(\s*['"]\.\/([^'"]+)['"]\s*\)/g);
    for (const [, ref] of requires) queue.push(ref.endsWith(".js") ? ref : `${ref}.js`);
  }
  seen.delete("main.js");
  return [...seen].sort();
}

test("打包清单覆盖所有主进程运行时引用的文件", () => {
  const packaged = new Set(packagedFiles());
  const referenced = collectMainProcessFiles();
  assert.ok(referenced.length >= 8, `扫描到的运行时文件太少，规则可能失效：${referenced.length}`);
  const missing = referenced.filter((name) => !packaged.has(name));
  assert.deepEqual(
    missing, [],
    `这些文件被主进程引用但没进 electron-builder.yml 的 files，打出来的安装包会启动失败：${missing.join(", ")}`,
  );
});

test("打包清单里列出的文件必须真实存在", () => {
  const dir = path.join(here, "..", "..", "src-electron");
  const gone = packagedFiles().filter((name) => !fs.existsSync(path.join(dir, name)));
  assert.deepEqual(gone, [], `electron-builder.yml 列了不存在的文件：${gone.join(", ")}`);
});

/**
 * 打包白名单必须排除测试/构建残留（2026-09-25）。
 *
 * 起因：`src-python/MagicMock/`（18 个文件，SelfEvolution 测试用 MagicMock 当
 * 技能注册表路径时意外建出）与 `.prebuild-hash` 混进了安装包。体积影响很小，
 * 但意义是「打包白名单没管住」——今天是 MagicMock，明天可能是别的运行时残留，
 * 而这类文件在开发期毫无征兆。与主进程清单守卫同一思路：把该排除的钉死。
 */
test("src-python 打包白名单排除测试与构建残留", () => {
  const yml = read("src-electron/electron-builder.yml");
  const block = yml.split("to: _up_/src-python", 2)[1] || "";
  for (const pattern of ["!MagicMock/**", "!.prebuild-hash"]) {
    assert.ok(block.includes(pattern), `electron-builder.yml 的 src-python filter 缺少 "${pattern}"`);
  }
});