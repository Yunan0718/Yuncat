/**
 * scripts/dev.js —— 开发启动 Electron（修好原来那条走不通的路径）
 *
 * 原 npm run dev 写死了 `node_modules/electron/dist/electron.exe`：
 *  - 有的机器上 Electron 装在别处（本项目用的是 .electron-dist），直接报「找不到文件」；
 *  - 就算能启动，如果 exe 同级有 `resources/app`，那份旧代码会盖过源码目录。
 *
 * 这里按顺序找二进制，先同步一次 resources/app，再启动，并把真实路径打出来。
 */
"use strict";

const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const { sync } = require("./sync-dev-app");

const ROOT = path.resolve(__dirname, "..");

function resolveElectron() {
  const candidates = [
    process.env.YUNCAT_ELECTRON_BIN,
    path.join(ROOT, "node_modules", "electron", "dist", "electron.exe"),
    path.join(ROOT, "node_modules", "electron", "dist", "electron"),
    path.join(ROOT, ".electron-dist", "electron.exe"),
    path.join(ROOT, ".electron-dist", "electron"),
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  throw new Error(
    "[dev] 找不到 Electron 可执行文件。请先 `npm install`，或用 YUNCAT_ELECTRON_BIN 指定路径。\n" +
      "已找过：\n  " + candidates.join("\n  "),
  );
}

const electron = resolveElectron();
console.log(`[dev] Electron: ${electron}`);
sync();

const child = spawn(electron, [ROOT], { stdio: "inherit", windowsHide: false, env: process.env });
child.on("exit", (code) => process.exit(code ?? 0));
