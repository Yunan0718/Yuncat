"""contract: 凭证明文/密文的处置必须分开

2026-09-25 用户实测事故：数据库里的 API Key 是 09-22 加密的，而 cred.key 在 09-25
被换成另一把钥匙 → 解密失败。当时 `get_decrypted` 的 except 分支**把密文本身当明文返回**，
于是那串 `gAAAAABqseg_...` 被拼成 `Authorization: Bearer gAAAAA...` 发给厂商，
换回一个与真实原因无关的 401。用户侧只看到「发消息没回复」，真实原因只在日志里。

本测试锁定两条相反的处理：
- 是密文却解不开 → 返回 None（凭证不可用），并留下 error 日志
- 本就不是密文（历史明文数据） → 原样返回，保持对旧数据兼容
"""
import logging
import os
import sys

import aiosqlite
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from repositories.schema import SCHEMA_SQL  # noqa: E402
from security import credentials as cred  # noqa: E402
from services.settings_service import SettingsService, _looks_like_ciphertext  # noqa: E402


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    yield conn
    await conn.close()


def test_ciphertext_detection():
    assert _looks_like_ciphertext("gAAAAABmThisIsAFernetToken==") is True
    assert _looks_like_ciphertext("sk-example-placeholder-0000000000000000") is False
    assert _looks_like_ciphertext("") is False


async def test_broken_ciphertext_returns_none_not_ciphertext(db, tmp_path, monkeypatch, caplog):
    """密文解不开时必须返回 None——绝不能把密文当密钥交给下游。"""
    import config

    # 指向一个干净的密钥目录：先写入密文，再换掉密钥，模拟 cred.key 被替换
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    svc = SettingsService(db)
    await svc.save("api_key:deepseek", "sk-real-key-123456")

    # 换一把钥匙 → 原密文再也解不开
    key_path = tmp_path / cred.CRED_KEY_FILE
    original = key_path.read_bytes()
    key_path.unlink()
    cred._create_new_key()
    assert key_path.read_bytes() != original

    with caplog.at_level(logging.ERROR, logger="yuncat.settings"):
        got = await svc.get_decrypted("api_key:deepseek")

    assert got is None, f"解密失败返回了 {got!r}——密文会被当成 API Key 发出去"
    assert any("解密失败" in r.message for r in caplog.records), "解密失败必须留 error 日志"


async def test_legacy_plaintext_still_returned(db, tmp_path, monkeypatch):
    """历史明文数据（非密文形态）按原值返回，不因这次收紧而被误伤。"""
    import config

    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    await db.execute(
        "INSERT INTO settings(key, value, version) VALUES(?,?,1)",
        ("api_key:legacy", "sk-legacy-plaintext-key"),
    )
    await db.commit()

    svc = SettingsService(db)
    assert await svc.get_decrypted("api_key:legacy") == "sk-legacy-plaintext-key"


async def test_non_encrypted_key_untouched(db, tmp_path, monkeypatch):
    """非加密键（如主题）不经加解密路径。"""
    import config

    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    svc = SettingsService(db)
    await svc.save("theme", "dark")
    assert await svc.get_decrypted("theme") == "dark"