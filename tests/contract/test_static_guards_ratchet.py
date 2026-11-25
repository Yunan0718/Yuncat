"""tests/contract/test_static_guards_ratchet.py —— 静态守卫棘轮（尺寸 + 静默异常）

**策略：棘轮（ratchet），不是一次性重构。**

- 新增违规 → 失败（不允许再欠新债）
- 已有违规继续变大/变多 → 失败（禁止在最脏的地方继续堆）
- 改善 → 通过，但要求把基线数字**同步下调**（棘轮只往紧的方向走）

两个维度都用 `scripts/static_guards.py` 的**同一份**扫描实现
（生成基线与校验共用，避免"两处实现必然漂移"）。

维度一 · 尺寸（AGENTS.md 硬约束 #4：文件 <800 行、函数 <50 行）
    实测 21 个文件、204 个函数超标；规则此前只写在文档里、无人守 → 静默腐化。

维度二 · 静默异常吞噬
    宽泛 `except Exception` 若只 pass / 返回空值，必须二选一：
      ① 无回退路径 → 记日志留痕（否则失败与"本来就没数据"不可区分）
      ② 有明确回退 → 在 handler 内注释声明意图（含「回退/降级/best-effort/可选/忽略」等词）
    两者皆无即计入基线：禁止新增、禁止比基线更多。

基线文件：`scripts/size_baseline.json`、`scripts/silent_except_baseline.json`（均提交进仓库）。
重新生成：``python scripts/static_guards.py --update [--only size|silent]``
"""
import json
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import static_guards as sg  # noqa: E402


@pytest.fixture(scope="module")
def size_baseline():
    return sg.load_json(sg.SIZE_BASELINE)


@pytest.fixture(scope="module")
def silent_baseline():
    return sg.load_json(sg.SILENT_BASELINE)


@pytest.fixture(scope="module")
def measured_size(size_baseline):
    return sg.measure_size(size_baseline["limits"]["file_lines"], size_baseline["limits"]["func_lines"])


@pytest.fixture(scope="module")
def measured_silent():
    return sg.measure_silent_except()


# ═══════════════════════════════════════════════════════════════
# 维度一 · 尺寸
# ═══════════════════════════════════════════════════════════════

def test_no_new_oversized_files(measured_size, size_baseline):
    files, _ = measured_size
    new = {f: n for f, n in files.items() if f not in size_baseline["files"]}
    assert new == {}, (
        "出现新的超长文件（约束：文件 <800 行）——请拆分模块；"
        f"确需例外请登记进 scripts/size_baseline.json：{new}"
    )


def test_no_oversized_file_grows(measured_size, size_baseline):
    files, _ = measured_size
    grew = {f: (size_baseline["files"][f], files[f]) for f in files
            if f in size_baseline["files"] and files[f] > size_baseline["files"][f]}
    assert grew == {}, f"已有超长文件又变大（基线 → 现状），请把新逻辑拆到新模块：{grew}"


def test_no_new_oversized_functions(measured_size, size_baseline):
    _, funcs = measured_size
    new = {k: n for k, n in funcs.items() if k not in size_baseline["funcs"]}
    assert new == {}, (
        "出现新的超长函数（约束：函数 <50 行）——请提取子函数；"
        f"确需例外请登记进 scripts/size_baseline.json：{new}"
    )


def test_no_oversized_function_grows(measured_size, size_baseline):
    _, funcs = measured_size
    grew = {k: (size_baseline["funcs"][k], funcs[k]) for k in funcs
            if k in size_baseline["funcs"] and funcs[k] > size_baseline["funcs"][k]}
    assert grew == {}, (
        "已有超长函数又变长了（基线 → 现状）——这正是「在大函数里继续堆逻辑」的信号，"
        f"请先提取：{grew}"
    )


def test_size_baseline_not_stale_upward(measured_size, size_baseline):
    """改善后必须下调基线，否则棘轮失去约束力。"""
    files, funcs = measured_size
    stale: list[str] = []
    for f, recorded in size_baseline["files"].items():
        now = files.get(f, 0)
        if now < recorded:
            stale.append(f"{f}: 基线 {recorded} → 现状 {now or '已达标'}")
    for k, recorded in size_baseline["funcs"].items():
        now = funcs.get(k, 0)
        if now < recorded:
            stale.append(f"{k}: 基线 {recorded} → 现状 {now or '已达标'}")
    assert stale == [], (
        "以下条目已收缩，请下调 scripts/size_baseline.json 的对应数字"
        f"（可用 `python scripts/static_guards.py --update --only size`）：{stale}"
    )


def test_size_baseline_shape_is_valid(size_baseline):
    assert size_baseline["limits"]["file_lines"] > 0
    assert size_baseline["limits"]["func_lines"] > 0
    for f, n in size_baseline["files"].items():
        assert n > size_baseline["limits"]["file_lines"], f"{f} 未超限却在基线里"
    for k, n in size_baseline["funcs"].items():
        assert n > size_baseline["limits"]["func_lines"], f"{k} 未超限却在基线里"


# ═══════════════════════════════════════════════════════════════
# 维度二 · 静默异常吞噬
# ═══════════════════════════════════════════════════════════════

def test_no_new_silent_except_sites(measured_silent, silent_baseline):
    new = {f: n for f, n in measured_silent.items() if f not in silent_baseline["counts"]}
    assert new == {}, (
        "新的「静默吞噬异常」——宽泛 except 里只 pass/返回空值时，请二选一："
        "① 记日志留痕（无回退路径）；② 注释声明回退意图（有回退）。"
        f"涉及文件：{new}"
    )


def test_no_silent_except_site_grows(measured_silent, silent_baseline):
    grew = {f: (silent_baseline["counts"][f], n) for f, n in measured_silent.items()
            if f in silent_baseline["counts"] and n > silent_baseline["counts"][f]}
    assert grew == {}, f"文件内静默吞噬变多（基线 → 现状），请不要继续增加：{grew}"


def test_total_silent_except_does_not_grow(measured_silent, silent_baseline):
    total = sum(measured_silent.values())
    assert total <= silent_baseline["total"], (
        f"静默吞噬总数变多：基线 {silent_baseline['total']} → 现状 {total}"
    )


def test_silent_except_baseline_not_stale_upward(measured_silent, silent_baseline):
    stale = [f"{f}: 基线 {n} → 现状 {measured_silent.get(f, 0)}"
             for f, n in silent_baseline["counts"].items()
             if measured_silent.get(f, 0) < n]
    assert stale == [], (
        "以下文件的静默吞噬已减少，请下调 scripts/silent_except_baseline.json"
        f"（可用 `python scripts/static_guards.py --update --only silent`）：{stale}"
    )


def test_silent_except_baseline_shape_is_valid(silent_baseline):
    assert silent_baseline["total"] == sum(silent_baseline["counts"].values())
    for f, n in silent_baseline["counts"].items():
        assert n > 0, f"{f} 计数为 0 却在基线里"
        assert f.endswith(".py"), f"{f} 不是 Python 文件"
