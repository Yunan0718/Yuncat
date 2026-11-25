"""tests/unit/test_credential_key_state.py —— 密钥「读不到」≠「不存在」

2026-10-05 用户实例：重装后 cred.key 被重建，库里 09-22 的密文再也解不开。
复盘发现两条会放大伤害的路径，本文件把它们钉死：

1. ``_load_existing_key`` 原来用 ``Path.exists()``——权限不足/文件被占用时它同样
   返回 False，于是「读不到」被当成「没有密钥」，下一次加密就生成新密钥并覆盖原件。
2. 密钥真的缺失而库里仍有密文时，保存路径必须留下可追溯的错误记录，
   不能悄悄把用户已保存的凭证变成垃圾。
"""
import os
import pathlib
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from security import credentials as cred


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """把 cred.key 落到 tmp_path，避免碰真实 %APPDATA%\\Yuncat。"""
    import config
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    return data_dir


# ---------- 密钥状态 ----------

def test_state_missing_when_no_key_file(isolated_env):
    assert cred.credential_key_state() == cred.KEY_STATE_MISSING


def test_state_ok_after_first_encrypt(isolated_env):
    cred.encrypt_value("secret")
    assert cred.credential_key_state() == cred.KEY_STATE_OK


def test_dir_in_place_of_key_is_unreadable_not_missing(isolated_env):
    """cred.key 位置上是目录 → 读它抛 OSError（而非 FileNotFoundError）→ 不可读。"""
    (isolated_env / cred.CRED_KEY_FILE).mkdir()
    assert cred.credential_key_state() == cred.KEY_STATE_UNREADABLE
    with pytest.raises(cred.CredentialKeyUnreadableError):
        cred.encrypt_value("secret")


def test_unreadable_key_is_never_overwritten(isolated_env, monkeypatch):
    """读不到时必须报错，且原密钥文件一个字节都不能变。"""
    cred.encrypt_value("secret")
    key_path = isolated_env / cred.CRED_KEY_FILE
    original = key_path.read_bytes()

    def boom(self):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(pathlib.Path, "read_bytes", boom)
    with pytest.raises(cred.CredentialKeyUnreadableError):
        cred.encrypt_value("another-secret")
    monkeypatch.undo()

    assert key_path.read_bytes() == original


def test_unreadable_error_is_runtime_error_subclass():
    assert issubclass(cred.CredentialKeyUnreadableError, RuntimeError)


def test_fernet_prefix_has_single_source():
    """前缀常量单源：settings_service 不得再自带一份字面量。"""
    from services import settings_service
    assert settings_service._FERNET_PREFIX == cred.FERNET_TOKEN_PREFIX


# ---------- 保存路径：密钥缺失 + 库里仍有密文 ----------

class _FakeCursor:
    def __init__(self, rows=(), rowcount=1):
        self._rows = rows
        self.rowcount = rowcount

    async def fetchall(self):
        return self._rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeDb:
    def __init__(self, rows=()):
        self.rows = rows

    async def execute(self, sql, params=()):
        return _FakeCursor(self.rows)

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_warns_when_key_missing_but_ciphertext_exists(isolated_env, caplog):
    from services import settings_service

    db = _FakeDb([("api_key:deepseek", "gAAAAABqseg_oldciphertext")])
    with caplog.at_level("ERROR"):
        stored = await settings_service._encrypt_for_storage(db, "api_key:deepseek", "sk-new")

    assert stored.startswith(cred.FERNET_TOKEN_PREFIX)
    assert "本地加密密钥" in caplog.text and "api_key:deepseek" in caplog.text


@pytest.mark.asyncio
async def test_quiet_on_fresh_install(isolated_env, caplog):
    """全新安装（库中没有任何密文）不该刷告警。"""
    from services import settings_service

    with caplog.at_level("ERROR"):
        await settings_service._encrypt_for_storage(_FakeDb([]), "api_key:deepseek", "sk-new")

    assert "本地加密密钥" not in caplog.text


@pytest.mark.asyncio
async def test_save_goes_through_guarded_encrypt(isolated_env, caplog):
    """防回归：SettingsService.save 必须走带告警的加密入口，不能绕过。"""
    from services.settings_service import SettingsService

    svc = SettingsService(_FakeDb([("api_key:deepseek", "gAAAAABqseg_oldcipher")]), cache_ttl=0)
    with caplog.at_level("ERROR"):
        await svc.save("api_key:deepseek", "sk-new")

    assert "本地加密密钥" in caplog.text
