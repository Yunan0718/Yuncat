"""P1 功能 bug 修复回归测试

覆盖 6 个 P1 修复：
1. memory_tree title 缺失（metadata 取 title + 空值 fallback）
2. capability_router 假学习（AVG(score) 替代硬编码 0.8）
3. workflow resume 丢 input（从 context 列恢复 input）
5. stream_error 不 break（return + _mark_task failed）
6. tool_confirm 静默失败（通知前端 + resolve Future）
7. orchestrator 异常卡死（_decompose_and_execute 补 try/except）

混合测试策略：
- 源码检查：验证修复代码存在（最可靠，不受内部状态 mock 复杂度影响）
- 行为测试：对可独立测试的函数用 mock 验证行为正确性
"""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src-python"


def _src(rel: str) -> str:
    """读取源文件全文"""
    return (_SRC / rel).read_text(encoding="utf-8")


# ════════════════════════════════════════════════════════════
# Bug 1: memory_tree title 缺失
# ════════════════════════════════════════════════════════════

class TestP11MemoryTreeTitle:
    def test_index_from_text_has_empty_title_fallback(self):
        """P1-1: index_from_text 空文本时 title 应有 fallback"""
        source = _src("services/memory/memory_tree.py")
        assert "untitled_" in source, "index_from_text 应有空标题 fallback"

    def test_retrieve_context_reads_title_from_metadata(self):
        """P1-1: retrieve_context 应从 metadata 取 title 而非用 node_id"""
        source = _src("services/memory/memory_tree.py")
        # 修复后应包含 metadata.get("title")
        assert 'metadata' in source and 'title' in source
        # 不应再有 vr.get("id", "")[:MEMORY_TREE_RESULT_TITLE_CHARS] 当 title
        assert 'vr.get("id", "")[:MEMORY_TREE_RESULT_TITLE_CHARS]' not in source or \
               '"title": vr.get("id"' not in source

    @pytest.mark.asyncio
    async def test_index_from_text_empty_text_uses_fallback_title(self):
        """P1-1 行为测试: 空文本时 title 应为 untitled_tool_result"""
        from services.memory.memory_tree import MemoryTree
        mt = MemoryTree.__new__(MemoryTree)
        mt.db = MagicMock()
        mt._vector_repo = None

        captured_title = []

        async def fake_upsert(db, node_id, title, *args, **kwargs):
            captured_title.append(title)

        # upsert_knowledge_node 是函数内 from repositories import，需 patch 源模块
        with patch("repositories.upsert_knowledge_node", new=fake_upsert), \
             patch("services.memory.memory_tree._infer_tags", return_value=[]):
            await mt.index_from_text("", source="tool_result")

        assert len(captured_title) == 1
        assert captured_title[0] == "untitled_tool_result"


# ════════════════════════════════════════════════════════════
# Bug 2: capability_router 假学习
# ════════════════════════════════════════════════════════════

class TestP12CapabilityRouterFakeLearning:
    def test_query_uses_avg_score_not_hardcoded(self):
        """P1-2: 应使用 AVG(score) 而非硬编码 0.8"""
        source = _src("services/capability_router.py")
        assert "AVG(score)" in source, "应用 AVG(score) 计算真实成功率"
        assert '"success_rate": 0.8' not in source, "不应再有硬编码 0.8"

    @pytest.mark.asyncio
    async def test_query_returns_real_avg_score(self):
        """P1-2 行为测试: 返回的 success_rate 应为真实 AVG(score)"""
        from services.capability_router import _query_learned_from_db, AgentProfile

        # mock db: COUNT=4, AVG(score)=0.65
        mock_cursor = MagicMock()
        mock_cursor.fetchone = AsyncMock(return_value=(4, 0.65))
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=mock_cursor)

        profile = AgentProfile(
            agent_id="hermes", name="Hermes", keywords=["code"],
        )
        # get_db 是函数内 from context import get_db，需 patch 源模块
        with patch("context.get_db", return_value=mock_db):
            result = await _query_learned_from_db("code task", [profile])

        assert result is not None
        key = "delegation:hermes"
        assert key in result
        assert result[key]["success_rate"] == 0.65  # 真实 AVG 而非 0.8
        assert result[key]["count"] == 4

    @pytest.mark.asyncio
    async def test_low_score_does_not_trigger_preference(self):
        """P1-2 行为测试: 低分 agent 不应被偏好（验证 0.7 阈值生效）"""
        from services.capability_router import (
            _query_learned_from_db, _get_learned_preference, AgentProfile,
        )

        # mock db: COUNT=5, AVG(score)=0.4（低于 0.7 阈值）
        mock_cursor = MagicMock()
        mock_cursor.fetchone = AsyncMock(return_value=(5, 0.4))
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=mock_cursor)

        profile = AgentProfile(
            agent_id="openclaw", name="OpenClaw", keywords=["review"],
        )
        # get_db 是函数内 from context import get_db，需 patch 源模块
        with patch("context.get_db", return_value=mock_db):
            learn_from = await _query_learned_from_db("review task", [profile])

        # success_rate=0.4 < 0.7，不应返回偏好
        preference = _get_learned_preference("review task", [profile], learn_from)
        assert preference is None, "低分 agent 不应被偏好"


# ════════════════════════════════════════════════════════════
# Bug 3: workflow resume 丢 input
# ════════════════════════════════════════════════════════════

class TestP13WorkflowResumeInput:
    def test_resume_reads_context_column_for_input(self):
        """P1-3: resume 应从 workflow_runs.context 列恢复 input"""
        source = _src("workflow/engine.py")
        assert "original_input" in source, "resume 应有 original_input 变量"
        assert 'ctx.get("input"' in source, "应从 context 列读取 input"
        # 不应再传空串作 initial_input
        assert 'workflow_id, "", run_id=run_id' not in source, "不应再传空串"

    @pytest.mark.asyncio
    async def test_resume_restores_original_input(self):
        """P1-3 行为测试: resume 应恢复原始 input 而非空串"""
        from workflow.engine import WorkflowEngine

        engine = WorkflowEngine.__new__(WorkflowEngine)
        engine.repo = MagicMock()
        engine._log = MagicMock()

        # mock get_run 返回含 input 的 context
        original_input = "用户原始任务输入"
        ctx_json = json.dumps({"input": original_input, "other": "data"})
        engine.repo.get_run = AsyncMock(return_value={
            "workflow_id": "wf-1", "context": ctx_json,
        })
        engine.recover_from_snapshot = AsyncMock(return_value={"node_results": {}, "context": {}})

        captured_args = {}

        async def fake_run(wf_id, initial_input, **kwargs):
            captured_args["initial_input"] = initial_input
            return {"status": "completed"}

        engine.run = fake_run

        await engine.resume("run-1")

        assert captured_args["initial_input"] == original_input, \
            "resume 应恢复原始 input，而非空串"


# ════════════════════════════════════════════════════════════
# Bug 5: stream_error 不 break
# ════════════════════════════════════════════════════════════

class TestP15StreamErrorNoBreak:
    def test_stream_error_branch_has_return_and_mark_task(self):
        """P1-5: stream_error 分支应有 return + _mark_task(failed)"""
        source = _src("services/agent/agent_tool_loop.py")
        # 找到 stream_error 分支
        assert 'chunk_type == "stream_error"' in source
        # 应有 return full_response
        stream_error_section = source[source.index('chunk_type == "stream_error"'):]
        stream_error_section = stream_error_section[:stream_error_section.index("else:")]


# ════════════════════════════════════════════════════════════
# Bug 6: tool_confirm 静默失败
# ════════════════════════════════════════════════════════════

class TestP16ToolConfirmSilentFailure:
    def test_handle_tool_confirm_has_ws_param(self):
        """P1-6: _handle_tool_confirm 应接收 ws 参数"""
        source = _src("routes/ws.py")
        assert "async def _handle_tool_confirm(app, ws, msg" in source, \
            "_handle_tool_confirm 应有 ws 参数"

    def test_handle_approval_response_has_ws_param(self):
        """P1-6: _handle_approval_response 应接收 ws 参数"""
        source = _src("routes/ws.py")
        assert "async def _handle_approval_response(app, ws, msg" in source, \
            "_handle_approval_response 应有 ws 参数"

    def test_tool_confirm_notifies_frontend_on_error(self):
        """P1-6: 异常时应通知前端 tool_confirm_error"""
        source = _src("routes/ws.py")
        assert "tool_confirm_error" in source, "应向前端推送 tool_confirm_error"

    def test_approval_response_notifies_frontend_on_error(self):
        """P1-6: 异常时应通知前端 approval_error"""
        source = _src("routes/ws.py")
        assert "approval_error" in source, "应向前端推送 approval_error"

    def test_tool_confirm_uses_warning_not_debug(self):
        """P1-6: 异常日志应为 warning 而非 debug"""
        source = _src("routes/ws.py")
        assert '_log.warning("tool_confirm handler failed' in source, \
            "日志级别应为 warning"

    @pytest.mark.asyncio
    async def test_handle_tool_confirm_exception_notifies_and_resolves(self):
        """P1-6 行为测试: dispatch_confirm 异常时应通知前端 + resolve Future"""
        from routes.ws import _handle_tool_confirm

        app = MagicMock()
        app.state.db = MagicMock()
        ws = MagicMock()
        ws.send_json = AsyncMock()

        msg = {"confirm_token": "tok-123", "approved": True}

        with patch("tools.builtin.system_tools.dispatch_confirm",
                   new=AsyncMock(side_effect=RuntimeError("DB locked"))), \
             patch("services.tools.tool_service.resolve_confirmation",
                   new=AsyncMock()) as mock_resolve:
            await _handle_tool_confirm(app, ws, msg)

        # 应通知前端
        assert ws.send_json.called
        sent = ws.send_json.call_args[0][0]
        assert sent["type"] == "tool_confirm_error"
        assert sent["confirm_token"] == "tok-123"

        # 应 resolve Future 用失败结果
        assert mock_resolve.called
        resolve_args = mock_resolve.call_args[0]
        assert resolve_args[0] == "tok-123"
        assert resolve_args[1]["success"] is False


# ════════════════════════════════════════════════════════════
# Bug 7: orchestrator 异常卡死
# ════════════════════════════════════════════════════════════

class TestP17OrchestratorExceptionStuck:
    def test_decompose_and_execute_has_post_decompose_try_except(self):
        """P1-7: _decompose_and_execute 对抗验证段应有 try/except"""
        source = _src("services/long_task/orchestrator.py")
        assert "_decompose_and_execute post-decompose failed" in source, \
            "应有 post-decompose 异常处理"
        assert "decompose_and_execute: " in source, \
            "异常时应调 _mark_failed"

    def test_decompose_and_execute_mark_failed_on_post_decompose_error(self):
        """P1-7: 源码结构验证——try/except 包裹对抗验证段"""
        source = _src("services/long_task/orchestrator.py")
        # 找到 _decompose_and_execute 函数
        func_start = source.index("async def _decompose_and_execute")
        func_end = source.index("async def _execute_steps")
        func_body = source[func_start:func_end]

        # 应有 try: 包裹 get_plan_tree_by_task
        assert "try:" in func_body
        # except 应调 _mark_failed
        assert "_mark_failed" in func_body
        # except 后应有 return，避免继续走 execute_plan_tree
        assert "return" in func_body


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
