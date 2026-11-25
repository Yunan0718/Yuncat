"""tests/unit/test_p0_functional_fixes.py — P0 功能 bug 修复回归测试

覆盖 2026-07-04 功能审计发现的 5 个 P0 bug：
- P0-1: WS resume 消息无 handler 导致重连后流式更新丢失
- P0-2: ws.py spec_mode status 三元表达式两分支都返回 completed
- P0-3: tool_service.py auto_approve dispatch_confirm 异常返回 preview result 误导 agent
- P0-4: agent_tool_loop.py 工具替代重试成功后仍用旧 result_success 计数失败
- P0-5: ws.py 主循环无 Exception 兜底导致 handler 异常时连接断裂不清理
"""
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_WS_PY = Path(__file__).resolve().parents[2] / "src-python" / "routes" / "ws.py"


def _ws_source() -> str:
    return _WS_PY.read_text(encoding="utf-8")


# ═══════════════════════════════════════════════════════════
# P0-1: WS resume 消息有对应 handler
# ═══════════════════════════════════════════════════════════

class TestP01WSResumeHandler:
    """P0-1: WS 重连后前端发 resume 消息，服务端必须处理而非静默丢弃。"""

    def test_ws_main_loop_handles_resume_message(self):
        """ws.py 主循环必须包含 resume 消息分支"""
        source = _ws_source()
        assert '"resume"' in source, (
            "ws.py 主循环必须处理 resume 消息——否则 WS 重连后 running task "
            "流式更新全部丢失（P0-1 bug）"
        )

    def test_resume_handler_calls_subscribe(self):
        """resume 消息应复用 _handle_subscribe（register_ws + sync_state）"""
        source = _ws_source()
        # resume 分支应调用 _handle_subscribe
        assert '_handle_subscribe(app, ws, msg)' in source, (
            "resume 应复用 _handle_subscribe"
        )


# ═══════════════════════════════════════════════════════════
# P0-2: spec_mode status 未收敛时返回 failed
# ═══════════════════════════════════════════════════════════

class TestP02SpecModeStatus:
    """P0-2: Spec 模式未收敛时 status 应为 failed，不是 completed。"""

    def test_spec_status_not_double_completed(self):
        """ws.py 中 status 三元表达式不能两分支都返回 completed"""
        source = _ws_source()
        # 原 bug: status = "completed" if result.get("converged") else "completed"
        assert 'else "completed"' not in source, (
            "spec_mode status 三元表达式不能两分支都返回 completed（P0-2 bug）"
        )

    def test_spec_status_failed_when_not_converged(self):
        """未收敛时应返回 failed"""
        source = _ws_source()
        assert 'else "failed"' in source, (
            "spec_mode 未收敛时 status 应为 failed（P0-2 修复）"
        )


# ═══════════════════════════════════════════════════════════
# P0-3: auto_approve dispatch_confirm 失败时返回 success=False
# ═══════════════════════════════════════════════════════════

class TestP03AutoApproveFailure:
    """P0-3: auto_approve 模式下 dispatch_confirm 异常时不能返回 preview result（success=True）。"""

    def test_auto_approve_failure_returns_not_success(self):
        """tool_service.py auto_approve 异常分支必须返回 success=False"""
        from services.tools.tool_service import ToolService

        source = inspect.getsource(ToolService.execute_confirmable)
        assert "return result  # fallback to preview" not in source, (
            "auto_approve 异常分支不能 return result（preview success=True 误导 agent）"
        )
        assert '"success": False' in source, (
            "auto_approve 异常分支必须返回 success=False（P0-3 修复）"
        )

    @pytest.mark.asyncio
    async def test_auto_approve_dispatch_failure_marks_unsuccess(self):
        """dispatch_confirm 抛异常时，execute_confirmable 返回 success=False"""
        from services.tools.tool_service import ToolService

        ts = ToolService.__new__(ToolService)
        ts.execute = AsyncMock(return_value={
            "success": True,
            "meta": {"phase": "preview"},
            "data": {"path": "/tmp/test.txt"},
        })

        with patch("tools.builtin.system_tools.create_pending_state", new=AsyncMock(return_value="tok")), \
             patch("tools.builtin.system_tools.dispatch_confirm", new=AsyncMock(side_effect=RuntimeError("db locked"))), \
             patch("security.sandbox.get_workspace", return_value=Path("/tmp")), \
             patch("services.tools.tool_service._resolve_pending_path", return_value=None):
            result = await ts.execute_confirmable(
                tool_name="write_file",
                arguments={"path": "/tmp/test.txt", "content": "x"},
                agent_id="hermes",
                db=MagicMock(),
                auto_approve=True,
            )

        assert result["success"] is False, (
            "auto_approve dispatch_confirm 失败时必须返回 success=False（P0-3）"
        )
        assert "auto_approve execution failed" in result.get("error", ""), (
            "error 消息应说明是 auto_approve 执行失败"
        )
        assert result.get("auto_approved") is True


# ═══════════════════════════════════════════════════════════
# P0-4: 替代工具重试后更新 result_success
# ═══════════════════════════════════════════════════════════

class TestP04AlternativeToolResultSuccess:
    """P0-4: 替代工具重试成功后必须更新 result_success，否则 failure_streak 错误累积。"""

    def test_alternative_retry_updates_result_success(self):
        """post_process.py 替代工具重试后必须更新 result_success"""
        from services.agent import post_process

        source = inspect.getsource(post_process)
        assert "result_success = bool(alt_result.get" in source, (
            "替代工具重试后必须更新 result_success（P0-4 修复）——"
            "否则重试成功的工具仍被计数为失败，过早触发工具降级"
        )


# ═══════════════════════════════════════════════════════════
# P0-5: WS 主循环有 generic Exception 兜底
# ═══════════════════════════════════════════════════════════

class TestP05WSHandlerExceptionCatch:
    """P0-5: WS 主循环必须有 generic Exception 兜底，避免 handler 异常导致连接断裂不清理。"""

    def test_ws_main_loop_has_exception_catch(self):
        """ws.py 主循环除 WebSocketDisconnect 外必须有 Exception 兜底"""
        source = _ws_source()
        assert "except Exception as e:" in source, (
            "WS 主循环必须有 except Exception 兜底（P0-5 修复）——"
            "否则 handler 异常会导致连接断裂不清理 + task 流式更新丢失"
        )

    def test_ws_exception_catch_does_not_break_connection(self):
        """Exception 兜底不应 break 循环（除非 send_json 也失败）"""
        source = _ws_source()
        assert "_log.warning" in source, "Exception 兜底应记录日志"
        assert '"type": "error"' in source, "Exception 兜底应向前端推送 error 消息"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
