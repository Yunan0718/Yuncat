// Yuncat preload — contextBridge 暴露 IPC 给前端
// 前端通过 window.yuncat 调用，代替 @tauri-apps/api

const { contextBridge, ipcRenderer } = require('electron');  // preload context — electron works here

// ── 路径安全校验：拒绝敏感系统目录 ──
// 正斜杠形式统一——与 main.js _isSafePath（path.resolve 规范化）对齐，
// 混合分隔符（foo/..\bar）与 C:/Windows 形式不再漏判（sandbox 内无 path 模块，用字符串替换等价实现）。
const _SENSITIVE_DIRS = /^(C:\/Windows|C:\/Program Files|C:\/Program Files \(x86\)|\/etc|\/usr|\/bin|\/sbin|\/root)(\/|$)/i;
function _isSafePath(filePath) {
  if (typeof filePath !== 'string' || !filePath) return false;
  const normalized = filePath.replace(/\\/g, '/');
  if (_SENSITIVE_DIRS.test(normalized)) return false;
  // 拒绝路径穿越
  if (normalized.includes('..')) return false;
  return true;
}

contextBridge.exposeInMainWorld('yuncat', {
  // 后端信息（替代 invoke("get_backend_info")）
  getBackendInfo: () => ipcRenderer.invoke('get_backend_info'),

  // 标题栏主题（替代 invoke("set_titlebar_theme")）
  setTitleBarTheme: (theme) => ipcRenderer.invoke('set_titlebar_theme', theme),

  // 自动更新（替代 invoke("check_for_updates") 等）
  checkForUpdates: () => ipcRenderer.invoke('check_for_updates'),
  downloadUpdate: () => ipcRenderer.invoke('download_update'),
  installUpdate: () => ipcRenderer.invoke('install_update'),

  // 事件监听（替代 listen("backend-ready"), listen("backend-error"), etc.）
  onBackendReady: (callback) => {
    const handler = (_event, data) => callback(data);
    ipcRenderer.on('backend-ready', handler);
    return () => ipcRenderer.removeListener('backend-ready', handler);
  },
  onBackendError: (callback) => {
    const handler = (_event, data) => callback(data);
    ipcRenderer.on('backend-error', handler);
    return () => ipcRenderer.removeListener('backend-error', handler);
  },
  onBackendStarting: (callback) => {
    const handler = (_event, data) => callback(data);
    ipcRenderer.on('backend-starting', handler);
    return () => ipcRenderer.removeListener('backend-starting', handler);
  },
  onTrayAction: (callback) => {
    const handler = (_event, data) => callback(data);
    ipcRenderer.on('tray-action', handler);
    return () => ipcRenderer.removeListener('tray-action', handler);
  },
  // 自动更新事件
  onUpdateAvailable: (callback) => {
    const handler = (_event, data) => callback(data);
    ipcRenderer.on('update-available', handler);
    return () => ipcRenderer.removeListener('update-available', handler);
  },
  onUpdateDownloaded: (callback) => {
    const handler = (_event, data) => callback(data);
    ipcRenderer.on('update-downloaded', handler);
    return () => ipcRenderer.removeListener('update-downloaded', handler);
  },
  // Phase 94: 更新校验失败 / 无可用更新 / 下载进度事件
  onUpdateError: (callback) => {
    const handler = (_event, data) => callback(data);
    ipcRenderer.on('update-error', handler);
    return () => ipcRenderer.removeListener('update-error', handler);
  },
  onUpdateNotAvailable: (callback) => {
    const handler = (_event, data) => callback(data);
    ipcRenderer.on('update-not-available', handler);
    return () => ipcRenderer.removeListener('update-not-available', handler);
  },
  onDownloadProgress: (callback) => {
    const handler = (_event, data) => callback(data);
    ipcRenderer.on('download-progress', handler);
    return () => ipcRenderer.removeListener('download-progress', handler);
  },

  // 全局快捷键 / 深度链接事件
  onNewChat: (callback) => {
    const handler = (_event, data) => callback(data);
    ipcRenderer.on('new-chat', handler);
    return () => ipcRenderer.removeListener('new-chat', handler);
  },
  onDeepLink: (callback) => {
    const handler = (_event, data) => callback(data);
    ipcRenderer.on('deep-link', handler);
    return () => ipcRenderer.removeListener('deep-link', handler);
  },

  // 开发工具 —— 重启后端
  restartBackend: () => ipcRenderer.invoke('restart_backend'),

  // 多窗口：新开一个窗口（命令面板 / Ctrl+Shift+N 的窗口内路径）
  newWindow: () => ipcRenderer.invoke('new_window'),

  // 内置浏览器：从应用界面打开。浏览器窗口独立存在，AI 与用户可以同时操作同一批标签页
  openBrowser: (url) => ipcRenderer.invoke('browser:app-open', typeof url === 'string' ? url : ''),
  browserState: () => ipcRenderer.invoke('browser:app-state'),

  // Batch 7.6: 触发主进程刷新 access token（refresh token 不暴露给 renderer）
  refreshAccessToken: () => ipcRenderer.invoke('refresh-access-token'),

  // 桌面通知 —— 长任务完成/失败时弹出系统通知（link 用于点击后导航）
  showNotification: (title, body, link) => ipcRenderer.invoke('show_notification', { title, body, link }),

  // 通知点击事件 —— 前端据此导航到对应内容
  onNotificationClick: (callback) => {
    const handler = (_event, link) => callback(link);
    ipcRenderer.on('notification-click', handler);
    return () => ipcRenderer.removeListener('notification-click', handler);
  },

  // 原生文件操作（替代所有自定义文件浏览器）——调用 OS 资源管理器/默认应用
  openPath: (filePath) => {
    if (!_isSafePath(filePath)) return Promise.resolve({ ok: false, error: 'unsafe_path' });
    return ipcRenderer.invoke('open_path', filePath);
  },
  showItemInFolder: (filePath) => {
    if (!_isSafePath(filePath)) return Promise.resolve({ ok: false, error: 'unsafe_path' });
    return ipcRenderer.invoke('show_item_in_folder', filePath);
  },
  openFileDialog: (options) => ipcRenderer.invoke('open_file_dialog', options),

  // Phase 92: 崩溃报告——前端启动时查询最近 crash / 打开 crash 目录 / 标记已处理 / 重启应用
  getLastCrash: () => ipcRenderer.invoke('crash:get_last'),
  openCrashDir: () => ipcRenderer.invoke('crash:open_dir'),
  dismissCrash: () => ipcRenderer.invoke('crash:dismiss'),
  relaunchApp: () => ipcRenderer.invoke('crash:relaunch'),

  // Phase 95: 开机自启——查询/设置系统登录项 + 托盘勾选变化事件
  getAutostart: () => ipcRenderer.invoke('autostart:get'),
  setAutostart: (enabled) => ipcRenderer.invoke('autostart:set', !!enabled),
  onAutostartChanged: (callback) => {
    const handler = (_event, data) => callback(data);
    ipcRenderer.on('autostart-changed', handler);
    return () => ipcRenderer.removeListener('autostart-changed', handler);
  },

  // Phase 96: 用户偏好——Electron 层持久化配置（minimizeToTray 等）
  getPreference: (key) => ipcRenderer.invoke('preferences:get', key),
  setPreference: (key, value) => ipcRenderer.invoke('preferences:set', key, value),
  onPreferenceChanged: (callback) => {
    const handler = (_event, data) => callback(data);
    ipcRenderer.on('preference-changed', handler);
    return () => ipcRenderer.removeListener('preference-changed', handler);
  },
});
