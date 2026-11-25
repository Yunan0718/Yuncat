"""harness/runtime.py —— 进程终止统一接口 + 日志截断 + 崩溃恢复扫描（Phase C）

设计要点（docs/budget-reliability-design.md §5）：
  - kill_process_tree：统一"进程组杀"入口，按平台分发——
      POSIX: start_new_session 保证 pid==pgid，killpg(SIGTERM) → grace → killpg(SIGKILL)
      Windows: 优先 Job Object terminate_job（OS 级整组杀，含孙进程）；
               无 hjob 时降级 os.kill 主进程（与 adapters/base.py 既有 terminate/kill 同语义）
    幂等：进程已退出（ProcessLookupError）→ no-op。
  - truncate_log：日志超限保留尾部（最新状态在尾部），防 Worker 日志无限膨胀。
  - scan_stale_running_tasks：崩溃恢复——RUNNING/WAITING_PERMISSION 且 updated_at
    超时（无心跳）→ 重置 TIMEOUT + 通知。由 lifespan_services 的 60s 周期循环驱动。

注意：本模块不依赖 adapters/event_bus——notifier 由调用方（lifespan）注入，
保持可测性与依赖方向（harness 不反向依赖 service 层）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time as _time
from pathlib import Path

_log = logging.getLogger("yuncat.harness.runtime")

LOG_MAX_BYTES = 10 * 1024 * 1024  # 10MB 日志截断阈值
KILL_GRACE_SEC = 5.0  # SIGTERM → SIGKILL 宽限
# Windows 的 signal 模块无 SIGKILL 常量——用 POSIX 标准值 9 兜底（保证导入/测试安全）
_SIGKILL = getattr(signal, "SIGKILL", 9)


# ============================================================
# 进程组杀统一接口
# ============================================================
async def kill_process_tree(
    pid: int,
    reason: str = "",
    *,
    grace_sec: float = KILL_GRACE_SEC,
    hjob: int | None = None,
) -> None:
    """终止 pid 所在进程组（整棵树）。幂等：进程已退出 → no-op。

    POSIX: pid 必须是进程组 id（start_new_session=True 启动时 pid==pgid）。
    Windows: hjob 提供时走 TerminateJobObject（整组强杀）；否则降级
    os.kill 主进程 + grace + kill（与 adapter 既有语义一致，子进程由调用方负责）。
    """
    if pid <= 0:
        return
    if sys.platform == "win32":
        await _kill_windows(pid, reason, grace_sec=grace_sec, hjob=hjob)
    else:
        await _kill_posix(pid, reason, grace_sec=grace_sec)


async def _kill_posix(pid: int, reason: str, *, grace_sec: float) -> None:
    if reason:
        _log.warning("killpg(pid=%s, SIGTERM) reason=%s", pid, reason)
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return  # 已退出——幂等
    if grace_sec > 0:
        await asyncio.sleep(grace_sec)
    try:
        os.killpg(pid, _SIGKILL)
    except ProcessLookupError:
        pass


async def _kill_windows(pid: int, reason: str, *, grace_sec: float, hjob: int | None) -> None:
    # 优先 Job Object 整组杀（含孙进程）——OS 级保证
    if hjob:
        try:
            from security.job_object import terminate_job

            if terminate_job(hjob):
                _log.warning("TerminateJobObject(hjob=%s) reason=%s", hjob, reason)
                return
        except Exception:
            _log.warning("terminate_job failed, fallback to os.kill", exc_info=True)
    # 降级：主进程强杀。Windows 无 POSIX 信号——os.kill(pid, SIGTERM) 即
    # TerminateProcess（强制终止，等价 SIGKILL），且 signal 模块无 SIGKILL 常量，
    # 故不二段重试（TerminateProcess 是同步请求，无需宽限）。
    if reason:
        _log.warning("kill pid=%s reason=%s", pid, reason)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return


# ============================================================
# 日志截断（保尾）
# ============================================================
def truncate_log(path: Path, max_bytes: int = LOG_MAX_BYTES) -> bool:
    """日志文件超限时保留尾部 max_bytes（最新状态在尾部）。未超限/不存在 no-op。

    Returns: 是否发生截断。
    """
    if max_bytes <= 0:
        return False
    try:
        if not path.exists() or not path.is_file():
            return False
        size = path.stat().st_size
        if size <= max_bytes:
            return False
        with path.open("rb") as f:
            f.seek(size - max_bytes)
            tail = f.read()
        path.write_bytes(tail)
        _log.warning("truncated log %s: %d -> %d bytes", path, size, max_bytes)
        return True
    except OSError as e:
        _log.warning("truncate_log failed for %s: %s", path, e)
        return False


# ============================================================
# 崩溃恢复扫描
# ============================================================
async def scan_stale_running_tasks(
    conn,
    *,
    stale_sec: int = 300,
    notifier=None,
) -> list[dict]:
    """重启后扫描残留：RUNNING/WAITING_PERMISSION 且 updated_at 超时 → 重置 TIMEOUT。

    语义：
      - updated_at 为 NULL（未经历 transition）视为新鲜，不重置——保守防误杀；
      - 每条重置走 task_manager.transition（先 validate 后落库 + state_transitions 审计）；
      - notifier 提供 publish(evt, payload) 时发 task_reset 事件（lifespan 注入 event_bus）。

    Returns: 被重置任务列表 [{task_id, from_status, to_status}]。
    """
    from harness.task_manager import TaskStatus, transition

    cutoff = int(_time.time()) - max(1, int(stale_sec))
    cursor = await conn.execute(
        "SELECT id, status FROM harness_tasks "
        "WHERE status IN (?, ?) AND updated_at IS NOT NULL AND updated_at < ?",
        (TaskStatus.RUNNING, TaskStatus.WAITING_PERMISSION, cutoff),
    )
    rows = await cursor.fetchall()
    reset: list[dict] = []
    for row in rows:
        task_id, from_status = row["id"], row["status"]
        try:
            task = await transition(
                conn, task_id, TaskStatus.TIMEOUT,
                f"crash recovery: stale {stale_sec}s without heartbeat",
            )
        except Exception as e:
            _log.warning("scan reset failed for %s: %s", task_id, e)
            continue
        item = {"task_id": task_id, "from_status": from_status, "to_status": TaskStatus.TIMEOUT}
        reset.append(item)
        if notifier is not None:
            try:
                await notifier.publish("task_reset", {
                    "task_id": task_id,
                    "from_status": from_status,
                    "to_status": TaskStatus.TIMEOUT,
                    "reason": "crash recovery",
                })
            except Exception:
                _log.debug("task_reset notify failed for %s", task_id, exc_info=True)
        _log.warning("crash recovery: %s %s -> %s", task_id, from_status, TaskStatus.TIMEOUT)
    return reset
