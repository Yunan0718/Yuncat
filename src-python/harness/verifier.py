"""harness/verifier.py —— 客观取证 + 交叉验证

对齐 docs/harness-supervisor-worker.md Phase B：
  - collect_observation: git diff --name-only + untracked + sha256 hash 快照
    + 测试退出码捕获 → RuntimeObservation（客观事实，不信任 Worker 主张）
  - verify: claimed 集合 == actual 集合 且 退出码一致 → VERIFIED，否则 MISMATCH
  - 非 git 工作区 fail-closed：无法取证 → 空 observation + error_message，
    交叉验证必为 MISMATCH（信任边界：无法证明 = 不通过）
  - runner 可注入：单测用 fake runner，不依赖真实 git
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Awaitable, Callable

from harness.schemas import RuntimeObservation, WorkerReport

VERIFIED = "VERIFIED"
MISMATCH = "MISMATCH"

Runner = Callable[[list[str], Path], Awaitable[tuple[int, str, str]]]

_GIT_TIMEOUT_SEC = 15


def _sha256(path: Path) -> str:
    """文件内容 sha256 快照——供审计与后续内容级交叉验证。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


async def _default_runner(cmd: list[str], cwd: Path) -> tuple[int, str, str]:
    """默认取证执行器：git -C <workspace> <cmd>，15s 超时。"""
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(cwd), *cmd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", "git 取证超时"
    code = proc.returncode if proc.returncode is not None else -1
    return code, out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace")


async def collect_observation(
    workspace: Path,
    *,
    test_command: list[str] | None = None,
    runner: Runner | None = None,
) -> RuntimeObservation:
    """客观取证：git diff --name-only(HEAD) + 未跟踪文件 + hash 快照 + 测试退出码。

    非 git 工作区 → fail-closed（actual 为空 + error_message，决策必 MISMATCH）。
    test_command 为 None 时不跑测试（test_exit_code 保持 None，verify 跳过退出码校验）。
    """
    workspace = Path(workspace)
    run = runner or _default_runner

    rc, _, _ = await run(["rev-parse", "--is-inside-work-tree"], workspace)
    if rc != 0:
        return RuntimeObservation(
            actual_files_modified=[],
            test_exit_code=None,
            error_message="非 git 工作区，Verifier 无法取证（fail-closed）",
        )

    actual: list[str] = []
    rc, out, _ = await run(["diff", "--name-only", "HEAD"], workspace)
    if rc == 0:
        actual += [line.strip() for line in out.splitlines() if line.strip()]
    rc, out, _ = await run(["ls-files", "--others", "--exclude-standard"], workspace)
    if rc == 0:
        actual += [line.strip() for line in out.splitlines() if line.strip()]
    actual = sorted(set(actual))

    hashes: dict[str, str] = {}
    for rel in actual:
        p = workspace / rel
        if p.is_file():
            try:
                hashes[rel] = _sha256(p)
            except OSError:
                continue

    test_exit: int | None = None
    if test_command:
        rc, _, _ = await run(test_command, workspace)
        test_exit = rc

    return RuntimeObservation(actual_files_modified=actual, file_hashes=hashes, test_exit_code=test_exit)


def verify(report: WorkerReport, observation: RuntimeObservation) -> str:
    """交叉验证 Worker 主张 vs 客观取证。

    - claimed 集合 == actual 集合（文件级）
    - 退出码：observation 未跑测试（None）→ 跳过；否则必须与声称一致
    两者皆真 → VERIFIED；否则 MISMATCH（信任边界：WorkerReport 不可信）。
    """
    files_match = set(report.claimed_files_modified) == set(observation.actual_files_modified)
    exit_match = observation.test_exit_code is None or report.test_exit_code == observation.test_exit_code
    return VERIFIED if (files_match and exit_match) else MISMATCH


async def verify_workspace(
    workspace: Path,
    report: WorkerReport,
    *,
    test_command: list[str] | None = None,
    runner: Runner | None = None,
) -> tuple[str, RuntimeObservation]:
    """一键取证 + 验证，返回 (decision, observation)。"""
    obs = await collect_observation(workspace, test_command=test_command, runner=runner)
    return verify(report, obs), obs
