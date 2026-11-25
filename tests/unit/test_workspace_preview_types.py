"""tests/unit/test_workspace_preview_types.py —— 文件预览的类型面

前端能不能「看得见」一个格式，取决于两处声明是否对得上：
  1. 后端的 PREVIEW_MEDIA_TYPES（决定 /workspace/files/raw 会不会 415）
  2. 前端的 previewKind()（决定双击时走哪条渲染路径）
两边漏一边，用户看到的就是「这个格式打不开」。这里把关键格式钉住。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from routes.workspace import PREVIEW_MEDIA_TYPES


def test_native_media_types_kept():
    for ext in (".png", ".pdf", ".mp3", ".mp4", ".svg", ".webp"):
        assert ext in PREVIEW_MEDIA_TYPES, f"{ext} 被移出了可预览类型"


def test_office_types_are_previewable():
    """Office 走带鉴权的字节通道，由前端解析后只读展示。"""
    expected = {
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
    for ext, mime in expected.items():
        assert PREVIEW_MEDIA_TYPES.get(ext) == mime, f"{ext} 的 MIME 不对：{PREVIEW_MEDIA_TYPES.get(ext)}"


def test_legacy_binary_office_stays_unsupported():
    """旧版二进制格式解析不了就不该假装能预览（前端给的是「请另存为」提示）。"""
    for ext in (".doc", ".xls", ".ppt"):
        assert ext not in PREVIEW_MEDIA_TYPES
