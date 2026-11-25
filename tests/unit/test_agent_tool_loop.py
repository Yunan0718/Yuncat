"""tests/unit/test_agent_tool_loop.py —— Phase 83 Gap 3 集成测试

覆盖 _execute_exclusive_tools 的 confirmable 分支 + Phase 83 pre_dangerous_tool 接入：
1. halt —— cancel pending + 返回错误 result + push_tool_confirm 不调用
2. ask_user —— review 合并到 _confirm_data["_review"] + push_tool_confirm 调用
3. proceed —— 正常确认流 + review 合并到弹窗
4. 单例缺失 —— _review_dangerous_tool 返回 None + 正常确认流不阻塞

设计要点：
- _execute_exclusive_tools 是模块级函数（接收 self 首参），用 _MockAgentSvcLite 提供 self 依赖
- mock ts.execute_confirmable 返回 preview result + future + token，future 经
  _add_pending_confirmation 入注册表（让 halt 路径的 cancel_pending_confirmation 能清理）
- ask_user/proceed 路径在 push_tool_confirm 内调 resolve_confirmation 立即 resolve future
  （否则 await confirm_future 卡 30 分钟 CONFIRM_TIMEOUT）
- 用虚构工具名 "dangerous_op"（不在 _suggest_alternative_tool 的 ALTERNATIVES 表），
  避免 halt 失败路径触发备选工具执行
"""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ═══════════════════════════════════════════════════════════════
# Mocks
# ═══════════════════════════════════════════════════════════════

class _MockMemoryTree:
    """Mock MemoryTree 记录 index_from_text 调用"""

    def __init__(self):
        self.calls: list[dict] = []

    async def index_from_text(self, text: str, source: str = "tool_result") -> None:
        self.calls.append({"text": text, "source": source})


class _MockFeedbackLearner:
    """Mock FeedbackLearner 记录 record_feedback 调用"""

    def __init__(self):
        self.calls: list[dict] = []

    async def record_feedback(self, event_type: str, task_id: str, context: str = "", agent_id: str = None) -> None:
        self.calls.append({"event_type": event_type, "task_id": task_id, "context": context, "agent_id": agent_id})


class _MockAgentSvcLite:
    """轻量 mock self——只提供 _execute_exclusive_tools 需要的方法/属性"""

    def __init__(self, db, auto_approve: bool = False):
        self.db = db
        self._auto_approve = auto_approve
        # 生产端 AgentService.__init__ 一直有这份 per-task 预授权表；
        # 长任务的 AI 代审要读它，mock 缺了就会 AttributeError
        self._task_auto_approve: dict[str, bool] = {}
        self.adapters = {}  # 不走 OC 路径
        self._pending_failure_analysis: str | None = None
        self._memory_tree = _MockMemoryTree()
        self._feedback_learner = _MockFeedbackLearner()
        self.pushed_confirms: list[dict] = []
        self.pushed_batch_confirms: list[dict] = []
        self.sent: list[dict] = []

    async def _safe_send(self, task_id: str, payload: dict) -> None:
        self.sent.append({"task_id": task_id, **payload})

    async def _should_auto_approve(self, task_id: str = "") -> bool:
        # 第一批 auto_approve 改动: 生产代码 _should_auto_approve(task_id) 接受 task_id 参数
        # mock 签名需同步，否则 TypeError 被外层 try 捕获导致 result_str 变 {"error": ...}
        return self._auto_approve or bool(self._task_auto_approve.get(task_id))

    async def push_tool_confirm(
        self, task_id: str, tool_name: str, data: dict, confirm_token: str,
        expires_at: float = 0.0,
    ) -> None:
        self.pushed_confirms.append({
            "task_id": task_id, "tool_name": tool_name,
            "data": data, "confirm_token": confirm_token,
        })
        # 模拟用户立即确认——resolve future 让 await 不阻塞
        from services.tools.tool_service import resolve_confirmation
        await resolve_confirmation(confirm_token, {
            "success": True, "output": "executed", "data": {},
        })

    async def push_tool_confirm_batch(
        self, task_id: str, items: list[dict],
    ) -> None:
        self.pushed_batch_confirms.append({
            "task_id": task_id, "items": items, "count": len(items),
        })
        # 模拟用户立即全部确认
        from services.tools.tool_service import resolve_confirmation
        for it in items:
            await resolve_confirmation(it["confirm_token"], {
                "success": True, "output": "executed", "data": {},
            })

    async def _analyze_tool_failure_and_send(self, *args, **kwargs):
        # Phase 85: 修复预存 bug 后方法名对齐真实方法
        return None


class _MockToolService:
    """mock ts——execute_confirmable 返回 preview + future + token"""

    def __init__(self, db):
        self._db = db
        self.execute_calls: list[dict] = []

    async def execute_confirmable(
        self, db, tool_name: str, arguments: dict, agent_id: str | None = None,
        auto_approve: bool = False, task_id: str = "",
    ) -> dict:
        self.execute_calls.append({
            "tool_name": tool_name, "args": arguments, "auto_approve": auto_approve,
        })
        from services.tools.tool_service import _add_pending_confirmation
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        token = f"tok-mock-{len(self.execute_calls)}"
        _add_pending_confirmation(token, future)
        return {
            "success": True,
            "meta": {"phase": "preview"},
            "data": {"path": "/tmp/x"},
            "confirm_token": token,
            "_confirm_future": future,
        }


class _MockFinalReviewAgent:
    """mock FinalReviewAgent——review 返回可控结果"""

    def __init__(self, result: dict | None):
        self._result = result
        self.review_calls: list[tuple] = []

    async def review(self, task_id, stage, context, step_id=None, force=False,
                     approve_on_uncertain=True):
        # 2026-10-08：review() 新增 approve_on_uncertain（AI 代审时没结论要交回用户）
        self.review_calls.append((task_id, stage, context, step_id, force, approve_on_uncertain))
        return self._result


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
async def db(tmp_path):
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_tool_loop.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    yield conn
    await conn.close()


def _make_call(tool_name: str = "dangerous_op", args: dict | None = None) -> list[dict]:
    """构造单个 tool_call 列表"""
    args = args or {"path": "/tmp/x", "content": "hi"}
    return [{
        "id": "call-1",
        "function": {"name": tool_name, "arguments": json.dumps(args)},
    }]


def _inject_agent(mock_agent):
    """注入 mock FinalReviewAgent 单例，返回原始单例"""
    from services.long_task.final_review_agent import (
        get_final_review_agent, set_final_review_agent,
    )
    original = get_final_review_agent()
    set_final_review_agent(mock_agent)
    return original


def _restore_agent(original):
    from services.long_task.final_review_agent import set_final_review_agent
    set_final_review_agent(original)


def _spy_cancel():
    """spy cancel_pending_confirmation——记录调用但不影响真实清理"""
    import services.tools.tool_service as ts_mod
    original = ts_mod.cancel_pending_confirmation
    calls: list[str] = []

    def spy(token: str) -> None:
        calls.append(token)
        return original(token)

    ts_mod.cancel_pending_confirmation = spy
    return calls, original


def _restore_cancel(original):
    import services.tools.tool_service as ts_mod
    ts_mod.cancel_pending_confirmation = original


# ═══════════════════════════════════════════════════════════════
# TestPreDangerousToolIntegration
# ═══════════════════════════════════════════════════════════════

class TestPreDangerousToolIntegration:
    """Phase 83 Gap 3: agent_tool_loop 接入 pre_dangerous_tool 终审"""

    async def test_halt_blocks_execution(self, db):
        """pre_dangerous_tool 返回 halt——cancel pending + 错误 result + 不推弹窗"""
        from services.agent.agent_tool_loop import _execute_exclusive_tools
        svc = _MockAgentSvcLite(db)
        ts = _MockToolService(db)
        halt_review = {
            "pass": False, "score": 20, "action": "halt",
            "issues": ["危险操作"], "reason": "可能破坏系统",
            "stage": "pre_dangerous_tool", "task_id": "t1", "ts": 0,
        }
        mock_agent = _MockFinalReviewAgent(halt_review)
        cancel_calls, orig_cancel = _spy_cancel()
        orig_agent = _inject_agent(mock_agent)
        try:
            results = await _execute_exclusive_tools(
                svc, _make_call(), ts, "hermes", "t1",
                _confirmable_names={"dangerous_op"},
                _openclaw_tool_names=set(),
                model_cfg={}, adapter=None, context={"history": []},
            )
        finally:
            _restore_agent(orig_agent)
            _restore_cancel(orig_cancel)

        # 1. tool_result 是错误（halt 拒绝执行）
        assert len(results) == 1
        content = json.loads(results[0]["content"])
        assert content["success"] is False
        assert "审批未通过" in content["error"]
        assert content["review"]["action"] == "halt"
        # 2. 2026-09-16 新语义：否决发生在创建待确认状态之前——没有 pending 要清，
        #    工具连预览都不执行
        assert cancel_calls == []
        assert ts.execute_calls == []
        # 3. push_tool_confirm 未调用（halt 不打扰用户）
        assert len(svc.pushed_confirms) == 0
        # 4. FinalReviewAgent.review 被调用，stage 正确
        assert len(mock_agent.review_calls) == 1
        assert mock_agent.review_calls[0][1] == "pre_dangerous_tool"

    async def test_ask_user_merges_into_confirm(self, db):
        """pre_dangerous_tool 返回 ask_user——review 合并到弹窗 _review 字段"""
        from services.agent.agent_tool_loop import _execute_exclusive_tools
        svc = _MockAgentSvcLite(db)
        ts = _MockToolService(db)
        ask_review = {
            "pass": False, "score": 50, "action": "ask_user",
            "issues": ["需用户确认"], "reason": "高风险但可能必要",
            "stage": "pre_dangerous_tool", "task_id": "t1", "ts": 0,
        }
        mock_agent = _MockFinalReviewAgent(ask_review)
        orig_agent = _inject_agent(mock_agent)
        try:
            results = await _execute_exclusive_tools(
                svc, _make_call(), ts, "hermes", "t1",
                _confirmable_names={"dangerous_op"},
                _openclaw_tool_names=set(),
                model_cfg={}, adapter=None, context={"history": []},
            )
        finally:
            _restore_agent(orig_agent)

        # 1. push_tool_confirm 被调用
        assert len(svc.pushed_confirms) == 1
        confirm = svc.pushed_confirms[0]
        # 2. review 合并到 data._review
        assert "_review" in confirm["data"]
        assert confirm["data"]["_review"]["action"] == "ask_user"
        assert confirm["data"]["_review"]["reason"] == "高风险但可能必要"
        # 3. 工具正常执行（mock 确认后 success=True）
        assert len(results) == 1
        content = json.loads(results[0]["content"])
        assert content["success"] is True

    async def test_proceed_normal_flow(self, db):
        """pre_dangerous_tool 返回 proceed——正常确认流 + review 仍合并到弹窗"""
        from services.agent.agent_tool_loop import _execute_exclusive_tools
        svc = _MockAgentSvcLite(db)
        ts = _MockToolService(db)
        proceed_review = {
            "pass": True, "score": 85, "action": "proceed",
            "issues": [], "reason": "审查通过",
            "stage": "pre_dangerous_tool", "task_id": "t1", "ts": 0,
        }
        mock_agent = _MockFinalReviewAgent(proceed_review)
        orig_agent = _inject_agent(mock_agent)
        try:
            results = await _execute_exclusive_tools(
                svc, _make_call(), ts, "hermes", "t1",
                _confirmable_names={"dangerous_op"},
                _openclaw_tool_names=set(),
                model_cfg={}, adapter=None, context={"history": []},
            )
        finally:
            _restore_agent(orig_agent)

        # 1. push_tool_confirm 被调用
        assert len(svc.pushed_confirms) == 1
        # 2. review 合并到 data._review（proceed 也合并，供用户参考）
        assert "_review" in svc.pushed_confirms[0]["data"]
        assert svc.pushed_confirms[0]["data"]["_review"]["action"] == "proceed"
        # 3. 工具正常执行
        content = json.loads(results[0]["content"])
        assert content["success"] is True

    async def test_missing_agent_no_block(self, db):
        """FinalReviewAgent 单例缺失——_review_dangerous_tool 返回 None + 正常确认流"""
        from services.agent.agent_tool_loop import _execute_exclusive_tools
        svc = _MockAgentSvcLite(db)
        ts = _MockToolService(db)
        # 注入 None 单例（模拟未初始化）
        orig_agent = _inject_agent(None)
        try:
            results = await _execute_exclusive_tools(
                svc, _make_call(), ts, "hermes", "t1",
                _confirmable_names={"dangerous_op"},
                _openclaw_tool_names=set(),
                model_cfg={}, adapter=None, context={"history": []},
            )
        finally:
            _restore_agent(orig_agent)

        # 1. push_tool_confirm 被调用（正常确认流）
        assert len(svc.pushed_confirms) == 1
        # 2. data 不含 _review（None 不合并）
        assert "_review" not in svc.pushed_confirms[0]["data"]
        # 3. 工具正常执行
        content = json.loads(results[0]["content"])
        assert content["success"] is True

    async def test_review_exception_no_block(self, db):
        """FinalReviewAgent.review 抛异常——_review_dangerous_tool 兜底返回 None + 不阻塞"""
        from services.agent.agent_tool_loop import _execute_exclusive_tools

        class _ExplodingAgent:
            async def review(self, *args, **kwargs):
                raise RuntimeError("LLM 不可用")

        svc = _MockAgentSvcLite(db)
        ts = _MockToolService(db)
        orig_agent = _inject_agent(_ExplodingAgent())
        try:
            results = await _execute_exclusive_tools(
                svc, _make_call(), ts, "hermes", "t1",
                _confirmable_names={"dangerous_op"},
                _openclaw_tool_names=set(),
                model_cfg={}, adapter=None, context={"history": []},
            )
        finally:
            _restore_agent(orig_agent)

        # 异常被 _review_dangerous_tool 兜底 → 返回 None → 正常确认流
        assert len(svc.pushed_confirms) == 1
        assert "_review" not in svc.pushed_confirms[0]["data"]
        content = json.loads(results[0]["content"])
        assert content["success"] is True


# ═══════════════════════════════════════════════════════════════
# Phase D F1.3+F1.4: OC 工具结果入 MemoryTree + 反馈学习
# ═══════════════════════════════════════════════════════════════

class _MockOCAdapter:
    """Mock OpenClaw adapter execute_tool"""

    def __init__(self, result: dict):
        self._result = result
        self.calls: list[tuple] = []

    async def execute_tool(self, tool_name: str, arguments: dict) -> dict:
        self.calls.append((tool_name, arguments))
        return self._result


class TestOcToolResultIndexing:
    """Phase D F1.3+F1.4: OC 工具结果应索引到 MemoryTree 并记录反馈"""

    async def test_oc_success_indexes_memory_and_records_accepted_feedback(self, db):
        """OC 工具成功 → MemoryTree 索引 + human_accepted 反馈"""
        from services.agent.agent_tool_loop import _execute_exclusive_tools

        svc = _MockAgentSvcLite(db)
        svc.adapters = {"openclaw": _MockOCAdapter({"ok": True, "result": "found"})}
        ts = _MockToolService(db)

        results = await _execute_exclusive_tools(
            svc, _make_call(tool_name="web_search", args={"query": "x"}),
            ts, "hermes", "t1",
            _confirmable_names=set(),
            _openclaw_tool_names={"web_search"},
            model_cfg={}, adapter=None, context={"history": []},
        )

        content = json.loads(results[0]["content"])
        assert content.get("ok") is True

        mt = svc._memory_tree
        assert len(mt.calls) == 1
        assert "web_search" in mt.calls[0]["text"]
        assert mt.calls[0]["source"] == "openclaw_tool:web_search"

        fb = svc._feedback_learner
        assert len(fb.calls) == 1
        assert fb.calls[0]["event_type"] == "human_accepted"
        assert fb.calls[0]["context"] == "openclaw_tool:web_search"
        assert fb.calls[0]["agent_id"] == "hermes"

    async def test_oc_failure_indexes_memory_and_records_rejected_feedback(self, db):
        """OC 工具失败 → MemoryTree 仍索引 + human_rejected 反馈"""
        from services.agent.agent_tool_loop import _execute_exclusive_tools

        svc = _MockAgentSvcLite(db)
        svc.adapters = {"openclaw": _MockOCAdapter({"ok": False, "error": "timeout"})}
        ts = _MockToolService(db)

        results = await _execute_exclusive_tools(
            svc, _make_call(tool_name="web_search", args={"query": "x"}),
            ts, "hermes", "t1",
            _confirmable_names=set(),
            _openclaw_tool_names={"web_search"},
            model_cfg={}, adapter=None, context={"history": []},
        )

        content = json.loads(results[0]["content"])
        assert content.get("ok") is False

        assert len(svc._memory_tree.calls) == 1
        assert len(svc._feedback_learner.calls) == 1
        assert svc._feedback_learner.calls[0]["event_type"] == "human_rejected"

    async def test_oc_unavailable_records_failure_feedback(self, db):
        """OC adapter 不可用 → 仍记录 human_rejected 反馈（不阻塞）"""
        from services.agent.agent_tool_loop import _execute_exclusive_tools

        svc = _MockAgentSvcLite(db)
        # adapters 无 openclaw → 走失败分支
        ts = _MockToolService(db)

        results = await _execute_exclusive_tools(
            svc, _make_call(tool_name="web_search", args={"query": "x"}),
            ts, "hermes", "t1",
            _confirmable_names=set(),
            _openclaw_tool_names={"web_search"},
            model_cfg={}, adapter=None, context={"history": []},
        )

        content = json.loads(results[0]["content"])
        assert content.get("success") is False
        assert len(svc._feedback_learner.calls) == 1
        assert svc._feedback_learner.calls[0]["event_type"] == "human_rejected"


# ═══════════════════════════════════════════════════════════════
# Batch 3.2: 多项工具批量确认
# ═══════════════════════════════════════════════════════════════

class TestBatchConfirmIntegration:
    """Batch 3.2: 同一轮多个 confirmable 工具应聚合为批量弹窗。"""

    async def test_single_confirmable_still_single_message(self, db):
        """只有 1 个 confirmable 时仍走原单弹窗路径。"""
        from services.agent.agent_tool_loop import _execute_exclusive_tools
        svc = _MockAgentSvcLite(db)
        ts = _MockToolService(db)
        results = await _execute_exclusive_tools(
            svc, _make_call(), ts, "hermes", "t1",
            _confirmable_names={"dangerous_op"},
            _openclaw_tool_names=set(),
            model_cfg={}, adapter=None, context={"history": []},
        )
        assert len(results) == 1
        assert len(svc.pushed_confirms) == 1
        assert len(svc.pushed_batch_confirms) == 0
        assert json.loads(results[0]["content"])["success"] is True

    async def test_multiple_confirmable_sends_batch_message(self, db):
        """2 个 confirmable 应发送 1 条批量确认消息，结果按顺序返回。"""
        from services.agent.agent_tool_loop import _execute_exclusive_tools
        svc = _MockAgentSvcLite(db)
        ts = _MockToolService(db)
        calls = [
            _make_call(tool_name="dangerous_op", args={"path": "/tmp/a"})[0],
            _make_call(tool_name="dangerous_op", args={"path": "/tmp/b"})[0],
        ]
        calls[0]["id"] = "call-1"
        calls[1]["id"] = "call-2"
        results = await _execute_exclusive_tools(
            svc, calls, ts, "hermes", "t1",
            _confirmable_names={"dangerous_op"},
            _openclaw_tool_names=set(),
            model_cfg={}, adapter=None, context={"history": []},
        )
        assert len(results) == 2
        assert len(svc.pushed_confirms) == 0
        assert len(svc.pushed_batch_confirms) == 1
        batch = svc.pushed_batch_confirms[0]
        assert batch["count"] == 2
        assert len(batch["items"]) == 2
        assert json.loads(results[0]["content"])["success"] is True
        assert json.loads(results[1]["content"])["success"] is True

    async def test_batch_mixed_halt_and_confirm(self, db):
        """1 个 halt + 2 个正常 confirmable：halt 直接失败，其余 2 个进批量弹窗。"""
        from services.agent.agent_tool_loop import _execute_exclusive_tools
        from services.long_task.final_review_agent import (
            get_final_review_agent, set_final_review_agent,
        )

        class _HaltThenPassAgent:
            def __init__(self):
                self._call_count = 0

            async def review(self, task_id, stage, context, step_id=None, force=False,
                             approve_on_uncertain=True):
                self._call_count += 1
                if self._call_count == 1:
                    return {
                        "pass": False, "score": 20, "action": "halt",
                        "issues": ["危险"], "reason": "halt-test",
                        "stage": stage, "task_id": task_id, "ts": 0,
                    }
                return None

        svc = _MockAgentSvcLite(db)
        ts = _MockToolService(db)
        calls = [
            _make_call(tool_name="dangerous_op", args={"path": "/tmp/1"})[0],
            _make_call(tool_name="dangerous_op", args={"path": "/tmp/2"})[0],
            _make_call(tool_name="dangerous_op", args={"path": "/tmp/3"})[0],
        ]
        for i, c in enumerate(calls, start=1):
            c["id"] = f"call-{i}"
        mock_agent = _HaltThenPassAgent()
        orig_agent = get_final_review_agent()
        set_final_review_agent(mock_agent)
        try:
            results = await _execute_exclusive_tools(
                svc, calls, ts, "hermes", "t1",
                _confirmable_names={"dangerous_op"},
                _openclaw_tool_names=set(),
                model_cfg={}, adapter=None, context={"history": []},
            )
        finally:
            set_final_review_agent(orig_agent)

        assert len(results) == 3
        assert json.loads(results[0]["content"])["success"] is False
        assert "审批未通过" in json.loads(results[0]["content"])["error"]
        assert len(svc.pushed_batch_confirms) == 1
        assert svc.pushed_batch_confirms[0]["count"] == 2
        assert json.loads(results[1]["content"])["success"] is True
        assert json.loads(results[2]["content"])["success"] is True


# ═══════════════════════════════════════════════════════════════
# Batch 3.7: 超时返回结构化提示
# ═══════════════════════════════════════════════════════════════

class _NonResolvingMockAgentSvcLite(_MockAgentSvcLite):
    """模拟用户不确认——push 后不解 future，让 wait_for 超时。"""

    async def push_tool_confirm(
        self, task_id: str, tool_name: str, data: dict, confirm_token: str,
        expires_at: float = 0.0,
    ) -> None:
        self.pushed_confirms.append({
            "task_id": task_id, "tool_name": tool_name,
            "data": data, "confirm_token": confirm_token,
        })
        # 故意不解 future → 触发 CONFIRM_TIMEOUT 超时

    async def push_tool_confirm_batch(
        self, task_id: str, items: list[dict],
    ) -> None:
        self.pushed_batch_confirms.append({
            "task_id": task_id, "items": items, "count": len(items),
        })
        # 故意不解 future → 触发 CONFIRM_TIMEOUT 超时


class TestConfirmTimeoutStructured:
    """Batch 3.7: 用户未在超时窗口内确认，返回包含 reason/recommendation 的结构。"""

    async def test_single_confirmable_timeout_structured(self, db, monkeypatch):
        """单个 confirmable 超时：结果含 reason='timeout' 与 recommendation。"""
        from services.agent import agent_tool_loop as loop_mod
        from services.agent import confirmable as confirmable_mod
        monkeypatch.setattr(loop_mod, "CONFIRM_TIMEOUT", 0.01)
        monkeypatch.setattr(confirmable_mod, "CONFIRM_TIMEOUT", 0.01)

        from services.agent.agent_tool_loop import _execute_exclusive_tools
        svc = _NonResolvingMockAgentSvcLite(db)
        ts = _MockToolService(db)
        results = await _execute_exclusive_tools(
            svc, _make_call(), ts, "hermes", "t1",
            _confirmable_names={"dangerous_op"},
            _openclaw_tool_names=set(),
            model_cfg={}, adapter=None, context={"history": []},
        )
        assert len(results) == 1
        content = json.loads(results[0]["content"])
        assert content["success"] is False
        assert content["action"] == "timeout"
        assert content["reason"] == "timeout"
        assert "recommendation" in content
        assert "重新运行" in content["recommendation"]
        assert content.get("meta", {}).get("tool_name") == "dangerous_op"

    async def test_batch_confirmable_timeout_structured(self, db, monkeypatch):
        """批量 confirmable 全部超时：每条结果都含 reason/recommendation。"""
        from services.agent import agent_tool_loop as loop_mod
        from services.agent import confirmable as confirmable_mod
        monkeypatch.setattr(loop_mod, "CONFIRM_TIMEOUT", 0.01)
        monkeypatch.setattr(confirmable_mod, "CONFIRM_TIMEOUT", 0.01)

        from services.agent.agent_tool_loop import _execute_exclusive_tools
        svc = _NonResolvingMockAgentSvcLite(db)
        ts = _MockToolService(db)
        calls = [
            _make_call(tool_name="dangerous_op", args={"path": "/tmp/a"})[0],
            _make_call(tool_name="dangerous_op", args={"path": "/tmp/b"})[0],
        ]
        calls[0]["id"] = "call-1"
        calls[1]["id"] = "call-2"
        results = await _execute_exclusive_tools(
            svc, calls, ts, "hermes", "t1",
            _confirmable_names={"dangerous_op"},
            _openclaw_tool_names=set(),
            model_cfg={}, adapter=None, context={"history": []},
        )
        assert len(results) == 2
        assert len(svc.pushed_batch_confirms) == 1
        for res in results:
            content = json.loads(res["content"])
            assert content["success"] is False
            assert content["reason"] == "timeout"
            assert "recommendation" in content
            assert content.get("meta", {}).get("tool_name") == "dangerous_op"



class TestAiApprovalMode:
    """审批方式=ai：终审结论直接决定执行/拒绝，并把结论推给前端（2026-09-16）"""

    async def _set_mode(self, db, mode: str) -> None:
        await db.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES('approval_mode', ?)", (mode,),
        )
        await db.commit()

    async def _collect(self, db, ts, svc, calls):
        from services.agent.confirmable import _collect_confirmable_items

        pairs = [(idx, tc) for idx, tc in enumerate(calls)]
        return await _collect_confirmable_items(svc, pairs, ts, "hermes", "t1", {})

    def _agent(self, action: str, reason: str = "只改工作区内文件"):
        return _MockFinalReviewAgent({
            "pass": action == "proceed", "score": 90, "action": action, "reason": reason,
            "stage": "pre_dangerous_tool", "task_id": "t1", "ts": 0,
        })

    async def test_proceed_executes_and_reports(self, db):
        svc, ts = _MockAgentSvcLite(db), _MockToolService(db)
        await self._set_mode(db, "ai")
        orig = _inject_agent(self._agent("proceed"))
        try:
            items = await self._collect(db, ts, svc, _make_call())
        finally:
            _restore_agent(orig)

        assert items[0]["ai_approved"] is True
        assert items[0]["pending"] is False
        assert ts.execute_calls[0]["auto_approve"] is True
        events = [m for m in svc.sent if m.get("type") == "approval_review"]
        assert len(events) == 1 and events[0]["decision"] == "approve"
        assert events[0]["tool_name"] == "dangerous_op"

    async def test_halt_denies_without_pending_state(self, db):
        svc, ts = _MockAgentSvcLite(db), _MockToolService(db)
        await self._set_mode(db, "ai")
        orig = _inject_agent(self._agent("halt", reason="会删库"))
        try:
            items = await self._collect(db, ts, svc, _make_call())
        finally:
            _restore_agent(orig)

        assert items[0]["halted"] is True
        assert items[0]["pending"] is False
        assert ts.execute_calls == [], "已否决就不能再去执行预览"
        assert "会删库" in items[0]["result"]["error"]
        events = [m for m in svc.sent if m.get("type") == "approval_review"]
        assert events[0]["decision"] == "deny"

    async def test_uncertain_falls_back_to_the_user(self, db):
        """审查不出结论 → 回到弹窗，不能当默认放行"""
        svc, ts = _MockAgentSvcLite(db), _MockToolService(db)
        await self._set_mode(db, "ai")
        orig = _inject_agent(self._agent("ask_user", reason="看不出"))
        try:
            items = await self._collect(db, ts, svc, _make_call())
        finally:
            _restore_agent(orig)

        assert items[0]["pending"] is True
        assert items[0]["halted"] is False
        assert ts.execute_calls[0]["auto_approve"] is False

    async def test_ask_mode_reviewer_cannot_approve_for_the_user(self, db):
        """默认档：终审只有否决权，不能替用户点头"""
        svc, ts = _MockAgentSvcLite(db), _MockToolService(db)
        orig = _inject_agent(self._agent("proceed"))
        try:
            items = await self._collect(db, ts, svc, _make_call())
        finally:
            _restore_agent(orig)

        assert items[0]["pending"] is True
        assert ts.execute_calls[0]["auto_approve"] is False
        assert not [m for m in svc.sent if m.get("type") == "approval_review"]

    async def test_ai_mode_reviews_every_call(self, db):
        """AI 档位下结论就是决策：两次调用要两次审查（不能吃冷却缓存）"""
        svc, ts = _MockAgentSvcLite(db), _MockToolService(db)
        await self._set_mode(db, "ai")
        agent = self._agent("proceed")
        orig = _inject_agent(agent)
        try:
            calls = _make_call() + _make_call(args={"path": "/tmp/y", "content": "hi"})
            await self._collect(db, ts, svc, calls)
        finally:
            _restore_agent(orig)

        assert len(agent.review_calls) == 2
        assert all(call[4] is True for call in agent.review_calls), "审批决策必须 force=True"
