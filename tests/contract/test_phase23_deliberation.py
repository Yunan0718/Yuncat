"""tests/contract/test_phase23_deliberation.py —— Phase 23 讨论引擎优化合约测试"""
import os
import sys
import pytest


# ---- 结构化解析 ----

def test_parse_structured_proposals():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.deliberation.deliberation_service import parse_structured_output
    text = "[提案] 使用 JWT 替代 session\n[发现] 存在 SQL 注入风险\n[风险] 迁移可能影响现有用户"
    r = parse_structured_output(text)
    assert len(r["proposal"]) == 1
    assert len(r["finding"]) == 1
    assert len(r["risk"]) == 1
    assert "JWT" in r["proposal"][0]


def test_parse_structured_empty():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.deliberation.deliberation_service import parse_structured_output
    r = parse_structured_output("")
    assert r["proposal"] == []
    assert r["finding"] == []
    assert r["risk"] == []


def test_parse_structured_multiple():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.deliberation.deliberation_service import parse_structured_output
    text = "[提案] 方案A\n一些解释文本\n[提案] 方案B\n更多内容\n[发现] 问题C"
    r = parse_structured_output(text)
    assert len(r["proposal"]) == 2
    assert len(r["finding"]) == 1


# ---- 分歧统计 ----

def test_count_divergence_basic():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.deliberation.deliberation_service import count_divergence
    proposals = [{"role_name": "a", "output": "test"}]
    reviews = [{"review": "我不同意安全专家的观点，这个方案有风险"}]
    assert count_divergence(proposals, reviews) >= 1


def test_count_divergence_none():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.deliberation.deliberation_service import count_divergence
    reviews = [{"review": "完全同意，方案很好"}]
    assert count_divergence([], reviews) == 0


def test_count_divergence_multiple():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.deliberation.deliberation_service import count_divergence
    reviews = [
        {"review": "我不同意第一点。反对第二项。方案不正确，需重新考虑"},
    ]
    assert count_divergence([], reviews) >= 2


# ---- Hook 系统扩展 ----

def test_hook_type_has_stop():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from hooks.registry import HookType
    assert HookType.STOP.value == "stop"
    assert HookType.SESSION_START.value == "session_start"
    assert HookType.SESSION_END.value == "session_end"


@pytest.mark.asyncio
async def test_run_stop_hooks_no_hooks():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from hooks.registry import run_stop_hooks
    result = await run_stop_hooks("test-session", "")
    assert result["continue"] is True
    assert result["reactivate"] is False


@pytest.mark.asyncio
async def test_run_stop_hooks_max_reactivations():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from hooks.registry import run_stop_hooks, _MAX_REACTIVATIONS, _reactivation_counts
    sid = "test-max-reactivations"
    _reactivation_counts[sid] = _MAX_REACTIVATIONS
    result = await run_stop_hooks(sid, "")
    assert result["continue"] is True
    assert result["reactivate"] is False
    del _reactivation_counts[sid]


def test_stop_hook_is_registered():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from hooks.registry import _hooks, HookType
    assert HookType.STOP in _hooks
    assert len(_hooks[HookType.STOP]) >= 1


def test_stop_hook_script_exists():
    script = os.path.join(
        os.path.dirname(__file__), "..", "..", ".yuncat", "hooks", "stop-check-tests.sh",
    )
    assert os.path.exists(script), "Stop hook example script missing"


# ---- @import 语法 ----

def test_resolve_imports_no_at_sign():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from prompts.commander import _resolve_imports
    result = _resolve_imports("普通文本 without @import")
    assert result == "普通文本 without @import"


def test_resolve_imports_invalid_path():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from prompts.commander import _resolve_imports
    result = _resolve_imports("参考 @nonexistent/file.md 的内容")
    assert "@import failed" in result or "nonexistent" in result


# ---- #记住 快捷指令 ----

def test_main_has_remember_command():
    ws_code = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "routes", "ws.py"),
        encoding="utf-8",
    ).read()
    assert "#记住" in ws_code, "routes/ws.py missing #记住 quick command"
    assert "insert_memory" in ws_code


# ---- DeliberationService 回归 ----

def test_assess_complexity_unchanged():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.deliberation.deliberation_service import DeliberationService
    ds = DeliberationService()
    r = ds.assess_complexity("修复一个简单的bug")
    assert "level" in r
    assert "effort" in r
    assert r["level"] == "low"


def test_complexity_signals_still_work():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.deliberation.deliberation_service import DeliberationService
    ds = DeliberationService()
    r = ds.assess_complexity("帮我设计一个微服务架构并且做分布式安全审计")
    assert r["level"] in ("high", "extreme")
    assert r["effort"] in ("deep", "ultracode")


# ---- Adversarial 集成已存在 ----

def test_run_deliberation_has_adversarial_stage():
    d_code = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "services", "dynamic_orchestrator.py"),
        encoding="utf-8",
    ).read()
    assert "AdversarialReviewer" in d_code
    assert "ADVERSARIAL_PROMPT" in d_code


# ---- 结构化输出标签在 ROLE_SYSTEM_TEMPLATE ----

def test_role_template_has_structured_tags():
    d_code = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "services", "deliberation", "deliberation_discussion.py"),
        encoding="utf-8",
    ).read()
    assert "[提案]" in d_code, "ROLE_SYSTEM_TEMPLATE should require [提案] tags"
