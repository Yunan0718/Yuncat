"""tests/contract/test_inline_completion.py —— /api/v1/inline-completion 契约测试

Phase 49-O: 验证 Monaco 行内补全端点的输入输出契约。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class TestInlineCompletionContract:
    """Monaco inline completion 端点契约"""

    @pytest.fixture
    def client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routes.completion import router as completion_router

        app = FastAPI()
        app.include_router(completion_router)
        return TestClient(app)

    def test_inline_completion_returns_items_field(self, client):
        """POST /api/v1/inline-completion 返回包含 items 数组的 JSON"""
        res = client.post("/api/v1/inline-completion", json={
            "prefix": 'name = "foo"\nname = ',
            "suffix": "",
            "language": "python",
        })
        assert res.status_code == 200
        data = res.json()
        assert "items" in data
        assert isinstance(data["items"], list)
        if data["items"]:
            assert "insertText" in data["items"][0]

    def test_inline_completion_keyword_skeleton(self, client):
        """前缀匹配关键词时返回代码骨架"""
        res = client.post("/api/v1/inline-completion", json={
            "prefix": "def ",
            "suffix": "",
            "language": "python",
        })
        assert res.status_code == 200
        data = res.json()
        assert any("def name(self):" in item["insertText"] for item in data["items"])

    def test_inline_completion_defaults_language(self, client):
        """未传 language 时默认使用 plaintext 并返回合法结构"""
        res = client.post("/api/v1/inline-completion", json={
            "prefix": "some code",
            "suffix": "",
        })
        assert res.status_code == 200
        data = res.json()
        assert "items" in data
        assert isinstance(data["items"], list)
