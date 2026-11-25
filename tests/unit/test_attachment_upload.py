"""图片附件 HTTP 上传端点。"""
import os
import struct
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


def _client(tmp_path, monkeypatch):
    from routes import attachments

    monkeypatch.setattr("config.DATA_DIR", str(tmp_path / "data"))
    app = FastAPI()
    app.include_router(attachments.router)
    return TestClient(app)


def test_upload_image_returns_reference(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    payload = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 64, 64) + b"\x08\x06\x00\x00\x00"
    response = client.post(
        "/api/v1/attachments?session_id=s1&filename=shot.png",
        content=payload,
        headers={"Content-Type": "image/png"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "shot.png"
    assert body["bytes"] == len(payload)
    assert os.path.isfile(body["path"])
    assert "data" not in body


def test_upload_rejects_non_image_and_oversize(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.post(
        "/api/v1/attachments?session_id=s1&filename=a.txt",
        content=b"text", headers={"Content-Type": "text/plain"},
    )
    assert response.status_code == 415

    from routes import attachments
    monkeypatch.setattr(attachments, "MAX_IMAGE_BYTES", 4)
    response = client.post(
        "/api/v1/attachments?session_id=s1&filename=a.png",
        content=b"12345", headers={"Content-Type": "image/png"},
    )
    assert response.status_code == 413

    from routes import attachments as attachment_routes
    monkeypatch.setattr(attachment_routes, "MAX_IMAGE_BYTES", 1024)
    response = client.post(
        "/api/v1/attachments?session_id=s1&filename=a.png",
        content=b"not-a-png", headers={"Content-Type": "image/png"},
    )
    assert response.status_code == 415
