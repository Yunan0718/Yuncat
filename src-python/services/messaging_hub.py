"""services/messaging_hub.py —— Phase 51-F: Messaging Hub

将 OpenClaw 通讯网关能力（telegram/discord/slack/钉钉/企微/whatsapp 等）
统一包装为前端可调用的消息发送服务。
"""
from __future__ import annotations

import logging
from typing import Any

from config import OPENCLAW_AGENT_ID

_log = logging.getLogger("yuncat.messaging_hub")

# 平台到工具名的映射
PLATFORM_TOOL_MAP = {
    "telegram": "telegram_send",
    "discord": "discord_send",
    "slack": "slack_send",
    "dingtalk": "dingtalk_send",
    "wechat_work": "wechat_work_send",
    "whatsapp": "whatsapp_send",
}


def _resolve_tool_name(platform: str, use_openclaw_notify: bool) -> str:
    """根据平台解析要调用的工具名。"""
    if use_openclaw_notify:
        return "openclaw_notify"
    return PLATFORM_TOOL_MAP.get(platform, f"{platform}_send")


def _build_arguments(tool_name: str, platform: str, message: str, title: str | None = None) -> dict[str, Any]:
    """构造工具参数。"""
    if tool_name == "openclaw_notify":
        return {
            "platform": platform,
            "message": message,
            "title": title or "Yuncat 通知",
        }
    return {"message": f"{title}\n{message}" if title else message}


async def send_message(
    platform: str,
    message: str,
    title: str | None = None,
    tool_service=None,
    adapters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """通过 OpenClaw 向指定平台发送消息。

    优先使用 Yuncat ToolService（如果注入）；否则回退到直接调用 openclaw adapter。
    """
    if not message:
        return {"ok": False, "error": "message is required"}

    tool_name = _resolve_tool_name(platform, use_openclaw_notify=True)
    args = _build_arguments(tool_name, platform, message, title)

    # 优先走 ToolService（副作用唯一入口）
    if tool_service is not None:
        try:
            result = await tool_service.execute(tool_name, args, agent_id=OPENCLAW_AGENT_ID)
            return {"ok": bool(result.get("ok")), "platform": platform, "detail": result}
        except Exception as exc:
            _log.warning("ToolService messaging failed: %s", exc)
            return {"ok": False, "error": str(exc), "platform": platform}

    # 回退：直接调用 adapter
    if adapters:
        for adapter in adapters.values():
            if getattr(adapter, "agent_id", "") == OPENCLAW_AGENT_ID:
                try:
                    result = await adapter.execute_tool(tool_name, args, timeout_sec=10)
                    if result.get("ok"):
                        return {"ok": True, "platform": platform}
                    return {"ok": False, "error": result.get("error", ""), "platform": platform}
                except Exception as exc:
                    return {"ok": False, "error": str(exc), "platform": platform}
        return {"ok": False, "error": "OpenClaw adapter not found", "platform": platform}

    return {"ok": False, "error": "No messaging backend available", "platform": platform}


async def list_supported_platforms() -> list[str]:
    """返回当前支持的消息平台列表。"""
    return list(PLATFORM_TOOL_MAP.keys()) + ["openclaw_notify"]
