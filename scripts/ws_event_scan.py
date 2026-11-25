"""scripts/ws_event_scan.py —— 后端 WS 事件 → 前端 handler 反向扫描

问题形态（2026-10-22 实测过两例）：
- `tool_output_chunk`：后端从 Phase 61 起就在流式推，前端 wsHandlers 从没注册 →
  命令实时输出只在后端存在，界面永远看不到；
- `memory_saved`：`#记住` 写完记忆后后端回执，前端没接 → 记忆存了但界面毫无反应。

这类问题的共同点：**后端发了、前端没接，消息被静默丢弃，不报错也不掉线**。
正向的"前端注册了 handler、后端确实会发"由 `test_ws_event_surface.py` 的另一半守住。

扫描口径（刻意收窄，避免把内部事件也算进来）：
- `send_json(...)` / `safe_send(...)` / `_safe_send(...)` → 直接发到前端，算；
- `event_bus.publish(...)` → 只有被 lifespan 转发到 WS 的那几个才算（当前是
  tool_start / tool_done / tool_output_chunk，已在前端注册），其余是内部事件，
  不进本扫描——否则通知中心、内部编排事件会淹没清单。

用法：``python scripts/ws_event_scan.py`` 打印后端发了但前端没接的事件。
"""
from __future__ import annotations

import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(ROOT, "src-python")
FRONTEND_HANDLERS = os.path.join(
    ROOT, "src-frontend", "src", "hooks", "wsHandlers", "index.ts",
)

SKIP_DIRS = {"__pycache__", "python-embed", "node-embed", ".venv", "node_modules"}
# 直接发往前端的消息发送函数（_safe_ws 是 long_task 里 safe_send 的薄包装）
_SEND_FUNCS = {"send_json", "safe_send", "_safe_send", "_safe_ws"}
# event_bus 里确实会被转发到 WS 的事件（其余是内部事件，不进扫描）
_BUS_TO_WS = {"tool_start", "tool_done", "tool_output_chunk"}


def _callee_name(func: ast.AST) -> str:
    """取调用的最后一段名字（ws.send_json / ag._safe_send / bus.publish）。"""
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _dict_type_value(node: ast.AST) -> str | None:
    """从 dict 字面量里取 "type" 的字符串值。"""
    if not isinstance(node, ast.Dict):
        return None
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and key.value == "type":
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
    return None


def _extract_event(call: ast.Call) -> str | None:
    """从一个发送调用里取出事件名。"""
    name = _callee_name(call.func)
    if name in _SEND_FUNCS:
        for arg in list(call.args) + [kw.value for kw in call.keywords]:
            event = _dict_type_value(arg)
            if event:
                return event
        return None
    if name == "publish":
        # publish(event_name, data)
        for arg in call.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                return arg.value if arg.value in _BUS_TO_WS else None
    return None


def collect_backend_events() -> dict[str, set[str]]:
    """返回 {事件名: {来源文件相对路径}}。"""
    out: dict[str, set[str]] = {}
    for dirpath, dirnames, filenames in os.walk(BACKEND_DIR):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
            except SyntaxError:
                continue
            rel = os.path.relpath(path, ROOT).replace("\\", "/")
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                event = _extract_event(node)
                if event:
                    out.setdefault(event, set()).add(rel)
    return out


def collect_frontend_events() -> set[str]:
    """返回前端已处理的事件类型。

    两处都要算：
    1. `hooks/wsHandlers/index.ts` 的 handler map（主 WS 通道）；
    2. 组件里自己接的 WS 消息（`msg.type === "xxx"`）——工作区/终端/LSP
       这些独立通道的事件在组件里处理，不在 handler map 里。
    """
    events: set[str] = set()
    try:
        text = open(FRONTEND_HANDLERS, encoding="utf-8").read()
    except OSError:
        text = ""
    match = re.search(r"const handlers:\s*Record<string,\s*Handler>\s*=\s*\{(.*?)\n\};", text, re.S)
    if match:
        events |= set(re.findall(r"^\s{2}([a-zA-Z_][\w]*)\s*:", match.group(1), re.M))
    for path in _frontend_sources():
        try:
            src = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        events |= set(re.findall(r"\btype\s*===\s*[\"']([a-zA-Z_][\w]*)[\"']", src))
    return events


def _frontend_sources() -> list[str]:
    """真正持有 WS 连接的前端文件。

    只认**自己 onmessage 的文件**（主 WS 的 useWebSocket、终端面板、LSP 客户端）。
    其它文件里的 `type === "xxx"` 多半是业务枚举（比如 Git 操作类型），
    混进来会让守卫失去意义——那等于"看起来处理过"。
    """
    out: list[str] = []
    base = os.path.join(ROOT, "src-frontend", "src")
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in {"node_modules", "__tests__"}]
        for fn in filenames:
            if not fn.endswith((".ts", ".tsx")):
                continue
            path = os.path.join(dirpath, fn)
            try:
                src = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            if "onmessage" in src:
                out.append(path)
    return out


def unhandled_events(
    backend: dict[str, set[str]] | None = None,
    frontend: set[str] | None = None,
) -> list[tuple[str, list[str]]]:
    """返回后端发了但前端没接的事件 [(事件名, 来源文件)]。"""
    backend = collect_backend_events() if backend is None else backend
    frontend = collect_frontend_events() if frontend is None else frontend
    out: list[tuple[str, list[str]]] = []
    for event, files in sorted(backend.items()):
        if event not in frontend:
            out.append((event, sorted(files)))
    return out


if __name__ == "__main__":
    for event, files in unhandled_events():
        print(f"{event}\t{', '.join(files)}")
