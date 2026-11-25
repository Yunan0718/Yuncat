"""services/backup_scheduler.py —— Batch 5.6: 定时备份调度器

独立于 agent 任务的 cron 调度（lifespan.py 的 _scheduler_tick 专用于 agent 任务）。
通过 LoopSupervisor 注册为 timer loop，每 LOOP_INTERVAL_BACKUP_SCHEDULER_SEC 秒检查一次：
1. 读 settings（enabled / cron_expr / password / retention_count / last_fire_at）
2. 评估 cron 表达式，到点则触发 backup_service.create_scheduled_backup()
3. 更新 last_fire_at

异常吞掉 + 日志——不崩 loop（LoopSupervisor 的 PERMANENT 策略兜底重启）。
"""
from __future__ import annotations

import logging
import time

from config import (
    BACKUP_DEFAULT_CRON_EXPR,
    BACKUP_DEFAULT_RETENTION_COUNT,
)
from services.cron_parser import CronParseError, next_run_at

_log = logging.getLogger("yuncat.backup_scheduler")

# settings keys
KEY_ENABLED = "backup.schedule.enabled"
KEY_CRON_EXPR = "backup.schedule.cron_expr"
KEY_RETENTION = "backup.schedule.retention_count"
KEY_PASSWORD = "backup.schedule.password"
KEY_LAST_FIRE_AT = "backup.schedule.last_fire_at"


async def _get_setting(settings_service, key: str, default: str = "") -> str:
    """从 settings_service 读取解密后的值，失败返回 default。"""
    try:
        val = await settings_service.get_decrypted(key)
        return val if val is not None else default
    except Exception:
        _log.debug("get_setting %s failed", key, exc_info=True)
        return default


async def run_backup_tick(settings_service, data_dir: str) -> dict:
    """单次调度检查——读 settings、评估 cron、到点触发备份。

    Args:
        settings_service: SettingsService 实例
        data_dir: 数据目录路径

    Returns:
        {"triggered": bool, "reason": str}
        - (False, "disabled"): 调度未启用
        - (False, "no_password"): 未配置备份密码
        - (False, "not_due"): 未到触发时间
        - (False, "invalid_cron"): cron 表达式非法
        - (True, "created"): 备份已创建
        - (True, "skipped:no_changes"): 到点但无变更，跳过
        - (False, "error:..."): 触发失败
    """
    # 1. 读 settings
    enabled_str = await _get_setting(settings_service, KEY_ENABLED, "false")
    enabled = enabled_str.lower() in ("true", "1", "yes")
    if not enabled:
        return {"triggered": False, "reason": "disabled"}

    cron_expr = await _get_setting(settings_service, KEY_CRON_EXPR, BACKUP_DEFAULT_CRON_EXPR)
    password = await _get_setting(settings_service, KEY_PASSWORD, "")
    if not password:
        _log.warning("Backup schedule enabled but no password configured")
        return {"triggered": False, "reason": "no_password"}

    retention_str = await _get_setting(settings_service, KEY_RETENTION, str(BACKUP_DEFAULT_RETENTION_COUNT))
    try:
        retention_count = int(retention_str)
    except ValueError:
        retention_count = BACKUP_DEFAULT_RETENTION_COUNT

    # 2. 评估 cron——读 last_fire_at，计算下次触发时间
    last_fire_str = await _get_setting(settings_service, KEY_LAST_FIRE_AT, "")
    try:
        last_fire_at = int(last_fire_str) if last_fire_str else 0
    except ValueError:
        last_fire_at = 0

    now = int(time.time())
    try:
        next_run = next_run_at(cron_expr, after=last_fire_at if last_fire_at > 0 else None)
    except (CronParseError, ValueError) as e:
        _log.warning("Invalid cron expression %r: %s", cron_expr, e)
        return {"triggered": False, "reason": "invalid_cron"}

    if now < next_run:
        return {"triggered": False, "reason": "not_due"}

    # 3. 到点——触发备份
    _log.info("Backup schedule triggered (cron=%s, last_fire=%d, now=%d)",
              cron_expr, last_fire_at, now)

    try:
        # 延迟导入避免循环依赖
        from services.backup_service import create_scheduled_backup
        result = create_scheduled_backup(
            data_dir=data_dir,
            password=password,
            trigger="scheduled",
            retention_count=retention_count,
        )

        # 无论是否跳过，都更新 last_fire_at（避免每次 tick 都重复评估）
        await settings_service.save(KEY_LAST_FIRE_AT, str(now))

        if result.get("created"):
            _log.info("Scheduled backup created: %s", result.get("backup_id"))
            return {"triggered": True, "reason": "created", "backup_id": result.get("backup_id")}
        else:
            _log.info("Scheduled backup skipped: %s", result.get("reason"))
            return {"triggered": True, "reason": f"skipped:{result.get('reason')}"}

    except Exception as e:
        _log.exception("Scheduled backup failed")
        # 失败也更新 last_fire_at，避免每次 tick 都重试（下次 cron 到点再试）
        try:
            await settings_service.save(KEY_LAST_FIRE_AT, str(now))
        except Exception:
            pass
        return {"triggered": False, "reason": f"error:{e}"}
