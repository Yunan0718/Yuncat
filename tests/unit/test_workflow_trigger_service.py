"""tests/unit/test_workflow_trigger_service.py —— Phase 101: 工作流事件触发器合约测试

覆盖:
- workflow_triggers 表 CRUD
- 事件匹配: config.event + config.filter
- cron 触发: next_run_at 到期 + 更新下次时间
- manual 触发: fire_trigger 直接返回 run_id
- 审批拦截: require_approval=True 时创建 pending_state 令牌
- dispatch_confirm 审批通过后执行工作流
"""
import json
import os
import sys
import tempfile
import time as _time

import aiosqlite
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from repositories import init_db
from repositories.schema import SCHEMA_SQL


@pytest.fixture
async def db():
    """fresh DB——executescript(SCHEMA_SQL)，并插入测试用 workflows 行满足外键约束。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = await init_db(path)
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    # Phase 101: workflow_triggers 有外键 REFERENCES workflows(id)
    await conn.execute(
        "INSERT OR IGNORE INTO workflows(id, name, graph) VALUES(?,?,?)",
        ("wf-1", "test wf 1", "{}"),
    )
    await conn.execute(
        "INSERT OR IGNORE INTO workflows(id, name, graph) VALUES(?,?,?)",
        ("wf-2", "test wf 2", "{}"),
    )
    await conn.commit()
    yield conn
    await conn.close()
    os.unlink(path)


@pytest.fixture
def mock_workflow_service():
    """记录 run() 调用，返回固定 run_id。"""
    class _Service:
        def __init__(self):
            self.calls = []

        async def run(self, workflow_id: str, initial_input: str):
            self.calls.append((workflow_id, initial_input))
            return f"run-{workflow_id}"

    return _Service()


@pytest.fixture
async def svc(db, mock_workflow_service):
    from services.workflow_trigger_service import WorkflowTriggerService
    return WorkflowTriggerService(db=db, workflow_service=mock_workflow_service)


# ============================================================
# 1. CRUD
# ============================================================
@pytest.mark.unit
class TestTriggerCrud:
    async def test_create_and_get(self, svc):
        tid = await svc.create_trigger(
            "wf-1", "event",
            {"event": "task_completed", "filter": {"agent_id": "hermes"}},
        )
        trigger = await svc.get_trigger(tid)
        assert trigger["workflow_id"] == "wf-1"
        assert trigger["trigger_type"] == "event"
        assert trigger["config"]["event"] == "task_completed"
        assert trigger["enabled"] is True

    async def test_list_by_workflow(self, svc):
        await svc.create_trigger("wf-1", "manual", {})
        await svc.create_trigger("wf-1", "event", {"event": "file_changed"})
        await svc.create_trigger("wf-2", "manual", {})
        items = await svc.list_triggers("wf-1")
        assert len(items) == 2
        assert {i["trigger_type"] for i in items} == {"manual", "event"}

    async def test_delete_trigger(self, svc):
        tid = await svc.create_trigger("wf-1", "manual", {})
        assert await svc.delete_trigger(tid) is True
        assert await svc.get_trigger(tid) is None
        assert await svc.delete_trigger(tid) is False


# ============================================================
# 2. 事件匹配
# ============================================================
@pytest.mark.unit
class TestEventMatching:
    def test_match_event_simple(self, svc):
        config = {"event": "task_completed", "filter": {"agent_id": "hermes"}}
        assert svc.match_event(config, "task_completed", {"agent_id": "hermes"}) is True
        assert svc.match_event(config, "task_failed", {"agent_id": "hermes"}) is False
        assert svc.match_event(config, "task_completed", {"agent_id": "openclaw"}) is False

    def test_match_event_no_filter(self, svc):
        config = {"event": "file_changed"}
        assert svc.match_event(config, "file_changed", {"path": "x"}) is True
        assert svc.match_event(config, "other", {}) is False

    def test_match_event_substring(self, svc):
        config = {"event": "task_completed", "filter": {"output": "success"}}
        assert svc.match_event(config, "task_completed", {"output": "operation success"}) is True
        assert svc.match_event(config, "task_completed", {"output": "failed"}) is False

    async def test_handle_event_triggers_workflow(self, svc, mock_workflow_service):
        await svc.create_trigger(
            "wf-1", "event",
            {"event": "task_completed", "filter": {"agent_id": "hermes"}},
        )
        await svc.handle_event("task_completed", {"agent_id": "hermes", "result": "ok"})
        assert len(mock_workflow_service.calls) == 1
        assert mock_workflow_service.calls[0][0] == "wf-1"

    async def test_handle_event_no_match(self, svc, mock_workflow_service):
        await svc.create_trigger(
            "wf-1", "event",
            {"event": "task_completed", "filter": {"agent_id": "hermes"}},
        )
        await svc.handle_event("task_completed", {"agent_id": "openclaw"})
        assert len(mock_workflow_service.calls) == 0


# ============================================================
# 3. Cron 触发
# ============================================================
@pytest.mark.unit
class TestCronTrigger:
    async def test_cron_next_run_at_set_on_create(self, svc):
        now = int(_time.time())
        tid = await svc.create_trigger(
            "wf-1", "cron",
            {"cron_expr": "* * * * *"},  # 每分钟
        )
        trigger = await svc.get_trigger(tid)
        assert trigger["next_run_at"] is not None
        assert trigger["next_run_at"] > now

    async def test_cron_due_triggers_and_updates_next_run(self, svc, mock_workflow_service):
        now = int(_time.time())
        tid = await svc.create_trigger(
            "wf-1", "cron",
            {"cron_expr": "* * * * *", "initial_input": "cron-input"},
        )
        # 强制已到期
        await svc.db.execute(
            "UPDATE workflow_triggers SET next_run_at=? WHERE id=?",
            (now - 1, tid),
        )
        await svc.db.commit()

        count = await svc.cron_tick(now=now)
        assert count == 1
        assert len(mock_workflow_service.calls) == 1
        assert mock_workflow_service.calls[0] == ("wf-1", "cron-input")

        trigger = await svc.get_trigger(tid)
        assert trigger["next_run_at"] > now
        assert trigger["run_count"] == 1

    async def test_cron_not_due(self, svc, mock_workflow_service):
        now = int(_time.time())
        tid = await svc.create_trigger(
            "wf-1", "cron",
            {"cron_expr": "0 0 * * *"},  # 每天 0:0
        )
        await svc.db.execute(
            "UPDATE workflow_triggers SET next_run_at=? WHERE id=?",
            (now + 3600, tid),
        )
        await svc.db.commit()

        count = await svc.cron_tick(now=now)
        assert count == 0
        assert len(mock_workflow_service.calls) == 0


# ============================================================
# 4. Manual 触发
# ============================================================
@pytest.mark.unit
class TestManualTrigger:
    async def test_manual_fire(self, svc, mock_workflow_service):
        tid = await svc.create_trigger("wf-1", "manual", {"initial_input": "hello"})
        result = await svc.fire_trigger(tid)
        assert result["success"] is True
        assert result["run_id"] == "run-wf-1"
        assert mock_workflow_service.calls[-1] == ("wf-1", "hello")

    async def test_fire_disabled_trigger(self, svc):
        tid = await svc.create_trigger("wf-1", "manual", {}, enabled=False)
        result = await svc.fire_trigger(tid)
        assert result["success"] is False
        assert result["error"] == "trigger-disabled"

    async def test_fire_nonexistent_trigger(self, svc):
        result = await svc.fire_trigger("missing-id")
        assert result["success"] is False
        assert result["error"] == "trigger-not-found"


# ============================================================
# 5. 审批拦截
# ============================================================
@pytest.mark.unit
class TestApprovalInterception:
    async def test_require_approval_creates_pending_token(self, svc, mock_workflow_service):
        tid = await svc.create_trigger(
            "wf-1", "manual",
            {"initial_input": "approve me", "require_approval": True},
        )
        result = await svc.fire_trigger(tid)
        assert result["success"] is True
        assert result["status"] == "pending_approval"
        assert "token" in result
        # 工作流未直接执行
        assert len(mock_workflow_service.calls) == 0

        # runtime_states 表中有 pending 记录
        cursor = await svc.db.execute(
            "SELECT id, state_type, status, title, task_id FROM runtime_states "
            "WHERE id=?", (result["token"],)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[1] == "pending_workflow_trigger"
        assert row[2] == "pending"
        assert row[4] == tid


# ============================================================
# 6. dispatch_confirm 审批通过后执行工作流
# ============================================================
@pytest.mark.unit
class TestDispatchConfirmExecution:
    async def test_approve_then_runs_workflow(self, db, mock_workflow_service):
        from services.workflow_trigger_service import WorkflowTriggerService
        from tools.builtin.system_tool_confirm import (
            create_pending_state, dispatch_confirm,
        )

        svc = WorkflowTriggerService(db=db, workflow_service=mock_workflow_service)

        # 模拟 context 单例，dispatch_confirm handler 会通过 get_app() 读取
        class _FakeApp:
            class state:
                workflow_service = mock_workflow_service
                workflow_trigger_service = svc

        from context import set_app
        set_app(_FakeApp)

        token = await create_pending_state(
            db, "pending_workflow_trigger",
            {"workflow_id": "wf-1", "initial_input": "approved", "trigger_id": "t-1"},
            title="test",
        )
        result = await dispatch_confirm(db, token)
        assert result["success"] is True
        assert result["run_id"] == "run-wf-1"
        assert len(mock_workflow_service.calls) == 1
        assert mock_workflow_service.calls[0] == ("wf-1", "approved")
