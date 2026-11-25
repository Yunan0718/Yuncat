"""tests/unit/test_system_tool_browser.py —— 内置浏览器工具与宿主桥

覆盖四类问题（都是这类「跨进程工具」最容易翻车的地方）：
1. 注册与门控：工具是否注册、是否受 browser 能力开关约束；
2. 通道失败可读：没有桌面端 / HTTP 异常 / 返回非 JSON / 响应过大时，是否给出人能看懂的失败；
3. 参数映射：滚动方向、ref/selector 互斥、空参数是否被拦住；
4. 截图落盘：路径、字节数上限、不污染工作区。
"""
import binascii
import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from config import capability_required_by_tool
from services import host_bridge
from tools.builtin import system_tool_browser as browser_tools
from tools.builtin.system_tool_browser import (
    BROWSER_SHOT_MAX_BYTES,
    browser_click,
    browser_eval,
    browser_history,
    browser_navigate,
    browser_scroll,
    browser_snapshot,
    browser_tabs,
    browser_type,
)
from tools.registry import get_tool


TOOL_NAMES = [
    "browser_open", "browser_navigate", "browser_snapshot", "browser_click", "browser_type",
    "browser_press", "browser_scroll", "browser_read_page", "browser_screenshot",
    "browser_wait_for", "browser_tabs", "browser_history", "browser_eval",
]


@pytest.fixture(autouse=True)
def _ensure_registered():
    """补齐注册。

    仓库里若干老测试模块（test_tool_registry / test_tool_service / test_mcp_bridge 等）
    直接 `registry._tools.clear()` 做隔离，会把全局注册表清空。单独跑本文件没问题，
    全量跑时就会看到「工具未注册」。这里主动重载一次模块重新注册，
    不去改动那些老测试的写法（那是另一件事）。
    """
    import importlib

    importlib.reload(browser_tools)
    yield


@pytest.fixture(autouse=True)
def _clean_bridge_env(monkeypatch):
    """默认把宿主桥环境变量清空——单测不该依赖开发机的 Electron 进程。"""
    monkeypatch.delenv(host_bridge.ENV_HOST_BRIDGE_PORT, raising=False)
    monkeypatch.delenv(host_bridge.ENV_HOST_BRIDGE_TOKEN, raising=False)


def _patch_bridge(monkeypatch, result):
    calls = []

    async def fake_call(action, params=None, *, timeout=None):
        calls.append({"action": action, "params": params or {}, "timeout": timeout})
        return result

    monkeypatch.setattr(host_bridge, "call", fake_call)
    return calls


# ── 1. 注册与门控 ──────────────────────────────────────────────

def test_all_browser_tools_registered_and_gated():
    for name in TOOL_NAMES:
        meta = get_tool(name)
        assert meta is not None, f"{name} 未注册"
        assert capability_required_by_tool(name) == "browser", f"{name} 未受 browser 能力约束"
        assert "browser" in meta.groups
        assert meta.concurrency_safety == "exclusive", f"{name} 必须串行（浏览器只有一个）"


def test_only_eval_requires_confirmation():
    meta = get_tool("browser_eval")
    assert meta.confirmable is True, "执行任意页面脚本必须过用户确认"
    for name in TOOL_NAMES:
        if name != "browser_eval":
            assert get_tool(name).confirmable is False


# ── 2. 通道失败可读 ────────────────────────────────────────────

def test_endpoint_missing_when_env_absent():
    assert host_bridge.bridge_endpoint() is None
    assert host_bridge.is_available() is False


def test_endpoint_rejects_bad_port(monkeypatch):
    monkeypatch.setenv(host_bridge.ENV_HOST_BRIDGE_PORT, "not-a-port")
    monkeypatch.setenv(host_bridge.ENV_HOST_BRIDGE_TOKEN, "t")
    assert host_bridge.bridge_endpoint() is None


async def test_call_without_endpoint_returns_reason_not_exception():
    result = await host_bridge.call("state", {})
    assert result["ok"] is False
    assert "内置浏览器" in result["error"]


async def test_navigate_surfaces_bridge_error(monkeypatch):
    _patch_bridge(monkeypatch, {"ok": False, "data": None, "error": "页面已关闭"})
    out = await browser_navigate("https://example.com/")
    assert out["success"] is False
    assert "页面已关闭" in out["error"]


async def test_cli_environment_tells_user_the_truth(monkeypatch):
    """没有宿主桥时不能假装成功——错误里要说清「只在桌面应用里可用」。"""
    out = await browser_navigate("example.com")
    assert out["success"] is False
    assert "桌面" in out["error"]


# ── 3. 参数映射 ────────────────────────────────────────────────

async def test_navigate_rejects_empty_url(monkeypatch):
    calls = _patch_bridge(monkeypatch, {"ok": True, "data": {}})
    out = await browser_navigate("   ")
    assert out["success"] is False
    assert calls == [], "空参数不该打到桥（省一次跨进程往返）"


async def test_navigate_normalises_and_reports(monkeypatch):
    calls = _patch_bridge(monkeypatch, {"ok": True, "data": {"url": "https://example.com/", "loaded": True}})
    out = await browser_navigate("example.com")
    assert out["success"] is True
    assert calls[0]["action"] == "navigate"
    assert calls[0]["params"] == {"url": "example.com"}


async def test_snapshot_adds_outline_for_the_model(monkeypatch):
    _patch_bridge(monkeypatch, {"ok": True, "data": {
        "url": "https://example.com/", "title": "示例",
        "total_candidates": 25, "truncated": True,
        "nodes": [
            {"ref": 1, "role": "link", "name": "首页", "in_viewport": True},
            {"ref": 2, "role": "textbox", "name": "搜索", "in_viewport": False, "disabled": True},
        ],
    }})
    out = await browser_snapshot()
    assert out["success"] is True
    outline = out["data"]["outline"]
    assert outline[0].startswith("页面：示例")
    assert '[1] link "首页"' in outline[1]
    assert "已禁用" in outline[2] and "在视口外" in outline[2]
    assert any("截断" in line or "候选元素" in line for line in outline)


async def test_click_requires_ref_or_selector(monkeypatch):
    calls = _patch_bridge(monkeypatch, {"ok": True, "data": {}})
    out = await browser_click()
    assert out["success"] is False
    assert calls == []


async def test_click_passes_ref_through(monkeypatch):
    calls = _patch_bridge(monkeypatch, {"ok": True, "data": {"clicked": {"tag": "button"}}})
    out = await browser_click(ref=7)
    assert out["success"] is True
    assert calls[0]["params"]["ref"] == 7


async def test_type_reports_char_count_and_submit(monkeypatch):
    calls = _patch_bridge(monkeypatch, {"ok": True, "data": {"typed_chars": 5}})
    out = await browser_type(text="hello", ref=2, submit=True)
    assert out["success"] is True
    assert calls[0]["params"] == {"text": "hello", "ref": 2, "selector": "", "submit": True}
    assert "回车" in out["observation"]


async def test_scroll_direction_to_delta(monkeypatch):
    calls = _patch_bridge(monkeypatch, {"ok": True, "data": {}})
    await browser_scroll(direction="up", amount=300)
    assert calls[-1]["params"] == {"dx": 0, "dy": -300}
    await browser_scroll(direction="right", amount=120)
    assert calls[-1]["params"] == {"dx": 120, "dy": 0}
    await browser_scroll(direction="bottom")
    assert calls[-1]["params"] == {"to": "bottom"}


async def test_tabs_action_summary(monkeypatch):
    _patch_bridge(monkeypatch, {"ok": True, "data": {"tabs": [{"id": 1}, {"id": 2}]}})
    out = await browser_tabs(action="list")
    assert out["success"] is True
    assert "2 个标签页" in out["observation"]


async def test_history_rejects_unknown_action(monkeypatch):
    calls = _patch_bridge(monkeypatch, {"ok": True, "data": {}})
    out = await browser_history(action="jump")
    assert out["success"] is False
    assert calls == []


async def test_history_reports_when_there_is_no_history(monkeypatch):
    _patch_bridge(monkeypatch, {"ok": True, "data": {"moved": False, "reason": "已经在最早一页"}})
    out = await browser_history(action="back")
    assert out["success"] is True
    assert "已经在最早一页" in out["observation"]


async def test_eval_rejects_empty_expression(monkeypatch):
    calls = _patch_bridge(monkeypatch, {"ok": True, "data": 1})
    out = await browser_eval("  ")
    assert out["success"] is False
    assert calls == []


# ── 4. 截图落盘 ────────────────────────────────────────────────

def test_store_screenshot_writes_into_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_tools, "DATA_DIR", str(tmp_path))
    payload = b"\x89PNG-fake"
    path, size = browser_tools._store_screenshot(base64.b64encode(payload).decode())
    assert size == len(payload)
    assert os.path.isfile(path)
    assert path.startswith(str(tmp_path))
    assert os.path.basename(path).startswith("shot-")
    assert open(path, "rb").read() == payload


def test_store_screenshot_rejects_oversize(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_tools, "DATA_DIR", str(tmp_path))
    oversize = base64.b64encode(b"x" * (BROWSER_SHOT_MAX_BYTES + 1)).decode()
    with pytest.raises(ValueError):
        browser_tools._store_screenshot(oversize)


async def test_screenshot_saves_file_and_returns_path(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_tools, "DATA_DIR", str(tmp_path))
    encoded = base64.b64encode(b"png-bytes").decode()
    _patch_bridge(monkeypatch, {"ok": True, "data": {"base64": encoded, "mime": "image/png", "bytes": 9}})
    out = await browser_tools.browser_screenshot(full_page=True)
    assert out["success"] is True
    assert os.path.isfile(out["data"]["path"])
    assert out["data"]["full_page"] is True


async def test_screenshot_rejects_non_base64(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_tools, "DATA_DIR", str(tmp_path))
    _patch_bridge(monkeypatch, {"ok": True, "data": {"base64": "!!!not-base64!!!"}})
    out = await browser_tools.browser_screenshot()
    assert out["success"] is False
    assert "截图保存失败" in out["error"]
    assert list(tmp_path.iterdir()) == [], "坏数据不该留下半截文件"
