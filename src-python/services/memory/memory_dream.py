"""services/memory_dream.py —— Auto-Dream 后台记忆整合（从 memory_service.py 拆分）

包含: _get_dream_params / auto_dream / _consolidate_memories
      / _try_dream_on_new_session / _scan_openclaw_sessions
      / _consolidate_with_oc_sessions

v41: Auto-Dream 后台记忆整合——24h/50轮/锁 三重门控。
v65: Dream 频率可由用户在设置中调整。
"""
import asyncio
import glob as _glob
import json as _json
import logging
import time as _time
from pathlib import Path

from config import (
    MEMORY_DREAM_FACTS_LIMIT, MEMORY_DREAM_OC_FACTS_LIMIT,
    MEMORY_DREAM_TITLE_PREVIEW_CHARS, MEMORY_DREAM_CONTENT_MAX_CHARS,
    MEMORY_DREAM_FACT_CONTENT_PREVIEW_CHARS, MEMORY_DREAM_OC_FACT_CONTENT_PREVIEW_CHARS,
    MEMORY_DREAM_OC_FACT_MIN_CHARS,
)

_log = logging.getLogger("yuncat.memory")


async def _get_dream_params(self) -> dict:
    """从 settings 表读取 Dream 频率配置。未设置时使用默认值（standard）。"""
    DREAM_FREQUENCY_PRESETS = {
        "aggressive": {"check_interval": 300, "time_gate": 600, "min_turns": 10},
        "standard":   {"check_interval": 600, "time_gate": 1800, "min_turns": 15},
        "relaxed":    {"check_interval": 1800, "time_gate": 3600, "min_turns": 30},
    }
    try:
        cursor = await self.db.execute(
            "SELECT value FROM settings WHERE key='dream_frequency'",
        )
        row = await cursor.fetchone()
        preset_name = row[0] if row else "aggressive"
    except Exception:
        preset_name = "aggressive"
    return DREAM_FREQUENCY_PRESETS.get(preset_name, DREAM_FREQUENCY_PRESETS["standard"])


async def auto_dream(self):
    """v65: 后台记忆整合——频率可由用户在设置中调整。

    每 check_interval 秒检查一次。短周期确保用户在密集编码会话中
    产生的知识不会因开新对话而丢失。每次整合约消耗 500 tokens（￥0.0005）。
    """
    if not self._memory_repo or not self._summarizer:
        return
    while True:
        params = await self._get_dream_params()
        await asyncio.sleep(params["check_interval"])
        now = _time.time()

        # 门控 1: 距上次整合太近 → 跳过
        last_dream = await self._memory_repo.get_dream_state("last_dream_time")
        if last_dream and now - last_dream < params["time_gate"]:
            continue

        # 门控 2: 新增对话轮次不够 → 跳过
        new_turns = await self._memory_repo.count_conversations_since(last_dream or 0)
        if new_turns < params["min_turns"]:
            continue

        # 门控 3: 排他锁
        if not await self._memory_repo.acquire_dream_lock():
            continue

        try:
            await self._consolidate_memories()
            # Phase 43 F2.3: OC 会话事实注入
            await self._consolidate_with_oc_sessions()
            await self._memory_repo.set_dream_state("last_dream_time", now)
            self._dream_failures = 0
        except Exception as e:
            self._dream_failures += 1
            _log.warning(
                f"Auto-Dream consolidation failed (#{self._dream_failures}): {e}"
            )
            # Phase 38: 递增退避——连续失败越多等待越久 (1h→2h→...→24h)
            backoff = 3600 * min(self._dream_failures + 1, 24)
            await self._memory_repo.set_dream_state("last_dream_time", now - backoff)
        finally:
            await self._memory_repo.release_dream_lock()


async def _consolidate_memories(self):
    """整合步骤：
    1. 提取未整理事实 → LLM 去重合并
    2. 模糊表述 → 固化
    3. 清理低置信度孤立记忆
    4. 冲突检测
    """
    raw_facts = await self._memory_repo.get_unconsolidated_facts(limit=MEMORY_DREAM_FACTS_LIMIT)
    if not raw_facts:
        return

    # 调用 LLM 去重合并
    from prompts.memory_context import DREAM_CONSOLIDATE_PROMPT
    facts_text = "\n".join(
        f"- [{f['confidence']:.0%}] {f['content']}" for f in raw_facts
    )
    prompt = DREAM_CONSOLIDATE_PROMPT.format(facts=facts_text)

    try:
        consolidated = await self._summarizer(prompt)
    except Exception:
        _log.exception("Dream LLM call failed")
        return

    # 旧事实降权
    for fact in raw_facts:
        await self._memory_repo.update_confidence(fact["id"], fact["confidence"] * 0.5)

    # 写入整合结果 — 同步到 knowledge_nodes(Commander检索) 和 memories(Auto-Dream源)
    if consolidated.strip():
        await self._memory_repo.save_semantic(
            consolidated, "auto_dream", "system", 0.8, "fact", verified=True,
        )
        # Phase 34: 同步到 knowledge_nodes — Commander 从 knowledge_fts 检索
        try:
            from repositories import upsert_knowledge_node
            import uuid as _uid
            await upsert_knowledge_node(
                self.db, str(_uid.uuid4()), f"Dream整合: {consolidated[:MEMORY_DREAM_TITLE_PREVIEW_CHARS]}",
                consolidated[:MEMORY_DREAM_CONTENT_MAX_CHARS], source="auto_dream", tags=["dream", "consolidated"],
                score=0.8,
            )
        except Exception:
            _log.debug("Unexpected error", exc_info=True)

    # 清理孤立低质记忆
    deleted = await self._memory_repo.delete_low_confidence_orphans(0.3)
    if deleted > 0:
        _log.info(f"Dream cleaned {deleted} low-confidence orphan memories")


async def _try_dream_on_new_session(self) -> None:
    """新会话时的轻量 Dream：距上次整合 > 5 分钟且有 10+ 轮未整合对话。"""
    try:
        now = _time.time()
        last_dream = await self._memory_repo.get_dream_state("last_dream_time")
        if last_dream and now - last_dream < 300:
            return  # 刚整合过，跳过
        new_turns = await self._memory_repo.count_conversations_since(last_dream or 0)
        if new_turns < 10:
            return  # 对话量不够
        if not await self._memory_repo.acquire_dream_lock():
            return
        try:
            await self._consolidate_memories()
            # Phase 43 F2.3: OC 会话事实注入
            await self._consolidate_with_oc_sessions()
            await self._memory_repo.set_dream_state("last_dream_time", now)
            self._dream_failures = 0
        finally:
            await self._memory_repo.release_dream_lock()
    except Exception:
        _log.debug("Unexpected error", exc_info=True)  # 失败不阻塞对话流程


async def _scan_openclaw_sessions(self) -> list[dict]:
    """扫描 OpenClaw Gateway 会话 JSONL，提取最新对话事实。

    OpenClaw 在 ~/.openclaw/agents/main/sessions/ 下保存 JSONL 会话记录。
    扫描最近 5 个文件，提取有实质内容的消息 → 注入 auto_dream 合并管道。
    """
    import glob as _glob
    sessions_dir = Path.home() / ".openclaw" / "agents" / "main" / "sessions"
    if not sessions_dir.exists():
        return []
    facts = []
    try:
        pattern = str(sessions_dir / "*.jsonl")
        for f in sorted(_glob.glob(pattern))[-5:]:
            try:
                with open(f, encoding="utf-8") as fh:
                    for line in fh:
                        if not line.strip():
                            continue
                        try:
                            turn = _json.loads(line)
                        except _json.JSONDecodeError:
                            continue
                        content = turn.get("content", "")
                        if len(content) > MEMORY_DREAM_OC_FACT_MIN_CHARS:
                            facts.append({
                                "source": "openclaw_session",
                                "content": content[:MEMORY_DREAM_FACT_CONTENT_PREVIEW_CHARS],
                                "timestamp": turn.get("timestamp", 0),
                            })
            except OSError:
                continue
    except Exception:
        _log.debug("OC session scan failed", exc_info=True)
    return facts


async def _consolidate_with_oc_sessions(self) -> int:
    """auto_dream 增强: 将 OC 会话事实注入合并管道。"""
    oc_facts = await self._scan_openclaw_sessions()
    if not oc_facts:
        return 0
    added = 0
    for fact in oc_facts[:MEMORY_DREAM_OC_FACTS_LIMIT]:  # 最多 20 条/次
        try:
            await self._memory_repo.save_semantic(
                f"[OC] {fact['content'][:MEMORY_DREAM_OC_FACT_CONTENT_PREVIEW_CHARS]}",
                "openclaw_session",
                "system",
                0.6,  # 中等置信度——来自外部Agent
                "fact",
            )
            added += 1
        except Exception:
            _log.debug("注入 OC session 事实失败", exc_info=True)
    if added > 0:
        _log.info("Dream: injected %d facts from OC sessions", added)
    return added
