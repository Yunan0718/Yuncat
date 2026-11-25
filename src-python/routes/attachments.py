"""图片附件上传——小 WS 消息只传引用，原图走受控 HTTP 上传。"""
from fastapi import APIRouter, HTTPException, Request

from services.attachment_store import MAX_IMAGE_BYTES, persist_image_bytes

router = APIRouter(prefix="/api/v1", tags=["attachments"])


@router.post("/attachments")
async def upload_attachment(request: Request, session_id: str = "", filename: str = ""):
    mime_type = (request.headers.get("content-type") or "").split(";", 1)[0].lower()
    if not mime_type.startswith("image/"):
        raise HTTPException(415, "Only image attachments are supported")
    body = await request.body()
    if not body:
        raise HTTPException(400, "Empty attachment body")
    if len(body) > MAX_IMAGE_BYTES:
        raise HTTPException(413, f"Attachment exceeds {MAX_IMAGE_BYTES} bytes")
    try:
        saved = persist_image_bytes(session_id, body, mime_type, filename)
    except ValueError as exc:
        raise HTTPException(415, f"Invalid image attachment: {exc}") from exc
    except OSError as exc:
        raise HTTPException(500, f"Attachment store failed: {exc}") from exc
    return {
        "ok": True, "name": filename or "image", "type": saved["type"],
        "path": saved["path"], "bytes": saved["bytes"], "sha256": saved["sha256"],
    }
