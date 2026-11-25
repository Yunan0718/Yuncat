"""用户问题回答的 WS 路由。"""
from tools.builtin.system_tool_ask_user import MAX_ANSWER_CHARS, resolve_user_question


async def handle_user_question_response(ws, msg: dict) -> None:
    token = str(msg.get("token") or "").strip()
    if not token:
        await ws.send_json({"type": "user_question_error", "error": "missing token"})
        return
    answer = str(msg.get("answer") or "")
    cancelled = bool(msg.get("cancelled"))
    if not cancelled and len(answer) > MAX_ANSWER_CHARS:
        await ws.send_json({
            "type": "user_question_invalid", "token": token,
            "error": "answer too long", "max_chars": MAX_ANSWER_CHARS,
        })
        return
    accepted = resolve_user_question(token, answer, cancelled)
    await ws.send_json({
        "type": "user_question_ack", "token": token, "accepted": accepted,
        "error": "" if accepted else "question is no longer active",
    })
