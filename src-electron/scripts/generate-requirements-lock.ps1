# Phase 48 T1-7: 生成 hash-pinned requirements.lock 供 --require-hashes 安装
#
# 主流做法（pip-tools / pip --require-hashes）：
#   1. pip-compile 从 pyproject.toml 解析全量依赖（含传递依赖）到 requirements.lock
#   2. --generate-hashes 为每个包写入所有兼容 wheel/sdist 的 sha256 哈希
#   3. pip install --require-hashes -r requirements.lock 安装时强制哈希校验
#      → 任何被篡改的 PyPI 包（供应链攻击）因哈希不匹配而被拒绝
#
# 用法（在项目根目录，使用目标 Python 3.12）：
#   pwsh scripts/generate-requirements-lock.ps1
#
# 注意：
#   - 必须用目标嵌入式 Python 3.12 运行（哈希需覆盖 cp312 wheel）
#   - pip-compile --generate-hashes 会为所有兼容平台/版本生成哈希，
#     产出的 requirements.lock 跨平台可用
#   - 生成后应提交到版本控制（保证全员/CI 用同一组验证过的哈希）
#   - 安装命令迁移：dev.bat / dev.ps1 / prebuild.js 中的 pip install
#     改为 `pip install --require-hashes -r requirements.lock`
#     （hermes-agent 等单独安装的包也需纳入 lockfile 或单独哈希校验）

$ErrorActionPreference = "Stop"
$ROOT = Resolve-Path "$PSScriptRoot\..\.."
$PYPROJECT = Join-Path $ROOT "pyproject.toml"
$LOCKFILE = Join-Path $ROOT "requirements.lock"

if (-not (Test-Path $PYPROJECT)) { throw "pyproject.toml not found at $PYPROJECT" }

$python = Join-Path $ROOT "src-python\python-embed\python.exe"
if (-not (Test-Path $python)) {
  Write-Warning "嵌入式 Python 未找到，回退到系统 Python（注意：哈希需覆盖目标 cp312 wheel）"
  $python = "python"
}

# 确保 pip-tools 可用
& $python -m pip install --quiet --upgrade pip-tools 2>$null
if ($LASTEXITCODE -ne 0) { throw "pip-tools 安装失败——请检查网络/pip" }

Write-Host "[lock] 生成 requirements.lock (with hashes)..." -ForegroundColor Cyan
& $python -m piptools compile `
  --generate-hashes `
  --output-file="$LOCKFILE" `
  --resolver=backtracking `
  "$PYPROJECT"

if ($LASTEXITCODE -eq 0) {
  Write-Host "[lock] 完成: $LOCKFILE" -ForegroundColor Green
  Write-Host "[lock] 安装时使用: pip install --require-hashes -r requirements.lock" -ForegroundColor Cyan
} else {
  throw "pip-compile 失败"
}
