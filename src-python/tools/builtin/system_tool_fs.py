"""system_tool_fs.py —— 文件系统工具"""
from ..registry import tool
from config import SYSTEM_TOOL_FS_DIR_LIST_PREVIEW_MAX


# ════════════════════════════════════════════════════════════
# Phase 33: mkdir + reveal_in_explorer 工具
# ════════════════════════════════════════════════════════════

@tool(
    name="mkdir", agents=["hermes", "openclaw"],
    description="Create a new directory in the workspace. Parent directories are created automatically.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path within workspace to create"},
        },
        "required": ["path"],
    },
    is_concurrency_safe=True,
    groups=["fs"],
)
async def mkdir(path: str) -> dict:
    """创建工作区目录"""
    from security.sandbox import validate_path, get_workspace
    from tools.result import ToolResult
    try:
        full = validate_path(path)
    except PermissionError as e:
        return ToolResult(success=False, error=str(e), meta={"tool_name": "mkdir"}).to_dict()
    try:
        full.mkdir(parents=True, exist_ok=True)
        rel = str(full.relative_to(get_workspace()))
        return ToolResult(success=True, data={"path": rel},
                          observation=f"目录已创建: {rel}",
                          meta={"tool_name": "mkdir"}).to_dict()
    except Exception as e:
        return ToolResult(success=False, error=str(e), meta={"tool_name": "mkdir"}).to_dict()


@tool(
    name="reveal_in_explorer", agents=["hermes", "openclaw"],
    description=(
        "Open a file or folder in the system file explorer (Finder on macOS, "
        "Explorer on Windows). Use this when the user wants to locate a file "
        "or browse the workspace visually. The frontend will call the OS-native API."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File or directory path to reveal"},
        },
        "required": ["path"],
    },
    is_read_only=True, is_concurrency_safe=True,
    groups=["fs"],
)
async def reveal_in_explorer(path: str) -> dict:
    """在系统资源管理器中打开文件/目录位置"""
    from security.sandbox import validate_path, get_workspace
    from tools.result import ToolResult
    import json as _json
    try:
        full = validate_path(path)
    except PermissionError as e:
        return ToolResult(success=False, error=str(e), meta={"tool_name": "reveal_in_explorer"}).to_dict()
    rel = str(full.relative_to(get_workspace()))
    abspath = str(full.resolve())
    return ToolResult(
        success=True,
        data={"path": rel, "absolute_path": abspath, "is_dir": full.is_dir()},
        observation=f"已在资源管理器中打开: {rel}",
        meta={"tool_name": "reveal_in_explorer", "client_action": _json.dumps({
            "type": "reveal_in_explorer", "path": abspath, "is_dir": full.is_dir(),
        })},
    ).to_dict()


@tool(
    name="rename_file", agents=["hermes", "openclaw"],
    description="Rename or move a file/directory within the workspace.",
    parameters={
        "type": "object",
        "properties": {
            "old_path": {"type": "string", "description": "Current path within workspace"},
            "new_path": {"type": "string", "description": "New path within workspace"},
        },
        "required": ["old_path", "new_path"],
    },
    confirmable=True,
    groups=["fs"],
)
async def rename_file(old_path: str, new_path: str) -> dict:
    """重命名或移动文件/目录——预览阶段返回信息，确认后执行。"""
    from security.sandbox import validate_path, get_workspace
    from tools.result import ToolResult
    try:
        old = validate_path(old_path)
        new = validate_path(new_path)
    except PermissionError as e:
        return ToolResult(success=False, error=str(e), meta={"tool_name": "rename_file"}).to_dict()
    if not old.exists():
        return ToolResult(success=False, error=f"文件不存在: {old_path}",
                          meta={"tool_name": "rename_file"}).to_dict()
    rel_old = str(old.relative_to(get_workspace()))
    rel_new = str(new.relative_to(get_workspace()))
    return ToolResult(
        success=True,
        data={"old_path": rel_old, "new_path": rel_new, "size": old.stat().st_size if old.is_file() else 0, "is_dir": old.is_dir()},
        observation=f"即将重命名/移动: {rel_old} → {rel_new}。请确认。",
        meta={"tool_name": "rename_file", "phase": "preview"},
    ).to_dict()


@tool(
    name="delete_dir", agents=["hermes", "openclaw"],
    description="Delete an empty directory from the workspace. Directory must be empty.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path within workspace to delete"},
        },
        "required": ["path"],
    },
    confirmable=True,
    groups=["fs"],
)
async def delete_dir(path: str) -> dict:
    """删除空目录——预览+确认模式。"""
    from security.sandbox import validate_path, get_workspace
    from tools.result import ToolResult
    try:
        full = validate_path(path)
    except PermissionError as e:
        return ToolResult(success=False, error=str(e), meta={"tool_name": "delete_dir"}).to_dict()
    if not full.exists():
        return ToolResult(success=False, error=f"目录不存在: {path}",
                          meta={"tool_name": "delete_dir"}).to_dict()
    if not full.is_dir():
        return ToolResult(success=False, error=f"是文件而非目录: {path}，请使用 delete_file",
                          meta={"tool_name": "delete_dir"}).to_dict()
    contents = list(full.iterdir())
    if contents:
        names = ", ".join(c.name for c in contents[:SYSTEM_TOOL_FS_DIR_LIST_PREVIEW_MAX])
        more = f" (+{len(contents)-SYSTEM_TOOL_FS_DIR_LIST_PREVIEW_MAX})" if len(contents) > SYSTEM_TOOL_FS_DIR_LIST_PREVIEW_MAX else ""
        return ToolResult(success=False,
                          error=f"目录非空（{len(contents)} 项: {names}{more}），请先清空或使用文件管理器",
                          meta={"tool_name": "delete_dir"}).to_dict()
    rel = str(full.relative_to(get_workspace()))
    return ToolResult(
        success=True,
        data={"path": rel},
        observation=f"即将删除空目录: {rel}。请确认。",
        meta={"tool_name": "delete_dir", "phase": "preview"},
    ).to_dict()
