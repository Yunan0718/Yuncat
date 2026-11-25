"""system_tool_browser.py —— 内置浏览器工具（AI 可操控的那一半）

对标的形态：Codex / Claude 内置浏览器、Playwright MCP 的 ref 语义——先
`browser_snapshot` 拿到带编号的可交互元素，再 `browser_click(ref)` / `browser_type(ref, text)`。

为什么不做成「AI 自己开一个无头浏览器」：用户需要**看得见** AI 在做什么，也需要随时接手。
所以浏览器窗口住在 Electron 主进程里，人类可以直接在窗口里点、输、切标签页；
这里所有工具都通过 `services/host_bridge` 打到同一个窗口，两边操作的是同一批标签页。

能力边界：工具名都含 `browser`，因此受 `config.CAPABILITY_FLAGS` 的 `browser` 开关约束
（轻量档默认关闭，标准/强力档开启）。页面跑在独立 session partition 里，
拿不到应用的 cookie 与本地文件，`file:`/`javascript:` 协议一律被宿主拒绝。

`browser_eval` 会执行任意页面脚本，因此声明为 confirmable——需要用户点头。
"""
from __future__ import annotations

import base64
import binascii
import logging
import secrets
from datetime import datetime
from pathlib import Path

from config import (
    DATA_DIR, DEFAULT_AGENT_ID, ERROR_PREVIEW_CHARS, HERMES_AGENT_ID, OPENCLAW_AGENT_ID,
)
from services import host_bridge
from ..registry import tool

_log = logging.getLogger("yuncat.tools.browser")

# 原生 agent 可见全部工具；hermes / openclaw 通过适配器调用同一批工具时同样开放
_BROWSER_AGENTS = [DEFAULT_AGENT_ID, HERMES_AGENT_ID, OPENCLAW_AGENT_ID]
_GROUPS = ["browser"]

# 截图落盘位置与上限（人为上限，可调）：放在数据目录而不是工作区，避免污染用户仓库
BROWSER_SHOT_DIR_NAME = "browser-shots"
BROWSER_SHOT_MAX_BYTES = 24 * 1024 * 1024
# 快照大纲的行数上限：模型一次看不下整页，超出部分靠 margin 提示重新快照
BROWSER_SNAPSHOT_OUTLINE_MAX_LINES = 150


def _fail(tool_name: str, error: str) -> dict:
    from tools.result import ToolResult

    return ToolResult(
        success=False,
        error=str(error)[:ERROR_PREVIEW_CHARS],
        meta={"tool_name": tool_name},
    ).to_dict()


def _ok(tool_name: str, data, observation: str) -> dict:
    from tools.result import ToolResult

    return ToolResult(
        success=True, data=data, observation=observation[:200], meta={"tool_name": tool_name},
    ).to_dict()


async def _invoke(tool_name: str, action: str, params: dict | None = None, *, timeout: float | None = None):
    """统一调用入口：返回 (data, error)，error 非空即失败。"""
    result = await host_bridge.call(action, params or {}, timeout=timeout)
    if result.get("ok"):
        return result.get("data"), ""
    return None, result.get("error") or "内置浏览器调用失败"


def _outline(snapshot: dict) -> list[str]:
    """把快照压成「[ref] role "name" 状态」的短行，便于模型直接挑编号。"""
    nodes = snapshot.get("nodes") or []
    lines = [f'页面：{snapshot.get("title") or "(无标题)"} — {snapshot.get("url") or ""}']
    for node in nodes[:BROWSER_SNAPSHOT_OUTLINE_MAX_LINES]:
        state = []
        if node.get("disabled"):
            state.append("已禁用")
        if node.get("checked"):
            state.append("已勾选")
        if not node.get("in_viewport", True):
            state.append("在视口外")
        suffix = f" ({', '.join(state)})" if state else ""
        lines.append(f'[{node.get("ref")}] {node.get("role")} "{node.get("name")}"{suffix}')
    total = snapshot.get("total_candidates")
    if snapshot.get("truncated"):
        lines.append(f"... 共 {total} 个候选元素，本次只列出前 {len(nodes)} 个")
    return lines


def _store_screenshot(encoded: str) -> tuple[str, int]:
    """把 base64 截图写入数据目录，返回 (绝对路径, 字节数)。失败由调用方转成错误。"""
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) > BROWSER_SHOT_MAX_BYTES:
        raise ValueError(f"截图过大（{len(raw)} bytes，上限 {BROWSER_SHOT_MAX_BYTES}）")
    directory = Path(DATA_DIR) / BROWSER_SHOT_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    name = f"shot-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}.png"
    target = directory / name
    target.write_bytes(raw)
    return str(target), len(raw)


@tool(
    name="browser_open", agents=_BROWSER_AGENTS,
    description=(
        "Open the built-in browser window (or focus it if it is already open). Pass a url to "
        "navigate the active tab there. USE as the first browser step when no page is open yet. "
        "The window is visible to the user, who can take over clicking and typing at any time."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Optional http/https URL to open."},
        },
    },
    is_read_only=False, concurrency_safety="exclusive", groups=_GROUPS, tool_timeout=60,
)
async def browser_open(url: str = "") -> dict:
    data, error = await _invoke("browser_open", "open", {"url": url or ""})
    if error:
        return _fail("browser_open", error)
    tabs = (data or {}).get("tabs") or []
    return _ok("browser_open", data, f"内置浏览器已就绪，当前 {len(tabs)} 个标签页。")


@tool(
    name="browser_navigate", agents=_BROWSER_AGENTS,
    description=(
        "Navigate the active browser tab to a URL (https:// is assumed when the scheme is missing). "
        "Only http/https is allowed. Returns the final URL and whether the page finished loading. "
        "After navigating, call browser_snapshot before clicking anything."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Target URL, e.g. 'example.com' or 'https://example.com/a'."},
        },
        "required": ["url"],
    },
    is_read_only=False, concurrency_safety="exclusive", groups=_GROUPS, tool_timeout=120,
)
async def browser_navigate(url: str = "") -> dict:
    if not (url or "").strip():
        return _fail("browser_navigate", "url 不能为空")
    data, error = await _invoke("browser_navigate", "navigate", {"url": url}, timeout=host_bridge.HOST_BRIDGE_LONG_TIMEOUT_SEC)
    if error:
        return _fail("browser_navigate", error)
    loaded = (data or {}).get("loaded")
    return _ok("browser_navigate", data,
               f"已导航到 {(data or {}).get('url') or url}{'' if loaded else '（页面仍在加载）'}。")


@tool(
    name="browser_snapshot", agents=_BROWSER_AGENTS,
    description=(
        "Read the current page as a numbered list of interactive elements (ref / role / name / state), "
        "plus the visible text. THIS IS THE PREFERRED WAY TO SEE A PAGE. "
        "Use the returned ref numbers with browser_click / browser_type. "
        "Call it again after any navigation or DOM change — old refs become invalid."
    ),
    parameters={
        "type": "object",
        "properties": {
            "max_nodes": {"type": "integer", "description": "Max interactive elements to list (default 120, max 600)."},
            "include_text": {"type": "boolean", "description": "Include visible page text (default true)."},
            "max_text_chars": {"type": "integer", "description": "Cap for the visible text (default 6000)."},
        },
    },
    is_read_only=True, concurrency_safety="exclusive", groups=_GROUPS, tool_timeout=60,
)
async def browser_snapshot(max_nodes: int = 120, include_text: bool = True, max_text_chars: int = 6000) -> dict:
    data, error = await _invoke("browser_snapshot", "snapshot", {
        "maxNodes": max_nodes, "includeText": include_text, "maxTextChars": max_text_chars,
    })
    if error:
        return _fail("browser_snapshot", error)
    snapshot = data or {}
    snapshot["outline"] = _outline(snapshot)
    nodes = snapshot.get("nodes") or []
    return _ok("browser_snapshot", snapshot, f"页面快照：{len(nodes)} 个可交互元素。")


@tool(
    name="browser_click", agents=_BROWSER_AGENTS,
    description=(
        "Click an element with a real mouse event (dispatchMouseEvent), so overlays, custom dropdowns "
        "and canvas pages behave like a human click. Pass ref from browser_snapshot, or a CSS selector. "
        "Fails with a clear hint when the ref is stale — re-run browser_snapshot then."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ref": {"type": "integer", "description": "Element ref from browser_snapshot."},
            "selector": {"type": "string", "description": "Alternative CSS selector when no ref is available."},
            "button": {"type": "string", "enum": ["left", "right"], "description": "Mouse button (default left)."},
        },
    },
    is_read_only=False, concurrency_safety="exclusive", groups=_GROUPS, tool_timeout=60,
)
async def browser_click(ref: int | None = None, selector: str = "", button: str = "left") -> dict:
    if ref is None and not selector:
        return _fail("browser_click", "需要给出 ref（来自 browser_snapshot）或 selector")
    data, error = await _invoke("browser_click", "click", {"ref": ref, "selector": selector, "button": button})
    if error:
        return _fail("browser_click", error)
    clicked = (data or {}).get("clicked") or {}
    target = clicked.get("type") or clicked.get("tag") or "元素"
    return _ok("browser_click", data, f"已点击 {target}。")


@tool(
    name="browser_type", agents=_BROWSER_AGENTS,
    description=(
        "Type text into the focused element, or focus an element first via ref/selector then type. "
        "Uses Input.insertText, which produces genuine input events (React/Vue controlled inputs work). "
        "Set submit=true to press Enter afterwards."
    ),
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to insert."},
            "ref": {"type": "integer", "description": "Element ref to focus before typing."},
            "selector": {"type": "string", "description": "Alternative CSS selector to focus before typing."},
            "submit": {"type": "boolean", "description": "Press Enter after typing (default false)."},
        },
        "required": ["text"],
    },
    is_read_only=False, concurrency_safety="exclusive", groups=_GROUPS, tool_timeout=60,
)
async def browser_type(text: str = "", ref: int | None = None, selector: str = "", submit: bool = False) -> dict:
    data, error = await _invoke("browser_type", "type", {
        "text": text, "ref": ref, "selector": selector, "submit": submit,
    })
    if error:
        return _fail("browser_type", error)
    return _ok("browser_type", data,
               f"已输入 {len(text or '')} 个字符{('并回车' if submit else '')}。")


@tool(
    name="browser_press", agents=_BROWSER_AGENTS,
    description=(
        "Press a single key in the page: Enter, Tab, Escape, Backspace, ArrowUp/Down/Left/Right, "
        "PageUp/PageDown, Home, End, Delete, Space or a single character. Optional modifiers: "
        "ctrl / alt / shift / meta."
    ),
    parameters={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Key name, e.g. 'Enter' or 'Escape'."},
            "modifiers": {
                "type": "array", "items": {"type": "string"},
                "description": "Modifier keys held during the press, e.g. ['ctrl'].",
            },
        },
        "required": ["key"],
    },
    is_read_only=False, concurrency_safety="exclusive", groups=_GROUPS, tool_timeout=30,
)
async def browser_press(key: str = "", modifiers: list | None = None) -> dict:
    data, error = await _invoke("browser_press", "press", {"key": key, "modifiers": modifiers or []})
    if error:
        return _fail("browser_press", error)
    return _ok("browser_press", data, f"已按下 {key}。")


@tool(
    name="browser_scroll", agents=_BROWSER_AGENTS,
    description=(
        "Scroll the page with a real mouse wheel. direction=down/up/left/right scrolls by `amount` "
        "pixels; direction=top/bottom jumps to the page start/end (use it to load infinite-scroll lists)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "direction": {"type": "string", "enum": ["down", "up", "left", "right", "top", "bottom"]},
            "amount": {"type": "integer", "description": "Pixels for down/up/left/right (default 600)."},
        },
    },
    is_read_only=True, concurrency_safety="exclusive", groups=_GROUPS, tool_timeout=30,
)
async def browser_scroll(direction: str = "down", amount: int = 600) -> dict:
    direction = (direction or "down").lower()
    if direction in ("top", "bottom"):
        data, error = await _invoke("browser_scroll", "scroll", {"to": direction})
    else:
        dx = amount if direction == "right" else (-amount if direction == "left" else 0)
        dy = amount if direction == "down" else (-amount if direction == "up" else 0)
        data, error = await _invoke("browser_scroll", "scroll", {"dx": dx, "dy": dy})
    if error:
        return _fail("browser_scroll", error)
    return _ok("browser_scroll", data, f"已滚动（{direction}）。")


@tool(
    name="browser_read_page", agents=_BROWSER_AGENTS,
    description=(
        "Get the current URL, title and visible text of the page. Cheaper than browser_snapshot when you "
        "only need the readable content (articles, docs, search results)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "max_chars": {"type": "integer", "description": "Max characters of page text (default 12000)."},
        },
    },
    is_read_only=True, concurrency_safety="exclusive", groups=_GROUPS, tool_timeout=60,
)
async def browser_read_page(max_chars: int = 12000) -> dict:
    data, error = await _invoke("browser_read_page", "read", {"max_chars": max_chars})
    if error:
        return _fail("browser_read_page", error)
    text_len = len((data or {}).get("text") or "")
    return _ok("browser_read_page", data, f"已读取页面文本 {text_len} 字符。")


@tool(
    name="browser_screenshot", agents=_BROWSER_AGENTS,
    description=(
        "Capture a PNG screenshot of the visible area (or the full page with full_page=true) and save it "
        "to the app data directory. Returns the absolute file path, which you can then inspect with "
        "vision_analyze. Use it only when layout/visuals matter — browser_snapshot is cheaper."
    ),
    parameters={
        "type": "object",
        "properties": {
            "full_page": {"type": "boolean", "description": "Capture beyond the viewport (default false)."},
        },
    },
    is_read_only=True, concurrency_safety="exclusive", groups=_GROUPS, tool_timeout=120,
)
async def browser_screenshot(full_page: bool = False) -> dict:
    data, error = await _invoke("browser_screenshot", "screenshot", {"full_page": full_page},
                                timeout=host_bridge.HOST_BRIDGE_LONG_TIMEOUT_SEC)
    if error:
        return _fail("browser_screenshot", error)
    encoded = (data or {}).get("base64") or ""
    try:
        path, size = _store_screenshot(encoded)
    except (ValueError, binascii.Error) as exc:
        _log.warning("screenshot store failed: %s", exc)
        return _fail("browser_screenshot", f"截图保存失败：{exc}")
    payload = {"path": path, "bytes": size, "full_page": bool(full_page)}
    return _ok("browser_screenshot", payload, f"截图已保存：{path}")


@tool(
    name="browser_wait_for", agents=_BROWSER_AGENTS,
    description=(
        "Wait until a CSS selector appears or the page text contains a string (polling, up to timeout_ms). "
        "USE after actions that trigger async loading, before taking a new snapshot."
    ),
    parameters={
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS selector to wait for."},
            "text": {"type": "string", "description": "Substring that must appear in the page text."},
            "timeout_ms": {"type": "integer", "description": "Max wait in ms (default 8000, max 60000)."},
        },
    },
    is_read_only=True, concurrency_safety="exclusive", groups=_GROUPS, tool_timeout=90,
)
async def browser_wait_for(selector: str = "", text: str = "", timeout_ms: int = 8000) -> dict:
    if not selector and not text:
        return _fail("browser_wait_for", "需要 selector 或 text 之一")
    data, error = await _invoke("browser_wait_for", "wait_for", {
        "selector": selector, "text": text, "timeoutMs": timeout_ms,
    }, timeout=host_bridge.HOST_BRIDGE_LONG_TIMEOUT_SEC)
    if error:
        return _fail("browser_wait_for", error)
    found = bool((data or {}).get("found"))
    return _ok("browser_wait_for", data, "条件已满足。" if found else "等待超时，条件未满足。")


@tool(
    name="browser_tabs", agents=_BROWSER_AGENTS,
    description=(
        "Manage browser tabs. action=list returns every tab with its id/url/title; "
        "action=new opens a tab (optional url); action=activate switches to tab_id; action=close closes it."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "new", "activate", "close"]},
            "tab_id": {"type": "integer", "description": "Tab id for activate/close."},
            "url": {"type": "string", "description": "Optional URL for action=new."},
        },
        "required": ["action"],
    },
    is_read_only=False, concurrency_safety="exclusive", groups=_GROUPS, tool_timeout=60,
)
async def browser_tabs(action: str = "list", tab_id: int | None = None, url: str = "") -> dict:
    data, error = await _invoke("browser_tabs", "tabs", {"action": action, "tab_id": tab_id, "url": url})
    if error:
        return _fail("browser_tabs", error)
    tabs = (data or {}).get("tabs")
    summary = f"标签动作 {action} 完成" + (f"，当前 {len(tabs)} 个标签页" if tabs is not None else "")
    return _ok("browser_tabs", data, summary)


@tool(
    name="browser_history", agents=_BROWSER_AGENTS,
    description=(
        "Browser history control for the active tab: action=back / forward / reload. "
        "Tells you honestly when there is nothing to go back or forward to."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["back", "forward", "reload"]},
        },
        "required": ["action"],
    },
    is_read_only=False, concurrency_safety="exclusive", groups=_GROUPS, tool_timeout=120,
)
async def browser_history(action: str = "back") -> dict:
    if action not in ("back", "forward", "reload"):
        return _fail("browser_history", f"不支持的动作：{action}（可用 back/forward/reload）")
    data, error = await _invoke("browser_history", action, {},
                                timeout=host_bridge.HOST_BRIDGE_LONG_TIMEOUT_SEC)
    if error:
        return _fail("browser_history", error)
    if action != "reload" and (data or {}).get("moved") is False:
        return _ok("browser_history", data, (data or {}).get("reason") or "没有可跳转的历史记录。")
    return _ok("browser_history", data, f"已执行 {action}。")


@tool(
    name="browser_eval", agents=_BROWSER_AGENTS, confirmable=True,
    description=(
        "Evaluate a JavaScript expression inside the current page and return its value (JSON-serialisable). "
        "POWERFUL AND UNSAFE: it runs arbitrary page script, so the user must confirm every call. "
        "Prefer browser_snapshot / browser_click / browser_read_page; use this only when the page needs "
        "custom scripting that no other browser tool covers."
    ),
    parameters={
        "type": "object",
        "properties": {
            "expression": {"type": "string", "description": "JavaScript expression, e.g. 'document.title'."},
        },
        "required": ["expression"],
    },
    is_read_only=False, concurrency_safety="exclusive", groups=_GROUPS, tool_timeout=60,
)
async def browser_eval(expression: str = "") -> dict:
    if not (expression or "").strip():
        return _fail("browser_eval", "expression 不能为空")
    data, error = await _invoke("browser_eval", "eval", {"expression": expression})
    if error:
        return _fail("browser_eval", error)
    return _ok("browser_eval", data, "脚本已执行，结果见 data。")
