"""tests/unit/test_intent_service.py —— 意图路由引擎测试"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from config import DEFAULT_AGENT_ID


class MockAgentService:
    def __init__(self, adapters: dict | None = None):
        self.adapters = adapters or {DEFAULT_AGENT_ID: True}

    async def is_running(self, agent_id: str) -> bool:
        return agent_id in self.adapters

    async def invoke(self, agent_id: str, prompt: str, context: dict = None) -> str:
        return f"task-{agent_id}"


class MockRulesRepo:
    async def get_enabled_rules(self) -> list:
        return []


@pytest.fixture
def intent_svc():
    from services.intent_service import IntentService
    return IntentService(
        agent_service=MockAgentService(),
        rules_repo=MockRulesRepo(),
    )


class TestIntentServiceClassifyRoute:
    """测试 classify_route 纯路由分类"""

    async def test_default_route_to_default_agent(self, intent_svc):
        """无匹配时路由到默认 agent"""
        route = await intent_svc.classify_route("今天天气怎么样")
        assert route["agent_id"] == DEFAULT_AGENT_ID
        assert "默认路由" in route["reason"]

    async def test_code_request_routes_to_native_agent(self, intent_svc):
        """编程请求路由到原生 Yuncat——**外部 Agent 不参与自动路由**。

        2026-09-10 决策：意图分类器只会建议原生 Yuncat / 工作流模板；
        把任务判给未启动的外部网关会造成静默失败（用户以为跑起来了其实没跑）。
        外部能力需要时由原生 Agent 通过 delegate_to_agent 显式委托。
        """
        from config import EXTERNAL_AGENT_IDS

        intent_svc.agent_service = MockAgentService({DEFAULT_AGENT_ID: True, "openclaw": True})
        route = await intent_svc.classify_route("帮我写一个排序函数")
        assert route["agent_id"] == DEFAULT_AGENT_ID
        assert route["agent_id"] not in EXTERNAL_AGENT_IDS

    async def test_classify_never_returns_external_agent(self, intent_svc):
        """任何输入都不得被自动路由到外部 Agent（含已注册的情况）"""
        from config import EXTERNAL_AGENT_IDS

        intent_svc.agent_service = MockAgentService(
            {DEFAULT_AGENT_ID: True, "openclaw": True, "hermes": True})
        for prompt in ("帮我写一个排序函数", "分析这个 CSV 表格", "搜一下最新消息", "读一下这个文件"):
            route = await intent_svc.classify_route(prompt)
            assert route["agent_id"] not in EXTERNAL_AGENT_IDS, prompt
            assert route["agent_id"] == DEFAULT_AGENT_ID, prompt

    async def test_file_operation_routes_to_default_agent(self, intent_svc):
        """文件操作请求路由到默认 agent"""
        route = await intent_svc.classify_route("读取文件 test.py")
        assert route["agent_id"] == DEFAULT_AGENT_ID

    async def test_search_routes_to_default_agent(self, intent_svc):
        """搜索请求路由到默认 agent"""
        route = await intent_svc.classify_route("搜索 Python 教程")
        assert route["agent_id"] == DEFAULT_AGENT_ID


class TestIntentServiceAnalyze:
    """测试 analyze()——HTTP POST 路径（含分发）"""

    async def test_analyze_returns_task_id(self, intent_svc):
        """analyze 返回 task_id"""
        result = await intent_svc.analyze("今天天气怎么样")
        assert "task_id" in result
        assert result["task_id"] == f"task-{DEFAULT_AGENT_ID}"
        assert "route_info" in result

    async def test_analyze_includes_rules_context(self, intent_svc):
        """analyze 返回包含 rules_applied"""
        result = await intent_svc.analyze("你好")
        assert "rules_applied" in result["route_info"]


class TestGetActiveRules:
    """测试活跃规则获取"""

    async def test_default_rules_without_repo(self):
        """无 rules_repo 时返回内置规则"""
        from services.intent_service import IntentService
        svc = IntentService()
        rules = await svc.get_active_rules()
        assert len(rules) >= 1
        assert rules[0]["id"] == "builtin_confirm_write"


class TestExtractMentions:
    """Batch 4.3: @-mention 识别"""

    def test_extracts_folder_web_git(self):
        from services.intent_service import extract_mentions

        mentions = extract_mentions("请分析 @folder:src 和 @git:diff，再搜 @web:python")
        assert [m["type"] for m in mentions] == ["folder", "git", "web"]
        assert mentions[0]["value"] == "src"
        assert mentions[1]["value"] == "diff"
        assert mentions[2]["value"] == "python"

    def test_returns_empty_for_no_mentions(self):
        from services.intent_service import extract_mentions

        assert extract_mentions("没有引用") == []
        assert extract_mentions("") == []
        assert extract_mentions(None) == []

    def test_ignores_invalid_types(self):
        from services.intent_service import extract_mentions

        mentions = extract_mentions("@unknown:value @file:test.py")
        assert len(mentions) == 1
        assert mentions[0]["type"] == "file"

    def test_preserves_raw_and_order(self):
        from services.intent_service import extract_mentions

        mentions = extract_mentions("@session:s1 @agent:hermes @memory:rag")
        assert [m["raw"] for m in mentions] == ["@session:s1", "@agent:hermes", "@memory:rag"]

    async def test_analyze_includes_mentions_in_route_info(self, intent_svc):
        """analyze 在 route_info 中透传 mentions"""
        result = await intent_svc.analyze("总结 @folder:docs 内容")
        assert "mentions" in result["route_info"]
        assert result["route_info"]["mentions"][0]["type"] == "folder"
        assert result["route_info"]["mentions"][0]["value"] == "docs"
