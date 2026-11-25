"""图片附件的持久化文本引用（对标 DSH image/offload 的文本替代语义）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from services.attachment_store import format_image_reference  # noqa: E402


def test_reference_names_each_image_with_its_path():
    text = format_image_reference([
        {"type": "image/png", "name": "a.png", "path": "C:/data/attachments/s/a.png"},
        {"type": "image/jpeg", "name": "b.jpg", "path": "C:/data/attachments/s/b.jpg"},
    ])
    assert "a.png" in text and "C:/data/attachments/s/a.png" in text
    assert "b.jpg" in text and "C:/data/attachments/s/b.jpg" in text
    assert "vision_analyze" in text


def test_reference_is_empty_without_images():
    assert format_image_reference([]) == ""
    assert format_image_reference([{"type": "text/plain", "name": "a.txt"}]) == ""


def test_reference_without_path_still_names_the_image():
    text = format_image_reference([{"type": "image/png", "name": "a.png"}])
    assert "a.png" in text


def test_route_uses_the_reference_instead_of_a_bare_count():
    """路由里的 prompt 前缀必须走引用函数（否则图片上下文又只剩一个数字）。"""
    source = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "routes", "ws.py"),
        encoding="utf-8",
    ).read()
    assert "format_image_reference(attachments)" in source
