"""harness/scheduler.py —— write_paths 冲突检测 + 依赖 DAG（Phase D）

设计要点（docs/phase-d-scheduler-isolation.md §2）：
  - 冲突两类：写-写（硬冲突：路径相等或祖先-后代）+ 读-写（软依赖：B 读 A 写 → B 依赖 A）。
  - 路径解析复用 security.sandbox.canonicalize（跟随 symlink、拒绝越界）——与 Phase B 安全基座一致；
    声明路径越界视为声明错误，跳过并告警（不阻塞调度）。
  - 占用集合：仅 RUNNING / WAITING_PERMISSION 持有写入权；终态自动释放。
  - 全部纯函数（不依赖 DB / IO）——can_run 供 task_manager.acquire_task 领取门调用；
    build_dependency_graph 供调度提示/审计（本期不做自动推模型）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from harness.schemas import TaskPackage
from harness.task_manager import (
    TaskStatus,
    _row_to_task,
    get_task,
    record_attempt,
    transition,
)
from security.sandbox import canonicalize

_log = logging.getLogger("yuncat.harness.scheduler")

# 持有写入权的状态——终态/可重试态不占用（冲突自动解除）
_ACTIVE_STATUSES = frozenset({TaskStatus.RUNNING, TaskStatus.WAITING_PERMISSION})

KIND_WRITE_WRITE = "write_write"
KIND_READ_WRITE = "read_write"


def normalize_paths(paths: Iterable[str], workspace: Path) -> list[Path]:
    """声明路径（相对 workspace）→ 解析后的绝对路径列表。

    越界（canonicalize 抛 PermissionError）或解析失败 → 跳过并告警——
    声明错误不阻塞调度（fail-open 于路径解析，fail-closed 于后续冲突判定）。
    """
    workspace = Path(workspace).resolve()
    out: list[Path] = []
    for rel in paths or []:
        rel = str(rel).strip()
        if not rel:
            continue
        try:
            out.append(canonicalize(rel, root=workspace, boundary=workspace))
        except (PermissionError, ValueError) as e:
            _log.warning("write_paths 声明越界/非法，已跳过: %r (%s)", rel, e)
    return out


def write_conflict(a: Path, b: Path) -> bool:
    """两路径是否冲突：相等，或一方是另一方的祖先目录（写 dir 与写 dir/file 冲突）。"""
    return a == b or a in b.parents or b in a.parents


def _task_paths(task: TaskPackage, key: str) -> list[Path]:
    perms = task.permissions
    return list(getattr(perms, key, None) or [])


def find_conflicts(
    candidate: TaskPackage,
    active: list[TaskPackage],
    workspace: Path,
) -> list[dict]:
    """candidate 与 active 中 RUNNING/WAITING_PERMISSION 任务的冲突明细。

    Returns: [{other_task_id, kind: write_write|read_write, paths: [相对 workspace 的命中路径...]}]
    """
    workspace = Path(workspace).resolve()
    cand_write = normalize_paths(_task_paths(candidate, "write_paths"), workspace)
    cand_read = normalize_paths(_task_paths(candidate, "read_paths"), workspace)
    conflicts: list[dict] = []
    for other in active or []:
        if getattr(other, "status", "") not in _ACTIVE_STATUSES:
            continue  # 终态/可重试任务不占用写入权
        other_write = normalize_paths(_task_paths(other, "write_paths"), workspace)
        # 写-写：候选 write ∩ 占用 write
        ww = sorted({
            _rel(a, workspace)
            for a in cand_write for b in other_write if write_conflict(a, b)
        })
        if ww:
            conflicts.append({
                "other_task_id": other.id, "kind": KIND_WRITE_WRITE, "paths": ww,
            })
        # 读-写：候选 read ∩ 占用 write（B 读 A 写 → 软依赖，等 A 终态）
        rw = sorted({
            _rel(a, workspace)
            for a in cand_read for b in other_write if write_conflict(a, b)
        })
        if rw:
            conflicts.append({
                "other_task_id": other.id, "kind": KIND_READ_WRITE, "paths": rw,
            })
    return conflicts


def can_run(
    candidate: TaskPackage,
    active: list[TaskPackage],
    workspace: Path,
) -> tuple[bool, list[dict]]:
    """领取门：无冲突 → (True, [])；有冲突 → (False, conflicts)。"""
    conflicts = find_conflicts(candidate, active, workspace)
    return (len(conflicts) == 0, conflicts)


def build_dependency_graph(
    tasks: list[TaskPackage],
    workspace: Path,
) -> dict[str, set[str]]:
    """read∩write 推导依赖 DAG：{task_id: {被依赖的 task_id...}}。

    B 的 read_paths ∩ A 的 write_paths ≠ ∅ → B 依赖 A（A 未终态 B 不放行），
    边记作 dep[B_id].add(A_id)。对称性检查：依赖环（A→B 且 B→A）不在此函数处理
    （环检测由调用方决定是否拒绝，本期仅推导）。
    """
    workspace = Path(workspace).resolve()
    dep: dict[str, set[str]] = {t.id: set() for t in tasks or []}
    writes: dict[str, list[Path]] = {
        t.id: normalize_paths(_task_paths(t, "write_paths"), workspace)
        for t in tasks or []
    }
    for t in tasks or []:
        reads = normalize_paths(_task_paths(t, "read_paths"), workspace)
        for other in tasks or []:
            if other.id == t.id:
                continue
            if any(write_conflict(r, w) for r in reads for w in writes.get(other.id, [])):
                dep[t.id].add(other.id)  # t 依赖 other（other 写、t 读）
    return dep


def _rel(path: Path, workspace: Path) -> str:
    """绝对路径 → 相对 workspace 的 posix 串（冲突明细可读性）。"""
    try:
        return path.relative_to(workspace).as_posix()
    except ValueError:
        return str(path)


# ============================================================
# 领取门（拉模型）：QUEUED → RUNNING 唯一入口
# ============================================================
def _to_package(row: dict) -> TaskPackage:
    """DB 行（task_manager._row_to_task 产物）→ TaskPackage（Pydantic 自动转换嵌套 dict）。"""
    return TaskPackage(**{k: v for k, v in row.items() if k in TaskPackage.model_fields})


async def _load_active_tasks(conn, workspace: Path) -> list[TaskPackage]:
    """默认占用集加载：RUNNING/WAITING_PERMISSION 任务（终态/可重试不占用）。"""
    cursor = await conn.execute(
        "SELECT * FROM harness_tasks WHERE status IN (?, ?)",
        (TaskStatus.RUNNING, TaskStatus.WAITING_PERMISSION),
    )
    rows = await cursor.fetchall()
    return [_to_package(_row_to_task(r)) for r in rows]


async def acquire_task(
    conn,
    task_id: str,
    *,
    workspace: Path,
    active_loader=None,
) -> dict:
    """QUEUED → RUNNING 领取门——所有领取路径的唯一入口。

    1. 校验当前状态 == QUEUED（非法状态拒绝，fail-closed）；
    2. 加载占用集（RUNNING/WAITING_PERMISSION）→ can_run 冲突检查；
    3. 冲突 → {acquired: False, conflicts}（保持 QUEUED，不落审计——未发生迁移）；
    4. 无冲突 → transition(RUNNING) + record_attempt。

    active_loader 可注入（测试传内存列表 loader；生产默认 DB 查询）。
    """
    task = await get_task(conn, task_id)
    if task is None:
        return {"acquired": False, "error": f"任务不存在: {task_id}"}
    if task["status"] != TaskStatus.QUEUED:
        return {"acquired": False, "error": f"仅 QUEUED 可领取，当前: {task['status']}"}
    candidate = _to_package(task)
    if active_loader is None:
        active_loader = _load_active_tasks
    active = await active_loader(conn, Path(workspace))
    ok, conflicts = can_run(candidate, active, Path(workspace))
    if not ok:
        return {"acquired": False, "conflicts": conflicts, "task_id": task_id}
    task = await transition(conn, task_id, TaskStatus.RUNNING, "worker acquired task")
    attempt_id = await record_attempt(conn, task_id, task["attempt"], task.get("worker_agent_id") or "")
    return {"acquired": True, "task": task, "attempt_id": attempt_id}
