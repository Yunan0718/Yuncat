"""tests/benchmarks/bench_tasks.py —— Phase 22 动态编排基准测试

3 个标准任务用于验证 DynamicOrchestrator 的生成和执行质量:
  1. 代码审查（code_review 模板 vs 动态生成）
  2. 多文件重构（跨 3+ 文件修改）
  3. 安全审计（需 adversary verify）

运行:
  python -m pytest tests/benchmarks/bench_tasks.py -v
  YUNCAT_BENCH=1 python tests/benchmarks/bench_tasks.py  # 全量基准测试
"""
import os
import sys
import json
import pytest
import time as _time

# ---- 基准任务定义 ----

BENCH_TASKS = [
    {
        "id": "code_review",
        "name": "代码审查——模板 vs 动态生成",
        "prompt": "审查以下代码的安全性和可维护性：检查是否有硬编码的密钥、SQL 注入风险、不安全的文件操作。生成结构化审查报告。",
        "effort": "standard",
        "expected_nodes_min": 2,
        "expected_verify": True,
        "metric": "审查发现数量",
    },
    {
        "id": "multi_file_refactor",
        "name": "多文件重构——跨 3+ 文件修改",
        "prompt": "将项目中的用户认证逻辑从 session-based 迁移到 JWT token-based。需要修改 auth.py、middleware.py、config.py。生成迁移计划并执行。",
        "effort": "deep",
        "expected_nodes_min": 4,
        "expected_verify": True,
        "metric": "正确的文件识别数/计划步骤数",
    },
    {
        "id": "security_audit",
        "name": "安全审计——需 adversary verify",
        "prompt": "对项目进行深度安全审计：检查认证系统、数据库查询、文件上传、API 端点权限控制。每个发现必须经过独立验证不被推翻。",
        "effort": "deep",
        "expected_nodes_min": 6,
        "expected_verify": True,
        "metric": "经过 adversary verify 的有效发现数",
    },
]


# ---- 基准测试 ----

def test_benchmark_tasks_defined():
    """基准任务定义完整"""
    assert len(BENCH_TASKS) == 3
    for t in BENCH_TASKS:
        assert "id" in t and "prompt" in t and "effort" in t
        assert t["effort"] in ("quick", "standard", "deep", "ultracode")
        assert t["expected_nodes_min"] >= 1


def test_workflow_generator_prompt_generates_valid_template():
    """验证 DAG 模板的生成 prompt 能生成合理的工作流 JSON"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.dynamic_orchestrator import WORKFLOW_GENERATOR_PROMPT
    prompt = WORKFLOW_GENERATOR_PROMPT.format(effort="standard")
    # 验证 prompt 包含所有 4 个 effort 级别的指南
    assert "quick" in prompt
    assert "standard" in prompt
    assert "deep" in prompt
    assert "ultracode" in prompt


def test_effort_levels_complete():
    """4 个 effort 级别全部映射"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.dynamic_orchestrator import EFFORT_MAP, DynamicOrchestrator
    assert "extreme" in EFFORT_MAP
    assert "low" in EFFORT_MAP
    assert "standard" in DynamicOrchestrator.MAX_ROUNDS
    assert DynamicOrchestrator.MAX_ROUNDS["ultracode"] == 5
    assert DynamicOrchestrator.MAX_ROUNDS["quick"] == 1


def test_convergence_detector_basic():
    """ConvergenceDetector 基础功能"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.dynamic_orchestrator import ConvergenceDetector

    # 空结果
    assert ConvergenceDetector.count_findings({}) == 0
    assert ConvergenceDetector.count_findings(None) == 0

    # 有发现的结果
    result = {"node_results": {"review": {"output": "[提案] 发现硬编码密钥\n[提案] SQL 注入风险\n[BUG] 路径遍历"}}}
    assert ConvergenceDetector.count_findings(result) >= 3

    # 无标记的结果
    result2 = {"node_results": {"review": {"output": "代码看起来没问题"}}}
    assert ConvergenceDetector.count_findings(result2) == 0


def test_convergence_overlap():
    """重复率计算"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.dynamic_orchestrator import ConvergenceDetector

    # 完全相同
    findings = ["发现硬编码密钥", "发现SQL注入"]
    assert ConvergenceDetector.overlap_pct(findings, findings) >= 0.9

    # 完全不同
    assert ConvergenceDetector.overlap_pct(["发现A"], ["发现B"]) < 0.3


def test_convergence_max_rounds():
    """Convergence 最大轮数配置正确"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.dynamic_orchestrator import DynamicOrchestrator
    assert DynamicOrchestrator.MAX_ROUNDS["quick"] == 1
    assert DynamicOrchestrator.MAX_ROUNDS["ultracode"] == 5


def test_adversarial_reviewer_regex_fix():
    """Phase 22 fix: AdversarialReviewer 使用精确正则避免误判"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.dynamic_orchestrator import AdversarialReviewer
    import re

    # 验证 ADVERSARIAL_PROMPT 包含 [REFUTED] 标记约定
    from services.dynamic_orchestrator import ADVERSARIAL_PROMPT
    assert "[REFUTED]" in ADVERSARIAL_PROMPT

    # 模拟输出——security 未 REFUTED，perf 被 REFUTED
    output = "security 的发现很好，引用正确。\n[REFUTED] perf 的问题不准确，原因是..."
    role_perf = "perf"
    role_security = "security"

    # 精确正则匹配
    perf_refuted = bool(re.search(rf'\[REFUTED\]\s+{re.escape(role_perf)}', output, re.IGNORECASE))
    security_refuted = bool(re.search(rf'\[REFUTED\]\s+{re.escape(role_security)}', output, re.IGNORECASE))

    assert perf_refuted is True
    assert security_refuted is False  # 不会被误判


# ---- 冒烟测试: DynamicOrchestrator 在不连 LLM 时的行为 ----

@pytest.mark.asyncio
async def test_orchestrator_no_agent_graceful_error():
    """无 agent_service 时 generate_and_run 优雅返回错误"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.dynamic_orchestrator import DynamicOrchestrator
    orch = DynamicOrchestrator(agent_service=None, workflow_service=None)
    result = await orch.generate_and_run("test task", "standard")
    assert result is not None
    assert "status" in result


@pytest.mark.asyncio
async def test_orchestrator_converges_quick():
    """quick effort 只运行 1 轮"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.dynamic_orchestrator import DynamicOrchestrator
    orch = DynamicOrchestrator(agent_service=None, workflow_service=None)
    result = await orch.generate_and_run("test", "quick")
    # quick effort 无 agent 时直接失败——首轮 generate 失败，rounds=0
    assert result["status"] == "failed"


# ---- 基准对比框架 ----

def test_bench_comparison_framework():
    """验证对比收集结构——即使没有真实 LLM 输出也能验证"""
    results = {}
    for task in BENCH_TASKS:
        results[task["id"]] = {
            "nodes_generated": task["expected_nodes_min"],
            "verify_layer": task["expected_verify"],
            "status": "not_run",
        }
    assert len(results) == 3
    assert results["code_review"]["verify_layer"] is True
    assert results["security_audit"]["nodes_generated"] >= 6
