"""routes/backup.py —— Phase 96: 配置备份/恢复端点 + Batch 5.6 增量备份管理。

Phase 96（手动全量）:
- POST /api/v1/config/backup/export: checkpoint WAL → 加密打包 yuncat.db + cred.key → 返回二进制
- POST /api/v1/config/backup/import: 接收 {path, password} → 读文件 → stage_restore → 返回 restart_required

Batch 5.6（增量 + 调度 + 历史）:
- GET  /api/v1/config/backup/list: 列出备份历史
- GET  /api/v1/config/backup/settings: 获取调度设置（密码返回 has_password 布尔，不返回明文）
- PUT  /api/v1/config/backup/settings: 更新调度设置
- POST /api/v1/config/backup/trigger: 手动触发增量备份
- DELETE /api/v1/config/backup/{backup_id}: 删除指定备份
- GET  /api/v1/config/backup/{backup_id}/download: 下载指定备份文件

设计要点：
- 导出前 `PRAGMA wal_checkpoint(TRUNCATE)` 把 WAL 刷入主库，保证备份自洽
- 导入用文件路径而非 HTTP body——绕过 10MB body 限制（yuncat.db 可能较大）
- 导入只写 staging + 标记，不替换真实文件——前端调 relaunchApp 重启后 lifespan 应用恢复
- 调度密码加密存储于 settings（backup.schedule.password），GET 不返回明文
"""
import logging
import os
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from config import BACKUP_DEFAULT_CRON_EXPR, BACKUP_DEFAULT_RETENTION_COUNT
from services.backup_service import (
    BackupError,
    create_scheduled_backup,
    delete_backup,
    export_backup,
    get_backup_file,
    list_backups,
    stage_restore,
)

router = APIRouter(prefix="/api/v1", tags=["backup"])
_log = logging.getLogger("yuncat.backup")


def _get_data_dir(request: Request) -> str:
    """获取 data_dir，优先从 app.state，回退到 config.DATA_DIR。"""
    data_dir = getattr(request.app.state, "data_dir", None)
    if not data_dir:
        from config import DATA_DIR
        data_dir = DATA_DIR
    return data_dir



@router.post("/config/backup/export")
async def export_backup_route(request: Request):
    """加密导出 yuncat.db + cred.key 为二进制备份。

    Body: {"password": "user-chosen-password"}
    Returns: application/octet-stream（salt + Fernet 密文）
    """
    body = await request.json()
    password = body.get("password", "")
    if not password or len(password) < 4:
        return Response(
            content='{"error": "password too short (min 4 chars)"}',
            status_code=400,
            media_type="application/json",
        )

    db = request.app.state.db
    data_dir = _get_data_dir(request)

    # WAL checkpoint——把未提交到主库的 WAL 数据刷入，保证备份自洽
    try:
        await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as e:
        _log.warning("wal_checkpoint before backup failed: %s", e)

    try:
        backup_bytes = export_backup(data_dir, password)
    except FileNotFoundError:
        return Response(
            content='{"error": "yuncat.db not found — nothing to backup"}',
            status_code=404,
            media_type="application/json",
        )

    filename = f"yuncat-backup-{int(time.time())}.ybk"
    return Response(
        content=backup_bytes,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class ImportRequest(BaseModel):
    path: str
    password: str


@router.post("/config/backup/import")
async def import_backup_route(req: ImportRequest, request: Request):
    """从文件路径导入加密备份——校验 + 写 staging + 标记 restart_required。

    Body: {"path": "/abs/path/to/backup.ybk", "password": "..."}
    Returns: {"ok": true, "restart_required": true} 或 {"error": "..."}
    """
    backup_path = req.path
    # 路径安全校验：必须是存在的文件，且不在系统敏感目录
    if not backup_path or not os.path.isabs(backup_path):
        return {"error": "invalid path: must be absolute"}
    if not os.path.isfile(backup_path):
        return {"error": "file not found"}
    # 拒绝系统目录（与 main.js _isSafePath 同理）
    sensitive = ("/etc/", "/usr/", "/bin/", "/sbin/", "/root/")
    norm = backup_path.replace("\\", "/").lower()
    if any(norm.startswith(s) for s in sensitive):
        return {"error": "unsafe path"}

    try:
        with open(backup_path, "rb") as f:
            backup_bytes = f.read()
    except OSError as e:
        return {"error": f"read failed: {e}"}

    data_dir = _get_data_dir(request)

    try:
        result = stage_restore(data_dir, backup_bytes, req.password)
    except BackupError as e:
        return {"error": str(e)}
    except Exception as e:
        _log.exception("import_backup failed")
        return {"error": f"import failed: {e}"}

    _log.info("Config restore staged — restart required to apply")
    return result


# ============================================================
# Batch 5.6: 增量备份 + 调度 + 历史管理
# ============================================================


@router.get("/config/backup/list")
async def list_backups_route(request: Request, limit: int = 50):
    """列出备份历史，按 created_at DESC。"""
    data_dir = _get_data_dir(request)
    backups = list_backups(data_dir, limit=limit)
    return {"items": backups, "count": len(backups)}


@router.get("/config/backup/settings")
async def get_backup_settings_route(request: Request):
    """获取备份调度设置。

    密码不返回明文——只返回 has_password 布尔。
    """
    svc = request.app.state.settings_service

    enabled_str = await svc.get_decrypted("backup.schedule.enabled") or "false"
    cron_expr = await svc.get_decrypted("backup.schedule.cron_expr") or BACKUP_DEFAULT_CRON_EXPR
    retention_str = await svc.get_decrypted("backup.schedule.retention_count") or str(BACKUP_DEFAULT_RETENTION_COUNT)
    password = await svc.get_decrypted("backup.schedule.password") or ""

    return {
        "enabled": enabled_str.lower() in ("true", "1", "yes"),
        "cron_expr": cron_expr,
        "retention_count": int(retention_str) if retention_str.isdigit() else BACKUP_DEFAULT_RETENTION_COUNT,
        "has_password": bool(password),
    }


class BackupScheduleSettings(BaseModel):
    enabled: bool | None = None
    cron_expr: str | None = None
    retention_count: int | None = None
    password: str | None = None  # None=不改，""=清除，非空=更新


@router.put("/config/backup/settings")
async def update_backup_settings_route(settings: BackupScheduleSettings, request: Request):
    """更新备份调度设置。

    - password=None: 不改密码
    - password="": 清除密码（禁用调度）
    - password="newpass": 更新密码
    """
    svc = request.app.state.settings_service

    if settings.enabled is not None:
        await svc.save("backup.schedule.enabled", "true" if settings.enabled else "false")
    if settings.cron_expr is not None:
        # 校验 cron 表达式
        try:
            from services.cron_parser import parse_cron
            parse_cron(settings.cron_expr)
        except Exception as e:
            raise HTTPException(400, f"invalid cron expression: {e}")
        await svc.save("backup.schedule.cron_expr", settings.cron_expr)
    if settings.retention_count is not None:
        if settings.retention_count < 1 or settings.retention_count > 100:
            raise HTTPException(400, "retention_count must be between 1 and 100")
        await svc.save("backup.schedule.retention_count", str(settings.retention_count))
    if settings.password is not None:
        if len(settings.password) < 4 and len(settings.password) > 0:
            raise HTTPException(400, "password too short (min 4 chars)")
        await svc.save("backup.schedule.password", settings.password)

    return {"ok": True}


@router.post("/config/backup/trigger")
async def trigger_backup_route(request: Request):
    """手动触发增量备份。

    需要先配置密码（通过 settings 端点）。
    """
    svc = request.app.state.settings_service
    password = await svc.get_decrypted("backup.schedule.password") or ""
    if not password:
        raise HTTPException(400, "no backup password configured")

    retention_str = await svc.get_decrypted("backup.schedule.retention_count") or str(BACKUP_DEFAULT_RETENTION_COUNT)
    try:
        retention_count = int(retention_str)
    except ValueError:
        retention_count = BACKUP_DEFAULT_RETENTION_COUNT

    data_dir = _get_data_dir(request)
    db = request.app.state.db

    # WAL checkpoint
    try:
        await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as e:
        _log.warning("wal_checkpoint before manual backup failed: %s", e)

    try:
        result = create_scheduled_backup(
            data_dir=data_dir,
            password=password,
            trigger="manual",
            retention_count=retention_count,
        )
    except FileNotFoundError:
        raise HTTPException(404, "yuncat.db not found — nothing to backup")
    except Exception as e:
        _log.exception("manual backup trigger failed")
        raise HTTPException(500, f"backup failed: {e}")

    return result


@router.delete("/config/backup/{backup_id}")
async def delete_backup_route(backup_id: str, request: Request):
    """删除指定备份。"""
    data_dir = _get_data_dir(request)
    deleted = delete_backup(data_dir, backup_id)
    if not deleted:
        raise HTTPException(404, "backup not found")
    return {"ok": True, "deleted": backup_id}


@router.get("/config/backup/{backup_id}/download")
async def download_backup_route(backup_id: str, request: Request):
    """下载指定备份的加密文件。"""
    data_dir = _get_data_dir(request)
    enc_path = get_backup_file(data_dir, backup_id)
    if enc_path is None:
        raise HTTPException(404, "backup file not found")

    filename = f"{backup_id}.ybk"
    return FileResponse(
        path=str(enc_path),
        media_type="application/octet-stream",
        filename=filename,
    )

