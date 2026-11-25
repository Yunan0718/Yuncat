"""routes/notify.py —— 通讯网关桥接通知 API

从 main.py 提取（Phase 25），M1 拆分。
用于通过 OpenClaw 向 Telegram/Discord/Slack 等平台发送通知。

Phase 51-F: 使用 MessagingHub 统一包装，增加 /messaging/platforms。
"""
from fastapi import APIRouter, HTTPException, Request

from services.messaging_hub import list_supported_platforms, send_message

router = APIRouter(prefix="/api/v1", tags=["notify"])


@router.post("/notify")
async def send_notification(request: Request):
    """通过 OpenClaw 通讯网关发送通知到外部平台。

    Body: {"platform": "telegram|discord|slack|dingtalk|wechat_work|whatsapp",
           "message": "通知内容", "title": "可选标题"}

    用于工作流完成后的自动通知。
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    platform = body.get("platform", "telegram")
    message = body.get("message", "")
    title = body.get("title", "Yuncat 通知")

    tool_service = getattr(request.app.state, "tool_service", None)
    adapters = getattr(request.app.state, "adapters", {})
    return await send_message(
        platform=platform,
        message=message,
        title=title,
        tool_service=tool_service,
        adapters=adapters,
    )


@router.get("/messaging/platforms")
async def messaging_platforms():
    """Phase 51-F: 返回支持的消息平台列表。"""
    return {"ok": True, "platforms": await list_supported_platforms()}
