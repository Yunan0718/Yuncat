"""services/settings_service.py —— 设置服务（v56: api_key 加密存储；v77: TTL 缓存）"""
import asyncio
import logging
import time as _time

# Fernet 令牌前缀的单源在 security/credentials.py（那边同时用它判定密文/明文）。
from security.credentials import FERNET_TOKEN_PREFIX, KEY_STATE_MISSING

_log = logging.getLogger("yuncat.settings")

# 需要加密存储的 key（模型配置 JSON 内含 api_key，整体加密）
# Batch 5.6: backup.schedule.password 加密存储——丢失 cred.key 后无法解密，
# 但 cred.key 丢失本身即意味着所有 API Key 失守，备份密码无独立存在的意义
ENCRYPTED_KEYS = {
    "api_key", "primary_model", "multimodal_model",
    "github_token", "gitlab_token",
    "backup.schedule.password",
}

# Fernet 令牌的固定前缀（版本字节 0x80 + 时间戳，base64url 编码后总是 gAAAAA 开头）。
# 用它把「历史上以明文存的旧数据」与「本该能解却解不开的密文」区分开：
# 前者要兼容放行，后者必须报错——两者的处置正好相反。
_FERNET_PREFIX = FERNET_TOKEN_PREFIX


def _looks_like_ciphertext(value: str) -> bool:
    """粗判一个值是否为 Fernet 密文。"""
    return bool(value) and value.startswith(_FERNET_PREFIX)


async def _encrypted_setting_keys(db) -> list[str]:
    """列出「值看起来是 Fernet 密文」的设置项（用于密钥丢失时的影响面提示）。"""
    cursor = await db.execute("SELECT key, value FROM settings")
    rows = await cursor.fetchall()
    return [r[0] for r in rows if _looks_like_ciphertext(r[1] or "")]


async def _encrypt_for_storage(db, key: str, value: str) -> str:
    """加密待存值；本机密钥已缺失而库里仍有密文时，先大声告警再建新密钥。

    「库里有密文 + cred.key 缺失」= 用户先前保存的凭证已经解不开了（典型成因：
    卸载/清理对数据目录做了部分删除，库留下、密钥没了）。此时仍允许保存——用户
    正是在重新填写——但必须留下可追溯记录并给出恢复路径，否则就是静默丢失。
    """
    try:
        from security.credentials import encrypt_value, credential_key_state
    except ImportError as e:
        _log.error("cryptography not installed, refusing to store %s as plaintext: %s", key, e)
        raise RuntimeError(
            "无法安全存储 API Key：cryptography 未安装。"
            "请运行 pip install cryptography 后重试。"
        ) from e
    if credential_key_state() == KEY_STATE_MISSING:
        orphaned = await _encrypted_setting_keys(db)
        if orphaned:
            _log.error(
                "本地加密密钥（cred.key）缺失，但数据库里还有 %d 项密文：%s。"
                "即将新建密钥，这些凭证将无法再解开。若做过配置备份，可从备份包取回 "
                "cred.key 覆盖后重启；否则请在 设置 > 模型配置 重新填写。",
                len(orphaned), ", ".join(orphaned),
            )
    return encrypt_value(value)


class SettingsService:
    def __init__(self, db, cache_ttl: float | None = None):
        self.db = db
        # Batch 7.9: TTL 缓存——减少热点配置查询的 DB 压力
        from config import SETTINGS_SERVICE_CACHE_TTL_SEC
        self._cache_ttl = (
            SETTINGS_SERVICE_CACHE_TTL_SEC if cache_ttl is None else float(cache_ttl)
        )
        self._cache: dict[str, tuple[float, str | None]] = {}
        self._all_cache: tuple[float, dict] | None = None
        self._cache_lock = asyncio.Lock()

    def _cache_now(self) -> float:
        """使用 monotonic 时钟避免系统时间回拨导致缓存永不失效。"""
        return _time.monotonic()

    async def _get_raw_from_db(self, key: str) -> str | None:
        cursor = await self.db.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def get_all(self) -> dict:
        """返回所有设置（加密密钥的值替换为掩码）"""
        if self._cache_ttl <= 0:
            return await self._get_all_from_db()

        now = self._cache_now()
        async with self._cache_lock:
            if self._all_cache is not None and now - self._all_cache[0] < self._cache_ttl:
                return self._all_cache[1]

        result = await self._get_all_from_db()
        async with self._cache_lock:
            self._all_cache = (now, result)
        return result

    async def _get_all_from_db(self) -> dict:
        cursor = await self.db.execute("SELECT key, value FROM settings")
        rows = await cursor.fetchall()
        result = {}
        for r in rows:
            if r[1] and (r[0] in ENCRYPTED_KEYS or r[0].startswith("api_key:")):
                result[r[0]] = "***encrypted***"
            else:
                result[r[0]] = r[1]
        return result

    async def save(self, key: str, value: str, expected_version: int | None = None) -> bool | None:
        """保存设置——敏感密钥自动加密。

        A2: 乐观锁支持。
        - expected_version=None（默认）：upsert + version 自增，返回 None（向后兼容）。
        - expected_version 提供：仅当 DB 中 version 匹配时才更新，返回 True/False。
          expected_version=0 表示"仅当 key 不存在时创建"。
        """
        stored_value = value
        if value and (key in ENCRYPTED_KEYS or key.startswith("api_key:")):
            stored_value = await _encrypt_for_storage(self.db, key, value)

        if expected_version is not None:
            # A2: 乐观锁路径
            if expected_version == 0:
                # 仅当 key 不存在时创建——INSERT OR IGNORE 避免覆盖已有
                cursor = await self.db.execute(
                    "INSERT OR IGNORE INTO settings(key, value, version, updated_at) "
                    "VALUES(?,?,1,CAST(strftime('%s','now') AS INTEGER))",
                    (key, stored_value),
                )
                await self.db.commit()
                if cursor.rowcount > 0:
                    await self._invalidate_cache(key)
                return cursor.rowcount > 0
            else:
                # 仅当 version 匹配时更新——原子 CAS
                cursor = await self.db.execute(
                    "UPDATE settings SET value=?, version=version+1, "
                    "updated_at=CAST(strftime('%s','now') AS INTEGER) "
                    "WHERE key=? AND version=?",
                    (stored_value, key, expected_version),
                )
                await self.db.commit()
                if cursor.rowcount > 0:
                    await self._invalidate_cache(key)
                return cursor.rowcount > 0

        # 默认 upsert 路径——version 自增
        await self.db.execute(
            "INSERT INTO settings(key, value, version, updated_at) "
            "VALUES(?,?,1,CAST(strftime('%s','now') AS INTEGER)) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "version=settings.version+1, updated_at=excluded.updated_at",
            (key, stored_value),
        )
        await self.db.commit()
        await self._invalidate_cache(key)
        return None

    async def _invalidate_cache(self, key: str | None = None) -> None:
        """写操作后失效缓存；key=None 时清空全部缓存。"""
        async with self._cache_lock:
            if key is None:
                self._cache.clear()
                self._all_cache = None
            else:
                self._cache.pop(key, None)
                self._all_cache = None

    async def get_raw(self, key: str) -> str | None:
        """获取原始值（不解密）"""
        if self._cache_ttl <= 0:
            return await self._get_raw_from_db(key)

        now = self._cache_now()
        async with self._cache_lock:
            entry = self._cache.get(key)
            if entry is not None and now - entry[0] < self._cache_ttl:
                return entry[1]

        value = await self._get_raw_from_db(key)
        async with self._cache_lock:
            self._cache[key] = (now, value)
        return value

    async def get_decrypted(self, key: str) -> str | None:
        """获取解密后的值"""
        raw = await self.get_raw(key)
        if not raw:
            return None
        if key not in ENCRYPTED_KEYS and not key.startswith("api_key:"):
            return raw
        try:
            from security.credentials import decrypt_value
            return decrypt_value(raw)
        except Exception as e:
            if not _looks_like_ciphertext(raw):
                # 历史明文数据（早期版本直接存 sk-xxx）：不是错误，按原值使用
                _log.info("%s 以明文存储（历史数据），按原值使用", key)
                return raw
            # 是密文却解不开 = 凭证不可用。绝不能把密文当明文返回：它会被拼成
            # "Authorization: Bearer gAAAAA..." 发给厂商，换回一个与真实原因无关的 401，
            # 用户只看到「发消息没反应」，真实原因（cred.key 被替换/丢失）只在日志里。
            _log.error(
                "%s 解密失败，凭证不可用（密文无法用当前 cred.key 解开）：%s。"
                "若做过配置备份，可从备份包取回 cred.key 覆盖后重启；"
                "否则请在 设置 > 模型配置 重新填写该项。", key, e,
            )
            return None

    # ---- v64: 模型配置迁移至 models.yaml + model_config_service ----
    # 旧的 VENDOR_PRESETS / QUICK_PRESETS / get_model_config / save_model_config
    # / save_model_preset 已删除。模型配置统一走 ModelConfigService。

    # ---- H3: API key 轮换年龄追踪 ----
    async def get_key_updated_at(self, key: str) -> int | None:
        """获取某 setting 的 updated_at 时间戳（Unix 秒）。

        用于 H3 key 轮换提醒：settings.updated_at 列记录最后写入时间。
        旧库若未迁移（updated_at 为 NULL），回退用 0 表示"未知年龄"。
        """
        cursor = await self.db.execute(
            "SELECT updated_at FROM settings WHERE key=?", (key,)
        )
        row = await cursor.fetchone()
        if not row or row[0] is None:
            return None
        return int(row[0])

    async def get_key_age_days(self, key: str) -> int | None:
        """获取某 setting 的年龄（天数）。None 表示 key 不存在或 updated_at 缺失。"""
        updated_at = await self.get_key_updated_at(key)
        if updated_at is None:
            return None
        import time as _t
        return max(0, int((_t.time() - updated_at) // 86400))
