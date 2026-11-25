"""system_tool_checkpoint.py —— 文件检查点工具"""
from ..registry import tool


# ═══════════════════════════════════════════════════════════
# Phase F: 文件检查点工具 — 对标 VS Code localHistory
# ═══════════════════════════════════════════════════════════

@tool(
    name="checkpoint_list", agents=["hermes", "openclaw"],
    description=(
        "列出某个文件的所有历史版本检查点。在 write_file/delete_file/rename_file 之前"
        "系统自动保存副本。用这个工具查看: 最近改了什么、何时改的、文件大小变化。"
        "对标 VS Code localHistory / JetBrains Local History。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "工作区内的文件路径"},
            "limit": {"type": "integer", "description": "最多返回多少条 (默认 10)"},
        },
        "required": ["path"],
    },
    is_read_only=True, is_concurrency_safe=True,
    groups=["advanced"],
)
async def checkpoint_list(path: str, limit: int = 10) -> dict:
    """列出文件的完整修改历史——每次 write/delete/rename 之前都有快照。"""
    from services.infra.file_checkpoint import FileCheckpoint
    from security.sandbox import validate_path
    from tools.result import ToolResult
    # Phase 47 修复（2.4 路径遍历防护）：校验 path 在工作区内
    try:
        validate_path(path)
    except PermissionError:
        return ToolResult(
            success=False, error=f"路径越界，已拒绝: {path}",
            meta={"tool_name": "checkpoint_list"},
        ).to_dict()
    ck = FileCheckpoint()
    try:
        entries = ck.list(path)[:limit]
        return ToolResult(
            success=True,
            data={"file": path, "checkpoints": entries, "total": len(entries)},
            observation=f"{path} 有 {len(entries)} 个历史版本" if entries else f"{path} 暂无检查点",
            meta={"tool_name": "checkpoint_list"},
        ).to_dict()
    except Exception as e:
        return ToolResult(success=False, error=str(e), meta={"tool_name": "checkpoint_list"}).to_dict()


@tool(
    name="checkpoint_restore", agents=["hermes", "openclaw"],
    description=(
        "从历史检查点恢复文件的某个版本。先用 checkpoint_list 查看可用版本，"
        "然后用这个工具恢复到指定版本。恢复后当前内容会被覆盖。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要恢复的文件路径"},
            "checkpoint_id": {"type": "string", "description": "检查点 ID (从 checkpoint_list 获取)"},
        },
        "required": ["path", "checkpoint_id"],
    },
    confirmable=True,
    groups=["advanced"],
)
async def checkpoint_restore(path: str, checkpoint_id: str) -> dict:
    """从检查点恢复文件。"""
    from services.infra.file_checkpoint import FileCheckpoint
    from security.sandbox import validate_path
    from tools.result import ToolResult
    # Phase 47 修复（2.4 路径遍历防护）：校验 path 在工作区内
    try:
        validate_path(path)
    except PermissionError:
        return ToolResult(
            success=False, error=f"路径越界，已拒绝: {path}",
            meta={"tool_name": "checkpoint_restore"},
        ).to_dict()
    ck = FileCheckpoint()
    ok = ck.restore(path, checkpoint_id)
    if ok:
        return ToolResult(
            success=True,
            data={"file": path, "restored_from": checkpoint_id},
            observation=f"已从检查点 {checkpoint_id} 恢复 {path}",
            meta={"tool_name": "checkpoint_restore"},
        ).to_dict()
    return ToolResult(
        success=False,
        error=f"检查点 {checkpoint_id} 不存在于 {path} 的历史中",
        meta={"tool_name": "checkpoint_restore"},
    ).to_dict()
