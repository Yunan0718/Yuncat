"""services/analytics_stats.py —— 仪表盘统计的聚合出口

仪表盘要的几个数字（任务状态分布、会话消息数、计划步骤完成度）原先直接写成
routes/system.py 里的 SQL。路由该做参数与响应组装，聚合该留在服务层；更要紧的是
"前端读什么字段"和"后端给什么字段"各自演化，对不上时界面只会静默显示 0——
2026-09-17 的仪表盘就是这样：消息数恒为 0、成功率恒为 0%、饼图恒为空。

所以这里收成一处：谁要给仪表盘出数，都从这里拿。
"""


async def task_status_counts(db) -> dict[str, int]:
    """任务按状态计数——total / completed / failed / running 都从这里派生"""
    cursor = await db.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")
    return {row[0] or "unknown": row[1] for row in await cursor.fetchall()}


async def conversation_message_count(db) -> int:
    """会话消息总数（仪表盘"消息数"那个 KPI 的口径）"""
    cursor = await db.execute("SELECT COUNT(*) FROM conversations")
    return (await cursor.fetchone())[0]


async def plan_step_stats(db) -> dict:
    """长任务计划步骤统计——数据源 plan_steps（不是拿 tasks 的文本去 LIKE）"""
    cursor = await db.execute("SELECT status, COUNT(*) FROM plan_steps GROUP BY status")
    by_status = {row[0] or "unknown": row[1] for row in await cursor.fetchall()}
    total = sum(by_status.values())
    completed = by_status.get("completed", 0)
    return {
        "total": total, "completed": completed,
        "completion_rate": round(completed * 100 / total) if total else 0,
        "by_status": by_status,
    }
