"""Phase 96: 配置备份/恢复服务——加密 zip 打包 yuncat.db + cred.key。

流程：
- export_backup: 读 yuncat.db + cred.key → 打 zip → Fernet 加密（PBKDF2 派生密钥）→ salt(16) + 密文
- validate_backup: 解密 → 校验 zip + SQLite 文件头 → 返回内含文件名（无副作用）
- stage_restore: validate → 写 staging 文件（.restore 后缀）+ .restore-pending 标记（不替换真实文件）
- apply_pending_restore: 启动时检查标记 → 替换真实文件 → 删标记 → 返回 True

Batch 5.6 扩展（增量 + 调度 + 历史）：
- should_backup: 比对当前 yuncat.db 的 mtime+size 与上次 manifest，不同则算 sha256 二次确认
- create_scheduled_backup: 变更才备份，写 manifest，更新 _last_manifest.json，触发 cleanup_old_backups
- list_backups / delete_backup / cleanup_old_backups: 历史管理与保留策略

安全：
- 密码经 PBKDF2-HMAC-SHA256（10 万次迭代）派生 Fernet 密钥
- salt 随机生成，前置在密文前（16 bytes）——每次备份不同
- cred.key 与 yuncat.db 一起加密——备份密码保护所有 API Key
- 导入先写 staging，重启后替换——避免运行中替换打开的 DB（Windows 文件锁）

WAL 一致性：调用方（路由）在 export 前执行 `PRAGMA wal_checkpoint(TRUNCATE)` 把 WAL 刷入主库，
这样备份的 yuncat.db 是自洽的（无需 -wal / -shm）。恢复时删除残留 -wal / -shm 再替换主库。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import time
import uuid
import zipfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from config import (
    BACKUP_DEFAULT_RETENTION_COUNT,
    BACKUP_LIST_DEFAULT_LIMIT,
    BACKUP_SHA256_CHUNK_SIZE,
)

_log = logging.getLogger("yuncat.backup")

SALT_LEN = 16
PBKDF2_ITERATIONS = 100_000
# 备份包含的文件（位于 DATA_DIR）——cred.key 可能不存在（无 API Key 时）
BACKUP_FILES: tuple[str, ...] = ("yuncat.db", "cred.key")
SQLITE_HEADER = b"SQLite format 3\x00"
STAGING_SUFFIX = ".restore"
RESTORE_PENDING_FLAG = ".restore-pending"

# Batch 5.6: 增量备份相关常量
BACKUP_DIR_NAME = "backups"
LAST_MANIFEST_FILE = "_last_manifest.json"
MANIFEST_SUFFIX = ".manifest.json"
ENC_SUFFIX = ".enc"



class BackupError(ValueError):
    """备份/恢复失败——密码错误、文件损坏、格式非法等。"""


def _derive_key(password: str, salt: bytes) -> bytes:
    """PBKDF2-HMAC-SHA256 从密码 + salt 派生 Fernet 密钥（urlsafe base64）。"""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def export_backup(data_dir: str, password: str) -> bytes:
    """打包 yuncat.db + cred.key 为加密 zip，返回 salt(16) + Fernet 密文。

    Raises:
        FileNotFoundError: yuncat.db 不存在（无可备份的数据）
    """
    db_path = Path(data_dir) / "yuncat.db"
    if not db_path.exists():
        raise FileNotFoundError("yuncat.db not found — nothing to backup")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(db_path, "yuncat.db")
        cred_path = Path(data_dir) / "cred.key"
        if cred_path.exists():
            zf.write(cred_path, "cred.key")
    zip_bytes = buf.getvalue()

    salt = os.urandom(SALT_LEN)
    key = _derive_key(password, salt)
    encrypted = Fernet(key).encrypt(zip_bytes)
    return salt + encrypted


def validate_backup(backup_bytes: bytes, password: str) -> list[str]:
    """解密 + 校验备份，返回内含文件名列表（无副作用）。

    Raises:
        BackupError: 密码错误 / 文件损坏 / 格式非法 / SQLite 文件头不匹配
    """
    if len(backup_bytes) < SALT_LEN:
        raise BackupError("invalid backup: too short")
    salt = backup_bytes[:SALT_LEN]
    encrypted = backup_bytes[SALT_LEN:]
    key = _derive_key(password, salt)
    try:
        zip_bytes = Fernet(key).decrypt(encrypted)
    except InvalidToken:
        raise BackupError("wrong password or corrupt backup")

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            names = zf.namelist()
            # 安全校验：只允许白名单文件
            unexpected = [n for n in names if n not in BACKUP_FILES]
            if unexpected:
                raise BackupError(f"unexpected files in backup: {unexpected}")
            # SQLite 文件头校验——确保 yuncat.db 是有效数据库
            if "yuncat.db" in names:
                header = zf.read("yuncat.db")[:16]
                if not header.startswith(b"SQLite format"):
                    raise BackupError("invalid yuncat.db: not a SQLite database")
            return names
    except zipfile.BadZipFile:
        raise BackupError("corrupt backup: not a valid zip")


def stage_restore(data_dir: str, backup_bytes: bytes, password: str) -> dict:
    """校验备份 + 写 staging 文件 + 写 .restore-pending 标记。

    不替换真实文件——等 apply_pending_restore 在下次启动时替换。
    Returns: {"ok": True, "files": [...], "restart_required": True}
    """
    names = validate_backup(backup_bytes, password)

    # 重新解密取文件内容（validate_backup 已校验过，这里不会失败）
    salt = backup_bytes[:SALT_LEN]
    key = _derive_key(password, salt)
    zip_bytes = Fernet(key).decrypt(backup_bytes[SALT_LEN:])

    data = Path(data_dir)
    data.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        for name in BACKUP_FILES:
            if name not in names:
                continue
            staging = data / f"{name}{STAGING_SUFFIX}"
            with open(staging, "wb") as f:
                f.write(zf.read(name))

    # 写 restore-pending 标记——下次启动时 apply_pending_restore 检查
    (data / RESTORE_PENDING_FLAG).write_text(str(os.path.getmtime(data / "yuncat.db")), "utf-8")
    return {"ok": True, "files": [n for n in BACKUP_FILES if n in names], "restart_required": True}


def apply_pending_restore(data_dir: str) -> bool:
    """启动时调用——若存在 .restore-pending 标记，用 staging 文件替换真实文件。

    Returns: True 表示执行了恢复（DB 文件已替换），False 表示无待恢复。
    """
    data = Path(data_dir)
    flag = data / RESTORE_PENDING_FLAG
    if not flag.exists():
        return False

    try:
        # 删除残留的 WAL/SHM（旧 DB 的 WAL 状态，对新 DB 无意义）
        for suffix in ("-wal", "-shm"):
            wal = data / f"yuncat.db{suffix}"
            if wal.exists():
                try:
                    wal.unlink()
                except OSError:
                    pass

        # 用 staging 文件替换真实文件（os.replace 原子替换，Windows/Unix 通用）
        for name in BACKUP_FILES:
            staging = data / f"{name}{STAGING_SUFFIX}"
            if staging.exists():
                os.replace(staging, data / name)

        return True
    finally:
        # 无论成功失败都删标记——避免重启循环
        try:
            flag.unlink()
        except OSError:
            pass


# ============================================================
# Batch 5.6: 增量备份 + 历史管理 + 保留策略
# ============================================================


def get_backup_dir(data_dir: str) -> Path:
    """返回备份目录路径，不存在则创建。"""
    backup_dir = Path(data_dir) / BACKUP_DIR_NAME
    backup_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir


def _sha256_file(path: Path) -> str:
    """分块计算文件 sha256，避免大文件内存爆炸。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(BACKUP_SHA256_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _file_meta(path: Path) -> dict:
    """收集单个备份文件的元数据（size/mtime/sha256）。"""
    stat = path.stat()
    return {
        "name": path.name,
        "size": stat.st_size,
        "mtime": int(stat.st_mtime),
        "sha256": _sha256_file(path),
    }


def _generate_backup_id() -> str:
    """生成 backup_id: backup-YYYYMMDD-HHMMSS-{short_uuid}。"""
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    short_uuid = uuid.uuid4().hex[:6]
    return f"backup-{ts}-{short_uuid}"


def get_last_manifest(data_dir: str) -> dict | None:
    """读取最近一次成功备份的 manifest，用于增量比对。"""
    manifest_path = get_backup_dir(data_dir) / LAST_MANIFEST_FILE
    if not manifest_path.exists():
        return None
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        _log.warning("last manifest corrupt, ignoring: %s", manifest_path)
        return None


def should_backup(data_dir: str) -> tuple[bool, str]:
    """判断是否需要备份——比对当前 yuncat.db 与上次 manifest。

    Returns:
        (need_backup, reason)
        - (True, "no_previous_backup"): 首次备份
        - (True, "mtime_changed"): mtime 不同
        - (True, "size_changed"): size 不同
        - (True, "content_changed"): mtime+size 相同但 sha256 不同（罕见——文件被原地修改）
        - (False, "no_changes"): 完全一致
        - (True, "db_missing"): yuncat.db 不存在（不应发生，但 fail-safe 触发备份流程让上层报错）
    """
    db_path = Path(data_dir) / "yuncat.db"
    if not db_path.exists():
        return True, "db_missing"

    last = get_last_manifest(data_dir)
    if not last:
        return True, "no_previous_backup"

    # 找到上次 manifest 中 yuncat.db 的元数据
    last_files = last.get("files", [])
    last_db = next((f for f in last_files if f.get("name") == "yuncat.db"), None)
    if not last_db:
        return True, "no_previous_backup"

    current_stat = db_path.stat()
    current_mtime = int(current_stat.st_mtime)
    current_size = current_stat.st_size

    if current_mtime != last_db.get("mtime"):
        return True, "mtime_changed"
    if current_size != last_db.get("size"):
        return True, "size_changed"

    # mtime+size 相同——算 sha256 二次确认（防止原地修改 mtime 不变的罕见情况）
    current_sha = _sha256_file(db_path)
    if current_sha != last_db.get("sha256"):
        return True, "content_changed"

    return False, "no_changes"


def create_scheduled_backup(
    data_dir: str,
    password: str,
    trigger: str = "scheduled",
    retention_count: int = BACKUP_DEFAULT_RETENTION_COUNT,
) -> dict:
    """创建增量备份——变更才备份，否则跳过。

    Args:
        data_dir: 数据目录
        password: 备份加密密码
        trigger: "scheduled" | "manual"
        retention_count: 保留备份数，超出则清理最老的

    Returns:
        {"created": True, "backup_id": ..., "manifest": ...} 或
        {"created": False, "skipped": True, "reason": "no_changes"}
    """
    need, reason = should_backup(data_dir)
    if not need:
        _log.info("Backup skipped: %s", reason)
        return {"created": False, "skipped": True, "reason": reason}

    # 调用现有 export_backup 生成加密 zip
    encrypted_bytes = export_backup(data_dir, password)

    backup_id = _generate_backup_id()
    backup_dir = get_backup_dir(data_dir)
    enc_path = backup_dir / f"{backup_id}{ENC_SUFFIX}"

    # 原子写入：先写临时文件再 rename（避免崩溃产生半成品）
    tmp_enc = enc_path.with_suffix(".enc.tmp")
    with open(tmp_enc, "wb") as f:
        f.write(encrypted_bytes)
    os.replace(tmp_enc, enc_path)

    # 收集当前文件元数据用于 manifest
    files_meta = []
    for name in BACKUP_FILES:
        path = Path(data_dir) / name
        if path.exists():
            files_meta.append(_file_meta(path))

    manifest = {
        "id": backup_id,
        "created_at": int(time.time()),
        "trigger": trigger,
        "changed_since_last": True,
        "change_reason": reason,
        "files": files_meta,
        "encrypted_size": len(encrypted_bytes),
    }

    # 原子写 manifest
    manifest_path = backup_dir / f"{backup_id}{MANIFEST_SUFFIX}"
    tmp_manifest = manifest_path.with_suffix(".manifest.json.tmp")
    with open(tmp_manifest, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    os.replace(tmp_manifest, manifest_path)

    # 更新 _last_manifest.json（增量比对基准）
    last_manifest_path = backup_dir / LAST_MANIFEST_FILE
    tmp_last = last_manifest_path.with_suffix(".json.tmp")
    with open(tmp_last, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    os.replace(tmp_last, last_manifest_path)

    # 触发保留策略清理
    deleted = cleanup_old_backups(data_dir, retention_count)

    _log.info(
        "Backup created: %s (trigger=%s, reason=%s, encrypted_size=%d, cleaned=%d)",
        backup_id, trigger, reason, len(encrypted_bytes), deleted,
    )
    return {"created": True, "backup_id": backup_id, "manifest": manifest, "cleaned": deleted}


def list_backups(data_dir: str, limit: int = BACKUP_LIST_DEFAULT_LIMIT) -> list[dict]:
    """列出备份历史，按 created_at DESC 排序。"""
    backup_dir = get_backup_dir(data_dir)
    manifests = []
    for entry in backup_dir.iterdir():
        if not entry.name.endswith(MANIFEST_SUFFIX):
            continue
        if entry.name == LAST_MANIFEST_FILE:
            continue
        try:
            with open(entry, "r", encoding="utf-8") as f:
                manifests.append(json.load(f))
        except (json.JSONDecodeError, OSError):
            _log.warning("corrupt manifest ignored: %s", entry)
            continue
    manifests.sort(key=lambda m: m.get("created_at", 0), reverse=True)
    return manifests[:limit]


def delete_backup(data_dir: str, backup_id: str) -> bool:
    """删除指定备份的 .enc 和 .manifest.json 文件。

    不允许删除 _last_manifest.json（它是增量比对基准，不是备份记录）。
    """
    if backup_id.startswith("_"):
        return False  # 保护 _last_manifest.json 等内部文件

    backup_dir = get_backup_dir(data_dir)
    enc_path = backup_dir / f"{backup_id}{ENC_SUFFIX}"
    manifest_path = backup_dir / f"{backup_id}{MANIFEST_SUFFIX}"

    deleted = False
    if enc_path.exists():
        try:
            enc_path.unlink()
            deleted = True
        except OSError:
            _log.warning("failed to delete %s", enc_path, exc_info=True)
    if manifest_path.exists():
        try:
            manifest_path.unlink()
            deleted = True
        except OSError:
            _log.warning("failed to delete %s", manifest_path, exc_info=True)

    return deleted


def cleanup_old_backups(data_dir: str, retention_count: int) -> int:
    """保留最近 retention_count 个备份，删除更老的。

    Returns:
        删除的备份数
    """
    if retention_count <= 0:
        return 0

    backups = list_backups(data_dir, limit=10_000)  # 全量列出再截断
    if len(backups) <= retention_count:
        return 0

    to_delete = backups[retention_count:]
    deleted = 0
    for manifest in to_delete:
        backup_id = manifest.get("id")
        if backup_id and delete_backup(data_dir, backup_id):
            deleted += 1

    if deleted:
        _log.info("cleanup_old_backups: removed %d backups (retention=%d)", deleted, retention_count)
    return deleted


def get_backup_file(data_dir: str, backup_id: str) -> Path | None:
    """返回指定备份的 .enc 文件路径，不存在返回 None。"""
    if backup_id.startswith("_"):
        return None
    enc_path = get_backup_dir(data_dir) / f"{backup_id}{ENC_SUFFIX}"
    return enc_path if enc_path.exists() else None

