"""scripts/mutation_check_phase_c.py —— Phase C 变异验证驱动

对 budget_manager / runtime / 埋点（tool_service / adapters/base.py）做
「故意改坏实现 → 期望用例如期失败」的变异验证，证明 tests/test_harness_phase_c.py
不是空测试（每个变异都会被至少一个用例捕获）。

锚点对齐 docs/budget-reliability-design.md §8.5：
  - 删工具计数 / `>=` 改 `>` / tokens 累加改覆盖 / 删超限回调触发 → C-02/03/08
  - 熔断分支删除（实施期新增设计，非原文档锚点）→ C-24
  - invoke 不更新 last_usage → C-10
  - 删 killpg(SIGKILL) / 删 TerminateJobObject → C-12/13
  - 删日志截断 / 扫描不重置 / 扫描漏发通知 → C-14/16/18

用法：
  python scripts/mutation_check_phase_c.py [--pytest PATH]

每个变异：
  1. 备份目标文件 → 应用变异 → 运行指定用例
  2. 期望 pytest 失败（变异被捕获）→ 通过
  3. finally 恢复原文件
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTEST_TARGET = str(ROOT / "tests" / "test_harness_phase_c.py")

# (说明, 文件, 旧串, 新串, 期望捕获该变异的用例关键字)
MUTATIONS = [
    # ── budget_manager：硬计数语义 ────────────────────────────────
    (
        "BC-1 删除工具计数（count_tool_invocation 不再 ++）",
        "src-python/harness/budget_manager.py",
        """    state.tool_invocations += 1
    limit = state.budget.max_tool_invocations""",
        """    # 变异：删除工具计数
    limit = state.budget.max_tool_invocations""",
        "test_c02_tool_invocations_ge_semantics",
    ),
    (
        "BC-2 超限判定 >= 改 >（恰好达限不抛）",
        "src-python/harness/budget_manager.py",
        "if limit is not None and state.tool_invocations >= limit:",
        "if limit is not None and state.tool_invocations > limit:",
        "test_c02_tool_invocations_ge_semantics",
    ),
    (
        "BC-3 tokens 累加改覆盖（第二次汇报冲掉累计值）",
        "src-python/harness/budget_manager.py",
        "    state.tokens += int(total or 0)",
        "    state.tokens = int(total or 0)  # 变异：覆盖而非累加",
        "test_c03_tokens_incremental_accumulate",
    ),
    (
        "BC-4 删除超限回调触发（handler 不再被调用）",
        "src-python/harness/budget_manager.py",
        """    exc = BudgetExceeded(budget_type, limit, current_val, task_id=state.task_id)
    handler = _exceeded_handler
    if handler is not None:
        try:
            handler(exc)
        except Exception:
            _log.warning("budget exceeded handler failed", exc_info=True)
    raise exc""",
        """    exc = BudgetExceeded(budget_type, limit, current_val, task_id=state.task_id)
    # 变异：删除超限回调触发
    raise exc""",
        "test_c08_exceeded_handler_registry",
    ),
    (
        "BC-5 删除熔断分支（超限后工具调用不再被拒绝）",
        "src-python/harness/budget_manager.py",
        """    if state.exceeded:
        _raise_exceeded(state, state.exceeded_type or BUDGET_TYPE_TOOL, 0, state.tool_invocations)
    state.tool_invocations += 1""",
        """    # 变异：删除熔断分支——token/seconds 超限被吞后工具仍继续执行
    state.tool_invocations += 1""",
        "test_c24_budget_breaker_blocks_further_tools",
    ),
    # ── 埋点：invoke 非流式 usage 捕获 ─────────────────────────────
    (
        "BC-6 invoke 不更新 last_usage（非流式 token 预算形同虚设）",
        "src-python/adapters/base.py",
        """                if isinstance(data.get("usage"), dict):
                    usage = data["usage"]
                    self.last_usage = {""",
        """                if isinstance(data.get("usage"), dict):
                    pass  # 变异：invoke 不更新 last_usage
                    self.last_usage = {""",
        "test_c10_invoke_captures_usage",
    ),
    # ── runtime：进程组杀 ─────────────────────────────────────────
    (
        "RT-1 POSIX 删除 SIGKILL 第二段（宽限后残留进程不补刀）",
        "src-python/harness/runtime.py",
        """    if grace_sec > 0:
        await asyncio.sleep(grace_sec)
    try:
        os.killpg(pid, _SIGKILL)
    except ProcessLookupError:
        pass""",
        """    if grace_sec > 0:
        await asyncio.sleep(grace_sec)
    # 变异：删除 SIGKILL 第二段——进程可能残留""",
        "test_c12_kill_process_tree_posix",
    ),
    (
        "RT-2 Windows 不再走 Job Object 整组杀（孙进程逃逸）",
        "src-python/harness/runtime.py",
        """    if hjob:
        try:
            from security.job_object import terminate_job

            if terminate_job(hjob):
                _log.warning("TerminateJobObject(hjob=%s) reason=%s", hjob, reason)
                return
        except Exception:
            _log.warning("terminate_job failed, fallback to os.kill", exc_info=True)""",
        """    if False:  # 变异：不再走 Job Object 整组杀
        try:
            from security.job_object import terminate_job

            if terminate_job(hjob):
                _log.warning("TerminateJobObject(hjob=%s) reason=%s", hjob, reason)
                return
        except Exception:
            _log.warning("terminate_job failed, fallback to os.kill", exc_info=True)""",
        "test_c13_kill_process_tree_windows",
    ),
    # ── runtime：日志截断 ─────────────────────────────────────────
    (
        "RT-3 删除日志截断分支（超限日志不保尾裁剪）",
        "src-python/harness/runtime.py",
        """        with path.open("rb") as f:
            f.seek(size - max_bytes)
            tail = f.read()
        path.write_bytes(tail)
        _log.warning("truncated log %s: %d -> %d bytes", path, size, max_bytes)
        return True""",
        """        # 变异：删除日志截断——文件保持超限状态
        return False""",
        "test_c14_truncate_log_preserves_tail",
    ),
    # ── runtime：崩溃恢复扫描 ─────────────────────────────────────
    (
        "RT-4 扫描不重置状态（transition 调用被移除）",
        "src-python/harness/runtime.py",
        """            task = await transition(
                conn, task_id, TaskStatus.TIMEOUT,
                f"crash recovery: stale {stale_sec}s without heartbeat",
            )""",
        """            _ = None  # 变异：扫描不重置状态""",
        "test_c16_scan_resets_stale_running",
    ),
    (
        "RT-5 扫描漏发 task_reset 通知（notifier.publish 被绕过）",
        "src-python/harness/runtime.py",
        """        if notifier is not None:
            try:
                await notifier.publish("task_reset", {""",
        """        if False:  # 变异：扫描漏发通知
            try:
                await notifier.publish("task_reset", {""",
        "test_c18_scan_publishes_task_reset",
    ),
]


def run_pytest(pytest_exe: str, test_file: str, keyword: str) -> int:
    # 注意：必须 -m pytest，否则 python.exe file.py 会把测试文件当普通脚本运行（exit 0）
    cmd = [pytest_exe, "-m", "pytest", "-q", test_file, "-k", keyword, "--no-header"]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    return proc.returncode


def apply_mutation(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, f"变异锚点不唯一或不存在: {old[:60]!r}"
    return text.replace(old, new)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pytest", default=str(ROOT / "src-python" / "python-embed" / "python.exe"),
                        help="python 解释器路径（内部以 -m pytest 调用）")
    args = parser.parse_args()

    print(f"变异验证目标：{PYTEST_TARGET}")
    print(f"{'变异':<52} {'捕获用例':<42} 结果")
    print("-" * 128)
    all_caught = True
    for label, rel, old, new, kw in MUTATIONS:
        path = ROOT / rel
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)
        try:
            text = path.read_text(encoding="utf-8")
            path.write_text(apply_mutation(text, old, new), encoding="utf-8")
            rc = run_pytest(args.pytest, PYTEST_TARGET, kw)
            caught = rc != 0  # 期望 pytest 失败 = 变异被捕获
            all_caught &= caught
            print(f"{label:<52} {kw:<42} {'被捕获 ✓' if caught else '未被捕获 ✗'}")
            if not caught:
                print(f"    !! 变异未被捕获，需补测试。命令: {args.pytest} -m pytest -q {PYTEST_TARGET} -k {kw}")
        finally:
            shutil.move(str(backup), str(path))

    print("-" * 128)
    print("变异验证结论：", "全部被捕获，测试有效" if all_caught else "存在未被捕获的变异，测试不足！")
    return 0 if all_caught else 1


if __name__ == "__main__":
    sys.exit(main())
