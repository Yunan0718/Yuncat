"""security/credentials.py —— v39 凭证中继模式（借鉴 HiClaw 零信任安全）

Agent 子进程永远看不到真实 API Key。
Yuncat Gateway 收到 Agent 的 LLM 请求 → 从 keyring 解密 → 注入真实 Key → 转发。

Phase 47 修复（2.1 fail-closed）：
- decrypt_value 在密钥缺失/密文损坏时抛 RuntimeError（不再静默创建新密钥掩盖问题）
- encrypt_value 在密钥缺失时仍允许首次创建（首次加密是合法场景）
- 调用方（settings_service.get_decrypted）已有 except Exception 兜底，行为向后兼容
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

_log = logging.getLogger("yuncat.credentials")

# Fernet key 存储在 %APPDATA%/Yuncat/.cred_key（600 权限）
CRED_KEY_FILE = "cred.key"

# Fernet 令牌的固定前缀（版本字节 0x80 + 时间戳，base64url 编码后总是 gAAAAA 开头）。
# 单源放在这里：settings_service 用它区分「历史明文数据」与「本该能解却解不开的密文」，
# 两处各写一份常量必然漂移，而这两类的处置正好相反（放行 vs 报错）。
FERNET_TOKEN_PREFIX = "gAAAAA"

# credential_key_state() 的返回值
KEY_STATE_OK = "ok"
KEY_STATE_MISSING = "missing"
KEY_STATE_UNREADABLE = "unreadable"


class CredentialKeyMissingError(RuntimeError):
    """密钥文件缺失——decrypt 无法进行（fail-closed）。

    触发场景：cred.key 文件被删除/未随备份恢复/新设备首次启动但 DB 已有密文。
    调用方应提示用户重新输入 API Key，而非返回密文当原文使用。
    """
    pass


class CredentialKeyUnreadableError(RuntimeError):
    """密钥文件存在但当前进程读不到（权限不足 / 被占用 / IO 错误）。

    必须与「文件不存在」区分：把读取失败当成「没有密钥」，下一次加密就会生成一把
    新密钥并覆盖原件，用户已保存的凭证全部永久无法解密。典型成因是卸载/清理过程对
    数据目录做了**部分删除**（库还在、cred.key 没了），或杀软/索引器短暂锁住文件——
    两种情况都不该让程序自作主张地"重建"密钥。
    """
    pass


class CredentialDecryptionError(RuntimeError):
    """密文损坏或密钥不匹配（fail-closed）。

    触发场景：cred.key 被替换为不同密钥 / 密文被篡改 / 数据库损坏。
    """
    pass


def _get_cred_key_path() -> Path:
    from config import DATA_DIR
    return Path(DATA_DIR) / CRED_KEY_FILE


def _load_existing_key() -> bytes | None:
    """仅加载已存在的密钥，不存在返回 None（不自动创建）。

    与原 _get_or_create_key 区分：decrypt 路径不应自动创建新密钥
    （新密钥无法解密旧密文，会掩盖"密钥丢失"的真实问题）。
    """
    key_path = _get_cred_key_path()
    try:
        return key_path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise CredentialKeyUnreadableError(
            f"密钥文件存在但无法读取：{key_path}（{e}）。"
            "不会新建密钥——新密钥解不开已存储的密文。"
        ) from e


def credential_key_state() -> str:
    """密钥文件状态：``ok`` / ``missing`` / ``unreadable``。

    诊断用途：不创建文件、不抛异常——调用方（保存前告警、状态上报、启动自检）
    需要的是一条可安全记录的事实，而不是又一层要处理的异常。
    """
    try:
        key = _load_existing_key()
    except Exception:
        # best-effort 诊断：探测本身失败时按「不可读」上报，绝不据此创建新密钥
        _log.debug("credential_key_state 探测失败", exc_info=True)
        return KEY_STATE_UNREADABLE
    return KEY_STATE_MISSING if key is None else KEY_STATE_OK


def _current_user_sid() -> str | None:
    """当前**进程令牌**的用户 SID——用于收紧 cred.key 权限。

    不能读 os.environ["USERNAME"]：环境变量是可继承、可覆盖的，与进程真实令牌
    可能不一致（沙箱、服务账户、计划任务、容器、runas 降权）。2026-09-25 实测：
    两者不一致时 icacls 会把文件授权给一个当前进程并不属于的账户，
    cred.key 随即变成自己都读不出来——用户已保存的 API Key 全部无法解密。

    whoami /user 读的是令牌本身；CSV 输出里的 SID 与系统语言无关。
    """
    try:
        import csv
        import io
        import subprocess

        from config import ICACLS_TIMEOUT_SEC

        proc = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            capture_output=True, text=True, timeout=ICACLS_TIMEOUT_SEC,
        )
        if proc.returncode != 0:
            return None
        row = next(csv.reader(io.StringIO(proc.stdout or "")), None)
        if row and len(row) >= 2 and row[1].startswith("S-"):
            return row[1]
        _log.warning("whoami 输出无法解析为 SID: %r", (proc.stdout or "")[:120])
    except Exception:
        _log.debug("读取进程令牌 SID 失败", exc_info=True)
    return None


def _set_key_file_acl(key_path: Path, sid: str) -> None:
    """把 cred.key 收紧到「仅 sid 可读」，并在授权后自检可读性。

    自检不是多余的：icacls 失败或授权对象不对时，文件仍在磁盘上但进程读不出来——
    那等于把用户已保存的 API Key 永久锁死，且下次启动会以「密钥缺失」报错，
    完全对不上真实原因。宁可放宽回继承权限，也不能留下读不出的密钥文件。
    """
    try:
        import subprocess

        from config import ICACLS_TIMEOUT_SEC

        proc = subprocess.run(
            ["icacls", str(key_path), "/inheritance:r", "/grant:r", f"{sid}:R"],
            capture_output=True, timeout=ICACLS_TIMEOUT_SEC,
        )
        if proc.returncode != 0:
            _log.warning("icacls 收紧 cred.key 权限失败 (rc=%s)", proc.returncode)
    except Exception:
        _log.debug("设置 Windows 密钥文件权限失败", exc_info=True)

    # 自检：读不出来就回滚成继承权限，避免把自己锁在外面
    try:
        key_path.read_bytes()
    except OSError:
        _log.error("收紧 cred.key 权限后无法读取，回滚为继承权限: %s", key_path)
        try:
            import subprocess

            from config import ICACLS_TIMEOUT_SEC

            subprocess.run(
                ["icacls", str(key_path), "/inheritance:e"],
                capture_output=True, timeout=ICACLS_TIMEOUT_SEC,
            )
        except Exception:
            _log.debug("回滚 cred.key 权限失败", exc_info=True)


def _create_new_key() -> bytes:
    """首次加密时创建新密钥（带权限隔离）。

    仅由 encrypt_value 在密钥缺失时调用——首次启动 / 新设备 / 配置初始化场景。
    """
    from cryptography.fernet import Fernet
    key_path = _get_cred_key_path()
    key = Fernet.generate_key()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key)
    # 设置文件仅当前用户可读
    if os.name == "nt":
        # H5 修复：不 fallback 到 'Everyone'——会导致全系统进程可读密钥文件。
        # 授权对象取进程令牌身份（_current_user_sid），不是 USERNAME 环境变量：
        # 两者不一致时会把文件授权给别的账户，自己反而读不出来（2026-09-25 实测）。
        sid = _current_user_sid()
        if not sid:
            _log.warning(
                "无法确定当前进程身份，cred.key 权限未收紧（保留继承权限）: %s", key_path,
            )
        else:
            _set_key_file_acl(key_path, sid)
    else:
        # Linux/macOS: 限制密钥文件权限
        try:
            os.chmod(key_path, 0o600)
        except Exception:
            _log.debug("设置密钥文件权限失败", exc_info=True)
    return key


def _get_or_create_key() -> bytes:
    """[Deprecated] 保留向后兼容——优先用 _load_existing_key + _create_new_key。

    新代码不应直接调用此函数。encrypt_value 内部已拆分为 load→create 两步。
    """
    key = _load_existing_key()
    if key is None:
        key = _create_new_key()
    return key


def encrypt_value(plaintext: str) -> str:
    """加密字符串 → base64。

    密钥缺失时自动创建（首次加密合法场景，不是 fail-open）。
    """
    from cryptography.fernet import Fernet
    key = _load_existing_key()
    if key is None:
        key = _create_new_key()
    f = Fernet(key)
    return f.encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    """解密 base64 → 原文。

    Fail-closed 语义：
    - 密钥缺失抛 CredentialKeyMissingError（不再静默创建新密钥掩盖问题）
    - 密文损坏/密钥不匹配抛 CredentialDecryptionError

    调用方（settings_service.get_decrypted）已有 except Exception 兜底，
    会返回 row[0]（密文原值）作为 fallback——这是为兼容旧版明文存储设计的
    行为，新代码不应依赖此 fallback，应显式处理异常。
    """
    from cryptography.fernet import Fernet, InvalidToken
    key = _load_existing_key()
    if key is None:
        raise CredentialKeyMissingError(
            f"密钥文件缺失：{_get_cred_key_path()}。无法解密已存储的凭证。"
            "请重新输入 API Key 触发 encrypt_value 创建新密钥。"
        )
    f = Fernet(key)
    try:
        return f.decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise CredentialDecryptionError(
            "密文损坏或密钥不匹配（可能 cred.key 已被替换或密文被篡改）"
        ) from e
