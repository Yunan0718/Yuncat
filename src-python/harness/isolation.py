"""harness/isolation.py —— 工作区浅拷贝隔离（Phase D）

设计要点（docs/phase-d-scheduler-isolation.md §3）：
  - 副本布局：workspace/.harness/{task_id}-{attempt_id}/（对 Worker 隐藏，不在 write_paths 内）。
  - 复制范围：write_paths ∪ read_paths 声明路径（文件→复制文件；目录→整棵子树，保留相对结构）；
    声明整库（"." 或 workspace 本身）时全量复制，排除内部目录（.harness/.git/.pending/.backup/agents）。
  - 合并策略：仅 COMPLETED 走 merge_back，且只合并 write_paths 范围内变更（新增/修改/删除同步）；
    read_paths 只读不回流。合并保护：目标文件 hash 与复制前快照一致才覆盖——外部修改冲突时拒绝
    合并（不静默覆盖）转人工。
  - 副本不含 .git——Phase B verifier 的 git 路径对副本 fail-closed，回退文件 hash 快照取证。
  - 接口抽象 WorkspaceIsolation——V2 换 git worktree 实现不破坏调用方。
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path

from harness.schemas import TaskPackage
from harness.scheduler import normalize_paths

_log = logging.getLogger("yuncat.harness.isolation")

# 全量复制时排除的内部目录（与 get_workspace 的共享/代理目录一致）
_INTERNAL_DIRS = {".harness", ".git", ".pending", ".backup", "agents", "shared"}

_SNAPSHOT_NAME = ".harness-snapshot.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class WorkspaceIsolation:
    """浅拷贝隔离。create →（Worker 在副本执行，Verifier 对副本取证）→ merge_back / cleanup。"""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self._harness_dir = self.workspace / ".harness"

    # ── 路径 ──────────────────────────────────────────────────
    def copy_root(self, task_id: str, attempt_id: str) -> Path:
        return self._harness_dir / f"{task_id}-{attempt_id}"

    # ── create ────────────────────────────────────────────────
    def create(self, pkg: TaskPackage, attempt_id: str) -> Path:
        """复制 write_paths ∪ read_paths 到副本，记录 write 范围快照。返回副本根。"""
        copy_root = self.copy_root(pkg.id, attempt_id)
        if copy_root.exists():
            shutil.rmtree(copy_root)  # 幂等：重复 create 先清空
        write = normalize_paths(pkg.permissions.write_paths, self.workspace)
        read = normalize_paths(pkg.permissions.read_paths, self.workspace)
        sources = write + read
        whole = (not sources) or any(s == self.workspace for s in sources)  # 未声明/声明整库
        if whole:
            sources = [self.workspace]
        for src in sources:
            self._copy_one(src, copy_root, whole_root=whole)
        self._write_snapshot(copy_root, write if not whole else [self.workspace])
        _log.info("isolation create: %s -> %s (sources=%d)", pkg.id, copy_root, len(sources))
        return copy_root

    def _copy_one(self, src: Path, copy_root: Path, *, whole_root: bool) -> None:
        rel = src.relative_to(self.workspace)
        dst = copy_root / rel
        if src.is_dir():
            shutil.copytree(
                src, dst,
                ignore=_ignore_internal_factory(self.workspace) if whole_root else None,
                dirs_exist_ok=True,
            )
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        else:
            _log.warning("声明路径不存在，跳过: %s", src)

    def _write_snapshot(self, copy_root: Path, write_paths: list[Path]) -> None:
        """快照 write 范围内主工作区原始文件 {rel: sha256}——merge 时防外部修改覆盖。"""
        snap: dict[str, str] = {}
        for root in write_paths:
            for f in self._iter_files(root):
                snap[f.relative_to(self.workspace).as_posix()] = _sha256(f)
        (copy_root / _SNAPSHOT_NAME).write_text(json.dumps(snap, sort_keys=True), encoding="utf-8")

    # ── merge_back（仅 COMPLETED）─────────────────────────────
    def merge_back(self, pkg: TaskPackage, attempt_id: str) -> dict:
        """将副本 write 范围内变更同步回主工作区（新增/修改/删除）。

        保护：目标文件 hash ≠ 快照 hash（外部修改）→ 冲突跳过不覆盖，转人工。
        Returns: {"merged": [...], "deleted": [...], "conflicts": [...]}
        """
        copy_root = self.copy_root(pkg.id, attempt_id)
        empty = {"merged": [], "deleted": [], "conflicts": []}
        if not copy_root.exists():
            return empty
        snap_path = copy_root / _SNAPSHOT_NAME
        snap = json.loads(snap_path.read_text(encoding="utf-8")) if snap_path.exists() else {}
        write = normalize_paths(pkg.permissions.write_paths, self.workspace)
        if (not write) or any(w == self.workspace for w in write):
            write = [self.workspace]
        merged: list[str] = []
        deleted: list[str] = []
        conflicts: list[str] = []
        for src in copy_root.rglob("*"):
            if not src.is_file() or src.name == _SNAPSHOT_NAME:
                continue
            rel = src.relative_to(copy_root).as_posix()
            if not self._in_scope(rel, write):
                continue  # read 范围只读不合并
            dst = self.workspace / rel
            if dst.exists() and snap.get(rel) is not None and _sha256(dst) != snap[rel]:
                conflicts.append(rel)
                continue  # 外部修改——拒绝覆盖
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            merged.append(rel)
        # 删除同步：快照有、副本无、主工作区未被外部修改 → 删除
        for rel, h in snap.items():
            if (copy_root / rel).exists():
                continue
            dst = self.workspace / rel
            if dst.exists() and _sha256(dst) == h:
                dst.unlink()
                deleted.append(rel)
            elif dst.exists():
                conflicts.append(rel)
        _log.info("isolation merge_back: %s merged=%d deleted=%d conflicts=%d",
                  pkg.id, len(merged), len(deleted), len(conflicts))
        return {"merged": merged, "deleted": deleted, "conflicts": conflicts}

    def _in_scope(self, rel: str, write_paths: list[Path]) -> bool:
        p = self.workspace / rel
        return any(p == w or p.is_relative_to(w) for w in write_paths)

    # ── cleanup（非 COMPLETED 丢弃副本）───────────────────────
    def cleanup(self, task_id: str, attempt_id: str) -> bool:
        copy_root = self.copy_root(task_id, attempt_id)
        if copy_root.exists():
            shutil.rmtree(copy_root, ignore_errors=True)
            _log.info("isolation cleanup: %s", copy_root)
            return True
        return False

    # ── 内部工具 ──────────────────────────────────────────────
    def _iter_files(self, root: Path):
        if root.is_file():
            yield root
        elif root.is_dir():
            for f in sorted(root.rglob("*")):
                if f.is_file() and f.name != _SNAPSHOT_NAME:
                    yield f


def _ignore_internal_factory(workspace: Path):
    """copytree ignore 回调：仅在复制 workspace 根时排除内部目录（子目录正常复制）。"""
    ws = workspace.resolve()

    def _ignore(dirname: str, names: list[str]) -> set[str]:
        if Path(dirname).resolve() == ws:
            return {n for n in names if n in _INTERNAL_DIRS}
        return set()

    return _ignore
