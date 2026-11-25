"""services/llm_cache.py — Batch 8.4 LLM 响应缓存 MVP

精确哈希缓存（非语义）。默认关闭，由 LLM_CACHE_ENABLED 控制。
对标 LangChain Cache in-memory / Redis HTTP cache。

设计：
- OrderedDict LRU + 时间戳 TTL
- asyncio.Lock 保护并发读写
- 仅缓存非工具调用的 invoke() 响应
- 全局单例 get_global_cache()
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from typing import Any

from config import LLM_CACHE_ENABLED, LLM_CACHE_TTL_SEC, LLM_CACHE_MAX_ENTRIES


def _make_key(model_name: str, messages: list[dict]) -> str:
    """生成缓存键——model + 规范化 messages JSON。"""
    canonical = json.dumps(
        {"model": model_name, "messages": messages},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class LLMCache:
    """LRU + TTL 缓存。协程安全。"""

    def __init__(
        self,
        enabled: bool = LLM_CACHE_ENABLED,
        ttl_sec: int = LLM_CACHE_TTL_SEC,
        max_entries: int = LLM_CACHE_MAX_ENTRIES,
    ) -> None:
        self.enabled = enabled
        self.ttl_sec = ttl_sec
        self.max_entries = max_entries
        self._store: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._lock = asyncio.Lock()
        # 统计
        self.hits = 0
        self.misses = 0

    async def get(self, model_name: str, messages: list[dict]) -> dict[str, Any] | None:
        """查询缓存。命中返回 message dict，未命中/过期/禁用返回 None。"""
        if not self.enabled:
            return None
        key = _make_key(model_name, messages)
        now = time.time()
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                return None
            ts, value = entry
            if now - ts > self.ttl_sec:
                # TTL 过期——淘汰并计 miss
                self._store.pop(key, None)
                self.misses += 1
                return None
            # LRU：命中后移到末尾
            self._store.move_to_end(key)
            self.hits += 1
            return value

    async def put(
        self, model_name: str, messages: list[dict], value: dict[str, Any]
    ) -> None:
        """写入缓存。禁用时 no-op。超容量时淘汰最老。"""
        if not self.enabled:
            return
        key = _make_key(model_name, messages)
        now = time.time()
        async with self._lock:
            self._store[key] = (now, value)
            self._store.move_to_end(key)
            while len(self._store) > self.max_entries:
                self._store.popitem(last=False)  # FIFO 淘汰最老

    def stats(self) -> dict[str, Any]:
        """返回缓存统计快照。非阻塞，无需持锁（数值可能瞬间不一致，可接受）。"""
        total = self.hits + self.misses
        return {
            "enabled": self.enabled,
            "size": len(self._store),
            "max_entries": self.max_entries,
            "ttl_sec": self.ttl_sec,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate_pct": round(self.hits / total * 100, 1) if total > 0 else 0.0,
        }

    async def clear(self) -> None:
        """清空缓存与统计。测试用。"""
        async with self._lock:
            self._store.clear()
            self.hits = 0
            self.misses = 0


_global_cache: LLMCache | None = None


def get_global_cache() -> LLMCache:
    """获取全局缓存单例。首次调用时按 config 默认值初始化。"""
    global _global_cache
    if _global_cache is None:
        _global_cache = LLMCache()
    return _global_cache


def reset_global_cache() -> None:
    """测试用——重置全局单例，使下次 get_global_cache() 重新读取 config。"""
    global _global_cache
    _global_cache = None
