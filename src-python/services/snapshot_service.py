"""services/snapshot_service.py — 文件快照安全网 + Harness 录制回放（Phase 19）

快照方案：文件级备份（不依赖 git）。操作前备份目标，操作后再备份一次；
`POST /api/v1/tasks/{id}/undo`、`POST /api/v1/snapshots/{id}/restore` 据此回退。

2026-10-21 根因修复（此前整条链路恒空，撤销永远报「没有找到快照」）：
1. 路径口径：工具入参相对 `shared/`（或带 `shared/` 前缀），旧实现按工作区根解析，
   结果永远不是文件 → files_affected 为空 → 不写快照行。现在统一走工具侧的
   `resolve_shared_path`——同一份路径语义，不重复造轮子。
2. 任务身份：hook 从 `arguments["_task_id"]` 取任务号，而工具循环从不写这个键 →
   每行落成随机 uuid，`restore_turn(task_id)` 永远查不到。现在由
   `ToolService.execute()` 用 contextvar 传播真实 task_id。
3. 覆盖范围：hook 只匹配 write_file|run_command，edit/delete/rename 都没快照。
4. 备份命名：旧实现只留 basename，同名文件互相覆盖 → 回退写错内容。
5. 回退语义：旧实现把每个 pre 依次复制（最后一个赢），新建文件也无法删除。
   现在每个路径只认**最早**的 pre；改动前不存在的登记 existed=false，回退时删除；
   恢复走临时项 + os.replace，避免半写。

诚实边界：`run_command` 经 shell 改的文件不在本表（DSH 里由 git 树快照覆盖；Yuncat
共享工作区默认不是 git 仓库，因此这里不假装能回退 shell 改动）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path

from security.sandbox import get_workspace

_log = logging.getLogger("yuncat.snapshot")

# 会改文件的工具 → 路径参数名（与 tools/builtin 的入参一致）
MUTATING_TOOL_PATHS: dict[str, tuple[str, ...]] = {
    "write_file": ("path",),
    "edit_file": ("path",),
    "delete_file": ("path",),
    "rename_file": ("old_path", "new_path"),
    # 从 FileCheckpoint 恢复也会覆盖文件——同样要能被撤销
    "checkpoint_restore": ("path",),
}
# 目录级操作：递归备份（有大小上限，超限只登记不复制）
DIR_TOOL_PATHS: dict[str, tuple[str, ...]] = {
    "delete_dir": ("path",),
    "mkdir": ("path",),
}


def snapshot_hook_pattern() -> str:
    """hook 正则——覆盖所有会改文件的工具（供 hooks/registry.py 复用）。"""
    names = list(MUTATING_TOOL_PATHS) + list(DIR_TOOL_PATHS) + ["run_command"]
    return "|".join(names)


def snapshot_entry_name(abs_path: str) -> str:
    """备份项命名：路径哈希前缀 + basename，避免同名文件互相覆盖。"""
    digest = hashlib.sha1(str(abs_path).encode("utf-8")).hexdigest()[:16]
    return f"{digest}_{Path(abs_path).name}"


def snapshot_dir() -> Path:
    """快照根目录（位于工作区内、工具不可达的隐藏目录）。"""
    # Path(...) 包一层：测试里有人把 get_workspace 桩成返回字符串，
    # 直接 `get_workspace() / "..."` 会抛 "unsupported operand type(s) for /"。
    return Path(get_workspace()) / ".yuncat_snapshots"


def _tree_size_within(path: Path, limit: int) -> int:
    """目录树字节数；超过 limit 立即返回，不做无谓的全量遍历。"""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
            if total > limit:
                return total
    return total


def resolve_tool_paths(tool_name: str, arguments: dict) -> list[tuple[str, str]]:
    """按工具同一口径把入参解析成绝对路径，返回 [(abs_path, kind)]（保序去重）。

    kind ∈ {"file", "dir"}。路径非法/越界时跳过：工具自己会拒绝并返回结构化错误，
    这里不需要重复报错，也不该让快照 hook 阻断工具链。
    """
    from security.sandbox import get_shared_workspace
    from tools.builtin.system_tool_file_ops import resolve_shared_path

    keys = MUTATING_TOOL_PATHS.get(tool_name) or DIR_TOOL_PATHS.get(tool_name) or ()
    if not keys:
        return []
    is_dir_tool = tool_name in DIR_TOOL_PATHS
    base = get_shared_workspace()
    found: dict[str, str] = {}
    for key in keys:
        raw = str(arguments.get(key) or "").strip()
        if not raw:
            continue
        try:
            abs_path = str(resolve_shared_path(raw, base=base))
        except (ValueError, OSError):
            # 回退：越界路径交给工具层拒绝；这里跳过，不写快照
            continue
        if abs_path not in found:
            found[abs_path] = "dir" if is_dir_tool else "file"
    return list(found.items())


def normalize_entries(raw) -> list[dict]:
    """兼容两种落库格式：新格式（对象数组）与旧格式（纯路径字符串数组）。"""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    entries: list[dict] = []
    for item in data:
        if isinstance(item, str):
            entries.append({"path": item, "kind": "file", "existed": True})
        elif isinstance(item, dict) and item.get("path"):
            entries.append({
                "path": str(item["path"]),
                "kind": "dir" if item.get("kind") == "dir" else "file",
                "existed": bool(item.get("existed", True)),
            })
    return entries


def _remove_path(path: Path) -> bool:
    """删除文件或目录（不存在返回 False）。"""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
        return True
    if path.exists() or path.is_symlink():
        path.unlink()
        return True
    return False


def _remove_empty_dir(path: Path) -> bool:
    """只删空目录（非空/占用/失败返回 False）——用户新放进来的文件不陪葬。"""
    try:
        if not path.is_dir() or any(path.iterdir()):
            return False
        path.rmdir()
        return True
    except OSError:
        return False  # 回退：删除失败由调用方记入 skipped，不静默吞掉结果


def _restore_copy(src: Path, target: Path, kind: str) -> None:
    """原子恢复：先写到同目录临时项，再 os.replace 换入，避免半写。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f"{target.name}.{uuid.uuid4().hex[:8]}.restore")
    if kind == "dir":
        shutil.copytree(src, temp)
        if target.exists():
            _remove_path(target)  # Windows 上目录不能被 os.replace 覆盖
        os.replace(temp, target)
        return
    if target.is_dir():
        _remove_path(target)
    shutil.copy2(src, temp)
    os.replace(temp, target)


class SnapshotService:
    """文件级快照安全网：操作前后各备份一次，支持按 task 回退。"""

    def __init__(self, db):
        self.db = db
        self._snap_dir: Path | None = None
        self._cleaned = False

    @property
    def snap_dir(self) -> Path:
        if self._snap_dir is None:
            d = snapshot_dir()
            d.mkdir(parents=True, exist_ok=True)
            self._snap_dir = d
        return self._snap_dir

    # ============================================================
    # 快照
    # ============================================================

    async def pre_tool(
        self, tool_name: str, arguments: dict, task_id: str, turn_seq: int = 0,
        agent_source: str = "",
    ) -> str | None:
        """工具执行前备份受影响路径 → 返回 snapshot_id；无可追踪路径返回 None。"""
        await self._cleanup_once()
        entries = self._collect(tool_name, arguments)
        if not entries:
            return None
        sid = str(uuid.uuid4())
        backed = self._backup(entries, self.snap_dir / sid)
        await self._insert(sid, task_id, turn_seq, "pre", tool_name, None, backed)
        return sid

    async def post_tool(
        self, tool_name: str, arguments: dict, result: dict,
        task_id: str, turn_seq: int = 0, pre_snapshot_id: str | None = None,
    ) -> str | None:
        """工具执行后备份受影响路径 → 返回 snapshot_id（供交付物比对行数）。"""
        entries = self._collect(tool_name, arguments)
        if not entries:
            return None
        sid = str(uuid.uuid4())
        backed = self._backup(entries, self.snap_dir / sid)
        await self._insert(
            sid, task_id, turn_seq, "post", tool_name, pre_snapshot_id, backed)
        return sid

    def _collect(self, tool_name: str, arguments: dict) -> list[dict]:
        """按**当前**磁盘状态生成条目 [{path, kind, existed}]。"""
        entries: list[dict] = []
        for abs_path, kind in resolve_tool_paths(tool_name, arguments):
            path = Path(abs_path)
            # 目录工具即使目标尚未创建也保持 dir（回调时要按目录语义保守处理）
            real_kind = "dir" if kind == "dir" or path.is_dir() else "file"
            entries.append({
                "path": abs_path,
                "kind": real_kind,
                "existed": path.exists(),
            })
        return entries

    def _backup(self, entries: list[dict], turn_dir: Path) -> list[dict]:
        """备份存在的条目；超过上限或复制失败的条目保留并显式标注。"""
        from settings import settings as _settings

        file_cap = max(0, int(_settings.snapshot_max_file_bytes))
        dir_cap = max(0, int(_settings.snapshot_max_dir_bytes))
        turn_dir.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            path = Path(entry["path"])
            if not entry["existed"] or not path.exists():
                continue
            dest = turn_dir / snapshot_entry_name(str(path))
            try:
                if entry["kind"] == "dir":
                    if _tree_size_within(path, dir_cap) > dir_cap:
                        entry["size_limited"] = True
                    else:
                        shutil.copytree(path, dest)
                elif path.stat().st_size > file_cap:
                    entry["size_limited"] = True
                else:
                    shutil.copy2(path, dest)
            except OSError as exc:
                _log.warning("snapshot: backup failed for %s: %s", path, exc)
                entry["backup_error"] = True
        self._prune_empty(turn_dir)
        return entries

    @staticmethod
    def _prune_empty(turn_dir: Path) -> None:
        """没有真正复制到任何内容时不留空目录。"""
        try:
            if not any(turn_dir.iterdir()):
                turn_dir.rmdir()
        except OSError:
            return  # 回退：目录已不存在/被占用，保持现状即可

    async def _insert(
        self, sid: str, task_id: str, turn_seq: int, phase: str,
        tool_name: str, pre_id: str | None, entries: list[dict],
    ) -> None:
        await self.db.execute(
            "INSERT INTO snapshots(id, task_id, turn_seq, phase, tool_name, "
            "pre_snapshot_id, files_affected, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (sid, task_id, turn_seq, phase, tool_name, pre_id,
             json.dumps(entries, ensure_ascii=False), int(time.time())),
        )
        await self.db.commit()

    async def _cleanup_once(self) -> None:
        """每个进程第一次快照时清一次过期快照（防止目录无限增长）。"""
        if self._cleaned:
            return
        self._cleaned = True
        from settings import settings as _settings
        cutoff = time.time() - max(1, int(_settings.snapshot_retention_days)) * 86400
        try:
            for child in self.snap_dir.iterdir():
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
            await self.db.execute("DELETE FROM snapshots WHERE created_at < ?", (int(cutoff),))
            await self.db.commit()
        except OSError as exc:
            _log.warning("snapshot: retention cleanup failed: %s", exc)

    # ============================================================
    # 回退
    # ============================================================

    async def restore_turn(self, task_id: str) -> dict:
        """按 task 回退到第一次改动之前（每个路径只认最早的 pre 快照）。"""
        completed_at, guard_error = await self._undo_guard(task_id)
        if guard_error:
            return {"status": "error", "message": guard_error}
        cursor = await self.db.execute(
            "SELECT id, files_affected FROM snapshots WHERE task_id=? AND phase='pre' "
            "ORDER BY created_at ASC, rowid ASC", (task_id,))
        rows = await cursor.fetchall()
        if not rows:
            return {"status": "error", "message": "没有找到快照"}

        earliest: dict[str, tuple[str, dict]] = {}
        for sid, raw in rows:
            for entry in normalize_entries(raw):
                earliest.setdefault(entry["path"], (sid, entry))

        restored, removed, skipped = self._apply_restore(earliest, completed_at)

        await self._drop_task(task_id)
        return {
            "status": "ok" if not skipped else "partial",
            "restored_files": restored,
            "removed_new_files": removed,
            "skipped": skipped,
            "snapshots": len(rows),
        }

    async def _undo_guard(self, task_id: str) -> tuple[float | None, str | None]:
        """撤销前置检查：运行中的任务拒绝撤销；返回完成时间供 mtime 保护使用。"""
        cursor = await self.db.execute(
            "SELECT status, completed_at FROM tasks WHERE id=?", (task_id,))
        row = await cursor.fetchone()
        if not row:
            return None, None  # 没有任务行（测试/历史数据）：不加额外限制
        status, completed_at = row[0], row[1]
        if status == "running":
            return None, "任务正在运行——先停止任务再撤销"
        return (float(completed_at) if completed_at else None), None

    def _apply_restore(self, earliest: dict, completed_at: float | None = None) -> tuple[int, int, list[str]]:
        """恢复旧内容 → 删任务新建的文件 → 删任务新建且**已空**的目录。"""
        ws_root = Path(get_workspace()).resolve()
        restored = removed = 0
        skipped: list[str] = []
        new_dirs: list[str] = []
        for path, (sid, entry) in earliest.items():
            target = Path(path)
            if self._changed_after_task(target, completed_at):
                # 任务结束后又被改过（用户/其他任务）——撤销会覆盖新改动，跳过
                _log.warning("snapshot: %s changed after task end, skip undo", path)
                skipped.append(path)
                continue
            # fail-closed：只回退工作区内的路径（防 DB 被篡改后写出边界）
            if not target.resolve().is_relative_to(ws_root):
                _log.warning("snapshot: refuse to restore outside workspace: %s", path)
                skipped.append(path)
            elif entry.get("existed"):
                if self._restore_one(path, sid, entry):
                    restored += 1
                else:
                    skipped.append(path)
            elif entry.get("kind") == "dir":
                new_dirs.append(path)
            else:
                removed += self._delete_new(path, skipped)
        # 目录深的先处理；只删空目录——任务期间用户放进去的文件不陪葬
        for path in sorted(new_dirs, key=lambda p: len(Path(p).parts), reverse=True):
            target = Path(path)
            if not target.exists():
                continue
            if _remove_empty_dir(target):
                removed += 1
            else:
                skipped.append(path)
        return restored, removed, skipped

    @staticmethod
    def _changed_after_task(target: Path, completed_at: float | None) -> bool:
        """文件在任务完成之后又被修改过？（2 秒时钟余量，降低误判）"""
        if not completed_at or not target.exists():
            return False
        try:
            return target.stat().st_mtime > completed_at + 2
        except OSError:
            return False  # 回退：读不到 mtime 时不阻断撤销（由备份缺失另行报告）

    def _restore_one(self, path: str, sid: str, entry: dict) -> bool:
        """把某路径恢复到 pre 备份内容；备份缺失/写入失败返回 False。"""
        src = self.snap_dir / sid / snapshot_entry_name(path)
        if not src.exists():
            return False
        try:
            _restore_copy(src, Path(path), entry.get("kind", "file"))
            return True
        except OSError as exc:
            _log.warning("snapshot: restore failed for %s: %s", path, exc)
            return False

    @staticmethod
    def _delete_new(path: str, skipped: list[str]) -> int:
        """删除任务新建的文件；失败记日志并进 skipped。"""
        try:
            return 1 if _remove_path(Path(path)) else 0
        except OSError as exc:
            _log.warning("snapshot: remove-new-file failed for %s: %s", path, exc)
            skipped.append(path)
            return 0

    async def _drop_task(self, task_id: str) -> None:
        cursor = await self.db.execute(
            "SELECT id FROM snapshots WHERE task_id=?", (task_id,))
        for (sid,) in await cursor.fetchall():
            shutil.rmtree(self.snap_dir / sid, ignore_errors=True)
        await self.db.execute("DELETE FROM snapshots WHERE task_id=?", (task_id,))
        await self.db.commit()

    async def list_snapshots(self, task_id: str) -> list[dict]:
        """列出某 task 的快照时间线（files_affected 已归一为对象数组）。"""
        cursor = await self.db.execute(
            "SELECT id, turn_seq, phase, tool_name, files_affected, created_at "
            "FROM snapshots WHERE task_id=? ORDER BY created_at ASC, rowid ASC",
            (task_id,),
        )
        return [
            {
                "id": r[0], "turn_seq": r[1], "phase": r[2],
                "tool_name": r[3], "files_affected": normalize_entries(r[4]),
                "created_at": r[5],
            }
            for r in await cursor.fetchall()
        ]


# ============================================================
# Harness 录制/回放
# ============================================================

class HarnessRecorder:
    """Ore Code 风格录制回放测试。"""

    def __init__(self, db):
        self.db = db
        self._enabled = os.environ.get("YUNCAT_HARNESS_RECORD") == "1"

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def record_turn(self, task_id: str, turn_data: dict) -> str | None:
        """录制一个完整的 Agent turn（prompt → tool_calls → responses）。"""
        if not self._enabled:
            return None
        rid = str(uuid.uuid4())
        await self.db.execute(
            "INSERT INTO harness_recordings(id, task_id, turn_data, created_at) "
            "VALUES(?,?,?,?)",
            (rid, task_id, json.dumps(turn_data, ensure_ascii=False), int(time.time())),
        )
        await self.db.commit()
        return rid

    async def list_recordings(self, limit: int = 20) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT id, task_id, created_at FROM harness_recordings "
            "ORDER BY created_at DESC LIMIT ?", (limit,),
        )
        return [
            {"id": r[0], "task_id": r[1], "created_at": r[2]}
            for r in await cursor.fetchall()
        ]

    async def get_recording(self, rid: str) -> dict | None:
        cursor = await self.db.execute(
            "SELECT id, task_id, turn_data, created_at FROM harness_recordings WHERE id=?",
            (rid,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "task_id": row[1],
            "turn_data": json.loads(row[2]) if isinstance(row[2], str) else row[2],
            "created_at": row[3],
        }

    async def replay(self, rid: str) -> dict:
        """回放——读取录制 → 用相同 args 调真实 tool → diff report。"""
        recording = await self.get_recording(rid)
        if not recording:
            return {"status": "error", "message": "Recording not found"}

        turn = recording["turn_data"]
        tool_name = turn.get("tool_name", "unknown")
        args = {k: v for k, v in turn.get("arguments", {}).items()
                 if not k.startswith("_")}  # 去除 _task_id 等内部参数
        original_result = turn.get("result", {})

        from tools.registry import get_all_tools
        tools = get_all_tools()
        tool = tools.get(tool_name)
        if not tool:
            return {"status": "error", "message": f"Tool {tool_name} not found"}

        try:
            replayed_result = await tool.func(**args)
        except Exception as e:
            return {"status": "error", "message": str(e), "original": original_result}

        diffs = []
        if original_result.get("success") != replayed_result.get("success"):
            diffs.append(f"success: {original_result.get('success')} → {replayed_result.get('success')}")
        if original_result.get("data") != replayed_result.get("data"):
            diffs.append("data changed")

        return {
            "status": "ok",
            "match": len(diffs) == 0,
            "diffs": diffs,
            "original": original_result,
            "replayed": replayed_result,
        }
