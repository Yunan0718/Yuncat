"""services/memory/tool_message_codec.py —— tool 消息在 conversations 里的编码

conversations 表没有独立的 tool_call_id 列。工具结果行复用 `tool_calls_json`
存一个最小信封：

    {"tool_call_id": "call_abc"}

assistant 行仍存 tool_calls 数组。读取端据 role 区分：
- role == "tool" → 还原 tool_call_id；
- 其余 → 还原 tool_calls。

这样不新增 schema，也能让重启后的历史保持 assistant(tool_calls) + tool(result) 成对。
"""
from __future__ import annotations

import json


def encode_tool_meta(tool_calls: list[dict] | None, tool_call_id: str | None) -> str | None:
    """把 assistant.tool_calls 或 tool.tool_call_id 编码进同一列。"""
    if tool_call_id:
        return json.dumps({"tool_call_id": tool_call_id}, ensure_ascii=False)
    if tool_calls:
        return json.dumps(tool_calls, ensure_ascii=False)
    return None


def decode_tool_meta(raw: str | None) -> tuple[str | None, list[dict] | None]:
    """返回 (tool_call_id, tool_calls)；无法解析时两者均为 None。"""
    if not raw:
        return None, None
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None, None
    if isinstance(data, dict):
        cid = data.get("tool_call_id")
        return (str(cid), None) if cid else (None, None)
    if isinstance(data, list):
        return None, data
    return None, None
