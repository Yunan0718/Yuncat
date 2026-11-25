/**
 * scripts/sync-dev-app.js —— 把主进程代码同步到 `.electron-dist/resources/app`
 *
 * 为什么需要它：Electron 的「默认 app」是 exe 同级的 `resources/app`。
 * 一旦这个目录存在，**它会盖过命令行里传入的应用目录** —— 于是你双击/启动了那个 exe，
 * 跑的却是目录里躺着的一份旧代码（本项目实测：那份快照停留在 8 月 18 日，
 * 里面没有多窗口、没有内置浏览器，所以「浏览器按钮点了没反应」）。
 *
 * 因此这份文件列表只有一个来源：electron-builder.yml 的 files 段。
 * 打包用什么，这里就同步什么——两边不可能再漂移。
 */
"use strict";

const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const BUILDER_YML = path.join(ROOT, "electron-builder.yml");
const ELECTRON_DIR = process.env.YUNCAT_ELECTRON_DIR || path.join(ROOT, ".electron-dist");
const TARGET = path.join(ELECTRON_DIR, "resources", "app");

function packagedFiles() {
  const lines = fs.readFileSync(BUILDER_YML, "utf-8").split(/\r?\n/);
  const start = lines.findIndex((line) => /^files:\s*$/.test(line));
  if (start < 0) throw new Error("electron-builder.yml 里找不到 files 段");
  const entries = [];
  for (let i = start + 1; i < lines.length; i++) {
    const line = lines[i];
    if (/^\S/.test(line)) break;
    const matched = line.match(/^\s+-\s+(\S+)\s*$/);
    if (matched) entries.push(matched[1]);
  }
  if (!entries.length) throw new Error("files 段没有解析到任何条目");
  return entries;
}

function sync() {
  if (!fs.existsSync(ELECTRON_DIR)) {
    console.log(`[sync-dev-app] 跳过：没有找到 ${ELECTRON_DIR}`);
    return 0;
  }
  fs.mkdirSync(TARGET, { recursive: true });
  let copied = 0;
  const missing = [];
  for (const name of packagedFiles()) {
    const from = path.join(ROOT, name);
    if (!fs.existsSync(from)) {
      missing.push(name);
      continue;
    }
    const to = path.join(TARGET, name);
    const before = fs.existsSync(to) ? fs.readFileSync(to) : null;
    const after = fs.readFileSync(from);
    if (before && before.equals(after)) continue;
    fs.copyFileSync(from, to);
    copied += 1;
    console.log(`[sync-dev-app] 更新 ${name}`);
  }
  if (missing.length) {
    // 清单里列了但源码目录没有：那是清单写错了，必须显式说出来
    console.warn(`[sync-dev-app] electron-builder.yml 里列了不存在的文件：${missing.join(", ")}`);
  }
  console.log(`[sync-dev-app] 完成：${TARGET}（更新 ${copied} 个文件）`);
  return copied;
}

if (require.main === module) sync();

module.exports = { sync, packagedFiles, TARGET };
