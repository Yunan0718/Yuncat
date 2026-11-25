"""adapters/sse_utils.py —— SSE（Server-Sent Events）流解析共享工具

M28 修复：从 base.py / hermes.py / openclaw.py 提取重复的 SSE 行解析逻辑。
三个适配器原先各自实现相同的 buffer 累积 + 行分割 + `data: ` 前缀剥离 +
`[DONE]` 检测 + JSON 解析，~20 行 × 3 = ~60 行重复代码。

设计参考：httpx-sse / aiohttp-sse-client——协议解析与业务逻辑分离。
本模块只负责 SSE 协议层（行解析 + data 前缀 + [DONE]），
delta 提取（reasoning_content / tool_calls / content / usage）留给各适配器。
"""
import json as _json
from typing import AsyncIterator


async def iter_sse_events(resp) -> AsyncIterator[dict | None]:
    """解析 OpenAI 兼容 SSE 流，yield 解析后的 JSON dict。

    遇到 ``data: [DONE]`` 时 yield ``None`` 表示流结束，随后停止迭代。
    JSON 解析失败的行静默跳过（对标 aiohttp-sse-client 行为）。

    用法::

        async for data in iter_sse_events(resp):
            if data is None:  # [DONE]
                return
            delta = data.get("choices", [{}])[0].get("delta", {})
            # ... 适配器各自的 delta 处理

    Note: 部分 OpenAI 兼容服务在 ``[DONE]`` 之后同一 chunk 内附带 usage 行。
    此非标准行为已废弃——usage 应在最后一个正常 chunk 中（``stream_options:
    {"include_usage": true}``）。各适配器在正常 chunk 中已捕获 usage，
    故本工具不刷新 ``[DONE]`` 后的残留 buffer。
    """
    buffer = ""
    async for chunk_bytes in resp.content.iter_any():
        buffer += chunk_bytes.decode(errors="replace")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str == "[DONE]":
                yield None
                return
            try:
                yield _json.loads(data_str)
            except _json.JSONDecodeError:
                continue


async def iter_sse_named_events(resp) -> AsyncIterator[tuple[str | None, dict | None]]:
    """带事件名的 SSE 解析：yield ``(event_name, data)``。

    为什么需要它：标准 OpenAI 流只有 ``data:`` 行，但 Hermes 的 API server 会发
    ``event: hermes.tool.progress`` 这类**具名事件**来报告工具执行进度
    （见 gateway/platforms/api_server.py 的 _write_sse_chat_completion）。
    旧的 iter_sse_events 会把这类事件的 data 当成普通 chat chunk——既读不出事件名，
    又会因为缺少 ``choices`` 被静默丢弃，导致外部 Agent 的“行动链”完全不可见。

    协议细节：``event:`` 行设置当前事件名，后续 ``data:`` 行携带负载；空行表示一个
    事件块结束，事件名复位为 None（标准 SSE 语义）。
    """
    buffer = ""
    event_name: str | None = None
    async for chunk_bytes in resp.content.iter_any():
        buffer += chunk_bytes.decode(errors="replace")
        while "\n" in buffer:
            raw_line, buffer = buffer.split("\n", 1)
            line = raw_line.rstrip("\r")
            stripped = line.strip()
            if not stripped:
                event_name = None
                continue
            if stripped.startswith(":"):
                continue  # SSE 注释 / keepalive
            if stripped.startswith("event:"):
                event_name = stripped[6:].strip() or None
                continue
            if not stripped.startswith("data:"):
                continue
            data_str = stripped[5:].lstrip()
            if data_str == "[DONE]":
                yield event_name, None
                return
            try:
                yield event_name, _json.loads(data_str)
            except _json.JSONDecodeError:
                continue
