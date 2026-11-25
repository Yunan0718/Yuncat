"""tests/unit/test_proc_wait_timeout.py —— Task 3.4: proc.wait() 阻塞修复测试

对标:
- asyncio 最佳实践: proc.wait() 加超时，防止僵尸进程永久阻塞
- Claude Code: 子进程超时后强制 kill + 清理

覆盖:
- 正常退出: proc.wait() 立即返回 → 不触发 kill
- wait 超时: proc.wait() 阻塞 > 5s → 触发 proc.kill() + 再 wait
- 外层超时: timeout_sec 到期 → cleanup 路径 proc.kill() + wait_for(timeout=3)
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


def _make_async_stream(lines: list[bytes] | None = None):
    """构造 mock async stream——async for 返回 lines 后结束"""
    lines = lines or []

    class _Stream:
        def __init__(self):
            self._lines = list(lines)
            self._closed = False

        async def readline(self):
            if self._lines:
                return self._lines.pop(0)
            return b""

        def close(self):
            self._closed = True

    return _Stream()


def _make_mock_proc(wait_side_effect=None, returncode=0):
    """构造 mock subprocess.Process"""
    proc = MagicMock()
    proc.pid = 12345
    proc.returncode = returncode
    proc.stdout = _make_async_stream([b"mock output\n"])
    proc.stderr = _make_async_stream([])
    proc.stdin = MagicMock()

    # wait() 默认立即返回；可通过 wait_side_effect 定制（如阻塞）
    if wait_side_effect is None:
        proc.wait = AsyncMock(return_value=0)
    else:
        proc.wait = AsyncMock(side_effect=wait_side_effect)

    proc.kill = MagicMock()
    proc.terminate = MagicMock()
    return proc


class TestProcWaitTimeout:
    """Task 3.4: proc.wait() 超时保护"""

    @pytest.fixture
    def workspace(self, tmp_path, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv("YUNCAT_WORKSPACE", str(ws))
        from security.sandbox import get_workspace
        get_workspace()
        return ws

    async def test_normal_wait_returns_within_timeout(self, workspace):
        """proc.wait() 立即返回 → 不触发 kill，正常返回输出"""
        proc = _make_mock_proc(returncode=0)
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            from tools.builtin.system_tool_command import _execute_command
            result = await _execute_command("echo hello", timeout_sec=10, task_id=None)
        # 输出含 mock output
        assert "mock output" in result.output
        # wait 被调用（正常路径）
        assert proc.wait.called
        # kill 未被调用（正常退出）
        proc.kill.assert_not_called()

    async def test_wait_timeout_triggers_kill(self, workspace):
        """proc.wait() 阻塞 > 5s → 触发 proc.kill() + 再 wait"""
        # 构造 wait() 永远阻塞的 mock——第一次 wait 超时后，kill 后再 wait 应立即返回
        wait_call_count = [0]

        async def _blocking_wait():
            wait_call_count[0] += 1
            if wait_call_count[0] == 1:
                # 第一次 wait 永久阻塞——模拟僵尸进程
                await asyncio.sleep(60)
            return 0  # kill 后的第二次 wait 立即返回

        proc = _make_mock_proc(wait_side_effect=_blocking_wait)
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            from tools.builtin.system_tool_command import _execute_command
            # timeout_sec=10 但 proc.wait() 内部用 5s 超时
            # 总执行时间应 < 30s（5s wait 超时 + kill + 再 wait）
            result = await asyncio.wait_for(
                _execute_command("stuck_cmd", timeout_sec=10, task_id=None),
                timeout=30,
            )
        # 验证 _execute_command 返回了（没永久阻塞）
        assert isinstance(result.output, str)
        # kill 被调用（wait 超时后强制 kill）
        assert proc.kill.called

    async def test_stream_timeout_does_not_block_forever(self, workspace):
        """stream 读取超时 → _execute_command 不永久阻塞，返回部分输出"""
        # stream 读取永久阻塞 → 触发 timeout_sec 超时 → pass → proc.wait() → 返回
        async def _blocking_stream_read():
            await asyncio.sleep(60)

        proc = _make_mock_proc()
        # 让 stdout/stderr.readline 永久阻塞
        proc.stdout.readline = _blocking_stream_read
        proc.stderr.readline = _blocking_stream_read

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            from tools.builtin.system_tool_command import _execute_command
            # timeout_sec=2 → stream 读取 2s 超时 → 走清理 → proc.wait() → 返回
            # _execute_command 不抛 TimeoutError（stream 超时被 except pass），
            # 而是返回部分输出（这里 stream 阻塞所以 output_parts 为空 → "(no output)"）
            result = await asyncio.wait_for(
                _execute_command("blocking_cmd", timeout_sec=2, task_id=None),
                timeout=30,
            )
        # 验证 _execute_command 返回了（没永久阻塞）
        assert isinstance(result.output, str)
        # proc.wait 被调用（stream 超时后仍要 wait 进程退出）
        assert proc.wait.called
