@echo off
title Yuncat Dev
set "ROOT=%~dp0"
cd /d "%ROOT%"

rem ═══ 离线模式：禁止 HuggingFace 联网下载模型，避免国内无镜像时启动卡死 ═══
rem 已缓存的 all-MiniLM-L6-v2 会直接本地加载；未缓存的 reranker(bge) 会快速跳过（best-effort）
set HF_HUB_OFFLINE=1
set TRANSFORMERS_OFFLINE=1

echo ================================================
echo             Yuncat Dev Launcher
echo ================================================
echo.

echo [1/5] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo   [FAIL] Python not found in PATH
    echo.
    pause
    exit /b 1
)
echo   Python OK

echo.
echo [2/5] Python deps...
python -c "import aiohttp, aiosqlite, yaml, fastapi, cryptography, watchdog, pytest, tiktoken, winpty" >nul 2>&1
if errorlevel 1 (
    echo   installing missing packages...
    python -m pip install aiohttp aiosqlite pyyaml fastapi uvicorn cryptography watchdog pytest tiktoken pywinpty -q
    if errorlevel 1 (
        echo   [FAIL] pip install failed
        pause
        exit /b 1
    )
    echo   done
) else (
    echo   all deps OK
)
echo   ^(可选^) Agent 子进程模式依赖: scripts\setup-dev-deps.bat

echo.
echo [3/5] Tests + type check...
python -m pytest src-python\tests -x -q
if errorlevel 1 (
    echo   [FAIL] Tests failed
    pause
    exit /b 1
)

cd /d src-frontend
if not exist node_modules\ (
    echo   installing frontend deps...
    call npm install
    if errorlevel 1 (
        echo   [FAIL] npm install failed
        pause
        exit /b 1
    )
)
call npx tsc --noEmit
if errorlevel 1 (
    echo   [FAIL] TypeScript errors
    pause
    exit /b 1
)
cd ..

echo.
echo [4/5] Vite ^(127.0.0.1:1420^) ...
netstat -ano 2>nul | findstr "127.0.0.1:1420" | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo   already running
    goto vite_done
)

start "" /min cmd /c "cd /d %ROOT%src-frontend && npx vite --port 1420 --strictPort --host 127.0.0.1"
echo   waiting...
for /l %%i in (1,1,30) do (
    netstat -ano 2>nul | findstr "127.0.0.1:1420" | findstr "LISTENING" >nul
    if not errorlevel 1 (
        echo   ready
        goto vite_done
    )
    ping -n 2 127.0.0.1 >nul
)
echo   [FAIL] Vite did not start within 60s
pause
exit /b 1

:vite_done

:: Embedded Python health check
echo.
echo [5/5] Embedded Python check + Launch Electron...
if exist "src-python\python-embed\python.exe" (
    src-python\python-embed\python.exe -c "import aiohttp, aiosqlite, yaml, fastapi, cryptography, watchdog, tiktoken, winpty; print('e: ok')" >nul 2>&1
    if errorlevel 1 (
        echo   [WARN] Embedded Python missing deps - rebuild: cd src-electron ^& npm run prebuild
    ) else (
        echo   embedded Python OK
    )
)

set YUNCAT_DEV=1
cd src-electron

if not exist ".electron-dist\electron.exe" (
    echo   [FAIL] Electron .electron-dist not found. Run: cd src-electron ^& npm install ^& npm run dev
    pause
    exit /b 1
)

:: 复制 main.js / preload.js 到 Electron 的资源目录（_dirname = resources/app/）
if not exist ".electron-dist\resources\app" mkdir ".electron-dist\resources\app"
copy /Y "main.js" ".electron-dist\resources\app\main.js" >nul
copy /Y "preload.js" ".electron-dist\resources\app\preload.js" >nul
copy /Y "crash-handler.js" ".electron-dist\resources\app\crash-handler.js" >nul

echo   starting Electron...
cd .electron-dist
.\electron.exe

echo.
echo ================================================
echo   Yuncat closed.
echo ================================================
pause
