"""tests/unit/test_structured_logging.py —— Task 7.15: 结构化 JSON 日志

TDD 测试——覆盖:
- JsonFormatter 输出合法 JSON + 必需字段（timestamp/level/logger/message）
- contextvars 注入 trace_id/agent_id/task_id（非空时才包含）
- exc_info 格式化为 exception 字段
- setup_structured_logging 配置 handler
- routes/logs.py 兼容 JSON + 纯文本 + 混合格式
"""
import json
import logging
import os
import sys

import pytest

# 确保能 import src-python 模块
_SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src-python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


# ── JsonFormatter ──

class TestJsonFormatter:
    """JsonFormatter 核心行为"""

    def _make_record(self, msg="hello", level=logging.INFO, exc_info=None):
        logger = logging.getLogger("test.structured")
        return logger.makeRecord(
            name="test.structured", level=level, fn="test.py", lno=42,
            msg=msg, args=(), exc_info=exc_info,
        )

    def test_produces_valid_json(self):
        from yuncat_utils.structured_logging import JsonFormatter
        formatter = JsonFormatter()
        record = self._make_record()
        output = formatter.format(record)
        parsed = json.loads(output)  # 不抛异常 = 合法 JSON
        assert isinstance(parsed, dict)

    def test_includes_required_fields(self):
        from yuncat_utils.structured_logging import JsonFormatter
        formatter = JsonFormatter()
        record = self._make_record(msg="backend starting")
        parsed = json.loads(formatter.format(record))
        assert "timestamp" in parsed
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "test.structured"
        assert parsed["message"] == "backend starting"

    def test_includes_module_function_line(self):
        from yuncat_utils.structured_logging import JsonFormatter
        formatter = JsonFormatter()
        record = self._make_record()
        parsed = json.loads(formatter.format(record))
        assert parsed["line"] == 42
        assert "module" in parsed
        assert "function" in parsed

    def test_trace_id_included_when_set(self):
        from yuncat_utils.structured_logging import JsonFormatter, trace_id_var
        formatter = JsonFormatter()
        record = self._make_record()
        token = trace_id_var.set("abc-123")
        try:
            parsed = json.loads(formatter.format(record))
            assert parsed["trace_id"] == "abc-123"
        finally:
            trace_id_var.reset(token)

    def test_trace_id_omitted_when_empty(self):
        from yuncat_utils.structured_logging import JsonFormatter, trace_id_var
        formatter = JsonFormatter()
        record = self._make_record()
        token = trace_id_var.set("")
        try:
            parsed = json.loads(formatter.format(record))
            assert "trace_id" not in parsed
        finally:
            trace_id_var.reset(token)

    def test_agent_id_included_when_set(self):
        from yuncat_utils.structured_logging import JsonFormatter, agent_id_var
        formatter = JsonFormatter()
        record = self._make_record()
        token = agent_id_var.set("hermes")
        try:
            parsed = json.loads(formatter.format(record))
            assert parsed["agent_id"] == "hermes"
        finally:
            agent_id_var.reset(token)

    def test_task_id_included_when_set(self):
        from yuncat_utils.structured_logging import JsonFormatter, task_id_var
        formatter = JsonFormatter()
        record = self._make_record()
        token = task_id_var.set("task-789")
        try:
            parsed = json.loads(formatter.format(record))
            assert parsed["task_id"] == "task-789"
        finally:
            task_id_var.reset(token)

    def test_exception_info_formatted(self):
        from yuncat_utils.structured_logging import JsonFormatter
        formatter = JsonFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            import sys as _sys
            exc_info = _sys.exc_info()
        record = self._make_record(exc_info=exc_info)
        parsed = json.loads(formatter.format(record))
        assert "exception" in parsed
        assert "ValueError" in parsed["exception"]
        assert "test error" in parsed["exception"]

    def test_non_ascii_message_not_escaped(self):
        """中文消息不应被 \\uXXXX 转义——ensure_ascii=False"""
        from yuncat_utils.structured_logging import JsonFormatter
        formatter = JsonFormatter()
        record = self._make_record(msg="启动后端")
        output = formatter.format(record)
        assert "启动后端" in output  # 直接包含中文字符


# ── _ContextVarFilter ──

class TestContextVarFilter:
    """Filter 注入 contextvars 到 LogRecord"""

    def test_filter_adds_trace_id_to_record(self):
        from yuncat_utils.structured_logging import _ContextVarFilter, trace_id_var
        f = _ContextVarFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="t.py", lineno=1,
            msg="x", args=(), exc_info=None,
        )
        token = trace_id_var.set("trace-xyz")
        try:
            assert f.filter(record) is True
            assert record.trace_id == "trace-xyz"
        finally:
            trace_id_var.reset(token)

    def test_filter_adds_empty_when_unset(self):
        from yuncat_utils.structured_logging import _ContextVarFilter
        f = _ContextVarFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="t.py", lineno=1,
            msg="x", args=(), exc_info=None,
        )
        f.filter(record)
        assert record.trace_id == ""
        assert record.agent_id == ""
        assert record.task_id == ""


# ── setup_structured_logging ──

class TestSetupStructuredLogging:
    """配置函数"""

    def test_setup_replaces_formatter(self):
        from yuncat_utils.structured_logging import setup_structured_logging, JsonFormatter
        handler = logging.StreamHandler()
        setup_structured_logging(handler, level=logging.DEBUG)
        assert isinstance(handler.formatter, JsonFormatter)
        assert handler.level == logging.DEBUG

    def test_setup_adds_context_filter(self):
        from yuncat_utils.structured_logging import setup_structured_logging, _ContextVarFilter
        handler = logging.StreamHandler()
        setup_structured_logging(handler)
        assert any(isinstance(f, _ContextVarFilter) for f in handler.filters)


# ── routes/logs.py 兼容性 ──

class TestLogsTailJsonCompat:
    """routes/logs.py 兼容 JSON + 纯文本 + 混合格式"""

    def test_parse_log_line_valid_json(self):
        from routes.logs import _parse_log_line
        line = '{"timestamp":"2026-07-12T10:30:00","level":"INFO","logger":"yuncat","message":"hello"}'
        parsed = _parse_log_line(line)
        assert parsed is not None
        assert parsed["level"] == "INFO"
        assert parsed["message"] == "hello"

    def test_parse_log_line_invalid_json(self):
        from routes.logs import _parse_log_line
        parsed = _parse_log_line("not json at all")
        assert parsed is None

    def test_parse_log_line_empty(self):
        from routes.logs import _parse_log_line
        assert _parse_log_line("") is None
        assert _parse_log_line("   ") is None

    def test_format_log_line_json(self):
        from routes.logs import _format_log_line
        parsed = {
            "timestamp": "2026-07-12T10:30:00.123+08:00",
            "level": "INFO",
            "logger": "yuncat.lifespan",
            "message": "Starting backend",
        }
        display = _format_log_line(parsed)
        assert "[INFO]" in display
        assert "yuncat.lifespan" in display
        assert "Starting backend" in display

    def test_format_log_line_with_context(self):
        from routes.logs import _format_log_line
        parsed = {
            "timestamp": "2026-07-12T10:30:00",
            "level": "ERROR",
            "logger": "yuncat",
            "message": "fail",
            "trace_id": "abc",
            "agent_id": "hermes",
            "task_id": "t1",
        }
        display = _format_log_line(parsed)
        assert "trace=abc" in display
        assert "agent=hermes" in display
        assert "task=t1" in display

    def test_format_log_line_preserves_error_level_for_color(self):
        """前端颜色编码依赖 [ERROR] / [WARNING] 子串——格式化后必须保留"""
        from routes.logs import _format_log_line
        for level in ("ERROR", "WARNING", "INFO", "DEBUG"):
            parsed = {"timestamp": "2026-07-12T10:30:00", "level": level, "logger": "x", "message": "m"}
            display = _format_log_line(parsed)
            assert f"[{level}]" in display, f"color marker [<{level}>] missing in: {display}"


# ── 集成: log viewer 端点处理混合格式 ──

class TestLogViewerMixedFormat:
    """log viewer 端点能处理 JSON + 纯文本混合日志"""

    @pytest.mark.asyncio
    async def test_tail_handles_mixed_json_and_plain(self, tmp_path, monkeypatch):
        """混合日志文件: JSON 行 + 纯文本行 都能被正确过滤"""
        from routes import logs as logs_mod
        from config import DATA_DIR

        log_file = tmp_path / "yuncat.log"
        log_file.write_text(
            '{"timestamp":"2026-07-12T10:30:00","level":"ERROR","logger":"yuncat","message":"json error"}\n'
            "2026-07-12 10:31:00 [INFO] yuncat: plain text info\n"
            '{"timestamp":"2026-07-12T10:32:00","level":"INFO","logger":"yuncat","message":"json info"}\n'
            "2026-07-12 10:33:00 [ERROR] yuncat: plain text error\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(logs_mod, "DATA_DIR", str(tmp_path))

        # 模拟 request.app.state — routes/logs.py 不使用 request
        result = await logs_mod.tail_logs(
            request=None, lines=100, level="ERROR", q="",
        )
        assert "error" not in result or result.get("error") is None
        filtered_lines = result["lines"]
        # 2 个 ERROR 行（1 JSON + 1 纯文本）
        assert len(filtered_lines) == 2
        for line in filtered_lines:
            assert "[ERROR]" in line  # 格式化后都包含 [ERROR]

    @pytest.mark.asyncio
    async def test_tail_keyword_search_in_json_message(self, tmp_path, monkeypatch):
        """关键字搜索在 JSON 消息字段中匹配"""
        from routes import logs as logs_mod

        log_file = tmp_path / "yuncat.log"
        log_file.write_text(
            '{"timestamp":"2026-07-12T10:30:00","level":"INFO","logger":"yuncat","message":"Starting Yuncat backend"}\n'
            '{"timestamp":"2026-07-12T10:31:00","level":"INFO","logger":"yuncat","message":"Loading config"}\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(logs_mod, "DATA_DIR", str(tmp_path))

        result = await logs_mod.tail_logs(
            request=None, lines=100, level="", q="yuncat",
        )
        assert result["filtered"] == 1
        assert "Starting Yuncat backend" in result["lines"][0]
