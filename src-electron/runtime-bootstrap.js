"use strict";
/**
 * runtime-bootstrap.js —— 首次启动的运行时解压（splash + 7z 离线包）
 *
 * 为什么要单独成文件：main.js 只应负责「装配」（谁先起、谁依赖谁），而解压进度、
 * splash 生命周期、失败弹窗是另一件事。拆开后 main.js 不再继续膨胀
 * （AGENTS.md 硬约束 #4：文件 <800 行，超标文件不允许继续长大），
 * 这部分的逻辑也能脱离主进程单独验证。
 *
 * 依赖极简：只用 electron 的 BrowserWindow / dialog 与 node 内置模块，
 * 不读任何全局状态——需要用的东西都由 Electron 自身提供。
 */

const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");
const { BrowserWindow, dialog } = require("electron");

// ── 首次启动：解压运行时组件 ──
// 安装程序只复制 7z 文件，不解压；首次启动时由 Electron 自己解压。
// 原因：2 个 7z 解压后产生数万个文件，在 Windows Defender 等实时保护下，
// 安装向导内解压极慢。改为首次启动时显示进度画面，体验接近 Docker Desktop。
let splashWindow = null;

function _getResourcePath(...parts) {
  return path.join(process.resourcesPath, ...parts);
}

function _hasRuntimeArchives() {
  try {
    const res = process.resourcesPath;
    return fs.existsSync(path.join(res, 'python-core.7z')) ||
           fs.existsSync(path.join(res, 'python-ml.7z'));
  } catch { return false; }
}

function _createSplashWindow() {
  splashWindow = new BrowserWindow({
    width: 400, height: 320,
    frame: false, resizable: false, movable: true,
    alwaysOnTop: true, center: true, show: false,
    webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true }
  });
  const htmlPath = path.join(__dirname, 'splash.html');
  if (fs.existsSync(htmlPath)) {
    splashWindow.loadFile(htmlPath);
  } else {
    splashWindow.loadURL(`data:text/html,${encodeURIComponent('<h1>首次运行准备中...</h1>')}`);
  }
  splashWindow.once('ready-to-show', () => splashWindow?.show());
}

function _updateSplashProgress(percent, message) {
  try {
    if (!splashWindow || splashWindow.isDestroyed()) return;
    splashWindow.webContents.executeJavaScript(`
      window.postMessage({ type: 'progress', value: ${percent}, message: ${JSON.stringify(message)} }, '*');
    `).catch(() => {});
  } catch {}
}

async function _extractRuntime() {
  const script = _getResourcePath('extract-runtime.ps1');
  if (!fs.existsSync(script)) {
    throw new Error('extract-runtime.ps1 not found in resources');
  }
  return new Promise((resolve, reject) => {
    // process.resourcesPath == <installDir>\resources; extract-runtime.ps1
    // expects -TargetDir to be the application root (<installDir>) and then
    // resolves archives/7z.exe under <TargetDir>\resources.
    const appRoot = path.dirname(process.resourcesPath);
    const proc = spawn('powershell.exe', [
      '-ExecutionPolicy', 'Bypass',
      '-File', script,
      '-TargetDir', appRoot,
    ], {
      windowsHide: true,
      cwd: process.resourcesPath,
    });
    let stderr = '';
    proc.stdout.on('data', (data) => {
      const lines = data.toString().split(/\r?\n/);
      for (const line of lines) {
        const m = line.match(/^YUNCAT_EXTRACT_PROGRESS:(\d+):(.+)$/);
        if (m) {
          const pct = parseInt(m[1], 10);
          _updateSplashProgress(pct, m[2]);
        }
      }
    });
    proc.stderr.on('data', (d) => { stderr += d.toString(); });
    proc.on('error', reject);
    proc.on('exit', (code) => {
      if (code === 0) resolve();
      else reject(new Error(`extract-runtime.ps1 exited with ${code}: ${stderr}`));
    });
  });
}

async function checkAndExtractRuntime() {
  if (!_hasRuntimeArchives()) return;
  console.log('[runtime] first launch: extracting runtime archives...');
  _createSplashWindow();
  try {
    await _extractRuntime();
    console.log('[runtime] extraction complete');
  } catch (e) {
    console.error('[runtime] extraction failed:', e.message);
    dialog.showErrorBox('首次运行准备失败', `解压运行时组件失败：${e.message}\n\n请检查磁盘空间并重试。`);
    throw e;
  } finally {
    try { splashWindow?.close(); } catch {}
    splashWindow = null;
  }
}

module.exports = {
  checkAndExtractRuntime,
  // 下面的下划线名字保留原样，导出时给出对外别名，避免调用方被「内部命名」绑住
  hasRuntimeArchives: _hasRuntimeArchives,
  extractRuntime: _extractRuntime,
  getResourcePath: _getResourcePath,
};
