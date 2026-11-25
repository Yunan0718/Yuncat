// Yuncat Electron — Sidecar Python backend
//
// 启动: 窗口先出现→后台解压运行时→spawn Python→YUNCAT_READY→IPC通知前端
// 退出: HTTP shutdown→kill Python→保存窗口状态→destroy
// 托盘: 关闭窗口→最小化到托盘; 退出由托盘菜单触发

const { app, BrowserWindow, Tray, Menu, ipcMain, nativeImage, nativeTheme, shell, dialog, Notification, globalShortcut, screen, WebContentsView } = require('electron');
const { spawn, execSync } = require('child_process');
const path = require('path');
const crypto = require('crypto');
const fs = require('fs');
const http = require('http');
const crashHandler = require('./crash-handler');
// 2026-09-15: 多窗口管理抽成独立模块（一个后端进程 + 多个窗口，对标 VS Code / Cursor）
const { createWindowManager, installContentSecurityPolicy } = require('./windows');
// 2026-09-16: 内置浏览器（Electron 自带 Chromium + CDP）+ 运行时解压
const { startBrowserIntegration } = require('./browser');
const { startOptionalBrowserIntegration } = require('./startup-browser');
const runtimeBootstrap = require('./runtime-bootstrap');
const { recoverAuthAfterRefreshFailure } = require('./auth-recovery');

// ── 路径安全校验（主进程纵深防御，与 preload.js 保持一致） ──
const _SENSITIVE_DIRS = /^(C:\\Windows|C:\\Program Files|C:\\Program Files \(x86\)|\/etc|\/usr|\/bin|\/sbin|\/root)(\\|\/|$)/i;
function _isSafePath(filePath) {
  if (typeof filePath !== 'string' || !filePath) return false;
  // M27 修复：先规范化路径再检测 .. —— 原代码对 "foo/..\\bar" 等混合分隔符路径漏判。
  const normalized = path.resolve(filePath);
  if (_SENSITIVE_DIRS.test(normalized)) return false;
  if (normalized.includes('..')) return false;
  return true;
}

// ── openExternal 域名白名单（非白名单域名需用户确认） ──
const _EXTERNAL_LINK_WHITELIST = [
  'github.com', 'gitlab.com', 'bitbucket.org',
  'code.visualstudio.com', 'electronjs.org', 'nodejs.org',
  'python.org', 'fastapi.tiangolo.com', 'tailwindcss.com',
  'ui.shadcn.com', 'react.dev', 'vitejs.dev',
];

// E6: 导航/新窗口本地放行判定——仅放行 file:// 与已知本地端口
// （Vite dev server 1420 + 动态 backendPort）。此前用 http://localhost 前缀
// 匹配会放行任意本地端口，XSS 可借此跳转/连到其它本机服务。
function _isAllowedLocalUrl(url) {
  try {
    const u = new URL(url);
    if (u.protocol === 'file:') return true;
    if (u.protocol !== 'http:') return false;
    if (u.hostname !== 'localhost' && u.hostname !== '127.0.0.1') return false;
    const port = parseInt(u.port, 10);
    return port === 1420 || (backendPort !== null && port === backendPort);
  } catch { return false; }
}

// ── electron-updater: lazy load after app.whenReady() ──
let autoUpdater = null;
function getAutoUpdater() {
  if (!autoUpdater) {
    try { autoUpdater = require('electron-updater').autoUpdater; } catch (e) { console.error('[autoUpdater]', e.message); }
  }
  return autoUpdater;
}

// ── State ──
let backendPort = null;
const token = crypto.randomBytes(32).toString('hex');
let accessToken = null;   // Batch 7.6: 短期 access token（派发给前端）
let refreshToken = null;  // Batch 7.6: 长期 refresh token（主进程持有）
let mainWindow = null;      // 最近聚焦的窗口（需要单一目标的操作使用）；窗口集合由 wins 管理
let wins = null;            // 窗口管理器（见 windows.js），app.whenReady 前初始化
let tray = null;
let pythonProcess = null;
let pythonRestarting = false;
let isQuitting = false;  // 托盘退出 vs 关闭窗口
let _cspPort = null;
let browserIntegration = null;  // 内置浏览器宿主 + 宿主桥（Python 侧 browser_* 工具经它落地）        // Phase 48 T1-2: 当前文档 CSP 已允许的后端端口
let _preferencesCache = null; // 用户偏好缓存（minimizeToTray / autostart）

// Phase 48 T1-2: 后端端口确定后，若与当前文档 CSP 端口不同则 reload 使新 CSP 生效。
// 必要性：CSP 在文档加载时建立且固定，而 backendPort 在 backend-ready 后才可知
// （loadFile 时 backendPort=null）。故首次 ready 及重启换端口后需 reload 一次，
// 让 onHeadersReceived 携带实际端口重建 CSP，从而用具体端口取代 127.0.0.1:* 通配
// （闭合 XSS 连任意 localhost 服务的风险）。
function _applyCspForPort(port) {
  if (!port || _cspPort === port) return;
  if (!wins || !app.isPackaged) return;
  _cspPort = port;
  console.log('[Yuncat] reload windows to apply CSP for backend port', port);
  wins.reloadForCsp(port);
}

function findFreePort() {
  const net = require('net');
  const server = net.createServer();
  return new Promise((resolve, reject) => {
    server.listen(0, '127.0.0.1', () => { const p = server.address().port; server.close(() => resolve(p)); });
    server.on('error', reject);
  });
}

// ── 查找可用端口（带重试） ──
// Phase 47: 避免硬编码 fallback，把 startBackend / restart_backend 中重复的重试逻辑统一到这里。
async function findFreePortWithRetry(maxRetries = 3, delayMs = 200) {
  try { const p = await findFreePort(); if (p) return p; } catch {}
  for (let i = 0; i < maxRetries; i++) {
    try {
      const p = await findFreePort();
      if (p) {
        if (delayMs > 0) await new Promise(r => setTimeout(r, delayMs));
        return p;
      }
    } catch {}
  }
  throw new Error('No free port available');
}

// ── 全局快捷键 ──
function toggleMainWindow() {
  const win = wins ? wins.focused() : null;
  if (!win) { createWindow(); return; }
  if (win.isVisible() && win.isFocused()) { win.hide(); return; }
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

function sendToRendererWhenReady(channel, data) {
  if (!wins) return;
  wins.sendWhenReady(wins.focused() || wins.create(), channel, data);
}

function registerGlobalShortcuts() {
  const toggleKey = process.platform === 'darwin' ? 'Cmd+Shift+Y' : 'Ctrl+Shift+Y';
  const failed = [];
  if (!globalShortcut.register(toggleKey, toggleMainWindow)) {
    console.warn('[globalShortcut] failed to register', toggleKey);
    failed.push(toggleKey);
  }
  // Ctrl+Shift+N = 新建窗口（VS Code / Cursor / Chrome 的约定）；「新建对话」走应用菜单
  if (!globalShortcut.register('Ctrl+Shift+N', () => { createWindow(); })) {
    console.warn('[globalShortcut] failed to register Ctrl+Shift+N');
    failed.push('Ctrl+Shift+N');
  }
  // 快捷键注册失败时用系统通知提醒用户(可能被其他应用占用)
  if (failed.length > 0 && Notification.isSupported()) {
    new Notification({
      title: 'Yuncat',
      body: `全局快捷键 ${failed.join(', ')} 注册失败,可能被其他应用占用。可在设置中查看。`,
      icon: path.join(__dirname, 'icon.ico'),
    }).show();
  }
}

// ── 深度链接 ──
function isYuncatDeepLink(url) {
  return typeof url === 'string' && url.startsWith('yuncat://');
}

function parseDeepLink(url) {
  try {
    const u = new URL(url);
    return { prompt: u.searchParams.get('prompt') || '', agent: u.searchParams.get('agent') || '' };
  } catch (e) { console.warn('[deep-link] parse failed:', url, e.message); return null; }
}

function handleDeepLink(url) {
  if (!isYuncatDeepLink(url)) return;
  const payload = parseDeepLink(url);
  if (!payload) return;
  console.log('[deep-link]', url);
  wins.focusOrCreate();
  sendToRendererWhenReady('deep-link', payload);
}

function handleArgv(argv) {
  const url = argv.find(isYuncatDeepLink);
  if (url) handleDeepLink(url);
}

function registerDeepLinkProtocol() {
  if (process.defaultApp) {
    if (process.argv.length >= 2) {
      app.setAsDefaultProtocolClient('yuncat', process.execPath, [path.resolve(process.argv[1])]);
    }
  } else {
    app.setAsDefaultProtocolClient('yuncat');
  }
}

// ── 单实例锁 ──
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) { app.quit(); process.exit(0); }
app.on('second-instance', (_event, argv) => {
  handleArgv(argv);
  // 二次启动（双击图标 / 深链）：复用已有窗口，没有则新开
  if (wins) wins.focusOrCreate();
});

// ── 路径 ──
// Phase 91: 便携模式——打包后 Yuncat.exe 同级存在 .portable 文件时，
// 数据目录改放 exe 同级 yuncat-data/，实现便携（U 盘运行数据跟随）。
// Python 子进程通过 YUNCAT_DATA_DIR 环境变量继承（settings.py 已支持）。
let appDataDir = path.join(app.getPath('appData'), 'Yuncat');
if (app.isPackaged) {
  const _exeDir = path.dirname(app.getPath('exe'));
  if (fs.existsSync(path.join(_exeDir, '.portable'))) {
    appDataDir = path.join(_exeDir, 'yuncat-data');
    process.env.YUNCAT_DATA_DIR = appDataDir;
    console.log('[Yuncat] Portable mode: data dir =', appDataDir);
  }
}
// Phase 92: 初始化崩溃报告处理器（注册 process.on(uncaughtException) + app.on(child-process-gone)）
crashHandler.init(app, appDataDir);

// 主进程未捕获异常兜底回收——best-effort 终止 Python 后端，避免主进程崩溃后子进程成为孤儿占用端口
// （正常退出由 will-quit / shutdownBackend 覆盖；此处补 JS 异常导致崩溃的路径）
process.on('uncaughtException', () => { void shutdownBackend(2000).catch(() => {}); });

// ── 用户偏好持久化（JSON 文件，Electron 层配置） ──
// 用于 minimizeToTray / autostart 等前端可切换、主进程启动时即需读取的配置。
// 默认全部关闭，避免新安装包首次启动后自动占托盘 / 开机自启。
const PREFERENCES_FILE = path.join(appDataDir, 'preferences.json');

function _loadPreferences() {
  if (_preferencesCache) return _preferencesCache;
  try {
    if (fs.existsSync(PREFERENCES_FILE)) {
      _preferencesCache = JSON.parse(fs.readFileSync(PREFERENCES_FILE, 'utf-8'));
    }
  } catch (e) {
    console.error('[preferences] load failed:', e.message);
  }
  if (!_preferencesCache || typeof _preferencesCache !== 'object') {
    _preferencesCache = {};
  }
  return _preferencesCache;
}

function _savePreferences(prefs) {
  _preferencesCache = prefs;
  try {
    fs.mkdirSync(appDataDir, { recursive: true });
    fs.writeFileSync(PREFERENCES_FILE, JSON.stringify(prefs, null, 2), 'utf-8');
  } catch (e) {
    console.error('[preferences] save failed:', e.message);
  }
}

function getUserPreference(key, defaultValue = false) {
  const prefs = _loadPreferences();
  return prefs.hasOwnProperty(key) ? prefs[key] : defaultValue;
}

function setUserPreference(key, value) {
  const prefs = _loadPreferences();
  prefs[key] = value;
  _savePreferences(prefs);
}
// 从 __dirname 向上搜索项目根目录（兼容 src-electron/ 和 resources/app/ 等多种启动位置）
function findProjectRoot(startDir) {
  let dir = startDir;
  for (let i = 0; i < 10; i++) {
    if (fs.existsSync(path.join(dir, 'src-python', 'main.py'))) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return path.join(startDir, '..');  // fallback
}
const projectRoot = findProjectRoot(__dirname);

function findPython() {
  const devMain = path.join(projectRoot, 'src-python', 'main.py');
  const devPython = path.join(projectRoot, 'src-python', 'python-embed', 'python.exe');

  if (!app.isPackaged && fs.existsSync(devMain)) {
    return { python: devPython, main: devMain, embedDir: path.join(projectRoot, 'src-python') };
  }

  // Phase 49: 主流做法 —— 运行时作为 loose files 直接打包在 resources/ 下，
  // 安装时就位，启动时不解压，避免 1GB+ zip 解压超时导致文件残缺。
  const pythonDir = path.join(process.resourcesPath, 'python-embed');
  const candidates = [
    path.join(process.resourcesPath, '_up_', 'src-python'),
    path.join(path.dirname(app.getPath('exe')), '_up_', 'src-python'),
  ];
  const srcBase = candidates.find(d => fs.existsSync(path.join(d, 'main.py'))) || candidates[0];
  return { python: path.join(pythonDir, 'python.exe'), main: path.join(srcBase, 'main.py'), pythonDir, srcBase };
}

// ── 运行时准备 ──
// Phase 49: 主流做法 —— 运行时作为 loose files 随安装包分发到 resources/ 下，
// 安装时就绪，启动时不解压，避免 PowerShell Expand-Archive 处理 1GB+ 文件时
// 超时或文件残缺（VS Code / Cursor / Postman 均把 Language Server / 运行时
// 作为 loose files 放进 resources，安装阶段由 NSIS 统一解压）。
async function ensureRuntimesAsync() {  // 无窗口时也要能报错：状态走 wins.broadcast()
  const loc = findPython();
  const pythonExe = loc.python;
  // dev 模式（非打包）运行时在 src-python/ 下；打包后作为 loose files 在 resources/ 下。
  // findPython() 已按 dev/package 解析 python 路径，node 路径按同一规则派生。
  if (!fs.existsSync(pythonExe)) {
    wins?.broadcast('backend-error', `未找到 Python 运行时: ${pythonExe}`);
    return;
  }
  // Phase 融合: OpenClaw / Hermes 已改为「外部 agent」（用户自行安装），不再 vendored
  // node-embed，故移除对 node-embed/node.exe 的运行时存在性检查（否则会卡死启动）。

  wins?.broadcast('backend-starting', '运行时检查通过');
}

function appendLog(msg) {
  try { fs.appendFileSync(path.join(appDataDir, 'backend.log'), msg + '\n'); }
  catch (e) { console.error('[appendLog] failed:', e.message); }
}

// ── spawnBackend: stderr fd 由 spawn 内部 dup，父进程不提前关闭 ──
function spawnBackend(pythonExe, mainPy, port) {
  const embedDir = path.dirname(pythonExe);
  const srcDir = path.dirname(mainPy);
  const env = { ...process.env, YUNCAT_TOKEN: token, YUNCAT_PARENT_PID: String(process.pid),
    // 宿主桥地址与 token 每次启动随机生成，只通过环境变量交给自己的 Python 子进程
    ...(browserIntegration ? browserIntegration.env : {}),
    PYTHONUNBUFFERED: '1', PYTHONPATH: srcDir, PATH: `${embedDir};${process.env.PATH || ''}`,
    // 离线模式：禁止 HuggingFace 联网下载模型（国内无镜像时启动会反复重试卡死）。
    // 已缓存的 embedding 模型直接本地加载；未缓存（如 reranker bge）快速跳过（best-effort）。
    // 如需联网下载（配合 HF_ENDPOINT 镜像），在外层显式设 HF_HUB_OFFLINE=0 覆盖。
    HF_HUB_OFFLINE: process.env.HF_HUB_OFFLINE || '1',
    TRANSFORMERS_OFFLINE: process.env.TRANSFORMERS_OFFLINE || '1' };

  fs.mkdirSync(appDataDir, { recursive: true });
  const logFd = fs.openSync(path.join(appDataDir, 'backend.log'), 'a');
  const proc = spawn(pythonExe, [mainPy, '--port', String(port)], {
    env, stdio: ['ignore', 'pipe', logFd], windowsHide: true });
  fs.closeSync(logFd);  // child 有自己的句柄副本，父进程可以关闭
  return proc;
}

function waitForReady(proc, timeoutSecs) {
  return new Promise((resolve, reject) => {
    const start = Date.now(); let buf = ''; let resolved = false;
    // stdout buffer 上限——防御性保护，避免后端异常时持续输出 stdout 导致内存累积。
    // 后端启动期间正常输出 < 100KB（YUNCAT_STAGE 信号 + 调试日志），10MB 是 100x 余量。
    // 超过上限时拒绝启动并报告错误，避免 OOM。
    const MAX_STDOUT_BUF = 10 * 1024 * 1024;
    const heartbeat = setInterval(() => { if (!resolved) try { wins?.broadcast('backend-starting', 'waiting'); } catch {} }, 5000);
    const onData = (chunk) => {
      // 后端启动完成后仍持续排空 stdout 到日志，避免管道写满(64KB)后阻塞后端 write
      if (resolved) { appendLog(chunk.toString()); return; }
      buf += chunk.toString();
      if (buf.length > MAX_STDOUT_BUF) {
        cleanup();
        reject(new Error(`Backend stdout buffer exceeded ${MAX_STDOUT_BUF} bytes without YUNCAT_READY — check backend.log for errors`));
        return;
      }
      const lines = buf.split('\n');
      const stdoutLogs = []; // EL2: 批量拼接，逐行 appendFileSync → 单次 append
      while (lines.length > 1) {
        const line = lines.shift().trim();
        if (line.startsWith('YUNCAT_STAGE ')) {
          // Phase 49-H: 后端启动阶段信号——转发给前端 StartupScreen 更新进度
          const stageName = line.slice('YUNCAT_STAGE '.length).trim();
          if (stageName) try { wins?.broadcast('backend-starting', stageName); } catch {}
        }
        if (line.startsWith('YUNCAT_READY ')) {
          try { const idx = line.indexOf('{'); if (idx >= 0) { const data = JSON.parse(line.slice(idx)); if (data.port) {
            resolved = true;
            // 启动完成：停止解析与超时，但保留 stdout 持续排空（见 onData），让后端日志正常落盘
            clearInterval(timer); clearInterval(heartbeat);
            proc.removeListener('exit', onExit);
            resolve(data.port); return;
          } } } catch {}
        }
        stdoutLogs.push(`[stdout] ${line}`);
      }
      if (stdoutLogs.length) appendLog(stdoutLogs.join('\n'));
      buf = lines[0] || '';
    };
    const onExit = (code) => { if (!resolved) { cleanup(); reject(new Error(`Backend exited with code ${code}`)); } };
    const timer = setInterval(() => { if (!resolved && Date.now() - start > timeoutSecs * 1000) { cleanup(); reject(new Error('Backend startup timeout')); } }, 500);
    function cleanup() { clearInterval(timer); clearInterval(heartbeat); proc.stdout.removeListener('data', onData); proc.removeListener('exit', onExit); }
    proc.stdout.on('data', onData); proc.on('exit', onExit);
  });
}

// ── 后端退出监控（共享：startup + restart） ──
// E9: 意外崩溃自动重启——指数退避 1s/5s/15s，10 分钟滑动窗口内最多 3 次，
// 超限后只发 backend-error 让用户手动重启（防止崩溃风暴反复拖垮机器）。
const _CRASH_RESTART_DELAYS_MS = [1000, 5000, 15000];
const _CRASH_RESTART_WINDOW_MS = 10 * 60 * 1000;
let _crashRestartCount = 0;
let _crashRestartWindowStart = 0;

function monitorBackendExit(proc) {
  let exitResolved = false, _exitCode = null;
  proc.on('exit', (code) => { if (!exitResolved) { exitResolved = true; _exitCode = code; } });
  return (async () => {
    await new Promise(r => { if (exitResolved) r(); else proc.once('exit', r); });
    // E3: 身份校验——restart/shutdown 已拉起新进程后，旧进程延迟 exit 不得清掉新引用
    if (proc !== pythonProcess) return;
    backendPort = null; pythonProcess = null;
    if (!pythonRestarting && _exitCode !== 0) {
      appendLog(`[system] backend exited (code ${_exitCode})`);
      const now = Date.now();
      if (now - _crashRestartWindowStart > _CRASH_RESTART_WINDOW_MS) {
        _crashRestartWindowStart = now; _crashRestartCount = 0;
      }
      if (_crashRestartCount < _CRASH_RESTART_DELAYS_MS.length) {
        const delay = _CRASH_RESTART_DELAYS_MS[_crashRestartCount++];
        appendLog(`[system] backend crashed — auto restart in ${delay}ms (attempt ${_crashRestartCount}/${_CRASH_RESTART_DELAYS_MS.length})`);
        wins?.broadcast('backend-starting', 'auto-restarting');
        setTimeout(() => {
          // 防抖：等待期间用户已手动重启/退出、或后端已被其它路径拉起，则放弃自动重启
          if (isQuitting || pythonRestarting || pythonProcess) return;
          pythonRestarting = true;
          startBackend().catch(e => console.error('[auto-restart]', e.message))
            .finally(() => { pythonRestarting = false; });
        }, delay);
      } else {
        wins?.broadcast('backend-error', `backend exited with code ${_exitCode} (auto-restart limit reached, please restart manually)`);
      }
    }
  })();
}

// ── Batch 7.6: token 兑换/刷新 ──
function _postJson(port, path, headers, body) {
  return new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : '';
    const req = http.request({
      hostname: '127.0.0.1',
      port,
      path,
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(data),
        ...headers,
      },
    }, (res) => {
      let chunks = '';
      res.on('data', (chunk) => { chunks += chunk; });
      res.on('end', () => {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          try { resolve(JSON.parse(chunks)); } catch { resolve(chunks); }
        } else {
          reject(new Error(`HTTP ${res.statusCode}: ${chunks}`));
        }
      });
    });
    req.on('error', reject);
    // EL2: 10s 超时——后端挂起时不无限等待，destroy 触发 error → reject
    req.setTimeout(10000, () => req.destroy(new Error('request timeout after 10s')));
    if (data) req.write(data);
    req.end();
  });
}

// 2026-10-07：`timeout` 也算可重试。实测 10-06 启动期同步 embedding 堵住事件循环 90s，
// 端口迟迟无法 listen，主进程收到的是 timeout 而非 ECONNREFUSED 就一次放弃。
async function exchangeToken(port, attempt = 0) {
  try {
    const result = await _postJson(port, '/auth/token', { Authorization: `Bearer ${token}` });
    accessToken = result.access_token || null;
    refreshToken = result.refresh_token || null;
    if (!accessToken || !refreshToken) throw new Error('backend did not return tokens');
    return { ok: true };
  } catch (e) {
    if (attempt * 500 < 300_000 && /ECONNREFUSED|ECONNRESET|socket hang up|EPIPE|timeout/i.test(e.message)) {
      if (attempt % 6 === 0) { wins?.broadcast('backend-starting', 'waiting for port'); }
      await new Promise(r => setTimeout(r, 500));
      return exchangeToken(port, attempt + 1);
    }
    accessToken = null; refreshToken = null;
    console.error('[Yuncat] token exchange failed:', e.message);
    appendLog(`[system] token exchange failed: ${e.message}`);  // 留痕：失败会 kill 后端
    return { ok: false, error: e.message };
  }
}

async function refreshAccessToken() {
  // 用 refresh token 换取新的 access token。
  // Batch A H4: 后端实现了 refresh token 轮换（RFC 6749 §10.4），旧 token 使用后即撤销，
  // 响应包含新的 refresh_token，必须更新本地存储，否则下次刷新会用已撤销的旧 token。
  if (!backendPort || !refreshToken) return { ok: false, error: 'no_refresh_token' };
  const oldRefreshToken = refreshToken;
  try {
    const result = await _postJson(backendPort, '/auth/refresh', {}, {
      refresh_token: oldRefreshToken,
    });
    accessToken = result.access_token || null;
    if (!accessToken) {
      throw new Error('backend did not return access token');
    }
    if (result.refresh_token) refreshToken = result.refresh_token;  // 存储轮换后的新 token
    return { ok: true, token: accessToken };
  } catch (e) {
    accessToken = null;   // 后端重启后旧 refresh token 也失效：回静态 token 重新兑换
    const recovered = await recoverAuthAfterRefreshFailure(e, () => exchangeToken(backendPort), () => accessToken);
    if (recovered.ok) return recovered;   // 已换到新 token
    refreshToken = null; // 旧 token 已在后端被撤销，清除本地引用
    console.error('[Yuncat] token refresh failed:', e.message);
    return { ok: false, error: e.message };
  }
}

async function startBackend() {  // 不收窗口参数，状态一律 wins.broadcast()
  await ensureRuntimesAsync();
  const loc = findPython();
  if (!fs.existsSync(loc.python)) { wins?.broadcast('backend-error', `Python not found: ${loc.python}`); return; }
  if (!fs.existsSync(loc.main)) { wins?.broadcast('backend-error', `main.py not found: ${loc.main}`); return; }

  // 残留进程清理由 cleanupStalePid() 在 whenReady 阶段按 PID 精确处理（不用窗口标题/端口清理）

  wins?.broadcast('backend-starting', 'spawning');
  let port;
  try {
    port = await findFreePortWithRetry(3, 200);
  } catch {
    wins?.broadcast('backend-error', 'No free port available');
    return;
  }
  const proc = spawnBackend(loc.python, loc.main, port);
  pythonProcess = proc;

  try {
    const actualPort = await waitForReady(proc, 300);  // Phase 49: 300s（torch + chromadb 冷加载，低配置机器需要更久）
    // G2 修复：使用 waitForReady 从 YUNCAT_READY 返回的实际端口——不是 Electron findFreePort 的端口
    const exchange = await exchangeToken(actualPort);
    if (!exchange.ok) {
      wins?.broadcast('backend-error', `token exchange failed: ${exchange.error}`);
      try { proc.kill(); } catch {}
      return;
    }
    backendPort = actualPort; wins?.broadcast('backend-ready', [actualPort, accessToken]);
    _applyCspForPort(port);  // Phase 48 T1-2: reload 使 CSP 携带实际端口
    writePid();
  } catch (e) {
    try { proc.kill(); } catch {}
    if (proc.exitCode == null) { try { await new Promise(r => { proc.on('exit', r); setTimeout(r, 1000); }); } catch {} }
    wins?.broadcast('backend-error', proc.exitCode === 1 ? 'backend crashed (code 1) — check backend.log' : `startup failed: ${e.message}`);
    return;
  }
    // 共享退出监控——异步不阻塞 startup
    monitorBackendExit(proc).catch(e => console.error('[monitorBackendExit]', e.message));
  // L1 修复：启动日志不输出 token 前缀——即使 8 字符也可能泄露到日志文件/issue 报告
  console.log('[Yuncat] backend started on port', port, 'token: set');
}

// ── IPC ──
function setupIPC() {
  // E9: 永不下发静态 master token——accessToken 为空时先尝试补兑换/刷新一次
  // （10s 冷却防轮询风暴）；仍为空则返回 null，前端 useBackendReady 视为未就绪，
  // 继续轮询或等 backend-ready 事件。
  let _lastTokenRescueAt = 0;
  ipcMain.handle('get_backend_info', async () => {
    if (!accessToken && backendPort && Date.now() - _lastTokenRescueAt > 10_000) {
      _lastTokenRescueAt = Date.now();
      if (refreshToken) await refreshAccessToken();
      else await exchangeToken(backendPort);
    }
    return [backendPort || 0, accessToken];
  });

  // 多窗口：渲染进程请求新开窗口（命令面板「新建窗口」、Ctrl+Shift+N 的窗口内路径）
  ipcMain.handle('new_window', () => {
    const win = createWindow();
    return { ok: !!win };
  });

  // Batch 7.6: 前端触发 access token 刷新（主进程持有 refresh token）
  ipcMain.handle('refresh-access-token', async () => {
    const result = await refreshAccessToken();
    if (result.ok) {
      return { ok: true, token: result.token };
    }
    return { ok: false, error: result.error };
  });

  ipcMain.handle('restart_backend', async () => {
    if (!wins || wins.alive().length === 0) return { ok: false, error: 'no_window' };
    pythonRestarting = true; wins.broadcast('backend-starting', 'restarting');
    // E3: kill() 只发信号给直接子进程，hermes/openclaw 孙进程需树杀——与
    // shutdownBackend 同模式（taskkill /F /PID <pid> /T）。killed 在 kill()
    // 后恒为 true（只表示信号已发出），存活判定必须用 exitCode === null。
    try { pythonProcess?.kill(); } catch {}; await new Promise(r => setTimeout(r, 2000));
    if (pythonProcess && pythonProcess.exitCode === null) {
      try { if (process.platform === 'win32') execSync(`taskkill /F /PID ${pythonProcess.pid} /T`, { windowsHide: true, timeout: 5000 }); else pythonProcess.kill('SIGKILL'); } catch {}
    }
    pythonProcess = null; backendPort = null;
    const loc = findPython(); let port;
    try {
      port = await findFreePortWithRetry(3, 200);
    } catch {
      wins.broadcast('backend-error', 'No free port available');
      return { ok: false, error: 'no_port' };
    }
    pythonProcess = spawnBackend(loc.python, loc.main, port);
    pythonRestarting = false;  // 重置标志
    try {
      // 重启场景与启动场景使用相同的 300s 超时——
      // 即使模块已 OS 缓存，chromadb/torch 重新加载在低配机器上仍可能较慢。
      // 之前 120s 在冷缓存重启时可能导致超时失败。
      await waitForReady(pythonProcess, 300);
      // Batch 7.6: 重启后重新兑换 token
      const exchange = await exchangeToken(port);
      if (!exchange.ok) {
        wins.broadcast('backend-error', `token exchange failed: ${exchange.error}`);
        return { ok: false, error: exchange.error };
      }
      backendPort = port; wins.broadcast('backend-ready', [port, accessToken]);
      _applyCspForPort(port);  // Phase 48 T1-2: reload 使 CSP 携带实际端口（重启换端口时）
      writePid();
      monitorBackendExit(pythonProcess).catch(e => console.error('[monitorBackendExit]', e.message));  // 注册退出监控
      return { ok: true, port };
    }
    catch (e) { pythonRestarting = false; wins.broadcast('backend-error', `restart failed: ${e.message}`); return { ok: false, error: e.message }; }
  });

  ipcMain.handle('set_titlebar_theme', (_event, theme) => {
    wins?.applyTitleBarTheme(theme);
  });

  // ── 系统通知（长任务完成/失败时弹出） ──
  ipcMain.handle('show_notification', (_event, { title, body, link } = {}) => {
    if (!title) return { ok: false, error: 'no_title' };
    if (Notification.isSupported()) {
      const n = new Notification({ title, body: body || '', icon: path.join(__dirname, 'icon.ico') });
      n.on('click', () => {
        const target = wins?.focusOrCreate();
        // 点击通知导航到对应内容（link 为前端路由，如 /chat?task=xxx）
        if (target && link) wins.sendWhenReady(target, 'notification-click', link);
      });
      n.show();
      return { ok: true };
    }
    return { ok: false, error: 'not_supported' };
  });

  // ── 文件/文件夹选择器 ──
  ipcMain.handle('open_file_dialog', async (event, options = {}) => {
    // 多窗口：对话框挂在「发起请求的那个窗口」上，否则会变成另一个窗口的模态
    const owner = wins?.byWebContentsId(event.sender.id) || wins?.focused();
    if (!owner) return { ok: false, error: 'no_window' };
    // 参数名对齐前端：selectFolder → openDirectory，selectFile → openFile
    const result = await dialog.showOpenDialog(owner, {
      properties: [options.selectFolder ? 'openDirectory' : 'openFile',
                   ...(options.multiSelections ? ['multiSelections'] : [])],
      filters: options.filters || [],
      title: options.title || undefined,
    });
    if (result.canceled) return { ok: false, canceled: true };
    return { ok: true, path: options.multiSelections ? result.filePaths : result.filePaths[0] };
  });

  // ── 用系统默认应用打开文件（主进程二次校验路径安全） ──
  ipcMain.handle('open_path', async (_event, filePath) => {
    if (!filePath) return { ok: false, error: 'no_path' };
    if (!_isSafePath(filePath)) return { ok: false, error: 'unsafe_path' };
    const error = await shell.openPath(filePath);
    if (error) return { ok: false, error };
    return { ok: true };
  });

  // ── Phase 92: 崩溃报告 IPC ──
  // 前端启动时查询最近未处理的 crash（已 dismissed 返回 null）
  ipcMain.handle('crash:get_last', async () => {
    try {
      return { ok: true, crash: crashHandler.getLastCrash() };
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  });
  // 打开 crash-reports 目录（shell.openPath 不走 _isSafePath——crash 目录是 app 可信路径）
  ipcMain.handle('crash:open_dir', async () => {
    try {
      const dir = crashHandler.getCrashDir();
      if (!dir) return { ok: false, error: 'not_initialized' };
      // 确保目录存在
      try { fs.mkdirSync(dir, { recursive: true }); } catch { /* ignore */ }
      const error = await shell.openPath(dir);
      if (error) return { ok: false, error };
      return { ok: true };
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  });
  // 标记当前 crash 已处理（写 .crash-dismissed 标记，下次启动不弹窗）
  ipcMain.handle('crash:dismiss', async () => {
    try {
      crashHandler.dismissCrash();
      return { ok: true };
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  });
  // 重启应用——app.relaunch() 重新启动进程，app.exit() 退出当前进程
  ipcMain.handle('crash:relaunch', async () => {
    try {
      crashHandler.dismissCrash();  // 重启前标记已处理，避免下次启动再弹窗
      // E4: 先优雅退出后端（HTTP shutdown + 树杀兜底）再 relaunch——
      // 直接 app.exit 会跳过清理，Python/hermes/openclaw 子进程将成为孤儿
      try { await shutdownBackend(); } catch (e) { console.error('[crash:relaunch] shutdownBackend failed:', e.message); }
      app.relaunch();
      app.exit(0);
      return { ok: true };
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  });

  // ── Phase 95: 开机自启 IPC ──
  // 用 app.setLoginItemSettings 写入系统登录项（Windows: HKCU Run / macOS: LSSharedFileList / Linux: autostart .desktop）
  ipcMain.handle('autostart:get', async () => {
    try { return { ok: true, enabled: getAutostart() }; }
    catch (e) { return { ok: false, error: String(e) }; }
  });
  ipcMain.handle('autostart:set', async (_event, enabled) => {
    try {
      const ok = setAutostart(!!enabled);
      // 同步托盘菜单勾选状态
      try { tray?.setContextMenu(buildTrayMenu()); } catch { /* ignore */ }
      // 通知所有 renderer 偏好已变化
      wins?.broadcast('preference-changed', { key: 'autostart', value: getAutostart() });
      return { ok, enabled: getAutostart() };
    } catch (e) { return { ok: false, error: String(e) }; }
  });

  // ── 用户偏好 IPC（minimizeToTray 等 Electron 层配置） ──
  ipcMain.handle('preferences:get', async (_event, key) => {
    try { return { ok: true, key, value: getUserPreference(key) }; }
    catch (e) { return { ok: false, error: String(e) }; }
  });
  ipcMain.handle('preferences:set', async (_event, key, value) => {
    try {
      setUserPreference(key, value);
      // 如果是托盘行为变化，即时生效；通知 renderer 同步
      wins?.broadcast('preference-changed', { key, value });
      return { ok: true, key, value: getUserPreference(key) };
    } catch (e) { return { ok: false, error: String(e) }; }
  });

  // ── 在资源管理器中显示文件（主进程二次校验路径安全） ──
  ipcMain.handle('show_item_in_folder', (_event, filePath) => {
    if (!filePath) return { ok: false, error: 'no_path' };
    if (!_isSafePath(filePath)) return { ok: false, error: 'unsafe_path' };
    shell.showItemInFolder(filePath);
    return { ok: true };
  });

  // ── 自动更新 ──
  ipcMain.handle('check_for_updates', async () => {
    if (!app.isPackaged) return { ok: false, error: 'not packaged' };
    const updater = getAutoUpdater();
    if (!updater) return { ok: false, error: 'autoUpdater not available' };
    try { const result = await updater.checkForUpdates(); return { ok: true, ...result }; }
    catch (e) { return { ok: false, error: e.message }; }
  });

  ipcMain.handle('download_update', async () => {
    if (!app.isPackaged) return { ok: false, error: 'not packaged' };
    const updater = getAutoUpdater();
    if (!updater) return { ok: false, error: 'autoUpdater not available' };
    try { await updater.downloadUpdate(); return { ok: true }; }
    catch (e) { return { ok: false, error: e.message }; }
  });

  ipcMain.handle('install_update', () => {
    if (!app.isPackaged) return { ok: false, error: 'not packaged' };
    const updater = getAutoUpdater();
    if (!updater) return { ok: false, error: 'autoUpdater not available' };
    updater.quitAndInstall();
    return { ok: true };
  });
}

// ── 首次启动的运行时解压（splash + 7z 离线包）已抽到 runtime-bootstrap.js ──
// 抽出的理由：main.js 只做「装配」；解压进度与 splash 生命周期是独立关注点，也便于单测。

// ── 窗口（多窗口：一个后端进程 + 多个窗口，对标 VS Code / Cursor）──
// 窗口的创建/登记/几何状态/关闭语义都在 windows.js；这里只保留应用菜单与主窗口指针。
function buildApplicationMenu() {
  const isDev = !app.isPackaged;
  return Menu.buildFromTemplate([
    {
      label: '文件',
      submenu: [
        { label: '新建窗口', accelerator: 'CmdOrCtrl+Shift+N', click: () => createWindow() },
        { label: '新建对话', click: () => sendToRendererWhenReady('new-chat') },
        { type: 'separator' },
        { label: '关闭窗口', role: 'close' },
        { label: '退出', role: 'quit' },
      ],
    },
    {
      label: '编辑',
      submenu: [
        { role: 'undo' }, { role: 'redo' }, { type: 'separator' },
        { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' },
      ],
    },
    {
      label: '视图',
      submenu: [
        ...(isDev ? [{ role: 'reload' }, { role: 'toggleDevTools' }, { type: 'separator' }] : []),
        { role: 'zoomIn' }, { role: 'zoomOut' }, { role: 'resetZoom' },
        ...(browserIntegration ? browserIntegration.menuItems() : []),
      ],
    },
  ]);
}

function createWindow() {
  if (!wins) return null;
  const isFirst = wins.alive().length === 0;
  const win = wins.create();
  if (!win) return null;
  mainWindow = win;
  // 开发模式只在首个窗口自动打开 DevTools，避免多窗口时每个都弹一个面板
  if (isFirst && !app.isPackaged && process.env.YUNCAT_DEVTOOLS !== '0') {
    try { win.webContents.openDevTools(); } catch (e) { console.warn('[devtools]', e.message); }
  }
  return win;
}

// ── Phase 95: 开机自启 —— 默认关闭，用户偏好转持久化 ──
// 读取系统登录项当前状态；启动时如果 preferences.json 不存在，会强制同步为 false。
function _getSystemAutostart() {
  try { return !!app.getLoginItemSettings().openAtLogin; } catch { return false; }
}
function _setSystemAutostart(enabled) {
  try { app.setLoginItemSettings({ openAtLogin: !!enabled }); return true; } catch { return false; }
}

function getAutostart() {
  // 以 preferences.json 为准；若文件不存在则默认 false 并同步系统登录项。
  return getUserPreference('autostart', false);
}

function setAutostart(enabled) {
  const ok = _setSystemAutostart(enabled);
  if (ok) setUserPreference('autostart', !!enabled);
  return ok;
}

// 启动时确保系统登录项与偏好一致（首次安装后默认关闭）。
function _syncAutostartOnLaunch() {
  const prefs = _loadPreferences();
  const hasExplicitPreference = prefs.hasOwnProperty('autostart');
  if (!hasExplicitPreference) {
    // 首次启动：默认关闭，覆盖系统可能遗留的旧登录项。
    _setSystemAutostart(false);
    setUserPreference('autostart', false);
  } else {
    _setSystemAutostart(!!prefs.autostart);
  }
}

// ── 托盘 ──
// buildTrayMenu 独立为函数——开机自启状态变化时重建菜单更新勾选
function buildTrayMenu() {
  return Menu.buildFromTemplate([
    { label: '新建窗口', click: () => { createWindow(); } },
    { label: '显示窗口', click: () => { wins?.focusOrCreate(); } },
    { type: 'separator' },
    // Phase 95: 开机自启 checkable 项——勾选状态实时反映系统登录项
    { label: '开机自启', type: 'checkbox', checked: getAutostart(), click: (item) => {
      const next = !getAutostart();
      setAutostart(next);
      item.checked = next;  // 立即更新视觉状态
      // 同步前端设置页开关（若已加载）
      try { wins?.broadcast('autostart-changed', next); } catch { /* ignore */ }
    } },
    { type: 'separator' },
    { label: '退出 Yuncat', click: () => { isQuitting = true; shutdownAndQuit(); } },
  ]);
}
function createTray() {
  let icon = nativeImage.createEmpty();
  try { const p = path.join(__dirname, 'icon.ico'); if (fs.existsSync(p)) icon = nativeImage.createFromPath(p); } catch {}
  tray = new Tray(icon); tray.setToolTip('Yuncat');
  tray.setContextMenu(buildTrayMenu());
  tray.on('click', () => { wins?.focusOrCreate(); });
}

// ── 退出流程 (对标 VSCode: graceful shutdown → kill → force-quit) ──
// PID 文件 —— 启动时杀上次残留
const PID_FILE = path.join(appDataDir, 'backend.pid');

// ── 系统关机 / 安装器关闭 检测 ──
// Windows 关机、重启或 NSIS 安装器使用 Restart Manager / WM_CLOSE 关闭应用时，
// 必须立即退出而不是隐藏到托盘。Electron 默认 close 事件会被托盘逻辑拦截，
// 这里通过全局标志让 close 事件直接放行。
let _systemShutdown = false;
let _installerClose = false;

function _isForcedClose() {
  return _quitting || _systemShutdown || _installerClose;
}

// ── 安装器关闭检测 ──
// NSIS 安装/升级前会写入 $APPDATA\Yuncat\.installer-close，
// 提示 close 事件：这是安装器在关闭，不要隐藏到托盘，直接放行。
// 对标 VS Code / Discord：安装器与应用通过文件标记通信，避免 WM_CLOSE 被托盘逻辑拦截。
function _isInstallerClose() {
  try {
    return fs.existsSync(path.join(appDataDir, '.installer-close'));
  } catch { return false; }
}

// Windows 下监听系统关机/重启事件（WM_QUERYENDSESSION）
// 对标 VS Code：收到系统关闭信号后立即标记退出，不再阻止窗口关闭。
const { powerMonitor } = require('electron');
if (process.platform === 'win32') {
  // Electron 官方文档（powerMonitor 'shutdown' 事件）：
  //   "If the event handler invokes e.preventDefault(), Electron will attempt to delay
  //    system shutdown in order for the app to exit cleanly. If e.preventDefault() is
  //    called, the app should exit as soon as possible by calling something like app.quit()."
  // 原代码未 preventDefault，回调返回后系统立即关机，shutdownBackend(3000) 的异步清理
  // 被强制中断，Python 子进程可能成为孤儿直到下次 cleanupStalePid 才清理。
  // 修复：preventDefault 阻止立即关机 → 异步 quit() 完成 shutdownBackend 后调用 app.exit(0)
  // 退出进程，OS 看到进程退出后继续关机流程。
  powerMonitor.on('shutdown', (event) => {
    console.log('[quit] system shutdown detected — deferring for graceful shutdown');
    event.preventDefault();
    _systemShutdown = true;
    isQuitting = true;
    quit(true);  // forced=true → shutdownBackend 用 8s 短超时（后端 6s shutdown + 2s 余量），
                 //                避免被系统判定为无响应但又能完成 DB flush
  });
  // electron-updater / NSIS 可能通过 WM_CLOSE 关闭窗口，
  // powerMonitor 的 shutdown 事件在普通卸载时未必触发，因此再监听 session-end。
  app.on('session-end', () => {
    console.log('[quit] session-end detected');
    _systemShutdown = true;
    isQuitting = true;
  });
}

function writePid() {
  try { fs.mkdirSync(appDataDir, { recursive: true }); fs.writeFileSync(PID_FILE, String(pythonProcess?.pid || '')); } catch {}
}

// E8: PID 复用防护——仅当该 PID 的可执行文件位于 Yuncat 自身目录
// （安装目录 / 当前 Python 运行时目录）内才视为残留进程。查询失败或路径
// 不匹配则跳过：宁可漏杀（后端有 YUNCAT_PARENT_PID 自杀兜底，下次启动还会再查），
// 不可误杀被系统复用了该 PID 的无关进程。
function _isYuncatOwnedPid(pid) {
  try {
    const out = execSync(
      `powershell.exe -NoProfile -NonInteractive -Command "(Get-Process -Id ${pid} -ErrorAction SilentlyContinue).Path"`,
      { windowsHide: true, timeout: 5000, encoding: 'utf8' }
    ).trim();
    if (!out) return false;
    const exePath = path.resolve(out).toLowerCase();
    const roots = [path.dirname(process.execPath), path.dirname(findPython().python)]
      .map(d => path.resolve(d).toLowerCase());
    return roots.some(d => exePath === d || exePath.startsWith(d + path.sep));
  } catch (e) {
    console.warn('[cleanupStalePid] pid identity check failed:', e.message);
    return false;
  }
}

function cleanupStalePid() {
  try {
    if (fs.existsSync(PID_FILE)) {
      const oldPid = parseInt(fs.readFileSync(PID_FILE, 'utf8').trim(), 10);
      if (oldPid && oldPid !== process.pid && process.platform === 'win32') {
        if (_isYuncatOwnedPid(oldPid)) {
          try { spawn('taskkill', ['/F', '/PID', String(oldPid), '/T'], { windowsHide: true }); } catch {}
        } else {
          console.log('[cleanupStalePid] skip kill: pid', oldPid, 'is not a Yuncat-owned process (reused or gone)');
        }
      }
      try { fs.unlinkSync(PID_FILE); } catch {}
    }
  } catch {}
}

async function shutdownBackend(timeoutMs = 6000) {
  const start = Date.now();
  const remaining = () => Math.max(0, timeoutMs - (Date.now() - start));

  if (backendPort) {
    try {
      await new Promise((resolve) => {
        // 2026-09-15: 补 Authorization——后端 /api/** 全要 Bearer，裸 GET 恒 403，优雅退出从未生效
        const req = http.get(`http://127.0.0.1:${backendPort}/api/v1/shutdown`, { headers: { Authorization: `Bearer ${accessToken || token}` } }, (res) => {
          let data = '';
          res.on('data', d => data += d);
          res.on('end', () => { console.log('[shutdown] backend response:', res.statusCode, data.slice(0, 200)); resolve(); });
        });
        req.setTimeout(Math.min(remaining(), 3000), () => { req.destroy(); console.log('[shutdown] http timeout — forcing kill'); resolve(); });
        req.on('error', (e) => { console.log('[shutdown] http error:', e.message); resolve(); });
      });
    } catch {}
  }
  pythonRestarting = true;
  // 先等优雅退出：/shutdown 响应后 Python 延迟 0.1s 触发 SIGINT，跑 lifespan
  // finally（flush DB、adapter.stop、SESSION_END）。给足宽限时间，避免硬杀打断 flush。
  if (pythonProcess && pythonProcess.exitCode === null) {
    await new Promise((resolve) => {
      let settled = false;
      const exitTimeout = setTimeout(() => { settled = true; resolve(); }, remaining());
      pythonProcess.once('exit', () => { if (!settled) { clearTimeout(exitTimeout); resolve(); } });
    });
  }
  // 优雅退出超时 → 树杀兜底。存活判定用 exitCode === null（kill 后 killed 恒 true、
  // 只表示信号已发出）；Windows 用 taskkill /T 树杀孙进程，与 restart_backend 对齐。
  if (pythonProcess && pythonProcess.exitCode === null) {
    try {
      if (process.platform === 'win32') {
        execSync(`taskkill /F /PID ${pythonProcess.pid} /T`, { windowsHide: true, timeout: Math.min(remaining(), 5000) });
      } else {
        pythonProcess.kill('SIGKILL');
      }
    } catch {}
  }
  pythonProcess = null; backendPort = null;
  try { fs.unlinkSync(PID_FILE); } catch {}
}

function shutdownAndQuit() {
  quit();
}

// ── 自动更新 ──
// Phase 94: 校验失败检测——electron-updater 在 sha512/blockmap 校验失败时
// 抛出含 sha512|checksum|signature|hash|integrity 关键字的 error。检测到时
// 弹原生错误框拒绝安装，并转发 update-error 事件到渲染进程（含
// isVerificationError 标志），前端据此显示专门校验失败文案。
const _VERIFICATION_ERROR_RE = /sha512|checksum|signature|hash mismatch|integrity/i;
function _isVerificationError(message) {
  return typeof message === 'string' && _VERIFICATION_ERROR_RE.test(message);
}

function setupAutoUpdater() {
  if (!app.isPackaged) return;
  const updater = getAutoUpdater(); if (!updater) return;
  updater.autoDownload = false; updater.autoInstallOnAppQuit = true;
  // E10: 差分下载保持关闭——electron-builder.yml 已设 differentialPackage: false
  // （无 .blockmap 生成），运行时开启只会每次 404 再回退全量下载。
  // TODO: 接入代码签名（Azure Trusted Signing，electron-builder sign 配置）后，
  // 重新启用 differentialPackage + blockmap，方可恢复 updater.differentialDownload = true。
  updater.on('checking-for-update', () => wins?.broadcast('checking-for-update'));
  updater.on('update-available', (info) => wins?.broadcast('update-available', info));
  updater.on('update-not-available', (info) => wins?.broadcast('update-not-available', info));
  updater.on('download-progress', (progress) => {
    wins?.broadcast('download-progress', { percent: progress && typeof progress.percent === 'number' ? progress.percent : 0 });
  });
  updater.on('update-downloaded', (info) => wins?.broadcast('update-downloaded', info));
  // Phase 94: 校验失败检测 + update-error 事件转发
  updater.on('error', (err) => {
    const msg = (err && err.message) ? err.message : String(err);
    const isVerificationError = _isVerificationError(msg);
    console.error('AutoUpdater error:', msg, 'isVerificationError=', isVerificationError);
    if (isVerificationError) {
      dialog.showErrorBox('更新校验失败', '下载的安装包校验不匹配，可能被篡改或下载不完整，请从官网重新下载。');
    }
    wins?.broadcast('update-error', { message: msg, isVerificationError });
  });
  setTimeout(() => updater.checkForUpdates().catch(() => {}), 10000);
}

// ── App 启动 ──
// ── 窗口管理器（多窗口）──
// 依赖注入而非直接 require electron：便于用 Node 单测窗口登记/几何逻辑（见 tests/electron）。
wins = createWindowManager({
  electron: { BrowserWindow, Menu, screen, dialog, shell, nativeTheme, app },
  appDataDir, fs, path,
  buildMenu: buildApplicationMenu,
  getUserPreference,
  isQuitting: () => isQuitting || _quitting,
  isForcedClose: _isForcedClose,
  isAllowedLocalUrl: _isAllowedLocalUrl,
  externalLinkWhitelist: _EXTERNAL_LINK_WHITELIST,
  crashHandler,
});

app.whenReady().then(async () => {
  // Windows 系统通知(toast)需显式设置 AppUserModelId，否则 Win10/11 可能静默不显示
  if (process.platform === 'win32') app.setAppUserModelId('com.yuncat.app');
  cleanupStalePid();             // 杀上次残留的旧进程
  await runtimeBootstrap.checkAndExtractRuntime(); // 首次启动时解压运行时组件（安装向导不解压）
  _syncAutostartOnLaunch();      // 首次启动默认关闭开机自启；后续同步偏好到系统登录项
  installContentSecurityPolicy({ app, getBackendPort: () => backendPort });
  setupIPC(); createWindow(); createTray();
  registerDeepLinkProtocol();
  registerGlobalShortcuts();
  handleArgv(process.argv);
  // 可选桥有明确超时；它异常时主后端仍继续启动。
  browserIntegration = await startOptionalBrowserIntegration(startBrowserIntegration, {
    electron: { BrowserWindow, WebContentsView, ipcMain, nativeTheme, shell, screen },
    appDataDir, fs, path, log: console,
  });
  setImmediate(() => {
    console.log('[startup] dispatching backend start');
    startBackend().catch(err => { appendLog(`[system] startBackend failed: ${err && err.stack || err}`); wins?.broadcast('backend-error', String(err)); });
  });
  setupAutoUpdater();
});

// ── 退出（所有路径的统一出口）──
// 对标 VSCode: 注册单出口 quit()，before-quit 统一路由到这里
let _quitting = false;
async function quit(forced = false) {
  if (_quitting) return;
  _quitting = true;
  isQuitting = true;
  _installerClose = forced;
  try {
    // 系统关机/安装器场景下给较短的关闭时间，避免被系统判定为无响应。
    // 8s = 后端 6s shutdown + 2s 安全余量。Windows 关机默认 5s 判定无响应，
    // 但 preventDefault 后 Electron 会向 OS 报告"正在关机"，实际窗口比 5s 宽松。
    // uvicorn timeout_graceful_shutdown=5s 需要 ≥5s 才能完成 DB flush，
    // 之前 3s 会强制中断 Python 后端导致 pending writes 丢失。
    await shutdownBackend(forced ? 8000 : 6000);
  } catch (e) {
    console.error('[quit] shutdown error:', e.message);
  }
  // 内置浏览器：先关标签与窗口，再关宿主桥，避免 Python 侧还在等一个已经消失的端点
  try {
    await browserIntegration?.close();
  } catch (e) {
    console.error('[quit] browser shutdown error:', e.message);
  }
  // 窗口几何 + 缩放：窗口管理器统一落盘（关闭时也会存，这里兜底退出路径）
  try { wins?.saveState(mainWindow); } catch (e) { console.error('[quit] save window state:', e.message); }
  // 对标 VSCode: 用 app.exit(0) 而非 app.quit()——不再触发 before-quit 循环
  app.exit(0);
}

app.on('before-quit', (event) => {
  if (!_quitting && pythonProcess && !pythonProcess.killed) {
    event.preventDefault();
    quit();
  }
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
});

app.on('open-url', (_event, url) => {
  handleDeepLink(url);
});

// ── 窗口关闭时不用托盘──直接退出（无托盘模式），Windows 下关闭窗口隐藏到托盘
app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
