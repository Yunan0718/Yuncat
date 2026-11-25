"""system_tool_advanced.py —— 高级工具（git / edit_file / best_of_n / create_mcp_server）"""
import difflib as _difflib
from pathlib import Path as _Path

from config import (
    SYSTEM_NOTIFICATIONS_LIMIT,
    GIT_COMMIT_MESSAGE_MAX_CHARS,
    GIT_STDOUT_PREVIEW_MAX_CHARS,
    GIT_STDERR_PREVIEW_MAX_CHARS,
    GIT_OBSERVATION_PREVIEW_MAX_CHARS,
    SYSTEM_TOOL_ADVANCED_EDIT_SEARCH_PREVIEW_CHARS,
    SYSTEM_TOOL_ADVANCED_DELEGATE_RESULT_PREVIEW_CHARS,
    SYSTEM_TOOL_ADVANCED_BEST_OF_N_PROMPT_PREVIEW_CHARS,
    SYSTEM_TOOL_ADVANCED_CRON_TASK_PREVIEW_CHARS,
    SYSTEM_TOOL_ADVANCED_CRON_TASK_OBSERVATION_CHARS,
    SYSTEM_TOOL_ADVANCED_CRON_LIST_TASK_PREVIEW_CHARS,
    SYSTEM_TOOL_ADVANCED_CRON_RAW_PREVIEW_CHARS,
    FILE_OPS_DIFF_CONTENT_PREVIEW_CHARS,
)
from ..registry import tool


# ── Phase 43 U.1: git — 白名单 git 子命令 ──

GIT_WHITELIST = {
    "init","add","commit","diff","log","status","branch","checkout",
    "stash","push","pull","remote","restore","reset","tag","show",
}

@tool(
    name="git", agents=["hermes"],
    description=(
        "在 workspace 中执行安全的 git 操作。支持子命令: "
        + ", ".join(sorted(GIT_WHITELIST))
        + "。所有操作限制在 workspace 目录内。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "subcommand": {"type": "string", "description": "git subcommand (init/add/commit/diff/...)"},
            "args": {"type": "string", "description": "额外的参数", "default": ""},
            "message": {"type": "string", "description": "commit message (commit时)", "default": ""},
        },
        "required": ["subcommand"],
    },
    confirmable=True,
    groups=["runtime"],
    tool_timeout=120,  # P2-2.4: git 子进程超时（白名单子命令，远低于 run_command）
)
async def git(subcommand: str, args: str = "", message: str = "") -> dict:
    """安全的 git 封装——白名单子命令 + workspace 限制。"""
    from tools.result import ToolResult
    from security.sandbox import get_workspace
    import subprocess as _subprocess
    import asyncio

    if subcommand not in GIT_WHITELIST:
        return ToolResult(
            success=False,
            error=f"Git subcommand '{subcommand}' not in whitelist: {', '.join(sorted(GIT_WHITELIST))}",
            meta={"tool_name": "git"},
        ).to_dict()

    ws = str(get_workspace())
    import shlex as _shlex
    cmd_list = ["git", subcommand]
    # Phase 48 T1-13: 过滤危险 flag 防 -C/--git-dir/--work-tree 绕过 workspace
    _GIT_DANGER_FLAGS = {"-C", "--git-dir", "--work-tree"}
    if args:
        try:
            parts = _shlex.split(args)
        except ValueError:
            parts = [args]
        for p in parts:
            if p in _GIT_DANGER_FLAGS:
                return ToolResult(
                    success=False, error=f"危险 git flag 被拒绝: {p}",
                    meta={"tool_name": "git"},
                ).to_dict()
        cmd_list.extend(parts)
    if subcommand == "commit" and message:
        cmd_list.extend(["-m", message[:GIT_COMMIT_MESSAGE_MAX_CHARS]])

    try:
        result = await asyncio.to_thread(
            _subprocess.run,
            cmd_list, capture_output=True, text=True,
            timeout=30, cwd=ws,
        )
        return ToolResult(
            success=result.returncode == 0,
            data={"stdout": result.stdout[:GIT_STDOUT_PREVIEW_MAX_CHARS], "stderr": result.stderr[:GIT_STDERR_PREVIEW_MAX_CHARS], "returncode": result.returncode},
            observation=result.stdout[:GIT_OBSERVATION_PREVIEW_MAX_CHARS] or result.stderr[:GIT_OBSERVATION_PREVIEW_MAX_CHARS],
            meta={"tool_name": "git", "subcommand": subcommand},
        ).to_dict()
    except _subprocess.TimeoutExpired:
        return ToolResult(success=False, error="Git 操作超时 (30s)", meta={"tool_name": "git"}).to_dict()
    except FileNotFoundError:
        return ToolResult(success=False, error="Git 未安装", meta={"tool_name": "git"}).to_dict()


# ── Phase 43 V.3: edit_file — SEARCH/REPLACE 精确编辑 ──

@tool(
    name="edit_file", agents=["hermes"],
    description=(
        "精确编辑文件——用 SEARCH/REPLACE 块替换特定代码段，而不是重写整个文件。"
        "对标 Claude Code SEARCH/REPLACE 模式。"
        "适合编辑大文件（>200 行）或小改动（<30% 内容）。"
        "新文件或大改（>50%）请用 write_file。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "编辑的文件路径（工作区内）"},
            "edits": {
                "type": "array",
                "description": "编辑项列表，每项包含 search 和 replace",
                "items": {
                    "type": "object",
                    "properties": {
                        "search": {"type": "string", "description": "要查找的精确文本（必须完全匹配）"},
                        "replace": {"type": "string", "description": "替换后的文本"},
                        "replace_all": {"type": "boolean", "description": "Task 6.12: 是否替换所有匹配（默认 false 只替换第一个）。对标 Claude Code replace_all 参数。", "default": False},
                    },
                    "required": ["search", "replace"],
                },
            },
            "expected_hash": {"type": "string", "default": "",
                              "description": "Task 6.4: Expected SHA-256 of current file content. If provided, edit is rejected when file was modified externally (optimistic concurrency). Obtain from read_file result 'content_hash'."},
            "expected_mtime": {"type": "number", "default": 0,
                               "description": "Task 6.4: Expected file mtime (epoch seconds). Secondary check used only when expected_hash is not provided. Obtain from read_file result 'mtime'."},
        },
        "required": ["path", "edits"],
    },
    confirmable=True,
    groups=["fs"],
)
async def edit_file(path: str, edits: list[dict],
                    expected_hash: str = "", expected_mtime: float = 0.0) -> dict:
    """SEARCH/REPLACE 精确编辑——默认只替第一次精确匹配·对标 Claude Code。

    Task 6.12 (F-042): 每个 edit 项可选 replace_all=True 替换所有匹配。
    对标 Claude Code edit 工具的 replace_all 参数。

    Task 6.4 (F-043): expected_hash / expected_mtime 乐观并发控制——
    若文件内容 hash/mtime 与预期不匹配（被外部修改），拒绝编辑。
    对标 Claude Code content hash / mtime 检查。
    """
    import os as _os
    from tools.result import ToolResult
    from security.sandbox import get_shared_workspace, get_workspace  # Phase 48 T1-5: 加路径校验防穿越
    # Task 6.4: 复用 write_file 的冲突检测逻辑（避免重复实现）
    from .system_tool_file_ops import check_file_conflict, compute_content_hash, resolve_shared_path

    # path 与 read/write_file 同口径解析（自动兼容带/不带 shared/ 前缀，避免 shared/shared 双写）
    try:
        full_path = str(resolve_shared_path(path, get_shared_workspace()))
    except PermissionError as e:
        return ToolResult(
            success=False, error=str(e),
            meta={"tool_name": "edit_file"},
        ).to_dict()
    if not _os.path.exists(full_path):
        return ToolResult(
            success=False,
            error=f"文件不存在: {path}",
            meta={"tool_name": "edit_file"},
        ).to_dict()

    try:
        original = _Path(full_path).read_text(encoding="utf-8")
    except Exception as e:
        return ToolResult(
            success=False,
            error=f"读取文件失败: {e}",
            meta={"tool_name": "edit_file"},
        ).to_dict()

    # Task 6.4: 外部修改冲突检测——校验 expected_hash / expected_mtime
    # 对标 Claude Code content hash 检查：文件被外部修改时拒绝编辑，提示重新读取
    _current_mtime = _Path(full_path).stat().st_mtime
    ok, err, conflict = check_file_conflict(
        original, _current_mtime, expected_hash, expected_mtime,
    )
    if not ok:
        return ToolResult(
            success=False, error=err, observation=err,
            meta={"tool_name": "edit_file", "conflict": conflict, "phase": "preview"},
        ).to_dict()

    current = original
    matches = 0
    failed = []

    for i, edit in enumerate(edits):
        search = edit.get("search", "")
        replace = edit.get("replace", "")
        if not search:
            failed.append(f"edit[{i}]: search 不能为空")
            continue
        # DSH 借鉴: search == replace 是无意义 no-op，直接拒绝
        # （对标 dsh edit 工具 old_string !== new_string 校验）
        if search == replace:
            failed.append(f"edit[{i}]: search 与 replace 相同，是无意义的编辑")
            continue

        # Task 6.12 (F-042): replace_all=True 替换所有匹配（对标 Claude Code）
        do_replace_all = bool(edit.get("replace_all", False))

        if do_replace_all:
            # str.replace 替换所有匹配
            count = current.count(search)
            if count == 0:
                failed.append(
                    f"edit[{i}]: 未找到匹配文本 (前{SYSTEM_TOOL_ADVANCED_EDIT_SEARCH_PREVIEW_CHARS}字: {search[:SYSTEM_TOOL_ADVANCED_EDIT_SEARCH_PREVIEW_CHARS]}...)"
                )
                continue
            current = current.replace(search, replace)
            matches += count
        else:
            # 精确匹配——只替换第一次出现
            idx = current.find(search)
            if idx == -1:
                failed.append(
                    f"edit[{i}]: 未找到匹配文本 (前{SYSTEM_TOOL_ADVANCED_EDIT_SEARCH_PREVIEW_CHARS}字: {search[:SYSTEM_TOOL_ADVANCED_EDIT_SEARCH_PREVIEW_CHARS]}...)"
                )
                continue
            current = current[:idx] + replace + current[idx + len(search):]
            matches += 1

    if matches == 0:
        return ToolResult(
            success=False,
            error=f"所有 {len(edits)} 个编辑块均未找到匹配。\n" + "\n".join(failed),
            meta={"tool_name": "edit_file", "failed": failed},
        ).to_dict()

    # E1: rel_path 相对 workspace 根（如 "shared/foo.txt"）——与 write_file 一致，
    # 供 _resolve_pending_path 正确解析为绝对路径（否则 payload["path"] 指向 ws 根而非 shared）
    rel_path = str(_Path(full_path).relative_to(get_workspace()))

    # 生成 unified diff 预览
    diff_lines = list(_difflib.unified_diff(
        original.splitlines(keepends=True),
        current.splitlines(keepends=True),
        fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}",
    ))

    return ToolResult(
        success=True,
        data={
            "path": rel_path, "matches": matches, "total_edits": len(edits),
            "unified_diff": "".join(diff_lines),
            "failed": failed if failed else None,
            "old_size": len(original), "new_size": len(current),
            # E1: 补 old_content/new_content 供前端 InlineDiff 渲染（对标 write_file）
            "old_content": original[:FILE_OPS_DIFF_CONTENT_PREVIEW_CHARS],
            "new_content": current[:FILE_OPS_DIFF_CONTENT_PREVIEW_CHARS],
            "diff_summary": f"{matches}/{len(edits)} 处替换",
            # H2: 工作区根路径——供前端确认弹窗可视化安全边界
            "workspace_root": str(get_workspace()),
            # Task 6.4: 当前文件状态——供 execute 阶段冲突检测 / agent 重试用
            "content_hash": compute_content_hash(original),
            "mtime": _current_mtime,
        },
        observation=f"已精确替换 {matches}/{len(edits)} 处（共 {len(original)}→{len(current)} 字符）",
        meta={"tool_name": "edit_file", "phase": "preview"},
    ).to_dict()


# ── Phase 43 W.5: best_of_n — 多后端并行比较 ──

@tool(
    name="best_of_n", agents=["hermes"],
    description=(
        "将同一任务发送给 N 个后端并行执行，返回所有结果供对比选择。"
        "对标 Cursor /best-of-n。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "要发送的任务（会发给所有可用后端）"},
            "n": {"type": "integer", "description": "后端数量 (默认 2)", "default": 2},
        },
        "required": ["prompt"],
    },
    groups=["advanced"],
    tool_timeout=600,  # P2-2.4: 并行 LLM 调用超时（对齐 ADAPTER_INVOKE_TIMEOUT）
)
async def best_of_n(prompt: str, n: int = 2) -> dict:
    """N 个后端并行 → 返回全部结果。

    2026-09-17: n 按能力档封顶（默认 3 / MAX 5）——这是成本乘数。
    """
    from services.capability_limits import current_limits

    n = max(1, min(int(n or 2), current_limits().parallel_candidates))

    from tools.result import ToolResult
    from adapters.registry import get_global_adapters
    import asyncio as _asyncio

    adapters = get_global_adapters()
    agent_ids = list(adapters.keys())[:n]
    if not agent_ids:
        return ToolResult(success=False, error="No backends available", meta={"tool_name": "best_of_n"}).to_dict()

    async def _run_one(aid: str) -> dict:
        try:
            ad = adapters[aid]
            if hasattr(ad, "invoke"):
                r = await ad.invoke(prompt)
                return {"agent": aid, "result": r.get("content", str(r))[:SYSTEM_TOOL_ADVANCED_DELEGATE_RESULT_PREVIEW_CHARS] if isinstance(r, dict) else str(r)[:SYSTEM_TOOL_ADVANCED_DELEGATE_RESULT_PREVIEW_CHARS]}
            return {"agent": aid, "error": "invoke not supported"}
        except Exception as e:
            return {"agent": aid, "error": str(e)}

    results = await _asyncio.gather(*[_run_one(aid) for aid in agent_ids])
    return ToolResult(
        success=True,
        data={"results": results, "prompt": prompt[:SYSTEM_TOOL_ADVANCED_BEST_OF_N_PROMPT_PREVIEW_CHARS]},
        observation=f"从 {len(results)} 个后端获取结果",
        meta={"tool_name": "best_of_n"},
    ).to_dict()


# ── Phase 43 W.6: create_mcp_server — 对标 Cline auto-MCP ──

@tool(
    name="create_mcp_server", agents=["hermes"],
    description=(
        "自动创建一个 MCP 服务器。Agent 编写代码 → 自动安装·配置·注册到 Yuncat。"
        "对标 Cline: 'add a tool that...' → Agent creates MCP server automatically。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "MCP 服务器名称"},
            "code": {"type": "string", "description": "MCP 服务器代码（stdio JSON-RPC 兼容）"},
            "description": {"type": "string", "description": "服务器描述", "default": ""},
        },
        "required": ["name", "code"],
    },
    confirmable=True,
    groups=["config"],
)
async def create_mcp_server(name: str, code: str, description: str = "") -> dict:
    """自动创建 MCP 服务器——写文件·注册到 DB·Yuncat 重启后自动启动。"""
    from tools.result import ToolResult
    import os as _os
    import re as _re

    # Phase 48 T1-12: name 白名单校验防路径穿越/RCE
    if not _re.match(r"^[a-zA-Z0-9_-]+$", name):
        return ToolResult(
            success=False,
            error=f"非法 name '{name}'——只允许字母/数字/下划线/连字符",
            meta={"tool_name": "create_mcp_server"},
        ).to_dict()

    mcp_dir = _os.path.join(_os.path.expanduser("~"), ".yuncat", "mcp", name)
    _os.makedirs(mcp_dir, exist_ok=True)

    # 写入服务器代码
    code_path = _os.path.join(mcp_dir, f"{name}.py")
    with open(code_path, "w", encoding="utf-8") as f:
        f.write(code)

    # 写入注册信息
    from context import get_db
    db = get_db()
    if db:
        import json as _json, time as _time, uuid as _uuid
        server_id = str(_uuid.uuid4())
        await db.execute(
            "INSERT OR REPLACE INTO mcp_servers(id, name, command, args, env, enabled) "
            "VALUES(?,?,?,?,?,?)",
            (server_id, name, _os.sys.executable, _json.dumps([code_path]),
             _json.dumps({}), 1),
        )
        await db.commit()
        return ToolResult(
            success=True,
                    data={"server_id": server_id, "name": name, "path": code_path},
                    observation=f"MCP 服务器 '{name}' 已创建并注册。重启 Yuncat 后自动启动。",
                    meta={"tool_name": "create_mcp_server"},
                ).to_dict()
    return ToolResult(
        success=False, error="无法访问数据库",
        meta={"tool_name": "create_mcp_server"},
    ).to_dict()


# ── Phase 47 P1 / AUDIT #9: Cron 调度器 — 真正评估 cron 表达式 ──
# AUDIT #9 修复前: cron 表达式作为死数据存入 loop_trigger_queue，~30s 内即触发
# 修复后: 存入 scheduled_tasks 表，_scheduler_tick 按 next_run_at <= now 检索触发

def _validate_schedule_args(task: str, schedule: str, run_at: str) -> str | None:
    """校验排期参数；返回错误文案，None 表示通过。"""
    if not task.strip():
        return "任务描述不能为空"
    if not run_at.strip() and not schedule.strip():
        return "要么给 schedule（cron 表达式），要么给 run_at（一次性排期）"
    return None


def _resolve_schedule(schedule: str, run_at: str) -> tuple[str, int]:
    """解析排期 → (cron_expr, next_run_at)。

    一次性排期的 cron_expr 留空——调度器据此在触发后标 completed
    （见 lifespan_postinit._reschedule_after_fire）。
    """
    from services.cron_parser import next_run_at, parse_run_at

    if run_at.strip():
        return "", parse_run_at(run_at)
    return schedule, next_run_at(schedule)


def _schedule_created_result(
    task: str, cron_id: str, schedule: str, nxt: int,
    session_id: str, budget: int, run_at: str,
) -> dict:
    """组装 cron_schedule 的成功结果（工具主体保持精简）。"""
    from datetime import datetime as _dt
    from tools.result import ToolResult

    next_str = _dt.fromtimestamp(nxt).strftime("%Y-%m-%d %H:%M:%S")
    mode_label = f"一次性（{run_at}）" if run_at.strip() else schedule
    return ToolResult(
        success=True,
        data={
            "cron_id": cron_id, "cron_expr": schedule,
            "once": bool(run_at.strip()),
            "next_run_at": nxt, "next_run_str": next_str,
            "task": task[:SYSTEM_TOOL_ADVANCED_CRON_TASK_PREVIEW_CHARS],
            "session_id": session_id,
            "budget": budget,
        },
        observation=f"定时任务已创建 (id={cron_id}): {mode_label} → 下次触发 {next_str}。"
                    f"任务: {task[:SYSTEM_TOOL_ADVANCED_CRON_TASK_OBSERVATION_CHARS]}。"
                    f"用 cron_list / cron_delete 管理。",
        meta={"tool_name": "cron_schedule"},
    ).to_dict()


@tool(
    name="cron_schedule", agents=["hermes", "openclaw"],
    description=(
        "创建定时任务——按 cron 表达式在指定时间自动执行任务。"
        "对标 Claude Code /schedule。到点后 Agent 自动执行。"
        "用 cron_list 查看已有任务，cron_delete 删除。"
        "AUDIT #9: 现在真正评估 cron 表达式（不再插入即触发）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "schedule": {"type": "string", "default": "",
                         "description": "标准 5 字段 cron 表达式: '分 时 日 月 周'。"
                                        "例: '0 9 * * *'(每天9点) / '*/15 * * * *'(每15分钟) / '0 0 * * 0'(每周日)。"
                                        "一次性任务改用 run_at，二者填一个。"},
            "run_at": {"type": "string", "default": "",
                       "description": "一次性排期（可选）：30m / 2h / 1d / 'in 30 minutes' / 09:30 / "
                                      "2026-10-23T09:00。填了就只跑一次，不用 schedule。"},
            "task": {"type": "string",
                     "description": "到点执行的任务——自然语言提示（Agent 会自主执行）"},
            "notify": {"type": "string", "default": "",
                       "description": "完成时通知渠道(可选): telegram/discord/email"},
            "label": {"type": "string", "default": "",
                      "description": "任务标签——用于 cron_list 筛选和 cron_delete 批量删除"},
            "session_id": {"type": "string", "default": "",
                           "description": "绑定到指定会话执行（可选）——不填则在新会话执行"},
            "budget": {"type": "integer", "default": 0,
                       "description": "Token 预算上限（可选，0=不限）。AUDIT #10/#13: "
                                      "到点执行时累加 token 用量，超限则优雅暂停"},
        },
        "required": ["task"],
    },
    groups=["advanced"],
)
async def cron_schedule(schedule: str = "", task: str = "", notify: str = "",
                        label: str = "", session_id: str = "", budget: int = 0,
                        run_at: str = "") -> dict:
    """AUDIT #9: 创建定时任务——解析 cron 表达式，计算 next_run_at，存入 scheduled_tasks 表。

    后台 _scheduler_tick Loop（30s 轮询）按 next_run_at <= now 检索到期任务并触发执行。
    循环 cron：执行完成后重新计算 next_run_at 恢复 pending。

    AUDIT #10/#13: budget>0 时为该 cron 任务设置 Token 预算上限，到点执行时
    _scheduler_tick 将其传入 task 的 max_budget_tokens，agent_tool_loop 累加
    used_tokens 并在超限时优雅暂停（status='paused_budget'）。
    """
    from tools.result import ToolResult
    from context import get_db
    from services.cron_parser import CronParseError
    from repositories.scheduled_tasks_repo import insert_scheduled_task

    db = get_db()
    if not db:
        return ToolResult(success=False, error="DB不可用",
                          meta={"tool_name": "cron_schedule"}).to_dict()
    invalid = _validate_schedule_args(task, schedule, run_at)
    if invalid:
        return ToolResult(success=False, error=invalid,
                          meta={"tool_name": "cron_schedule"}).to_dict()
    try:
        schedule, nxt = _resolve_schedule(schedule, run_at)
    except (CronParseError, ValueError) as e:
        return ToolResult(success=False, error=f"排期无效: {e}",
                          meta={"tool_name": "cron_schedule"}).to_dict()

    # AUDIT #10/#13: 传入 session_id 和 max_budget_tokens
    cron_id = await insert_scheduled_task(
        db, task_input=task, cron_expr=schedule, next_run_at=nxt,
        label=label, notify=notify,
        session_id=session_id or None,
        max_budget_tokens=budget if budget > 0 else None,
    )
    return _schedule_created_result(
        task, cron_id, schedule, nxt, session_id, budget, run_at)


@tool(
    name="cron_list", agents=["hermes", "openclaw"],
    description="列出所有待执行的定时任务。",
    parameters={"type": "object", "properties": {}, "required": []},
    is_read_only=True,
    groups=["advanced"],
)
async def cron_list() -> dict:
    """AUDIT #9: 列出所有 pending 的 scheduled_tasks。"""
    from tools.result import ToolResult
    from context import get_db
    from repositories.scheduled_tasks_repo import list_scheduled_tasks
    from datetime import datetime as _dt

    db = get_db()
    if not db:
        return ToolResult(success=False, error="DB不可用",
                          meta={"tool_name": "cron_list"}).to_dict()
    rows = await list_scheduled_tasks(db, limit=SYSTEM_NOTIFICATIONS_LIMIT)
    tasks = []
    for r in rows:
        nxt_str = _dt.fromtimestamp(r["next_run_at"]).strftime("%Y-%m-%d %H:%M:%S") if r["next_run_at"] else ""
        tasks.append({
            "id": r["id"],
            "cron_expr": r["cron_expr"],
            "task": r["task_input"][:SYSTEM_TOOL_ADVANCED_CRON_LIST_TASK_PREVIEW_CHARS],
            "label": r["label"],
            "next_run_at": r["next_run_at"],
            "next_run_str": nxt_str,
            "run_count": r["run_count"],
            "created_at": r["created_at"],
            "max_budget_tokens": r.get("max_budget_tokens"),
            "used_tokens": r.get("used_tokens", 0),
        })
    return ToolResult(
        success=True, data={"count": len(tasks), "tasks": tasks},
        observation=f"共 {len(tasks)} 个待执行的定时任务。",
        meta={"tool_name": "cron_list"},
    ).to_dict()


@tool(
    name="cron_delete", agents=["hermes", "openclaw"],
    description="删除定时任务——按 cron_id 或 label 删除。",
    parameters={
        "type": "object",
        "properties": {
            "cron_id": {"type": "string", "description": "要删除的 cron_id(从 cron_list 获取)"},
            "label": {"type": "string", "default": "", "description": "按标签批量删除"},
        },
        "required": [],
    },
    groups=["advanced"],
)
async def cron_delete(cron_id: str = "", label: str = "") -> dict:
    """AUDIT #9: 取消定时任务（status='paused'）。"""
    from context import get_db
    from tools.result import ToolResult
    from repositories.scheduled_tasks_repo import cancel_scheduled_task

    db = get_db()
    if not db:
        return ToolResult(success=False, error="DB不可用",
                          meta={"tool_name": "cron_delete"}).to_dict()
    if not cron_id and not label:
        return ToolResult(success=False, error="需提供 cron_id 或 label",
                          meta={"tool_name": "cron_delete"}).to_dict()
    affected = await cancel_scheduled_task(db, task_id=cron_id, label=label)
    return ToolResult(
        success=True,
        data={"affected": affected},
        observation=f"已取消 {affected} 个匹配的定时任务。",
        meta={"tool_name": "cron_delete"},
    ).to_dict()

#
