/**
 * zipimport-python-embed.js — 保留占位文件
 *
 * 历史用途：尝试把 site-packages 打包成 zip 以减少 Defender 扫描文件数。
 * 已回退到简单方案：直接分发 5 个 7z 组件 + 安装时并行解压。
 * 保留此文件及其导出，避免引用它的脚本因模块缺失而崩溃。
 */
function prepareSitePackagesZip() {
  // 简单方案：不再打包 site-packages.zip
  // 5 个 7z 组件已经包含完整运行时，安装时解压即可
}

module.exports = { prepareSitePackagesZip };
