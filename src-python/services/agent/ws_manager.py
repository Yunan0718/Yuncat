"""services/agent/ws_manager.py —— AgentService WebSocket 连接管理拆分。

Phase 52-N / 架构债务：将 WS 注册、心跳、推送能力从 agent_service.py 抽出，
降低 agent_service 复杂度并保持 API 兼容。

P2-4 修复: 所有 WS send 路径加 asyncio.wait_for 超时（WS_SEND_TIMEOUT_SEC），
避免慢客户端阻塞整个 task 推送。对标 FastAPI/aiohttp WS 最佳实践。
"""
import asyncio
import logging
import time as _time

from settings import settings as _settings
from config import WS_SEND_TIMEOUT_SEC  # P2-4: WS send 超时

_log = logging.getLogger("yuncat.agent.ws")


class WsManager:
    """管理 task_id → WebSocket 连接的映射与心跳。"""

    def __init__(self):
        self._connections: dict[str, list] = {}  # task_id → [ws, ...]
        self._last_active: dict[int, float] = {}  # id(ws) → last_active_ts

    # ---- 注册 / 注销 ----

    def register_ws(self, task_id: str, ws) -> None:
        """注册 WS 连接——用于流式推送"""
        self._connections.setdefault(task_id, []).append(ws)
        self._last_active[id(ws)] = _time.time()

    def unregister_ws(self, ws) -> None:
        """移除已断开的 WS 连接"""
        ws_id = id(ws)
        for task_id, conns in list(self._connections.items()):
            self._connections[task_id] = [c for c in conns if c != ws]
            if not self._connections[task_id]:
                del self._connections[task_id]
        self._last_active.pop(ws_id, None)

    def all_connections(self) -> list:
        """获取所有 WS 连接（去重）。"""
        seen: set[int] = set()
        result: list = []
        for conns in self._connections.values():
            for ws in conns:
                if id(ws) not in seen:
                    seen.add(id(ws))
                    result.append(ws)
        return result

    def pop_task_connections(self, task_id: str) -> list:
        """取出并移除某 task 的全部 WS 连接（用于 cancel_task 清理）。"""
        return self._connections.pop(task_id, []) or []

    # ---- 心跳 ----

    async def heartbeat_tick(self) -> None:
        """WS 心跳——由 LoopSupervisor 定期调用。

        1. 向所有活跃连接发送 ping
        2. 清理超过配置时间未响应的死连接

        P2-4: ping send 加 wait_for 超时。
        """
        now = _time.time()
        timeout = _settings.ws_heartbeat_timeout_sec
        dead_ws: list = []
        for ws_conn in self.all_connections():
            ws_id = id(ws_conn)
            last = self._last_active.get(ws_id, now)
            if now - last > timeout:
                dead_ws.append(ws_conn)
                continue
            try:
                await asyncio.wait_for(
                    ws_conn.send_json({"type": "ping", "ts": now}),
                    timeout=WS_SEND_TIMEOUT_SEC,
                )
                self._last_active[ws_id] = now
            except (asyncio.TimeoutError, Exception):
                dead_ws.append(ws_conn)
        for ws in dead_ws:
            _log.info("WS heartbeat: removing dead connection %s", id(ws))
            self.unregister_ws(ws)

    # ---- 推送 ----

    async def safe_send(self, task_id: str, payload: dict) -> None:
        """向指定 task 的所有 WS 连接发送消息，自动清理失效连接。

        P2-4: 加 asyncio.wait_for 超时——慢客户端不再阻塞后续连接的推送。
        """
        dead: list = []
        for ws_conn in self._connections.get(task_id, []):
            try:
                await asyncio.wait_for(
                    ws_conn.send_json(payload), timeout=WS_SEND_TIMEOUT_SEC,
                )
                self._last_active[id(ws_conn)] = _time.time()
            except (asyncio.TimeoutError, Exception):
                # P2-4: 超时或发送失败都视为死连接，立即清理
                dead.append(ws_conn)
        for d in dead:
            try:
                self._connections[task_id].remove(d)
            except (ValueError, KeyError):
                pass
            self._last_active.pop(id(d), None)
        if task_id in self._connections and not self._connections[task_id]:
            self._connections.pop(task_id, None)

    async def push_stream_chunk(self, task_id: str, data: str, index: int) -> None:
        await self.safe_send(task_id, {
            "type": "stream_chunk", "task_id": task_id, "data": data, "index": index,
        })

    async def push_stream_done(self, task_id: str) -> None:
        await self.safe_send(task_id, {"type": "stream_done", "task_id": task_id})

    async def push_hitl_request(self, run_id: str, node_id: str, prompt: str, timeout: int,
                                  task_id: str = "") -> None:
        """Phase 47: HITL 审批请求——仅推送到相关 task 的 WS 连接

        P2-4: send 加 wait_for 超时。
        """
        targets = self._connections.get(task_id, []) if task_id else []
        if not targets:
            targets = [ws for conns in self._connections.values() for ws in conns]
        for ws in targets:
            try:
                await asyncio.wait_for(
                    ws.send_json({"type": "workflow_hitl", "run_id": run_id,
                                  "node_id": node_id, "prompt": prompt, "timeout": timeout}),
                    timeout=WS_SEND_TIMEOUT_SEC,
                )
            except (asyncio.TimeoutError, Exception):
                _log.warning("WS push_hitl failed", exc_info=False)

    async def push_tool_confirm(self, task_id: str, tool_name: str, preview: dict,
                                confirm_token: str, expires_at: float = 0.0,
                                task_prompt: str = "") -> None:
        """Phase 47: 工具确认弹窗——仅推送到发起 task 的 WS 连接

        Batch 4b: payload 增加 expires_at（epoch 秒，前端倒计时/过期过滤）
        与 task_prompt（来源任务 input 前 80 字符，供用户做高危决策的上下文）。
        """
        await self.safe_send(task_id, {
            "type": "tool_confirm_required",
            "task_id": task_id,
            "tool_name": tool_name,
            "preview": preview,
            "confirm_token": confirm_token,
            "expires_at": expires_at,
            "task_prompt": task_prompt,
        })

    async def push_tool_confirm_batch(
        self, task_id: str, items: list[dict], task_prompt: str = "",
    ) -> None:
        """Batch 3.2: 批量工具确认弹窗——仅推送到发起 task 的 WS 连接

        Batch 4b: items 内各项带 expires_at；payload 顶层带 task_prompt。
        """
        await self.safe_send(task_id, {
            "type": "tool_confirm_batch_required",
            "task_id": task_id,
            "count": len(items),
            "items": items,
            "task_prompt": task_prompt,
        })

    async def broadcast(self, payload: dict) -> None:
        """Phase 106: 向所有 WS 连接广播消息。

        P2-4: send 加 wait_for 超时。
        """
        dead: list = []
        for task_id, conns in list(self._connections.items()):
            for ws_conn in conns:
                try:
                    await asyncio.wait_for(
                        ws_conn.send_json(payload), timeout=WS_SEND_TIMEOUT_SEC,
                    )
                    self._last_active[id(ws_conn)] = _time.time()
                except (asyncio.TimeoutError, Exception):
                    dead.append((task_id, ws_conn))
        for task_id, ws_conn in dead:
            try:
                self._connections[task_id].remove(ws_conn)
            except (ValueError, KeyError):
                pass
            self._last_active.pop(id(ws_conn), None)
            if task_id in self._connections and not self._connections[task_id]:
                self._connections.pop(task_id, None)

    async def push_error(self, task_id: str, error: str) -> None:
        await self.safe_send(task_id, {"type": "error", "task_id": task_id, "error": error})

    async def push_agent_status(self, agent_id: str, status: str, reason: str = "") -> None:
        """Phase 48: 广播 agent_status 事件到所有 WS 连接。

        P2-4: send 加 wait_for 超时。
        reason: 状态为 config_error 时携带可执行的修复说明（如「请在设置里重填 API Key」），
        让界面能直接告诉用户怎么办，而不是只显示一个不健康图标。
        """
        payload = {"type": "agent_status", "agent_id": agent_id, "status": status}
        if reason:
            payload["reason"] = reason
        for ws_conn in self.all_connections():
            try:
                await asyncio.wait_for(
                    ws_conn.send_json(payload), timeout=WS_SEND_TIMEOUT_SEC,
                )
                self._last_active[id(ws_conn)] = _time.time()
            except (asyncio.TimeoutError, Exception):
                _log.warning("push_agent_status WS send failed", exc_info=False)
