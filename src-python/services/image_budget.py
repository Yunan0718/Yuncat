"""services/image_budget.py —— 图像请求预算、token 预估与超限卸载

对标 deepseek-harness 的图像策略（`imagePixelBudget` / `imageMaxBytes` /
请求级字节+张数预算 / 超限卸载带占位文本），但**零第三方依赖**：
尺寸解析用 stdlib 直接读图片头（PNG/JPEG/GIF/WebP），不做重编码
（内置 Python 运行时无 Pillow，为单一功能引入重依赖不划算）。

解决的三个问题：
1. **成本不可见**：图像 token 随张数/分辨率非线性增长，这里给出确定性预估
   （OpenAI/DeepSeek 同款 512px tile 算法），供上下文预算与成本展示使用。
2. **预算无上限**：一次请求可携带任意多/任意大的图 → 每轮重复计费。这里按
   「张数 / 单图字节 / 总字节」三级预算，超限部分**卸载为占位文本**（而非静默丢弃）。
3. **detail 档**：模型可配 `image_detail: low` 走最省档（low 恒 85 token）。

卸载顺序确定性：先按「超大单图」→「超量张数」，从最旧到最新逐个卸载，
保证同一输入每次得到同样的请求体（dsh 的 imageOffloadCountQuantum 语义）。
"""
from __future__ import annotations

import base64
import binascii
import logging
import struct

_log = logging.getLogger("yuncat.image_budget")

# ---- 默认预算（config.py 可覆盖）----
DEFAULT_MAX_IMAGES_PER_REQUEST = 20       # 单请求最多几张图
DEFAULT_MAX_TOTAL_BYTES = 20 * 1024 * 1024  # 单请求图像总字节上限（base64 前原始字节）
DEFAULT_MAX_ONE_IMAGE_BYTES = 8 * 1024 * 1024  # 单图字节上限
DEFAULT_IMAGE_DETAIL = "auto"             # auto | low | high

# OpenAI/DeepSeek 视觉计费常数（tile 512px，低档恒定 85）
_LOW_DETAIL_TOKENS = 85
_TILE_TOKENS = 170
_TILE_SIZE = 512
_MAX_SIDE = 2048
_SHORT_SIDE = 768


def _decode_size(data: str) -> bytes | None:
    """把 base64 裸串或 data URL 还原为原始字节（失败返回 None）。"""
    if not isinstance(data, str) or not data:
        return None
    raw = data
    if data.startswith("data:"):
        _, _, raw = data.partition(",")
    try:
        return base64.b64decode(raw, validate=False)
    except (binascii.Error, ValueError):
        return None


def probe_dimensions(raw: bytes) -> tuple[int, int] | None:
    """从图片字节头解析 (width, height)——纯 stdlib，不解码像素。

    支持 PNG / JPEG / GIF / WebP(VP8/VP8L/VP8X)。解析失败返回 None
    （调用方按"尺寸未知"处理，不阻断请求）。
    """
    if not raw or len(raw) < 16:
        return None
    try:
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            # IHDR: 8 字节签名 + 4 长度 + 4 类型 + width(4) + height(4)
            w, h = struct.unpack(">II", raw[16:24])
            return int(w), int(h)
        if raw[:3] == b"GIF":
            w, h = struct.unpack("<HH", raw[6:10])
            return int(w), int(h)
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            return _webp_size(raw)
        if raw[:2] == b"BM" and len(raw) >= 26:
            w, h = struct.unpack("<ii", raw[18:26])
            return abs(int(w)), abs(int(h))
        if raw[:2] == b"\xff\xd8":
            return _jpeg_size(raw)
    except (struct.error, IndexError, ValueError):
        return None
    return None


def detect_image_mime(raw: bytes) -> str | None:
    """按文件魔数检测图片 MIME，不信任客户端声明的 Content-Type。"""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw.startswith(b"BM"):
        return "image/bmp"
    return None


def _webp_size(raw: bytes) -> tuple[int, int] | None:
    chunk = raw[12:16]
    if chunk == b"VP8X":
        w = 1 + int.from_bytes(raw[24:27], "little")
        h = 1 + int.from_bytes(raw[27:30], "little")
        return w, h
    if chunk == b"VP8 ":
        w = int.from_bytes(raw[26:28], "little") & 0x3FFF
        h = int.from_bytes(raw[28:30], "little") & 0x3FFF
        return w, h
    if chunk == b"VP8L":
        bits = int.from_bytes(raw[21:25], "little")
        w = (bits & 0x3FFF) + 1
        h = ((bits >> 14) & 0x3FFF) + 1
        return w, h
    return None


def _jpeg_size(raw: bytes) -> tuple[int, int] | None:
    i = 2
    end = len(raw)
    while i + 9 < end:
        if raw[i] != 0xFF:
            i += 1
            continue
        marker = raw[i + 1]
        # SOF0-SOF15（跳过 DHT/DAC/RSTn 等非 SOF 标记）
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", raw[i + 5:i + 9])
            return int(w), int(h)
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", raw[i + 2:i + 4])[0]
        i += 2 + seg_len
    return None


def estimate_image_tokens(width: int | None, height: int | None,
                          detail: str = DEFAULT_IMAGE_DETAIL) -> int:
    """预估单张图的视觉 token 消耗（OpenAI/DeepSeek 同款算法）。

    - low：恒定 85（无视尺寸）。
    - auto/high：先缩放到最长边 ≤2048、最短边 ≤768，再按 512px tile 计
      ``85 + 170 * tiles``。宽高未知时按 1024x1024 保守估（4 tiles = 765）。
    """
    if detail == "low":
        return _LOW_DETAIL_TOKENS
    w = width or 1024
    h = height or 1024
    if w <= 0 or h <= 0:
        w = h = 1024
    # 长边约束
    longest, shortest = max(w, h), min(w, h)
    if longest > _MAX_SIDE:
        scale = _MAX_SIDE / longest
        w, h = max(1, int(w * scale)), max(1, int(h * scale))
        shortest = min(w, h)
    if shortest > _SHORT_SIDE:
        scale = _SHORT_SIDE / shortest
        w, h = max(1, int(w * scale)), max(1, int(h * scale))
    tiles = max(1, -(-w // _TILE_SIZE)) * max(1, -(-h // _TILE_SIZE))
    return _LOW_DETAIL_TOKENS + _TILE_TOKENS * tiles


def offload_placeholder(att: dict, reason: str = "因超出单次请求图像预算") -> str:
    """被卸载图片的占位文本——让模型知道"这里原本有图"而不是静默丢失。"""
    name = att.get("name") or "未命名图片"
    path = att.get("path")
    if path:
        return (
            f"[图片附件「{name}」{reason}未随本次请求发送；"
            f"已保存于 {path}，可用 vision_analyze(image_path=...) 按需查看]"
        )
    return f"[图片附件「{name}」{reason}未随本次请求发送]"


def apply_image_budget(
    attachments: list[dict],
    *,
    max_images: int = DEFAULT_MAX_IMAGES_PER_REQUEST,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_one_bytes: int = DEFAULT_MAX_ONE_IMAGE_BYTES,
    detail: str = DEFAULT_IMAGE_DETAIL,
) -> tuple[list[dict], list[str], int]:
    """按预算过滤图像附件。

    返回 ``(kept, placeholders, estimated_tokens)``：
    - kept：保留的附件（附加 `_detail` 供调用方写 image_url.detail）
    - placeholders：被卸载图片的占位文本（按原顺序）
    - estimated_tokens：保留图片的视觉 token 预估合计

    预算顺序确定性（从旧到新逐个卸载，同输入必得同输出）：
    1. 单图超过 max_one_bytes → 卸载
    2. 累计张数超过 max_images → 卸载
    3. 累计原始字节超过 max_total_bytes → 卸载
    """
    kept: list[dict] = []
    placeholders: list[str] = []
    total_bytes = 0
    tokens = 0

    for att in attachments:
        raw = _decode_size(att.get("data", ""))
        size = len(raw) if raw is not None else 0
        invalid = raw is None or size == 0
        over_one = size > max_one_bytes
        over_count = len(kept) >= max_images
        over_total = (total_bytes + size) > max_total_bytes
        if invalid or over_one or over_count or over_total:
            reason = ("图片数据无效" if invalid else "单图过大" if over_one
                      else "张数超限" if over_count else "总字节超限")
            _log.info("image offloaded (%s): %s bytes", reason, size)
            placeholders.append(offload_placeholder(
                att, f"因{reason}" if invalid else "因超出单次请求图像预算",
            ))
            continue
        dims = probe_dimensions(raw) if raw else None
        w, h = dims if dims else (None, None)
        tokens += estimate_image_tokens(w, h, detail)
        total_bytes += size
        kept_att = dict(att)
        kept_att["_detail"] = detail
        kept.append(kept_att)

    return kept, placeholders, tokens


def detail_from_model_config(model_cfg: dict | None) -> str:
    """模型配置里的图像档（`image_detail`），默认 auto。"""
    cfg = model_cfg or {}
    detail = str(cfg.get("image_detail") or DEFAULT_IMAGE_DETAIL).lower()
    return detail if detail in ("auto", "low", "high") else DEFAULT_IMAGE_DETAIL
