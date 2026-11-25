"""services/memory_sessions.py —— 会话与消息管理（从 memory_service.py 拆分）

包含: add_message / get_session_context / _evict_sessions / _get_session_lock
      / get_conversation / estimate_tokens

附录 K 写穿透：先写 DB，再更新 _sessions 内存缓存。
"""
import asyncio
import logging
import time as _time

from services.memory.tool_message_codec import decode_tool_meta, encode_tool_meta

_log = logging.getLogger("yuncat.memory")


async def add_message(self, session_id: str, role: str, content: str, agent_id: str | None = None, token_count: int = 0, tool_calls: list[dict] | None = None, reasoning: str | None = None, tool_call_id: str | None = None) -> dict:
    """v66: 单一入口——先写 DB（含 agent_id + token_count + tool_calls_json），再更新内存缓存。
    新会话首条消息时触发一次轻量 Dream（异步，不阻塞）。
    每 5 条消息批量 commit，高频对话时减少 80% 磁盘 IO。
    Phase 47: tool_calls JSON 持久化——重启后保留工具调用结构。
    """
    from repositories import insert_conversation
    tc_json = encode_tool_meta(tool_calls, tool_call_id)
    conv_id = await insert_conversation(self.db, session_id, role, content, agent_id=agent_id, token_count=token_count, tool_calls_json=tc_json, reasoning_content=reasoning)
    msg = {"id": conv_id, "role": role, "content": content, **({"reasoning_content": reasoning} if reasoning else {}), **({"tool_call_id": tool_call_id} if tool_call_id else {})}
    if tool_calls:
        msg["tool_calls"] = tool_calls

    # v66: 批量 commit——每 5 条消息或 assistant 回复后提交
    # Batch 5a C8: user 消息也立即 commit——持久化一致性优先（tasks.prompt 独立 commit，
    # 若 user 消息凑满 5 条才提交，流式中途崩溃会丢失最近 user 消息）。assistant 流式聚合路径不变。
    # D1: commit 失败时必须 rollback + 重置计数器，避免后续写入状态错乱
    self._pending_writes = getattr(self, '_pending_writes', 0) + 1
    if self._pending_writes >= 5 or role in ("assistant", "user", "tool"):
        try:
            await self.db.commit()
        except Exception:
            # commit 失败——回滚事务避免连接进入错误状态，重置计数器让后续写入能正常工作
            try:
                await self.db.rollback()
            except Exception as rb_err:
                _log.warning("rollback after commit failure also failed: %s", rb_err)
            self._pending_writes = 0
            raise
        self._pending_writes = 0
        # Task 6.6: 事件驱动即时提取——assistant 回复提交后发布事件，
        # MemoryTree 订阅后 debounce 触发 index_from_conversations。
        # 对标 Claude Code 每轮即时提取；15min background_loop 仍作兜底。
        if role == "assistant":
            try:
                from services.event_bus import get_event_bus, Events
                await get_event_bus().publish(
                    Events.ASSISTANT_TURN_COMPLETE,
                    {"session_id": session_id},
                )
            except Exception:
                _log.debug("Failed to publish assistant_turn_complete", exc_info=True)

    # v44: 原子获取或创建会话锁
    async with self._global_session_lock:
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
    # v49: 先独立锁做全局清理
    now = _time.time()
    async with self._evict_lock:
        self._evict_sessions(now)

    is_new_session = session_id not in self._sessions

    async with self._session_locks[session_id]:
        if is_new_session:
            self._sessions[session_id] = []
        self._sessions[session_id].append(msg)
        self._session_last_access[session_id] = now
        cache = self._sessions[session_id]
        if len(cache) > self._max_turns * 2:
            self._sessions[session_id] = cache[-(self._max_turns * 2):]

    # v65: 新会话时异步触发 Dream——让 AI 在对话开始前获取最新知识
    if is_new_session and self._summarizer and self._memory_repo:
        asyncio.create_task(self._try_dream_on_new_session())

    return msg


async def get_session_context(self, session_id: str) -> list[dict]:
    """获取会话上下文——先查缓存，缓存 miss 时查 DB"""
    async with self._global_session_lock:
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
    async with self._session_locks[session_id]:
        self._session_last_access[session_id] = _time.time()
        if session_id in self._sessions and self._sessions[session_id]:
            return list(self._sessions[session_id][-self._max_turns * 2:])
    # 缓存 miss → 从 DB 加载
    from repositories import get_conversation
    from repositories.compaction_repo import get_compaction_checkpoint
    import json as _json
    checkpoint = await get_compaction_checkpoint(self.db, session_id) if self.db is not None else None
    rows = await get_conversation(self.db, session_id, self._max_turns * 2)
    loaded: list[dict] = []
    for r in rows:
        msg = {"id": r["id"], "role": r["role"], "content": r["content"]}
        # Phase 47: 还原 tool_calls / tool_call_id（tool 行复用同一列存信封）
        tc_json = r.get("tool_calls_json", "")
        if tc_json:
            tool_call_id, tool_calls = decode_tool_meta(tc_json)
            if tool_call_id:
                msg["tool_call_id"] = tool_call_id
            elif tool_calls is not None:
                msg["tool_calls"] = tool_calls
            else:
                _log.warning("Failed to restore tool metadata for session %s", session_id)
        # 还原 reasoning_content
        rc = r.get("reasoning_content", "")
        if rc:
            msg["reasoning_content"] = rc
        loaded.append(msg)
    if checkpoint:
        source_ids = set(checkpoint["source_message_ids"])
        loaded = [message for message in loaded if message.get("id") not in source_ids]
        from services.memory.memory_compact import _format_compact_boundary
        loaded.insert(0, {
            "id": f"compact:{checkpoint['checkpoint_id']}", "role": "system",
            "content": _format_compact_boundary(checkpoint["summary"]),
        })
    async with self._session_locks[session_id]:
        self._sessions[session_id] = loaded
        return list(self._sessions[session_id])


def _evict_sessions(self, now: float) -> None:
    """v49: LRU 淘汰 + 闲置过期——持独立 _evict_lock。

    不删除 DB 记录，仅释放内存缓存。
    """
    # 1. 清理闲置超过 _session_idle_ttl 的会话
    stale = [
        sid for sid, ts in self._session_last_access.items()
        if now - ts > self._session_idle_ttl
    ]
    for sid in stale:
        self._sessions.pop(sid, None)
        self._summaries.pop(sid, None)
        self._session_last_access.pop(sid, None)
        _release_session_lock(self, sid)
    # 2. 如果仍超限，淘汰最久未访问的
    if len(self._sessions) > self._max_active_sessions:
        sorted_sids = sorted(
            self._session_last_access.keys(),
            key=lambda k: self._session_last_access[k],
        )
        to_remove = sorted_sids[:len(self._sessions) - self._max_active_sessions]
        for sid in to_remove:
            self._sessions.pop(sid, None)
            self._summaries.pop(sid, None)
            self._session_last_access.pop(sid, None)
            _release_session_lock(self, sid)


def _release_session_lock(self, sid: str) -> None:
    """Batch 5a C3: 会话淘汰时释放未持有的锁——避免 _session_locks 无限增长。

    仅当 lock 未被持有时 pop；持锁中跳过，下次 evict 再清，防止并发协程
    拿到被删除的锁后写内存缓存出错。
    """
    lock = self._session_locks.get(sid)
    if lock is not None and not lock.locked():
        self._session_locks.pop(sid, None)


async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
    """获取或创建会话锁"""
    async with self._global_session_lock:
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
    return self._session_locks[session_id]


async def get_conversation(self, session_id: str, limit: int = 50) -> list[dict]:
    from repositories import get_conversation
    return await get_conversation(self.db, session_id, limit)


async def estimate_tokens(self, messages: list[dict]) -> int:
    """统一 token 计数入口（vendor API → tiktoken → CJK）。"""
    from config import count_tokens
    counted = await count_tokens(messages, model_cfg=getattr(self, "model_config", None))
    return counted["tokens"]


async def flush_pending(self) -> None:
    """D1: 显式刷新所有待提交的会话写入。

    在跨会话操作（summarization / dream / compact）前调用，确保 DB 状态持久化。
    commit 失败时回滚 + 重置计数器，与 add_message 的批量 commit 错误处理保持一致。
    """
    if self._pending_writes <= 0:
        return
    try:
        await self.db.commit()
    except Exception:
        try:
            await self.db.rollback()
        except Exception as rb_err:
            _log.warning("rollback after flush_pending commit failure also failed: %s", rb_err)
        self._pending_writes = 0
        raise
    self._pending_writes = 0
