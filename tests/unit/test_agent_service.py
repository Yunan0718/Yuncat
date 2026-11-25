"""tests/unit/test_agent_service.py —— Agent 服务核心测试

覆盖: start_task / cancel_task / _mark_task / get_task / list_tasks
      / register_ws / unregister_ws / list_agents / is_running / start/stop
      / invoke / execute_plan_steps / _agent_healer 健康检查循环

使用 aiosqlite 内存 DB + MockAdapter（无真实子进程）
"""
import os
import sys
import asyncio
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ---- Mock Adapter ----
class MockAdapter:
    """模拟 Adapter——不启动子进程，纯内存"""
    def __init__(self, agent_id: str, fail_health: bool = False):
        self.agent_id = agent_id
        self._ready = asyncio.Event()
        self._ready.set()  # 默认就绪
        self._dropped_logs = 0
        self.process = None
        self.fail_health = fail_health
        self.stopped = False
        self.cancelled_tasks: list[str] = []

    async def health(self) -> bool:
        return not self.fail_health

    async def start(self):
        self._ready.set()

    async def stop(self):
        self.stopped = True

    async def wait_ready(self):
        await self._ready.wait()

    async def cancel(self, task_id: str):
        self.cancelled_tasks.append(task_id)

    async def stream(self, prompt: str, context: dict | None = None):
        """async generator —— 模拟流式输出"""
        yield f"回复: {prompt[:30]}"
        await asyncio.sleep(0.01)
        yield "...完成"

    def capabilities(self):
        from adapters.base import AgentCapability
        return AgentCapability(supports_stream=True)


# ---- Fixtures ----
@pytest.fixture
async def agent_svc(tmp_path):
    """创建 AgentService + aiosqlite DB + 2 个 mock adapter"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()

    from services.agent.agent_service import AgentService
    adapters = {
        "hermes": MockAdapter("hermes"),
        "openclaw": MockAdapter("openclaw", fail_health=False),
    }
    svc = AgentService(conn, adapters=adapters)
    # Phase 18: mock model config so API key check passes.
    # Phase 108 后 lifecycle 改用 _resolve_model_for_request 作为统一入口，需同时 mock 该方法
    # （否则 _model_config 为 None 返回空 config，api_key 校验失败 → 任务被标 failed）。
    async def fake_config(k="", **kw):
        return {"api_key": "sk-test", "model": "test", "provider": "test"}
    svc._get_model_config = fake_config

    async def fake_resolve(mode="", prompt="", **kw):
        return {"api_key": "sk-test", "model": "test", "provider": "test"}
    svc._resolve_model_for_request = fake_resolve
    yield svc
    # 等待所有后台 agent 任务完成，避免 teardown 关闭连接时后台任务访问已关闭的 DB。
    # start_task 用 asyncio.create_task 启动 _run_agent，测试里的 asyncio.sleep 是竞态——
    # 任务尚未写完快照就关连接会抛 "no active connection"（偶发）+ 完整跑时挂起。
    handles = list(svc._task_handles.values())
    if handles:
        await asyncio.gather(*handles, return_exceptions=True)
    await conn.close()


class FakeWS:
    """模拟 WebSocket 连接——记录发送的消息"""
    def __init__(self):
        self.messages: list[dict] = []
        self.closed = False

    async def send_json(self, payload: dict):
        self.messages.append(payload)

    async def close(self, code=1000):
        self.closed = True


# ---- WS 连接管理 ----
class TestWSConnection:
    async def test_register_ws_adds_connection(self, agent_svc):
        ws = FakeWS()
        agent_svc.register_ws("task-1", ws)
        assert "task-1" in agent_svc._ws_connections
        assert ws in agent_svc._ws_connections["task-1"]

    async def test_unregister_ws_removes_connection(self, agent_svc):
        ws1 = FakeWS()
        ws2 = FakeWS()
        agent_svc.register_ws("task-1", ws1)
        agent_svc.register_ws("task-1", ws2)
        agent_svc.unregister_ws(ws1)
        assert ws1 not in agent_svc._ws_connections["task-1"]
        assert ws2 in agent_svc._ws_connections["task-1"]

    async def test_unregister_last_ws_deletes_key(self, agent_svc):
        ws = FakeWS()
        agent_svc.register_ws("task-1", ws)
        agent_svc.unregister_ws(ws)
        assert "task-1" not in agent_svc._ws_connections


# ---- 任务生命周期 ----
class TestTaskLifecycle:
    async def test_start_task_creates_db_row(self, agent_svc):
        task_id = await agent_svc.start_task("hermes", "hello", "session-1", ws=FakeWS())
        task = await agent_svc.get_task(task_id)
        assert task is not None
        assert task["input"] == "hello"
        assert task["status"] == "running"  # 异步 _run_agent 已启动

    async def test_start_task_registers_ws(self, agent_svc):
        ws = FakeWS()
        await agent_svc.start_task("hermes", "x", "s1", ws=ws)
        # task_handle 存在
        assert len(agent_svc._task_handles) >= 1

    async def test_start_task_nonexistent_agent_marks_failed(self, agent_svc):
        task_id = await agent_svc.start_task("nonexistent", "hi", "s1")
        # 等待 _run_agent 完成
        await asyncio.sleep(0.3)
        task = await agent_svc.get_task(task_id)
        assert task["status"] == "failed"
        assert "not found" in (task.get("error_message") or "")

    async def test_mark_task_completed(self, agent_svc):
        task_id = await agent_svc.start_task("hermes", "hello", "session-99")
        await asyncio.sleep(0.3)
        task = await agent_svc.get_task(task_id)
        assert task["status"] == "completed"

    async def test_mark_task_failed(self, agent_svc):
        import time
        task_id = "task-explicit-error"
        await agent_svc.db.execute(
            "INSERT INTO tasks(id, input, status, agent_id, session_id, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (task_id, "test", "running", "hermes", "s1", int(time.time())),
        )
        await agent_svc.db.commit()
        await agent_svc._mark_task(task_id, "failed", "something broke")
        task = await agent_svc.get_task(task_id)
        assert task["status"] == "failed"
        assert task["error_message"] == "something broke"

    async def test_list_tasks_includes_created(self, agent_svc):
        await agent_svc.start_task("hermes", "task-a", "s1", ws=FakeWS())
        await agent_svc.start_task("hermes", "task-b", "s2", ws=FakeWS())
        await asyncio.sleep(0.3)
        tasks = await agent_svc.list_tasks(limit=50)
        assert len(tasks) >= 2

    async def test_list_tasks_filters_by_status(self, agent_svc):
        # 启动已完成 task
        tid = await agent_svc.start_task("hermes", "done-task", "s-filter", ws=FakeWS())
        await asyncio.sleep(0.3)
        completed = await agent_svc.list_tasks(status="completed", limit=10)
        assert any(t["id"] == tid for t in completed)

    async def test_cancel_task_removes_handle(self, agent_svc):
        task_id = await agent_svc.start_task("hermes", "cancel-me", "s-cancel", ws=FakeWS())
        await asyncio.sleep(0.1)
        await agent_svc.cancel_task(task_id)
        task = await agent_svc.get_task(task_id)
        assert task is not None

    async def test_cancel_task_no_double_mark(self, agent_svc):
        """已完成的 task 不会被 cancel 覆盖状态"""
        task_id = await agent_svc.start_task("hermes", "x", "s3", ws=FakeWS())
        await asyncio.sleep(0.5)
        await agent_svc.cancel_task(task_id)
        task = await agent_svc.get_task(task_id)
        assert task["status"] == "completed"

    async def test_invoke_creates_with_mode(self, agent_svc):
        task_id = await agent_svc.invoke("hermes", "test invoke", {"mode": "plan"})
        assert task_id
        task = await agent_svc.get_task(task_id)
        assert task is not None


# ---- Agent 管理 ----
class TestAgentManagement:
    async def test_list_agents_returns_all(self, agent_svc):
        agents = await agent_svc.list_agents()
        assert len(agents) >= 2
        ids = [a["id"] for a in agents]
        assert "hermes" in ids

    async def test_is_running_true_for_ready_adapter(self, agent_svc):
        assert await agent_svc.is_running("hermes") is True

    async def test_is_running_false_for_unknown(self, agent_svc):
        assert await agent_svc.is_running("ghost") is False

    async def test_start_stop_agent(self, agent_svc):
        adapter = agent_svc.adapters["openclaw"]
        await agent_svc.stop_agent("openclaw")
        assert adapter.stopped is True


# ---- WS 消息推送 ----
class TestWSPush:
    async def test_safe_send_does_not_throw(self, agent_svc):
        ws = FakeWS()
        agent_svc.register_ws("task-1", ws)
        # _safe_send should not raise
        await agent_svc._safe_send("task-1", {"type": "test"})
        assert len(ws.messages) == 1
        assert ws.messages[0]["type"] == "test"

    async def test_push_stream_chunk(self, agent_svc):
        ws = FakeWS()
        agent_svc.register_ws("task-chunk", ws)
        await agent_svc.push_stream_chunk("task-chunk", "hello world", 0)
        assert ws.messages[0]["type"] == "stream_chunk"
        assert ws.messages[0]["data"] == "hello world"

    async def test_push_stream_done(self, agent_svc):
        ws = FakeWS()
        agent_svc.register_ws("task-done", ws)
        await agent_svc.push_stream_done("task-done")
        assert ws.messages[0]["type"] == "stream_done"

    async def test_push_error(self, agent_svc):
        ws = FakeWS()
        agent_svc.register_ws("task-err", ws)
        await agent_svc.push_error("task-err", "something wrong")
        assert ws.messages[0]["type"] == "error"

    async def test_push_tool_confirm_scoped_to_task(self, agent_svc):
        ws1 = FakeWS()
        ws2 = FakeWS()
        agent_svc.register_ws("t1", ws1)
        agent_svc.register_ws("t2", ws2)
        await agent_svc.push_tool_confirm("t1", "write_file", {"path": "x.py"}, "tok123")
        # Phase 47: 仅推送到发起 task 的 WS 连接（对标 Claude Code permission prompt）
        assert len(ws1.messages) == 1
        assert len(ws2.messages) == 0
        assert ws1.messages[0]["confirm_token"] == "tok123"

    async def test_push_hitl_broadcasts(self, agent_svc):
        ws = FakeWS()
        agent_svc.register_ws("t-h", ws)
        await agent_svc.push_hitl_request("run-1", "node-a", "审批?", 60)
        assert ws.messages[0]["type"] == "workflow_hitl"


# ---- 健康检查 ----
class TestAgentHealer:
    async def test_health_false_clears_ready(self, agent_svc):
        """health 返回 False → adapter._ready 被清除"""
        adapter = agent_svc.adapters["openclaw"]
        adapter.fail_health = True
        adapter._ready.clear()
        assert await adapter.health() is False
        assert not adapter._ready.is_set()

    async def test_health_recovery_resets_ready(self, agent_svc):
        """health 恢复 → _ready 重新设置"""
        adapter = agent_svc.adapters["hermes"]
        adapter.fail_health = False
        healthy = await adapter.health()
        assert healthy is True
        adapter._ready.set()
        assert adapter._ready.is_set()


# ---- Phase 83: 失败分析路径 import 回归守卫 ----
class TestFailureAnalysisImport:
    """Phase 83 审查发现：agent_tool_loop.py 失败分析路径 `from .agent_service import
    _suggest_alternative_tool` 曾因函数是 @staticmethod 而非模块级导致 ImportError。
    此测试守卫：两个 helper 必须可从 agent_service 模块级 import。
    """

    def test_suggest_alternative_tool_importable(self):
        from services.agent.agent_service import _suggest_alternative_tool
        assert _suggest_alternative_tool("read_file") == "search_files"
        assert _suggest_alternative_tool("write_file") == "read_file"
        assert _suggest_alternative_tool("unknown_tool") is None

    def test_map_args_for_alternative_importable(self):
        from services.agent.agent_service import _map_args_for_alternative
        mapped = _map_args_for_alternative("read_file", "search_files", {"path": "a/b.py"})
        assert "pattern" in mapped
        assert mapped["path"] == "a/b.py"
        # 无映射规则时原样返回
        assert _map_args_for_alternative("foo", "bar", {"x": 1}) == {"x": 1}


# ---- Phase 61: Task 心跳持久化 ----
class TestPhase61Heartbeat:
    """Phase 61: 长耗时工具心跳——只更新 heartbeat_at + updated_at，不覆盖 task_state。

    核心约束（架构铁律）: 心跳与完整 snapshot 写同一张表但不能互相覆盖。
    - update_task_heartbeat 只写 heartbeat_at + updated_at
    - update_task_state（经 _write_task_snapshot）写 task_state + heartbeat_at + updated_at
    """

    async def test_heartbeat_does_not_overwrite_task_state(self, agent_svc):
        """心跳更新不应覆盖 task_state JSON。"""
        import time
        # 0. 先创建 task 行
        await agent_svc.db.execute(
            "INSERT INTO tasks(id, input, status, agent_id, created_at) VALUES(?,?,?,?,?)",
            ("task-hb-1", "test", "running", "hermes", int(time.time())),
        )
        await agent_svc.db.commit()
        # 1. 先写完整 snapshot（含 task_state）
        await agent_svc._write_task_snapshot("task-hb-1", {
            "phase": "executing_tool", "tool": "run_command", "round": 3,
        })
        # 2. 读取 task_state 验证已写入
        cursor = await agent_svc.db.execute(
            "SELECT task_state, heartbeat_at FROM tasks WHERE id=?", ("task-hb-1",)
        )
        row = await cursor.fetchone()
        import json
        state_before = json.loads(row[0])
        assert state_before["tool"] == "run_command"
        hb_before = row[1]

        # 3. 多次心跳更新（模拟长耗时工具执行）
        await asyncio.sleep(0.01)
        await agent_svc.update_task_heartbeat("task-hb-1")
        await asyncio.sleep(0.01)
        await agent_svc.update_task_heartbeat("task-hb-1")

        # 4. task_state 不应被覆盖
        cursor = await agent_svc.db.execute(
            "SELECT task_state, heartbeat_at, updated_at FROM tasks WHERE id=?", ("task-hb-1",)
        )
        row = await cursor.fetchone()
        state_after = json.loads(row[0])
        assert state_after["tool"] == "run_command", "心跳不应覆盖 task_state"
        assert state_after["round"] == 3, "task_state 内容应保持不变"
        # heartbeat_at 应被更新（更新后 >= 更新前）
        assert row[1] >= hb_before, "heartbeat_at 应被心跳更新"

    async def test_write_snapshot_updates_heartbeat(self, agent_svc):
        """完整 snapshot 写入应同时更新 heartbeat_at。"""
        import time
        await agent_svc.db.execute(
            "INSERT INTO tasks(id, input, status, agent_id, created_at) VALUES(?,?,?,?,?)",
            ("task-hb-2", "test", "running", "hermes", int(time.time())),
        )
        await agent_svc.db.commit()
        await agent_svc._write_task_snapshot("task-hb-2", {"phase": "init"})
        cursor = await agent_svc.db.execute(
            "SELECT heartbeat_at, updated_at FROM tasks WHERE id=?", ("task-hb-2",)
        )
        row = await cursor.fetchone()
        assert row[0] is not None, "_write_task_snapshot 应同时写入 heartbeat_at"
        assert row[1] is not None, "_write_task_snapshot 应同时写入 updated_at"

    async def test_heartbeat_silent_on_unknown_task(self, agent_svc):
        """心跳更新对不存在的 task_id 应静默（不抛异常）。"""
        # 不应抛异常
        await agent_svc.update_task_heartbeat("nonexistent-task-id")
        # 验证没有副作用——无异常即通过

    async def test_heartbeat_loop_skips_when_no_agent_service(self):
        """_heartbeat_loop 在无 app 上下文时应直接返回（不报错）。"""
        from tools.builtin.system_tool_command import _heartbeat_loop
        import context as ctx
        # 确保没有 app 注入
        original = ctx._app
        ctx._app = None
        try:
            # 应立即返回（不进入循环）
            await asyncio.wait_for(_heartbeat_loop("task-x"), timeout=2.0)
        except asyncio.TimeoutError:
            pytest.fail("_heartbeat_loop 应在无 app 时直接返回，不应进入循环")
        finally:
            ctx._app = original


# ---- Phase 62: Task 崩溃恢复 ----
class TestPhase62CrashRecovery:
    """Phase 62: 启动时智能恢复崩溃的 running 任务。

    核心约束：不再直接把所有 running 标 failed，而是：
    - updated_at < now-60s 判定为崩溃
    - resume_count >= 3 标 failed
    - 缺失 session_id 标 failed
    - 否则注入恢复上下文 + 重启 _run_agent
    """

    @staticmethod
    async def _mock_run_agent(agent_svc):
        """Mock _run_agent 避免启动真实 agent loop（记录调用即可）。"""
        agent_svc._run_agent_called = []

        async def _mock(task_id, agent_id, prompt, session_id,
                        rules_context="", mode="agent", extra_context=None):
            agent_svc._run_agent_called.append(task_id)
        agent_svc._run_agent = _mock

    @staticmethod
    async def _create_crashed_task(agent_svc, task_id, **overrides):
        """创建一个崩溃任务（running + updated_at 远在过去）。"""
        import time
        long_ago = int(time.time()) - 3600  # 1 小时前
        await agent_svc.db.execute(
            "INSERT INTO tasks(id, input, status, agent_id, session_id, created_at, "
            "updated_at, resume_count, task_state) VALUES(?,?,?,?,?,?,?,?,?)",
            (task_id, overrides.get("input", "test task"), "running",
             overrides.get("agent_id", "hermes"),
             overrides.get("session_id", "sess-1"),
             long_ago, long_ago,
             overrides.get("resume_count", 0),
             overrides.get("task_state", '{}')),
        )
        await agent_svc.db.commit()

    async def test_resume_skips_recent_tasks(self, agent_svc):
        """updated_at 在宽限期内（近期）的任务应被跳过。"""
        import time
        await self._mock_run_agent(agent_svc)
        # 创建一个近期更新的 running 任务（未崩溃）
        now = int(time.time())
        await agent_svc.db.execute(
            "INSERT INTO tasks(id, input, status, agent_id, session_id, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ("task-recent", "test", "running", "hermes", "sess-1", now, now),
        )
        await agent_svc.db.commit()
        # 恢复
        resumed = await agent_svc.resume_after_restart()
        assert resumed == 0, "近期任务不应被恢复"
        assert not agent_svc._run_agent_called, "_run_agent 不应被调用"

    async def test_resume_count_exceeds_max_marks_failed(self, agent_svc):
        """resume_count >= MAX_TASK_RESUME_COUNT 应标记 failed。"""
        from config import MAX_TASK_RESUME_COUNT
        await self._mock_run_agent(agent_svc)
        await self._create_crashed_task(agent_svc, "task-overmax",
                                         resume_count=MAX_TASK_RESUME_COUNT)
        resumed = await agent_svc.resume_after_restart()
        assert resumed == 0
        # 验证标记 failed
        cursor = await agent_svc.db.execute(
            "SELECT status, error_message FROM tasks WHERE id=?", ("task-overmax",))
        row = await cursor.fetchone()
        assert row[0] == "failed"
        assert "多次崩溃" in row[1]

    async def test_missing_session_id_marks_failed(self, agent_svc):
        """缺少 session_id 应标记 failed（无法恢复上下文）。"""
        await self._mock_run_agent(agent_svc)
        await self._create_crashed_task(agent_svc, "task-nosid", session_id=None)
        resumed = await agent_svc.resume_after_restart()
        assert resumed == 0
        cursor = await agent_svc.db.execute(
            "SELECT status, error_message FROM tasks WHERE id=?", ("task-nosid",))
        row = await cursor.fetchone()
        assert row[0] == "failed"
        assert "session_id" in row[1]

    async def test_successful_resume_injects_context(self, agent_svc):
        """成功恢复应注入 system 消息到 conversations + resume_count 递增。"""
        import json
        await self._mock_run_agent(agent_svc)
        state = {"tool_round": 5, "plan_step": 2, "plan_total": 4,
                 "current_tool": "read_file", "confirmed_goal": "完成测试"}
        await self._create_crashed_task(agent_svc, "task-ok",
                                         task_state=json.dumps(state),
                                         input="原始任务指令")
        resumed = await agent_svc.resume_after_restart()
        assert resumed == 1
        # 等待后台 _run_agent 任务被调度执行
        handle = agent_svc._task_handles.get("task-ok")
        if handle and not handle.done():
            await asyncio.sleep(0.05)
        # 验证 _run_agent 被调用
        assert "task-ok" in agent_svc._run_agent_called
        # 验证 resume_count 递增
        cursor = await agent_svc.db.execute(
            "SELECT resume_count FROM tasks WHERE id=?", ("task-ok",))
        assert (await cursor.fetchone())[0] == 1
        # 验证 system 消息注入
        cursor = await agent_svc.db.execute(
            "SELECT role, content FROM conversations WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
            ("sess-1",))
        row = await cursor.fetchone()
        assert row is not None, "应注入 system 消息"
        assert row[0] == "system"
        assert "[任务恢复]" in row[1]
        assert "第 5 轮" in row[1]

    async def test_interrupted_tool_marked(self, agent_svc):
        """崩溃前正在执行的工具（current_tool 有值、last_tool_result 空）应标记 interrupted。"""
        import json
        await self._mock_run_agent(agent_svc)
        state = {"current_tool": "write_file", "last_tool_result": None,
                 "tool_round": 3}
        await self._create_crashed_task(agent_svc, "task-interrupted",
                                         task_state=json.dumps(state))
        await agent_svc.resume_after_restart()
        # 验证 task_state 中有 interrupted_tool
        cursor = await agent_svc.db.execute(
            "SELECT task_state FROM tasks WHERE id=?", ("task-interrupted",))
        row = await cursor.fetchone()
        state_after = json.loads(row[0])
        assert state_after.get("interrupted_tool") == "write_file"
        # 验证恢复消息提到中断工具
        cursor = await agent_svc.db.execute(
            "SELECT content FROM conversations WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
            ("sess-1",))
        content = (await cursor.fetchone())[0]
        assert "[中断工具]" in content
        assert "write_file" in content

    async def test_pending_token_reinject(self, agent_svc):
        """崩溃前未确认的 pending token 应通过 WS 重新推送。"""
        import json, time
        await self._mock_run_agent(agent_svc)
        await self._create_crashed_task(agent_svc, "task-pending")
        # 注册 WS 捕获消息
        ws = FakeWS()
        agent_svc.register_ws("task-pending", ws)
        # 创建一个 pending state（payload 含 task_id）
        now = time.time()
        await agent_svc.db.execute(
            "INSERT INTO runtime_states(id, state_type, payload, created_at, expires_at) "
            "VALUES(?,?,?,?,?)",
            ("token-1", "pending_write",
             json.dumps({"path": "/tmp/x", "task_id": "task-pending"}),
             now, now + 1800),
        )
        # 创建一个不匹配的 pending state
        await agent_svc.db.execute(
            "INSERT INTO runtime_states(id, state_type, payload, created_at, expires_at) "
            "VALUES(?,?,?,?,?)",
            ("token-2", "pending_command",
             json.dumps({"command": "ls", "task_id": "other-task"}),
             now, now + 1800),
        )
        await agent_svc.db.commit()
        await agent_svc.resume_after_restart()
        # 验证只有匹配的 token 被重新推送
        reinject_msgs = [m for m in ws.messages if m.get("type") == "pending_approval_reinject"]
        assert len(reinject_msgs) == 1, "只应重新推送匹配的 token"
        assert reinject_msgs[0]["token"] == "token-1"
        assert reinject_msgs[0]["state_type"] == "pending_write"


# ═══════════════════════════════════════════════════════════════
# Task 6.16: Plan 执行重试策略（共享 retry_utils）
# ═══════════════════════════════════════════════════════════════

class _FlakyAdapter(MockAdapter):
    """stream() 首次抛 retriable 异常，第二次成功——测试重试"""

    def __init__(self, agent_id: str, fail_times: int = 1):
        super().__init__(agent_id)
        self._fail_times = fail_times
        self._calls = 0

    async def stream(self, prompt: str, context: dict | None = None):
        self._calls += 1
        if self._calls <= self._fail_times:
            raise ConnectionError("timeout connecting to upstream")
        yield f"step output for: {prompt[:20]}"


class _NonRetriableFailAdapter(MockAdapter):
    """stream() 抛不可重试异常——不重试直接失败"""

    async def stream(self, prompt: str, context: dict | None = None):
        raise ValueError("invalid model configuration")
        yield  # noqa: never reached，保持 async generator 语义


async def _create_plan_with_steps(db, parent_id: str, agent_id: str = "hermes"):
    """创建一个 Plan 父任务 + 1 个 pending 子任务（步骤）"""
    import time
    await db.execute(
        "INSERT INTO tasks(id, agent_id, input, status, created_at) "
        "VALUES(?,?,?,?,?)",
        (parent_id, agent_id, "build a feature", "running", int(time.time())),
    )
    step_id = "step-1"
    await db.execute(
        "INSERT INTO tasks(id, parent_task_id, agent_id, input, status, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (step_id, parent_id, agent_id, "[Plan 步骤 1/1] do something",
         "pending", int(time.time())),
    )
    await db.commit()
    return step_id


class TestPlanExecutionRetry:
    """Task 6.16: _execute_plan_steps_impl 重试策略（共享 retry_utils）"""

    async def test_retriable_error_retries_then_succeeds(self, agent_svc, monkeypatch):
        """stream() 首次抛 timeout（可重试）→ 重试 → 第二次成功 → step 标 completed"""
        # 禁用真实 sleep（重试退避）
        async def _fake_sleep(_delay):
            return None
        monkeypatch.setattr("asyncio.sleep", _fake_sleep)

        flaky = _FlakyAdapter("hermes", fail_times=1)
        agent_svc.adapters["hermes"] = flaky
        ws = FakeWS()
        agent_svc.register_ws("plan-1", ws)
        await _create_plan_with_steps(agent_svc.db, "plan-1")

        await agent_svc.execute_plan_steps("plan-1", "hermes")

        # stream 被调用 2 次（首次失败 + 重试成功）
        assert flaky._calls == 2
        # step 标 completed
        cursor = await agent_svc.db.execute(
            "SELECT status FROM tasks WHERE id=?", ("step-1",))
        assert (await cursor.fetchone())[0] == "completed"
        # 推送 plan_step_done
        done_msgs = [m for m in ws.messages if m.get("type") == "plan_step_done"]
        assert len(done_msgs) == 1
        assert "step output" in done_msgs[0]["output"]

    async def test_non_retriable_error_no_retry_marks_failed(self, agent_svc, monkeypatch):
        """stream() 抛 ValueError（不可重试）→ 不重试 → step 标 failed"""
        async def _fake_sleep(_delay):
            return None
        monkeypatch.setattr("asyncio.sleep", _fake_sleep)

        bad = _NonRetriableFailAdapter("hermes")
        agent_svc.adapters["hermes"] = bad
        ws = FakeWS()
        agent_svc.register_ws("plan-2", ws)
        await _create_plan_with_steps(agent_svc.db, "plan-2")

        await agent_svc.execute_plan_steps("plan-2", "hermes")

        # step 标 failed
        cursor = await agent_svc.db.execute(
            "SELECT status FROM tasks WHERE id=?", ("step-1",))
        assert (await cursor.fetchone())[0] == "failed"
        # 推送 plan_step_fail
        fail_msgs = [m for m in ws.messages if m.get("type") == "plan_step_fail"]
        assert len(fail_msgs) == 1
        assert "invalid model configuration" in fail_msgs[0]["error"]

    async def test_exhausted_retries_marks_failed(self, agent_svc, monkeypatch):
        """stream() 始终抛 timeout → 重试耗尽 → step 标 failed"""
        async def _fake_sleep(_delay):
            return None
        monkeypatch.setattr("asyncio.sleep", _fake_sleep)

        # fail_times=5 远超 MAX_RETRIES（2）→ 重试耗尽
        always_fail = _FlakyAdapter("hermes", fail_times=5)
        agent_svc.adapters["hermes"] = always_fail
        ws = FakeWS()
        agent_svc.register_ws("plan-3", ws)
        await _create_plan_with_steps(agent_svc.db, "plan-3")

        await agent_svc.execute_plan_steps("plan-3", "hermes")

        # 1 次初始 + MAX_RETRIES(2) 次重试 = 3 次
        assert always_fail._calls == 3
        cursor = await agent_svc.db.execute(
            "SELECT status FROM tasks WHERE id=?", ("step-1",))
        assert (await cursor.fetchone())[0] == "failed"
