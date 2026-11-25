"""tests/unit/test_tool_spill.py — 截断工具结果的持久化 side-store（借鉴 dsh-spill）

覆盖：
- save/load 往返；
- task/tool 名清洗，不能路径穿越；
- 单产物大小上限；
- LRU 清空/重启后 request_more_context 仍能从落盘 spill 取回；
- 过期清理。
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src-python"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import config  # noqa: E402
from services import tool_spill  # noqa: E402


@pytest.fixture
def spill_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(tool_spill, "SPILL_ENABLED", True)
    monkeypatch.setattr(tool_spill, "SPILL_MAX_BYTES", 1024)
    return tmp_path


def test_save_and_load_roundtrip(spill_dir):
    content = "line 1\nline 2\n" * 10
    path = tool_spill.save_spill("task-1", "read_file", 0, content)
    assert path is not None
    assert tool_spill.load_spill("task-1", "read_file", 0) == content


def test_path_components_are_sanitized(spill_dir):
    path = tool_spill.save_spill("../../evil", "../tool", 0, "payload")
    assert path is not None
    resolved = Path(path).resolve()
    root = tool_spill.spill_root().resolve()
    assert root in resolved.parents
    assert ".." not in resolved.parts


def test_size_limit_skips_oversized(spill_dir):
    tool_spill.SPILL_MAX_BYTES = 4
    assert tool_spill.save_spill("t", "x", 0, "0123456789") is None


def test_find_spill_returns_newest(spill_dir):
    tool_spill.save_spill("t1", "run_command", 0, "old")
    import time
    time.sleep(0.01)
    tool_spill.save_spill("t2", "run_command", 0, "new")
    assert tool_spill.find_spill("run_command", 0) == "new"


def test_request_more_context_falls_back_to_spill(spill_dir):
    """清空内存 LRU（等价于重启/淘汰）后仍可取回落盘内容。"""
    from tools.builtin.system_tool_context import (
        _TOOL_RESULT_CACHE,
        _cache_tool_result,
        request_more_context,
    )

    _cache_tool_result("t-spill", "read_file", 0, "FULL-CONTENT")
    _TOOL_RESULT_CACHE.clear()
    result = asyncio.run(request_more_context("read_file", 0))
    assert result["success"] is True
    assert result["data"]["content"] == "FULL-CONTENT"
    assert result["meta"]["source"] == "spill"


def test_cleanup_removes_expired(spill_dir):
    path = tool_spill.save_spill("t", "read_file", 0, "x")
    assert path
    old = 1_600_000_000
    os.utime(path, (old, old))
    removed = tool_spill.cleanup_spill(max_age_days=1)
    assert removed == 1
    assert not Path(path).exists()
