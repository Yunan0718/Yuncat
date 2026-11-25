"""tests/test_credentials_fail_closed.py —— 凭证 fail-closed 行为测试

Phase 47 修复（2.1）：
- decrypt_value 在密钥缺失时抛 CredentialKeyMissingError（不再静默创建新密钥）
- decrypt_value 在密文损坏/密钥不匹配时抛 CredentialDecryptionError
- encrypt_value 在密钥缺失时仍允许首次创建（合法场景）
"""
import os
import sys
from pathlib import Path

import pytest

# 确保 src-python 在 path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src-python"))

from security import credentials as cred


# ═══════════════════════════════════════════════════════════
# Fixtures: 隔离 DATA_DIR 到临时目录，避免污染真实 cred.key
# ═══════════════════════════════════════════════════════════

@pytest.fixture
def isolated_cred_env(tmp_path, monkeypatch):
    """隔离 cred.key 文件到 tmp_path，并 mock config.DATA_DIR。

    每个测试函数独立 tmp_path，互不干扰。
    """
    fake_data_dir = tmp_path / "data"
    fake_data_dir.mkdir(parents=True, exist_ok=True)

    # mock config.DATA_DIR（credentials._get_cred_key_path 通过 from config import DATA_DIR 取值）
    import config
    monkeypatch.setattr(config, "DATA_DIR", str(fake_data_dir))

    # 确保测试前没有 cred.key 文件
    cred_key = fake_data_dir / cred.CRED_KEY_FILE
    if cred_key.exists():
        cred_key.unlink()

    yield fake_data_dir

    # 清理
    if cred_key.exists():
        cred_key.unlink()


# ═══════════════════════════════════════════════════════════
# 正常路径测试
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
def test_encrypt_then_decrypt_round_trip(isolated_cred_env):
    """加密→解密正常往返。"""
    plaintext = "sk-deepseek-1234567890abcdef"
    ciphertext = cred.encrypt_value(plaintext)

    # 密文应不同于明文
    assert ciphertext != plaintext
    # cred.key 文件应被创建
    assert (isolated_cred_env / cred.CRED_KEY_FILE).exists()

    # 解密应返回原文
    decrypted = cred.decrypt_value(ciphertext)
    assert decrypted == plaintext


@pytest.mark.unit
def test_encrypt_creates_key_on_first_use(isolated_cred_env):
    """首次加密时自动创建 cred.key（合法场景）。"""
    # 初始状态：无密钥文件
    assert not (isolated_cred_env / cred.CRED_KEY_FILE).exists()

    ciphertext = cred.encrypt_value("secret")

    # 加密后密钥文件应存在
    assert (isolated_cred_env / cred.CRED_KEY_FILE).exists()
    assert ciphertext  # 非空


@pytest.mark.unit
def test_encrypt_reuses_existing_key(isolated_cred_env):
    """第二次加密复用已有密钥（不创建新密钥）。"""
    # 第一次加密创建密钥
    cred.encrypt_value("secret1")
    key_bytes_1 = (isolated_cred_env / cred.CRED_KEY_FILE).read_bytes()

    # 第二次加密应复用同一密钥
    cred.encrypt_value("secret2")
    key_bytes_2 = (isolated_cred_env / cred.CRED_KEY_FILE).read_bytes()

    assert key_bytes_1 == key_bytes_2


# ═══════════════════════════════════════════════════════════
# Fail-closed 测试
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
def test_decrypt_fails_closed_when_key_missing(isolated_cred_env):
    """密钥缺失时 decrypt 抛 CredentialKeyMissingError（fail-closed）。

    关键：不创建新密钥掩盖问题。
    """
    # 用一个独立密钥加密生成密文（在另一个 tmp 目录）
    other_dir = isolated_cred_env.parent / "other_env"
    other_dir.mkdir(parents=True, exist_ok=True)
    import config
    original_data_dir = config.DATA_DIR
    try:
        config.DATA_DIR = str(other_dir)
        ciphertext = cred.encrypt_value("secret")
    finally:
        config.DATA_DIR = original_data_dir

    # 当前 isolated_cred_env 下没有 cred.key
    assert not (isolated_cred_env / cred.CRED_KEY_FILE).exists()

    # decrypt 应抛 CredentialKeyMissingError
    with pytest.raises(cred.CredentialKeyMissingError) as exc_info:
        cred.decrypt_value(ciphertext)

    # 错误消息应包含密钥文件路径
    assert cred.CRED_KEY_FILE in str(exc_info.value)

    # 关键：decrypt 不应创建新密钥文件
    assert not (isolated_cred_env / cred.CRED_KEY_FILE).exists()


@pytest.mark.unit
def test_decrypt_fails_closed_on_corrupted_ciphertext(isolated_cred_env):
    """密文损坏时 decrypt 抛 CredentialDecryptionError。"""
    # 先创建密钥
    cred.encrypt_value("secret")

    # 用损坏的密文解密
    corrupted_ciphertext = "gAAAAABmThisIsNotValidFernetToken=="
    with pytest.raises(cred.CredentialDecryptionError) as exc_info:
        cred.decrypt_value(corrupted_ciphertext)

    assert "密文损坏" in str(exc_info.value) or "密钥不匹配" in str(exc_info.value)


@pytest.mark.unit
def test_decrypt_fails_closed_on_key_mismatch(isolated_cred_env, tmp_path):
    """密钥不匹配（cred.key 被替换）时 decrypt 抛 CredentialDecryptionError。"""
    # 用第一个密钥加密
    ciphertext = cred.encrypt_value("secret")
    key_1 = (isolated_cred_env / cred.CRED_KEY_FILE).read_bytes()

    # 删除密钥文件，触发 encrypt 创建新密钥（模拟密钥被替换）
    (isolated_cred_env / cred.CRED_KEY_FILE).unlink()
    cred.encrypt_value("trigger_new_key_creation")
    key_2 = (isolated_cred_env / cred.CRED_KEY_FILE).read_bytes()

    # 确认两个密钥不同
    assert key_1 != key_2

    # 用新密钥解密旧密文应失败
    with pytest.raises(cred.CredentialDecryptionError):
        cred.decrypt_value(ciphertext)


# ═══════════════════════════════════════════════════════════
# 异常类型层级测试
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
def test_credentials_errors_are_runtime_error_subclass():
    """两个异常类型都应继承 RuntimeError（便于 except Exception 兜底）。"""
    assert issubclass(cred.CredentialKeyMissingError, RuntimeError)
    assert issubclass(cred.CredentialDecryptionError, RuntimeError)


@pytest.mark.unit
def test_settings_service_get_decrypted_fallback_tolerates_fail_closed(isolated_cred_env):
    """settings_service.get_decrypted 的 except Exception 应能捕获新异常。

    验证向后兼容：现有 fallback 逻辑（返回 row[0]）仍能工作。
    """
    # 模拟 settings_service 的 decrypt 调用模式
    ciphertext = "gAAAAABmThisIsNotValidFernetToken=="

    # 模拟 settings_service.get_decrypted 的 try/except 模式
    try:
        result = cred.decrypt_value(ciphertext)
    except Exception:
        # 这是 settings_service 的 fallback 路径
        result = ciphertext  # 返回密文作为 fallback

    # fallback 应返回密文（向后兼容旧行为）
    assert result == ciphertext
