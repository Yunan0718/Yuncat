"""图片附件落盘、预算占位路径与受控读取。"""
import base64
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


def _png_bytes() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 64, 64) + b"\x08\x06\x00\x00\x00"


def test_persist_is_content_addressed_and_deduplicated(tmp_path, monkeypatch):
    from services import attachment_store

    monkeypatch.setenv("YUNCAT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("config.DATA_DIR", str(tmp_path / "data"))
    att = {
        "name": "image.png", "type": "image/png",
        "data": base64.b64encode(_png_bytes()).decode(),
    }
    first = attachment_store.persist_image_attachment("session-1", att)
    second = attachment_store.persist_image_attachment("session-1", att)
    assert first == second
    assert os.path.isfile(first["path"])
    assert first["path"].endswith(f"{first['sha256']}.png")


def test_invalid_attachment_keeps_original_inline_data(tmp_path, monkeypatch):
    from services import attachment_store

    monkeypatch.setattr("config.DATA_DIR", str(tmp_path / "data"))
    att = {"name": "bad.png", "type": "image/png", "data": "NOT_BASE64!"}
    result = attachment_store.persist_image_attachments("s", [att])
    assert result == [att]


def test_offload_placeholder_points_to_vision_analyze():
    from services.image_budget import offload_placeholder

    text = offload_placeholder({"name": "large.png", "path": "/data/attachments/a.png"})
    assert "/data/attachments/a.png" in text
    assert "vision_analyze(image_path=...)" in text


def test_resolve_allowed_image_path_rejects_outside_roots(tmp_path, monkeypatch):
    from services.attachment_store import resolve_allowed_image_path

    monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setattr("config.DATA_DIR", str(tmp_path / "data"))
    outside = tmp_path / "outside.png"
    outside.write_bytes(_png_bytes())
    with pytest.raises(PermissionError):
        resolve_allowed_image_path(str(outside))


def test_resolve_allowed_image_path_accepts_attachment_dir(tmp_path, monkeypatch):
    from services import attachment_store

    data_dir = tmp_path / "data"
    monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setattr("config.DATA_DIR", str(data_dir))
    att = {
        "name": "image.png", "type": "image/png",
        "data": base64.b64encode(_png_bytes()).decode(),
    }
    saved = attachment_store.persist_image_attachment("s", att)
    resolved = attachment_store.resolve_allowed_image_path(saved["path"])
    assert str(resolved) == str(saved["path"])


def test_reference_only_attachment_is_hydrated_for_model_request(tmp_path, monkeypatch):
    from services import attachment_store

    monkeypatch.setattr("config.DATA_DIR", str(tmp_path / "data"))
    raw = _png_bytes()
    saved = attachment_store.persist_image_bytes("s", raw, "image/png", "image.png")
    hydrated = attachment_store.persist_image_attachments("s", [{
        "name": "image.png", "type": "image/png", "path": saved["path"],
    }])
    assert hydrated[0]["data"] == base64.b64encode(raw).decode()
    assert hydrated[0]["sha256"] == saved["sha256"]


def test_cleanup_removes_old_attachments_and_empty_dirs(tmp_path, monkeypatch):
    from services import attachment_store

    monkeypatch.setattr("config.DATA_DIR", str(tmp_path / "data"))
    saved = attachment_store.persist_image_bytes("old", _png_bytes(), "image/png", "a.png")
    old = 1
    os.utime(saved["path"], (old, old))
    assert attachment_store.cleanup_attachments(max_age_days=30) == 1
    assert not os.path.exists(saved["path"])
