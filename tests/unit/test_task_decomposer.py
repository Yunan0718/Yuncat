"""tests/unit/test_task_decomposer.py —— Phase 79 Task Decomposer 测试

覆盖：
1. decompose 正常路径——LLM 返回嵌套树 → 扁平化 → 写入 plan_steps
2. decompose LLM 失败兜底——单节点
3. decompose JSON 解析失败兜底
4. decompose adapter 缺失兜底
5. decompose 校验空 title 节点被丢弃
6. decompose level > 2 截断
7. decompose 节点数上限 _MAX_NODES
8. regenerate_subtree 正常路径——删 pending 子孙 + 重新分解
9. regenerate_subtree 节点不存在 → ValueError
10. regenerate_subtree LLM 失败兜底
11. _normalize_tree / _normalize_children / _flatten_node 单元测试
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from config import DEFAULT_AGENT_ID


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
async def db(tmp_path):
    """aiosqlite + SCHEMA_SQL——每个测试独立 DB"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_task_decomposer.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    yield conn
    await conn.close()


class _MockAdapter:
    """模拟 adapter

    - invoke_return: 单一返回（向后兼容旧测试）
    - invoke_returns: list[dict]——按调用顺序返回不同结果（Task 6.12 两阶段测试用）
    - raise_on_invoke: 调用即抛异常
    - raise_on_call_idx: 第 N 次调用抛异常（best-effort 测试用，1-based）
    """
    def __init__(self, invoke_return=None, invoke_returns=None,
                 raise_on_invoke=False, raise_on_call_idx=None):
        self.invoke_return = invoke_return or {"content": "[]"}
        self.invoke_returns = invoke_returns  # list[dict] | None
        self.raise_on_invoke = raise_on_invoke
        self.raise_on_call_idx = raise_on_call_idx  # int | None（1-based）
        self.invoke_calls: list[str] = []
        self._call_idx = 0

    async def invoke(self, prompt: str, context=None):
        self._call_idx += 1
        self.invoke_calls.append(prompt)
        if self.raise_on_invoke:
            raise RuntimeError("mock invoke fail")
        if self.raise_on_call_idx is not None and self._call_idx == self.raise_on_call_idx:
            raise RuntimeError(f"mock invoke fail on call {self._call_idx}")
        if self.invoke_returns is not None:
            # 按调用顺序返回不同结果——超出列表则回退最后一个（防御性）
            idx = min(self._call_idx - 1, len(self.invoke_returns) - 1)
            return self.invoke_returns[idx]
        return self.invoke_return


class _MockModelConfig:
    """模拟 ModelConfigService"""
    async def get_active_config(self, agent_id):
        return {"model": "flash-mock", "api_key": "mock"}


def _make_decomposer(adapter, db):
    """构造 TaskDecomposer——按默认 agent id 注册 adapter（Phase N 起默认 yuncat）"""
    from services.long_task.task_decomposer import TaskDecomposer
    return TaskDecomposer(
        adapters={DEFAULT_AGENT_ID: adapter},
        model_config=_MockModelConfig(),
        conn=db,
    )


# ═══════════════════════════════════════════════════════════════
# decompose
# ═══════════════════════════════════════════════════════════════

class TestDecompose:
    """TaskDecomposer.decompose"""

    async def test_decompose_normal_writes_tree(self, db):
        """LLM 返回嵌套树 → 扁平化 → 写入 plan_steps"""
        from services.long_task.task_decomposer import TaskDecomposer
        tree_json = {
            "id": "root", "title": "目标", "description": "做 X",
            "acceptance_criteria": ["完成 X"],
            "children": [
                {
                    "id": "p1", "title": "阶段1", "description": "...",
                    "acceptance_criteria": ["阶段1完成"],
                    "children": [
                        {"id": "a1", "title": "动作1", "description": "...",
                         "acceptance_criteria": ["动作1完成"]},
                        {"id": "a2", "title": "动作2", "description": "...",
                         "acceptance_criteria": ["动作2完成"]},
                    ],
                },
                {"id": "p2", "title": "阶段2", "description": "...",
                 "acceptance_criteria": ["阶段2完成"]},
            ],
        }
        adapter = _MockAdapter(invoke_return={"content": json.dumps(tree_json)})
        decomposer = _make_decomposer(adapter, db)

        result = await decomposer.decompose("task-1", "做 X")

        assert result["source"] == "llm"
        assert result["total"] == 5  # root + p1 + a1 + a2 + p2
        assert result["root_count"] == 1
        # 验证 level 分布：root(0) + p1/p2(1) + a1/a2(2) → [0,1,1,2,2]
        levels = sorted(n["level"] for n in result["nodes"])
        assert levels == [0, 1, 1, 2, 2]
        # 验证 parent_id 关系
        root = next(n for n in result["nodes"] if n["level"] == 0)
        phases = [n for n in result["nodes"] if n["level"] == 1]
        actions = [n for n in result["nodes"] if n["level"] == 2]
        assert all(p["parent_id"] == root["id"] for p in phases)
        assert all(a["parent_id"] == phases[0]["id"] for a in actions)
        # 验证 acceptance_criteria 非空
        for n in result["nodes"]:
            assert len(n["acceptance_criteria"]) > 0

    async def test_decompose_llm_exception_fallback(self, db):
        """LLM 调用抛异常 → 单节点兜底"""
        adapter = _MockAdapter(raise_on_invoke=True)
        decomposer = _make_decomposer(adapter, db)

        result = await decomposer.decompose("task-2", "做 Y")

        assert result["source"] == "fallback"
        assert result["total"] == 1
        assert result["root_count"] == 1
        assert result["nodes"][0]["level"] == 0
        assert result["nodes"][0]["title"] == "做 Y"

    async def test_decompose_adapter_error_field_fallback(self, db):
        """adapter 返回 {"error": ...} → 单节点兜底"""
        adapter = _MockAdapter(invoke_return={"error": "rate limited"})
        decomposer = _make_decomposer(adapter, db)

        result = await decomposer.decompose("task-3", "做 Z")

        assert result["source"] == "fallback"
        assert result["total"] == 1

    async def test_decompose_invalid_json_fallback(self, db):
        """adapter 返回非 JSON → 单节点兜底"""
        adapter = _MockAdapter(invoke_return={"content": "not a json"})
        decomposer = _make_decomposer(adapter, db)

        result = await decomposer.decompose("task-4", "做 W")

        assert result["source"] == "fallback"
        assert result["total"] == 1

    async def test_decompose_adapter_missing_fallback(self, db):
        """adapters 里没 hermes → 兜底"""
        from services.long_task.task_decomposer import TaskDecomposer
        decomposer = TaskDecomposer(
            adapters={},  # 无任何 adapter → 兜底 fallback
            model_config=_MockModelConfig(),
            conn=db,
        )

        result = await decomposer.decompose("task-5", "做 V")

        assert result["source"] == "fallback"
        assert result["total"] == 1

    async def test_decompose_drops_empty_title_node(self, db):
        """LLM 返回的节点 title 为空 → 该节点被丢弃"""
        tree_json = {
            "id": "root", "title": "目标", "acceptance_criteria": ["完成"],
            "children": [
                {"id": "p1", "title": "", "acceptance_criteria": ["..."]},  # 空 title 丢弃
                {"id": "p2", "title": "阶段2", "acceptance_criteria": ["..."]},
            ],
        }
        adapter = _MockAdapter(invoke_return={"content": json.dumps(tree_json)})
        decomposer = _make_decomposer(adapter, db)

        result = await decomposer.decompose("task-6", "目标")

        assert result["total"] == 2  # root + p2
        titles = [n["title"] for n in result["nodes"]]
        assert "阶段2" in titles
        assert "" not in titles

    async def test_decompose_empty_criteria_filled_with_default(self, db):
        """acceptance_criteria 为空 list → 自动填默认值「完成: {title}」"""
        tree_json = {
            "id": "root", "title": "目标", "acceptance_criteria": [],
            "children": [],
        }
        adapter = _MockAdapter(invoke_return={"content": json.dumps(tree_json)})
        decomposer = _make_decomposer(adapter, db)

        result = await decomposer.decompose("task-7", "目标")

        assert result["total"] == 1
        assert result["nodes"][0]["acceptance_criteria"] == ["完成: 目标"]

    async def test_decompose_level_truncation(self, db):
        """LLM 输出 4 层 → 第 4 层被截断（level > _MAX_LEVEL 不展开）"""
        deep_tree = {
            "id": "r", "title": "L0", "acceptance_criteria": ["..."],
            "children": [{
                "id": "c1", "title": "L1", "acceptance_criteria": ["..."],
                "children": [{
                    "id": "c2", "title": "L2", "acceptance_criteria": ["..."],
                    "children": [{
                        "id": "c3", "title": "L3-should-be-truncated", "acceptance_criteria": ["..."],
                    }],
                }],
            }],
        }
        adapter = _MockAdapter(invoke_return={"content": json.dumps(deep_tree)})
        decomposer = _make_decomposer(adapter, db)

        result = await decomposer.decompose("task-8", "L0")

        # L0 + L1 + L2 = 3 节点（L3 被截断）
        assert result["total"] == 3
        titles = [n["title"] for n in result["nodes"]]
        assert "L3-should-be-truncated" not in titles
        assert max(n["level"] for n in result["nodes"]) == 2

    async def test_decompose_max_nodes_limit(self, db):
        """LLM 输出 > 30 节点 → 截断到 _MAX_NODES"""
        children = [
            {"id": f"c{i}", "title": f"阶段{i}", "acceptance_criteria": ["..."],
             "children": [
                 {"id": f"a{i}_{j}", "title": f"动作{i}_{j}", "acceptance_criteria": ["..."]}
                 for j in range(5)
             ]}
            for i in range(10)
        ]
        tree_json = {
            "id": "root", "title": "目标", "acceptance_criteria": ["..."],
            "children": children,
        }
        adapter = _MockAdapter(invoke_return={"content": json.dumps(tree_json)})
        decomposer = _make_decomposer(adapter, db)

        result = await decompose_with_limit(decomposer, "task-9", "目标")

        assert result["total"] <= 30


async def decompose_with_limit(decomposer, task_id, goal):
    """辅助——decompose 已经内置 _MAX_NODES 截断，直接调"""
    return await decomposer.decompose(task_id, goal)


# ═══════════════════════════════════════════════════════════════
# regenerate_subtree
# ═══════════════════════════════════════════════════════════════

class TestRegenerateSubtree:
    """TaskDecomposer.regenerate_subtree"""

    async def test_regenerate_node_not_found_raises(self, db):
        """node_id 不存在 → ValueError"""
        adapter = _MockAdapter()
        decomposer = _make_decomposer(adapter, db)

        with pytest.raises(ValueError, match="node not found"):
            await decomposer.regenerate_subtree("task-x", "nonexistent-node")

    async def test_regenerate_normal_replaces_pending_children(self, db):
        """regenerate_subtree 删 pending 子孙 + 重新分解"""
        from repositories import create_plan_tree, get_plan_tree_by_task
        # 先建一棵树：root + p1 + a1 + a2
        await create_plan_tree(db, "task-r", [
            {"id": "root", "task_id": "task-r", "level": 0, "title": "根", "acceptance_criteria": ["..."]},
            {"id": "p1", "task_id": "task-r", "parent_id": "root", "level": 1, "title": "阶段1", "acceptance_criteria": ["..."]},
            {"id": "a1", "task_id": "task-r", "parent_id": "p1", "level": 2, "title": "旧动作1", "acceptance_criteria": ["..."]},
            {"id": "a2", "task_id": "task-r", "parent_id": "p1", "level": 2, "title": "旧动作2", "acceptance_criteria": ["..."]},
        ])

        # LLM 返回新子节点
        new_children = [
            {"id": "c1", "title": "新动作1", "description": "...", "acceptance_criteria": ["..."]},
            {"id": "c2", "title": "新动作2", "description": "...", "acceptance_criteria": ["..."]},
        ]
        adapter = _MockAdapter(invoke_return={"content": json.dumps(new_children)})
        decomposer = _make_decomposer(adapter, db)

        result = await decomposer.regenerate_subtree("task-r", "p1")

        assert result["source"] == "llm"
        assert result["deleted_count"] == 2  # a1 + a2 被删
        assert len(result["nodes"]) == 2  # 新动作1 + 新动作2
        # 验证 DB 里只剩 root + p1 + 新动作1 + 新动作2
        tree = await get_plan_tree_by_task(db, "task-r")
        titles = [n["title"] for n in tree]
        assert "根" in titles
        assert "阶段1" in titles
        assert "新动作1" in titles
        assert "新动作2" in titles
        assert "旧动作1" not in titles
        assert "旧动作2" not in titles

    async def test_regenerate_preserves_completed_children(self, db):
        """regenerate_subtree 保留 completed 状态的子节点"""
        from repositories import create_plan_tree, update_plan_node_status, get_plan_tree_by_task
        # root + p1 + a1(completed) + a2(pending)
        await create_plan_tree(db, "task-r2", [
            {"id": "root", "task_id": "task-r2", "level": 0, "title": "根", "acceptance_criteria": ["..."]},
            {"id": "p1", "task_id": "task-r2", "parent_id": "root", "level": 1, "title": "阶段1", "acceptance_criteria": ["..."]},
            {"id": "a1", "task_id": "task-r2", "parent_id": "p1", "level": 2, "title": "已完成动作", "acceptance_criteria": ["..."], "status": "completed"},
            {"id": "a2", "task_id": "task-r2", "parent_id": "p1", "level": 2, "title": "待办动作", "acceptance_criteria": ["..."]},
        ])

        # LLM 返回新子节点
        new_children = [
            {"id": "c1", "title": "新动作", "description": "...", "acceptance_criteria": ["..."]},
        ]
        adapter = _MockAdapter(invoke_return={"content": json.dumps(new_children)})
        decomposer = _make_decomposer(adapter, db)

        result = await decomposer.regenerate_subtree("task-r2", "p1")

        assert result["deleted_count"] == 1  # 只删 a2（pending）
        # completed 的 a1 保留
        tree = await get_plan_tree_by_task(db, "task-r2")
        titles = [n["title"] for n in tree]
        assert "已完成动作" in titles
        assert "待办动作" not in titles
        assert "新动作" in titles

    async def test_regenerate_llm_fail_fallback(self, db):
        """regenerate_subtree LLM 失败 → 生成单子节点兜底"""
        from repositories import create_plan_tree
        await create_plan_tree(db, "task-r3", [
            {"id": "root", "task_id": "task-r3", "level": 0, "title": "根", "acceptance_criteria": ["..."]},
            {"id": "p1", "task_id": "task-r3", "parent_id": "root", "level": 1, "title": "阶段1", "acceptance_criteria": ["..."]},
        ])

        adapter = _MockAdapter(raise_on_invoke=True)
        decomposer = _make_decomposer(adapter, db)

        result = await decomposer.regenerate_subtree("task-r3", "p1")

        assert result["source"] == "fallback"
        assert len(result["nodes"]) == 1
        assert result["nodes"][0]["parent_id"] == "p1"
        assert result["nodes"][0]["level"] == 2  # parent_level(1) + 1

    async def test_regenerate_at_max_level_no_children(self, db):
        """regenerate_subtree 在 level=2 节点上调用 → child_level=2（不再增加）"""
        from repositories import create_plan_tree
        await create_plan_tree(db, "task-r4", [
            {"id": "root", "task_id": "task-r4", "level": 0, "title": "根", "acceptance_criteria": ["..."]},
            {"id": "p1", "task_id": "task-r4", "parent_id": "root", "level": 1, "title": "阶段", "acceptance_criteria": ["..."]},
            {"id": "a1", "task_id": "task-r4", "parent_id": "p1", "level": 2, "title": "动作", "acceptance_criteria": ["..."]},
        ])

        # LLM 返回新子节点——但因 parent_level=2=_MAX_LEVEL，child_level 仍为 2
        new_children = [
            {"id": "c1", "title": "新动作", "acceptance_criteria": ["..."]},
        ]
        adapter = _MockAdapter(invoke_return={"content": json.dumps(new_children)})
        decomposer = _make_decomposer(adapter, db)

        result = await decomposer.regenerate_subtree("task-r4", "a1")

        assert result["source"] == "llm"
        assert len(result["nodes"]) == 1
        assert result["nodes"][0]["level"] == 2  # min(2+1, 2) = 2


# ═══════════════════════════════════════════════════════════════
# _normalize_tree / _normalize_children 单元测试
# ═══════════════════════════════════════════════════════════════

class TestNormalize:
    """_normalize_tree / _normalize_children / _flatten_node"""

    def test_normalize_tree_non_dict_returns_empty(self):
        from services.long_task.task_decomposer import TaskDecomposer
        d = TaskDecomposer.__new__(TaskDecomposer)
        assert d._normalize_tree([]) == []
        assert d._normalize_tree("not a dict") == []
        assert d._normalize_tree(None) == []

    def test_normalize_children_non_list_returns_empty(self):
        from services.long_task.task_decomposer import TaskDecomposer
        d = TaskDecomposer.__new__(TaskDecomposer)
        assert d._normalize_children({"not": "list"}, parent_id="p", base_level=1) == []
        assert d._normalize_children(None, parent_id="p", base_level=1) == []

    def test_normalize_tree_assigns_sequential_ids(self):
        from services.long_task.task_decomposer import TaskDecomposer
        d = TaskDecomposer.__new__(TaskDecomposer)
        tree = {
            "title": "r", "acceptance_criteria": ["..."],
            "children": [
                {"title": "c1", "acceptance_criteria": ["..."]},
                {"title": "c2", "acceptance_criteria": ["..."]},
            ],
        }
        flat = d._normalize_tree(tree)
        ids = [n["id"] for n in flat]
        assert ids == ["n1", "n2", "n3"]  # 顺序：root → c1 → c2（DFS）

    def test_normalize_children_assigns_parent_id(self):
        from services.long_task.task_decomposer import TaskDecomposer
        d = TaskDecomposer.__new__(TaskDecomposer)
        children = [
            {"title": "c1", "acceptance_criteria": ["..."]},
            {"title": "c2", "acceptance_criteria": ["..."]},
        ]
        flat = d._normalize_children(children, parent_id="parent-x", base_level=1)
        assert all(n["parent_id"] == "parent-x" for n in flat)
        assert all(n["level"] == 1 for n in flat)


# ═══════════════════════════════════════════════════════════════
# Task 6.12: 两阶段分解（粗→细）
# ═══════════════════════════════════════════════════════════════

class TestTwoPhaseDecompose:
    """Task 6.12: TASK_DECOMPOSER_TWO_PHASE 开关——粗分解(level 0-1) + 每阶段细分解(level 2)

    设计：
    - TWO_PHASE=False（默认）→ 单阶段，行为同旧版，1 次 LLM 调用
    - TWO_PHASE=True → 粗分解(1 次) + 每个 level 1 节点细分解(N 次) = 1+N 次调用
    - 细分解 best-effort：单个细分解失败不阻塞其他阶段
    - 粗分解失败 → 整体走 fallback 单节点
    """

    def _coarse_tree(self):
        """粗分解结果——root + 2 phases，无 children（level 0-1）"""
        return {
            "id": "root", "title": "目标", "description": "做 X",
            "acceptance_criteria": ["完成 X"],
            "children": [
                {"id": "p1", "title": "阶段1", "description": "...",
                 "acceptance_criteria": ["阶段1完成"]},
                {"id": "p2", "title": "阶段2", "description": "...",
                 "acceptance_criteria": ["阶段2完成"]},
            ],
        }

    def _fine_children(self, prefix="a"):
        """细分解结果——2 个 actions（level 2 children 数组）"""
        return [
            {"id": f"{prefix}1", "title": f"动作{prefix}1", "description": "...",
             "acceptance_criteria": ["..."]},
            {"id": f"{prefix}2", "title": f"动作{prefix}2", "description": "...",
             "acceptance_criteria": ["..."]},
        ]

    async def test_two_phase_disabled_backward_compat(self, db):
        """TWO_PHASE=False（默认）→ 单阶段，1 次 LLM 调用，向后兼容"""
        full_tree = {
            "id": "root", "title": "目标", "acceptance_criteria": ["完成"],
            "children": [
                {"id": "p1", "title": "阶段1", "acceptance_criteria": ["..."],
                 "children": [{"id": "a1", "title": "动作1", "acceptance_criteria": ["..."]}]},
            ],
        }
        adapter = _MockAdapter(invoke_return={"content": json.dumps(full_tree)})
        decomposer = _make_decomposer(adapter, db)

        result = await decomposer.decompose("tp-1", "目标")

        assert result["source"] == "llm"
        assert len(adapter.invoke_calls) == 1  # 单阶段 = 1 次调用
        assert result["total"] == 3  # root + p1 + a1

    async def test_two_phase_enabled_coarse_then_fine(self, db, monkeypatch):
        """TWO_PHASE=True → 粗分解 + 每阶段细分解，结果含完整 3 层"""
        import config
        monkeypatch.setattr(config, "TASK_DECOMPOSER_TWO_PHASE", True)

        adapter = _MockAdapter(invoke_returns=[
            {"content": json.dumps(self._coarse_tree())},          # 第 1 次：粗分解
            {"content": json.dumps(self._fine_children("a"))},     # 第 2 次：p1 细分解
            {"content": json.dumps(self._fine_children("b"))},     # 第 3 次：p2 细分解
        ])
        decomposer = _make_decomposer(adapter, db)

        result = await decomposer.decompose("tp-2", "目标")

        assert result["source"] == "llm"
        # root + p1 + p2 + a1 + a2 + b1 + b2 = 7
        assert result["total"] == 7
        # level 分布：root(0) + p1/p2(1) + 4 actions(2)
        levels = sorted(n["level"] for n in result["nodes"])
        assert levels == [0, 1, 1, 2, 2, 2, 2]
        # 验证 actions 的 parent_id 指向对应 phase
        phases = [n for n in result["nodes"] if n["level"] == 1]
        actions = [n for n in result["nodes"] if n["level"] == 2]
        phase_ids = {p["id"] for p in phases}
        assert all(a["parent_id"] in phase_ids for a in actions)
        # 每个 phase 都有 children
        for p in phases:
            children = [a for a in actions if a["parent_id"] == p["id"]]
            assert len(children) == 2

    async def test_two_phase_llm_call_count(self, db, monkeypatch):
        """TWO_PHASE=True + 2 phases → 1（粗）+ 2（细）= 3 次 LLM 调用"""
        import config
        monkeypatch.setattr(config, "TASK_DECOMPOSER_TWO_PHASE", True)

        adapter = _MockAdapter(invoke_returns=[
            {"content": json.dumps(self._coarse_tree())},
            {"content": json.dumps(self._fine_children("a"))},
            {"content": json.dumps(self._fine_children("b"))},
        ])
        decomposer = _make_decomposer(adapter, db)

        await decomposer.decompose("tp-3", "目标")

        assert len(adapter.invoke_calls) == 3  # 1 粗 + 2 细

    async def test_two_phase_coarse_fail_fallback(self, db, monkeypatch):
        """TWO_PHASE=True + 粗分解 LLM 异常 → 整体走 fallback 单节点"""
        import config
        monkeypatch.setattr(config, "TASK_DECOMPOSER_TWO_PHASE", True)

        adapter = _MockAdapter(raise_on_invoke=True)
        decomposer = _make_decomposer(adapter, db)

        result = await decomposer.decompose("tp-4", "目标")

        assert result["source"] == "fallback"
        assert result["total"] == 1
        assert result["nodes"][0]["level"] == 0

    async def test_two_phase_fine_fail_best_effort(self, db, monkeypatch):
        """TWO_PHASE=True + 某阶段细分解失败 → 该阶段无 children，其他阶段正常

        关键：单个细分解失败不阻塞其他阶段（best-effort）
        """
        import config
        monkeypatch.setattr(config, "TASK_DECOMPOSER_TWO_PHASE", True)

        # 第 1 次（粗）成功，第 2 次（p1 细）失败，第 3 次（p2 细）成功
        adapter = _MockAdapter(
            invoke_returns=[
                {"content": json.dumps(self._coarse_tree())},
                {"content": json.dumps(self._fine_children("a"))},
                {"content": json.dumps(self._fine_children("b"))},
            ],
            raise_on_call_idx=2,  # 第 2 次调用抛异常（p1 细分解失败）
        )
        decomposer = _make_decomposer(adapter, db)

        result = await decomposer.decompose("tp-5", "目标")

        assert result["source"] == "llm"
        # root + p1 + p2 + p2 的 2 actions = 5（p1 无 children）
        assert result["total"] == 5
        phases = [n for n in result["nodes"] if n["level"] == 1]
        actions = [n for n in result["nodes"] if n["level"] == 2]
        # 找到失败的 phase（p1）——无 children
        p_failed = next(p for p in phases if not any(a["parent_id"] == p["id"] for a in actions))
        p_ok = next(p for p in phases if p["id"] != p_failed["id"])
        # 成功的 phase 有 2 actions
        p_ok_actions = [a for a in actions if a["parent_id"] == p_ok["id"]]
        assert len(p_ok_actions) == 2

    async def test_two_phase_no_phases_no_fine_calls(self, db, monkeypatch):
        """TWO_PHASE=True + 粗分解只返回 root（无 phases）→ 不触发细分解"""
        import config
        monkeypatch.setattr(config, "TASK_DECOMPOSER_TWO_PHASE", True)

        root_only = {
            "id": "root", "title": "目标", "acceptance_criteria": ["完成"],
            "children": [],  # 无 phases
        }
        adapter = _MockAdapter(invoke_returns=[
            {"content": json.dumps(root_only)},
        ])
        decomposer = _make_decomposer(adapter, db)

        result = await decomposer.decompose("tp-6", "目标")

        assert result["source"] == "llm"
        assert result["total"] == 1  # 只有 root
        assert len(adapter.invoke_calls) == 1  # 只粗分解，无细分解

    async def test_two_phase_respects_max_level(self, db, monkeypatch):
        """TWO_PHASE=True + 细分解 LLM 返回嵌套 children（level 3）→ 截断到 MAX_LEVEL=2"""
        import config
        monkeypatch.setattr(config, "TASK_DECOMPOSER_TWO_PHASE", True)

        # 细分解返回带 children 的节点——但 phase 已是 level 1，children 应为 level 2
        # LLM 输出了 level 3 嵌套，应被截断
        fine_with_nested = [
            {"id": "a1", "title": "动作1", "acceptance_criteria": ["..."],
             "children": [  # 这些应是 level 3，会被截断
                 {"id": "a1_sub", "title": "子动作", "acceptance_criteria": ["..."]},
             ]},
        ]
        adapter = _MockAdapter(invoke_returns=[
            {"content": json.dumps(self._coarse_tree())},
            {"content": json.dumps(fine_with_nested)},  # p1 细分解
            {"content": json.dumps(self._fine_children("b"))},  # p2 细分解
        ])
        decomposer = _make_decomposer(adapter, db)

        result = await decomposer.decompose("tp-7", "目标")

        # 不应出现 level 3 节点
        assert max(n["level"] for n in result["nodes"]) <= 2
        titles = [n["title"] for n in result["nodes"]]
        assert "子动作" not in titles  # level 3 被截断


# ═══════════════════════════════════════════════════════════════
# Task 6.13: standards_context 注入 decompose prompt
# ═══════════════════════════════════════════════════════════════

class TestStandardsContext:
    """Task 6.13: decompose 前置查询结果作为 standards_context 注入 prompt

    设计：
    - standards_context 非空 → prompt 注入「## 参考文档（最新标准）」段
    - standards_context=None → prompt 不含参考文档段（向后兼容）
    - 两阶段粗分解 prompt 也注入
    """

    async def test_decompose_with_standards_context_injects_into_prompt(self, db):
        """standards_context 非空 → prompt 含参考文档段"""
        standards = "## 参考文档\n- React 18 并发渲染\n- 使用 createRoot 替代 ReactDOM.render"
        adapter = _MockAdapter(invoke_return={"content": json.dumps({
            "id": "root", "title": "目标", "acceptance_criteria": ["完成"], "children": []
        })})
        decomposer = _make_decomposer(adapter, db)

        await decomposer.decompose("sc-1", "目标", standards_context=standards)

        assert len(adapter.invoke_calls) == 1
        prompt = adapter.invoke_calls[0]
        assert "React 18 并发渲染" in prompt
        assert "createRoot" in prompt

    async def test_decompose_without_standards_context_no_section(self, db):
        """standards_context=None → prompt 不含参考文档段（向后兼容）"""
        adapter = _MockAdapter(invoke_return={"content": json.dumps({
            "id": "root", "title": "目标", "acceptance_criteria": ["完成"], "children": []
        })})
        decomposer = _make_decomposer(adapter, db)

        await decomposer.decompose("sc-2", "目标")  # 不传 standards_context

        prompt = adapter.invoke_calls[0]
        assert "参考文档" not in prompt

    async def test_two_phase_coarse_prompt_with_standards_context(self, db, monkeypatch):
        """两阶段粗分解 prompt 也注入 standards_context"""
        import config
        monkeypatch.setattr(config, "TASK_DECOMPOSER_TWO_PHASE", True)

        standards = "## 参考文档\n- React 18 并发渲染"
        adapter = _MockAdapter(invoke_returns=[
            {"content": json.dumps({
                "id": "root", "title": "目标", "acceptance_criteria": ["完成"],
                "children": [{"id": "p1", "title": "阶段1", "acceptance_criteria": ["..."]}]
            })},
            {"content": json.dumps([{"id": "a1", "title": "动作1", "acceptance_criteria": ["..."]}])},
        ])
        decomposer = _make_decomposer(adapter, db)

        await decomposer.decompose("sc-3", "目标", standards_context=standards)

        # 粗分解 prompt（第 1 次调用）应含 standards
        coarse_prompt = adapter.invoke_calls[0]
        assert "React 18 并发渲染" in coarse_prompt
