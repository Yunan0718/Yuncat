"""tests/unit/test_fim_completion.py — Task 6.1: LLM FIM 内联补全测试

对标:
- Cursor / GitHub Copilot / Continue 的 FIM 补全
- DeepSeek /beta/completions FIM 端点
- Codestral/Mistral /v1/fim/completions 端点

覆盖:
- FIM 配置缺失时回退规则引擎
- FIM 配置存在但 prefix 过短时不触发 LLM
- DeepSeek FIM prompt 格式正确
- Codestral FIM API 调用格式正确
- FIM HTTP 错误时回退
- FIM 超时时回退
- FIM 缓存命中不重复请求
- prefix/suffix 截断
- 不支持的 provider 不调用 FIM
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture(autouse=True)
def _clear_cache():
    """每个测试前清空 FIM 缓存。"""
    from services.completion_service import clear_fim_cache
    clear_fim_cache()
    yield
    clear_fim_cache()


class TestFIMConfigDetection:
    """FIM 配置检测——何时触发 LLM vs 回退规则引擎"""

    async def test_no_fim_config_returns_empty(self):
        """provider 未配置 fim 段 → 返回空（调用方回退规则引擎）"""
        from services.completion_service import suggest_inline_completions_llm

        model_cfg = {
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test",
            # 无 fim 段
        }
        items = await suggest_inline_completions_llm("python", "def hello(", "", model_cfg)
        assert items == []

    async def test_no_api_key_returns_empty(self):
        """无 api_key → 返回空"""
        from services.completion_service import suggest_inline_completions_llm

        model_cfg = {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "",
            "fim": {"model": "deepseek-coder", "format": "deepseek"},
        }
        items = await suggest_inline_completions_llm("python", "def hello(", "", model_cfg)
        assert items == []

    async def test_prefix_too_short_returns_empty(self):
        """prefix < FIM_MIN_PREFIX_CHARS → 不触发 LLM（省 token）"""
        from services.completion_service import suggest_inline_completions_llm
        from config import FIM_MIN_PREFIX_CHARS

        model_cfg = {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-test",
            "fim": {"model": "deepseek-coder", "format": "deepseek"},
        }
        # prefix 长度 < FIM_MIN_PREFIX_CHARS
        short_prefix = "a" * (FIM_MIN_PREFIX_CHARS - 1)
        items = await suggest_inline_completions_llm("python", short_prefix, "", model_cfg)
        assert items == []

    async def test_unsupported_provider_returns_empty(self):
        """provider 不在 _FIM_DISPATCH 中 → 返回空"""
        from services.completion_service import suggest_inline_completions_llm

        model_cfg = {
            "provider": "unknown_provider",
            "base_url": "https://api.unknown.com/v1",
            "api_key": "sk-test",
            "fim": {"model": "unknown-model", "format": "unknown_provider"},
        }
        items = await suggest_inline_completions_llm("python", "def hello_world():", "", model_cfg)
        assert items == []


class TestDeepSeekFIM:
    """DeepSeek FIM API 调用"""

    async def test_deepseek_fim_prompt_format(self):
        """DeepSeek FIM prompt 用特殊 token 包裹 prefix/suffix"""
        from services.completion_service import _build_deepseek_fim_prompt

        prompt = _build_deepseek_fim_prompt("def hello():", "    return None")
        assert "<｜fim▁begin｜>" in prompt
        assert "<｜fim▁hole｜>" in prompt
        assert "<｜fim▁end｜>" in prompt
        assert "def hello():" in prompt
        assert "    return None" in prompt

    async def test_deepseek_fim_success(self):
        """DeepSeek FIM 成功返回补全项"""
        from services.completion_service import suggest_inline_completions_llm

        model_cfg = {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-test",
            "fim": {"model": "deepseek-coder", "format": "deepseek"},
        }

        # mock httpx.AsyncClient.post 返回成功响应
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"text": "    return 'hello'"}],
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            items = await suggest_inline_completions_llm(
                "python", "def hello():\n", "\n# end", model_cfg,
            )

        assert len(items) == 1
        assert "return 'hello'" in items[0]["insertText"]

        # 验证请求 URL 是 /beta/completions
        call_args = mock_client.post.call_args
        url = call_args[0][0] if call_args[0] else call_args[1].get("url", "")
        assert "/beta/completions" in url

    async def test_deepseek_fim_http_error_returns_empty(self):
        """DeepSeek FIM HTTP 错误 → 返回空（调用方回退规则引擎）"""
        from services.completion_service import suggest_inline_completions_llm

        model_cfg = {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-test",
            "fim": {"model": "deepseek-coder", "format": "deepseek"},
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            # 模拟 HTTP 500 错误
            mock_response = MagicMock()
            mock_response.status_code = 500
            mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
                "Internal Server Error", request=MagicMock(), response=mock_response,
            )
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            items = await suggest_inline_completions_llm(
                "python", "def hello():\n", "", model_cfg,
            )

        assert items == []

    async def test_deepseek_fim_timeout_returns_empty(self):
        """DeepSeek FIM 超时 → 返回空"""
        from services.completion_service import suggest_inline_completions_llm

        model_cfg = {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-test",
            "fim": {"model": "deepseek-coder", "format": "deepseek"},
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.side_effect = httpx.ReadTimeout("timeout")
            mock_client_cls.return_value = mock_client

            items = await suggest_inline_completions_llm(
                "python", "def hello():\n", "", model_cfg,
            )

        assert items == []


class TestCodestralFIM:
    """Codestral/Mistral FIM API 调用"""

    async def test_codestral_fim_success(self):
        """Codestral FIM 成功返回补全项"""
        from services.completion_service import suggest_inline_completions_llm

        model_cfg = {
            "provider": "mistral",
            "base_url": "https://api.mistral.ai/v1",
            "api_key": "sk-test",
            "fim": {"model": "codestral-latest", "format": "mistral"},
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "    return 42"}}],
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            items = await suggest_inline_completions_llm(
                "python", "def answer():\n", "", model_cfg,
            )

        assert len(items) == 1
        assert "return 42" in items[0]["insertText"]

        # 验证请求 URL 是 /v1/fim/completions
        call_args = mock_client.post.call_args
        url = call_args[0][0] if call_args[0] else call_args[1].get("url", "")
        assert "/fim/completions" in url

        # 验证 payload 包含 prompt 和 suffix 分离字段
        payload = call_args[1].get("json", {})
        assert "prompt" in payload
        assert "suffix" in payload


class TestFIMCache:
    """FIM 缓存行为"""

    async def test_cache_hit_does_not_repeat_request(self):
        """相同 prefix+suffix 缓存命中时不重复请求 LLM"""
        from services.completion_service import suggest_inline_completions_llm

        model_cfg = {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-test",
            "fim": {"model": "deepseek-coder", "format": "deepseek"},
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"choices": [{"text": "completion"}]}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            # 第一次调用——应该发 HTTP 请求
            items1 = await suggest_inline_completions_llm(
                "python", "def cached_func():\n", "", model_cfg,
            )
            assert mock_client.post.call_count == 1

            # 第二次相同输入——应命中缓存，不发请求
            items2 = await suggest_inline_completions_llm(
                "python", "def cached_func():\n", "", model_cfg,
            )
            assert mock_client.post.call_count == 1  # 仍然只有 1 次

        assert items1 == items2

    async def test_different_prefix_does_not_hit_cache(self):
        """不同 prefix 不命中缓存"""
        from services.completion_service import suggest_inline_completions_llm

        model_cfg = {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-test",
            "fim": {"model": "deepseek-coder", "format": "deepseek"},
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"choices": [{"text": "x"}]}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            await suggest_inline_completions_llm("python", "def func_a():\n", "", model_cfg)
            await suggest_inline_completions_llm("python", "def func_b():\n", "", model_cfg)

        assert mock_client.post.call_count == 2


class TestFIMTruncation:
    """prefix/suffix 截断"""

    def test_long_prefix_truncated_to_max(self):
        """超长 prefix 被截断到 FIM_MAX_PREFIX_CHARS"""
        from services.completion_service import _truncate_for_fim
        from config import FIM_MAX_PREFIX_CHARS

        long_prefix = "x" * (FIM_MAX_PREFIX_CHARS + 1000)
        prefix_t, suffix_t = _truncate_for_fim(long_prefix, "")
        assert len(prefix_t) == FIM_MAX_PREFIX_CHARS

    def test_long_suffix_truncated_to_max(self):
        """超长 suffix 被截断到 FIM_MAX_SUFFIX_CHARS"""
        from services.completion_service import _truncate_for_fim
        from config import FIM_MAX_SUFFIX_CHARS

        long_suffix = "y" * (FIM_MAX_SUFFIX_CHARS + 500)
        prefix_t, suffix_t = _truncate_for_fim("short", long_suffix)
        assert len(suffix_t) == FIM_MAX_SUFFIX_CHARS

    def test_short_prefix_suffix_unchanged(self):
        """短 prefix/suffix 不被截断"""
        from services.completion_service import _truncate_for_fim

        prefix_t, suffix_t = _truncate_for_fim("short prefix", "short suffix")
        assert prefix_t == "short prefix"
        assert suffix_t == "short suffix"

    def test_truncation_keeps_cursor_proximity(self):
        """截断保留光标附近内容——prefix 保留末尾，suffix 保留开头"""
        from services.completion_service import _truncate_for_fim
        from config import FIM_MAX_PREFIX_CHARS, FIM_MAX_SUFFIX_CHARS

        # prefix 末尾标记 + 超长前缀（超过 FIM_MAX_PREFIX_CHARS）
        prefix = "BEGINING" + "x" * (FIM_MAX_PREFIX_CHARS + 100) + "END_NEAR_CURSOR"
        prefix_t, _ = _truncate_for_fim(prefix, "")
        assert "END_NEAR_CURSOR" in prefix_t  # 末尾保留
        assert "BEGINING" not in prefix_t  # 开头被截断

        # suffix 开头标记 + 超长后缀（超过 FIM_MAX_SUFFIX_CHARS）
        suffix = "START_NEAR_CURSOR" + "y" * (FIM_MAX_SUFFIX_CHARS + 100) + "ENDING"
        _, suffix_t = _truncate_for_fim("x", suffix)
        assert "START_NEAR_CURSOR" in suffix_t  # 开头保留


class TestRouteFallback:
    """路由层 FIM → 规则引擎回退"""

    def test_route_returns_source_field(self):
        """路由返回包含 source 字段（fim 或 rule）"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routes.completion import router as completion_router

        app = FastAPI()
        app.include_router(completion_router)
        client = TestClient(app)

        res = client.post("/api/v1/inline-completion", json={
            "prefix": "def ",
            "suffix": "",
            "language": "python",
        })
        assert res.status_code == 200
        data = res.json()
        assert "items" in data
        assert "source" in data
        # 无 model_config_service → 回退规则引擎
        assert data["source"] == "rule"

    def test_route_fim_failure_falls_back_to_rule(self):
        """FIM 失败时回退规则引擎——source=rule"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routes.completion import router as completion_router

        app = FastAPI()
        app.include_router(completion_router)

        # 注入 mock model_config_service——get_active_config 抛异常
        class _BrokenModelConfigService:
            async def get_active_config(self):
                raise RuntimeError("model config broken")

        app.state.model_config_service = _BrokenModelConfigService()
        client = TestClient(app)

        res = client.post("/api/v1/inline-completion", json={
            "prefix": "def ",
            "suffix": "",
            "language": "python",
        })
        assert res.status_code == 200
        data = res.json()
        assert data["source"] == "rule"
        assert len(data["items"]) > 0  # 规则引擎仍返回补全

    def test_route_fim_success_returns_fim_source(self):
        """FIM 成功时 source=fim"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routes.completion import router as completion_router

        app = FastAPI()
        app.include_router(completion_router)

        # 注入 mock model_config_service——返回带 fim 配置的 model_cfg
        class _MockModelConfigService:
            async def get_active_config(self):
                return {
                    "provider": "deepseek",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "sk-test",
                    "fim": {"model": "deepseek-coder", "format": "deepseek"},
                }

        app.state.model_config_service = _MockModelConfigService()
        client = TestClient(app)

        # mock FIM 调用返回成功
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"choices": [{"text": "    return 'fim'"}]}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            res = client.post("/api/v1/inline-completion", json={
                "prefix": "def fim_func():\n",
                "suffix": "",
                "language": "python",
            })

        assert res.status_code == 200
        data = res.json()
        assert data["source"] == "fim"
        assert len(data["items"]) == 1
        assert "return 'fim'" in data["items"][0]["insertText"]
