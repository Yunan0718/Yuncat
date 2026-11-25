"""tests/unit/test_analysis_agent.py —— Task 6.10 AnalysisAgent 单元测试

覆盖 AnalysisAgent.analyze 的 5 维度规范化 + 各种失败兜底路径。

设计：mock adapter.invoke 返回预设 content；mock model_config.get_active_config。
不启动真实子进程，不发起真实 HTTP。

5 维度（参考 MetaGPT RequirementsAnalysis / OpenHands task analysis）：
1. complexity    —— 复杂度评估（level + summary + factors）
2. dependencies  —— 依赖关系（external/internal/blockers）
3. risks         —— 风险评估（items + severity + mitigation + summary）
4. resources     —— 资源需求（tech_stack/tools/skills）
5. acceptance    —— 验收标准（criteria + test_points）
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from config import DEFAULT_AGENT_ID


# ═══════════════════════════════════════════════════════════════
# Mock 工具
# ═══════════════════════════════════════════════════════════════

class _MockAdapter:
    """模拟 adapter——可配置 invoke 返回值，记录调用"""

    def __init__(self, invoke_return=None):
        self.agent_id = "hermes"
        self._invoke_return = invoke_return if invoke_return is not None else {"content": "{}"}
        self.invoke_calls: list[tuple[str, dict]] = []

    async def invoke(self, prompt: str, context: dict | None = None) -> dict:
        self.invoke_calls.append((prompt, context or {}))
        if isinstance(self._invoke_return, Exception):
            raise self._invoke_return
        return self._invoke_return


class _MockModelConfig:
    """模拟 ModelConfigService——get_active_config 返回预设 cfg"""

    def __init__(self, cfg=None):
        self._cfg = cfg if cfg is not None else {
            "model": "test-flash",
            "api_key": "test-key",
            "base_url": "http://test.local/v1",
        }

    async def get_active_config(self, agent_id: str = "") -> dict:
        if isinstance(self._cfg, Exception):
            raise self._cfg
        return self._cfg


def _valid_report() -> dict:
    """构造一份合法的 5 维度报告（LLM 返回样例）"""
    return {
        "complexity": {
            "level": "complex",
            "summary": "涉及多模块集成",
            "factors": ["跨服务", "异步流程"],
        },
        "dependencies": {
            "external": ["redis", "postgres"],
            "internal": ["auth模块", "user模块"],
            "blockers": ["需先完成 schema 迁移"],
        },
        "risks": {
            "items": [
                {"description": "迁移期间数据丢失", "severity": "high", "mitigation": "先备份"},
                {"description": "性能下降", "severity": "medium", "mitigation": "加索引"},
            ],
            "summary": "2 项风险需关注",
        },
        "resources": {
            "tech_stack": ["python", "sql"],
            "tools": ["pytest", "aiosqlite"],
            "skills": ["异步编程"],
        },
        "acceptance": {
            "criteria": ["迁移成功", "测试通过"],
            "test_points": ["跑全部 pytest"],
        },
    }


@pytest.fixture
def make_agent():
    """工厂 fixture——返回 (agent, adapter, model_config) 三元组

    DEFAULT_AGENT_ID 已从 hermes 改为 yuncat（Phase N）——按默认 agent 注册。
    """
    def _make(invoke_return=None, cfg=None):
        adapter = _MockAdapter(invoke_return=invoke_return)
        mc = _MockModelConfig(cfg=cfg)
        from services.long_task.analysis_agent import AnalysisAgent
        agent = AnalysisAgent(adapters={DEFAULT_AGENT_ID: adapter}, model_config=mc)
        return agent, adapter, mc
    return _make


# ═══════════════════════════════════════════════════════════════
# 正常路径
# ═══════════════════════════════════════════════════════════════

class TestAnalyzeNormal:
    """analyze —— 正常路径"""

    async def test_normal_returns_5_dimensions(self, make_agent):
        """正常返回 5 维度结构"""
        report_json = json.dumps(_valid_report())
        agent, _, _ = make_agent(invoke_return={"content": report_json})

        result = await agent.analyze("实现用户登录", "原始任务")

        # 5 维度都存在
        assert "complexity" in result
        assert "dependencies" in result
        assert "risks" in result
        assert "resources" in result
        assert "acceptance" in result
        # 不是兜底报告
        assert result.get("_fallback") is not True

    async def test_complexity_level_preserved(self, make_agent):
        """complexity.level 正常保留"""
        report = _valid_report()
        report["complexity"]["level"] = "simple"
        agent, _, _ = make_agent(invoke_return={"content": json.dumps(report)})

        result = await agent.analyze("任务")

        assert result["complexity"]["level"] == "simple"

    async def test_risk_items_structure(self, make_agent):
        """risks.items 每项含 description/severity/mitigation"""
        report = _valid_report()
        agent, _, _ = make_agent(invoke_return={"content": json.dumps(report)})

        result = await agent.analyze("任务")

        assert len(result["risks"]["items"]) == 2
        for item in result["risks"]["items"]:
            assert "description" in item
            assert "severity" in item
            assert "mitigation" in item
        assert result["risks"]["items"][0]["severity"] == "high"
        assert result["risks"]["items"][1]["severity"] == "medium"

    async def test_dependencies_three_lists(self, make_agent):
        """dependencies 含 external/internal/blockers 三个列表"""
        report = _valid_report()
        agent, _, _ = make_agent(invoke_return={"content": json.dumps(report)})

        result = await agent.analyze("任务")

        assert result["dependencies"]["external"] == ["redis", "postgres"]
        assert result["dependencies"]["internal"] == ["auth模块", "user模块"]
        assert result["dependencies"]["blockers"] == ["需先完成 schema 迁移"]

    async def test_resources_three_lists(self, make_agent):
        """resources 含 tech_stack/tools/skills 三个列表"""
        report = _valid_report()
        agent, _, _ = make_agent(invoke_return={"content": json.dumps(report)})

        result = await agent.analyze("任务")

        assert result["resources"]["tech_stack"] == ["python", "sql"]
        assert result["resources"]["tools"] == ["pytest", "aiosqlite"]
        assert result["resources"]["skills"] == ["异步编程"]

    async def test_acceptance_criteria_and_test_points(self, make_agent):
        """acceptance 含 criteria + test_points"""
        report = _valid_report()
        agent, _, _ = make_agent(invoke_return={"content": json.dumps(report)})

        result = await agent.analyze("任务")

        assert result["acceptance"]["criteria"] == ["迁移成功", "测试通过"]
        assert result["acceptance"]["test_points"] == ["跑全部 pytest"]

    async def test_max_tokens_800_passed(self, make_agent):
        """context 含 max_tokens=800（对齐方案文档）"""
        report_json = json.dumps(_valid_report())
        agent, adapter, _ = make_agent(invoke_return={"content": report_json})

        await agent.analyze("任务")

        assert len(adapter.invoke_calls) == 1
        ctx = adapter.invoke_calls[0][1]
        assert ctx["max_tokens"] == 800

    async def test_temperature_03_passed(self, make_agent):
        """context 含 temperature=0.3（低温度保证结构化输出稳定）"""
        report_json = json.dumps(_valid_report())
        agent, adapter, _ = make_agent(invoke_return={"content": report_json})

        await agent.analyze("任务")

        ctx = adapter.invoke_calls[0][1]
        assert ctx["temperature"] == 0.3


# ═══════════════════════════════════════════════════════════════
# 规范化
# ═══════════════════════════════════════════════════════════════

class TestNormalize:
    """_normalize_report —— 边界值规范化"""

    async def test_invalid_complexity_level_normalized(self, make_agent):
        """complexity.level 非法 → 规范化为 'moderate'"""
        report = _valid_report()
        report["complexity"]["level"] = "extreme"  # 非法
        agent, _, _ = make_agent(invoke_return={"content": json.dumps(report)})

        result = await agent.analyze("任务")

        assert result["complexity"]["level"] == "moderate"

    async def test_invalid_risk_severity_normalized(self, make_agent):
        """risks.items.severity 非法 → 规范化为 'medium'"""
        report = _valid_report()
        report["risks"]["items"][0]["severity"] = "critical"  # 非法
        agent, _, _ = make_agent(invoke_return={"content": json.dumps(report)})

        result = await agent.analyze("任务")

        assert result["risks"]["items"][0]["severity"] == "medium"

    async def test_missing_sections_filled_with_empty(self, make_agent):
        """缺某些维度 → 用空结构填充，不抛异常"""
        report = {"complexity": {"level": "simple"}}  # 仅 complexity
        agent, _, _ = make_agent(invoke_return={"content": json.dumps(report)})

        result = await agent.analyze("任务")

        assert result["complexity"]["level"] == "simple"
        assert result["dependencies"]["external"] == []
        assert result["dependencies"]["internal"] == []
        assert result["dependencies"]["blockers"] == []
        assert result["risks"]["items"] == []
        assert result["resources"]["tech_stack"] == []
        assert result["acceptance"]["criteria"] == []

    async def test_non_list_factors_normalized_to_empty(self, make_agent):
        """complexity.factors 非列表 → 规范化为空列表"""
        report = _valid_report()
        report["complexity"]["factors"] = "不是列表"
        agent, _, _ = make_agent(invoke_return={"content": json.dumps(report)})

        result = await agent.analyze("任务")

        assert result["complexity"]["factors"] == []

    async def test_risk_item_non_dict_skipped(self, make_agent):
        """risks.items 含非 dict 项 → 跳过该项"""
        report = _valid_report()
        report["risks"]["items"].append("不是 dict")  # 非法项
        report["risks"]["items"].append(None)
        agent, _, _ = make_agent(invoke_return={"content": json.dumps(report)})

        result = await agent.analyze("任务")

        # 2 个合法项保留，2 个非法项被跳过
        assert len(result["risks"]["items"]) == 2

    async def test_empty_strings_filtered_from_lists(self, make_agent):
        """列表中的空字符串被过滤"""
        report = _valid_report()
        report["dependencies"]["external"] = ["redis", "", "  ", "postgres"]
        agent, _, _ = make_agent(invoke_return={"content": json.dumps(report)})

        result = await agent.analyze("任务")

        assert result["dependencies"]["external"] == ["redis", "postgres"]

    async def test_section_not_dict_treated_as_empty(self, make_agent):
        """维度子节点非 dict（如 list/str）→ 当作空 dict 处理"""
        report = _valid_report()
        report["dependencies"] = ["not", "a", "dict"]  # 非法
        agent, _, _ = make_agent(invoke_return={"content": json.dumps(report)})

        result = await agent.analyze("任务")

        assert result["dependencies"]["external"] == []
        assert result["dependencies"]["internal"] == []


# ═══════════════════════════════════════════════════════════════
# 失败兜底
# ═══════════════════════════════════════════════════════════════

class TestAnalyzeFallback:
    """analyze —— 各种失败场景的兜底路径"""

    async def test_fallback_report_has_5_dimensions(self, make_agent):
        """兜底报告也含 5 维度结构（前端无需特殊处理）"""
        agent, _, _ = make_agent(invoke_return=RuntimeError("LLM 挂了"))

        result = await agent.analyze("任务", "原始")

        assert "complexity" in result
        assert "dependencies" in result
        assert "risks" in result
        assert "resources" in result
        assert "acceptance" in result
        # 兜底报告标记
        assert result.get("_fallback") is True

    async def test_llm_exception_returns_fallback(self, make_agent):
        """adapter.invoke 抛异常 → 返回兜底报告"""
        agent, _, _ = make_agent(invoke_return=RuntimeError("网络错误"))

        result = await agent.analyze("任务")

        assert result.get("_fallback") is True

    async def test_adapter_missing_returns_fallback(self, make_agent):
        """adapters 中无指定 agent_id → 兜底报告"""
        from services.long_task.analysis_agent import AnalysisAgent
        mc = _MockModelConfig()
        agent = AnalysisAgent(adapters={}, model_config=mc)  # 无 adapter

        result = await agent.analyze("任务", agent_id="hermes")

        assert result.get("_fallback") is True

    async def test_model_config_failure_returns_fallback(self, make_agent):
        """model_config.get_active_config 抛异常 → 兜底报告"""
        agent, _, _ = make_agent(cfg=RuntimeError("配置加载失败"))

        result = await agent.analyze("任务")

        assert result.get("_fallback") is True

    async def test_adapter_error_returns_fallback(self, make_agent):
        """adapter 返回 {"error": ...} → 兜底报告"""
        agent, _, _ = make_agent(invoke_return={"error": "模型不可用"})

        result = await agent.analyze("任务")

        assert result.get("_fallback") is True

    async def test_json_parse_failure_returns_fallback(self, make_agent):
        """content 非 JSON → 兜底报告"""
        agent, _, _ = make_agent(invoke_return={"content": "不是 JSON"})

        result = await agent.analyze("任务")

        assert result.get("_fallback") is True

    async def test_parsed_not_dict_returns_fallback(self, make_agent):
        """JSON 解析后非 dict（如 list/str）→ 兜底报告"""
        agent, _, _ = make_agent(invoke_return={"content": '["not", "a", "dict"]'})

        result = await agent.analyze("任务")

        assert result.get("_fallback") is True

    async def test_empty_confirmed_goal_returns_fallback(self, make_agent):
        """confirmed_goal 为空 → 直接返回兜底报告（不调 LLM）"""
        report_json = json.dumps(_valid_report())
        agent, adapter, _ = make_agent(invoke_return={"content": report_json})

        result = await agent.analyze("", "原始任务")

        assert result.get("_fallback") is True
        # 不应调用 adapter
        assert len(adapter.invoke_calls) == 0

    async def test_fallback_uses_confirmed_goal_in_criteria(self, make_agent):
        """兜底报告 acceptance.criteria 含 confirmed_goal（保证最小可用）"""
        agent, _, _ = make_agent(invoke_return=RuntimeError("失败"))

        result = await agent.analyze("实现登录功能", "原始")

        assert "实现登录功能" in result["acceptance"]["criteria"]


# ═══════════════════════════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════════════════════════

class TestSingleton:
    """get_analysis_agent / set_analysis_agent 全局单例"""

    def test_get_returns_none_by_default(self):
        """未 set 前返回 None（测试隔离：保证无残留）"""
        from services.long_task import analysis_agent as mod
        # 先清理可能残留
        mod._analysis_agent = None
        assert mod.get_analysis_agent() is None

    def test_set_and_get_roundtrip(self):
        """set 后 get 返回同一实例"""
        from services.long_task.analysis_agent import (
            AnalysisAgent, get_analysis_agent, set_analysis_agent,
        )
        import services.long_task.analysis_agent as mod
        mc = _MockModelConfig()
        agent = AnalysisAgent(adapters={}, model_config=mc)
        try:
            set_analysis_agent(agent)
            assert get_analysis_agent() is agent
        finally:
            # 清理全局状态，避免污染后续测试
            mod._analysis_agent = None
