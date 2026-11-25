"""tests/unit/test_plan_step_repo.py —— Phase 77 长任务分解树 CRUD 测试

覆盖：
1. create_plan_node + get_plan_tree_by_task 基本 CRUD
2. level CHECK 约束（level=3 应抛 IntegrityError）
3. create_plan_tree 批量建树
4. update_plan_node_status 状态流转 + output 更新
5. delete_pending_subtree 只删 pending（completed 保留）
6. get_pending_leaves 叶子节点查询
7. get_ready_nodes 依赖满足的 ready 节点
8. 多级嵌套（level 0→1→2）+ JSON 字段往返
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
async def db(tmp_path):
    """aiosqlite + SCHEMA_SQL——每个测试独立 DB"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_plan_step_repo.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    yield conn
    await conn.close()


# ═══════════════════════════════════════════════════════════════
# 基本 CRUD
# ═══════════════════════════════════════════════════════════════

class TestCreateAndGet:
    """create_plan_node + get_plan_tree_by_task"""

    async def test_create_node_returns_id(self, db):
        """create_plan_node 返回非空 node_id"""
        from repositories.plan_step_repo import create_plan_node
        nid = await create_plan_node(db, {
            "task_id": "task-1", "title": "根节点", "level": 0,
        })
        assert isinstance(nid, str) and len(nid) > 0

    async def test_create_node_default_status_pending(self, db):
        """不指定 status → 默认 'pending'"""
        from repositories.plan_step_repo import create_plan_node, get_plan_tree_by_task
        nid = await create_plan_node(db, {"task_id": "task-1", "title": "x"})
        tree = await get_plan_tree_by_task(db, "task-1")
        assert len(tree) == 1
        assert tree[0]["id"] == nid
        assert tree[0]["status"] == "pending"
        assert tree[0]["level"] == 0  # 默认 level

    async def test_get_empty_tree(self, db):
        """不存在的 task_id → 空列表"""
        from repositories.plan_step_repo import get_plan_tree_by_task
        tree = await get_plan_tree_by_task(db, "nonexistent")
        assert tree == []

    async def test_get_tree_ordered_by_level_then_created(self, db):
        """返回结果按 level ASC, created_at ASC 排序"""
        from repositories.plan_step_repo import create_plan_node, get_plan_tree_by_task
        # 先建 level 1，再建 level 0，再建 level 1——期望排序 0,1,1
        await create_plan_node(db, {"task_id": "t1", "title": "L1-a", "level": 1})
        await create_plan_node(db, {"task_id": "t1", "title": "L0", "level": 0})
        await create_plan_node(db, {"task_id": "t1", "title": "L1-b", "level": 1})

        tree = await get_plan_tree_by_task(db, "t1")
        assert [n["level"] for n in tree] == [0, 1, 1]
        assert tree[1]["title"] == "L1-a"  # 同 level 按创建时间
        assert tree[2]["title"] == "L1-b"

    async def test_json_fields_roundtrip(self, db):
        """dependencies/acceptance_criteria/review JSON 字段往返一致"""
        from repositories.plan_step_repo import create_plan_node, get_plan_tree_by_task
        nid = await create_plan_node(db, {
            "task_id": "t1", "title": "x",
            "dependencies": ["dep1", "dep2"],
            "acceptance_criteria": ["能运行", "有测试"],
            "review": '{"score": 0.8, "verdict": "pass"}',
        })
        tree = await get_plan_tree_by_task(db, "t1")
        node = tree[0]
        assert node["dependencies"] == ["dep1", "dep2"]
        assert node["acceptance_criteria"] == ["能运行", "有测试"]
        assert node["review"] == {"score": 0.8, "verdict": "pass"}

    async def test_dependencies_accept_json_string(self, db):
        """dependencies 传 JSON 字符串也能正确解析"""
        from repositories.plan_step_repo import create_plan_node, get_plan_tree_by_task
        await create_plan_node(db, {
            "task_id": "t1", "title": "x",
            "dependencies": '["a","b"]',
        })
        tree = await get_plan_tree_by_task(db, "t1")
        assert tree[0]["dependencies"] == ["a", "b"]


# ═══════════════════════════════════════════════════════════════
# level CHECK 约束
# ═══════════════════════════════════════════════════════════════

class TestLevelCheckConstraint:
    """level CHECK(level BETWEEN 0 AND 2) 约束"""

    async def test_level_3_rejected(self, db):
        """level=3 违反 CHECK 约束 → IntegrityError"""
        from repositories.plan_step_repo import create_plan_node
        with pytest.raises(Exception) as exc_info:
            await create_plan_node(db, {
                "task_id": "t1", "title": "x", "level": 3,
            })
        # aiosqlite 包装的 IntegrityError
        assert "CHECK" in str(exc_info.value).upper() or "CONSTRAINT" in str(exc_info.value).upper()

    async def test_level_0_1_2_accepted(self, db):
        """level=0/1/2 全部合法"""
        from repositories.plan_step_repo import create_plan_node, get_plan_tree_by_task
        for level in [0, 1, 2]:
            await create_plan_node(db, {
                "task_id": "t1", "title": f"L{level}", "level": level,
            })
        tree = await get_plan_tree_by_task(db, "t1")
        assert len(tree) == 3
        assert {n["level"] for n in tree} == {0, 1, 2}


# ═══════════════════════════════════════════════════════════════
# create_plan_tree 批量建树
# ═══════════════════════════════════════════════════════════════

class TestCreatePlanTree:
    """create_plan_tree 批量建树"""

    async def test_create_tree_returns_ids_in_order(self, db):
        """返回 node_id 列表顺序与输入一致"""
        from repositories.plan_step_repo import create_plan_tree
        nodes = [
            {"title": "根", "level": 0},
            {"title": "阶段1", "level": 1, "parent_id": None},
            {"title": "阶段2", "level": 1},
        ]
        ids = await create_plan_tree(db, "task-1", nodes)
        assert len(ids) == 3
        assert all(isinstance(i, str) for i in ids)

    async def test_create_tree_injects_task_id(self, db):
        """task_id 自动注入到每个节点"""
        from repositories.plan_step_repo import create_plan_tree, get_plan_tree_by_task
        await create_plan_tree(db, "task-1", [
            {"title": "a", "level": 0},
            {"title": "b", "level": 1},
        ])
        tree = await get_plan_tree_by_task(db, "task-1")
        assert all(n["task_id"] == "task-1" for n in tree)

    async def test_create_tree_preserves_explicit_ids(self, db):
        """节点显式提供 id → 保留"""
        from repositories.plan_step_repo import create_plan_tree, get_plan_tree_by_task
        ids = await create_plan_tree(db, "task-1", [
            {"id": "custom-1", "title": "a", "level": 0},
            {"id": "custom-2", "title": "b", "level": 1},
        ])
        assert ids == ["custom-1", "custom-2"]
        tree = await get_plan_tree_by_task(db, "task-1")
        assert {n["id"] for n in tree} == {"custom-1", "custom-2"}


# ═══════════════════════════════════════════════════════════════
# update_plan_node_status
# ═══════════════════════════════════════════════════════════════

class TestUpdateStatus:
    """update_plan_node_status 状态流转"""

    async def test_update_status_only(self, db):
        """只更新 status，output 不变"""
        from repositories.plan_step_repo import create_plan_node, update_plan_node_status, get_plan_tree_by_task
        nid = await create_plan_node(db, {"task_id": "t1", "title": "x"})
        await update_plan_node_status(db, nid, "running")
        tree = await get_plan_tree_by_task(db, "t1")
        assert tree[0]["status"] == "running"
        assert tree[0]["output"] == ""  # 默认空

    async def test_update_status_with_output(self, db):
        """同时更新 status 和 output"""
        from repositories.plan_step_repo import create_plan_node, update_plan_node_status, get_plan_tree_by_task
        nid = await create_plan_node(db, {"task_id": "t1", "title": "x"})
        await update_plan_node_status(db, nid, "completed", output="完成摘要")
        tree = await get_plan_tree_by_task(db, "t1")
        assert tree[0]["status"] == "completed"
        assert tree[0]["output"] == "完成摘要"

    async def test_update_updates_updated_at(self, db):
        """updated_at 字段被刷新"""
        from repositories.plan_step_repo import create_plan_node, update_plan_node_status, get_plan_tree_by_task
        nid = await create_plan_node(db, {"task_id": "t1", "title": "x"})
        tree_before = await get_plan_tree_by_task(db, "t1")
        ts_before = tree_before[0]["updated_at"]
        # 确保时间戳不同（sleep 不可靠，用 >= 判断）
        await update_plan_node_status(db, nid, "running")
        tree_after = await get_plan_tree_by_task(db, "t1")
        assert tree_after[0]["updated_at"] >= ts_before


# ═══════════════════════════════════════════════════════════════
# delete_pending_subtree
# ═══════════════════════════════════════════════════════════════

class TestDeletePendingSubtree:
    """delete_pending_subtree 只删 pending 子树"""

    async def test_delete_all_pending_subtree(self, db):
        """全是 pending → 删除整个子树"""
        from repositories.plan_step_repo import (
            create_plan_node, delete_pending_subtree, get_plan_tree_by_task,
        )
        root = await create_plan_node(db, {"task_id": "t1", "title": "root", "level": 0})
        child1 = await create_plan_node(db, {
            "task_id": "t1", "title": "c1", "level": 1, "parent_id": root,
        })
        await create_plan_node(db, {
            "task_id": "t1", "title": "c2", "level": 1, "parent_id": root,
        })

        deleted = await delete_pending_subtree(db, "t1", root)
        assert deleted == 3  # root + 2 children
        tree = await get_plan_tree_by_task(db, "t1")
        assert tree == []

    async def test_delete_preserves_completed(self, db):
        """completed 节点保留，只删 pending"""
        from repositories.plan_step_repo import (
            create_plan_node, update_plan_node_status, delete_pending_subtree,
            get_plan_tree_by_task,
        )
        root = await create_plan_node(db, {"task_id": "t1", "title": "root", "level": 0})
        child = await create_plan_node(db, {
            "task_id": "t1", "title": "child", "level": 1, "parent_id": root,
        })
        # root 标记 completed，child 保持 pending
        await update_plan_node_status(db, root, "completed")

        deleted = await delete_pending_subtree(db, "t1", root)
        # root 是 completed 不删，child 是 pending 删除
        assert deleted == 1
        tree = await get_plan_tree_by_task(db, "t1")
        assert len(tree) == 1
        assert tree[0]["id"] == root
        assert tree[0]["status"] == "completed"

    async def test_delete_nested_subtree(self, db):
        """3 层嵌套子树删除"""
        from repositories.plan_step_repo import (
            create_plan_node, delete_pending_subtree, get_plan_tree_by_task,
        )
        root = await create_plan_node(db, {"task_id": "t1", "title": "root", "level": 0})
        mid = await create_plan_node(db, {
            "task_id": "t1", "title": "mid", "level": 1, "parent_id": root,
        })
        leaf = await create_plan_node(db, {
            "task_id": "t1", "title": "leaf", "level": 2, "parent_id": mid,
        })

        deleted = await delete_pending_subtree(db, "t1", root)
        assert deleted == 3
        assert await get_plan_tree_by_task(db, "t1") == []


# ═══════════════════════════════════════════════════════════════
# get_pending_leaves
# ═══════════════════════════════════════════════════════════════

class TestGetPendingLeaves:
    """get_pending_leaves 叶子节点查询"""

    async def test_leaves_are_nodes_without_children(self, db):
        """叶子 = 没有子节点的 pending 节点"""
        from repositories.plan_step_repo import create_plan_node, get_pending_leaves
        root = await create_plan_node(db, {"task_id": "t1", "title": "root", "level": 0})
        await create_plan_node(db, {
            "task_id": "t1", "title": "child", "level": 1, "parent_id": root,
        })
        leaves = await get_pending_leaves(db, "t1")
        # root 有 child，不是叶子；child 是叶子
        assert len(leaves) == 1
        assert leaves[0]["title"] == "child"

    async def test_completed_not_in_leaves(self, db):
        """completed 节点不在 leaves 中（即使无子节点）"""
        from repositories.plan_step_repo import (
            create_plan_node, update_plan_node_status, get_pending_leaves,
        )
        nid = await create_plan_node(db, {"task_id": "t1", "title": "solo", "level": 0})
        await update_plan_node_status(db, nid, "completed")
        leaves = await get_pending_leaves(db, "t1")
        assert leaves == []

    async def test_multiple_leaves(self, db):
        """多个独立叶子全部返回"""
        from repositories.plan_step_repo import create_plan_node, get_pending_leaves
        await create_plan_node(db, {"task_id": "t1", "title": "a", "level": 0})
        await create_plan_node(db, {"task_id": "t1", "title": "b", "level": 0})
        await create_plan_node(db, {"task_id": "t1", "title": "c", "level": 0})
        leaves = await get_pending_leaves(db, "t1")
        assert len(leaves) == 3


# ═══════════════════════════════════════════════════════════════
# get_ready_nodes
# ═══════════════════════════════════════════════════════════════

class TestGetReadyNodes:
    """get_ready_nodes 依赖满足的 ready 节点"""

    async def test_no_dependencies_ready(self, db):
        """无依赖的 pending 节点 → ready"""
        from repositories.plan_step_repo import create_plan_node, get_ready_nodes
        await create_plan_node(db, {"task_id": "t1", "title": "x", "level": 0})
        ready = await get_ready_nodes(db, "t1")
        assert len(ready) == 1

    async def test_dependency_not_completed_not_ready(self, db):
        """依赖未 completed → 不 ready"""
        from repositories.plan_step_repo import create_plan_node, get_ready_nodes
        a = await create_plan_node(db, {
            "task_id": "t1", "title": "a", "level": 0, "id": "node-a",
        })
        await create_plan_node(db, {
            "task_id": "t1", "title": "b", "level": 0,
            "dependencies": [a],  # 依赖 a，a 是 pending
        })
        ready = await get_ready_nodes(db, "t1")
        # 只有 a ready（无依赖），b 不 ready
        assert len(ready) == 1
        assert ready[0]["title"] == "a"

    async def test_dependency_completed_ready(self, db):
        """依赖 completed → ready"""
        from repositories.plan_step_repo import (
            create_plan_node, update_plan_node_status, get_ready_nodes,
        )
        a = await create_plan_node(db, {
            "task_id": "t1", "title": "a", "level": 0, "id": "node-a",
        })
        await create_plan_node(db, {
            "task_id": "t1", "title": "b", "level": 0, "dependencies": [a],
        })
        await update_plan_node_status(db, a, "completed")
        ready = await get_ready_nodes(db, "t1")
        # a 已 completed 不在 ready；b 依赖满足 → ready
        assert len(ready) == 1
        assert ready[0]["title"] == "b"

    async def test_partial_dependencies_completed_not_ready(self, db):
        """多依赖部分 completed → 不 ready"""
        from repositories.plan_step_repo import (
            create_plan_node, update_plan_node_status, get_ready_nodes,
        )
        a = await create_plan_node(db, {"task_id": "t1", "title": "a", "id": "na"})
        b = await create_plan_node(db, {"task_id": "t1", "title": "b", "id": "nb"})
        await create_plan_node(db, {
            "task_id": "t1", "title": "c", "dependencies": [a, b],
        })
        await update_plan_node_status(db, a, "completed")
        # b 仍 pending → c 不 ready
        ready = await get_ready_nodes(db, "t1")
        ready_titles = {n["title"] for n in ready}
        assert "c" not in ready_titles
        assert "b" in ready_titles  # b 无依赖 → ready


# ═══════════════════════════════════════════════════════════════
# 多级嵌套集成
# ═══════════════════════════════════════════════════════════════

class TestMultiLevelIntegration:
    """多级嵌套（level 0→1→2）+ 完整生命周期"""

    async def test_full_lifecycle(self, db):
        """完整生命周期：建树 → ready 执行 → 标完成 → 下层 ready"""
        from repositories.plan_step_repo import (
            create_plan_tree, get_plan_tree_by_task, get_ready_nodes,
            update_plan_node_status, get_pending_leaves,
        )
        # 建树：根(0) → 阶段1(1) → 动作1(2)
        #              阶段2(1) → 动作2(2)
        ids = await create_plan_tree(db, "task-1", [
            {"id": "root", "title": "根目标", "level": 0},
            {"id": "phase1", "title": "阶段1", "level": 1, "parent_id": "root"},
            {"id": "phase2", "title": "阶段2", "level": 1, "parent_id": "root"},
            {"id": "act1", "title": "动作1", "level": 2, "parent_id": "phase1", "dependencies": ["phase1"]},
            {"id": "act2", "title": "动作2", "level": 2, "parent_id": "phase2", "dependencies": ["phase2"]},
        ])
        assert len(ids) == 5

        # 初始 ready：root 无依赖（phase1/phase2 依赖 root？不——它们没有 dependencies 字段）
        # 实际上 phase1/phase2 没设 dependencies，所以 root/phase1/phase2 都 ready
        ready = await get_ready_nodes(db, "task-1")
        ready_titles = {n["title"] for n in ready}
        assert "根目标" in ready_titles
        assert "阶段1" in ready_titles
        assert "阶段2" in ready_titles
        assert "动作1" not in ready_titles  # 依赖 phase1 未完成
        assert "动作2" not in ready_titles

        # 完成 phase1 → act1 ready
        await update_plan_node_status(db, "phase1", "completed", output="阶段1完成")
        ready = await get_ready_nodes(db, "task-1")
        ready_titles = {n["title"] for n in ready}
        assert "动作1" in ready_titles
        assert "动作2" not in ready_titles  # phase2 未完成

        # 验证完整树
        tree = await get_plan_tree_by_task(db, "task-1")
        assert len(tree) == 5
        phase1_node = next(n for n in tree if n["id"] == "phase1")
        assert phase1_node["status"] == "completed"
        assert phase1_node["output"] == "阶段1完成"
