"""repositories/memory_repo.py —— 记忆仓库（Dream 状态管理 + 事实操作）

v41: Auto-Dream 依赖的 Repository 方法。
"""
import logging

_log = logging.getLogger("yuncat.memory_repo")


class MemoryRepository:
    """Auto-Dream 专用仓库——dream_state 管理 + 事实 CRUD"""

    def __init__(self, db):
        self.db = db

    # ============================================================
    # Dream 状态管理（dream_state 表，singleton row id=1）
    # ============================================================
    async def get_dream_state(self, key: str) -> float | None:
        """读取 dream 状态字段值"""
        column_map = {
            "last_dream_time": "last_dream_time",
            "dream_lock": "dream_lock",
            "total_dreams": "total_dreams",
        }
        col = column_map.get(key)
        if col is None:
            _log.warning("get_dream_state rejected unknown key=%r", key)
            return None
        cursor = await self.db.execute(f"SELECT {col} FROM dream_state WHERE id=1")
        row = await cursor.fetchone()
        if not row or row[0] is None:
            return None
        return float(row[0])

    async def set_dream_state(self, key: str, value: float) -> None:
        """写入 dream 状态字段"""
        column_map = {
            "last_dream_time": "last_dream_time",
            "dream_lock": "dream_lock",
            "total_dreams": "total_dreams",
        }
        col = column_map.get(key)
        if col is None:
            _log.warning("set_dream_state rejected unknown key=%r", key)
            return
        await self.db.execute(f"UPDATE dream_state SET {col}=? WHERE id=1", (value,))
        await self.db.commit()

    async def acquire_dream_lock(self) -> bool:
        """尝试获取 Dream 排他锁。

        使用 SQLite UPDATE 的原子性——只有当前锁为 0 时才设置为 1。
        返回 True 表示成功获取。
        """
        cursor = await self.db.execute(
            "UPDATE dream_state SET dream_lock=1 WHERE id=1 AND dream_lock=0"
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def release_dream_lock(self) -> None:
        """释放 Dream 排他锁"""
        await self.db.execute("UPDATE dream_state SET dream_lock=0 WHERE id=1")
        await self.db.commit()

    # ============================================================
    # 事实操作
    # ============================================================
    async def count_conversations_since(self, since_ts: float | None) -> int:
        """统计自某时间戳以来的新增对话轮次"""
        if since_ts is None:
            since_ts = 0
        cursor = await self.db.execute(
            "SELECT COUNT(*) FROM conversations WHERE created_at > ?", (int(since_ts),)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_unconsolidated_facts(self, limit: int = 100) -> list[dict]:
        """获取未整合的记忆（verified=0 且 confidence < 0.8）"""
        cursor = await self.db.execute(
            "SELECT id, content, source, owner, confidence, memory_type, verified "
            "FROM memories WHERE verified=0 AND confidence < 0.8 "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0], "content": row[1], "source": row[2],
                "owner": row[3], "confidence": row[4], "memory_type": row[5],
                "verified": bool(row[6]),
            }
            for row in rows
        ]

    async def update_confidence(self, mem_id: str, confidence: float) -> None:
        """更新记忆置信度"""
        await self.db.execute(
            "UPDATE memories SET confidence=? WHERE id=?", (confidence, mem_id)
        )
        await self.db.commit()

    async def save_semantic(
        self, content: str, source: str, owner: str,
        confidence: float = 0.5, memory_type: str = "fact", verified: bool = False,
    ) -> str:
        """写入语义记忆"""
        import uuid as _uuid
        mem_id = str(_uuid.uuid4())
        await self.db.execute(
            "INSERT INTO memories(id, content, source, owner, confidence, memory_type, verified) "
            "VALUES(?,?,?,?,?,?,?)",
            (mem_id, content, source, owner, confidence, memory_type, 1 if verified else 0),
        )
        await self.db.commit()
        return mem_id

    async def delete_low_confidence_orphans(self, threshold: float = 0.3) -> int:
        """删除低置信度且无标签的孤立记忆。返回删除数。"""
        cursor = await self.db.execute(
            "DELETE FROM memories WHERE confidence < ? AND verified=0 "
            "AND id NOT IN (SELECT DISTINCT memory_id FROM memory_tags)",
            (threshold,),
        )
        await self.db.commit()
        return cursor.rowcount

    async def find_conflicts(self, limit: int = 10) -> list[dict]:
        """检测潜在冲突的记忆对。

        复用 repositories.memories_repo 中的 find_memory_conflicts 实现，
        把模块级能力封装到 MemoryRepository 接口，供 MemoryService 统一调用。
        """
        from repositories.memories_repo import find_memory_conflicts

        return await find_memory_conflicts(self.db, limit)
