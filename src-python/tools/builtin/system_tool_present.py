"""tools/builtin/system_tool_present.py —— 交付物声明（对标 DSH tool-present）

模型在做完活之后调用 `present`，把"我要交给用户的最终文件"声明出来：
路径会被校验（必须在工作区内、必须是存在的普通文件），记录绝对路径，
用户就能在 UI 上一键打开。记录的是路径与说明，**不复制文件内容**。
"""
from __future__ import annotations

import logging

from ..registry import tool
from tools.result import ToolResult

_log = logging.getLogger("yuncat.present")


def _max_files() -> int:
    from settings import settings as _settings
    return max(1, int(_settings.present_max_files))


def _resolve_deliverable(path: str, description: str) -> tuple[str, str]:
    """校验一个交付文件：必须在工作区内、必须是存在的普通文件。"""
    from security.sandbox import validate_path
    from services.deliverables import relative_to_workspace

    raw_path = str(path or "").strip()
    if not raw_path:
        raise ValueError("file path must be a non-empty string")
    try:
        resolved = validate_path(raw_path)
    except PermissionError as exc:
        raise ValueError(f"path is outside the workspace: {exc}") from exc
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError(f"invalid path: {exc}") from exc
    if not resolved.is_file():
        raise ValueError(f"not an existing regular file: {raw_path}")
    return str(resolved), str(description or "").strip()[:200]


@tool(
    name="present",
    agents=["yuncat", "hermes", "openclaw"],
    description=(
        "Declare the final files you produced so the user can open them directly. "
        "Call it after creating the files, listing the files you actually want to hand over. "
        "Relative paths resolve against the workspace; each path must be an existing regular "
        "file inside the workspace. Use it only for the final deliverables, "
        "not for every intermediate file."
    ),
    parameters={
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "description": "The final files to deliver.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path of the file to deliver (workspace-relative or absolute).",
                        },
                        "description": {
                            "type": "string",
                            "description": "One line describing what this file is.",
                        },
                    },
                    "required": ["path"],
                },
            },
        },
        "required": ["files"],
    },
    is_read_only=False,
    is_concurrency_safe=True,
    groups=["interaction"],
)
async def present(files: list) -> dict:
    """声明本次交付的最终文件。"""
    from context import get_current_task_id, get_db
    from repositories.deliverables_repo import add_session_deliverable
    from services.deliverables import relative_to_workspace, session_id_for_task

    if not isinstance(files, list) or not files:
        return ToolResult(
            success=False, error="files must be a non-empty array",
            meta={"tool_name": "present"}).to_dict()
    limit = _max_files()
    if len(files) > limit:
        return ToolResult(
            success=False, error=f"too many files (max {limit})",
            meta={"tool_name": "present"}).to_dict()
    db = get_db()
    task_id = get_current_task_id() or ""
    if db is None or not task_id:
        return ToolResult(
            success=False, error="present requires a running task in a session",
            meta={"tool_name": "present"}).to_dict()
    session_id = await session_id_for_task(db, task_id)
    if not session_id:
        return ToolResult(
            success=False, error="present could not resolve the session for this task",
            meta={"tool_name": "present"}).to_dict()
    delivered: list[str] = []
    for item in files:
        if not isinstance(item, dict):
            return ToolResult(
                success=False, error="each file entry must be an object",
                meta={"tool_name": "present"}).to_dict()
        try:
            resolved, description = _resolve_deliverable(
                item.get("path"), item.get("description", ""))
        except ValueError as exc:
            return ToolResult(
                success=False, error=str(exc),
                meta={"tool_name": "present"}).to_dict()
        await add_session_deliverable(
            db, session_id, str(resolved),
            rel_path=relative_to_workspace(str(resolved)),
            description=description, source="present", task_id=task_id,
        )
        delivered.append(relative_to_workspace(str(resolved)))
    await _broadcast_deliverables(session_id)
    return ToolResult(
        success=True,
        data={"files": delivered},
        observation=f"Presented {len(delivered)} file(s): {', '.join(delivered)}",
        meta={"tool_name": "present", "session_id": session_id}).to_dict()


async def _broadcast_deliverables(session_id: str) -> None:
    """通知前端刷新交付物面板。"""
    try:
        from context import get_app
        app = get_app()
        service = getattr(app.state, "agent_service", None) if app else None
        manager = getattr(service, "ws_manager", None) if service else None
        if manager is None:
            return
        await manager.broadcast({
            "type": "deliverables_updated", "session_id": session_id,
        })
    except Exception:
        _log.warning("推送 deliverables_updated 失败（交付物已落库）", exc_info=True)
