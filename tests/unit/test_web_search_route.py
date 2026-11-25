"""tests/unit/test_web_search_route.py —— Task 6.3: @web: 引用路由契约测试

GET /api/v1/web/search?q={query} 的分支:
1. 空查询 → ok:False, results:[]
2. OC 委托成功 (ok=True) → source=openclaw, results 注入
3. OC 委托成功 (success=True, 无 ok 键) → source=openclaw（对齐 delegate_tool 成功判定）
4. OC 委托失败 (ok=False) → 降级 url_hint
5. adapters 为空 → 降级 url_hint
6. OC 不在 adapters → 降级 url_hint
7. OC adapter 无 execute_tool → 降级 url_hint
8. delegate_tool 抛异常 → 降级 url_hint（异常被捕获）
9. URL hint 包含编码后的查询（google/bing/duckduckgo）
10. from_agent_id 从 context.get_current_agent_id() 获取
11. 特殊字符（空格/中文/&）正确 URL 编码
12. OC 返回 result 字段（list）→ 原样注入 results
13. OC 返回 results 字段 → 原样注入 results（result 字段缺失时）

路由用懒导入（函数内 from ... import），monkeypatch 模块级函数即可生效。
"""
import os
import sys
import urllib.parse

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.web_search import router as web_search_router


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(web_search_router)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


# ═══════════════════════════════════════════════════════════════
# 空查询契约
# ═══════════════════════════════════════════════════════════════

class TestEmptyQuery:
    """空查询 → ok:False, 不委托 OC"""

    def test_empty_string_returns_error(self, client):
        """q='' → ok:False, error='Query is empty'"""
        resp = client.get("/api/v1/web/search", params={"q": ""})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "Query is empty"
        assert body["results"] == []

    def test_whitespace_only_returns_error(self, client):
        """q='   '（纯空白）→ ok:False"""
        resp = client.get("/api/v1/web/search", params={"q": "   "})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "empty" in body["error"].lower()


# ═══════════════════════════════════════════════════════════════
# OC 委托成功契约
# ═══════════════════════════════════════════════════════════════

class TestDelegateSuccess:
    """OC web_search 委托成功 → source=openclaw"""

    def test_ok_key_success_with_result_field(self, client, monkeypatch):
        """OC 返回 ok=True + result=[...] → results 注入 result 字段"""
        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")

        async def _mock_delegate(**kwargs):
            return {"ok": True, "result": [
                {"title": "FastAPI docs", "url": "https://fastapi.tiangolo.com",
                 "snippet": "Modern Python web framework"},
            ]}
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        class _OCAdapter:
            async def execute_tool(self, tool_name, arguments):
                return {"ok": True}
        monkeypatch.setattr("adapters.registry.get_global_adapters",
                            lambda: {"openclaw": _OCAdapter()})

        resp = client.get("/api/v1/web/search", params={"q": "fastapi tutorial"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["source"] == "openclaw"
        assert body["query"] == "fastapi tutorial"
        assert len(body["results"]) == 1
        assert body["results"][0]["title"] == "FastAPI docs"

    def test_success_key_only_no_ok(self, client, monkeypatch):
        """OC 返回 success=True（无 ok 键）→ 判定成功（对齐 delegate_tool 逻辑）"""
        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")

        async def _mock_delegate(**kwargs):
            return {"success": True, "results": [
                {"title": "Result 1", "url": "https://example.com"},
            ]}
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        class _OCAdapter:
            async def execute_tool(self, tool_name, arguments):
                return {"ok": True}
        monkeypatch.setattr("adapters.registry.get_global_adapters",
                            lambda: {"openclaw": _OCAdapter()})

        resp = client.get("/api/v1/web/search", params={"q": "test"})
        body = resp.json()
        assert body["ok"] is True
        assert body["source"] == "openclaw"
        assert len(body["results"]) == 1

    def test_results_field_used_when_result_missing(self, client, monkeypatch):
        """OC 返回 ok=True 但只有 results 字段（无 result）→ results 注入"""
        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")

        async def _mock_delegate(**kwargs):
            return {"ok": True, "results": [
                {"title": "R1"}, {"title": "R2"},
            ]}
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        class _OCAdapter:
            async def execute_tool(self, tool_name, arguments):
                return {"ok": True}
        monkeypatch.setattr("adapters.registry.get_global_adapters",
                            lambda: {"openclaw": _OCAdapter()})

        resp = client.get("/api/v1/web/search", params={"q": "query"})
        body = resp.json()
        assert body["ok"] is True
        assert len(body["results"]) == 2

    def test_delegate_arguments_correct(self, client, monkeypatch):
        """委托参数：tool_name=web_search, to_agent_id=openclaw, arguments 含 query+num_results"""
        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")

        captured = {}

        async def _mock_delegate(**kwargs):
            captured.update(kwargs)
            return {"ok": True, "result": []}
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        class _OCAdapter:
            async def execute_tool(self, tool_name, arguments):
                return {"ok": True}
        monkeypatch.setattr("adapters.registry.get_global_adapters",
                            lambda: {"openclaw": _OCAdapter()})

        client.get("/api/v1/web/search", params={"q": "python asyncio"})

        assert captured["tool_name"] == "web_search"
        assert captured["to_agent_id"] == "openclaw"
        assert captured["from_agent_id"] == "hermes"
        assert captured["arguments"]["query"] == "python asyncio"
        assert captured["arguments"]["num_results"] == 5

    def test_from_agent_id_defaults_to_unknown_when_context_none(self, client, monkeypatch):
        """context.get_current_agent_id() 返回 None → from_agent_id='unknown'"""
        monkeypatch.setattr("context.get_current_agent_id", lambda: None)

        captured = {}

        async def _mock_delegate(**kwargs):
            captured.update(kwargs)
            return {"ok": True, "result": []}
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        class _OCAdapter:
            async def execute_tool(self, tool_name, arguments):
                return {"ok": True}
        monkeypatch.setattr("adapters.registry.get_global_adapters",
                            lambda: {"openclaw": _OCAdapter()})

        client.get("/api/v1/web/search", params={"q": "test"})
        assert captured["from_agent_id"] == "unknown"


# ═══════════════════════════════════════════════════════════════
# 降级 URL hint 契约
# ═══════════════════════════════════════════════════════════════

class TestUrlHintFallback:
    """OC 不可用/失败 → 降级返回搜索 URL hint"""

    def test_adapters_empty_falls_back_to_url_hint(self, client, monkeypatch):
        """adapters 为空 {} → 降级 url_hint"""
        monkeypatch.setattr("adapters.registry.get_global_adapters", lambda: {})

        resp = client.get("/api/v1/web/search", params={"q": "fastapi"})
        body = resp.json()
        assert body["ok"] is True
        assert body["source"] == "url_hint"
        assert body["results"] == []
        assert "url_hint" in body
        assert "google" in body["url_hint"]
        assert "bing" in body["url_hint"]
        assert "duckduckgo" in body["url_hint"]

    def test_oc_not_in_adapters_falls_back(self, client, monkeypatch):
        """adapters 只有 hermes（无 openclaw）→ 降级"""
        monkeypatch.setattr("adapters.registry.get_global_adapters",
                            lambda: {"hermes": object()})

        resp = client.get("/api/v1/web/search", params={"q": "test"})
        body = resp.json()
        assert body["source"] == "url_hint"

    def test_oc_without_execute_tool_falls_back(self, client, monkeypatch):
        """OC adapter 无 execute_tool 方法 → 降级"""
        monkeypatch.setattr("adapters.registry.get_global_adapters",
                            lambda: {"openclaw": object()})

        resp = client.get("/api/v1/web/search", params={"q": "test"})
        body = resp.json()
        assert body["source"] == "url_hint"

    def test_delegate_failure_falls_back(self, client, monkeypatch):
        """OC 委托返回 ok=False → 降级 url_hint"""
        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")

        async def _mock_delegate(**kwargs):
            return {"ok": False, "error": "web_search tool not available"}
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        class _OCAdapter:
            async def execute_tool(self, tool_name, arguments):
                return {"ok": True}
        monkeypatch.setattr("adapters.registry.get_global_adapters",
                            lambda: {"openclaw": _OCAdapter()})

        resp = client.get("/api/v1/web/search", params={"q": "test"})
        body = resp.json()
        assert body["source"] == "url_hint"
        assert body["ok"] is True  # 降级不是错误

    def test_delegate_exception_falls_back(self, client, monkeypatch):
        """delegate_tool 抛异常 → 异常被捕获，降级 url_hint"""
        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")

        async def _mock_delegate(**kwargs):
            raise RuntimeError("network timeout")
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        class _OCAdapter:
            async def execute_tool(self, tool_name, arguments):
                return {"ok": True}
        monkeypatch.setattr("adapters.registry.get_global_adapters",
                            lambda: {"openclaw": _OCAdapter()})

        resp = client.get("/api/v1/web/search", params={"q": "test"})
        body = resp.json()
        assert body["source"] == "url_hint"
        assert body["ok"] is True

    def test_url_hint_contains_encoded_query(self, client, monkeypatch):
        """url_hint 三个搜索引擎 URL 包含正确编码的查询"""
        monkeypatch.setattr("adapters.registry.get_global_adapters", lambda: {})

        resp = client.get("/api/v1/web/search", params={"q": "fastapi tutorial"})
        body = resp.json()
        encoded = urllib.parse.quote("fastapi tutorial")
        assert body["url_hint"]["google"] == f"https://www.google.com/search?q={encoded}"
        assert body["url_hint"]["bing"] == f"https://www.bing.com/search?q={encoded}"
        assert body["url_hint"]["duckduckgo"] == f"https://duckduckgo.com/?q={encoded}"

    def test_special_chars_encoded_correctly(self, client, monkeypatch):
        """特殊字符（空格/中文/&）正确 URL 编码"""
        monkeypatch.setattr("adapters.registry.get_global_adapters", lambda: {})

        query = "Python & FastAPI 教程"
        resp = client.get("/api/v1/web/search", params={"q": query})
        body = resp.json()
        encoded = urllib.parse.quote(query)
        # 验证编码后无原始空格/中文/&（均被 percent-encode）
        assert body["url_hint"]["google"].endswith(f"q={encoded}")
        assert " " not in body["url_hint"]["google"].split("q=")[-1]
        assert body["query"] == query  # 原始查询保留在 query 字段

    def test_hint_message_present(self, client, monkeypatch):
        """降级时返回 hint 提示信息"""
        monkeypatch.setattr("adapters.registry.get_global_adapters", lambda: {})

        resp = client.get("/api/v1/web/search", params={"q": "test"})
        body = resp.json()
        assert "hint" in body
        assert isinstance(body["hint"], str)
        assert len(body["hint"]) > 0
