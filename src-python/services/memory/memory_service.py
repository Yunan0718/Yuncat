"""services/memory_service.py —— 统一记忆系统（三层架构 + 会话隔离 + Auto-Dream）

附录 K 写穿透：先写 DB，再更新 _sessions 内存缓存。
v41: Auto-Dream 后台记忆整合——24h/50轮/锁 三重门控。
v44: 会话锁初始化用 _global_session_lock 原子保护。
v49: 独立 _evict_lock 避免锁语义越界。

v67: 方法体拆分到子模块——本文件仅保留 __init__ / 配置注入 / 薄包装方法。
     实际逻辑见: memory_sessions / memory_compact / memory_dream / memory_search
"""
import asyncio
import logging

from config import (
    HYBRID_QUERY_EXPANSION_DEFAULT,
    HYBRID_DIVERSIFY_DEFAULT,
    HYBRID_LATENCY_BUDGET_MS_DEFAULT,
    MAX_CACHE_TURNS_GLOBAL,
)

_log = logging.getLogger("yuncat.memory")

# Re-exports — 供外部模块通过 memory_service 访问压缩辅助函数
from services.memory.memory_compact import (
    STRUCTURED_COMPACT_PROMPT,
    QUALITY_PROBE_PROMPT,
    _format_existing_summary,
    _parse_structured_summary,
    _merge_section,
    _format_compact_boundary,
    _summary_size,
    _is_probe_passed,
)


class MemoryService:
    # Phase 44 C.3: Circuit Breaker — 连续压缩失败 3 次 → 熔断
    CIRCUIT_BREAKER_THRESHOLD = 3
    # Phase 48 C4: 熔断后 30min 冷却 → 自动恢复重试（原为永久禁用，无恢复机制）
    CIRCUIT_BREAKER_RECOVERY_SEC = 1800

    # v55: 上下文压缩常量
    from config import COMPACT_TRIGGER_STANDARD, COMPACT_COOLDOWN_SEC
    COMPACT_THRESHOLD = COMPACT_TRIGGER_STANDARD  # 95% 窗口占用时自动触发全量 LLM 压缩
    COMPACT_COOLDOWN_SEC = COMPACT_COOLDOWN_SEC   # 全量压缩冷却 (from config)
    # 高负载时放宽压缩门槛——tool 结果占比低也应该压
    COMPACT_MIN_TOOL_RESULT_RATIO_HIGH = 0.60
    COMPACT_MIN_TOOL_RESULT_RATIO_LOW = 0.20
    COMPACT_MIN_MESSAGES = 16
    _MAX_CACHE_TURNS_GLOBAL = MAX_CACHE_TURNS_GLOBAL  # Phase 48 C4: 2000→10000——1M 上下文模型压缩时旧消息已不在缓存；硬上限防 OOM

    def __init__(self, db, summarizer=None, memory_repo=None, vector_repo=None):
        self.db = db
        self._summarizer = summarizer  # v27: 可选回调，Phase 2 注入 LLM 摘要函数
        self._memory_repo = memory_repo  # v41: Dream 仓库
        self._vector_repo = vector_repo  # v56: chromadb 向量仓库
        self._sessions: dict[str, list[dict]] = {}
        self._summaries: dict[str, dict] = {}
        self._session_last_access: dict[str, float] = {}
        self._global_session_lock = asyncio.Lock()
        self._evict_lock = asyncio.Lock()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._dream_failures = 0
        self._pending_writes = 0  # Phase 48 T3-12: 批量 commit 计数器（原 getattr 防御性获取，应在 __init__ 初始化）
        # Phase 44 C.3: 压缩熔断器
        self._compact_failure_streak: dict[str, int] = {}
        self._compact_circuit_broken: set[str] = set()
        self._circuit_broken_at: dict[str, float] = {}  # Phase 48 C4: 熔断时间戳（用于冷却期恢复）
        self._agent_service = None  # 由 DeliberationService/AgentService 注入
        # v55: 结构化摘要缓存 + 冷却期追踪（须在 __init__ 初始化——
        # 测试与未调 set_model_config_for_sizing 的路径也会访问，否则 AttributeError）
        self._structured_summaries: dict[str, dict] = {}
        self._last_compact_time: dict[str, float] = {}
        from config import MAX_ACTIVE_SESSIONS, SESSION_IDLE_TTL, calc_max_turns_for_cache
        self._max_active_sessions = MAX_ACTIVE_SESSIONS
        self._session_idle_ttl = SESSION_IDLE_TTL
        self._max_turns = 600  # 启动瞬间的保守 fallback，首次对话前被 set_model_config_for_sizing() 覆盖为动态值
        self._calc_max_turns_fn = calc_max_turns_for_cache  # 按模型上下文窗口动态计算
        self._reranker = None  # Task 6.14: BGE cross-encoder 重排器（lifespan 注入）

    def set_model_config_ref(self, model_config) -> None:
        """注入 ModelConfigService 引用——质量门控 API 化 + 压缩时使用。"""
        self._model_config = model_config

    def set_adapters(self, adapters: dict) -> None:
        """注入 Adapter 字典——质量门控无 summarizer 时走 Agent Adapter，避免 Yuncat 直接调 LLM。"""
        self._adapters = adapters

    def set_reranker(self, reranker) -> None:
        """Task 6.14: 注入 Reranker 实例——供 multi_level_search 的 hybrid_search 传给 rerank 步骤"""
        self._reranker = reranker

    def set_model_config_for_sizing(self, model_config: dict) -> None:
        """注入模型配置以动态调整 _max_turns。
        每次对话开始时由 agent_service 调用。"""
        if self._calc_max_turns_fn:
            raw = self._calc_max_turns_fn(model_config)
            # 安全上限：无论模型多大窗口，单会话最多缓存 4000 条（≈2MB）
            self._max_turns = min(raw, self._MAX_CACHE_TURNS_GLOBAL)

    # ============================================================
    # 短期记忆（会话隔离 + v33 asyncio.Lock 并发保护）
    # ============================================================
    async def add_message(self, session_id: str, role: str, content: str, agent_id: str | None = None, token_count: int = 0, tool_calls: list[dict] | None = None, reasoning: str | None = None, tool_call_id: str | None = None) -> dict:
        from services.memory.memory_sessions import add_message as _impl
        # 2026-10-07 修复：此委托签名声明了 tool_calls / reasoning，转发时却把它俩丢了。
        # 后果是「思考链」与「行动链（tool_calls）」两条写入路径的最后一段被截断——
        # 外层调用看着成功，DB 里两列恒为空（实测全库两列非空数均为 0）。
        return await _impl(
            self, session_id, role, content,
            agent_id=agent_id, token_count=token_count,
            tool_calls=tool_calls, reasoning=reasoning, tool_call_id=tool_call_id,
        )

    async def get_session_context(self, session_id: str) -> list[dict]:
        from services.memory.memory_sessions import get_session_context as _impl
        return await _impl(self, session_id)

    async def flush_pending(self) -> None:
        """D1: 显式刷新待提交的会话写入——跨会话操作前调用确保 DB 持久化。"""
        from services.memory.memory_sessions import flush_pending as _impl
        return await _impl(self)

    def _evict_sessions(self, now: float) -> None:
        from services.memory.memory_sessions import _evict_sessions as _impl
        _impl(self, now)

    async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        from services.memory.memory_sessions import _get_session_lock as _impl
        return await _impl(self, session_id)

    # ============================================================
    # 上下文摘要
    # ============================================================
    async def summarize_context(self, session_id: str) -> str:
        from services.memory.memory_search import summarize_context as _impl
        return await _impl(self, session_id)

    # ============================================================
    # 多级检索
    # ============================================================
    async def multi_level_search(
        self, query: str, limit: int = 20,
        filters: dict | None = None,
        query_expansion: bool = HYBRID_QUERY_EXPANSION_DEFAULT,
        diversify: bool = HYBRID_DIVERSIFY_DEFAULT,
        latency_budget_ms: float = HYBRID_LATENCY_BUDGET_MS_DEFAULT,
    ) -> list[dict]:
        from services.memory.memory_search import multi_level_search as _impl
        return await _impl(
            self, query, limit,
            filters=filters,
            query_expansion=query_expansion,
            diversify=diversify,
            latency_budget_ms=latency_budget_ms,
        )

    # ============================================================
    # v41: Auto-Dream 后台记忆整合
    # ============================================================
    async def _get_dream_params(self) -> dict:
        from services.memory.memory_dream import _get_dream_params as _impl
        return await _impl(self)

    async def auto_dream(self):
        from services.memory.memory_dream import auto_dream as _impl
        return await _impl(self)

    async def _consolidate_memories(self):
        from services.memory.memory_dream import _consolidate_memories as _impl
        return await _impl(self)

    async def _try_dream_on_new_session(self) -> None:
        from services.memory.memory_dream import _try_dream_on_new_session as _impl
        return await _impl(self)

    async def _scan_openclaw_sessions(self) -> list[dict]:
        from services.memory.memory_dream import _scan_openclaw_sessions as _impl
        return await _impl(self)

    async def _consolidate_with_oc_sessions(self) -> int:
        from services.memory.memory_dream import _consolidate_with_oc_sessions as _impl
        return await _impl(self)

    # ============================================================
    # v55: 上下文压缩——四阶段 pipeline
    # ============================================================
    async def compact_now(self, session_id: str, model_config: dict | None = None) -> dict | None:
        from services.memory.memory_compact import compact_now as _impl
        return await _impl(self, session_id, model_config)

    async def _save_compact_marker(self, session_id: str, older: list[dict]) -> None:
        from services.memory.memory_compact import _save_compact_marker as _impl
        return await _impl(self, session_id, older)

    async def maybe_compact_context(
        self, session_id: str, current_tokens: int, max_tokens: int | None = None,
        system_prompt_tokens: int = 0,
    ) -> dict | None:
        from services.memory.memory_compact import maybe_compact_context as _impl
        return await _impl(
            self, session_id, current_tokens, max_tokens,
            system_prompt_tokens=system_prompt_tokens,
        )

    def _record_compact_failure(self, session_id: str):
        from services.memory.memory_compact import _record_compact_failure as _impl
        _impl(self, session_id)

    def get_circuit_breaker_status(self) -> dict:
        from services.memory.memory_compact import get_circuit_breaker_status as _impl
        return _impl(self)

    # ============================================================
    # 阶段 1 辅助: 分级分类
    # ============================================================
    @staticmethod
    def _is_tool_result(msg: dict) -> bool:
        from services.memory.memory_compact import _is_tool_result as _impl
        return _impl(msg)

    @staticmethod
    def _is_user_instruction(msg: dict) -> bool:
        from services.memory.memory_compact import _is_user_instruction as _impl
        return _impl(msg)

    def _calc_tool_result_ratio(self, conv: list[dict]) -> float:
        from services.memory.memory_compact import _calc_tool_result_ratio as _impl
        return _impl(self, conv)

    def _split_messages(
        self, conv: list[dict], model_config: dict | None = None,
        system_prompt_tokens: int = 0,
    ) -> tuple[list[dict], list[dict]]:
        from services.memory.memory_compact import _split_messages as _impl
        return _impl(self, conv, model_config, system_prompt_tokens)

    # ============================================================
    # 阶段 2: 结构化摘要（借鉴 Factory AI 6 节格式）
    # ============================================================
    async def _build_structured_summary(
        self, session_id: str, older: list[dict], recent: list[dict], tool_ratio: float,
        model_config: dict | None = None,
    ) -> dict:
        from services.memory.memory_compact import _build_structured_summary as _impl
        return await _impl(self, session_id, older, recent, tool_ratio, model_config)

    def _grade_messages(self, messages: list[dict]) -> list[dict]:
        from services.memory.memory_compact import _grade_messages as _impl
        return _impl(self, messages)

    @staticmethod
    def _format_graded_messages(graded: list[dict]) -> str:
        from services.memory.memory_compact import _format_graded_messages as _impl
        return _impl(graded)

    @staticmethod
    def _format_recent_context(recent: list[dict]) -> str:
        from services.memory.memory_compact import _format_recent_context as _impl
        return _impl(recent)

    # ============================================================
    # 阶段 3: 质量门控（借鉴 ContextPilot < 72 分回退）
    # ============================================================
    async def _quality_gate(
        self, structured: dict, older: list[dict], full_conv: list[dict],
        probes: list[str] | None = None, lenient: bool = False,
    ) -> bool:
        from services.memory.memory_compact import _quality_gate as _impl
        return await _impl(self, structured, older, full_conv, probes, lenient)

    @staticmethod
    def _extract_probes(older: list[dict]) -> list[str]:
        from services.memory.memory_compact import _extract_probes as _impl
        return _impl(older)

    # ============================================================
    # 阶段 4: 磁盘持久化 + 内存替换
    # ============================================================
    async def _persist_compaction_snapshot(self, session_id: str, older: list[dict]) -> None:
        from services.memory.memory_compact import _persist_compaction_snapshot as _impl
        return await _impl(self, session_id, older)

    async def _apply_compacted_context(
        self, session_id: str, structured: dict, recent: list[dict], snapshot,
    ) -> bool:
        from services.memory.memory_compact import _apply_compacted_context as _impl
        return await _impl(self, session_id, structured, recent, snapshot)

    # ============================================================
    # 便捷方法
    # ============================================================
    async def estimate_tokens(self, messages: list[dict]) -> int:
        from services.memory.memory_sessions import estimate_tokens as _impl
        return await _impl(self, messages)

    async def get_conversation(self, session_id: str, limit: int = 50) -> list[dict]:
        from services.memory.memory_sessions import get_conversation as _impl
        return await _impl(self, session_id, limit)

    async def store_memory(
        self, content: str, source: str = "conversation", owner: str = "",
        memory_type: str = "fact", project_id: str | None = None,
        confidence: float = 0.5,
    ) -> str:
        from services.memory.memory_search import store_memory as _impl
        return await _impl(self, content, source, owner, memory_type, project_id, confidence)

    async def _index_to_vector(
        self, mem_id: str, content: str,
        source: str = "", owner: str = "", memory_type: str = "fact",
        project_id: str | None = None,
    ) -> None:
        from services.memory.memory_search import _index_to_vector as _impl
        await _impl(
            self, mem_id, content,
            source=source, owner=owner, memory_type=memory_type,
            project_id=project_id,
        )

    async def vector_search(self, query: str, limit: int = 10) -> list[dict]:
        from services.memory.memory_search import vector_search as _impl
        return await _impl(self, query, limit)

    async def verify(self, mem_id: str) -> None:
        from services.memory.memory_search import verify as _impl
        return await _impl(self, mem_id)

    async def summarize_conversation(self, messages: list[dict]) -> str:
        from services.memory.memory_search import summarize_conversation as _impl
        return await _impl(self, messages)
