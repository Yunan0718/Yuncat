"""services/agent_finalize.py —— Agent 响应收尾模块

从 agent_service.py 拆分: _finalize_response。
采用委托模式: 模块级函数接收 self 作为首参。
"""
import logging
import time as _time

_log = logging.getLogger("yuncat.agent")


async def finalize_response(
    self, task_id: str, agent_id: str, session_id: str, prompt: str,
    full_response: str, mode: str, model_cfg: dict, adapter,
    user_conv_id: str, user_msg_id_str: str, elapsed: float,
) -> None:
    """对话完成收尾——Token统计→持久化→Plan解析→进化→压缩→标记完成。

    从原 _run_agent Phase 4 提取（原本 ~130 行内联代码）。
    """
    from config import resolve_context_window, SUB_AGENT_PROMPT_PREVIEW_CHARS, count_tokens
    from services.llm_usage import (
        FIELD_CACHE_MISS_TOKENS,
        FIELD_CACHED_TOKENS,
        FIELD_COMPLETION_TOKENS,
        FIELD_PROMPT_TOKENS,
        FIELD_REASONING_TOKENS,
    )

    # ── Token 统计 ──
    if self._memory_service and full_response:
        usage = getattr(adapter, "last_usage", {}) or {}
        # 字段名以 services/llm_usage.py 的规范名为唯一真源——适配器已完成归一化。
        # （2026-09-14 修复：这里原本读 DeepSeek 专有的 prompt_cache_hit_tokens，
        #   归一化改名后缓存命中恒为 0，前端缓存命中率随之静默归零。）
        assistant_tokens = usage.get(FIELD_COMPLETION_TOKENS, 0)
        user_tokens = usage.get(FIELD_PROMPT_TOKENS, 0)
        cache_hit = usage.get(FIELD_CACHED_TOKENS, 0)
        cache_miss = usage.get(FIELD_CACHE_MISS_TOKENS, 0)
        reasoning_tokens = usage.get(FIELD_REASONING_TOKENS, 0)
        _model_id = model_cfg.get("model", "gpt-4")

        async with self._msg_counter_lock:
            self._msg_counter += 1
            msg_seq = self._msg_counter
        assistant_counted = await count_tokens(full_response, model_cfg=model_cfg, model_id=_model_id)
        assistant_msg = await self._memory_service.add_message(
            session_id, "assistant", full_response, agent_id=agent_id,
            token_count=assistant_tokens if assistant_tokens else max(1, assistant_counted["tokens"]), reasoning=getattr(self, "_last_reasoning", "") or None,
        )
        assistant_conv_id = assistant_msg["id"]
        async with self._msg_counter_lock:
            self._msg_index[assistant_conv_id] = {
                "seq": msg_seq, "role": "assistant", "session_id": session_id,
                "content_preview": full_response[:SUB_AGENT_PROMPT_PREVIEW_CHARS], "refs": [],
            }
            if user_conv_id and user_conv_id in self._msg_index:
                self._msg_index[user_conv_id]["refs"].append(assistant_conv_id)
            self._evict_msg_index()
        user_seq = self._msg_index.get(user_conv_id, {}).get("seq", 0) if user_conv_id else 0
        await self._safe_send(task_id, {
            "type": "msg_numbered", "task_id": task_id,
            "msg_id": assistant_conv_id, "seq": msg_seq, "role": "assistant",
            "reply_to": user_msg_id_str,
            "reply_to_seq": user_seq,
        })
        try:
            user_counted = await count_tokens(prompt, model_cfg=model_cfg, model_id=_model_id)
            await self.db.execute(
                "UPDATE conversations SET token_count=? WHERE session_id=? AND role='user' "
                "ORDER BY created_at DESC LIMIT 1",
                (max(1, user_tokens) if user_tokens else max(1, user_counted["tokens"]), session_id),
            )
            await self.db.commit()
        except Exception:
            _log.debug("Unhandled exception", exc_info=True)

        await self._safe_send(task_id, {
            "type": "token_usage", "task_id": task_id,
            "prompt_tokens": user_tokens, "completion_tokens": assistant_tokens,
            "cached_tokens": cache_hit, "cache_miss_tokens": cache_miss,
            "reasoning_tokens": reasoning_tokens,
            "elapsed_ms": int(elapsed * 1000),
        })
        # Phase 38: 始终持久化 token_usage（不仅限于 cache hit）
        try:
            import json as _json
            await self.db.execute(
                "INSERT INTO analytics_events(event_type, event_data, created_at) "
                "VALUES('token_usage', ?, ?)",
                (_json.dumps({
                    "prompt_tokens": user_tokens,
                    "completion_tokens": assistant_tokens,
                    "total_tokens": user_tokens + assistant_tokens,
                    FIELD_CACHED_TOKENS: cache_hit,
                    FIELD_CACHE_MISS_TOKENS: cache_miss,
                    FIELD_REASONING_TOKENS: reasoning_tokens,
                    "model": model_cfg.get("model", ""),
                    "provider": model_cfg.get("provider", ""),
                    "session_id": session_id,
                    "agent_source": agent_id,  # Phase 43 F2.5
                }), int(_time.time())),
            )
            await self.db.commit()
        except Exception:
            _log.debug("token_usage persist failed", exc_info=True)

    # ── Plan 模式后处理 ──
    if mode == "plan" and full_response:
        try:
            from services.plan_parser import parse_plan
            import json as _json
            plan = parse_plan(full_response)
            plan_json = _json.dumps(plan, ensure_ascii=False)
            await self.db.execute(
                "UPDATE tasks SET output=? WHERE id=?",
                (plan_json, task_id),
            )
            await self.db.commit()
            await self._safe_send(task_id, {
                "type": "plan_ready",
                "task_id": task_id,
                "steps": plan.get("steps", []),
            })
        except Exception:
            _log.debug("Unhandled exception", exc_info=True)

    # ── AI 自我进化观察 ──
    if self._evolution_engine and full_response:
        tool_calls_log = [
            {"function": {"name": tc["function"]["name"]}}
            for tc in (getattr(adapter, "_last_tool_calls", []) or [])
        ]
        self._create_bg_task(
            self._evolution_engine.observe_conversation(
                session_id, prompt, full_response, tool_calls_log,
            ), label=f"evolution_observe_{session_id[:8]}"
        )

    # ── 标记完成 ──
    # D3: 乐观锁——仅 running→completed 生效，避免覆盖竞态中已被取消/失败的任务
    await self._mark_task(task_id, "completed",
                          output=full_response if mode != "plan" else None,
                          expected_from="running")

    # ── 对话后全量压缩 ──
    if self._memory_service and session_id:
        try:
            if session_id in self._needs_full_compact:
                await self._memory_service.compact_now(session_id, model_cfg)
                self._needs_full_compact.discard(session_id)
                _log.info("Post-emergency full compaction completed for session %s", session_id[:8])
            else:
                # Phase 56-P3: 压缩触发优先使用厂商 API 返回的真实 prompt_tokens
                usage = getattr(adapter, "last_usage", {}) or {}
                current_tokens = usage.get("prompt_tokens", 0)
                if current_tokens <= 0:
                    # fallback：统一 count_tokens 接口（vendor API → tiktoken → CJK）
                    conv = self._memory_service._sessions.get(session_id, [])
                    counted = await count_tokens(conv, model_cfg=model_cfg, model_id=_model_id)
                    current_tokens = counted["tokens"]
                await self._memory_service.maybe_compact_context(
                    session_id, current_tokens=current_tokens,
                    max_tokens=resolve_context_window(model_cfg),
                )
        except Exception:
            _log.debug("Post-task compaction failed", exc_info=True)
