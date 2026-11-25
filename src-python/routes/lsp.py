"""routes/lsp.py —— 语言服务：状态查询 + WebSocket 通道

为什么是「后端起服务器、前端直接说 LSP」：
  Monaco 只会说 LSP（补全/悬停/跳转/诊断），pyright 也只会说 LSP。
  中间最省事的做法不是再造一层语义协议，而是把两边接起来——后端负责
  「起哪个服务器、在哪起」，协议本身原样透传。

安全边界（与终端 WS 一致，别削弱）：
  - accept 前校验 Origin；首条消息必须是 {"type": "auth", "token": ...}，hmac 常量时间比较；
  - 语言白名单写死在 services/lsp_manager，前端不可能让它起任意进程。

工作区：语言服务器以当前工作区为根启动，跨文件跳转/引用才有意义。
"""
import hmac
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from config import TOKEN
from routes.ws import _is_origin_allowed
from security.sandbox import get_workspace
from services import lsp_manager

_log = logging.getLogger("yuncat.lsp.route")

router = APIRouter(prefix="/api/v1", tags=["lsp"])

# 与 /ws 端点对齐：单条 WS 消息上限，防止大 payload 打爆内存
WS_LSP_MAX_MESSAGE_BYTES = 8 * 1024 * 1024


@router.get("/lsp/status")
async def lsp_status(language: str = "python") -> dict:
    """某个语言的智能提示当前是否可用（不可用时带原因与原因代码）。"""
    status = lsp_manager.language_status(language)
    return {"ok": True, **status}


async def _reject(ws: WebSocket, code: int, reason: str) -> None:
    try:
        await ws.close(code=code, reason=reason)
    except RuntimeError:
        _log.debug("WS close failed during reject", exc_info=True)


async def _authenticate(ws: WebSocket) -> bool:
    """首帧鉴权：必须是 {"type": "auth", "token": ...}，且 token 恒定时间比较通过。"""
    try:
        handshake = await ws.receive_json()
    except (WebSocketDisconnect, ValueError):
        return False
    if not isinstance(handshake, dict) or handshake.get("type") != "auth":
        return False
    token = handshake.get("token", "")
    # 与主 /ws、/ws/terminal 同口径：静态 TOKEN 与主进程兑换的 access token 都接受。
    # 前端 appStore 里存的是 access token，只认静态 TOKEN 会让 LSP 恒被拒。
    if not token:
        return False
    if hmac.compare_digest(token, TOKEN):
        return True
    from services.auth_service import validate_access_token
    return validate_access_token(token)


async def _open_session(ws: WebSocket, language: str):
    """取（必要时启动）语言服务器；不可用时把原因说清楚并关闭连接。"""
    status = lsp_manager.language_status(language)
    if not status["available"]:
        await ws.send_json({"type": "unavailable", "language": language,
                            "reason": status["reason"], "code": status["code"]})
        await _reject(ws, 1011, "language server unavailable")
        return None
    try:
        return await lsp_manager.get_session(language, str(get_workspace()))
    except Exception as exc:
        _log.warning("LSP session start failed: %s", exc)
        await ws.send_json({"type": "unavailable", "language": language,
                            "reason": str(exc), "code": "start-failed"})
        await _reject(ws, 1011, "language server failed")
        return None


async def _relay(ws: WebSocket, session, language: str) -> None:
    """双向透传：服务端消息推给前端，前端消息写回语言服务器。"""

    async def forward_to_client(message: dict) -> None:
        try:
            await ws.send_json({"type": "lsp", "message": message})
        except Exception:
            # 降级：连接已断开时发送必然失败，由接收循环负责收尾
            _log.debug("LSP forward failed (client gone?)", exc_info=True)

    session.add_listener(forward_to_client)
    try:
        while True:
            message = await ws.receive_json()
            kind = message.get("type")
            if kind == "ping":
                await ws.send_json({"type": "pong"})
                continue
            if kind != "lsp":
                continue
            payload = message.get("message") or {}
            if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > WS_LSP_MAX_MESSAGE_BYTES:
                _log.warning("LSP message too large, dropped")
                continue
            if not await session.send(payload):
                await ws.send_json({"type": "unavailable", "language": language,
                                    "reason": "语言服务器已退出，重新打开文件即可重启",
                                    "code": "server-exited"})
                return
    except WebSocketDisconnect:
        _log.debug("LSP WS disconnected (language=%s)", language)
    except Exception as exc:
        _log.warning("LSP WS loop error: %s", exc)
    finally:
        session.remove_listener(forward_to_client)


async def ws_lsp(ws: WebSocket) -> None:
    """/ws/lsp?language=python —— 一个连接一个语言服务器会话（会话本身在工作区级别复用）"""
    language = (ws.query_params.get("language") or "python").lower()
    origin = ws.headers.get("origin", "")
    if not _is_origin_allowed(origin):
        _log.warning("LSP WS rejected origin=%s", origin)
        await _reject(ws, 1008, "origin not allowed")
        return

    await ws.accept()
    if not await _authenticate(ws):
        _log.warning("LSP WS auth rejected from=%s", origin or "?")
        await _reject(ws, 1008, "auth required")
        return
    await ws.send_json({"type": "auth_ok"})

    session = await _open_session(ws, language)
    if session is None:
        return
    await ws.send_json({"type": "ready", "language": language, "capabilities": session.capabilities})
    await _relay(ws, session, language)
    try:
        await ws.close()
    except RuntimeError:
        _log.debug("LSP WS close failed", exc_info=True)
