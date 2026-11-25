"""E3: lint_service 单元测试。

覆盖：
- detect_language: 扩展名 → 语言映射
- parse_ruff_json: ruff JSON 输出解析（空/无效/正常/缺字段）
- lint_python_file_sync: 同步 ruff 调用（mock subprocess，含 ruff 缺失/超时/正常路径）
- lint_python_file: 异步 ruff 调用（mock asyncio.create_subprocess_exec）
"""
import asyncio
import json
import sys
import os
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

# 让 tests/unit 能 import src-python
_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src-python"))

from services.lint_service import (
    detect_language,
    parse_ruff_json,
    lint_python_file_sync,
    lint_python_file,
)


# ==== detect_language ====

class TestDetectLanguage:
    def test_python(self):
        assert detect_language("foo.py") == "python"
        assert detect_language("a/b/c.py") == "python"
        assert detect_language("C:\\x\\y.py") == "python"

    def test_typescript_family(self):
        assert detect_language("a.js") == "typescript"
        assert detect_language("a.jsx") == "typescript"
        assert detect_language("a.ts") == "typescript"
        assert detect_language("a.tsx") == "typescript"

    def test_unsupported_returns_empty(self):
        assert detect_language("a.txt") == ""
        assert detect_language("a.md") == ""
        assert detect_language("a.json") == ""
        assert detect_language("a") == ""
        assert detect_language("") == ""

    def test_case_insensitive(self):
        assert detect_language("FOO.PY") == "python"
        assert detect_language("Foo.TS") == "typescript"


# ==== parse_ruff_json ====

class TestParseRuffJson:
    def test_empty(self):
        assert parse_ruff_json("") == []
        assert parse_ruff_json("   ") == []
        assert parse_ruff_json("\n") == []

    def test_invalid_json(self):
        assert parse_ruff_json("not json") == []
        assert parse_ruff_json("{broken") == []

    def test_single_error(self):
        ruff_out = [{
            "code": "F841",
            "message": "Local variable `x` is assigned to but never used",
            "location": {"row": 5, "column": 4},
            "fix": None,
        }]
        result = parse_ruff_json(json.dumps(ruff_out))
        assert len(result) == 1
        d = result[0]
        assert d["line"] == 5
        assert d["col"] == 4
        assert d["message"] == "Local variable `x` is assigned to but never used"
        assert d["severity"] == "error"  # fix is None → error
        assert d["code"] == "F841"

    def test_warning_with_fix(self):
        ruff_out = [{
            "code": "E501",
            "message": "Line too long",
            "location": {"row": 10, "column": 1},
            "fix": {"some": "fix"},  # 有 fix → warning
        }]
        result = parse_ruff_json(json.dumps(ruff_out))
        assert result[0]["severity"] == "warning"

    def test_multiple(self):
        ruff_out = [
            {"code": "F841", "message": "unused", "location": {"row": 1, "column": 1}, "fix": None},
            {"code": "E501", "message": "too long", "location": {"row": 2, "column": 1}, "fix": {}},
        ]
        result = parse_ruff_json(json.dumps(ruff_out))
        assert len(result) == 2
        assert result[0]["line"] == 1
        assert result[1]["line"] == 2

    def test_missing_fields_default(self):
        ruff_out = [{}]  # 完全空对象
        result = parse_ruff_json(json.dumps(ruff_out))
        assert len(result) == 1
        assert result[0]["line"] == 1  # 默认
        assert result[0]["col"] == 1
        assert result[0]["message"] == ""
        assert result[0]["severity"] == "error"  # fix is None → error
        assert result[0]["code"] == ""

    def test_missing_location(self):
        ruff_out = [{"code": "E501", "message": "x", "fix": None}]
        result = parse_ruff_json(json.dumps(ruff_out))
        assert result[0]["line"] == 1
        assert result[0]["col"] == 1


# ==== lint_python_file_sync ====

class TestLintPythonFileSync:
    def test_ruff_not_found(self):
        """ruff 不在 PATH → 返回空列表，不抛异常。"""
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            result = lint_python_file_sync("/tmp/foo.py")
        assert result == []

    def test_ruff_timeout(self):
        """ruff 超时 → 返回空列表。"""
        import subprocess as _sp
        with patch("subprocess.run", side_effect=_sp.TimeoutExpired(cmd="ruff", timeout=1)):
            result = lint_python_file_sync("/tmp/foo.py")
        assert result == []

    def test_ruff_success_with_errors(self):
        """ruff 返回 exit 1 + JSON 诊断 → 解析为列表。"""
        ruff_json = json.dumps([{
            "code": "F841", "message": "unused var",
            "location": {"row": 3, "column": 4}, "fix": None,
        }])
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ruff_json
        with patch("subprocess.run", return_value=mock_proc):
            result = lint_python_file_sync("/tmp/foo.py")
        assert len(result) == 1
        assert result[0]["line"] == 3
        assert result[0]["code"] == "F841"

    def test_ruff_success_no_errors(self):
        """ruff 返回 exit 0 + 空 JSON → 空列表。"""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "[]"
        with patch("subprocess.run", return_value=mock_proc):
            result = lint_python_file_sync("/tmp/foo.py")
        assert result == []

    def test_ruff_empty_stdout(self):
        """ruff 返回空 stdout → 空列表。"""
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        with patch("subprocess.run", return_value=mock_proc):
            result = lint_python_file_sync("/tmp/foo.py")
        assert result == []

    def test_ruff_crash_exit_code(self):
        """ruff 崩溃（exit 2）→ 返回空列表。"""
        mock_proc = MagicMock()
        mock_proc.returncode = 2
        mock_proc.stdout = "some error"
        with patch("subprocess.run", return_value=mock_proc):
            result = lint_python_file_sync("/tmp/foo.py")
        assert result == []

    def test_cwd_passed_through(self):
        """cwd 参数传递给 subprocess。"""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "[]"
        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            lint_python_file_sync("/tmp/foo.py", cwd="/custom/ws")
        assert mock_run.call_args.kwargs.get("cwd") == "/custom/ws"


# ==== lint_python_file (async) ====

class TestLintPythonFileAsync:
    def test_ruff_success(self):
        """async 版本正常路径。"""
        ruff_json = json.dumps([{
            "code": "E501", "message": "too long",
            "location": {"row": 7, "column": 1}, "fix": {},
        }])
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(ruff_json.encode(), b""))
        mock_proc.returncode = 1
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = asyncio.run(lint_python_file("/tmp/foo.py"))
        assert len(result) == 1
        assert result[0]["line"] == 7
        assert result[0]["severity"] == "warning"

    def test_ruff_not_found(self):
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError()):
            result = asyncio.run(lint_python_file("/tmp/foo.py"))
        assert result == []

    def test_ruff_timeout(self):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = asyncio.run(lint_python_file("/tmp/foo.py"))
        assert result == []

    def test_ruff_empty_stdout(self):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = asyncio.run(lint_python_file("/tmp/foo.py"))
        assert result == []

    def test_cwd_passed_through(self):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"[]", b""))
        mock_proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            asyncio.run(lint_python_file("/tmp/foo.py", cwd="/ws"))
        assert mock_exec.call_args.kwargs.get("cwd") == "/ws"


# ==== 字段契约——确保与前端 Diagnostic 类型对齐 ====

class TestDiagnosticContract:
    """确保诊断字典包含前端 Diagnostic 类型所需的全部字段。"""

    def test_fields_present(self):
        ruff_out = [{
            "code": "F841", "message": "unused",
            "location": {"row": 1, "column": 1}, "fix": None,
        }]
        result = parse_ruff_json(json.dumps(ruff_out))
        d = result[0]
        # 前端 Diagnostic 接口: { line, col, message, severity, code? }
        assert "line" in d
        assert "col" in d
        assert "message" in d
        assert "severity" in d
        assert "code" in d
        assert isinstance(d["line"], int)
        assert isinstance(d["col"], int)
        assert isinstance(d["message"], str)
        assert isinstance(d["severity"], str)
        assert d["severity"] in ("error", "warning")
