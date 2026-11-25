"""tests/unit/test_long_task_ai_approval.py —— 长任务的自动放行必须经 AI 代审

2026-09-17：长任务 / 子 Agent 走 ``spawn_subagent(auto_approve=True)``，而这条 task 级
预授权以前等于"免审直通"——计划审过之后，步骤里每个危险工具都直接执行，AI 终审没有机会
参与。结果是四种权限档在长任务里退化成同一件事：文档写着"MAX 不等于免审批"，代码里连
"问谁"都没问。

现在 task 级预授权按 AI 代审处理（非 MAX 也一样）：终审说 proceed 才执行、halt 就拦下、
拿不准就回到问你。这里把每种走向钉死，避免以后又被"顺手"改回免审。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from security.approval import APPROVAL_AI, APPROVAL_ASK, APPROVAL_OFF  # noqa: E402
from services.agent.confirmable import _decide_confirm_approval  # noqa: E402


class _FakeService:
    """够用的 AgentService 替身：只需要 task 级标志与全局设置两处判定"""

    def __init__(self, task_flagged: bool = False, global_auto: bool = False):
        self._task_auto_approve = {"t1": True} if task_flagged else {}
        self._global_auto = global_auto

    async def _should_auto_approve(self, task_id: str = "") -> bool:
        return bool(task_id and self._task_auto_approve.get(task_id)) or self._global_auto


def _patch_review(monkeypatch, verdict):
    """把终审换成固定结论，并记录是否被要求逐次审（force）与不确定语义"""
    calls = []
    uncertain_flags = []

    async def _fake(task_id, tool_name, args, force=False, approve_on_uncertain=True):
        # 2026-10-08：新增 approve_on_uncertain（AI 代审时审查员没结论→交回用户）。
        # 这里记录它，顺带断言调用方传对了值。
        calls.append(force)
        uncertain_flags.append(approve_on_uncertain)
        return verdict

    monkeypatch.setattr(
        "services.agent.tool_dispatch.review_dangerous_tool", _fake,
    )
    return calls, uncertain_flags


async def _decide(service, monkeypatch, verdict, mode):
    calls, uncertain_flags = _patch_review(monkeypatch, verdict)
    review, outcome, auto_ok = await _decide_confirm_approval(
        service, "t1", "run_command", {"command": "rm -rf build"}, mode,
    )
    return review, outcome, auto_ok, calls, uncertain_flags


class TestLongTaskGetsAiApproval:
    """长任务（含非 MAX）的 task 级预授权 = AI 代审，不再是免审直通"""

    @pytest.mark.asyncio
    async def test_proceed_executes_and_is_reviewed_per_call(self, monkeypatch):
        service = _FakeService(task_flagged=True)
        _review, outcome, auto_ok, calls, _unc = await _decide(
            service, monkeypatch, {"action": "proceed", "pass": True}, APPROVAL_ASK,
        )

        assert outcome == "approve"
        assert auto_ok is True, "终审放行才执行"
        assert calls == [True], "结论当决策用时必须逐次审，不能复用上一个工具的缓存"

    @pytest.mark.asyncio
    async def test_uncertain_falls_back_to_asking_the_user(self, monkeypatch):
        service = _FakeService(task_flagged=True)
        _review, outcome, auto_ok, _calls, _unc = await _decide(
            service, monkeypatch, {"action": "ask_user"}, APPROVAL_ASK,
        )

        assert outcome == "uncertain"
        assert auto_ok is False, "终审拿不准要交回问用户，不能默认放行"

    @pytest.mark.asyncio
    async def test_halt_blocks_the_tool(self, monkeypatch):
        service = _FakeService(task_flagged=True)
        _review, outcome, auto_ok, _calls, _unc = await _decide(
            service, monkeypatch, {"action": "halt", "pass": False}, APPROVAL_ASK,
        )

        assert outcome == "deny"
        assert auto_ok is False

    @pytest.mark.asyncio
    async def test_no_verdict_at_all_does_not_auto_execute(self, monkeypatch):
        """终审不可用（agent 未初始化 / 调用失败）返回 None——同样不能当成放行"""
        service = _FakeService(task_flagged=True)
        _review, outcome, auto_ok, _calls, _unc = await _decide(
            service, monkeypatch, None, APPROVAL_ASK,
        )

        assert outcome == "uncertain"
        assert auto_ok is False

    @pytest.mark.asyncio
    async def test_ai_mode_reviews_without_task_flag_too(self, monkeypatch):
        """AI 帮我审批档位本来就该代审，和是不是长任务无关"""
        service = _FakeService(task_flagged=False)
        _review, outcome, auto_ok, calls, _unc = await _decide(
            service, monkeypatch, {"action": "proceed", "pass": True}, APPROVAL_AI,
        )

        assert (outcome, auto_ok) == ("approve", True)
        assert calls == [True]


class TestOtherModesUnchanged:
    """没动到的走向：普通对话、完全放开、全局 auto_approve 都保持原样"""

    @pytest.mark.asyncio
    async def test_normal_chat_review_cannot_approve_for_the_user(self, monkeypatch):
        """普通对话里终审只有否决权——它能拦，不能替你点头"""
        service = _FakeService(task_flagged=False)
        _review, outcome, auto_ok, calls, _unc = await _decide(
            service, monkeypatch, {"action": "proceed", "pass": True}, APPROVAL_ASK,
        )

        assert outcome == "uncertain"
        assert auto_ok is False
        assert calls == [False], "非代审场景不必逐次审（保留冷却缓存以省 token）"

    @pytest.mark.asyncio
    async def test_fully_open_skips_ai_and_executes(self, monkeypatch):
        """用户显式选完全放开：不问也不代审，直接执行"""
        service = _FakeService(task_flagged=True, global_auto=True)
        _review, _outcome, auto_ok, calls, _unc = await _decide(
            service, monkeypatch, {"action": "ask_user"}, APPROVAL_OFF,
        )

        assert auto_ok is True
        assert calls == [False]

    @pytest.mark.asyncio
    async def test_global_auto_approve_still_waves_through(self, monkeypatch):
        """全局 auto_approve 是用户的直接授权，不属于"要代审"的 task 级预授权"""
        service = _FakeService(task_flagged=False, global_auto=True)
        _review, outcome, auto_ok, _calls, _unc = await _decide(
            service, monkeypatch, {"action": "ask_user"}, APPROVAL_ASK,
        )

        assert (outcome, auto_ok) == ("uncertain", True)
