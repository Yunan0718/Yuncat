"""services/notify.py — Phase 106 统一通知系统

早期设计（OpenHands send_to_user）只通过 EventBus 发布 agent_notification，
Phase 106 改为统一入口：
- 所有通知写入 DB `notifications` 表持久化
- 同时通过 WS 推送到前端
- 提供 list / mark_read / dismiss 等管理方法

失败降级：DB 写入失败仍尝试 WS 推送；WS 失败不抛异常。
"""
import logging
import time as _time
import uuid as _uuid
from dataclasses import dataclass, field

from services.agent.ws_manager import WsManager

_log = logging.getLogger("yuncat.notify")


@dataclass
class Notification:
    agent_id: str
    message: str
    level: str = "info"
    task_id: str = ""
    created_at: float = field(default_factory=_time.time)


class NotificationCenter:
    """Phase 106: 统一通知中心——落盘 + WS 实时推送。"""

    def __init__(self, conn=None, ws_manager: WsManager | None = None):
        self._db = conn
        self._ws = ws_manager

    def set_db(self, conn) -> None:
        self._db = conn

    def set_ws_manager(self, ws_manager: WsManager) -> None:
        self._ws = ws_manager

    # ── 旧 API 兼容 ──
    async def send_to_user(
        self, agent_id: str, message: str,
        level: str = "info", task_id: str = "",
    ) -> None:
        """兼容 Phase 47 OpenHands send_to_user 入口，内部转调统一 notify。"""
        await self.notify(
            title=message[:80],
            body=message,
            type="agent_message",
            level=level,
            task_id=task_id,
            agent_id=agent_id,
        )

    # ── 统一入口 ──
    async def notify(
        self,
        title: str,
        body: str = "",
        type: str = "info",
        level: str = "info",
        link: str | None = None,
        task_id: str = "",
        agent_id: str = "",
    ) -> dict:
        """创建通知：写入 DB + WS 推送。

        Returns:
            {"ok": True, "id": str} 或 {"ok": False, "error": str}
        """
        notif_id = str(_uuid.uuid4())
        ts = int(_time.time())
        result = {"ok": True, "id": notif_id}

        # 1. 写入 DB（失败降级，仍尝试 WS）
        if self._db is not None:
            try:
                await self._db.execute(
                    "INSERT INTO notifications(id, type, title, body, link, read, created_at, level, task_id, agent_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (notif_id, type, title, body or "", link or "", 0, ts, level, task_id, agent_id),
                )
                await self._db.commit()
            except Exception as e:
                _log.warning("Failed to persist notification: %s", e)
                result = {"ok": False, "error": str(e)}

        # 2. WS 推送（失败不抛异常）
        await self._push_ws({
            "type": "notification",
            "id": notif_id,
            "notification_type": type,
            "title": title,
            "body": body or "",
            "link": link or "",
            "level": level,
            "read": False,
            "task_id": task_id,
            "agent_id": agent_id,
            "created_at": ts,
        })

        return result

    async def _push_ws(self, payload: dict) -> None:
        if self._ws is None:
            return
        try:
            task_id = payload.get("task_id")
            if task_id:
                await self._ws.safe_send(task_id, payload)
            else:
                await self._ws.broadcast(payload)
        except Exception:
            _log.debug("WS notification push failed", exc_info=True)

    # ── 查询 ──
    async def list_all(self, limit: int = 50) -> list[dict]:
        if self._db is None:
            return []
        cur = await self._db.execute(
            "SELECT id, type, title, body, link, read, created_at, level, task_id, agent_id "
            "FROM notifications ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [
            {
                "id": r[0], "type": r[1], "title": r[2], "body": r[3],
                "link": r[4], "read": bool(r[5]), "created_at": r[6],
                "level": r[7], "task_id": r[8], "agent_id": r[9],
            }
            for r in rows
        ]

    async def list_unread(self, limit: int = 50) -> list[dict]:
        if self._db is None:
            return []
        cur = await self._db.execute(
            "SELECT id, type, title, body, link, read, created_at, level, task_id, agent_id "
            "FROM notifications WHERE read=0 ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [
            {
                "id": r[0], "type": r[1], "title": r[2], "body": r[3],
                "link": r[4], "read": bool(r[5]), "created_at": r[6],
                "level": r[7], "task_id": r[8], "agent_id": r[9],
            }
            for r in rows
        ]

    # ── 状态管理 ──
    async def mark_read(self, notif_id: str) -> bool:
        if self._db is None:
            return False
        try:
            await self._db.execute(
                "UPDATE notifications SET read=1 WHERE id=?", (notif_id,),
            )
            await self._db.commit()
            return True
        except Exception as e:
            _log.warning("mark_read failed: %s", e)
            return False

    async def mark_all_read(self) -> int:
        if self._db is None:
            return 0
        try:
            cur = await self._db.execute(
                "UPDATE notifications SET read=1 WHERE read=0",
            )
            await self._db.commit()
            return cur.rowcount
        except Exception as e:
            _log.warning("mark_all_read failed: %s", e)
            return 0

    async def dismiss(self, notif_id: str) -> bool:
        if self._db is None:
            return False
        try:
            await self._db.execute(
                "DELETE FROM notifications WHERE id=?", (notif_id,),
            )
            await self._db.commit()
            return True
        except Exception as e:
            _log.warning("dismiss failed: %s", e)
            return False

    async def clear_read(self) -> int:
        if self._db is None:
            return 0
        try:
            cur = await self._db.execute(
                "DELETE FROM notifications WHERE read=1",
            )
            await self._db.commit()
            return cur.rowcount
        except Exception as e:
            _log.warning("clear_read failed: %s", e)
            return 0

    # ── 兼容旧方法 ──
    async def get_pending(self) -> list[dict]:
        return await self.list_unread()

    async def load_from_disk(self) -> list[dict]:
        """Phase 106: 不再从磁盘加载，改为从 DB 读取未读通知。"""
        return await self.list_unread()
