"""scripts/mutation_check_phase_b.py —— Phase B 变异验证驱动

对 policy_engine / verifier 做「故意改坏实现 → 期望用例如期失败」的变异验证，
证明 tests/test_harness_phase_b.py 不是空测试（每个变异都会被至少一个用例捕获）。

用法：
  python scripts/mutation_check_phase_b.py [--pytest PATH]

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
PYTEST_TARGET = str(ROOT / "tests" / "test_harness_phase_b.py")

# (说明, 文件, 旧串, 新串, 期望捕获该变异的用例关键字)
MUTATIONS = [
    # ── policy_engine ──────────────────────────────────────────────
    (
        "PE-1 去掉 _mentioned 使命提及条件（声明内直接 L0）",
        "src-python/harness/policy_engine.py",
        "if declared and _within(canon, declared, workspace) and _mentioned(rel, mission, acceptance_criteria):",
        "if declared and _within(canon, declared, workspace):",
        "test_l1_declared_but_not_mentioned",
    ),
    (
        "PE-2 verify_approval 硬闸直接放行（跳过越界复核）",
        "src-python/harness/policy_engine.py",
        """    if not canonical:
        return True, "无路径请求"
    workspace = Path(workspace).resolve()""",
        """    if not canonical:
        return True, "无路径请求"
    return True, "审批路径在工作区内"  # 变异：跳过 canonicalize 复核""",
        "test_verify_approval_outside_rejected",
    ),
    (
        "PE-3 工具白名单判断取反（白名单内一律 DENY）",
        "src-python/harness/policy_engine.py",
        "if permissions.tools and tool_name not in permissions.tools:",
        "if permissions.tools and tool_name in permissions.tools:",
        "test_l0_allow_declared_and_mentioned",
    ),
    # ── verifier ───────────────────────────────────────────────────
    (
        "V-1 交叉验证用子集比较（claimed ⊆ actual 即通过）",
        "src-python/harness/verifier.py",
        "files_match = set(report.claimed_files_modified) == set(observation.actual_files_modified)",
        "files_match = set(report.claimed_files_modified) <= set(observation.actual_files_modified)",
        "test_verify_mismatch_on_partial_claim",
    ),
    (
        "V-2 取证丢弃未跟踪文件（不跑 ls-files）",
        "src-python/harness/verifier.py",
        """    rc, out, _ = await run(["ls-files", "--others", "--exclude-standard"], workspace)
    if rc == 0:
        actual += [line.strip() for line in out.splitlines() if line.strip()]""",
        "    pass  # 变异：不再收集未跟踪文件",
        "test_collect_observation_merges_diff_and_untracked",
    ),
    (
        "V-3 退出码校验恒为真（退出码不一致也 VERIFIED）",
        "src-python/harness/verifier.py",
        "exit_match = observation.test_exit_code is None or report.test_exit_code == observation.test_exit_code",
        "exit_match = True",
        "test_verify_mismatch_on_exit_code_diff",
    ),
    (
        "V-4 非 git 工作区不再 fail-closed（返回空观察）",
        "src-python/harness/verifier.py",
        """        return RuntimeObservation(
            actual_files_modified=[],
            test_exit_code=None,
            error_message="非 git 工作区，Verifier 无法取证（fail-closed）",
        )""",
        """        return RuntimeObservation(
            actual_files_modified=[],
            test_exit_code=None,
            error_message=None,
        )  # 变异：fail-closed 被移除""",
        "test_collect_observation_non_git_fail_closed",
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
    print(f"{'变异':<48} {'捕获用例':<42} 结果")
    print("-" * 120)
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
            print(f"{label:<48} {kw:<42} {'被捕获 ✓' if caught else '未被捕获 ✗'}")
            if not caught:
                print(f"    !! 变异未被捕获，需补测试。命令: {args.pytest} -m pytest -q {PYTEST_TARGET} -k {kw}")
        finally:
            shutil.move(str(backup), str(path))

    print("-" * 120)
    print("变异验证结论：", "全部被捕获，测试有效" if all_caught else "存在未被捕获的变异，测试不足！")
    return 0 if all_caught else 1


if __name__ == "__main__":
    sys.exit(main())
