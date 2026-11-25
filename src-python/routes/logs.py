"""routes/logs.py —— Phase 96-5: 只读日志查看器端点。

- GET /api/v1/logs/tail: 读取 yuncat.log 最后 N 行，支持 level + keyword 过滤

日志格式（Task 7.15 起为 JSON Lines）：
  {"timestamp":"2026-07-12T10:30:00.123+08:00","level":"INFO","logger":"yuncat","message":"..."}
旧格式（轮转文件残留）：
  2026-06-30 12:00:00,000 [INFO] yuncat.lifespan: Starting Yuncat backend

设计：
- 日志文件 ≤ 10MB（RotatingFileHandler maxBytes），可安全全量读入内存
- Task 7.15: JSON 行解析后按 level/message 字段过滤；旧纯文本行走 fallback 子串匹配
- keyword 过滤大小写不敏感子串匹配
- lines 参数限制最大 5000 防滥用
- 返回人类可读格式（JSON 格式化回文本）——前端 LogViewer 零改动
"""
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import Response

from config import DATA_DIR
import time

router = APIRouter(prefix="/api/v1", tags=["logs"])
_log = logging.getLogger("yuncat.logs")

LOG_FILENAME = "yuncat.log"
MAX_LINES = 5000
DEFAULT_LINES = 500
VALID_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _parse_log_line(line: str) -> dict | None:
    """尝试将日志行解析为 JSON。非 JSON 行返回 None。

    Task 7.15: JSON Lines 格式日志解析——用于 level/message 字段精确过滤。
    旧纯文本行（轮转文件残留或非 JSON 日志）返回 None，走 fallback 子串匹配。
    """
    stripped = line.strip()
    if not stripped or not stripped.startswith("{"):
        return None
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None


def _format_log_line(parsed: dict) -> str:
    """将解析后的 JSON 日志条目格式化为人类可读字符串。

    格式: 2026-07-12 10:30:00.123 [INFO] yuncat [trace=abc agent=hermes]: message
    保留 [LEVEL] 子串——前端 LogViewer 颜色编码依赖此标记。
    """
    ts = parsed.get("timestamp", "")
    # ISO 时间戳 → 人类可读: 2026-07-12T10:30:00.123+08:00 → 2026-07-12 10:30:00.123
    try:
        dt = datetime.fromisoformat(ts)
        ts_display = dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    except (ValueError, TypeError):
        ts_display = ts

    level = parsed.get("level", "?")
    logger_name = parsed.get("logger", "")
    message = parsed.get("message", "")

    # 上下文字段（紧凑显示）
    ctx_parts: list[str] = []
    if parsed.get("trace_id"):
        ctx_parts.append(f"trace={parsed['trace_id']}")
    if parsed.get("agent_id"):
        ctx_parts.append(f"agent={parsed['agent_id']}")
    if parsed.get("task_id"):
        ctx_parts.append(f"task={parsed['task_id']}")
    ctx_str = f" [{' '.join(ctx_parts)}]" if ctx_parts else ""

    return f"{ts_display} [{level}] {logger_name}{ctx_str}: {message}"


@router.get("/logs/tail")
async def tail_logs(
    request: Request,
    lines: int = DEFAULT_LINES,
    level: str = "",
    q: str = "",
):
    """读取 yuncat.log 最后 N 行，可按 level + keyword 过滤。

    Query params:
      lines: 返回行数（默认 500，上限 5000）
      level: 日志级别过滤（DEBUG/INFO/WARNING/ERROR/CRITICAL，空=全部）
      q:     关键字过滤（大小写不敏感子串匹配）

    Returns: {"lines": [...], "total": int, "filtered": int, "truncated": bool}
    """
    # 参数校验
    lines = max(1, min(lines, MAX_LINES))
    level = level.upper().strip() if level else ""
    if level and level not in VALID_LEVELS:
        return {"error": f"invalid level: {level}"}
    q_lower = q.lower() if q else ""

    log_path = Path(DATA_DIR) / LOG_FILENAME
    if not log_path.exists():
        return {"lines": [], "total": 0, "filtered": 0, "truncated": False, "note": "log file not yet created"}

    try:
        # 日志文件 ≤ 10MB（RotatingFileHandler），可安全全量读入
        raw = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        _log.warning("Failed to read log file: %s", e)
        return {"error": f"read failed: {e}"}

    all_lines = raw.splitlines()
    total = len(all_lines)

    # 取最后 N 行（先截断再过滤——保证过滤后的行数不超过 lines）
    tail = all_lines[-lines:] if total > lines else all_lines
    truncated = total > lines

    # 过滤——Task 7.15: JSON 行按字段精确过滤 + 旧纯文本行走 fallback 子串匹配
    filtered = []
    for line in tail:
        parsed = _parse_log_line(line)
        if parsed:
            # JSON 格式——按 level/message 字段精确匹配
            if level and parsed.get("level", "").upper() != level:
                continue
            if q_lower and q_lower not in parsed.get("message", "").lower():
                continue
            filtered.append(_format_log_line(parsed))
        else:
            # 旧纯文本格式（轮转残留或非 JSON 日志）——子串匹配
            if level and f"[{level}]" not in line:
                continue
            if q_lower and q_lower not in line.lower():
                continue
            filtered.append(line)

    return {
        "lines": filtered,
        "total": total,
        "filtered": len(filtered),
        "truncated": truncated,
    }


@router.get("/logs/download")
async def download_logs(request: Request):
    """下载完整 yuncat.log 文件（只读，不修改）。"""
    log_path = Path(DATA_DIR) / LOG_FILENAME
    if not log_path.exists():
        return Response(
            content='{"error": "log file not yet created"}',
            status_code=404,
            media_type="application/json",
        )
    try:
        content = log_path.read_bytes()
    except OSError as e:
        return Response(
            content=f'{{"error": "read failed: {e}"}}',
            status_code=500,
            media_type="application/json",
        )
    filename = f"yuncat-{int(time.time())}.log"
    return Response(
        content=content,
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
