"""凭据诊断——区分「从没填过 Key」与「填过但本机密钥失效」。"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class _Settings:
    def __init__(self, raw=None, error=None):
        self._raw = raw
        self._error = error

    async def get_raw(self, key):
        if self._error:
            raise self._error
        return self._raw


def test_missing_key_with_ciphertext_is_reported(monkeypatch):
    from security import credentials
    monkeypatch.setattr(credentials, "credential_key_state", lambda: "missing")
    from routes.models import _credential_diagnostics
    out = asyncio.run(_credential_diagnostics(_Settings("gAAAAAcipher"), "deepseek"))
    assert out["credential_state"] == "missing"
    assert out["has_stored_ciphertext"] is True


def test_plaintext_key_is_not_ciphertext(monkeypatch):
    from security import credentials
    monkeypatch.setattr(credentials, "credential_key_state", lambda: "ok")
    from routes.models import _credential_diagnostics
    out = asyncio.run(_credential_diagnostics(_Settings("sk-plain"), "deepseek"))
    assert out["credential_state"] == "ok"
    assert out["has_stored_ciphertext"] is False


def test_settings_read_failure_does_not_break_diagnostics(monkeypatch):
    from security import credentials
    monkeypatch.setattr(credentials, "credential_key_state", lambda: "unreadable")
    from routes.models import _credential_diagnostics
    out = asyncio.run(_credential_diagnostics(
        _Settings(error=RuntimeError("db locked")), "deepseek"))
    assert out["credential_state"] == "unreadable"
    assert out["has_stored_ciphertext"] is False


def test_no_settings_service_is_safe(monkeypatch):
    from security import credentials
    monkeypatch.setattr(credentials, "credential_key_state", lambda: "ok")
    from routes.models import _credential_diagnostics
    out = asyncio.run(_credential_diagnostics(None, "deepseek"))
    assert out == {"credential_state": "ok", "has_stored_ciphertext": False}
