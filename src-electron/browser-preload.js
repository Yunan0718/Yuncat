// browser-preload.js —— 内置浏览器工具栏的桥（只暴露工具栏需要的三件事）
//
// 工具栏页面本身是普通 file:// 页面：没有 nodeIntegration，也只能通过这三个方法
// 与主进程对话。地址栏输入的内容永远当作「网址」处理，不会变成可执行代码。

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("yuncatBrowser", {
  state: () => ipcRenderer.invoke("browser:toolbar-state"),
  command: (cmd, payload) => ipcRenderer.invoke("browser:toolbar-command", { cmd, payload }),
  onState: (callback) => {
    const handler = (_event, state) => callback(state);
    ipcRenderer.on("browser:state", handler);
    return () => ipcRenderer.removeListener("browser:state", handler);
  },
});