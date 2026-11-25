"""scripts/mutation_check_phase_d.py —— Phase D 变异验证驱动

对 scheduler / isolation / task_manager / supervisor_tools 做
「故意改坏实现 → 期望用例如期失败」的变异验证，证明 tests/test_harness_phase_d.py
不是空测试（每个变异都会被至少一个用例捕获）。

锚点对齐 docs/phase-d-scheduler-isolation.md §7（10 个）：
  - 冲突对称性 / 祖先-后代 / WAITING_PERMISSION 占用 / DAG 方向 → D-01~D-05
  - acquire_task 冲突放行 → D-06
  - 副本范围扩整库 / 合并方向反 / 删删除同步 → D-08/D-09
  - human_resolve 不重置 attempt / 删 BLOCKED→QUEUED 边 → D-11/D-13

用法：
  python scripts/mutation_check_phase_d.py [--pytest PATH]
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTEST_TARGET = str(ROOT / "tests" / "test_harness_phase_d.py")

# (说明, 文件, 旧串, 新串, 期望捕获该变异的用例关键字)
MUTATIONS = [
    # ── scheduler：冲突判定 ────────────────────────────────────
    (
        "SCH-1 冲突判定破坏对称性（只判 A 含 B，不判 B 含 A）",
        "src-python/harness/scheduler.py",
        "return a == b or a in b.parents or b in a.parents",
        "return a == b or a in b.parents",
        "test_d01_write_paths_equal_conflict",
    ),
    (
        "SCH-2 祖先-后代判定缺失（只判路径相等）",
        "src-python/harness/scheduler.py",
        "return a == b or a in b.parents or b in a.parents",
        "return a == b",
        "test_d02_ancestor_descendant_conflict",
    ),
    (
        "SCH-3 can_run 忽略 WAITING_PERMISSION 占用（L1 挂起不持有写入权）",
        "src-python/harness/scheduler.py",
        "_ACTIVE_STATUSES = frozenset({TaskStatus.RUNNING, TaskStatus.WAITING_PERMISSION})",
        "_ACTIVE_STATUSES = frozenset({TaskStatus.RUNNING})",
        "test_d03_can_run_reports_running_occupation",
    ),
    (
        "SCH-4 DAG 依赖方向反（写成被依赖方依赖依赖方）",
        "src-python/harness/scheduler.py",
        "                dep[t.id].add(other.id)  # t 依赖 other（other 写、t 读）",
        "                dep[other.id].add(t.id)  # 变异：依赖方向反",
        "test_d05_dependency_graph_read_write",
    ),
    (
        "SCH-5 acquire_task 冲突时仍放行 RUNNING",
        "src-python/harness/scheduler.py",
        """    if not ok:
        return {"acquired": False, "conflicts": conflicts, "task_id": task_id}""",
        """    if not ok:
        conflicts = []  # 变异：忽略冲突直接放行""",
        "test_d06_acquire_conflict_keeps_queued",
    ),
    # ── isolation：浅拷贝与合并 ────────────────────────────────
    (
        "ISO-1 副本复制范围扩大为整库（未声明路径入副本）",
        "src-python/harness/isolation.py",
        "        sources = write + read",
        "        sources = [self.workspace]  # 变异：整库复制",
        "test_d08_copy_only_declared_paths",
    ),
    (
        "ISO-2 merge_back 复制方向反（主工作区 → 副本）",
        "src-python/harness/isolation.py",
        """            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            merged.append(rel)""",
        """            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, src)  # 变异：复制方向反（主→副本）
            merged.append(rel)""",
        "test_d09_merge_back_syncs_write_scope",
    ),
    (
        "ISO-3 合并遗漏删除同步（副本删除的文件不回流删除）",
        "src-python/harness/isolation.py",
        """        # 删除同步：快照有、副本无、主工作区未被外部修改 → 删除
        for rel, h in snap.items():
            if (copy_root / rel).exists():
                continue
            dst = self.workspace / rel
            if dst.exists() and _sha256(dst) == h:
                dst.unlink()
                deleted.append(rel)
            elif dst.exists():
                conflicts.append(rel)""",
        "        pass  # 变异：合并遗漏删除同步",
        "test_d09_merge_back_syncs_write_scope",
    ),
    # ── HITL：human_resolve 与状态机 ───────────────────────────
    (
        "HITL-1 human_resolve(retry) 不重置 attempt（熔断历史残留）",
        "src-python/harness/supervisor_tools.py",
        """        # 熔断计数重置：人工介入后 attempt 归零（重新计算，不继承失败历史）
        await db.execute("UPDATE harness_tasks SET attempt=0 WHERE id=?", (task_id,))
        await db.commit()
        task = await transition(db, task_id, TaskStatus.QUEUED, f"人工裁决重试: {reason}")""",
        """        # 变异：不重置 attempt（熔断历史残留）
        task = await transition(db, task_id, TaskStatus.QUEUED, f"人工裁决重试: {reason}")""",
        "test_d11_human_resolve_retry",
    ),
    (
        "HITL-2 删除 BLOCKED→QUEUED 状态机边（BLOCKED 重回终态无出路）",
        "src-python/harness/task_manager.py",
        """    TaskStatus.BLOCKED: frozenset({
        # Phase D: HITL 人工裁决——retry 放行重试（BLOCKED→QUEUED），cancel 人工取消
        TaskStatus.QUEUED, TaskStatus.CANCELLED,
    }),""",
        """    TaskStatus.BLOCKED: frozenset({
        TaskStatus.CANCELLED,  # 变异：BLOCKED→QUEUED 边被删除
    }),""",
        "test_d13_blocked_state_transition_regression",
    ),
]


def run_pytest(pytest_exe: str, test_file: str, keyword: str) -> int:
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
