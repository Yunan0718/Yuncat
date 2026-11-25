// Phase 92: 崩溃报告——捕获 uncaughtException / render-process-gone / child-process-gone
// 写入 %APPDATA%/Yuncat/crash-reports/crash-{timestamp}-{reason}.log
// 保留最多 10 份，超出删最旧。默认不上传（隐私）。
//
// 设计要点：
// - crash log 含时间/版本/OS/错误栈/backend.log 最近 50 行摘要，便于离线诊断
// - .crash-dismissed 标记文件：用户点"忽略"后写标记，下次启动不弹窗
// - 新 crash 产生时清除 dismissed 标记（确保下次启动弹窗）
// - 所有 IO best-effort try/catch——crash-handler 自身不能抛异常

const fs = require('fs');
const path = require('path');
const os = require('os');

const MAX_CRASH_LOGS = 10;
const LOG_TAIL_LINES = 50;

let _crashDir = '';
let _backendLogPath = '';
let _dismissedMarker = '';

/**
 * 初始化崩溃处理器——注册 process 级 + app 级事件监听
 * main.js 在 app.whenReady 之前调用
 */
function init(app, appDataDir) {
  _crashDir = path.join(appDataDir, 'crash-reports');
  _backendLogPath = path.join(appDataDir, 'backend.log');
  _dismissedMarker = path.join(appDataDir, '.crash-dismissed');

  try { fs.mkdirSync(_crashDir, { recursive: true }); } catch { /* ignore */ }

  // 1. 主进程未捕获异常
  process.on('uncaughtException', (err) => {
    try {
      recordCrash(app, 'uncaughtException', err && err.stack ? err.stack : String(err), {});
    } catch { /* ignore */ }
    // 仍打印到 stderr——让 Electron 默认行为处理
    console.error('[Yuncat] uncaughtException:', err);
  });

  // 2. 子进程崩溃（Python 后端 / Node 子进程）
  app.on('child-process-gone', (_event, details) => {
    try {
      const stack = `Child process gone: type=${details.type}, reason=${details.reason}, exitCode=${details.exitCode}`;
      recordCrash(app, `child-process-gone:${details.type || 'unknown'}`, stack, details);
    } catch { /* ignore */ }
  });
}

/**
 * 记录 renderer 进程崩溃——由 main.js 的 webContents.on('render-process-gone') 调用
 */
function recordRendererCrash(app, details) {
  try {
    const stack = `Renderer process gone: reason=${details.reason}, exitCode=${details.exitCode}`;
    recordCrash(app, `renderer-gone:${details.reason || 'unknown'}`, stack, details);
  } catch { /* ignore */ }
}

/**
 * 核心记录函数——写 crash log + 清理旧文件 + 清除 dismissed 标记
 */
function recordCrash(app, reason, stack, details) {
  const ts = Date.now();
  const date = new Date(ts).toISOString();
  // reason 只保留安全字符做文件名
  const safeReason = String(reason).replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 50);
  const filename = `crash-${ts}-${safeReason}.log`;
  const filepath = path.join(_crashDir, filename);

  // 读 backend.log 最近 50 行
  let logTail = '';
  try {
    if (fs.existsSync(_backendLogPath)) {
      const content = fs.readFileSync(_backendLogPath, 'utf-8');
      const lines = content.split('\n').filter(Boolean);
      logTail = lines.slice(-LOG_TAIL_LINES).join('\n');
    }
  } catch { /* ignore */ }

  const body = [
    '=== Yuncat Crash Report ===',
    `Time: ${date}`,
    `Version: ${app.getVersion() || 'unknown'}`,
    `OS: ${os.type()} ${os.release()} (${os.platform()})`,
    `Reason: ${reason}`,
    `Details: ${JSON.stringify(details, null, 2)}`,
    '',
    '=== Stack ===',
    stack,
    '',
    `=== backend.log (last ${LOG_TAIL_LINES} lines) ===`,
    logTail || '(no log or empty)',
    '',
  ].join('\n');

  try {
    fs.writeFileSync(filepath, body, 'utf-8');
  } catch { /* ignore */ }

  // 清理超出上限的旧 log
  try { pruneOldCrashLogs(); } catch { /* ignore */ }

  // 有新 crash 时清除 dismissed 标记——下次启动应弹窗
  try {
    if (fs.existsSync(_dismissedMarker)) fs.unlinkSync(_dismissedMarker);
  } catch { /* ignore */ }
}

/**
 * 清理超出 MAX_CRASH_LOGS 的旧 crash log（按 mtime 降序保留最新）
 */
function pruneOldCrashLogs() {
  if (!fs.existsSync(_crashDir)) return;
  const files = fs.readdirSync(_crashDir)
    .filter((f) => f.startsWith('crash-') && f.endsWith('.log'))
    .map((f) => {
      const full = path.join(_crashDir, f);
      const stat = fs.statSync(full);
      return { name: f, path: full, mtime: stat.mtimeMs };
    })
    .sort((a, b) => b.mtime - a.mtime); // 新到旧

  for (const f of files.slice(MAX_CRASH_LOGS)) {
    try { fs.unlinkSync(f.path); } catch { /* ignore */ }
  }
}

/**
 * 获取最近未处理的 crash——供前端启动时调用
 * 返回 null 表示无 crash 或已 dismissed
 */
function getLastCrash() {
  try {
    if (!fs.existsSync(_crashDir)) return null;
    // 已 dismissed 则不弹窗
    if (fs.existsSync(_dismissedMarker)) return null;

    const files = fs.readdirSync(_crashDir)
      .filter((f) => f.startsWith('crash-') && f.endsWith('.log'))
      .map((f) => {
        const full = path.join(_crashDir, f);
        const stat = fs.statSync(full);
        return { name: f, path: full, mtime: stat.mtimeMs, size: stat.size };
      })
      .sort((a, b) => b.mtime - a.mtime);

    if (files.length === 0) return null;
    const latest = files[0];
    // 从文件名解析 timestamp：crash-{ts}-{reason}.log
    const match = latest.name.match(/^crash-(\d+)-.+\.log$/);
    const ts = match ? parseInt(match[1], 10) : latest.mtime;
    return {
      timestamp: ts,
      time: new Date(ts).toISOString(),
      file: latest.name,
      path: latest.path,
      size: latest.size,
    };
  } catch {
    return null;
  }
}

/**
 * 标记当前 crash 已处理——用户点"忽略"后调用
 */
function dismissCrash() {
  try {
    fs.writeFileSync(_dismissedMarker, String(Date.now()), 'utf-8');
  } catch { /* ignore */ }
}

/**
 * 返回 crash-reports 目录路径——供"打开 crash 目录"按钮用
 */
function getCrashDir() {
  return _crashDir;
}

module.exports = {
  init,
  recordRendererCrash,
  getLastCrash,
  dismissCrash,
  getCrashDir,
};
