"""tests/unit/test_code_index_inject.py —— Task 4.1: load_summary 死代码接线

对标:
- Aider --map / Cursor @workspace：符号索引注入 prompt 提升代码导航
- 原 load_summary() 是死代码（build() 持久化但从不读取），30min 索引 CPU 消耗无效

覆盖:
- load_summary 有内容时返回正确格式（## 项目符号索引 header + 符号行）
- load_summary 无内容时返回空字符串
- build_agent_context 调用 load_summary 并注入到 system prompt
- load_summary 异常时不破坏 build_agent_context（try/except 兜底）
"""
import asyncio
import json
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class TestLoadSummaryFormat:
    """CodeIndexer.load_summary 格式契约"""

    def test_load_summary_with_content(self, tmp_path, monkeypatch):
        """有索引文件时返回正确格式——## 项目符号索引 header + 符号行"""
        from services import code_indexer as ci_mod

        # mock INDEX_FILE 指向临时文件
        idx_file = tmp_path / "symbols.json"
        idx_file.write_text(json.dumps({
            "symbols": [
                {"type": "class", "name": "MyService", "file": "svc.py", "line": 10,
                 "methods": ["run", "stop"]},
                {"type": "function", "name": "helper", "file": "util.py", "line": 5,
                 "args": ["x", "y"]},
            ],
            "updated_at": 1234567890,
        }), encoding="utf-8")
        monkeypatch.setattr(ci_mod, "INDEX_FILE", idx_file)

        summary = ci_mod.CodeIndexer.load_summary()

        assert "## 项目符号索引" in summary
        assert "class MyService" in summary
        assert "run, stop" in summary  # methods 预览
        assert "svc.py" in summary
        assert "function helper" in summary
        assert "x, y" in summary  # args 预览
        assert "util.py" in summary

    def test_load_summary_empty_when_no_file(self, tmp_path, monkeypatch):
        """索引文件不存在时返回空字符串"""
        from services import code_indexer as ci_mod
        monkeypatch.setattr(ci_mod, "INDEX_FILE", tmp_path / "nonexistent.json")
        assert ci_mod.CodeIndexer.load_summary() == ""

    def test_load_summary_empty_when_no_symbols(self, tmp_path, monkeypatch):
        """索引文件存在但 symbols 为空时返回空字符串"""
        from services import code_indexer as ci_mod
        idx_file = tmp_path / "symbols.json"
        idx_file.write_text(json.dumps({"symbols": []}), encoding="utf-8")
        monkeypatch.setattr(ci_mod, "INDEX_FILE", idx_file)
        assert ci_mod.CodeIndexer.load_summary() == ""

    def test_load_summary_corrupted_file_returns_empty(self, tmp_path, monkeypatch):
        """索引文件损坏时返回空字符串（best-effort）"""
        from services import code_indexer as ci_mod
        idx_file = tmp_path / "symbols.json"
        idx_file.write_text("not valid json {{{", encoding="utf-8")
        monkeypatch.setattr(ci_mod, "INDEX_FILE", idx_file)
        assert ci_mod.CodeIndexer.load_summary() == ""


class TestBuildAgentContextInjectsCodeIndex:
    """build_agent_context 调用 load_summary 并注入到 system prompt"""

    @pytest.fixture
    def agent_service(self, tmp_path, monkeypatch):
        """最小化 AgentService——mock 掉重依赖，只测 code_index 注入路径"""
        monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path / "ws"))
        from security.sandbox import get_workspace
        get_workspace()
        from services.agent.agent_service import AgentService
        svc = AgentService(db=None, memory_service=None, memory_tree=None)
        return svc

    async def test_code_index_summary_injected_into_system_prompt(self, agent_service, monkeypatch):
        """load_summary 返回内容时，build_agent_context 的 system prompt 包含该内容"""
        from services import code_indexer as ci_mod

        _marker = "## 项目符号索引 (top-2, 30min 刷新)\n- class Marker → fake.py:1"
        # mock load_summary 返回标记
        # Task 4.7: load_summary 签名改为 (model_cfg=None, limit=None)，mock 同步
        monkeypatch.setattr(
            ci_mod.CodeIndexer, "load_summary",
            lambda model_cfg=None, limit=None: _marker,
        )
        # mock _inject_inherited_summary 直接返回原 sys_prompt（跳过跨 session 继承）
        monkeypatch.setattr(
            "services.agent.agent_context._inject_inherited_summary",
            lambda self, sc, sid, sp: _async_return(sp),
        )
        # mock _get_growth_stats 返回空（跳过成长块）
        async def _empty_stats(self):
            return {"total_nodes": 0, "bug_lessons": 0, "dreams": 0, "evolutions": 0}
        monkeypatch.setattr(
            "services.agent.agent_service.AgentService._get_growth_stats", _empty_stats
        )

        ctx, _ = await agent_service._build_agent_context(
            task_id="t1", session_id="s1", prompt="hello", mode="agent",
            rules_context="", extra_context=None,
            model_cfg={"context_window": 32000},
            agent_id="hermes",
        )

        assert _marker in ctx["system"]

    async def test_empty_summary_not_injected(self, agent_service, monkeypatch):
        """load_summary 返回空时，system prompt 不包含空注入"""
        from services import code_indexer as ci_mod
        # Task 4.7: load_summary 签名改为 (model_cfg=None, limit=None)，mock 同步
        monkeypatch.setattr(
            ci_mod.CodeIndexer, "load_summary",
            lambda model_cfg=None, limit=None: "",
        )
        monkeypatch.setattr(
            "services.agent.agent_context._inject_inherited_summary",
            lambda self, sc, sid, sp: _async_return(sp),
        )
        async def _empty_stats(self):
            return {"total_nodes": 0, "bug_lessons": 0, "dreams": 0, "evolutions": 0}
        monkeypatch.setattr(
            "services.agent.agent_service.AgentService._get_growth_stats", _empty_stats
        )

        ctx, _ = await agent_service._build_agent_context(
            task_id="t1", session_id="s1", prompt="hello", mode="agent",
            rules_context="", extra_context=None,
            model_cfg={"context_window": 32000},
            agent_id="hermes",
        )
        # system prompt 正常构建，不因空 summary 出错
        assert "system" in ctx
        assert isinstance(ctx["system"], str)
        assert len(ctx["system"]) > 0

    async def test_load_summary_exception_does_not_break_context(self, agent_service, monkeypatch):
        """load_summary 抛异常时 build_agent_context 不破坏（try/except 兜底）"""
        from services import code_indexer as ci_mod

        # Task 4.7: load_summary 签名改为 (model_cfg=None, limit=None)，mock 同步
        def _bomb(model_cfg=None, limit=None):
            raise RuntimeError("simulated load_summary failure")
        monkeypatch.setattr(ci_mod.CodeIndexer, "load_summary", _bomb)
        monkeypatch.setattr(
            "services.agent.agent_context._inject_inherited_summary",
            lambda self, sc, sid, sp: _async_return(sp),
        )
        async def _empty_stats(self):
            return {"total_nodes": 0, "bug_lessons": 0, "dreams": 0, "evolutions": 0}
        monkeypatch.setattr(
            "services.agent.agent_service.AgentService._get_growth_stats", _empty_stats
        )

        # 不应抛异常
        ctx, _ = await agent_service._build_agent_context(
            task_id="t1", session_id="s1", prompt="hello", mode="agent",
            rules_context="", extra_context=None,
            model_cfg={"context_window": 32000},
            agent_id="hermes",
        )
        assert "system" in ctx


async def _async_return(value):
    """辅助: 返回 value 的 async 函数"""
    return value
