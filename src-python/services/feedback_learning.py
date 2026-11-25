"""services/feedback_learning.py —— 用户反馈学习引擎（借鉴 OpenHuman ambient personalization）

核心机制：
1. 用户接受 → 相关知识的评分 +0.1（"这条知识被验证有用"）
2. 用户拒绝 → 相关知识的评分 -0.15（"这条知识可能不对"）
3. 触发时机：工具确认（Accept/Reject）+ 对话结束（completed/failed）
4. 在无用户反馈时，评分自然衰减（memory_tree.periodic_maintenance）

差异于 OpenHuman ambient cache：
- OpenHuman 是持续从所有已连接服务中拉取数据（Gmail/Calendar/Slack）
- Yuncat 是桌面应用，反馈来源是"用户对 Agent 行为的确认/拒绝"
"""
import logging
import time as _time

from config import (
    FEEDBACK_LEARNER_KNOWLEDGE_LIMIT,
    FEEDBACK_LEARNER_KEYWORD_KNOWLEDGE_LIMIT,
    FEEDBACK_LEARNER_FTS_LIMIT,
    FEEDBACK_LEARNER_PERSONALIZATION_LIMIT,
    FEEDBACK_LEARNER_TITLE_PREVIEW_CHARS,
    FEEDBACK_LEARNER_CONTENT_PREVIEW_CHARS,
    FEEDBACK_LEARNER_TAGS_PREVIEW_LIMIT,
    FEEDBACK_LEARNER_LOG_KEYWORD_LIMIT,
    FEEDBACK_LEARNER_FTS_KEYWORD_LIMIT,
    FEEDBACK_LEARNER_EXTRACT_KEYWORD_LIMIT,
    KNOWLEDGE_SCORE_CEILING,
    PERSONALIZATION_EXCLUDED_SOURCES,
    PERSONALIZATION_PRIORITY_SOURCES,
    PERSONALIZATION_SCORE_THRESHOLD,
)

_log = logging.getLogger("yuncat.feedback")


def _render_personalization_rows(rows: list) -> list[str]:
    """把 personalization 候选行渲染成 Markdown 列表项。"""
    lines = []
    for r in rows:
        tags_str = ""
        if r[3]:
            import json
            try:
                tags = json.loads(r[3])
                tags_str = f" [{', '.join(tags[:FEEDBACK_LEARNER_TAGS_PREVIEW_LIMIT])}]" if tags else ""
            except Exception:
                _log.warning("Feedback: parse personalization tags failed", exc_info=True)
        lines.append(f"- **{r[0]}**{tags_str}: {r[1][:FEEDBACK_LEARNER_CONTENT_PREVIEW_CHARS]}")
    return lines


class FeedbackLearner:
    """从用户反馈中学习——调整知识节点评分。

    User accepts tool execution → boost related knowledge score.
    User rejects tool execution → penalize related knowledge score.
    """

    def __init__(self, db):
        self.db = db

    async def record_feedback(
        self, event_type: str, task_id: str, context: str = "",
        agent_id: str = None,
    ) -> None:
        """记录用户反馈事件并更新知识评分。

        event_type: "human_accepted" | "human_rejected"
        context: 关联的 prompt 或工具名（用于定位相关知识）
        agent_id: Phase 48 T2-1 —— 委托反馈时传入 target_agent，写入 tags 使
                  capability_router._query_learned_from_db 的 `tags LIKE '%agent_id%'`
                  查询可达，闭合反馈学习闭环。非委托反馈不传（tags 为空数组）。
        """
        import json
        from repositories import adjust_knowledge_score, search_knowledge_tree

        delta = 0.10 if event_type == "human_accepted" else -0.15

        # 从 context 中提取关键词 → 搜索知识树
        keywords = _extract_keywords(context)
        if not keywords:
            # Phase 30: 正则未命中时尝试 FTS5 全文搜索
            keywords = await _fts_keywords(self.db, context) if context else []
        if not keywords and task_id:
            try:
                cursor = await self.db.execute(
                    "SELECT input FROM tasks WHERE id=?", (task_id,),
                )
                row = await cursor.fetchone()
                if row:
                        keywords = _extract_keywords(row[0])
                        if not keywords:
                            keywords = await _fts_keywords(self.db, row[0])
            except Exception:
                _log.warning("Feedback: load task input failed", exc_info=True)

        if not keywords:
            # Phase 30: 完全无匹配 → 用原始输入的首 3 个词作为种子关键词
            fallback = context or ""
            words = [w for w in fallback.split() if len(w) > 3]
            keywords = words[:FEEDBACK_LEARNER_LOG_KEYWORD_LIMIT] if words else ["general_feedback"]
            _log.debug("Feedback: no keywords matched, using fallback: %s", keywords)

            # Phase 31 fix: 无匹配时创建新知识节点（而非只读丢弃）
            # Phase 48 T2-1: tags 写入 agent_id 使 capability_router 反馈学习查询可达
            import uuid as _uuid
            import time as _time
            try:
                title = f"user_feedback:{fallback[:FEEDBACK_LEARNER_TITLE_PREVIEW_CHARS]}"
                content = f"Source: {event_type}\nContext: {fallback[:FEEDBACK_LEARNER_CONTENT_PREVIEW_CHARS]}\nDelta: {delta}"
                node_id = str(_uuid.uuid4())[:12]
                tags_json = json.dumps([agent_id]) if agent_id else "[]"
                await self.db.execute(
                    "INSERT INTO knowledge_nodes(id, title, content, source, score, tags, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (node_id, title, content, "agent_delegation", 0.3, tags_json,
                     int(_time.time()), int(_time.time())),
                )
                await self.db.commit()
                _log.info("Feedback: created new knowledge node '%s' (score=0.3, tags=%s)", title, tags_json)
            except Exception as e:
                _log.debug("Feedback: failed to create new node: %s", e)

        if not keywords:
            return

        # 找到相关知识节点 → 调整评分
        # ⚠️ 只取 id 做原子累加，不要读回 "score" 再 upsert：
        #   1. search_knowledge_tree 的 "score" 是 BM25 检索相关性，不是节点评分。
        #      用它做 +delta 等于把评分钉死成「相关性±delta」——实测连续接受 5 次
        #      评分纹丝不动，反馈从未累积过。
        #   2. upsert_knowledge_node 的 UPDATE 会连 source/tags/project_id 一起写，
        #      不传即用默认值（source→auto_dream、tags→[]、project_id→NULL），
        #      实测会让 capability_router 的委托学习查询命中数归零、项目隔离失效。
        for kw in keywords[:FEEDBACK_LEARNER_KEYWORD_KNOWLEDGE_LIMIT]:
            results = await search_knowledge_tree(self.db, kw, limit=FEEDBACK_LEARNER_KNOWLEDGE_LIMIT)
            for r in results:
                await adjust_knowledge_score(self.db, r["id"], delta)

        _log.debug(
            "Feedback %s: adjusted %d knowledge nodes (delta=%+.2f, keywords=%s)",
            event_type, min(FEEDBACK_LEARNER_LOG_KEYWORD_LIMIT, len(keywords)), delta,
            keywords[:FEEDBACK_LEARNER_LOG_KEYWORD_LIMIT],
        )

    async def get_personalization_context(self, limit: int = FEEDBACK_LEARNER_PERSONALIZATION_LIMIT) -> str:
        """获取个性化上下文——高分知识节点摘要，注入 Commander prompt。

        分两段渲染，因为两者的可信度不同，混在「你的偏好」一个标题下会误导模型：
          - 「你确认过的偏好」：用户明确表达或反复确认过的（agent_delegation /
            MEMORY.md / 外部 agent 同步记忆）
          - 「项目事实与发现」：会话沉淀的项目决策、技术选型、重要发现

        ⚠️ 必须按 source 过滤：knowledge_nodes 是多语义共用表，还装着工作流摘要、
        Spec/Loop 升级告警、技术债扫描、agent_perf 路由统计等内部记账节点，
        不过滤会让模型把机器日志当成用户偏好去迎合。

        ⚠️ 优先级来源必须单独取，不能和会话事实放在同一个 ORDER BY score 里竞争：
        会话摘要被 memory_tree 索引时直接给 score=1.0，而用户确认的偏好要从 0.3
        逐次累积，实测确认 6 次（0.9）仍会被 1.0 的会话摘要挤出 limit。
        """
        confirmed = await self._fetch_personalization_rows(limit, priority_only=True)
        lines: list[str] = []
        if confirmed:
            lines.append("## 你确认过的偏好")
            lines.extend(_render_personalization_rows(confirmed))

        remaining = limit - len(confirmed)
        if remaining > 0:
            facts = await self._fetch_personalization_rows(remaining, priority_only=False)
            if facts:
                lines.append("## 项目事实与发现")
                lines.extend(_render_personalization_rows(facts))

        return "\n".join(lines) if lines else ""

    async def _fetch_personalization_rows(self, limit: int, priority_only: bool) -> list:
        """取 personalization 候选行。priority_only=True 只取用户确认过的来源。"""
        excluded = sorted(PERSONALIZATION_EXCLUDED_SOURCES)
        priority = sorted(PERSONALIZATION_PRIORITY_SOURCES)
        ex_ph = ",".join("?" * len(excluded))
        pr_ph = ",".join("?" * len(priority))
        source_cond = f"source IN ({pr_ph})" if priority_only else f"source NOT IN ({pr_ph})"
        cursor = await self.db.execute(
            f"SELECT title, content, score, tags FROM knowledge_nodes "
            f"WHERE score >= ? AND source IS NOT NULL AND source NOT IN ({ex_ph}) "
            f"AND {source_cond} ORDER BY score DESC LIMIT ?",
            (PERSONALIZATION_SCORE_THRESHOLD, *excluded, *priority, limit),
        )
        return await cursor.fetchall()


    async def track_temporary_role(
        self, role_name: str, task_prompt: str, was_accepted: bool,
    ) -> str | None:
        """Phase 30: 追踪临时角色使用情况——写入 knowledge_nodes。

        如果节点已存在（同标题的临时角色），提高评分；否则创建新节点。
        was_accepted=False 时跳过（只有被接受的讨论才追踪——质量门槛）。

        返回: 创建的节点 ID，或 None（若被跳过）。
        """
        if not was_accepted:
            return None

        import uuid as _uuid
        from repositories import upsert_knowledge_node, search_knowledge_tree

        title = f"临时角色: {role_name}"
        tags = _extract_keywords(task_prompt)[:FEEDBACK_LEARNER_LOG_KEYWORD_LIMIT]
        if not tags:
            tags = ["auto_generated"]

        existing = await search_knowledge_tree(self.db, title, limit=1)
        if existing:
            node = existing[0]
            # "node_score" 才是节点持久化评分；"score" 是 BM25 相关性，
            # 拿它当基线会把评分重置成「相关性 + 0.2」，丢弃已有累积。
            new_score = min(
                KNOWLEDGE_SCORE_CEILING,
                (node.get("node_score") or 0.3) + 0.2,
            )
            node_id = node["id"]
        else:
            node_id = str(_uuid.uuid4())
            new_score = 0.3

        await upsert_knowledge_node(
            self.db, node_id, title,
            f"自动生成的临时讨论角色。来源任务: {task_prompt[:FEEDBACK_LEARNER_CONTENT_PREVIEW_CHARS]}",
            source="deliberation_tmp_role", tags=tags, score=new_score,
        )
        _log.info("Tracked temporary role: %s (score=%.2f, id=%s)", role_name, new_score, node_id)
        return node_id

    async def record_deliberation_feedback(
        self, role_name: str, agent_used: str, was_accepted: bool,
    ) -> None:
        """Task 7.1: 记录讨论角色表现反馈——调整 agent_perf 节点 score。

        在多角色讨论结束后，根据用户是否接受讨论结果，调整对应 role→agent
        的 agent_perf 节点评分。与 record_feedback 的 delta 保持一致：
        接受 +0.10 / 拒绝 -0.15。

        闭环路径:
          写入端: deliberation_discussion.py（source='mixed_deliberation',
                  title='agent_perf:{role}→{agent}', tags=json.dumps([agent])）
          反馈端: 本方法调整 score（接受加分/拒绝减分）
          读取端: capability_router._query_learned_from_db 聚合到 delegation:{agent} key，
                  AVG(score) 作为 success_rate，>0.7 且 count>=3 时触发自动路由偏好。
        """
        delta = 0.10 if was_accepted else -0.15
        node_title = f"agent_perf:{role_name}→{agent_used}"

        try:
            cursor = await self.db.execute(
                "SELECT id, score FROM knowledge_nodes "
                "WHERE source='mixed_deliberation' AND title=?",
                (node_title,),
            )
            rows = await cursor.fetchall()
            if not rows:
                _log.debug("record_deliberation_feedback: no matching node for '%s'", node_title)
                return

            for row in rows:
                node_id, old_score = row[0], row[1]
                new_score = max(0.01, min(1.0, old_score + delta))
                await self.db.execute(
                    "UPDATE knowledge_nodes SET score=?, updated_at=? WHERE id=?",
                    (new_score, int(_time.time()), node_id),
                )
            await self.db.commit()
            _log.info(
                "record_deliberation_feedback: adjusted %d node(s) for '%s' (delta=%+.2f)",
                len(rows), node_title, delta,
            )
        except Exception:
            _log.warning("record_deliberation_feedback failed", exc_info=True)


async def _fts_keywords(db, text: str) -> list[str]:
    """Phase 30: FTS5 全文搜索回退——正则未命中时从知识树提取关键词。
    取 FTS5 搜索结果中前 5 个节点的 tags 合并去重返回。
    """
    try:
        from repositories import search_knowledge_tree
        results = await search_knowledge_tree(db, text, limit=FEEDBACK_LEARNER_FTS_LIMIT)
        keywords = set()
        for r in results:
            tags = r.get("tags", [])
            if isinstance(tags, str):
                import json as _json
                try:
                    tags = _json.loads(tags)
                except Exception:
                    tags = []
            for t in (tags or []):
                if t and t != "general":
                    keywords.add(t)
        return list(keywords)[:FEEDBACK_LEARNER_FTS_KEYWORD_LIMIT]
    except Exception:
        return []


def _extract_keywords(text: str) -> list[str]:
    """从文本中提取关键词"""
    import re
    # 常见技术关键词
    patterns = [
        r'\b(python|javascript|typescript|rust|go|java|react|vue|angular)\b',
        r'\b(sql|postgres|mysql|sqlite|mongodb|redis|database|数据库)\b',
        r'\b(docker|kubernetes|k8s|deploy|部署|ci|cd)\b',
        r'\b(api|rest|graphql|grpc|接口|endpoint)\b',
        r'\b(test|测试|pytest|jest|vitest|coverage)\b',
        r'\b(安全|security|auth|认证|权限|encrypt)\b',
        r'\b(前端|frontend|后端|backend|fullstack|全栈)\b',
        r'\b(camelCase|snake_case|命名|convention|规范|偏好)\b',
        r'\b(微服务|microservice|架构|architect|设计|design)\b',
        r'\b(linux|windows|macos|ubuntu|debian)\b',
        r'\b(git|github|gitlab|version|分支|branch)\b',
        r'\b(llm|ai|ml|模型|model|训练|推理|token)\b',
    ]
    found = set()
    for pattern in patterns:
        matches = re.findall(pattern, text.lower())
        for m in matches:
            if isinstance(m, tuple):
                found.add(m[0])
            else:
                found.add(m)
    return list(found)[:FEEDBACK_LEARNER_EXTRACT_KEYWORD_LIMIT]
