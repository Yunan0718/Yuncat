"""tests/unit/test_m3_session_summary_inheritance.py —— M3: 跨 session 记忆继承

对标 Claude Code --continue：新会话可继承同 workspace 上次会话的结构化摘要。

覆盖:
- session_summaries 表的 repo 函数（save / get_last / 过滤 / 排除）
- _format_inherited_summary 格式化纯函数
- _inject_inherited_summary 注入逻辑（新会话注入 / 已有会话不注入 / 无摘要不注入）

使用 aiosqlite 内存 DB（无真实子进程 / 无 LLM 调用）
"""
import os
import sys
import time
import asyncio
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ============================================================
# Repo 函数测试
# ============================================================

class TestSessionSummaryRepo:
    """session_summary_repo: save / get_last_session_summary"""

    @pytest.fixture
    async def db(self, tmp_path):
        import aiosqlite
        db_path = os.path.join(str(tmp_path), "test.db")
        conn = await aiosqlite.connect(db_path)
        from repositories import SCHEMA_SQL
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()
        yield conn
        await conn.close()

    async def test_save_and_get_last_summary(self, db):
        """保存摘要后能按 workspace 查到最近一条。"""
        from repositories.session_summary_repo import (
            save_session_summary, get_last_session_summary,
        )
        summary = {"session_intent": "修 bug", "key_decisions": ["用乐观锁"]}
        await save_session_summary(db, "sess-A", summary, "/ws/proj1")
        result = await get_last_session_summary(db, "/ws/proj1", exclude_session_id="sess-other")
        assert result is not None
        assert result["session_id"] == "sess-A"
        assert result["summary"]["session_intent"] == "修 bug"

    async def test_save_upserts_existing_session(self, db):
        """同 session_id 再次保存 → 更新而非插入。"""
        from repositories.session_summary_repo import (
            save_session_summary, get_last_session_summary,
        )
        await save_session_summary(db, "sess-X", {"session_intent": "v1"}, "/ws/p")
        await save_session_summary(db, "sess-X", {"session_intent": "v2"}, "/ws/p")
        cursor = await db.execute("SELECT COUNT(*) FROM session_summaries WHERE session_id=?", ("sess-X",))
        count = (await cursor.fetchone())[0]
        assert count == 1
        result = await get_last_session_summary(db, "/ws/p", exclude_session_id="other")
        assert result["summary"]["session_intent"] == "v2"

    async def test_get_last_excludes_current_session(self, db):
        """get_last_session_summary 排除当前 session_id。"""
        from repositories.session_summary_repo import (
            save_session_summary, get_last_session_summary,
        )
        await save_session_summary(db, "sess-A", {"session_intent": "A"}, "/ws/p")
        await save_session_summary(db, "sess-B", {"session_intent": "B"}, "/ws/p")
        # 排除 sess-B → 应返回 sess-A
        result = await get_last_session_summary(db, "/ws/p", exclude_session_id="sess-B")
        assert result["session_id"] == "sess-A"
        # 排除 sess-A → 应返回 sess-B
        result2 = await get_last_session_summary(db, "/ws/p", exclude_session_id="sess-A")
        assert result2["session_id"] == "sess-B"

    async def test_get_last_returns_none_when_empty(self, db):
        """无摘要时返回 None。"""
        from repositories.session_summary_repo import get_last_session_summary
        result = await get_last_session_summary(db, "/ws/p", exclude_session_id="sess-X")
        assert result is None

    async def test_get_last_filters_by_workspace(self, db):
        """不同 workspace 的摘要互不干扰。"""
        from repositories.session_summary_repo import (
            save_session_summary, get_last_session_summary,
        )
        await save_session_summary(db, "sess-A", {"session_intent": "proj1"}, "/ws/proj1")
        await save_session_summary(db, "sess-B", {"session_intent": "proj2"}, "/ws/proj2")
        result = await get_last_session_summary(db, "/ws/proj1", exclude_session_id="other")
        assert result is not None
        assert result["summary"]["session_intent"] == "proj1"
        result2 = await get_last_session_summary(db, "/ws/proj2", exclude_session_id="other")
        assert result2["summary"]["session_intent"] == "proj2"

    async def test_get_last_returns_most_recent(self, db):
        """多条摘要时返回 updated_at 最大的一条。"""
        from repositories.session_summary_repo import (
            save_session_summary, get_last_session_summary,
        )
        # sess-A 先保存（旧），sess-B 后保存（新）
        await save_session_summary(db, "sess-A", {"session_intent": "old"}, "/ws/p")
        # 确保 updated_at 有差异
        await db.execute("UPDATE session_summaries SET updated_at=? WHERE session_id=?",
                         (int(time.time()) - 100, "sess-A"))
        await db.commit()
        await save_session_summary(db, "sess-B", {"session_intent": "new"}, "/ws/p")
        result = await get_last_session_summary(db, "/ws/p", exclude_session_id="other")
        assert result["session_id"] == "sess-B"


# ============================================================
# 格式化纯函数测试
# ============================================================

class TestFormatInheritedSummary:
    """_format_inherited_summary: dict → 注入文本"""

    def test_formats_all_sections(self):
        from services.agent.agent_context import _format_inherited_summary
        summary = {
            "session_intent": "修复登录 bug",
            "key_decisions": ["用 JWT", "加 rate limit"],
            "files_modified": ["auth.py", "login.py"],
            "pending_tasks": "写测试",
            "errors_and_fixes": "Token 泄漏 → 改临时文件",
            "next_steps": "部署",
        }
        text = _format_inherited_summary(summary)
        assert "上次会话摘要" in text
        assert "修复登录 bug" in text
        assert "JWT" in text
        assert "auth.py" in text
        assert "写测试" in text

    def test_returns_empty_for_empty_summary(self):
        from services.agent.agent_context import _format_inherited_summary
        text = _format_inherited_summary({})
        assert text == ""

    def test_returns_empty_for_none_values(self):
        from services.agent.agent_context import _format_inherited_summary
        text = _format_inherited_summary({"session_intent": None, "key_decisions": []})
        assert text == ""

    def test_handles_list_values(self):
        from services.agent.agent_context import _format_inherited_summary
        text = _format_inherited_summary({"key_decisions": ["A", "B", "C"]})
        assert "A" in text
        assert "B" in text
        assert "C" in text

    def test_caps_total_length(self):
        """超长摘要应被截断——防上下文膨胀。"""
        from services.agent.agent_context import _format_inherited_summary
        summary = {"session_intent": "X" * 5000}
        text = _format_inherited_summary(summary)
        assert len(text) <= 2000  # 应被截断


# ============================================================
# 注入逻辑测试（_inject_inherited_summary）
# ============================================================

class TestInjectInheritedSummary:
    """_inject_inherited_summary: 新会话注入 / 已有会话不注入 / 无摘要不注入"""

    @pytest.fixture
    async def setup(self, tmp_path, monkeypatch):
        """创建 DB + 预置 session_summaries + mock workspace"""
        import aiosqlite
        from pathlib import Path
        db_path = os.path.join(str(tmp_path), "test.db")
        conn = await aiosqlite.connect(db_path)
        from repositories import SCHEMA_SQL
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()

        from repositories.session_summary_repo import save_session_summary
        # 预置一个上次会话的摘要
        # workspace 字符串需与生产代码 str(get_workspace()) 一致（Windows 下 Path 转反斜杠）
        ws_str = str(Path("/ws/test-workspace"))
        await save_session_summary(conn, "sess-prev", {
            "session_intent": "上次会话的目标",
            "key_decisions": ["上次决策"],
        }, ws_str)

        # mock workspace——M3 代码内 from security.sandbox import get_workspace
        monkeypatch.setattr(
            "security.sandbox.get_workspace",
            lambda: Path("/ws/test-workspace"),
        )

        # 最小化 fake AgentService——只需 self.db
        class FakeAgentService:
            def __init__(self, db):
                self.db = db

        svc = FakeAgentService(conn)
        yield conn, svc
        await conn.close()

    async def test_new_session_injects_summary(self, setup):
        """新会话（无 assistant 回复）→ 注入上次会话摘要"""
        from services.agent.agent_context import _inject_inherited_summary
        conn, svc = setup
        # 新会话：session_ctx 只有 user 消息
        session_ctx = [{"role": "user", "content": "hello"}]
        result = await _inject_inherited_summary(svc, session_ctx, "sess-new", "BASE PROMPT")
        assert "上次会话摘要" in result
        assert "上次会话的目标" in result
        assert "BASE PROMPT" in result

    async def test_existing_session_no_injection(self, setup):
        """已有 assistant 回复 → 不注入"""
        from services.agent.agent_context import _inject_inherited_summary
        conn, svc = setup
        session_ctx = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello back"},
        ]
        result = await _inject_inherited_summary(svc, session_ctx, "sess-existing", "BASE PROMPT")
        assert result == "BASE PROMPT"
        assert "上次会话摘要" not in result

    async def test_no_previous_summary_no_injection(self, setup):
        """无上次摘要 → 不注入（不报错）"""
        from services.agent.agent_context import _inject_inherited_summary
        conn, svc = setup
        # workspace 下只有 sess-prev，排除当前 session=sess-prev → 无结果
        session_ctx = [{"role": "user", "content": "hello"}]
        result = await _inject_inherited_summary(svc, session_ctx, "sess-prev", "BASE PROMPT")
        assert result == "BASE PROMPT"

    async def test_empty_session_ctx_injects(self, setup):
        """空 session_ctx（边缘情况）→ 仍注入（视为新会话）"""
        from services.agent.agent_context import _inject_inherited_summary
        conn, svc = setup
        result = await _inject_inherited_summary(svc, [], "sess-new", "BASE PROMPT")
        assert "上次会话摘要" in result

    async def test_different_workspace_no_injection(self, setup, monkeypatch):
        """不同 workspace → 不注入（摘要不匹配）"""
        from pathlib import Path
        from services.agent.agent_context import _inject_inherited_summary
        conn, svc = setup
        # 切换到不同 workspace
        monkeypatch.setattr(
            "security.sandbox.get_workspace",
            lambda: Path("/ws/other-project"),
        )
        session_ctx = [{"role": "user", "content": "hello"}]
        result = await _inject_inherited_summary(svc, session_ctx, "sess-new", "BASE PROMPT")
        assert result == "BASE PROMPT"
