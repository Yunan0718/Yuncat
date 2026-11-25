"""yuncat_utils/structured_logging.py —— Task 7.15: 结构化 JSON 日志

将纯文本日志格式转为 JSON，支持 trace_id/agent_id/task_id 字段。
对标 ELK / Datadog / Grafana Loki 结构化日志标准——机器可解析，支持高效索引。

设计:
- 自定义 logging.Formatter 子类——不引入 python-json-logger 依赖（零供应链风险）
- contextvars 注入 trace_id/agent_id/task_id（async-safe，自动隔离并发请求）
- exc_info 格式化为结构化 exception 字段（保留完整堆栈）
- ensure_ascii=False——中文消息直接输出，不转义为 \\uXXXX

格式示例:
  {"timestamp":"2026-07-12T10:30:00.123+08:00","level":"INFO","logger":"yuncat",
   "message":"Starting backend","trace_id":"abc123","agent_id":"hermes",
   "task_id":"task-456","module":"lifespan","function":"init_db","line":84}

兼容性:
- routes/logs.py 的 log viewer 端点解析 JSON 后格式化回人类可读文本
- 旧纯文本日志行（轮转文件残留）通过 fallback 子串匹配处理
- 前端 LogViewer 颜色编码 [ERROR]/[WARNING] 子串在格式化后保留
"""
from __future__ import annotations

import json
import logging
import traceback
from datetime import datetime, timezone

# 复用 context.py 的 contextvars——agent_id 与 ToolService.execute 共享同一 ContextVar
# 这样 ToolService.execute 设置 agent_id 后，日志自动包含
from context import (
    _current_agent_id as agent_id_var,
    _current_task_id as task_id_var,
    _current_trace_id as trace_id_var,
)

__all__ = [
    "JsonFormatter",
    "_ContextVarFilter",
    "setup_structured_logging",
    "trace_id_var",
    "agent_id_var",
    "task_id_var",
]


class _ContextVarFilter(logging.Filter):
    """注入 contextvars 到 LogRecord——formatter 读取这些字段。

    logging.Filter.filter() 在 format() 之前调用，适合注入额外字段。
    对标 Django logging filter / Flask request context filter。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = trace_id_var.get()
        record.agent_id = agent_id_var.get() or ""
        record.task_id = task_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """JSON 日志格式器——输出机器可解析的 JSON 行。

    每条日志为一行 JSON（JSON Lines / NDJSON 格式），适合 ELK/Datadog 索引。
    空字段（trace_id/agent_id/task_id 为空字符串时）不包含在输出中——减少噪音。
    直接从 contextvars 读取上下文——不依赖 filter，自包含可独立使用。
    """

    def format(self, record: logging.LogRecord) -> str:
        # 基础字段
        log_entry: dict = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc,
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # 上下文字段——直接从 contextvars 读取（async-safe，自包含）
        # 仅非空时包含——减少日志噪音
        trace_id = trace_id_var.get()
        if trace_id:
            log_entry["trace_id"] = trace_id
        agent_id = agent_id_var.get() or ""
        if agent_id:
            log_entry["agent_id"] = agent_id
        task_id = task_id_var.get()
        if task_id:
            log_entry["task_id"] = task_id

        # 异常信息——格式化为完整堆栈字符串
        if record.exc_info:
            log_entry["exception"] = "".join(
                traceback.format_exception(*record.exc_info),
            )

        # 调试定位字段
        log_entry["module"] = record.module
        log_entry["function"] = record.funcName
        log_entry["line"] = record.lineno

        # default=str: 非 JSON 序列化值转为字符串（防 TypeError）
        # ensure_ascii=False: 中文直接输出（对标主流 JSON 日志实践）
        return json.dumps(log_entry, default=str, ensure_ascii=False)


def setup_structured_logging(
    handler: logging.Handler,
    level: int = logging.INFO,
) -> None:
    """配置结构化 JSON 日志——替换 handler 的 formatter + 添加 context filter。

    保留 handler 的轮转/文件路径等配置，仅替换格式化逻辑。

    Args:
        handler: 已配置的 RotatingFileHandler（保留 maxBytes/backupCount 等配置）
        level: 日志级别（默认 INFO）
    """
    handler.setFormatter(JsonFormatter())
    handler.setLevel(level)
    handler.addFilter(_ContextVarFilter())
    logging.basicConfig(level=level, handlers=[handler])


def set_log_level(level: int | str, logger_name: str = "") -> None:
    """运行时调整日志级别——供 `/admin/log-level` 端点调用。

    Args:
        level: 日志级别，支持字符串（DEBUG/INFO/WARNING/ERROR/CRITICAL）或 logging 常量。
        logger_name: 目标 logger 名，空字符串表示 root logger。

    Raises:
        ValueError: level 不是有效日志级别。
    """
    if isinstance(level, str):
        normalized = level.upper()
        if normalized not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError(f"Invalid log level: {level}")
        level_int = getattr(logging, normalized)
    elif isinstance(level, int):
        level_int = level
    else:
        raise ValueError(f"level must be int or str, got {type(level)}")

    target_logger = logging.getLogger(logger_name) if logger_name else logging.getLogger()
    target_logger.setLevel(level_int)
