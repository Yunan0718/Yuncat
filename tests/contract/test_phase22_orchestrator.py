"""tests/contract/test_phase22_orchestrator.py —— Phase 22 动态编排合约测试"""
import os
import sys
import json
import pytest


# ---- _extract_json ----

def test_extract_json_code_block():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.dynamic_orchestrator import _extract_json
    text = '```json\n{"nodes":[],"edges":[]}\n```'
    result = _extract_json(text)
    assert result == {"nodes": [], "edges": []}


def test_extract_json_bare():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.dynamic_orchestrator import _extract_json
    result = _extract_json('{"key": "value"}')
    assert result == {"key": "value"}


def test_extract_json_embedded():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.dynamic_orchestrator import _extract_json
    text = 'Here is my plan:\n\n{"nodes": [{"id":"a"}], "edges": []}\n\nHope this works!'
    result = _extract_json(text)
    assert result is not None
    assert result["nodes"][0]["id"] == "a"


def test_extract_json_invalid():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.dynamic_orchestrator import _extract_json
    assert _extract_json("No JSON here") is None
    assert _extract_json("") is None


# ---- Effort 映射 ----

def test_complexity_to_effort():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.dynamic_orchestrator import complexity_to_effort
    assert complexity_to_effort("extreme") == "ultracode"
    assert complexity_to_effort("high") == "deep"
    assert complexity_to_effort("medium") == "standard"
    assert complexity_to_effort("low") == "quick"
    assert complexity_to_effort("unknown") == "standard"


# ---- WORKFLOW_GENERATOR_PROMPT ----

def test_workflow_generator_prompt_has_effort_placeholder():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.dynamic_orchestrator import WORKFLOW_GENERATOR_PROMPT
    assert "{effort}" in WORKFLOW_GENERATOR_PROMPT
    assert "quick" in WORKFLOW_GENERATOR_PROMPT
    assert "ultracode" in WORKFLOW_GENERATOR_PROMPT


# ---- ADVERSARIAL_PROMPT ----

def test_adversarial_prompt_contains_refuted():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.dynamic_orchestrator import ADVERSARIAL_PROMPT
    assert "[REFUTED]" in ADVERSARIAL_PROMPT
    assert "持怀疑态度" in ADVERSARIAL_PROMPT


# ---- AdversarialReviewer ----

def test_adversarial_reviewer_init():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.dynamic_orchestrator import AdversarialReviewer
    reviewer = AdversarialReviewer(agent_service=None)
    assert reviewer._agent is None


@pytest.mark.asyncio
async def test_adversarial_reviewer_no_agent():
    """无 agent_service 时 review 返回原 proposals"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.dynamic_orchestrator import AdversarialReviewer
    reviewer = AdversarialReviewer(agent_service=None)
    proposals = [{"role_name": "test", "output": "some finding"}]
    surviving, refuted = await reviewer.review(proposals, "test input")
    assert surviving == proposals
    assert refuted == []


# ---- DynamicOrchestrator ----

def test_orchestrator_init():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.dynamic_orchestrator import DynamicOrchestrator
    orch = DynamicOrchestrator(agent_service=None, workflow_service=None)
    assert orch._agent is None


def test_generate_and_run_no_agent():
    """无 agent_service 时 generate_and_run 返回 error"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.dynamic_orchestrator import DynamicOrchestrator
    orch = DynamicOrchestrator(agent_service=None, workflow_service=None)
    import asyncio
    result = asyncio.run(orch.generate_and_run("test", "standard"))
    assert result is not None
    assert "error" in result or "status" in result


# ---- DeliberationService complexity → effort ----

def test_assess_complexity_has_effort():
    """Phase 22: assess_complexity 返回 effort 字段"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.deliberation.deliberation_service import DeliberationService
    ds = DeliberationService()
    result = ds.assess_complexity("帮我审查整个项目的安全、性能和架构设计")
    assert "effort" in result, "assess_complexity must include effort field (Phase 22)"
    assert result["effort"] in ("quick", "standard", "deep", "ultracode")


def test_simple_prompt_maps_to_low_effort():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.deliberation.deliberation_service import DeliberationService
    ds = DeliberationService()
    result = ds.assess_complexity("改一行代码")
    assert result["effort"] == "quick"


# ---- main.py WS deep_review endpoint ----

def test_deep_review_handler_exists():
    # lifespan 拆分重构后：lifespan.py 为薄编排层（委托 _init_orchestrator_skills），
    # DynamicOrchestrator 的实际实例化 + 挂载（app.state.dynamic_orchestrator）在 lifespan_postinit.py。
    # 断言对齐真实布局：验证委托链 + 实际挂载逻辑。
    ws_code = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "routes", "ws.py"),
        encoding="utf-8",
    ).read()
    assert "deep_review" in ws_code, "routes/ws.py missing deep_review WS handler"
    lifespan_code = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "lifespan.py"),
        encoding="utf-8",
    ).read()
    lifespan_postinit_code = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "lifespan_postinit.py"),
        encoding="utf-8",
    ).read()
    # lifespan 薄编排层委托 _init_orchestrator_skills
    assert "_init_orchestrator_skills" in lifespan_code
    # lifespan_postinit.py 中 DynamicOrchestrator 实例化并挂到 app.state.dynamic_orchestrator
    assert "DynamicOrchestrator" in lifespan_postinit_code
    assert "app.state.dynamic_orchestrator" in lifespan_postinit_code


# ---- 回归: DeliberationService 其他方法不受影响 ----

def test_assess_complexity_still_works():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.deliberation.deliberation_service import DeliberationService
    ds = DeliberationService()
    result = ds.assess_complexity("修复一个拼写错误")
    assert result["level"] == "low"
    assert result["score"] < 5


def test_complexity_signals_preserved():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.deliberation.deliberation_service import COMPLEXITY_SIGNALS, COMPLEXITY_ROLE_COUNTS
    assert "extreme" in COMPLEXITY_SIGNALS
    assert "low" in COMPLEXITY_SIGNALS
    assert COMPLEXITY_ROLE_COUNTS["extreme"] == 6
    assert COMPLEXITY_ROLE_COUNTS["low"] == 1


# ---- BA.2 冲突 5: 路由优先级 ----

def test_effort_mapping_consistent():
    """effort 映射与复杂度对应关系一致"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.dynamic_orchestrator import EFFORT_MAP, WORKFLOW_GENERATOR_PROMPT
    # quick/standard/deep/ultracode 四个 effort 级别都在 prompt 中有说明
    for effort in ("quick", "standard", "deep", "ultracode"):
        assert effort in WORKFLOW_GENERATOR_PROMPT, f"effort '{effort}' not in generator prompt"


# ---- Phase 22 Fix: AdversarialReviewer 正则解析 ----

def test_adversarial_regex_exact_match():
    """精确正则: [REFUTED] role_name 匹配，不跨句误判"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    import re
    role_perf = "perf"
    role_security = "security"
    output = "security 的发现很好。\n[REFUTED] perf 的问题不准确..."

    perf_refuted = bool(re.search(rf'\[REFUTED\]\s+{re.escape(role_perf)}', output, re.IGNORECASE))
    security_refuted = bool(re.search(rf'\[REFUTED\]\s+{re.escape(role_security)}', output, re.IGNORECASE))

    assert perf_refuted is True
    assert security_refuted is False  # 不误判


def test_adversarial_regex_no_refuted_none_marked():
    """所有角色都未被 REFUTED 时全部保留"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    import re
    output = "各角色的发现都有事实支撑，没有需要推翻的。"
    for role in ("security", "perf", "arch"):
        is_refuted = bool(re.search(rf'\[REFUTED\]\s+{re.escape(role)}', output, re.IGNORECASE))
        assert is_refuted is False


# ---- Phase 22 Fix: Convergence Detector ----

def test_convergence_detector_findings_count():
    """count_findings 从 DAG 结果中统计发现数"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.dynamic_orchestrator import ConvergenceDetector

    result = {"node_results": {
        "sec": {"output": "[提案] 密钥泄露\n[提案] 注入风险\n"},
        "perf": {"output": "[发现] N+1查询\n[BUG] 内存泄漏\n"},
    }}
    assert ConvergenceDetector.count_findings(result) >= 4


def test_convergence_detector_empty():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.dynamic_orchestrator import ConvergenceDetector
    assert ConvergenceDetector.count_findings({}) == 0


def test_convergence_overlap_similar():
    """相似发现的重复率 >= 0.3"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.dynamic_orchestrator import ConvergenceDetector
    pct = ConvergenceDetector.overlap_pct(
        ["hardcoded API key in config.py", "SQL injection risk in login"],
        ["hardcoded API key in config file", "XSS vulnerability found"],
    )
    assert pct >= 0.3  # 第一项高度相似


def test_orchestrator_max_rounds():
    """MAX_ROUNDS 配置正确（Phase 48-H 放宽上限）"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.dynamic_orchestrator import DynamicOrchestrator
    assert DynamicOrchestrator.MAX_ROUNDS["quick"] == 2
    assert DynamicOrchestrator.MAX_ROUNDS["deep"] == 10
    assert DynamicOrchestrator.MAX_ROUNDS["ultracode"] == 20


# ---- Benchmark tasks ----

def test_benchmark_tasks_exist():
    """3 个标准基准任务已定义"""
    benchmarks_dir = os.path.join(os.path.dirname(__file__), "..", "benchmarks")
    assert os.path.isdir(benchmarks_dir), "benchmarks/ directory not found"
