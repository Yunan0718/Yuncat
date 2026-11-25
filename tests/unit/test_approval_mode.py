"""tests/unit/test_approval_mode.py —— 审批方式与终审结论（security/approval.py）

守的是行为：三个档怎么读写、旧 auto_approve 怎么兼容、
终审结论如何归一——尤其是“不确定不等于放行”。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from security.approval import (  # noqa: E402
    APPROVAL_AI,
    APPROVAL_ASK,
    APPROVAL_OFF,
    MODES,
    describe_mode,
    mode_from_settings,
    normalize_mode,
    resolve_approval_mode,
    review_outcome,
    set_approval_mode,
)


class TestNormalizeMode:
    def test_known_modes_pass_through(self):
        for mode in MODES:
            assert normalize_mode(mode) == mode

    def test_case_and_space_tolerated(self):
        assert normalize_mode("  AI ") == APPROVAL_AI

    @pytest.mark.parametrize("raw", [None, "", "bogus", "true", 1])
    def test_unknown_falls_back_to_ask(self, raw):
        """未知值不能变成放行——fail-closed 到“问我”"""
        assert normalize_mode(raw) == APPROVAL_ASK


class TestModeFromSettings:
    def test_explicit_mode_wins(self):
        assert mode_from_settings({"approval_mode": "ai", "auto_approve": "true"}) == APPROVAL_AI

    def test_legacy_auto_approve_maps_to_off(self):
        assert mode_from_settings({"auto_approve": "true"}) == APPROVAL_OFF

    def test_default_is_ask(self):
        assert mode_from_settings({}) == APPROVAL_ASK
        assert mode_from_settings(None) == APPROVAL_ASK


class TestReviewOutcome:
    def test_proceed_is_approve(self):
        assert review_outcome({"action": "proceed", "pass": True}) == "approve"

    def test_halt_is_deny(self):
        assert review_outcome({"action": "halt", "pass": False}) == "deny"

    @pytest.mark.parametrize("review", [
        None, {}, {"action": "ask_user"}, {"action": "retry"}, {"action": "proceed", "pass": False},
    ])
    def test_everything_else_is_uncertain(self, review):
        """审查不到就当“拿不到结论”——交给用户，不能当默认放行"""
        assert review_outcome(review) == "uncertain"


class TestDescribeMode:
    def test_flags(self):
        assert describe_mode(APPROVAL_AI) == {
            "mode": "ai", "ask_user": False, "ai_review": True, "auto_execute": False}
        assert describe_mode(APPROVAL_OFF)["auto_execute"] is True
        assert describe_mode(APPROVAL_ASK)["ask_user"] is True


class _FakeSettings:
    def __init__(self):
        self.saved: dict[str, str] = {}

    async def save(self, key: str, value: str) -> None:
        self.saved[key] = value


class TestSetApprovalMode:
    @pytest.mark.asyncio
    async def test_writes_mode_and_legacy_alias(self, monkeypatch):
        monkeypatch.delenv("YUNCAT_APPROVAL_MODE", raising=False)
        service = _FakeSettings()

        result = await set_approval_mode(service, APPROVAL_OFF)

        assert result["mode"] == APPROVAL_OFF
        assert service.saved == {"approval_mode": "off", "auto_approve": "true"}
        assert os.environ["YUNCAT_APPROVAL_MODE"] == APPROVAL_OFF

    @pytest.mark.asyncio
    async def test_ai_mode_clears_legacy_flag(self, monkeypatch):
        monkeypatch.delenv("YUNCAT_APPROVAL_MODE", raising=False)
        service = _FakeSettings()

        await set_approval_mode(service, APPROVAL_AI)

        assert service.saved == {"approval_mode": "ai", "auto_approve": "false"}

    @pytest.mark.asyncio
    async def test_unknown_mode_rejected(self):
        service = _FakeSettings()
        with pytest.raises(ValueError):
            await set_approval_mode(service, "yolo")
        assert service.saved == {}


class TestResolveFromDatabase:
    @pytest.mark.asyncio
    async def test_reads_mode_then_legacy(self, tmp_path):
        import aiosqlite

        conn = await aiosqlite.connect(os.path.join(str(tmp_path), "s.db"))
        await conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        try:
            assert await resolve_approval_mode(conn) == APPROVAL_ASK

            await conn.execute("INSERT INTO settings(key, value) VALUES('auto_approve','true')")
            await conn.commit()
            assert await resolve_approval_mode(conn) == APPROVAL_OFF

            await conn.execute("INSERT INTO settings(key, value) VALUES('approval_mode','ai')")
            await conn.commit()
            assert await resolve_approval_mode(conn) == APPROVAL_AI
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_unreadable_table_falls_back_to_ask(self, tmp_path):
        import aiosqlite

        conn = await aiosqlite.connect(os.path.join(str(tmp_path), "empty.db"))
        try:
            assert await resolve_approval_mode(conn) == APPROVAL_ASK
        finally:
            await conn.close()


class TestApplyReviewOutcome:
    """confirmable.py 的纯函数：结论怎么落到这一项上"""

    def _item(self, with_future: bool = True, auto_approved: bool = False):
        return {
            "confirm_future": object() if with_future else None,
            "result": {"success": True, "data": {"path": "a.txt"}, "_auto_approved": auto_approved},
            "halted": False, "pending": False,
        }

    def test_deny_replaces_result_and_halts(self):
        from services.agent.confirmable import _apply_review_outcome

        item = self._item()
        _apply_review_outcome(item, "deny", {"action": "halt", "reason": "会删库"})

        assert item["halted"] is True
        assert item["pending"] is False
        assert item["result"]["success"] is False
        assert "会删库" in item["result"]["error"]
        assert item["result"]["review"]["action"] == "halt"

    def test_approve_marks_item_and_keeps_dialog_closed(self):
        from services.agent.confirmable import _apply_review_outcome

        item = self._item(auto_approved=True)
        _apply_review_outcome(item, "approve", {"action": "proceed"})

        assert item["ai_approved"] is True
        assert item["pending"] is False

    def test_uncertain_asks_the_user_and_attaches_review(self):
        from services.agent.confirmable import _apply_review_outcome

        item = self._item()
        review = {"action": "ask_user", "reason": "看不出"}
        _apply_review_outcome(item, "uncertain", review)

        assert item["pending"] is True
        assert item["preview"]["_review"] == review
