@echo off
REM ============================================================
REM  Yuncat Dev Dependencies Setup (修复 1.8 / FUSION I-003)
REM
REM  安装可选 Agent 后端依赖：
REM    - hermes-agent (pip) —— Hermes Agent 子进程模式
REM    - openclaw     (npm) —— OpenClaw Gateway 模式
REM
REM  幂等：已安装则跳过。未安装时才执行安装。
REM  产物：hermes.exe / openclaw 进入系统 PATH，适配器 fallback 路径命中。
REM
REM  用法：scripts\setup-dev-deps.bat
REM ============================================================
setlocal enabledelayedexpansion
title Yuncat Dev Deps Setup

echo ================================================================
echo             Yuncat Dev Dependencies Setup
echo ================================================================
echo.

REM ── 1. Python + pip 检查 ──
echo [1/4] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo   [FAIL] Python not found in PATH
    pause
    exit /b 1
)
echo   Python OK

REM ── 2. hermes-agent (pip) ──
echo.
echo [2/4] Checking hermes-agent...
where hermes >nul 2>&1
if not errorlevel 1 (
    echo   hermes already on PATH, skipping
) else (
    echo   installing hermes-agent via pip...
    python -m pip install hermes-agent -q
    if errorlevel 1 (
        echo   [WARN] pip install hermes-agent failed ^(package may not exist yet^)
        echo         HA subprocess mode will use direct HTTP fallback
    ) else (
        echo   hermes-agent installed
    )
)

REM ── 3. Node + npm 检查 ──
echo.
echo [3/4] Checking Node.js...
where npm >nul 2>&1
if errorlevel 1 (
    echo   [WARN] npm not found in PATH — skipping openclaw install
    echo         OC Gateway mode will be unavailable in dev
    goto :summary
)
echo   npm OK

REM ── 4. openclaw (npm) ──
echo.
echo [4/4] Checking openclaw...
where openclaw >nul 2>&1
if not errorlevel 1 (
    echo   openclaw already on PATH, skipping
) else (
    echo   installing openclaw via npm -g...
    call npm install -g openclaw
    if errorlevel 1 (
        echo   [WARN] npm install -g openclaw failed ^(package may not exist yet^)
        echo         OC Gateway mode will use direct HTTP fallback
    ) else (
        echo   openclaw installed
    )
)

:summary
echo.
echo ================================================================
echo   Setup complete.
echo.
echo   Notes:
echo   - If hermes/openclaw packages are not yet published, the
echo     adapters will fall back to direct HTTP mode (no subprocess).
echo   - Re-run this script after publishing to activate subprocess mode.
echo ================================================================
pause
endlocal
