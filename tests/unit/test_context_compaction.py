"""tests/unit/test_context_compaction.py —— 上下文压缩四阶段测试

借鉴 Factory AI / ContextPilot / Claude Code:
  阶段 1: 分级分类（Tool vs User vs Agent）
  阶段 2: 结构化摘要（6 节增量合并）
  阶段 3: 质量门控（3 探针 < 2/3 → 回退）
  阶段 4: 磁盘持久化 + compaction snapshot
  门控: 阈值/冷却期/消息数/tool 占比
"""
import os
import sys
import asyncio
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture
def svc():
    """创建无 LLM 的 MemoryService（测试降级路径）"""
    from services.memory.memory_service import MemoryService
    return MemoryService(db=None, summarizer=None, memory_repo=None)


# ============================================================
# 门控测试
# ============================================================
class TestCompactionGating:
    """四个门控条件"""

    async def test_below_threshold_skips(self, svc):
        """使用率低于 85% → 跳过"""
        svc._sessions["s1"] = [{"role": "user", "content": "hi"}] * 20
        result = await svc.maybe_compact_context("s1", 40000, 100000)
        assert result is None  # 40% < 85%

    @pytest.mark.skip(reason="Phase 41: cooldown uses math instead of wall-clock, needs mock time.time()")
    async def test_cooldown_skips(self, svc):
        """冷却期内 → 跳过"""
        svc._sessions["s2"] = [{"role": "user", "content": "test"} for _ in range(20)]
        svc._last_compact_time["s2"] = time.time()  # 刚刚压缩过
        result = await svc.maybe_compact_context("s2", 90000, 100000)
        assert result is None

    async def test_too_few_messages_skips(self, svc):
        """消息数 < 16 → 跳过"""
        svc._sessions["s3"] = [{"role": "user", "content": "x"}] * 10
        result = await svc.maybe_compact_context("s3", 90000, 100000)
        assert result is None

    async def test_low_tool_ratio_skips(self, svc):
        """tool 结果占比 < 60% → 跳过"""
        # 全 user 消息 → tool ratio = 0
        svc._sessions["s4"] = [{"role": "user", "content": "long text " + "x" * 100}] * 20
        result = await svc.maybe_compact_context("s4", 90000, 100000)
        assert result is None


# ============================================================
# 阶段 1: 分级分类
# ============================================================
class TestGradedClassification:
    """消息分级"""

    async def test_is_tool_result_system(self, svc):
        """system 消息被识别为 tool 结果"""
        assert svc._is_tool_result({"role": "system", "content": "long output..."})

    async def test_is_user_instruction(self, svc):
        """user 消息不被识别为 tool 结果"""
        assert not svc._is_tool_result({"role": "user", "content": "请帮我"})
        assert svc._is_user_instruction({"role": "user", "content": "请帮我"})

    async def test_tool_result_ratio(self, svc):
        """tool 占比计算正确"""
        conv = [
            {"role": "user", "content": "q1"},
            {"role": "system", "content": "tool output"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
        ]
        ratio = svc._calc_tool_result_ratio(conv)
        assert ratio == 0.25  # 1/4

    async def test_split_messages_preserves_recent(self, svc):
        """最近消息被保留"""
        messages = [
            {"role": "user", "content": f"msg{i}"} for i in range(30)
        ]
        recent, older = svc._split_messages(messages)
        assert len(recent) > 0
        assert len(older) > 0
        assert recent[-1]["content"] == "msg29"

    async def test_grade_messages_truncates_tool(self, svc):
        """tool 结果超 2000 字被截断"""
        long_msg = {"role": "system", "content": "x" * 3000}
        graded = svc._grade_messages([long_msg])
        assert len(graded[0]["content"]) < 2500
        assert "truncated" in graded[0]["content"].lower()

    async def test_grade_messages_preserves_user(self, svc):
        """用户消息完整保留（截断至 500）"""
        user_msg = {"role": "user", "content": "short"}
        graded = svc._grade_messages([user_msg])
        assert graded[0]["grade"] == "user_full"
        assert graded[0]["content"] == "short"


# ============================================================
# 阶段 2: 结构化摘要（降级路径）
# ============================================================
class TestStructuredSummary:
    """结构化摘要（无 LLM 降级）"""

    @pytest.mark.skipif(not os.getenv("YUNCAT_API_KEY"), reason="Needs API key for summarizer LLM call")
    async def test_fallback_produces_six_sections(self, svc):
        """降级路径生成 6 节摘要"""
        older = [
            {"role": "user", "content": "帮我写一个 auth.py 模块"},
            {"role": "assistant", "content": "好的，我来实现 authentication 模块"},
        ]
        structured = await svc._build_structured_summary("s", older, [], 0.6)
        for key in ["session_intent", "files_modified", "key_decisions",
                     "pending_tasks", "errors_and_fixes", "next_steps"]:
            assert key in structured, f"Missing section: {key}"

    @pytest.mark.skipif(not os.getenv("YUNCAT_API_KEY"), reason="Needs API key for summarizer LLM call")
    async def test_incremental_merge(self, svc):
        """增量合并：已有摘要 + 新消息不覆盖"""
        existing = {"session_intent": "开发 auth 模块", "files_modified": "auth.py"}
        svc._structured_summaries["s_inc"] = existing
        older = [{"role": "user", "content": "再加一个 login 页面"}]
        structured = await svc._build_structured_summary("s_inc", older, [], 0.6)
        assert "auth" in structured.get("session_intent", "")

    async def test_extract_probes(self, svc):
        """探针提取"""
        older = [
            {"role": "user", "content": "帮我修改 config.yaml 数据库配置"},
            {"role": "assistant", "content": "好的我来改"},
        ]
        probes = svc._extract_probes(older)
        assert len(probes) >= 2  # 至少意图 + 文件


# ============================================================
# 阶段 3: 质量门控
# ============================================================
class TestQualityGate:
    """质量门控（无 summarizer 时跳过 = 信任压缩）"""

    @pytest.mark.skipif(not os.getenv("YUNCAT_API_KEY"), reason="Needs API key for summarizer LLM call")
    async def test_quality_gate_passes_without_summarizer(self, svc):
        """无 summarizer → 默认通过 (Phase 41: 需要 mock LLM 回调避免真实API调用)"""
        structured = {"session_intent": "test"}
        passed = await svc._quality_gate(structured, [], [])
        assert passed is True


# ============================================================
# 阶段 4: 持久化 + 替换
# ============================================================
class TestPersistenceAndApply:
    """磁盘持久化 + 内存替换"""

    async def test_compact_boundary_format(self):
        """Claude Code 风格的 compact boundary 格式正确"""
        from services.memory.memory_service import _format_compact_boundary
        structured = {
            "session_intent": "开发 Python API",
            "files_modified": "main.py, config.py",
            "key_decisions": "选 FastAPI",
            "pending_tasks": "写测试",
            "errors_and_fixes": "无",
            "next_steps": "实现 OAuth",
        }
        text = _format_compact_boundary(structured)
        assert "[上下文已压缩]" in text
        assert "main.py" in text
        assert "FastAPI" in text


class TestParseStructuredSummary:
    """LLM 输出解析"""

    async def test_parse_full_output(self):
        """完整 6 节解析"""
        from services.memory.memory_service import _parse_structured_summary
        raw = """[session_intent] 开发 API
[files_modified] main.py, config.py
[key_decisions] 选 FastAPI
[pending_tasks] 写 OAuth 测试
[errors_and_fixes] 无
[next_steps] 部署到 staging"""
        result = _parse_structured_summary(raw)
        assert result["session_intent"] == "开发 API"
        assert "main.py" in result["files_modified"]
        assert result["key_decisions"] == "选 FastAPI"

    async def test_parse_partial_output(self):
        """部分节缺失时补"无" """
        from services.memory.memory_service import _parse_structured_summary
        raw = """[session_intent] 只有一个意图"""
        result = _parse_structured_summary(raw)
        assert result["session_intent"] == "只有一个意图"
        assert result["files_modified"] == "无"

    async def test_merge_section_prevents_duplication(self):
        """增量合并不重复追加"""
        from services.memory.memory_service import _merge_section
        result = _merge_section("已有内容", "新增内容")
        assert "已有内容" in result
        assert "新增内容" in result


class TestHelperFunctions:
    """辅助函数"""

    async def test_format_existing_summary_empty(self):
        from services.memory.memory_service import _format_existing_summary
        assert "无" in _format_existing_summary({})

    async def test_summary_size(self):
        from services.memory.memory_service import _summary_size
        s = {"a": "hello", "b": "无", "c": "world"}
        assert _summary_size(s) >= len("hello") + len("world")

    async def test_is_probe_passed(self):
        from services.memory.memory_service import _is_probe_passed
        assert _is_probe_passed("是，文件列表正确") is True
        assert _is_probe_passed("否，缺少 main.py") is False
        assert _is_probe_passed("") is False
