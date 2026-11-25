"""tests/contract/test_image_budget.py —— 图像请求预算 / token 预估 / 卸载契约

2026-09-10（对标 deepseek-harness 图像策略）：零依赖实现，纯 stdlib 解析图片头，
三级预算（单图字节/张数/总字节）超限卸载为占位文本，detail 档 low 恒定 85 token。
"""
import base64
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from services.image_budget import (  # noqa: E402
    DEFAULT_MAX_IMAGES_PER_REQUEST,
    apply_image_budget,
    detail_from_model_config,
    estimate_image_tokens,
    offload_placeholder,
    probe_dimensions,
)


def _png(width: int, height: int, pad: int = 0) -> str:
    """构造一张最小合法 PNG 头 + 任意填充字节，返回 base64 裸串。"""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = b"IHDR" + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    raw = sig + struct.pack(">I", 13) + ihdr + b"\x00" * pad
    return base64.b64encode(raw).decode()


def _jpeg(width: int, height: int, pad: int = 0) -> str:
    # SOI + APP0 段 + SOF0(width,height)
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    sof0 = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", height, width) + b"\x00" * 8
    raw = b"\xff\xd8" + app0 + sof0 + b"\x00" * pad
    return base64.b64encode(raw).decode()


# ---- 尺寸解析 ----

def test_probe_png_dimensions():
    raw = base64.b64decode(_png(800, 600))
    assert probe_dimensions(raw) == (800, 600)


def test_probe_jpeg_dimensions():
    raw = base64.b64decode(_jpeg(1920, 1080))
    assert probe_dimensions(raw) == (1920, 1080)


def test_probe_gif_dimensions():
    raw = b"GIF89a" + struct.pack("<HH", 320, 240) + b"\x00" * 8
    assert probe_dimensions(raw) == (320, 240)


def test_probe_invalid_returns_none():
    assert probe_dimensions(b"") is None
    assert probe_dimensions(b"not an image at all, but long enough....") is None


# ---- token 预估 ----

def test_low_detail_is_flat_85():
    assert estimate_image_tokens(4096, 4096, "low") == 85
    assert estimate_image_tokens(64, 64, "low") == 85


def test_auto_detail_tiles_512():
    # 512x512 → 1 tile → 85 + 170 = 255
    assert estimate_image_tokens(512, 512, "auto") == 255
    # 1024x1024 → 2x2 tiles → 85 + 680 = 765
    assert estimate_image_tokens(1024, 1024, "auto") == 765


def test_auto_detail_downscales_large_images():
    # 4096x4096 → 长边压到 2048（2048x2048）→ 短边再压到 768 → 768x768
    # → ceil(768/512)=2 → 2x2=4 tiles → 85 + 680 = 765
    assert estimate_image_tokens(4096, 4096, "auto") == 765
    # 1600x400：长边未超 2048，但短边 400 < 768 不再压缩 → 4x1 tiles → 85+680 = 765
    assert estimate_image_tokens(1600, 400, "auto") == 765


def test_unknown_dimensions_use_conservative_default():
    assert estimate_image_tokens(None, None, "auto") == 765


# ---- 预算与卸载 ----

def test_budget_keeps_within_limits_and_marks_detail():
    atts = [{"name": "a.png", "type": "image/png", "data": _png(256, 256)}]
    kept, placeholders, tokens = apply_image_budget(atts, detail="low")
    assert len(kept) == 1 and not placeholders
    assert kept[0]["_detail"] == "low"
    assert tokens == 85


def test_budget_offloads_oversized_single_image():
    big = {"name": "big.png", "type": "image/png", "data": _png(10, 10, pad=5000)}
    kept, placeholders, _ = apply_image_budget([big], max_one_bytes=100)
    assert kept == []
    assert placeholders == [offload_placeholder(big)]
    assert "big.png" in placeholders[0]


def test_budget_offloads_over_count_deterministically():
    atts = [{"name": f"{i}.png", "type": "image/png", "data": _png(64, 64)} for i in range(5)]
    kept, placeholders, _ = apply_image_budget(atts, max_images=2)
    assert [a["name"] for a in kept] == ["0.png", "1.png"]
    assert len(placeholders) == 3  # 从旧到新确定性卸载


def test_budget_offloads_over_total_bytes():
    atts = [
        {"name": "a.png", "type": "image/png", "data": _png(64, 64, pad=300)},
        {"name": "b.png", "type": "image/png", "data": _png(64, 64, pad=300)},
    ]
    kept, placeholders, _ = apply_image_budget(atts, max_total_bytes=400)
    assert len(kept) == 1
    assert len(placeholders) == 1


def test_default_max_images_is_bounded():
    assert 0 < DEFAULT_MAX_IMAGES_PER_REQUEST <= 100


def test_detail_from_model_config():
    assert detail_from_model_config({"image_detail": "low"}) == "low"
    assert detail_from_model_config({"image_detail": "LOW"}) == "low"
    assert detail_from_model_config({"image_detail": "bogus"}) == "auto"
    assert detail_from_model_config(None) == "auto"


# ---- 思考档位自描述（2026-09-10，对标 dsh per-model reasoning）----

def test_describe_efforts_are_directly_usable_by_frontend():
    from services.reasoning import describe_model_reasoning

    # 2026-10-08：档位**不再按厂商裁剪**——思考强度原样透传给厂商自己解释，
    # 因此前端拿到的永远是完整档位全集；vendor_efforts 仅作"该厂商原生支持哪些"的提示。
    ds = describe_model_reasoning("deepseek-v4-flash-vision-exp")
    assert ds["family"] == "deepseek"
    assert ds["efforts"] == ["none", "minimal", "low", "medium", "high", "xhigh"]
    assert ds["default_effort"] == "high"
    assert ds["off_supported"] is True
    assert ds["vendor_efforts"] == ["low", "medium", "high"]

    gpt = describe_model_reasoning("gpt-5.1")
    assert gpt["efforts"] == ["none", "minimal", "low", "medium", "high", "xhigh"]
    assert gpt["vendor_efforts"] == ["none", "low", "medium", "high"]

    cm = describe_model_reasoning("gpt-5.1-codex-max")
    assert "xhigh" in cm["efforts"]
