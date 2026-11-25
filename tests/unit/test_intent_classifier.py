"""tests/unit/test_intent_classifier.py —— LLM 分类器单元测试"""
import os
import sys
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class TestLlmClassify:
    """llm_classify 函数"""

    async def test_returns_none_without_summarizer(self):
        """无 summarizer 时返回 None"""
        from prompts.intent_classifier import llm_classify
        result = await llm_classify("test prompt", summarizer=None)
        assert result is None

    async def test_parses_valid_json(self):
        """解析有效的 JSON 响应"""
        from prompts.intent_classifier import llm_classify

        async def mock_summarizer(text: str) -> str:
            return '{"type":"direct","agent_id":"hermes","confidence":0.95,"reason":"测试"}'

        result = await llm_classify("test", summarizer=mock_summarizer)
        assert result is not None
        assert result["agent_id"] == "hermes"
        assert result["confidence"] == 0.95

    async def test_handles_markdown_wrapped_json(self):
        """处理 markdown 包裹的 JSON"""
        from prompts.intent_classifier import llm_classify

        async def mock_summarizer(text: str) -> str:
            return '```json\n{"type":"direct","agent_id":"openclaw","confidence":0.8,"reason":"编程"}\n```'

        result = await llm_classify("写代码", summarizer=mock_summarizer)
        assert result is not None
        assert result["agent_id"] == "openclaw"

    async def test_returns_none_on_invalid_json(self):
        """无效 JSON 时返回 None"""
        from prompts.intent_classifier import llm_classify

        async def mock_summarizer(text: str) -> str:
            return "not valid json at all"

        result = await llm_classify("test", summarizer=mock_summarizer)
        assert result is None

    async def test_returns_none_on_exception(self):
        """summarizer 抛异常时返回 None"""
        from prompts.intent_classifier import llm_classify

        async def mock_summarizer(text: str) -> str:
            raise RuntimeError("LLM unavailable")

        result = await llm_classify("test", summarizer=mock_summarizer)
        assert result is None


class TestIntentClassifierPrompt:
    """分类提示词模板"""

    def test_prompt_includes_routes(self):
        """提示词的「可用路由」只提供原生 Agent 与工作流模板。

        2026-09-10 决策：外部 Agent（hermes/openclaw）不进入自动分类候选；
        提示词里若列了它们，模型就会把任务判给未启动的网关 → 静默失败。
        （提示词正文可能在说明里提到 openclaw 作为"委托目标"，但不得作为 agent_id 选项。）
        """
        from prompts.intent_classifier import INTENT_CLASSIFIER_PROMPT
        assert '"agent_id":"yuncat"' in INTENT_CLASSIFIER_PROMPT
        assert "data_analysis" in INTENT_CLASSIFIER_PROMPT
        for external in ("hermes", "openclaw"):
            assert f'"agent_id":"{external}"' not in INTENT_CLASSIFIER_PROMPT
            assert f'"agent_id":"{external}"' not in INTENT_CLASSIFIER_PROMPT.replace(" ", "")

    def test_prompt_includes_output_format(self):
        from prompts.intent_classifier import INTENT_CLASSIFIER_PROMPT
        assert "仅输出 JSON" in INTENT_CLASSIFIER_PROMPT
