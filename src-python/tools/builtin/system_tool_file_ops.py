"""system_tool_file_ops.py —— 文件操作工具"""
import hashlib
import logging
from pathlib import Path

from ..registry import tool
from config import (
    calc_tool_result_limit, protect_critical_info,
    FILE_OPS_DIFF_CONTENT_PREVIEW_CHARS,
    FILE_OPS_WRITE_PREVIEW_LINES,
    FILE_OPS_DELETE_PREVIEW_CHARS,
    FILE_OPS_EDIT_OLD_PREVIEW_CHARS,
    FILE_OPS_EDIT_NEW_PREVIEW_CHARS,
    TOOL_SIZE_READ_FILE_CHARS,
    TOOL_SIZE_LIST_DIR_CHARS,
)

_log = logging.getLogger("yuncat.tools")

# 用户文件区的唯一约定：Agent 看到并回传的路径是「工作区根相对」（如 "shared/a.txt"），
# 而工具入参历史上以 shared/ 为锚点（写 "a.txt" 即 shared/a.txt）。两种写法都必须可用，
# 否则 write_file 的返回值喂回 read_file 会变成 shared/shared/a.txt（2026-09-22 实测）。
_SHARED_PREFIX = "shared/"


def resolve_shared_path(path: str, base: Path):
    """把用户/模型给的路径解析到工作区内的绝对路径（两种写法等价）。

    带 "shared/" 前缀 → 相对工作区根；否则 → 相对 shared 目录（历史语义，保持兼容）。
    """
    from security.sandbox import get_workspace, validate_path

    norm = str(path or "").replace("\\", "/").lstrip("/")
    if norm == "shared" or norm.startswith(_SHARED_PREFIX):
        # 已带 shared/ 前缀：它就是工作区根相对路径，不能再剥前缀
        return validate_path(norm, base=get_workspace())
    return validate_path(path, base=base)


# ── Task 6.4: 文件外部修改冲突检测（对标 Claude Code content hash / mtime 检查）──


def compute_content_hash(content: str) -> str:
    """计算文件内容的 SHA-256 哈希（hex digest）。

    用于 edit_file / write_file 的乐观并发控制——agent 读取文件时获得 hash，
    编辑时传回 expected_hash，执行前校验文件是否被外部修改。

    对标 Claude Code content hash 机制：用 SHA-256 检测 preview→confirm 间隙
    的外部修改（用户手动编辑 / 其他 IDE / 多工具并发），不匹配则拒绝写入。
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def check_file_conflict(
    current_content: str,
    current_mtime: float,
    expected_hash: str = "",
    expected_mtime: float = 0.0,
) -> tuple[bool, str, str]:
    """校验文件是否被外部修改（乐观并发控制）。

    优先校验 expected_hash（强校验，内容级）；若未传 hash 才校验 expected_mtime
    （弱校验，时间戳级）。两者均未传则跳过校验（向后兼容）。

    Args:
        current_content: 文件当前内容（用于计算 hash 对比）
        current_mtime: 文件当前 mtime（epoch seconds）
        expected_hash: agent 上次读取时获得的 content_hash（可选）
        expected_mtime: agent 上次读取时获得的 mtime（可选）

    Returns:
        (ok, error_msg, conflict_type):
        - ok=True: 校验通过（或未传 expected_*，跳过校验）
        - ok=False: 冲突 detected；error_msg 描述原因；conflict_type 为
          'hash_mismatch' / 'mtime_mismatch'
    """
    if expected_hash:
        actual_hash = compute_content_hash(current_content)
        if expected_hash != actual_hash:
            return (
                False,
                f"文件已被外部修改（content hash 不匹配）。请重新读取文件后再编辑。"
                f"expected={expected_hash[:16]}…, actual={actual_hash[:16]}…",
                "hash_mismatch",
            )
        # hash 匹配 → 跳过 mtime 检查（hash 是强校验，优先级最高）
        return True, "", ""
    if expected_mtime:
        # 1ms 容差应对 float 精度；文件被修改时 mtime 差异远超 1ms
        if abs(expected_mtime - current_mtime) > 0.001:
            return (
                False,
                f"文件已被外部修改（mtime 不匹配）。请重新读取文件后再编辑。"
                f"expected={expected_mtime:.6f}, actual={current_mtime:.6f}",
                "mtime_mismatch",
            )
    return True, "", ""


@tool(
    name="read_file", agents=["hermes", "openclaw"],
    description=(
        "Read a file from the workspace. Use offset and limit for large files. "
        "First 50KB returned if no offset specified."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative or absolute path within workspace"},
            "offset": {"type": "integer", "description": "Line number to start reading from (1-indexed)", "default": 0},
            "limit": {"type": "integer", "description": "Max lines to return (default: 500 lines if offset>0, uncapped if offset=0)", "default": 0},
        },
        "required": ["path"],
    },
    is_read_only=True, is_concurrency_safe=True,
    groups=["fs"],
    max_result_size_chars=TOOL_SIZE_READ_FILE_CHARS,  # Phase 56-P8: 自我声明结果大小
)
async def read_file(path: str, offset: int = 0, limit: int = 0) -> dict:
    """读取工作区文件，返回内容（截断 50KB）"""
    from security.sandbox import validate_path, get_shared_workspace
    from tools.result import ToolResult, gen_read_observation, gen_read_error_observation
    # Task 3.1: 路径 deny 主防线——读前阻止 .env / credentials / *.pem 等敏感文件
    # 对标 Claude Code disallowed-paths——读前阻止优于读后脱敏
    from tools.builtin.system_tool_safety import is_denied_path
    denied, deny_reason = is_denied_path(path)
    if denied:
        return ToolResult(
            success=False, error=deny_reason,
            observation=gen_read_error_observation(path, deny_reason),
            meta={"tool_name": "read_file", "denied": True},
        ).to_dict()
    try:
        full = resolve_shared_path(path, get_shared_workspace())
    except PermissionError as e:
        return ToolResult(
            success=False, error=str(e),
            observation=gen_read_error_observation(path, str(e)),
            meta={"tool_name": "read_file"},
        ).to_dict()

    if not full.exists():
        return ToolResult(
            success=False, error=f"文件不存在: {path}",
            observation=gen_read_error_observation(path, "不存在"),
            meta={"tool_name": "read_file"},
        ).to_dict()
    if full.is_dir():
        return ToolResult(
            success=False, error=f"路径是目录而非文件: {path}",
            observation=gen_read_error_observation(path, "目录"),
            meta={"tool_name": "read_file"},
        ).to_dict()

    try:
        content = full.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return ToolResult(
            success=False, error=str(e),
            observation=gen_read_error_observation(path, str(e)),
            meta={"tool_name": "read_file"},
        ).to_dict()

    # Task 6.4: 返回 content_hash + mtime——供 agent 传给 edit_file/write_file 做冲突检测
    # 对标 Claude Code：read 返回 hash，edit 时传回 expected_hash 校验文件未被外部修改
    _content_hash = compute_content_hash(content)
    _mtime = full.stat().st_mtime

    # Phase 34: 支持 offset/limit 精确行号分段读取，衔接多次调用
    if offset > 0 or limit > 0:
        lines = content.split("\n")
        total_lines = len(lines)
        start = max(0, offset - 1) if offset > 0 else 0
        end = start + min(limit or 2000, 2000) if (offset > 0 or limit > 0) else len(lines)
        end = min(end, len(lines))
        sliced = "\n".join(lines[start:end])
        has_more = end < total_lines
        return ToolResult(
            success=True,
            data={
                "path": str(full), "content": sliced,
                "total_lines": total_lines,
                "start_line": start + 1, "end_line": end,
                "has_more": has_more,
                "next_offset": end + 1 if has_more else None,
                "content_hash": _content_hash,
                "mtime": _mtime,
            },
            observation=(
                f"读取 {path} 第 {start+1}-{end} 行（共 {total_lines} 行）"
                + (f"。还有 {total_lines - end} 行未读，用 offset={end+1} 继续" if has_more else "")
            ),
            meta={"tool_name": "read_file", "total_lines": total_lines, "has_more": has_more},
        ).to_dict()

    # Phase 43 P0-DYN: read_file 动态上限 + has_more/next_offset 续读
    _max_chars = calc_tool_result_limit({"model": "default"})  # 从默认上下文窗口派生
    truncated = len(content) > _max_chars
    display = content[:_max_chars]
    total_lines = content.count('\n') + 1
    has_more = truncated
    if truncated:
        _critical = protect_critical_info(content[_max_chars:])
        display += f"\n\n... (截断，共 {len(content)} bytes，{total_lines} 行)"
        if _critical:
            display += f"\n关键信息:\n{_critical}"
        display += f"\n用 offset={len(display.split(chr(10)))} 继续读取"
    return ToolResult(
        success=True,
        data={
            "path": str(full),
            "content": display,
            "has_more": has_more,
            "next_offset": len(display.split('\n')) + 1 if has_more else None,
            "total_lines": total_lines,
            "truncated": truncated,
            "total_chars": len(content),
            "size_bytes": len(content),
            "content_hash": _content_hash,
            "mtime": _mtime,
        },
        observation=gen_read_observation(path, truncated, len(content)),
        meta={"tool_name": "read_file", "size_bytes": len(content), "total_lines": content.count("\n") + 1},
    ).to_dict()


@tool(
    name="list_dir", agents=["hermes", "openclaw"],
    description="List files and directories within a workspace directory.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path within workspace"},
        },
        "required": ["path"],
    },
    is_read_only=True, is_concurrency_safe=True,
    groups=["fs"],
    max_result_size_chars=TOOL_SIZE_LIST_DIR_CHARS,  # Phase 56-P8: 自我声明结果大小
)
async def list_dir(path: str) -> dict:
    """列出目录内容"""
    from security.sandbox import validate_path, get_shared_workspace
    from tools.result import ToolResult, gen_list_observation, gen_generic_error_observation
    # Task 3.1: 路径 deny 主防线——读前阻止 credentials / secrets 等敏感目录
    from tools.builtin.system_tool_safety import is_denied_path
    denied, deny_reason = is_denied_path(path)
    if denied:
        return ToolResult(
            success=False, error=deny_reason, observation=deny_reason,
            meta={"tool_name": "list_dir", "denied": True},
        ).to_dict()
    try:
        full = resolve_shared_path(path, get_shared_workspace())
    except PermissionError as e:
        return ToolResult(success=False, error=str(e), observation=str(e), meta={"tool_name": "list_dir"}).to_dict()

    if not full.exists():
        return ToolResult(success=False, error=f"目录不存在: {path}", observation=f"目录 {path} 不存在", meta={"tool_name": "list_dir"}).to_dict()
    if not full.is_dir():
        return ToolResult(success=False, error=f"路径不是目录: {path}", observation=f"{path} 不是目录", meta={"tool_name": "list_dir"}).to_dict()

    try:
        entries = []
        for entry in sorted(full.iterdir()):
            entry_type = "dir" if entry.is_dir() else "file"
            try:
                size = entry.stat().st_size if entry.is_file() else None
            except OSError:
                size = None
            entry_info = {"name": entry.name, "type": entry_type}
            if size is not None:
                entry_info["size"] = size
            entries.append(entry_info)
            if len(entries) >= 200:
                entries.append({"name": "... (truncated, 200+ entries)", "type": "note"})
                break
    except Exception as e:
        return ToolResult(success=False, error=str(e), observation=gen_generic_error_observation("list_dir", str(e)), meta={"tool_name": "list_dir"}).to_dict()

    return ToolResult(
        success=True,
        data={"path": str(full), "entries": entries, "count": len(entries)},
        observation=gen_list_observation(path, len(entries)),
        meta={"tool_name": "list_dir", "count": len(entries)},
    ).to_dict()


@tool(
    name="write_file", agents=["hermes", "openclaw"],
    description=(
        "Write or update a file in the workspace. "
        "If file exists, old content is returned for diff comparison. "
        "Supports partial edits: use mode='append' to add content at end of file. "
        "Default mode='overwrite' replaces the entire file."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path within workspace"},
            "content": {"type": "string", "description": "Content to write"},
            "mode": {"type": "string", "enum": ["overwrite", "append"], "default": "overwrite",
                      "description": "overwrite replaces file, append adds to end"},
            "expected_hash": {"type": "string", "default": "",
                              "description": "Task 6.4: Expected SHA-256 of current file content. If provided, write is rejected when file was modified externally (optimistic concurrency). Obtain from read_file result 'content_hash'. New files skip this check."},
            "expected_mtime": {"type": "number", "default": 0,
                               "description": "Task 6.4: Expected file mtime (epoch seconds). Secondary check used only when expected_hash is not provided. Obtain from read_file result 'mtime'."},
        },
        "required": ["path", "content"],
    },
    confirmable=True,
    groups=["fs"],
)
async def write_file(path: str, content: str, mode: str = "overwrite",
                     expected_hash: str = "", expected_mtime: float = 0.0) -> dict:
    """Claude Code 风格预览：生成 unified diff + 智能自动确认。

    mode='append'：追加到文件末尾，不替换已有内容。
    mode='overwrite'：替换整个文件。

    Task 6.4 (F-043): expected_hash / expected_mtime 乐观并发控制——
    若文件现有内容 hash/mtime 与预期不匹配（被外部修改），拒绝写入。
    对标 Claude Code content hash 检查。新文件（不存在）跳过校验。

    预览包含:
      - unified_diff: 标准 unified diff 格式文本（最多 200 行差异）
      - diff_stats: 增/删/改 统计
      - 总大小和行数
      - content_hash / mtime: 当前文件状态（供 execute 阶段或重试用）
    """
    from security.sandbox import validate_path, get_workspace, get_shared_workspace
    from tools.result import ToolResult, gen_write_observation
    try:
        full = resolve_shared_path(path, get_shared_workspace())
    except PermissionError as e:
        return ToolResult(success=False, error=str(e), observation=str(e), meta={"tool_name": "write_file"}).to_dict()

    old_content = ""
    current_mtime = 0.0
    if full.exists():
        try:
            old_content = full.read_text(encoding="utf-8", errors="replace")
            current_mtime = full.stat().st_mtime
        except Exception:
            _log.debug("读取旧文件内容失败", exc_info=True)
        # Task 6.4: 外部修改冲突检测——仅对已存在文件校验（新文件无冲突可能）
        ok, err, conflict = check_file_conflict(
            old_content, current_mtime, expected_hash, expected_mtime,
        )
        if not ok:
            return ToolResult(
                success=False, error=err, observation=err,
                meta={"tool_name": "write_file", "conflict": conflict,
                      "phase": "preview"},
            ).to_dict()

    if mode == "append":
        effective_content = old_content + ("\n" + content if old_content and not old_content.endswith("\n") and not content.startswith("\n") else content)
    else:
        effective_content = content

    rel_path = str(full.relative_to(get_workspace()))

    # 生成 unified diff（Claude Code / Cursor 风格）
    diff_text = _generate_unified_diff(old_content, effective_content, rel_path,
                                        is_new=not bool(old_content), mode=mode)
    diff_stats = _compute_diff_stats(old_content, effective_content)

    return ToolResult(
        success=True,
        data={
            "path": rel_path,
            "mode": mode,
            "is_new": not bool(old_content),
            "total_size": len(effective_content),
            "total_lines": effective_content.count("\n") + 1,
            "old_size": len(old_content) if old_content else 0,
            "old_lines": old_content.count("\n") + 1 if old_content else 0,
            "unified_diff": diff_text,
            "diff_stats": diff_stats,
            # 向后兼容旧字段
            "new_content": effective_content[:FILE_OPS_DIFF_CONTENT_PREVIEW_CHARS],
            "old_content": old_content[:FILE_OPS_DIFF_CONTENT_PREVIEW_CHARS],
            "diff_summary": diff_stats.get("summary", ""),
            # H2: 工作区根路径——供前端确认弹窗可视化安全边界
            "workspace_root": str(get_workspace()),
            # Task 6.4: 当前文件状态——供 execute 阶段冲突检测 / agent 重试用
            "content_hash": compute_content_hash(old_content) if old_content else "",
            "mtime": current_mtime,
        },
        observation=gen_write_observation(rel_path, len(effective_content)),
        meta={"tool_name": "write_file", "phase": "preview", "size": len(effective_content),
              "mode": mode, "is_new": not bool(old_content)},
    ).to_dict()


def _generate_unified_diff(old: str, new: str, path: str, is_new: bool = False,
                             mode: str = "overwrite", context_lines: int = 3) -> str:
    """生成 unified diff 格式文本。Claude Code / Cursor 风格：紧凑但可读。"""
    if is_new:
        lines = new.split("\n")
        preview = "\n".join(
            f"+ {line}" if line else "+"
            for line in lines[:FILE_OPS_WRITE_PREVIEW_LINES]
        )
        suffix = f"\n... ({'新建文件' if mode != 'append' else '追加'}, 共 {len(lines)} 行)" if len(lines) > FILE_OPS_WRITE_PREVIEW_LINES else ""
        return f"--- /dev/null\n+++ {path}\n@@ -0,0 +1,{len(lines)} @@\n{preview}{suffix}"

    import difflib
    old_lines = old.split("\n")
    new_lines = new.split("\n")

    differ = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        lineterm="",
        n=context_lines,  # 上下文行数
    )
    diff_lines = list(differ)

    # 限制输出行数（长 diff 截断）
    max_lines = 250
    if len(diff_lines) > max_lines:
        diff_lines = diff_lines[:max_lines]
        diff_lines.append(f"... [diff 截断，完整共 {len(list(difflib.unified_diff(old_lines, new_lines, lineterm='')))} 行差异]")

    return "\n".join(diff_lines)


def _compute_diff_stats(old: str, new: str) -> dict:
    """计算增/删/改统计。不再是 set-based——保留行序和重复。"""
    if not old:
        new_lines_count = new.count("\n") + 1
        return {"added": new_lines_count, "removed": 0, "changed": 0,
                "unchanged": 0, "total_old": 0, "total_new": new_lines_count,
                "summary": f"新建文件（{new_lines_count} 行, {len(new)} bytes）"}

    old_lines = old.split("\n")
    new_lines = new.split("\n")
    old_set = set(old_lines)
    new_set = set(new_lines)
    added = len(new_set - old_set)
    removed = len(old_set - new_set)
    unchanged = len(old_set & new_set)
    # 变化行 = 旧独有 + 新独有 - 纯增/纯删（粗略估计）
    changed = max(0, added + removed - abs(len(old_set) - len(new_set)))

    total_old = len(old_lines)
    total_new = len(new_lines)

    size_diff = len(new) - len(old)
    sign = "+" if size_diff > 0 else ""
    summary = (
        f"{' → '.join(str(p) for p in [f'+{added}', f'-{removed}', f'~{changed}' if changed else None, f'={unchanged}' if unchanged else None] if p)}"
        f" 行变化（{total_old}→{total_new} 行, {sign}{size_diff:,} bytes）"
    )

    return {
        "added": added, "removed": removed, "changed": changed,
        "unchanged": unchanged,
        "total_old": total_old, "total_new": total_new,
        "summary": summary,
    }


# ---- Phase 34: delete_file + diff 工具（Claude Code/Cursor 对标）----

@tool(
    name="delete_file", agents=["hermes", "openclaw"],
    description=(
        "Delete a file from the workspace. Returns file info before deletion. "
        "Use this to clean up temporary files or remove unwanted code."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path within workspace to delete"},
        },
        "required": ["path"],
    },
    confirmable=True,
    groups=["fs"],
)
async def delete_file(path: str) -> dict:
    """删除文件——预览阶段返回文件信息，确认后才执行。"""
    from security.sandbox import validate_path, get_workspace
    from tools.result import ToolResult
    try:
        full = validate_path(path)
    except PermissionError as e:
        return ToolResult(success=False, error=str(e), meta={"tool_name": "delete_file"}).to_dict()

    if not full.exists():
        return ToolResult(success=False, error=f"文件不存在: {path}",
                          meta={"tool_name": "delete_file"}).to_dict()
    if full.is_dir():
        return ToolResult(success=False, error=f"是目录而非文件: {path}，如需删除目录请手动操作",
                          meta={"tool_name": "delete_file"}).to_dict()

    try:
        size = full.stat().st_size
        preview = full.read_text(encoding="utf-8", errors="replace")[:FILE_OPS_DELETE_PREVIEW_CHARS]
    except Exception as e:
        return ToolResult(success=False, error=str(e), meta={"tool_name": "delete_file"}).to_dict()

    rel_path = str(full.relative_to(get_workspace()))
    return ToolResult(
        success=True,
        data={
            "path": rel_path, "size": size, "preview": preview,
            # H2: 工作区根路径——供前端确认弹窗可视化安全边界
            "workspace_root": str(get_workspace()),
        },
        observation=f"即将删除 {rel_path}（{size} bytes）。请确认。",
        meta={"tool_name": "delete_file", "phase": "preview"},
    ).to_dict()


async def _handle_delete(db, payload: dict) -> dict:
    """执行文件删除——确认后二次验证路径安全。"""
    from security.sandbox import validate_path, get_workspace
    from tools.result import ToolResult
    full = Path(payload["path"])
    try:
        validate_path(str(full.relative_to(Path(get_workspace()) / "shared")))
    except (ValueError, PermissionError):
        return ToolResult(success=False, error="路径验证失败",
                          meta={"tool_name": "delete_file"}).to_dict()
    try:
        size = full.stat().st_size
        full.unlink()
        return ToolResult(success=True, data={"path": str(full), "freed_bytes": size},
                          observation=f"已删除 {full.name}（释放 {size} bytes）",
                          meta={"tool_name": "delete_file", "phase": "executed"}).to_dict()
    except FileNotFoundError:
        return ToolResult(success=False, error="文件已不存在",
                          meta={"tool_name": "delete_file"}).to_dict()
    except Exception as e:
        return ToolResult(success=False, error=str(e),
                          meta={"tool_name": "delete_file"}).to_dict()


@tool(
    name="diff", agents=["hermes", "openclaw"],
    description=(
        "Show the difference between the current file content and a proposed new version. "
        "Use this before write_file to preview changes, or to show what would change."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to diff against"},
            "proposed_content": {"type": "string", "description": "Proposed new content to compare"},
        },
        "required": ["path", "proposed_content"],
    },
    is_read_only=True, is_concurrency_safe=True,
    groups=["fs"],
)
async def diff(path: str, proposed_content: str) -> dict:
    """独立的 diff 工具——Agent 可以在写文件前预览差异。"""
    from security.sandbox import validate_path, get_workspace
    from tools.result import ToolResult
    try:
        full = validate_path(path)
    except PermissionError as e:
        return ToolResult(success=False, error=str(e), meta={"tool_name": "diff"}).to_dict()

    old_content = ""
    is_new = not full.exists()
    if not is_new:
        try:
            old_content = full.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return ToolResult(success=False, error=str(e), meta={"tool_name": "diff"}).to_dict()

    rel_path = str(full.relative_to(get_workspace()))

    # 行级 diff
    old_lines = old_content.split("\n")
    new_lines = proposed_content.split("\n")
    added = 0
    removed = 0
    changed = 0
    max_len = max(len(old_lines), len(new_lines))
    for i in range(max_len):
        ol = old_lines[i] if i < len(old_lines) else None
        nl = new_lines[i] if i < len(new_lines) else None
        if ol is None and nl is not None:
            added += 1
        elif ol is not None and nl is None:
            removed += 1
        elif ol != nl:
            changed += 1

    return ToolResult(
        success=True,
        data={
            "path": rel_path,
            "is_new": is_new,
            "old_size": len(old_content),
            "new_size": len(proposed_content),
            "stats": {"added": added, "removed": removed, "changed": changed, "unchanged": max_len - added - removed - changed},
            "old_preview": old_content[:FILE_OPS_EDIT_OLD_PREVIEW_CHARS] if old_content else "",
            "new_preview": proposed_content[:FILE_OPS_EDIT_NEW_PREVIEW_CHARS],
        },
        observation=(
            f"新建文件 {rel_path}（{len(proposed_content)} bytes）" if is_new
            else f"diff {rel_path}: +{added} -{removed} ~{changed} 行变化（{len(old_content)}→{len(proposed_content)} bytes）"
        ),
        meta={"tool_name": "diff", "is_new": is_new},
    ).to_dict()
