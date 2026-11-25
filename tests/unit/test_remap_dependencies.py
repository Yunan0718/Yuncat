"""tests/unit/test_remap_dependencies.py —— Task 3.3: _remap_dependencies 静默丢弃依赖修复测试

对标:
- Claude Code: 工具/任务失败时不静默丢弃——记日志 + 保留诊断信息
- 主流做法: LLM 输出不可靠时，丢弃的依赖应有可观测性（日志 + metadata）

覆盖:
- 正常映射: old_id 在 id_mapping 中 → dependencies 正确回填
- 部分丢弃: old_id 不在 id_mapping 中 → 丢弃 + 记 warning + metadata.dropped_deps 记录
- 自引用: old_id 映射后等于节点自身 id → 丢弃 + 记录
- 重复依赖: 同一 new_dep 多次出现 → 去重，只保留一个
- 无 id_mapping: id_mapping={} → 全部丢弃 + 记 warning + metadata.dropped_deps
- 无 _raw_deps: 节点无原始依赖 → dependencies=[] + 无 dropped_deps + 无 metadata
- 临时字段清理: _remap 后 _raw_deps 字段被清理
- 空依赖列表: raw_deps=[] → dependencies=[] + 无 dropped_deps
"""
import os
import sys
import logging
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


def _make_node(node_id: str, raw_deps: list[str] | None = None) -> dict:
    """构造扁平节点 dict——含 _raw_deps 临时字段"""
    return {
        "id": node_id,
        "parent_id": None,
        "level": 0,
        "title": f"Node {node_id}",
        "description": "",
        "acceptance_criteria": ["done"],
        "dependencies": [],
        "_raw_deps": list(raw_deps) if raw_deps else [],
        "status": "pending",
        "metadata": {},
    }


class TestRemapDependencies:
    """Task 3.3: _remap_dependencies 丢弃依赖的诊断可观测性"""

    @pytest.fixture
    def decomposer(self):
        """轻量 TaskDecomposer 实例——只测 _remap_dependencies 纯函数行为"""
        from services.long_task.task_decomposer import TaskDecomposer
        return TaskDecomposer(adapters={}, model_config=None, conn=None)

    def test_normal_mapping_all_mapped(self, decomposer):
        """所有 old_id 都在 id_mapping 中且无自引用 → dependencies 正确回填，无 dropped_deps"""
        flat = [
            _make_node("n1", raw_deps=["old_b"]),  # n1 依赖 old_b→n2
            _make_node("n2", raw_deps=["old_a"]),  # n2 依赖 old_a→n1
        ]
        id_mapping = {"old_a": "n1", "old_b": "n2"}
        decomposer._remap_dependencies(flat, id_mapping)
        # n1 依赖 old_b→n2（无自引用）
        assert flat[0]["dependencies"] == ["n2"]
        assert "dropped_deps" not in flat[0].get("metadata", {})
        # n2 依赖 old_a→n1（无自引用）
        assert flat[1]["dependencies"] == ["n1"]
        assert "dropped_deps" not in flat[1].get("metadata", {})

    def test_dropped_deps_recorded_when_not_in_mapping(self, decomposer):
        """old_id 不在 id_mapping 中 → 丢弃 + metadata.dropped_deps 记录"""
        flat = [
            _make_node("n1", raw_deps=["old_a", "ghost_x", "ghost_y"]),
        ]
        id_mapping = {"old_a": "n1"}  # ghost_x/ghost_y 未映射
        decomposer._remap_dependencies(flat, id_mapping)
        # old_a→n1 是自引用 → 丢弃；ghost_x/ghost_y 未映射 → 丢弃
        assert flat[0]["dependencies"] == []
        dropped = flat[0]["metadata"]["dropped_deps"]
        assert "ghost_x" in dropped
        assert "ghost_y" in dropped
        # old_a 是自引用也应在 dropped_deps 里（Task 3.3: 自引用也算丢弃）
        assert "old_a" in dropped

    def test_self_reference_dropped(self, decomposer):
        """old_id 映射后等于节点自身 id → 自引用丢弃 + 记录"""
        flat = [
            _make_node("n1", raw_deps=["old_self"]),
        ]
        id_mapping = {"old_self": "n1"}  # 映射到自身
        decomposer._remap_dependencies(flat, id_mapping)
        assert flat[0]["dependencies"] == []
        assert flat[0]["metadata"]["dropped_deps"] == ["old_self"]

    def test_duplicate_deps_deduplicated(self, decomposer):
        """同一 new_dep 多次出现 → dependencies 去重，只保留一个"""
        flat = [
            _make_node("n1", raw_deps=["old_a", "old_a", "old_a"]),
        ]
        id_mapping = {"old_a": "n2"}
        decomposer._remap_dependencies(flat, id_mapping)
        # 3 次 old_a → n2，去重后只 1 个
        assert flat[0]["dependencies"] == ["n2"]
        # 重复的 2 次应记录到 dropped_deps（第 2/3 次是重复）
        dropped = flat[0]["metadata"]["dropped_deps"]
        assert dropped.count("old_a") == 2

    def test_empty_id_mapping_drops_all_with_warning(self, decomposer, caplog):
        """id_mapping={} → 全部丢弃 + 记 warning + metadata.dropped_deps"""
        flat = [
            _make_node("n1", raw_deps=["old_a", "old_b"]),
        ]
        with caplog.at_level(logging.WARNING, logger="yuncat.long_task"):
            decomposer._remap_dependencies(flat, id_mapping={})
        assert flat[0]["dependencies"] == []
        assert set(flat[0]["metadata"]["dropped_deps"]) == {"old_a", "old_b"}
        # 应记 warning 日志
        assert any("丢弃" in r.message for r in caplog.records)

    def test_no_raw_deps_no_dropped(self, decomposer):
        """节点无原始依赖 → dependencies=[] + 无 dropped_deps + 无 metadata"""
        flat = [_make_node("n1", raw_deps=[])]
        id_mapping = {"old_a": "n1"}
        decomposer._remap_dependencies(flat, id_mapping)
        assert flat[0]["dependencies"] == []
        assert "dropped_deps" not in flat[0].get("metadata", {})

    def test_raw_deps_field_cleaned_after_remap(self, decomposer):
        """_remap 后 _raw_deps 临时字段被清理（无论是否丢弃）"""
        flat = [_make_node("n1", raw_deps=["old_a"])]
        id_mapping = {"old_a": "n2"}
        decomposer._remap_dependencies(flat, id_mapping)
        assert "_raw_deps" not in flat[0]

    def test_empty_raw_deps_no_metadata(self, decomposer):
        """空 raw_deps → 无 dropped_deps + 无 metadata 字段（清理空 metadata）"""
        flat = [_make_node("n1", raw_deps=[])]
        id_mapping = {"old_a": "n2"}
        decomposer._remap_dependencies(flat, id_mapping)
        # 无诊断信息时 metadata 被清理
        assert "metadata" not in flat[0] or flat[0].get("metadata") == {}

    def test_warning_logged_when_deps_dropped(self, decomposer, caplog):
        """丢弃依赖时记 warning 日志——含节点 id + 丢弃数量 + 依赖列表"""
        flat = [_make_node("n1", raw_deps=["ghost_x"])]
        id_mapping = {}  # 无映射，全部丢弃
        with caplog.at_level(logging.WARNING, logger="yuncat.long_task"):
            decomposer._remap_dependencies(flat, id_mapping)
        # 日志含节点 id 和丢弃数量
        warning_records = [r for r in caplog.records if "丢弃" in r.message]
        assert len(warning_records) == 1
        assert "n1" in warning_records[0].message
        assert "1" in warning_records[0].message  # 丢弃 1 个

    def test_mixed_mapped_and_dropped(self, decomposer):
        """混合场景: 部分映射成功 + 部分丢弃"""
        flat = [
            _make_node("n1", raw_deps=["old_a", "old_b", "ghost_x", "ghost_y"]),
        ]
        id_mapping = {"old_a": "n2", "old_b": "n3"}
        # old_a→n2, old_b→n3 映射成功；ghost_x, ghost_y 丢弃
        decomposer._remap_dependencies(flat, id_mapping)
        assert set(flat[0]["dependencies"]) == {"n2", "n3"}
        assert set(flat[0]["metadata"]["dropped_deps"]) == {"ghost_x", "ghost_y"}
