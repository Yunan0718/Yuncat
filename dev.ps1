# Yuncat Dev — PowerShell 启动脚本
# 用法: powershell -ExecutionPolicy Bypass -File dev.ps1
$ErrorActionPreference = "Stop"
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ROOT

# ═══ 离线模式：禁止 HuggingFace 联网下载模型，避免国内无镜像时启动卡死 ═══
# 已缓存的 all-MiniLM-L6-v2 会直接本地加载；未缓存的 reranker(bge) 会快速跳过（best-effort）
$env:HF_HUB_OFFLINE = "1"
$env:TRANSFORMERS_OFFLINE = "1"

Write-Host "================================================" -ForegroundColor Cyan
Write-Host "            Yuncat Dev Launcher" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# [1/5] Python
Write-Host "[1/5] Checking Python..." -ForegroundColor Cyan
$PYTHON = Join-Path $ROOT "src-python\python-embed\python.exe"
if (-not (Test-Path $PYTHON)) {
    $PYTHON = "python"
    Write-Host "  embedded Python not found, using system: $PYTHON"
} else {
    Write-Host "  using embedded Python"
}

try { & $PYTHON --version 2>$null | Out-Null } catch {
    Write-Host "  [FAIL] Python not found at: $PYTHON" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

# [2/5] Python deps (check first, install only if missing)
Write-Host ""
Write-Host "[2/5] Checking Python deps..." -ForegroundColor Cyan
& $PYTHON -c "import aiohttp, aiosqlite, yaml, fastapi, cryptography, watchdog, pytest, tiktoken, winpty" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  installing missing packages..."
    & $PYTHON -m pip install aiohttp aiosqlite pyyaml fastapi uvicorn cryptography watchdog pytest tiktoken pywinpty -q
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [FAIL] pip install failed" -ForegroundColor Red
        Read-Host "Press Enter to exit"
        exit 1
    }
    Write-Host "  done"
} else {
    Write-Host "  all deps OK"
}

# [3/5] Tests + type check
Write-Host ""
Write-Host "[3/5] Running tests + type check..." -ForegroundColor Cyan

& $PYTHON -m pytest "$ROOT\src-python\tests" -x -q
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [FAIL] Tests failed" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

Set-Location "$ROOT\src-frontend"
if (-not (Test-Path "node_modules")) { npm install }
npx tsc --noEmit
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [FAIL] TypeScript errors" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

# [4/5] Vite
Write-Host ""
Write-Host "[4/5] Vite (127.0.0.1:1420) ..." -ForegroundColor Cyan

$alreadyRunning = netstat -ano 2>$null | Select-String "127.0.0.1:1420" | Select-String "LISTENING"
if ($alreadyRunning) {
    Write-Host "  already running (skipping)"
} else {
    $viteJob = Start-Process -FilePath "cmd" -ArgumentList "/c cd /d $ROOT\src-frontend && npx vite --port 1420 --strictPort --host 127.0.0.1" -PassThru -WindowStyle Minimized
    Write-Host "  waiting..."
    $ready = $false
    for ($i = 1; $i -le 30; $i++) {
        Start-Sleep -Seconds 2
        $check = netstat -ano 2>$null | Select-String "127.0.0.1:1420" | Select-String "LISTENING"
        if ($check) { Write-Host "  ready ($($i*2)s)"; $ready = $true; break }
        Write-Host -NoNewline "."
    }
    if (-not $ready) {
        Write-Host ""
        Write-Host "  [FAIL] Vite did not start within 60s" -ForegroundColor Red
        Read-Host "Press Enter to exit"
        exit 1
    }
}

# [5/5] Electron (Phase 47: 统一 .electron-dist 策略, 对齐 dev.bat)
Write-Host ""
Write-Host "[5/5] Launching Electron..." -ForegroundColor Cyan
# .electron-dist 位于 src-electron/ 下（与 dev.bat 的 `cd src-electron` 对齐）
Set-Location "$ROOT\src-electron"

if (-not (Test-Path ".electron-dist\electron.exe")) {
    Write-Host "  [FAIL] .electron-dist not found. Run: cd src-electron; npm install; npm run dev" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

# 复制 main.js/preload.js 到 .electron-dist (与 dev.bat 一致)
Copy-Item -Force "$ROOT\src-electron\main.js" ".electron-dist\resources\app\main.js" -ErrorAction SilentlyContinue
Copy-Item -Force "$ROOT\src-electron\preload.js" ".electron-dist\resources\app\preload.js" -ErrorAction SilentlyContinue
Copy-Item -Force "$ROOT\src-electron\crash-handler.js" ".electron-dist\resources\app\crash-handler.js" -ErrorAction SilentlyContinue

Set-Location ".electron-dist"
$electron = Start-Process -FilePath ".\electron.exe" -PassThru -Wait

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  Yuncat closed." -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Read-Host "Press Enter to exit"
