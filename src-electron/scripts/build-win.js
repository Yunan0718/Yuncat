/**
 * build-win.js — 一站式 Windows 打包编排
 *
 * 解决痛点：旧 build:win 只运行 prebuild + electron-builder，不会自动重新构建
 * src-frontend，导致前端代码修改后打包不生效。
 *
 * 流程：
 *   1. cd src-frontend && npm run build
 *   2. cd src-electron && npm run prebuild  （生成 2 个 7z 组件，带增量 hash 比对）
 *   3. cd src-electron && electron-builder --win
 *
 * 新方案（对标 Docker Desktop / Postman）：
 *   运行时拆分为 2 个 7z 预压缩组件（python-core / python-ml），chromadb 已由 sqlite-vec 替代，
 *   通过 electron-builder extraResources 打包，由 NSIS installer.nsh 在安装时解压。
 *   prebuild.js 基于内容 hash 增量构建——未变化的组件跳过 7z 重建。
 *
 * 使用 Node 脚本而非 shell && 链，避免 Windows cmd 不支持 && 的问题，
 * 同时保证任一阶段失败即退出并返回非零状态码。
 */
const { execSync } = require('child_process');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
const FRONTEND_DIR = path.join(ROOT, 'src-frontend');
const ELECTRON_DIR = path.join(ROOT, 'src-electron');

const STEP_TIMEOUT_MS = 30 * 60 * 1000; // 30 分钟 —— 给前端 build / electron-builder 足够时间

function run(label, cmd, cwd, env) {
  console.log(`\n[build-win] ${label}: ${cmd}`);
  execSync(cmd, {
    cwd,
    stdio: 'inherit',
    windowsHide: true,
    timeout: STEP_TIMEOUT_MS,
    env: { ...process.env, ...env },
  });
}

function main() {
  // 1. 构建前端生产产物（写入 src-frontend/dist）
  run('frontend build', 'npm run build', FRONTEND_DIR);

  // 2. 预处理运行时（修复 _pth + 生成运行时 7z 组件）
  run('prebuild', 'npm run prebuild', ELECTRON_DIR);

  // 3. Electron NSIS 打包（extraResources 打包运行时 7z + 7z.exe + 源码；installer.nsh 安装时解压）
  // --publish never: electron-builder 检测到 CI 环境会尝试发布到 GitHub，本地构建无 GH_TOKEN 会报错退出
  run('electron-builder', 'npx electron-builder --win --publish never', ELECTRON_DIR);

  console.log('\n[build-win] 打包完成');
}

main();
