"""tools/builtin/system_tool_todo.py —— 会话级任务清单工具

对标 deepseek-harness 的 `packages/todo/tool-todo`：

- 模型每次提交**完整清单**做整体替换（无部分更新、无逐项编辑）；
- 每项只有 content + status 两个字段，content 去空白后非空且不重复；
- status 取值 pending / in_progress / completed；
- 是否允许多个 in_progress 由部署配置决定（Yuncat 支持子 Agent 与并行工具，
  默认允许；描述词随之切换——与 DSH 的 allowParallelInProgress 同一套机制）；
- 清单归属会话，跨轮次、跨重启都在（工具结果与会话历史里也留有快照）。

与 DSH 的唯一偏差：DSH 的清单归属「单个 agent session」，Yuncat 归属「用户的一个
聊天会话」——因为 Yuncat 的子 Agent 只是主 Agent 的执行细节，用户要看到的是同一份
清单，子 Agent 各持一份的话用户与主 Agent 都看不到进展。
"""
from __future__ import annotations

import logging

from ..registry import tool
from tools.result import ToolResult

_log = logging.getLogger("yuncat.todo")

TODO_STATUSES = ("pending", "in_progress", "completed")

_DESC_HEAD = (
    "Record and update a task list to plan multi-step work and show progress; "
    "skip it for trivial single-step tasks. Add one todo per concrete step before you start. "
)
_DESC_PARALLEL = (
    "While work remains, keep the todos being worked on `in_progress`, "
    "several only when work runs in parallel. "
)
_DESC_SINGLE = "While work remains, keep exactly one todo `in_progress`. "
_DESC_TAIL = "Mark each todo `completed` as soon as it is done."


def describe_todo_tool(allow_parallel: bool) -> str:
    """拼装模型可见的工具描述——只有"活动项纪律"这一句随配置变。"""
    return (_DESC_HEAD + (_DESC_PARALLEL if allow_parallel else _DESC_SINGLE) + _DESC_TAIL)


def validate_todos(raw, allow_parallel: bool) -> list[dict]:
    """按 DSH 契约校验并规范化整份清单；不合法直接抛 ValueError。"""
    if not isinstance(raw, list):
        raise ValueError("todos must be an array")
    todos: list[dict] = []
    seen: set[str] = set()
    active = 0
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each todo must be an object")
        unknown = sorted(set(item) - {"content", "status"})
        if unknown:
            raise ValueError(f"unknown todo fields: {unknown}")
        content = str(item.get("content") or "").strip()
        if not content:
            raise ValueError("todo content must be a non-empty string")
        if content in seen:
            raise ValueError(f"duplicate todo content: {content!r}")
        seen.add(content)
        status = str(item.get("status") or "").strip()
        if status not in TODO_STATUSES:
            raise ValueError(f"invalid todo status: {status!r}")
        if status == "in_progress":
            active += 1
        todos.append({"content": content, "status": status})
    if not allow_parallel and active > 1:
        raise ValueError(f"at most one task may be in_progress (got {active})")
    return todos


def summarize_todos(todos: list[dict]) -> str:
    """生成回执文案（与 DSH 一致，让模型知道清单已生效）。"""
    counts = {status: 0 for status in TODO_STATUSES}
    for item in todos:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return (f"Updated todo list: {counts['pending']} pending, "
            f"{counts['in_progress']} in progress, {counts['completed']} completed.")


def _allow_parallel() -> bool:
    from settings import settings as _settings
    return bool(_settings.todo_allow_parallel)


async def _broadcast_todo_update(session_id: str, todos: list[dict]) -> None:
    """把清单推给所有窗口——UI 立即显示最新进度。"""
    try:
        from context import get_app
        app = get_app()
        service = getattr(app.state, "agent_service", None) if app else None
        manager = getattr(service, "ws_manager", None) if service else None
        if manager is None:
            return
        await manager.broadcast({
            "type": "todo_updated", "session_id": session_id, "todos": todos,
        })
    except Exception:
        _log.warning("推送 todo_updated 失败（清单已落库）", exc_info=True)


@tool(
    name="todo_write",
    agents=["yuncat", "hermes", "openclaw"],
    description=describe_todo_tool(_allow_parallel()),
    parameters={
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "The COMPLETE task list, replacing any previous list.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "What the task is — a short imperative line.",
                        },
                        "status": {
                            "type": "string",
                            "enum": list(TODO_STATUSES),
                            "description": "Task state.",
                        },
                    },
                    "required": ["content", "status"],
                },
            },
        },
        "required": ["todos"],
    },
    is_read_only=False,
    is_concurrency_safe=True,
    groups=["interaction"],
)
async def todo_write(todos: list) -> dict:
    """整表替换当前会话的任务清单，并广播给 UI。"""
    from context import get_current_agent_id, get_current_task_id, get_db
    from repositories.session_todos_repo import set_session_todos
    from services.deliverables import session_id_for_task

    try:
        normalized = validate_todos(todos, _allow_parallel())
    except ValueError as exc:
        return ToolResult(
            success=False, error=str(exc), meta={"tool_name": "todo_write"}).to_dict()
    db = get_db()
    task_id = get_current_task_id() or ""
    if db is None or not task_id:
        return ToolResult(
            success=False, error="todo_write requires a running task in a session",
            meta={"tool_name": "todo_write"}).to_dict()
    session_id = await session_id_for_task(db, task_id)
    if not session_id:
        return ToolResult(
            success=False, error="todo_write could not resolve the session for this task",
            meta={"tool_name": "todo_write"}).to_dict()
    await set_session_todos(
        db, session_id, normalized, owner_agent_id=get_current_agent_id() or "")
    await _broadcast_todo_update(session_id, normalized)
    return ToolResult(
        success=True, data={"todos": normalized},
        observation=summarize_todos(normalized),
        meta={"tool_name": "todo_write", "session_id": session_id}).to_dict()
