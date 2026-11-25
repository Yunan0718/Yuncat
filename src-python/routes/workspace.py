"""routes/workspace.py —— 工作区文件管理 + 终端 WS + Lint + LLM 补全 + 文件搜索"""

import asyncio
import hmac
import logging
import os
import time as _time

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from config import (
    TOKEN, WORKSPACE_GIT_TIMEOUT, WORKSPACE_LINT_TIMEOUT, WORKSPACE_TSC_TIMEOUT,
    WORKSPACE_FILE_SEARCH_DEFAULT_LIMIT, WORKSPACE_FILE_SEARCH_MAX_LIMIT,
    WORKSPACE_FILENAME_SEARCH_LIMIT, WORKSPACE_MAX_READ_SIZE, WORKSPACE_BINARY_SNIFF_BYTES,
    WORKSPACE_MAX_UPLOAD_SIZE,
    WORKSPACE_GIT_STATUS_PREVIEW_CHARS,
    WORKSPACE_CODE_PREVIEW_CHARS,
)
# M18 修复：复用 ws.py 的 Origin 白名单逻辑——避免重复造轮子，保持两端点安全策略一致
from routes.ws import _is_origin_allowed
from security.sandbox import get_workspace, validate_path
from services.lint_service import lint_python_file_sync  # E3: 共享 ruff 解析逻辑

_log = logging.getLogger("yuncat.workspace")

router = APIRouter(prefix="/api/v1", tags=["workspace"])


# ---- 文件 CRUD ----

@router.post("/workspace/files/root")
async def set_workspace_root(payload: dict, request: Request = None):
    """设置工作区根目录——直接保存到 settings，下次启动或 reload 时生效。

    与 Cursor 一致：用户从原生文件夹选择器选取目录后，该目录成为工作区。
    不创建符号链接——直接存 DB，运行时通过 YUNCAT_WORKSPACE 环境变量使用。
    """
    path = payload.get("path", "")
    if not path or not os.path.isdir(path):
        raise HTTPException(400, "Invalid workspace path")

    # 保存到 settings
    try:
        settings_svc = request.app.state.settings_service
        await settings_svc.save("workspace", path)
        # 即时生效——更新运行时环境变量
        os.environ["YUNCAT_WORKSPACE"] = path
        _log.info("Workspace set to %s (instantly)", path)
    except Exception as e:
        _log.warning("Failed to save workspace setting: %s", e)
        # 即使保存失败，也尝试更新 env
        os.environ["YUNCAT_WORKSPACE"] = path

    return {"ok": True, "path": path, "instant": True}


@router.get("/workspace/files")
async def list_workspace_files(dir: str = "", request: Request = None):
    ws = get_workspace()
    target = str(ws) if not dir else os.path.join(str(ws), dir)
    validate_path(target, ws)
    if not os.path.isdir(target):
        return []
    results = []
    for entry in sorted(os.scandir(target), key=lambda e: (not e.is_dir(), e.name.lower())):
        if entry.name.startswith("."):
            continue
        s = entry.stat()
        results.append({
            "name": entry.name, "path": entry.path, "is_dir": entry.is_dir(),
            "size": s.st_size if not entry.is_dir() else 0,
            "modified": int(s.st_mtime),
        })
    return results


@router.get("/workspace/files/read")
async def read_workspace_file(path: str, request: Request = None):
    ws = get_workspace()
    fp = os.path.join(str(ws), path) if not os.path.isabs(path) else path
    validate_path(fp, ws)
    if not os.path.isfile(fp):
        raise HTTPException(404, "File not found")
    s = os.stat(fp)
    # Phase 48 T1-8: 大小限制防 OOM（50MB 上限）
    if s.st_size > WORKSPACE_MAX_READ_SIZE:
        raise HTTPException(413, f"File too large ({s.st_size} bytes > {WORKSPACE_MAX_READ_SIZE} limit)")
    # 2026-09-15: 二进制嗅探——原先一律按 utf-8(errors="replace") 读成乱码，
    # 用户一旦保存就会把替换字符写回磁盘、把原文件毁掉。命中 NUL 即只读返回。
    with open(fp, "rb") as fb:
        binary = b"\x00" in fb.read(WORKSPACE_BINARY_SNIFF_BYTES)
    if binary:
        _log.info("workspace read: binary file opened read-only (%s bytes)", s.st_size)
        return {"content": "", "size": s.st_size, "modified": int(s.st_mtime), "binary": True}
    with open(fp, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    return {"content": content, "size": s.st_size, "modified": int(s.st_mtime), "binary": False}


@router.get("/workspace/files/blame")
async def git_blame(path: str, request: Request = None):
    """Phase 43 WS.4: Git blame — 获取文件每行的作者和时间。

    调用系统 git blame — 如果不在 git 仓库中，返回空列表。
    """
    import subprocess

    ws = get_workspace()
    fp = os.path.join(str(ws), path) if not os.path.isabs(path) else path
    validate_path(fp, ws)
    if not os.path.isfile(fp):
        raise HTTPException(404, "File not found")

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["git", "blame", "--line-porcelain", fp],
            capture_output=True, text=True, timeout=WORKSPACE_GIT_TIMEOUT,
            cwd=str(ws),
        )
        if result.returncode != 0:
            return {"blame": []}

        blame: list[dict] = []
        for line in result.stdout.splitlines():
            if line.startswith("author ") and not line.startswith("author-mail"):
                blame.append({"author": line[7:].strip()})
            elif line.startswith("author-time "):
                import time as _time
                ts = int(line[12:].strip())
                if blame:
                    blame[-1]["time"] = _time.strftime("%Y-%m-%d", _time.localtime(ts))
        return {"blame": blame}
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"blame": []}


@router.get("/workspace/files/git-status")
async def workspace_git_status(request: Request = None):
    """Phase 43 WS.4: 获取工作区的 git 文件状态（M/A/D 装饰用）"""
    import subprocess

    ws = get_workspace()
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=WORKSPACE_GIT_TIMEOUT,
            cwd=str(ws),
        )
        if result.returncode != 0:
            return {"files": {}}
        files: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if len(line) >= 3:
                status = line[:2].strip()
                fp = line[3:].strip()
                files[fp] = status or "M"
        return {"files": files}
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"files": {}}


# 可内联预览的媒体类型 → Content-Type。
# 文本类（代码/markdown/html 源码）仍走 /files/read；这里只放浏览器**原生能渲染**的格式。
# 办公文档（docx/xlsx/pptx）与压缩包等不在其中——主流的 VS Code/Cursor 同样需要扩展才能看，
# 这里如实返回 415，由界面提示"用系统默认应用打开"。
PREVIEW_MEDIA_TYPES: dict[str, str] = {
    # 图片
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
    ".avif": "image/avif",
    # 文档
    ".pdf": "application/pdf",
    # Office（前端解析后只读展示：docx→HTML、xlsx→表格、pptx→幻灯片）
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    # 音频
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
    # 视频
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".ogv": "video/ogg",
}


@router.get("/workspace/files/raw")
async def read_workspace_file_raw(path: str, request: Request = None):
    """按原始字节返回可预览的媒体文件（图片 / PDF / 音频 / 视频）。

    为什么单独开一个端点：`/files/read` 返回 JSON 文本（带二进制嗅探），媒体经它读出来
    是乱码；而 `<img src>` / `<video src>` 无法携带 Authorization 头，所以前端用带鉴权的
    fetch 取字节再转 blob URL——token 因此不会出现在 URL 与访问日志里。

    只放行浏览器原生能渲染的格式（见 PREVIEW_MEDIA_TYPES）；办公文档、压缩包等返回 415，
    由界面提示改用系统默认应用打开。上限沿用 WORKSPACE_MAX_READ_SIZE（大文件不适合整块转 blob）。
    """
    ws = get_workspace()
    fp = path if os.path.isabs(path) else os.path.join(str(ws), path)
    try:
        validate_path(fp, ws)
    except (PermissionError, ValueError) as e:
        raise HTTPException(403, "Path outside workspace") from e
    if not os.path.isfile(fp):
        raise HTTPException(404, "File not found")

    media_type = PREVIEW_MEDIA_TYPES.get(os.path.splitext(fp)[1].lower())
    if not media_type:
        raise HTTPException(415, "This file type cannot be previewed inline")
    size = os.path.getsize(fp)
    if size > WORKSPACE_MAX_READ_SIZE:
        raise HTTPException(413, f"File too large ({size} bytes > {WORKSPACE_MAX_READ_SIZE} limit)")
    return FileResponse(fp, media_type=media_type)


def _resolve_upload_dir(dir_arg: str) -> str:
    """把请求里的目录解析成工作区内的绝对路径；越界是客户端错误，回 403（而非 500）。"""
    ws = get_workspace()
    target = dir_arg if os.path.isabs(dir_arg) else os.path.join(str(ws), dir_arg)
    try:
        validate_path(target, ws)
    except (PermissionError, ValueError) as e:
        raise HTTPException(403, "Target directory is outside the workspace") from e
    return target


async def _read_upload_body(request: Request) -> bytes:
    """读取上传正文：先看 content-length，再核实实际字节数，超限/空体都在这里拒绝。"""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > WORKSPACE_MAX_UPLOAD_SIZE:
        raise HTTPException(413, f"File too large ({declared} bytes > {WORKSPACE_MAX_UPLOAD_SIZE} limit)")
    body = await request.body()
    if not body:
        raise HTTPException(400, "Empty upload body")
    if len(body) > WORKSPACE_MAX_UPLOAD_SIZE:
        raise HTTPException(413, f"File too large ({len(body)} bytes > {WORKSPACE_MAX_UPLOAD_SIZE} limit)")
    return body


def _unique_target(dir_path: str, name: str) -> str:
    """重名不覆盖——追加 (1)/(2)…，与浏览器「另存为」一致，避免静默顶掉已有文件。"""
    stem, ext = os.path.splitext(name)
    candidate = os.path.join(dir_path, name)
    n = 1
    while os.path.exists(candidate):
        candidate = os.path.join(dir_path, f"{stem} ({n}){ext}")
        n += 1
    return candidate


@router.post("/workspace/files/upload")
async def upload_workspace_file(request: Request, dir: str = "shared", filename: str = ""):
    """上传**一个**文件到工作区目录（FileTree 的「上传文件」与拖拽上传）。

    2026-09-15 修复：前端一直在调这个端点，而后端**从未实现**——上传永远 404，
    前端只能弹一句「上传失败」，用户分不清是路径问题还是网络问题。

    实现选择：走**原始字节 body**（`application/octet-stream`）而不是 multipart。
    原因：FastAPI 的 Form/File 需要 `python-multipart`，而它不在本项目的依赖清单里
    （装上会牵动 requirements.lock 与打包运行时）——为一个上传功能新增运行时依赖
    不划算。一个文件一个请求，前端用 Blob 直接发，服务端按流写盘。

    安全与一致性：目录经 validate_path 限定在工作区内；文件名只取 basename 并拒绝
    空名/`.`/`..`；体积上限引用 config.WORKSPACE_MAX_UPLOAD_SIZE（与全局请求体上限同源）；
    重名不覆盖（追加 (1)/(2)…）；先写 .part 再 os.replace 原子落盘。
    """
    target_dir = _resolve_upload_dir(dir)

    name = os.path.basename(filename or "")
    if not name or name in (".", ".."):
        raise HTTPException(400, "Missing or invalid 'filename'")

    body = await _read_upload_body(request)
    os.makedirs(target_dir, exist_ok=True)
    dest = _unique_target(target_dir, name)
    tmp = dest + ".part"
    try:
        with open(tmp, "wb") as out:
            out.write(body)
        os.replace(tmp, dest)  # 原子落盘：半成品不会以正式名字出现
    except Exception as e:
        # 有明确回退：清理半成品并如实报错，用户可重试
        _log.warning("upload failed for %s: %s", name, e)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise HTTPException(500, f"Failed to write upload: {e}") from e

    saved_name = os.path.basename(dest)
    return {"ok": True, "saved": {"name": saved_name, "path": dest, "size": len(body)}}


@router.post("/workspace/files/save")
async def save_workspace_file(payload: dict, request: Request):
    import shutil

    path = payload.get("path")
    content = payload.get("content")
    if not path:
        raise HTTPException(400, "Missing 'path' in payload")
    if content is None:
        raise HTTPException(400, "Missing 'content' in payload")
    ws = get_workspace()
    fp = os.path.join(str(ws), path) if not os.path.isabs(path) else path
    validate_path(fp, ws)
    # 2026-09-15: 乐观并发——前端带上「打开该文件时的 mtime」。文件在编辑器之外
    # 被改过（Agent 写文件 / 外部编辑器 / 其它标签）就回 409，由前端让用户选择
    # 覆盖还是重载，而不是静默把手写内容盖掉别人刚写的改动。
    expected_mtime = payload.get("expected_mtime")
    if expected_mtime is not None and os.path.isfile(fp):
        current_mtime = int(os.stat(fp).st_mtime)
        if int(expected_mtime) != current_mtime:
            raise HTTPException(
                409,
                f"File changed on disk since it was opened (disk mtime {current_mtime}, expected {int(expected_mtime)})",
            )
    bak = None
    if os.path.isfile(fp):
        d = os.path.join(str(ws), ".backup")
        os.makedirs(d, exist_ok=True)
        bak = os.path.join(d, os.path.basename(fp) + "." + str(int(_time.time())))
        shutil.copy2(fp, bak)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    # Phase 48 T1-9: 原子写（tmp→fsync→os.replace）防中途崩溃损坏文件
    # CodeWhale 借鉴 #15: 提取为 text_utils.write_atomic 复用
    from text_utils import write_atomic
    write_atomic(fp, content)
    return {"status": "ok", "backup_path": bak, "modified": int(os.stat(fp).st_mtime)}


@router.delete("/workspace/files")
async def delete_workspace_file(path: str, request: Request = None):
    import shutil

    ws = get_workspace()
    fp = os.path.join(str(ws), path) if not os.path.isabs(path) else path
    validate_path(fp, ws)
    if not os.path.isfile(fp):
        raise HTTPException(404, "File not found")
    d = os.path.join(str(ws), ".backup")
    os.makedirs(d, exist_ok=True)
    bak = os.path.join(d, os.path.basename(fp) + "." + str(int(_time.time())))
    shutil.copy2(fp, bak)
    os.unlink(fp)
    return {"status": "ok", "backup_path": bak}


@router.delete("/workspace/files/batch")
async def batch_delete_workspace_files(payload: dict, request: Request = None):
    """批量删除工作区文件——前端 FileTree 多选删除"""
    import shutil

    paths: list[str] = payload.get("paths", [])
    if not paths:
        raise HTTPException(400, "Missing 'paths' in payload")

    ws = get_workspace()
    results = []
    for path in paths:
        fp = os.path.join(str(ws), path) if not os.path.isabs(path) else path
        try:
            validate_path(fp, ws)
            if os.path.isfile(fp):
                d = os.path.join(str(ws), ".backup")
                os.makedirs(d, exist_ok=True)
                bak = os.path.join(d, os.path.basename(fp) + "." + str(int(_time.time())))
                shutil.copy2(fp, bak)
                os.unlink(fp)
                results.append({"path": path, "status": "ok", "backup_path": bak})
            elif os.path.isdir(fp):
                shutil.rmtree(fp)
                results.append({"path": path, "status": "ok"})
            else:
                results.append({"path": path, "status": "not_found"})
        except Exception as e:
            results.append({"path": path, "status": "error", "error": str(e)})
    return {"results": results}


@router.post("/workspace/files/rename")
async def rename_workspace_file(payload: dict, request: Request = None):
    old = payload.get("old_path")
    new = payload.get("new_path")
    if not old or not new:
        raise HTTPException(400, "Missing 'old_path' or 'new_path'")
    ws = get_workspace()
    old_fp = os.path.join(str(ws), old) if not os.path.isabs(old) else old
    new_fp = os.path.join(str(ws), new) if not os.path.isabs(new) else new
    validate_path(old_fp, ws)
    validate_path(new_fp, ws)
    if not os.path.exists(old_fp):
        raise HTTPException(404, "File not found")
    os.rename(old_fp, new_fp)
    return {"status": "ok", "new_path": new}


@router.post("/workspace/files/mkdir")
async def mkdir_workspace(payload: dict, request: Request = None):
    dir_path = payload.get("path", "")
    if not dir_path:
        raise HTTPException(400, "Missing 'path'")
    ws = get_workspace()
    fp = os.path.join(str(ws), dir_path) if not os.path.isabs(dir_path) else dir_path
    validate_path(fp, ws)
    os.makedirs(fp, exist_ok=True)
    return {"status": "ok", "path": dir_path}


# ---- 跨文件搜索 ----

@router.get("/workspace/files/search")
async def search_workspace_files(
    q: str, dir: str = "shared", limit: int = WORKSPACE_FILE_SEARCH_DEFAULT_LIMIT, request: Request = None,
):
    """跨文件内容搜索。Phase 49-I：limit 默认 100，最大 200，由后端控制。"""
    ws = get_workspace()
    target = os.path.join(str(ws), dir)
    validate_path(target, ws)
    if not os.path.isdir(target) or not q:
        return {"results": [], "error": "Invalid directory or query" if not q else ""}

    results = []
    q_lower = q.lower()
    max_results = min(limit, WORKSPACE_FILE_SEARCH_MAX_LIMIT)
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in files:
            if fname.startswith("."):
                continue
            fp = os.path.join(root, fname)
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if q_lower in line.lower():
                            rel = os.path.relpath(fp, str(ws))
                            results.append({
                                "file": rel, "line": i,
                                "text": line.strip()[:WORKSPACE_GIT_STATUS_PREVIEW_CHARS],
                            })
                            if len(results) >= max_results:
                                return {"results": results}
            except Exception:
                continue
    return {"results": results}


@router.get("/workspace/files/search-by-name")
async def search_files_by_name(q: str, limit: int = WORKSPACE_FILENAME_SEARCH_LIMIT, request: Request = None):
    """按文件名搜索。Phase 49-I：自动补全场景默认 5，由后端控制。"""
    ws = get_workspace()
    results: list[dict] = []
    q_lower = q.lower()
    ws_str = str(ws)
    for root, dirs, files in os.walk(ws_str):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in files:
            if fname.startswith("."):
                continue
            if q_lower in fname.lower():
                rel = os.path.relpath(os.path.join(root, fname), ws_str)
                results.append({"name": fname, "path": rel})
                if len(results) >= limit:
                    return results
    return results


# ---- Lint + LLM 补全 ----

@router.post("/lint")
async def lint_file(payload: dict, request: Request = None):
    import subprocess

    fp = payload.get("path", "")
    language = payload.get("language", "python")
    if not fp:
        raise HTTPException(400, "Missing 'path'")
    ws = get_workspace()
    full = os.path.join(str(ws), fp) if not os.path.isabs(fp) else fp
    validate_path(full, ws)
    if not os.path.isfile(full):
        return {"results": [], "error": "File not found"}

    results = []
    try:
        if language == "python":
            # E3: 复用 lint_service 共享逻辑（hook + 路由单源）
            results = lint_python_file_sync(full, cwd=str(ws))
        elif language in ("javascript", "typescript", "typescriptreact", "javascriptreact"):
            tsc_bin = os.path.join(
                os.path.dirname(__file__), "..", "..", "src-frontend",
                "node_modules", ".bin", "tsc",
            )
            if not os.path.exists(tsc_bin + (".cmd" if os.name == "nt" else "")):
                tsc_bin = "npx tsc"
            proc = await asyncio.to_thread(
                subprocess.run,
                [tsc_bin, "--noEmit", "--pretty", "false"],
                capture_output=True, text=True, timeout=WORKSPACE_TSC_TIMEOUT,
                cwd=os.path.join(os.path.dirname(__file__), "..", "..", "src-frontend"),
            )
            for line in proc.stdout.split("\n") + proc.stderr.split("\n"):
                parts = line.strip().split(":", 3)
                if len(parts) >= 3 and parts[1].isdigit():
                    results.append({
                        "line": int(parts[1]),
                        "col": int(parts[2]) if parts[2].isdigit() else 1,
                        "message": parts[3].strip() if len(parts) > 3 else parts[2].strip(),
                        "severity": "error",
                    })
    except FileNotFoundError:
        return {"results": [], "error": f"Linter not found for {language}"}
    except Exception as e:
        return {"results": [], "error": str(e)}
    return {"results": results}


# ---- 嵌入式终端 WebSocket ----

# 2026-09-15 修复：这个端点的实际路径曾是 /api/v1/ws/terminal（router 带 /api/v1 前缀），
# 而前端 TerminalPanel 连的是 ws://127.0.0.1:<port>/ws/terminal —— 路径对不上，
# **集成终端从未真正连上过**（握手被拒，界面上终端一直是空的）。
# 与 /ws 保持一致：终端 WS 不走 REST 前缀，由 main.py 直接挂到 app 上。


def _ws_token_ok(msg_token: str) -> bool:
    """终端 WS 的 token 校验——与主 /ws 同口径。

    Batch 7.6 起主 /ws 同时接受静态 TOKEN 与主进程兑换的 access token；
    终端此前只比静态 TOKEN，于是前端拿到 access token 后终端恒被 1008 拒
    （2026-09-22 实测日志：Terminal WS auth rejected have_token=True from=file://）。
    两处口径必须一致，否则内置终端永远连不上。

    用 hmac.compare_digest 做恒定时间比较，避免时序攻击。
    """
    if not msg_token:
        return False
    if hmac.compare_digest(msg_token, TOKEN):
        return True
    from services.auth_service import validate_access_token
    return validate_access_token(msg_token)


def _find_reusable_session(tm, ws_dir: str):
    """按 cwd 找可复用的存活会话；没有就返回 None，由调用方新建"""
    for session in tm._sessions.values():
        if session.cwd == ws_dir and session.alive:
            return session
    return None


async def _send_terminal_mode(ws, session) -> None:
    """告诉前端这是真终端还是降级管道。

    PTY 不可用时终端没有提示符也没有回显，用户只会觉得终端坏了，所以下发一次模式，
    让界面能把原因说清楚。best-effort：发不出去不影响会话本身，失败只记 debug。
    """
    from services.terminal_manager import pty_backend_name

    try:
        await ws.send_json({
            "type": "terminal_mode",
            "session_id": session.session_id,
            "backend": pty_backend_name(),
            "pty": bool(session.is_pty),
        })
    except Exception:
        _log.debug("下发 terminal_mode 失败", exc_info=True)


async def ws_terminal(ws: WebSocket):
    import asyncio

    # M18 修复：accept 前校验 Origin——与 /ws 端点保持一致，防恶意网页接管终端
    origin = ws.headers.get("origin", "")
    if not _is_origin_allowed(origin):
        _log.warning("Terminal WS rejected origin=%s", origin)
        try:
            await ws.close(code=1008)
        except RuntimeError:
            pass
        return

    await ws.accept()
    try:
        msg = await ws.receive_json()
        # M18 修复：消息大小限制（与 /ws 端点对齐，防 DoS）
        if len(str(msg)) > 1_048_576:
            await ws.close(code=1009, reason="Message too large")
            return
        msg_token = msg.get("token", "")
        token_match = _ws_token_ok(msg_token)
        if msg.get("type") != "auth" or not token_match:
            _log.warning("Terminal WS auth rejected: type=%r have_token=%s from=%s",
                         msg.get("type"), bool(msg_token), origin or "?")
            await ws.close(code=1008)
            return
    except Exception:
        _log.warning("Terminal WS 认证流程异常", exc_info=True)
        await ws.close(code=1008)
        return

    # P2 修复: auth 成功后显式回送 auth_ok —— 与 /ws 端点 (ws.py:138) 行为对齐。
    # 原 /ws/terminal auth 通过后不回任何消息，前端 TerminalPanel.tsx:232 靠"任何消息"
    # 标记 wsAuthRef.current=true；shell 无输出时前端永远卡在"未认证"且不会重连。
    try:
        await ws.send_json({"type": "auth_ok"})
    except Exception:
        _log.warning("Terminal WS 下发 auth_ok 失败", exc_info=True)
        await ws.close(code=1011)
        return

    from services.terminal_manager import get_terminal_manager
    tm = get_terminal_manager()
    ws_dir = os.path.join(get_workspace(), "shared")
    os.makedirs(ws_dir, exist_ok=True)

    # 尝试复用已有会话（按 cwd 匹配）
    session = _find_reusable_session(tm, ws_dir)

    if session:
        _log.info("Terminal: reusing session %s", session.session_id)
    else:
        session = tm.create_session(ws_dir)
        await tm.spawn_shell(session)
        _log.info("Terminal: created session %s (pty=%s)", session.session_id, session.is_pty)

    await _send_terminal_mode(ws, session)

    session.ws_connections.append(ws)
    read_task = asyncio.create_task(tm.read_stdout(session, ws))

    async def read_stdin():
        try:
            while True:
                msg = await ws.receive_json()
                if msg.get("type") == "input":
                    # Phase 21: 人类终端不走 is_safe_command——完全自由
                    data = msg.get("data", "")
                    if not data.endswith("\n"):
                        data += "\n"
                    await tm.write_stdin(session, data)
                elif msg.get("type") == "resize":
                    cols = msg.get("cols", 120)
                    rows = msg.get("rows", 40)
                    await tm.resize(session, cols, rows)
                elif msg.get("type") == "new_tab":
                    new_s = tm.create_session(ws_dir)
                    await tm.spawn_shell(new_s)
                    new_s.ws_connections.append(ws)
                    await ws.send_json({"type": "tab_created", "session_id": new_s.session_id})
                elif msg.get("type") == "switch_tab":
                    target_sid = msg.get("session_id")
                    if target_sid in tm._sessions:
                        session.ws_connections.remove(ws)
                        new_session = tm._sessions[target_sid]
                        new_session.ws_connections.append(ws)
                elif msg.get("type") == "close_tab":
                    # Phase 66: 前端关闭 tab 时通知后端清理 session（修复 Phase 47 遗留断点）
                    target_sid = msg.get("session_id")
                    if target_sid and target_sid in tm._sessions:
                        await tm.kill_session(target_sid)
        except Exception:
            _log.debug("处理 WS 消息失败", exc_info=True)

    stdin_task = asyncio.create_task(read_stdin())
    try:
        await asyncio.gather(read_task, stdin_task, return_exceptions=True)
    except Exception:
        _log.debug("gather 任务异常", exc_info=True)
    finally:
        read_task.cancel()
        stdin_task.cancel()
        if ws in session.ws_connections:
            session.ws_connections.remove(ws)
        # Phase 21: 会话保活——WS 断开不杀进程
        try:
            await ws.close()
        except Exception:
            _log.debug("关闭 WebSocket 失败", exc_info=True)
