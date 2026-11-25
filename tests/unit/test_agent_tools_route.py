"""tests/unit/test_agent_tools_route.py —— Agent 工具清单接口的三种走向

原来适配器不支持列表就直接 400，结果：原生 yuncat（工具就在注册表里）
和外部 CLI 都拿不到清单，Agent 页只能显示报错。
"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class _Bare:
    """没有 list_available_tools 的适配器"""


class _Lister:
    async def list_available_tools(self):
        return [{"name": "web_search"}]


def _request(adapters):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(adapters=adapters)))


@pytest.mark.asyncio
async def test_adapter_listing_wins():
    from routes.agents import list_agent_tools

    result = await list_agent_tools("openclaw", _request({"openclaw": _Lister()}))

    assert result["source"] == "adapter"
    assert result["tools"][0]["name"] == "web_search"


@pytest.mark.asyncio
async def test_native_agent_falls_back_to_registry():
    """原生 yuncat 的工具在 Yuncat 注册表里"""
    from conftest import load_all_builtin_tools
    from routes.agents import list_agent_tools

    load_all_builtin_tools()

    result = await list_agent_tools("yuncat", _request({"yuncat": _Bare()}))

    assert result["source"] == "registry"
    assert result["tools"], "原生 Agent 应能列出自己的工具"


@pytest.mark.asyncio
async def test_cli_agent_gets_empty_list_not_400():
    from routes.agents import list_agent_tools

    result = await list_agent_tools("codex", _request({"codex": _Bare()}))

    assert result["ok"] is True
    assert result["tools"] == []
    assert result["note"], "要告诉前端为什么是空的"


@pytest.mark.asyncio
async def test_unknown_agent_still_404():
    from fastapi import HTTPException

    from routes.agents import list_agent_tools

    with pytest.raises(HTTPException) as exc:
        await list_agent_tools("nope", _request({}))
    assert exc.value.status_code == 404
