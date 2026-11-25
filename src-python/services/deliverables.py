"""services/deliverables.py —— 交付物：present 声明 + 从会话历史还原的改动

对标 DSH `packages/deliverables`：
  - `tool-present`：模型声明"这是我要交给用户的最终文件"（落 session_deliverables）；
  - `workspace-changes`：记录一轮里改了哪些文件。DSH 靠 git 快照，
    Yuncat 这里从**会话历史里的工具调用**还原——工具层知道每个文件动作的真实路径，
    不需要为每一轮扫全工作区（工作区大时那会很贵）。

诚实边界：经 shell 命令改的文件不在工具层可见，这种改动还原不出来——
UI 与文档都只声称"工具层改过的文件"，不谎称覆盖所有改动。
"""
from __future__ import annotations

import difflib
import json
import logging
from pathlib import Path

_log = logging.getLogger("yuncat.deliverables")

# 会改文件的工具 → 其路径参数名
FILE_MUTATING_TOOLS: dict[str, tuple[str, ...]] = {
    "write_file": ("path",),
    "edit_file": ("path",),
    "delete_file": ("path",),
    "delete_dir": ("path",),
    "rename_file": ("old_path", "new_path"),
}


async def session_id_for_task(db, task_id: str) -> str:
    """由 task_id 解析会话 id（工具在任务上下文里跑，会话从任务表取）。"""
    if not db or not task_id:
        return ""
    cursor = await db.execute(
        "SELECT session_id FROM tasks WHERE id=?", (task_id,))
    row = await cursor.fetchone()
    return str(row[0] or "") if row else ""


def relative_to_workspace(path: str) -> str:
    """能落在工作区内就返回相对路径，否则返回原路径（前端照样能打开）。"""
    try:
        from security.sandbox import get_workspace
        rel = str(path).replace(str(Path(get_workspace()).resolve()), "").lstrip("\\/")
        # 统一斜杠：present 的 rel_path 与 changes 的 display 必须同口径，否则前端去重失效
        return (rel or str(path)).replace("\\", "/")
    except Exception:
        return str(path).replace("\\", "/")


def _iter_tool_calls(raw: str):
    """遍历一条 assistant 消息里的工具调用（OpenAI function calling 形状）。"""
    try:
        calls = json.loads(raw)
    except (TypeError, ValueError):
        return
    if not isinstance(calls, list):
        return
    for call in calls:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") or {}
        name = str(fn.get("name") or call.get("name") or "")
        args_raw = fn.get("arguments", call.get("arguments"))
        if isinstance(args_raw, str):
            try:
                args = json.loads(args_raw)
            except (TypeError, ValueError):
                continue
        elif isinstance(args_raw, dict):
            args = args_raw
        else:
            args = {}
        yield name, args if isinstance(args, dict) else {}


async def record_shell_changes(
    db, session_id: str, task_id: str, paths: list[str], repo_root: str,
) -> list[dict]:
    """记录 shell 命令改动的文件（source='shell'）。

    对标 DSH workspace-changes 的"工作区改动"清单——Yuncat 原本只覆盖
    工具层改动，shell 改的文件（拉依赖、生成产物、git checkout）完全不可见。
    """
    from repositories.deliverables_repo import add_session_deliverable

    recorded: list[dict] = []
    for rel in paths:
        if not rel:
            continue
        abs_path = str(Path(repo_root) / rel) if repo_root else rel
        await add_session_deliverable(
            db, session_id, abs_path,
            rel_path=relative_to_workspace(abs_path),
            description="", source="shell", task_id=task_id,
        )
        recorded.append({
            "file_path": abs_path,
            "rel_path": relative_to_workspace(abs_path),
            "source": "shell",
        })
    return recorded


async def list_observed_changes(db, session_id: str) -> list[dict]:
    """从会话历史还原"这一轮动过哪些文件"（按首次出现顺序去重）。"""
    if not db or not session_id:
        return []
    cursor = await db.execute(
        "SELECT tool_calls_json FROM conversations "
        "WHERE session_id=? AND role='assistant' "
        "AND tool_calls_json IS NOT NULL AND tool_calls_json<>''",
        (session_id,),
    )
    rows = await cursor.fetchall()
    found: dict[str, dict] = {}
    for (raw,) in rows:
        for name, args in _iter_tool_calls(raw):
            for key in FILE_MUTATING_TOOLS.get(name, ()):
                path = str(args.get(key) or "").strip()
                if not path or path in found:
                    continue
                found[path] = {"path": path, "tool": name, "arg": key}
    return list(found.values())


# ============================================================
# 改动文件 + 增删行数（对标 DSH workspace/changes 的摘要口径）
# ============================================================

def display_path(path: str) -> str:
    """排序/展示用路径：工作区内相对路径，其余绝对路径，统一斜杠分隔。"""
    try:
        from security.sandbox import get_workspace
        rel = Path(path).resolve().relative_to(Path(get_workspace()).resolve())
        return str(rel).replace("\\", "/")
    except (ValueError, OSError):
        # 回退：工作区外/无法解析 → 原样展示（前端照样能打开）
        return str(path).replace("\\", "/")


def line_stats(old: str, new: str) -> tuple[int, int]:
    """(added, deleted) 行数——按行对比，不计算字符级差异。"""
    added = deleted = 0
    for line in difflib.ndiff(old.splitlines(), new.splitlines()):
        if line.startswith("+ "):
            added += 1
        elif line.startswith("- "):
            deleted += 1
    return added, deleted


def _read_side(path: Path, cap: int) -> dict:
    """读一侧内容：{"text"} / {"binary": True} / {"oversized": True} / {"missing": True}。"""
    try:
        if path.stat().st_size > cap:
            return {"oversized": True}
        data = path.read_bytes()
    except OSError:
        return {"missing": True}
    if b"\x00" in data[:4096]:
        return {"binary": True}
    return {"text": data.decode("utf-8", errors="replace")}


def _snapshot_side(root: Path, snapshot, path: str, cap: int) -> dict:
    """取某一侧内容：快照备份优先；没有快照时读当前文件。"""
    from services.snapshot_service import snapshot_entry_name

    if snapshot is None:
        return _read_side(Path(path), cap)
    sid, entry = snapshot
    if not entry.get("existed"):
        return {"text": ""}
    if entry.get("size_limited"):
        return {"oversized": True}
    return _read_side(root / sid / snapshot_entry_name(path), cap)


_SESSION_SNAPSHOT_SQL = (
    "SELECT s.id, s.phase, s.files_affected FROM snapshots s "
    "JOIN tasks t ON t.id = s.task_id WHERE t.session_id=? "
    "ORDER BY s.created_at ASC, s.rowid ASC"
)


def _index_snapshot_rows(rows) -> tuple[list[str], dict, dict]:
    """把快照行整理成（出现顺序, 最早 pre, 最晚 post）。"""
    from services.snapshot_service import normalize_entries

    order: list[str] = []
    pre: dict[str, tuple[str, dict]] = {}
    post: dict[str, tuple[str, dict]] = {}
    for sid, phase, raw in rows:
        for entry in normalize_entries(raw):
            path = entry["path"]
            if path not in order:
                order.append(path)
            if phase == "pre":
                pre.setdefault(path, (sid, entry))
            elif phase == "post":
                post[path] = (sid, entry)
    return order, pre, post


def _change_item(path: str, pre_snap, post_snap, root: Path, cap: int) -> dict:
    """单个改动条目：旧侧取最早的 pre 备份，新侧取当前磁盘（见下方说明）。"""
    entry = (pre_snap or post_snap)[1]
    display = display_path(path)
    # path 是绝对路径（diff 端点按它定位）；rel_path 供工作区按相对路径打开
    item = {
        "path": path,
        "display": display,
        "rel_path": "" if display.startswith("/") or ":" in display[:3] else display,
        "kind": entry.get("kind", "file"),
    }
    if item["kind"] == "dir":
        item["directory"] = True
        return item
    old = _snapshot_side(root, pre_snap, path, cap)
    # 新侧取**当前磁盘**：write_file/edit_file 是「预览 → 确认后才落盘」，
    # post 快照拍在预览之后（内容未变），拿它比对会恒等于 0 行。
    new = _read_side(Path(path), cap)
    if new.get("missing"):
        item["missing"] = True
        new = {"text": ""}
    if old.get("oversized") or new.get("oversized"):
        item["oversized"] = True
    elif old.get("binary") or new.get("binary"):
        item["binary"] = True
    else:
        item["added"], item["deleted"] = line_stats(
            old.get("text") or "", new.get("text") or "")
    return item


async def list_changed_files_with_stats(db, session_id: str) -> list[dict]:
    """会话内改过的文件 + 增删行数（数据源：snapshots 的改动前整文件捕获）。

    目录只登记、不给行数；二进制/超限显式标注，不伪造成 0。
    """
    from services.snapshot_service import snapshot_dir
    from settings import settings as _settings

    if not db or not session_id:
        return []
    cursor = await db.execute(_SESSION_SNAPSHOT_SQL, (session_id,))
    rows = await cursor.fetchall()
    if not rows:
        return []
    order, pre, post = _index_snapshot_rows(rows)
    root = snapshot_dir()
    cap = max(64 * 1024, int(_settings.deliverable_diff_max_bytes))
    return [_change_item(path, pre.get(path), post.get(path), root, cap) for path in order]


def unified_diff_text(display: str, old: str, new: str, max_lines: int) -> tuple[str, bool]:
    """生成 unified diff 文本（3 行上下文）；超过 max_lines 截断并显式标注。"""
    lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=f"a/{display}", tofile=f"b/{display}", lineterm="",
    ))
    truncated = len(lines) > max_lines
    if truncated:
        lines = lines[:max_lines] + [f"... (diff 已截断，仅显示前 {max_lines} 行)"]
    return (("\n".join(lines) + "\n") if lines else ""), truncated


def _find_recorded_path(pre: dict, post: dict, path: str) -> str | None:
    """把请求路径对到本会话记录过的绝对路径（解析后比较，防任意文件读取）。"""
    try:
        target = str(Path(path).resolve())
    except OSError:
        return None
    for recorded in list(pre) + list(post):
        try:
            if str(Path(recorded).resolve()) == target:
                return recorded
        except OSError:
            continue  # 回退：单条坏记录跳过，不影响其余匹配
    return None


def _diff_result(base: dict, old: dict, new: dict, max_lines: int) -> dict:
    """把"旧侧 / 新侧"内容转成统一返回形状（二进制/超限/文本三分支）。"""
    if old.get("oversized") or new.get("oversized"):
        return {**base, "kind": "oversized", "diff": "", "added": 0,
                "deleted": 0, "truncated": False}
    if old.get("binary") or new.get("binary"):
        return {**base, "kind": "binary", "diff": "", "added": 0,
                "deleted": 0, "truncated": False}
    old_text = old.get("text") or ""
    new_text = "" if new.get("missing") else (new.get("text") or "")
    added, deleted = line_stats(old_text, new_text)
    diff, truncated = unified_diff_text(base["display"], old_text, new_text, max_lines)
    return {**base, "kind": "text", "diff": diff, "added": added,
            "deleted": deleted, "truncated": truncated}


async def _shell_file_diff(db, session_id: str, path: str) -> dict | None:
    """shell 改动文件的 diff：旧侧取 git HEAD，新侧取当前磁盘。

    只接受**本会话登记过的 shell 改动**（session_deliverables.source='shell'）——
    否则这个端点等于任意文件读取器。未跟踪/新文件没有旧侧内容，返回 unknown，
    不臆造 diff。
    """
    from security.sandbox import get_workspace
    from services.shell_changes import git_head_blob
    from settings import settings as _settings

    try:
        target = Path(path).resolve()
    except OSError:
        return None
    if not target.is_relative_to(Path(get_workspace()).resolve()):
        return None
    cursor = await db.execute(
        "SELECT 1 FROM session_deliverables "
        "WHERE session_id=? AND source='shell' AND file_path=? LIMIT 1",
        (session_id, str(target)),
    )
    if await cursor.fetchone() is None:
        return None
    base = {
        "path": str(target),
        "display": display_path(str(target)),
        "before": True,
        "after": target.exists(),
    }
    blob = await git_head_blob(str(target))
    if blob is None:
        return {**base, "before": False, "kind": "unknown", "diff": "",
                "added": 0, "deleted": 0, "truncated": False}
    cap = max(64 * 1024, int(_settings.deliverable_diff_max_bytes))
    if len(blob) > cap:
        old_side: dict = {"oversized": True}
    elif b"\x00" in blob[:4096]:
        old_side = {"binary": True}
    else:
        old_side = {"text": blob.decode("utf-8", errors="replace")}
    return _diff_result(
        base, old_side, _read_side(target, cap),
        max(50, int(_settings.deliverable_diff_max_lines)),
    )


async def file_diff(db, session_id: str, path: str) -> dict:
    """会话内单个改动文件的 unified diff（旧=最早 pre 备份，新=当前磁盘）。

    shell 改动的文件没有快照备份——旧侧改用 git HEAD（见 _shell_file_diff）。
    """
    from services.snapshot_service import snapshot_dir

    if not db or not session_id or not path:
        return {"status": "error", "message": "missing session or path"}
    cursor = await db.execute(_SESSION_SNAPSHOT_SQL, (session_id,))
    order, pre, post = _index_snapshot_rows(await cursor.fetchall())
    match = _find_recorded_path(pre, post, path) if order else None
    if match is None:
        shell_diff = await _shell_file_diff(db, session_id, path)
        if shell_diff is not None:
            return shell_diff
        # 只能看本会话记录过的路径——否则这个端点等于任意文件读取器
        return {"status": "error", "message": "path is not a recorded change in this session"}
    from security.sandbox import get_workspace
    if not Path(match).resolve().is_relative_to(Path(get_workspace()).resolve()):
        # 纵深防御：记录行也可能被篡改，越界一律拒绝
        return {"status": "error", "message": "path is outside the workspace"}
    from settings import settings as _settings
    root = snapshot_dir()
    cap = max(64 * 1024, int(_settings.deliverable_diff_max_bytes))
    base = {
        "path": match,
        "display": display_path(match),
        "before": bool((pre.get(match) or (None, {}))[1].get("existed")),
        "after": Path(match).exists(),
    }
    if match not in pre:
        # 只有 post（历史遗留行）：没有"改动前"内容，不臆造 diff
        return {**base, "kind": "unknown", "diff": "", "added": 0,
                "deleted": 0, "truncated": False}
    old = _snapshot_side(root, pre.get(match), match, cap)
    new = _read_side(Path(match), cap)
    return _diff_result(
        base, old, new, max(50, int(_settings.deliverable_diff_max_lines)))
