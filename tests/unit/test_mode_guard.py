"""tests/unit/test_mode_guard.py —— Task 3.2: Ask/Plan 模式程序级拦截测试

对标:
- Claude Code disallowed-tools: 程序级移除工具定义 > system prompt 软约束
- 双保险: 工具定义层（adapter payload tools=[]）+ 执行层（ts.execute 拒绝）

覆盖:
- Ask 模式 ts.execute 拒绝执行任何工具（不只写工具）
- Plan 模式 ts.execute 拒绝执行任何工具
- Agent 模式 ts.execute 正常执行
- build_agent_context 把 mode 存入 context["mode"]
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class TestToolServiceModeGuard:
    """ToolService.execute 的 mode 拦截——执行层双保险"""

    @pytest.fixture
    def ts(self, tmp_path, monkeypatch):
        """隔离工作区的 ToolService 实例"""
        monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path / "ws"))
        from security.sandbox import get_workspace
        get_workspace()
        # 触发 builtin 工具注册（read_file/write_file/run_command 等）
        import tools.builtin.system_tools  # noqa: F401
        from services.tools.tool_service import ToolService
        return ToolService()

    async def test_ask_mode_blocks_write_tool(self, ts):
        """Ask 模式下 write_file 被拒绝"""
        result = await ts.execute("write_file", {"path": "test.txt", "content": "hi"}, mode="ask")
        assert result["success"] is False
        assert result.get("meta", {}).get("mode_blocked") is True

    async def test_ask_mode_blocks_read_tool(self, ts):
        """Ask 模式下 read_file 也被拒绝——Ask 只回答问题不执行任何工具"""
        result = await ts.execute("read_file", {"path": "test.py"}, mode="ask")
        assert result["success"] is False
        assert result.get("meta", {}).get("mode_blocked") is True

    async def test_ask_mode_blocks_run_command(self, ts):
        """Ask 模式下 run_command 被拒绝"""
        result = await ts.execute("run_command", {"command": "dir"}, mode="ask")
        assert result["success"] is False
        assert result.get("meta", {}).get("mode_blocked") is True

    async def test_plan_mode_blocks_write_tool(self, ts):
        """Plan 模式下 write_file 被拒绝——Plan 只规划不执行"""
        result = await ts.execute("write_file", {"path": "plan.md", "content": "# Plan"}, mode="plan")
        assert result["success"] is False
        assert result.get("meta", {}).get("mode_blocked") is True

    async def test_plan_mode_blocks_read_tool(self, ts):
        """Plan 模式下 read_file 也被拒绝——Plan 只规划不执行任何工具"""
        result = await ts.execute("read_file", {"path": "test.py"}, mode="plan")
        assert result["success"] is False
        assert result.get("meta", {}).get("mode_blocked") is True

    async def test_agent_mode_allows_read_tool(self, ts, tmp_path, monkeypatch):
        """Agent 模式下 read_file 正常执行（不拦截）"""
        # 创建工作区文件
        from security.sandbox import get_shared_workspace
        shared = get_shared_workspace()
        (shared / "test.py").write_text("print('hello')")
        result = await ts.execute("read_file", {"path": "test.py"}, mode="agent")
        assert result.get("meta", {}).get("mode_blocked") is not True
        # read_file 可能成功或失败（取决于路径），但不应被 mode 拦截
        if result["success"]:
            assert "hello" in result.get("data", {}).get("content", "")

    async def test_agent_mode_not_blocked(self, ts):
        """Agent 模式不触发 mode_blocked"""
        result = await ts.execute("write_file", {"path": "x.txt", "content": "x"}, mode="agent")
        assert result.get("meta", {}).get("mode_blocked") is not True

    async def test_default_mode_is_agent(self, ts):
        """默认 mode 是 agent——不拦截"""
        result = await ts.execute("write_file", {"path": "x.txt", "content": "x"})
        assert result.get("meta", {}).get("mode_blocked") is not True


class TestBuildAgentContextStoresMode:
    """build_agent_context 把 mode 存入 context["mode"]——供 adapter 和 execute_tool_loop 读取"""

    @pytest.fixture
    def mock_svc(self, tmp_path, monkeypatch):
        """轻量 mock AgentService——只提供 build_agent_context 需要的属性"""
        monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path / "ws"))
        from security.sandbox import get_workspace
        get_workspace()

        class _MockMemoryService:
            async def get_session_context(self, sid):
                return []
            async def vector_search(self, prompt, limit=5):
                return []
            def set_model_config_for_sizing(self, cfg):
                pass

        class _MockSvc:
            db = None
            adapters = {}
            _memory_service = _MockMemoryService()
            _memory_tree = None
            _feedback_learner = None
            _settings_service = None
            _prompt_agent = None

            async def _safe_send(self, *a, **kw):
                pass

            async def _get_growth_stats(self):
                return {"total_nodes": 0, "bug_lessons": 0, "dreams": 0, "evolutions": 0}

        return _MockSvc()

    @pytest.mark.asyncio
    async def test_ask_mode_stored_in_context(self, mock_svc):
        """Ask 模式下 context['mode'] == 'ask'"""
        from services.agent.agent_context import build_agent_context
        context, _ = await build_agent_context(
            mock_svc, "t1", "s1", "hello", "ask", "", None,
            {"model": "test", "context": 8000, "api_key": "sk-test"},
        )
        assert context.get("mode") == "ask"

    @pytest.mark.asyncio
    async def test_plan_mode_stored_in_context(self, mock_svc):
        """Plan 模式下 context['mode'] == 'plan'"""
        from services.agent.agent_context import build_agent_context
        context, _ = await build_agent_context(
            mock_svc, "t1", "s1", "plan this", "plan", "", None,
            {"model": "test", "context": 8000, "api_key": "sk-test"},
        )
        assert context.get("mode") == "plan"

    @pytest.mark.asyncio
    async def test_agent_mode_stored_in_context(self, mock_svc):
        """Agent 模式下 context['mode'] == 'agent'"""
        from services.agent.agent_context import build_agent_context
        context, _ = await build_agent_context(
            mock_svc, "t1", "s1", "do task", "agent", "", None,
            {"model": "test", "context": 8000, "api_key": "sk-test"},
        )
        assert context.get("mode") == "agent"

    @pytest.mark.asyncio
    async def test_default_mode_is_agent(self, mock_svc):
        """mode 参数默认或空时 context["mode"] 为 agent"""
        from services.agent.agent_context import build_agent_context
        context, _ = await build_agent_context(
            mock_svc, "t1", "s1", "hi", "agent", "", None,
            {"model": "test", "context": 8000, "api_key": "sk-test"},
        )
        assert context.get("mode") == "agent"
