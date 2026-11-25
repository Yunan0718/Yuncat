"""services/shell_changes.py —— shell 命令改动的文件（对标 DSH workspace-changes）

背景（DSH 对照表里一直标着「未做」的那一项）：
DSH 会汇总「这一轮改动了哪些文件」——文件工具改的用整文件拷贝兜底，
shell 改的用 git 工作树快照（轮次边界比较前后两棵树）得到。
Yuncat 的快照服务覆盖了「文件工具改过的文件」（services/snapshot_service.py），
但 **shell 命令改的文件完全不可见**——交付物面板只列工具层改动，
shell 拉下来的依赖、脚本生成的产物、`git checkout` 换掉的文件都不在里面。

本模块补上 shell 这一半：命令执行前后各取一次 git 脏文件集合，比差集。

为什么不用 DSH 的临时 index + write-tree：
- DSH 要把工作树内容写进会话私有的临时对象库，才能拿到行数与逐文件 diff；
  代价是磁盘占用 + 一次全量 add（仓库越大越贵）。
- Yuncat 侧缺的只是「哪些文件被 shell 改了」这一层事实；行数与逐文件 diff 由
  已有快照口径负责（工具层改动有精确行数）。所以用 `git status --porcelain`
  取脏文件集合就够：只 stat，不写对象、不碰仓库的 index/对象库/ref。

已知边界（如实写明，不假装覆盖）：
1. .gitignore 命中的文件 git 不报 → shell 改了它们也看不到；
2. 命令执行前就已经是脏的文件，命令又改了它 → 不重复计入（内容变化看不见）；
3. 不是 git 仓库 / git 不可用 → 明确返回「不可用」，不猜、不假装没变化。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time as _time
from pathlib import Path

_log = logging.getLogger("yuncat.shell_changes")

# 单条 git 命令超时——卡住的 git 不能把 agent 循环拖住
GIT_TIMEOUT_SEC = 10.0
# 同时跟踪的任务数上限（每个任务一份基线，状态很小，但要有护栏）
_MAX_TRACKED_TASKS = 64
# 基线保留时长：任务结束后一段时间内的命令仍可结算
_BASELINE_TTL_SEC = 3600.0


class _Baseline:
    """某任务当前的工作区脏文件基线（仓库相对路径）。"""

    __slots__ = ("paths", "ts", "repo_root", "available")

    def __init__(self, paths: set[str], repo_root: str, available: bool) -> None:
        self.paths = paths
        self.repo_root = repo_root
        self.available = available
        self.ts = _time.time()


# task_id → _Baseline
_BASELINES: dict[str, _Baseline] = {}


def _git_env() -> dict[str, str]:
    """git 子进程环境——复用命令工具的环境过滤，别把 token 传给 git。

    过滤逻辑只写一处（tools/builtin/system_tool_command._build_filtered_env）。
    """
    from tools.builtin.system_tool_command import _build_filtered_env

    env = _build_filtered_env()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GIT_OPTIONAL_LOCKS", "0")
    return env


async def _run_git(args: list[str], cwd: Path) -> tuple[int, str] | None:
    """跑一条 git 命令，返回 (returncode, stdout)；失败返回 None。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=_git_env(),
        )
    except (OSError, ValueError) as exc:
        _log.warning("git 不可用（%s）: %s", args[0] if args else "?", exc)
        return None
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), timeout=GIT_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        _log.warning("git %s 超时（%.0fs），本次 shell 改动不记录", args[0] if args else "?", GIT_TIMEOUT_SEC)
        return None
    return proc.returncode or 0, out.decode("utf-8", errors="replace")


async def _run_git_raw(args: list[str], cwd: Path) -> tuple[int, bytes] | None:
    """跑一条 git 命令并保留原始字节（`git show` 的 blob 可能是二进制）。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=_git_env(),
        )
    except (OSError, ValueError) as exc:
        _log.warning("git 不可用（%s）: %s", args[0] if args else "?", exc)
        return None
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), timeout=GIT_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        _log.warning("git %s 超时，放弃读取", args[0] if args else "?")
        return None
    return proc.returncode or 0, out


async def git_head_blob(path: str) -> bytes | None:
    """tracked 文件在 HEAD 的原始内容（shell 改动的 diff 旧侧）。

    未跟踪 / 新文件 / 非 git 仓库 / git 不可用 → None（调用方不臆造 diff）。
    """
    target = Path(path)
    root_result = await _run_git(["rev-parse", "--show-toplevel"], target.parent)
    if root_result is None:
        return None
    code, out = root_result
    if code != 0 or not out.strip():
        return None
    root = Path(out.strip())
    try:
        rel = target.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None
    show = await _run_git_raw(["show", f"HEAD:{rel}"], root)
    if show is None:
        return None
    show_code, blob = show
    if show_code != 0:
        return None
    return blob


def _parse_porcelain(text: str) -> set[str]:
    """解析 `git status --porcelain -z` 的输出 → 仓库相对路径集合。

    -z 模式用 NUL 分隔，路径不做引号转义（普通模式会给带特殊字符的路径加引号）。
    重命名/复制的条目会多带一个源路径条目（NUL 分隔），要跳过。
    """
    paths: set[str] = set()
    entries = [e for e in text.split("\x00") if e]
    skip_next = False
    for entry in entries:
        if skip_next:
            skip_next = False
            continue
        if len(entry) < 4:
            continue
        status = entry[:2]
        path = entry[3:]
        if status[0] in ("R", "C"):
            # 重命名/复制：下一个条目是源路径
            skip_next = True
        if path:
            paths.add(path)
    return paths


async def git_dirty_paths(workspace: Path) -> tuple[Path, set[str]] | None:
    """当前工作区里"相对 HEAD 有改动"的文件（仓库相对路径）。

    返回 (仓库根, 路径集合)；不是 git 仓库或 git 不可用 → None。
    """
    root_result = await _run_git(["rev-parse", "--show-toplevel"], workspace)
    if root_result is None:
        return None
    code, out = root_result
    root = out.strip()
    if code != 0 or not root:
        return None
    root_path = Path(root)
    status = await _run_git(
        ["status", "--porcelain", "-z", "--untracked-files=normal"], root_path,
    )
    if status is None:
        return None
    status_code, status_out = status
    if status_code not in (0, 1):
        # git status 正常返回 0；1 表示有无法 stat 的条目，但仍会输出内容
        _log.warning("git status 返回 %d，放弃本次 shell 改动记录", status_code)
        return None
    return root_path, _parse_porcelain(status_out)


def _evict_stale() -> None:
    """淘汰过期/超量的基线登记。"""
    now = _time.time()
    for key in [k for k, v in _BASELINES.items() if now - v.ts > _BASELINE_TTL_SEC]:
        _BASELINES.pop(key, None)
    while len(_BASELINES) > _MAX_TRACKED_TASKS:
        _BASELINES.pop(next(iter(_BASELINES)), None)


def _workspace_path() -> Path:
    """当前工作区（工具执行目录）——工作区口径统一走 security.sandbox。"""
    from security.sandbox import get_workspace

    return Path(get_workspace())


# 明确只读的命令——它们不会改文件，不必为它们跑一次 git status。
# 大仓库里 `git status` 实测 ~200ms（要 stat 整个工作树），agent 循环里
# 大部分命令是读操作，逐条付这个代价不划算。
# 刻意保守：只列**不会改任何文件**的独立命令；git 之类的多用途命令一律照常追踪
#（git commit / git checkout 都会改文件，漏判的代价是"改动没被记录"）。
_READ_ONLY_COMMANDS = frozenset({
    "ls", "dir", "cat", "type", "pwd", "echo", "where", "which",
    "head", "tail", "wc", "tree",
})


def is_read_only_command(command: str) -> bool:
    """首 token 在只读白名单里 → 这条命令不会改文件。

    空命令/认不出来的命令一律当作"可能会改"（保守：漏判的代价是改动没被记录，
    多判的代价只是多跑一次 git status）。
    """
    tokens = (command or "").strip().split()
    if not tokens:
        return False
    return tokens[0].lower() in _READ_ONLY_COMMANDS


async def begin_command(task_id: str, command: str = "") -> None:
    """命令执行前：确保这个任务有一份基线（没有就取一次）。

    取不到（非 git 仓库 / git 不可用）时登记一个"不可用"基线——后续结算直接跳过，
    不会每条命令都再试一次 git（试失败也是开销）。
    只读命令直接跳过追踪：它们改不了文件。
    """
    if not task_id:
        return
    if task_id in _BASELINES:
        return
    if is_read_only_command(command):
        return
    snapshot = await git_dirty_paths(_workspace_path())
    if snapshot is None:
        _BASELINES[task_id] = _Baseline(set(), "", available=False)
    else:
        root, paths = snapshot
        _BASELINES[task_id] = _Baseline(paths, str(root), available=True)
    _evict_stale()


async def end_command(task_id: str) -> tuple[str, list[str]]:
    """命令执行后：返回 (仓库根, 本条命令新弄脏的文件)。

    基线推进到当前状态——下一条命令比的是"这条命令又改了什么"。
    """
    baseline = _BASELINES.get(task_id)
    if baseline is None:
        return "", []
    if not baseline.available:
        return "", []
    snapshot = await git_dirty_paths(_workspace_path())
    if snapshot is None:
        return "", []
    root, current = snapshot
    # 先算差集再推进基线——顺序反了差集永远是空的
    new_paths = sorted(current - baseline.paths)
    baseline.paths = current
    baseline.ts = _time.time()
    return str(root), new_paths


async def record_command_changes(db, task_id: str, session_id: str) -> list[dict]:
    """命令执行后结算：把本条命令新弄脏的文件记成会话交付物（source='shell'）。

    对标 DSH workspace-changes——"这一轮改动了哪些文件"里，shell 改的那部分。
    记录失败不影响命令本身的结果（命令已经执行完了），只记日志。
    """
    if not task_id or not session_id:
        return []
    try:
        root, paths = await end_command(task_id)
        if not paths:
            return []
        from services.deliverables import record_shell_changes

        return await record_shell_changes(db, session_id, task_id, paths, root)
    except Exception as exc:
        _log.warning("记录 shell 改动失败（不影响命令结果）: %s", exc)
        return []


def forget(task_id: str) -> None:
    """任务结束/取消时清掉基线。"""
    if task_id:
        _BASELINES.pop(task_id, None)
