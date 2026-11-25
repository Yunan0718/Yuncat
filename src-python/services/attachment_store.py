"""用户图片附件的持久化与受控路径解析。"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import re
import secrets
import time
from pathlib import Path

from services.image_budget import DEFAULT_MAX_ONE_IMAGE_BYTES

_log = logging.getLogger("yuncat.attachments")

ATTACHMENT_DIR_NAME = "attachments"
BROWSER_SHOT_DIR_NAME = "browser-shots"
MAX_IMAGE_BYTES = DEFAULT_MAX_ONE_IMAGE_BYTES
ATTACHMENT_MAX_AGE_DAYS = 30

_IMAGE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}
_SAFE_PART = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_part(value: str, fallback: str = "item") -> str:
    text = _SAFE_PART.sub("_", str(value or "")).strip("._")
    return (text or fallback)[:80]


def decode_image(att: dict) -> bytes:
    mime = str(att.get("type") or "").lower().split(";", 1)[0]
    if not mime.startswith("image/"):
        raise ValueError("not an image attachment")
    data = str(att.get("data") or "")
    if data.startswith("data:"):
        _, _, data = data.partition(",")
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid base64 image data") from exc
    if not raw:
        raise ValueError("empty image data")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes")
    from services.image_budget import detect_image_mime
    if detect_image_mime(raw) is None:
        raise ValueError("unsupported or corrupt image format")
    return raw


def persist_image_attachment(session_id: str, att: dict) -> dict:
    """按内容哈希原子保存图片，返回可审计路径与元数据。"""
    raw = decode_image(att)
    return persist_image_bytes(
        session_id, raw,
        str(att.get("type") or "image/png"),
        str(att.get("name") or ""),
    )


def persist_image_bytes(session_id: str, raw: bytes, mime_type: str, name: str = "") -> dict:
    """按内容哈希原子保存原始图片字节。"""
    from config import DATA_DIR

    if not raw:
        raise ValueError("empty image data")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes")
    digest = hashlib.sha256(raw).hexdigest()
    from services.image_budget import detect_image_mime
    mime = detect_image_mime(raw)
    if mime is None:
        raise ValueError("unsupported or corrupt image format")
    from services.image_budget import probe_dimensions
    if probe_dimensions(raw) is None:
        raise ValueError("image header is missing valid dimensions")
    suffix = _IMAGE_EXTENSIONS.get(mime) or Path(name).suffix.lower()
    if suffix not in set(_IMAGE_EXTENSIONS.values()):
        suffix = ".png"
    directory = Path(DATA_DIR) / ATTACHMENT_DIR_NAME / _safe_part(session_id, "session")
    target = directory / f"{digest}{suffix}"
    if not target.is_file():
        directory.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + f".{secrets.token_hex(4)}.tmp")
        with open(temp, "wb") as fh:
            fh.write(raw)
        os.replace(temp, target)
    return {"path": str(target), "bytes": len(raw), "sha256": digest, "type": mime}


def persist_image_attachments(session_id: str, attachments: list[dict]) -> list[dict]:
    """保存附件中的图片；单张失败保留原 inline 数据并记录日志。"""
    hydrated: list[dict] = []
    for att in attachments:
        if not isinstance(att, dict) or not str(att.get("type", "")).lower().startswith("image/"):
            hydrated.append(att)
            continue
        try:
            if att.get("path") and not att.get("data"):
                path = resolve_attachment_path(str(att["path"]))
                raw = path.read_bytes()
                hydrated.append({
                    **att, "data": base64.b64encode(raw).decode(),
                    "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                })
            else:
                hydrated.append({**att, **persist_image_attachment(session_id, att)})
        except (OSError, ValueError) as exc:
            _log.warning("image attachment persist failed: %s", exc)
            hydrated.append(att)
    return hydrated


def format_image_reference(attachments: list[dict]) -> str:
    """把图片附件渲染成一段**可持久化**的文本引用（对标 DSH 的 image/offload 语义）。

    之前写进会话历史的只有「[含 N 张图片]」——后续轮次里模型既不知道具体有哪些图、
    也不知道去哪找。改成点名每张图并给出只读路径，模型可以用
    vision_analyze(image_path=...) 按需重看，图片上下文也不会随首轮结束而蒸发。
    """
    items: list[str] = []
    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        if not str(att.get("type", "")).lower().startswith("image/"):
            continue
        name = str(att.get("name") or "未命名图片")
        path = str(att.get("path") or "")
        items.append(f"{name} → {path}" if path else name)
    if not items:
        return ""
    return "[图片附件（已保存到本地，可用 vision_analyze 按路径查看）：" + "；".join(items) + "]"


def resolve_attachment_path(path: str) -> Path:
    """解析本服务上传的附件路径，禁止引用其他数据目录或工作区文件。"""
    from config import DATA_DIR

    root = (Path(DATA_DIR) / ATTACHMENT_DIR_NAME).resolve()
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise PermissionError("attachment path is outside the attachment store")
    if candidate.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes")
    return candidate


def cleanup_attachments(max_age_days: int = ATTACHMENT_MAX_AGE_DAYS) -> int:
    """清理超过保留期的附件及空会话目录。"""
    from config import DATA_DIR

    root = Path(DATA_DIR) / ATTACHMENT_DIR_NAME
    cutoff = time.time() - max(0, max_age_days) * 86400
    removed = 0
    if not root.is_dir():
        return 0
    for path in root.glob("*/*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    for directory in root.iterdir():
        try:
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            continue
    return removed


def resolve_allowed_image_path(path: str) -> Path:
    """只允许工作区共享目录和受控附件/截图目录中的图片文件。"""
    from config import DATA_DIR
    from security.sandbox import get_shared_workspace
    from tools.builtin.system_tool_safety import is_denied_path

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = get_shared_workspace() / candidate
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise FileNotFoundError(f"image not found: {path}")
    allowed_roots = [
        get_shared_workspace().resolve(),
        (Path(DATA_DIR) / ATTACHMENT_DIR_NAME).resolve(),
        (Path(DATA_DIR) / BROWSER_SHOT_DIR_NAME).resolve(),
    ]
    if not any(candidate.is_relative_to(root) for root in allowed_roots):
        raise PermissionError("image path is outside the allowed workspace/data roots")
    denied, reason = is_denied_path(str(candidate))
    if denied:
        raise PermissionError(reason)
    if candidate.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes")
    return candidate
