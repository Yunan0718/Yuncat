"""repositories/plan_step_repo.py —— Phase 77 长任务分解树 CRUD

plan_steps 表存储长任务的分级分解树（level 0-2），与 tasks.parent_task_id
子任务机制并存：
- plan_steps：规划分解树节点（目标→阶段→动作，带依赖和验收标准）
- tasks：可执行运行单元（plan_steps.task_id 关联根 task）

函数命名用 plan_node 前缀，避免与 tasks_repo.create_plan_steps（操作 tasks 表）混淆。
"""
import json as _json
import time as _time
import uuid as _uuid


def _gen_id() -> str:
    return str(_uuid.uuid4())


def _ensure_list(val) -> list:
    """确保返回 list——val 是 list 则原样返回，否则 json.loads 或返回 []"""
    if isinstance(val, list):
        return val
    if isinstance(val, str) and val:
        try:
            parsed = _json.loads(val)
            return parsed if isinstance(parsed, list) else []
        except (_json.JSONDecodeError, TypeError):
            return []
    return []


async def create_plan_node(conn, node: dict) -> str:
    """插入单个分解树节点，返回 node_id

    Args:
        node: 节点字段 dict，必含 task_id + title；可选 level/parent_id/
              dependencies/acceptance_criteria/description/agent_id/id
              - id 不提供则自动生成 uuid
              - level 不提供默认 0
              - dependencies/acceptance_criteria 是 list 或 JSON 字符串
    """
    nid = node.get("id") or _gen_id()
    dependencies = _ensure_list(node.get("dependencies", []))
    acceptance_criteria = _ensure_list(node.get("acceptance_criteria", []))
    await conn.execute(
        "INSERT INTO plan_steps("
        "id, task_id, parent_id, level, title, description, "
        "dependencies, acceptance_criteria, agent_id, output, status, review, "
        "created_at, updated_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            nid,
            node["task_id"],
            node.get("parent_id"),
            int(node.get("level", 0)),
            node["title"],
            node.get("description", ""),
            _json.dumps(dependencies, ensure_ascii=False),
            _json.dumps(acceptance_criteria, ensure_ascii=False),
            node.get("agent_id"),
            node.get("output", ""),
            node.get("status", "pending"),
            node.get("review", "{}"),
            int(_time.time()),
            int(_time.time()),
        ),
    )
    await conn.commit()
    return nid


async def create_plan_tree(conn, task_id: str, nodes: list[dict]) -> list[str]:
    """批量创建分解树节点

    Args:
        task_id: 根任务 id（自动注入到每个节点的 task_id 字段）
        nodes: 节点 dict 列表，字段同 create_plan_node（task_id 可省略）

    Returns:
        node_id 列表（顺序与输入一致）
    """
    ids: list[str] = []
    for node in nodes:
        node_dict = dict(node)
        node_dict.setdefault("task_id", task_id)
        nid = await create_plan_node(conn, node_dict)
        ids.append(nid)
    return ids


async def get_plan_tree_by_task(conn, task_id: str) -> list[dict]:
    """返回某根任务的所有分解节点（按 level、created_at 排序）

    JSON 字段（dependencies/acceptance_criteria/review）自动反序列化为 Python 对象。
    """
    cursor = await conn.execute(
        "SELECT id, task_id, parent_id, level, title, description, "
        "dependencies, acceptance_criteria, agent_id, output, step_output, status, review, "
        "created_at, updated_at "
        "FROM plan_steps WHERE task_id=? "
        "ORDER BY level ASC, created_at ASC",
        (task_id,)
    )
    rows = await cursor.fetchall()
    result: list[dict] = []
    for r in rows:
        result.append({
            "id": r[0],
            "task_id": r[1],
            "parent_id": r[2],
            "level": r[3],
            "title": r[4],
            "description": r[5],
            "dependencies": _ensure_list(r[6]),
            "acceptance_criteria": _ensure_list(r[7]),
            "agent_id": r[8],
            "output": r[9],
            "step_output": _json.loads(r[10]) if r[10] else {},
            "status": r[11],
            "review": _json.loads(r[12]) if r[12] else {},
            "created_at": r[13],
            "updated_at": r[14],
        })
    return result


async def update_plan_node_status(
    conn, node_id: str, status: str, output: str | None = None,
    step_output: dict | None = None,
    expected_from: str | None = None,
) -> bool | None:
    """更新节点状态（可选更新 output / step_output）

    D4: expected_from 提供时走乐观锁——仅当当前状态匹配时才转移。
    状态不匹配（affected_rows=0）时返回 False，不覆盖已有状态/output。
    expected_from=None（默认）时无条件 UPDATE，返回 None（向后兼容）。

    Args:
        node_id: 节点 id
        status: pending/running/completed/failed/blocked/skipped
        output: 完成后的结果摘要（None 则不更新 output 字段）
        step_output: Phase 108 结构化输出 dict（None 则不更新）
        expected_from: 期望的当前状态，提供时走乐观锁

    Returns:
        True=转移成功, False=乐观锁跳过, None=无条件 UPDATE（向后兼容）
    """
    if expected_from:
        where_clause = "WHERE id=? AND status=?"
        params_tail = (node_id, expected_from)
    else:
        where_clause = "WHERE id=?"
        params_tail = (node_id,)

    updates = ["status=?", "updated_at=?"]
    params: list = [status, int(_time.time())]
    if output is not None:
        updates.append("output=?")
        params.append(output)
    if step_output is not None:
        updates.append("step_output=?")
        params.append(_json.dumps(step_output, ensure_ascii=False))

    cursor = await conn.execute(
        f"UPDATE plan_steps SET {', '.join(updates)} {where_clause}",
        (*params, *params_tail),
    )
    await conn.commit()

    # D4: 乐观锁——affected_rows=0 表示状态已被其他进程改变
    if expected_from:
        return cursor.rowcount > 0
    return None


async def delete_pending_subtree(conn, task_id: str, node_id: str) -> int:
    """递归删除 node_id 及其所有 pending 子孙节点

    completed/failed/skipped 节点保留（保留历史记录）。
    仅删除 status='pending' 的节点。

    Returns:
        实际删除的节点数量
    """
    # 收集所有子孙节点（BFS）
    to_check: list[str] = [node_id]
    all_descendants: list[str] = []
    while to_check:
        current = to_check.pop()
        cursor = await conn.execute(
            "SELECT id FROM plan_steps WHERE task_id=? AND parent_id=?",
            (task_id, current),
        )
        children = [r[0] for r in await cursor.fetchall()]
        for child_id in children:
            if child_id not in all_descendants:
                all_descendants.append(child_id)
                to_check.append(child_id)

    # 候选删除集 = node_id 本身 + 所有子孙
    candidates = [node_id] + all_descendants
    deleted = 0
    for cid in candidates:
        cursor = await conn.execute(
            "DELETE FROM plan_steps WHERE id=? AND status='pending'",
            (cid,),
        )
        deleted += cursor.rowcount
    await conn.commit()
    return deleted


async def delete_pending_children(conn, task_id: str, parent_id: str) -> int:
    """Phase 79: 删除 parent_id 的所有 pending 子孙节点，但保留 parent_id 本身

    用于 regenerate_subtree——重新分解某节点子树时，保留该节点（不管状态），
    只清空其下属的 pending 子树。completed/failed/skipped 子孙节点保留。

    与 delete_pending_subtree 的区别：本函数不删 parent_id 本身。

    Returns:
        实际删除的节点数量
    """
    # BFS 收集 parent_id 的所有子孙（不含 parent_id 本身）
    to_check: list[str] = [parent_id]
    all_descendants: list[str] = []
    while to_check:
        current = to_check.pop()
        cursor = await conn.execute(
            "SELECT id FROM plan_steps WHERE task_id=? AND parent_id=?",
            (task_id, current),
        )
        children = [r[0] for r in await cursor.fetchall()]
        for child_id in children:
            if child_id not in all_descendants:
                all_descendants.append(child_id)
                to_check.append(child_id)

    deleted = 0
    for cid in all_descendants:
        cursor = await conn.execute(
            "DELETE FROM plan_steps WHERE id=? AND status='pending'",
            (cid,),
        )
        deleted += cursor.rowcount
    await conn.commit()
    return deleted


async def get_pending_leaves(conn, task_id: str) -> list[dict]:
    """返回 status='pending' 且没有子节点的叶子节点

    叶子定义：没有其他节点的 parent_id 指向它。
    用于执行器挑选最细粒度的待执行动作。
    """
    cursor = await conn.execute(
        "SELECT id, task_id, parent_id, level, title, description, "
        "dependencies, acceptance_criteria, agent_id, output, status, review, "
        "created_at, updated_at "
        "FROM plan_steps WHERE task_id=? AND status='pending' "
        "AND id NOT IN (SELECT parent_id FROM plan_steps "
        "               WHERE task_id=? AND parent_id IS NOT NULL) "
        "ORDER BY level ASC, created_at ASC",
        (task_id, task_id),
    )
    rows = await cursor.fetchall()
    result: list[dict] = []
    for r in rows:
        result.append({
            "id": r[0], "task_id": r[1], "parent_id": r[2], "level": r[3],
            "title": r[4], "description": r[5],
            "dependencies": _ensure_list(r[6]),
            "acceptance_criteria": _ensure_list(r[7]),
            "agent_id": r[8], "output": r[9], "status": r[10],
            "review": _json.loads(r[11]) if r[11] else {},
            "created_at": r[12], "updated_at": r[13],
        })
    return result


async def get_ready_nodes(conn, task_id: str) -> list[dict]:
    """返回 status='pending' 且 dependencies 全部 completed 的节点

    dependencies 为空的 pending 节点也算 ready（无依赖立即可执行）。
    用于调度器挑选可执行节点。
    """
    all_nodes = await get_plan_tree_by_task(conn, task_id)
    completed_ids = {n["id"] for n in all_nodes if n["status"] == "completed"}
    ready: list[dict] = []
    for node in all_nodes:
        if node["status"] != "pending":
            continue
        deps = node["dependencies"]
        # 依赖全部 completed（空依赖视为已满足）
        if all(dep_id in completed_ids for dep_id in deps):
            ready.append(node)
    return ready
