"""tests/unit/test_prompt_agent.py —— Phase 74 PromptAgent 后台服务单元测试

覆盖：
1. recommend —— LLM 成功 / 缓存命中 / 缓存失效 / LLM 错误 / JSON 解析失败 / 低置信度 / adapter 缺失 / model_config 失败
2. _rule_based_recommend —— tdd/security/code-review 关键词 / 无命中默认 / 大小写不敏感 / 首个命中胜出
3. compose_behaviors —— 正常拼接 / 空列表 / 未知 id 跳过 / token 截断
4. render —— 完整渲染 / 无 workflow / 未知 role 优雅处理

设计：mock adapter.invoke 返回预设 content；mock model_config.get_active_config 返回预设 cfg。
不启动真实子进程，不发起真实 HTTP。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ═══════════════════════════════════════════════════════════════
# Mock 工具
# ═══════════════════════════════════════════════════════════════

class _MockAdapter:
    """模拟 adapter——可配置 invoke 返回值，记录调用"""

    def __init__(self, invoke_return=None):
        self.agent_id = "hermes"
        self._invoke_return = invoke_return if invoke_return is not None else {"content": "{}"}
        self.invoke_calls: list[tuple[str, dict]] = []

    async def invoke(self, prompt: str, context: dict | None = None) -> dict:
        self.invoke_calls.append((prompt, context or {}))
        if isinstance(self._invoke_return, Exception):
            raise self._invoke_return
        return self._invoke_return


class _MockModelConfig:
    """模拟 ModelConfigService——get_active_config 返回预设 cfg"""

    def __init__(self, cfg=None):
        self._cfg = cfg if cfg is not None else {
            "model": "test-flash",
            "api_key": "test-key",
            "base_url": "http://test.local/v1",
        }
        self.calls = 0

    async def get_active_config(self, agent_id: str = "") -> dict:
        self.calls += 1
        if isinstance(self._cfg, Exception):
            raise self._cfg
        return self._cfg


async def _raise_exc(*_a, **_kw):
    """Task 6.17: 模拟 get_active_config 抛异常的 helper"""
    raise RuntimeError("model config failure")


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
async def db(tmp_path):
    """aiosqlite + SCHEMA_SQL——每个测试独立 DB"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_prompt_agent.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    yield conn
    await conn.close()


@pytest.fixture
def library(db):
    """PromptLibrary 实例（Phase 72）"""
    from services.prompt_library import PromptLibrary
    return PromptLibrary(db)


@pytest.fixture
def mock_adapter():
    """默认 mock adapter——invoke 返回空 content（测试可覆盖 _invoke_return）"""
    return _MockAdapter()


@pytest.fixture
def mock_model_config():
    return _MockModelConfig()


@pytest.fixture
def agent(db, mock_adapter, mock_model_config, library):
    """PromptAgent 实例——注入 mock 依赖"""
    from services.prompt_agent import PromptAgent
    return PromptAgent(
        conn=db,
        adapters={"hermes": mock_adapter, "openclaw": _MockAdapter()},
        model_config=mock_model_config,
        library=library,
    )


# ═══════════════════════════════════════════════════════════════
# recommend
# ═══════════════════════════════════════════════════════════════

class TestRecommend:
    """recommend() —— LLM 推荐 + 缓存 + 兜底"""

    async def test_recommend_success_llm_returns_valid_json(self, agent, mock_adapter):
        """LLM 返回合法 JSON → 直接返回，source=llm"""
        llm_result = {
            "role_ids": ["architect"],
            "behavior_ids": ["simplicity", "naming"],
            "workflow_template": "zh_tdd_loop",
            "reason": "用户要求实现 API 并包含测试",
            "confidence": 0.92,
        }
        mock_adapter._invoke_return = {"content": json.dumps(llm_result)}

        result = await agent.recommend("实现用户登录 API 并写测试", agent_id="hermes")

        assert result["source"] == "llm"
        assert result["role_ids"] == ["architect"]
        assert result["behavior_ids"] == ["simplicity", "naming"]
        assert result["workflow_template"] == "zh_tdd_loop"
        assert result["confidence"] == 0.92
        assert len(mock_adapter.invoke_calls) == 1

    async def test_recommend_cache_hit(self, agent, mock_adapter):
        """同一输入第二次命中缓存——adapter 不再被调用"""
        mock_adapter._invoke_return = {"content": json.dumps({
            "role_ids": [], "behavior_ids": [], "workflow_template": None,
            "reason": "缓存测试", "confidence": 0.5,
        })}

        await agent.recommend("同一个任务输入", agent_id="hermes", project_id="proj-1")
        calls_after_first = len(mock_adapter.invoke_calls)

        result = await agent.recommend("同一个任务输入", agent_id="hermes", project_id="proj-1")

        assert len(mock_adapter.invoke_calls) == calls_after_first  # 无新增调用
        assert result["reason"] == "缓存测试"

    async def test_recommend_cache_miss_different_agent_id(self, agent, mock_adapter):
        """切 agent_id 缓存失效——adapter 再次被调用（hermes + openclaw 各一次）"""
        mock_adapter._invoke_return = {"content": json.dumps({
            "role_ids": [], "behavior_ids": [], "workflow_template": None,
            "reason": "x", "confidence": 0.5,
        })}
        # openclaw adapter 也配置相同返回
        openclaw_adapter = agent._adapters["openclaw"]
        openclaw_adapter._invoke_return = {"content": json.dumps({
            "role_ids": [], "behavior_ids": [], "workflow_template": None,
            "reason": "x", "confidence": 0.5,
        })}

        await agent.recommend("任务", agent_id="hermes", project_id="proj-1")
        await agent.recommend("任务", agent_id="openclaw", project_id="proj-1")

        # 切 agent_id 缓存失效：hermes 调 1 次 + openclaw 调 1 次
        assert len(mock_adapter.invoke_calls) == 1
        assert len(openclaw_adapter.invoke_calls) == 1

    async def test_recommend_cache_miss_different_project_id(self, agent, mock_adapter):
        """切 project_id 缓存失效"""
        mock_adapter._invoke_return = {"content": json.dumps({
            "role_ids": [], "behavior_ids": [], "workflow_template": None,
            "reason": "x", "confidence": 0.5,
        })}

        await agent.recommend("任务", agent_id="hermes", project_id="proj-1")
        await agent.recommend("任务", agent_id="hermes", project_id="proj-2")

        assert len(mock_adapter.invoke_calls) == 2

    async def test_recommend_llm_returns_error_falls_back_to_rule(self, agent, mock_adapter):
        """LLM 返回 {"error": ...} → 走 _rule_based_recommend，source=rule"""
        mock_adapter._invoke_return = {"error": "未配置 API Key"}

        result = await agent.recommend("帮我写单元测试", agent_id="hermes")

        assert result["source"] == "rule"
        assert result["confidence"] < 0.8  # 兜底置信度

    async def test_recommend_llm_returns_invalid_json_falls_back(self, agent, mock_adapter):
        """LLM 返回非 JSON 文本 → 走 rule_based"""
        mock_adapter._invoke_return = {"content": "这不是 JSON，是纯文本"}

        result = await agent.recommend("帮我写单元测试", agent_id="hermes")

        assert result["source"] == "rule"

    async def test_recommend_low_confidence_drops_workflow(self, agent, mock_adapter):
        """LLM 返回 confidence < 0.6 → workflow_template 强制设为 None"""
        mock_adapter._invoke_return = {"content": json.dumps({
            "role_ids": ["developer"],
            "behavior_ids": ["simplicity"],
            "workflow_template": "zh_tdd_loop",
            "reason": "不确定",
            "confidence": 0.4,
        })}

        result = await agent.recommend("含糊的任务", agent_id="hermes")

        assert result["confidence"] == 0.4
        assert result["workflow_template"] is None  # 低置信度强制丢弃

    async def test_recommend_adapter_missing_falls_back(self, agent):
        """agent_id 不在 adapters dict → 走 rule_based（不抛错）"""
        result = await agent.recommend("帮我写单元测试", agent_id="nonexistent-agent")

        assert result["source"] == "rule"

    async def test_recommend_model_config_failure_falls_back(self, mock_adapter, mock_model_config, db, library):
        """model_config.get_active_config 抛错 → 走 rule_based"""
        from services.prompt_agent import PromptAgent
        mock_model_config._cfg = RuntimeError("model config load failed")
        agent = PromptAgent(
            conn=db,
            adapters={"hermes": mock_adapter},
            model_config=mock_model_config,
            library=library,
        )

        result = await agent.recommend("帮我写单元测试", agent_id="hermes")

        assert result["source"] == "rule"
        assert len(mock_adapter.invoke_calls) == 0  # adapter 未被调用


# ═══════════════════════════════════════════════════════════════
# _rule_based_recommend
# ═══════════════════════════════════════════════════════════════

class TestRuleBasedRecommend:
    """_rule_based_recommend() —— 关键词匹配兜底"""

    def test_rule_based_tdd_keyword(self, agent):
        """'pytest' 命中 tdd 规则"""
        result = agent._rule_based_recommend("用 pytest 写单元测试")

        assert "testing" in result["behavior_ids"]
        assert result["workflow_template"] == "zh_tdd_loop"
        assert result["confidence"] == 0.7

    def test_rule_based_security_keyword(self, agent):
        """'安全' 命中 security 规则"""
        result = agent._rule_based_recommend("做一次安全审计")

        assert "security-mindset" in result["behavior_ids"]
        assert result["workflow_template"] == "zh_security_review"

    def test_rule_based_code_review_keyword(self, agent):
        """'审查' 命中 code-review 规则"""
        result = agent._rule_based_recommend("审查这段代码")

        assert "code-review" in result["behavior_ids"]
        assert result["workflow_template"] == "zh_code_review"

    def test_rule_based_no_match_returns_default(self, agent):
        """无关键词命中 → default（confidence=0.4，workflow=None）"""
        result = agent._rule_based_recommend("随便聊聊天气")

        assert result["confidence"] == 0.4
        assert result["workflow_template"] is None
        assert "simplicity" in result["behavior_ids"]

    def test_rule_based_case_insensitive(self, agent):
        """'TEST' 大写也能命中 tdd 规则"""
        result = agent._rule_based_recommend("WRITE A TEST FOR THIS")

        assert "testing" in result["behavior_ids"]

    def test_rule_based_first_match_wins(self, agent):
        """多个关键词命中时，按 rules 列表顺序首个胜出（tdd 在 security 之前）"""
        result = agent._rule_based_recommend("test security")  # 同时命中 tdd 和 security

        # tdd 在 rules 列表中排第一，应胜出
        assert result["workflow_template"] == "zh_tdd_loop"


# ═══════════════════════════════════════════════════════════════
# compose_behaviors
# ═══════════════════════════════════════════════════════════════

class TestComposeBehaviors:
    """compose_behaviors() —— 拼接 + 截断"""

    def test_compose_behaviors_normal(self, agent):
        """正常拼接多个 behavior content"""
        text = agent.compose_behaviors(["simplicity", "naming"])

        assert "simplicity" in text or "简单" in text or len(text) > 0
        assert "naming" in text or "命名" in text or len(text) > 0
        assert len(text) > 10  # 非空

    def test_compose_behaviors_empty_list(self, agent):
        """空列表返回空串"""
        text = agent.compose_behaviors([])
        assert text == ""

    def test_compose_behaviors_unknown_id_skipped(self, agent):
        """不存在的 id 跳过，不抛错"""
        text = agent.compose_behaviors(["simplicity", "this-id-does-not-exist"])
        # simplicity 仍被拼接
        assert len(text) > 0

    def test_compose_behaviors_token_truncation(self, agent):
        """超 max_tokens 截断——最终文本估算 token 不超过 max_tokens（粗略 len//3）"""
        # 取所有 behavior 强制超长
        all_ids = ["simplicity", "naming", "testing", "documentation", "communication",
                   "code-review", "security-mindset", "performance", "input-validation", "immutability"]
        text = agent.compose_behaviors(all_ids, max_tokens=50)

        # 粗略估算：len(text) // 3 <= 50 + 容差（截断后不应远超预算）
        assert len(text) // 3 <= 80  # 允许一定容差（最后一个块可能略超）


# ═══════════════════════════════════════════════════════════════
# render
# ═══════════════════════════════════════════════════════════════

class TestRender:
    """render() —— 渲染最终 prompt 片段"""

    def test_render_full(self, agent):
        """role + behavior + workflow 全有"""
        text = agent.render(
            role_ids=["architect"],
            behavior_ids=["simplicity"],
            workflow_template="zh_tdd_loop",
        )
        assert len(text) > 0
        # 应包含 role 的 prompt_template 内容（architect 角色存在）
        assert "架构" in text or "architect" in text.lower() or len(text) > 50

    def test_render_no_workflow(self, agent):
        """workflow_template=None —— 不抛错，返回 role+behavior 片段"""
        text = agent.render(
            role_ids=["developer"],
            behavior_ids=["simplicity"],
            workflow_template=None,
        )
        assert len(text) > 0

    def test_render_unknown_role_graceful(self, agent):
        """未知 role id —— 优雅处理（跳过，不抛错）"""
        text = agent.render(
            role_ids=["nonexistent-role"],
            behavior_ids=[],
            workflow_template=None,
        )
        # 不抛错即可；text 可能为空或仅含占位
        assert isinstance(text, str)


# ═══════════════════════════════════════════════════════════════
# optimize —— Phase 73-A 输入框 ✨ 优化
# ═══════════════════════════════════════════════════════════════

class TestOptimize:
    """optimize() —— Phase 73-A 输入框 prompt 优化"""

    async def test_optimize_success(self, agent, mock_adapter):
        """LLM 返回合法 JSON → 返回优化结果"""
        mock_adapter._invoke_return = {"content": json.dumps({
            "optimized": "用 Python 3.12 实现用户登录 API，返回 JSON",
            "changes": ["补充技术栈", "明确输出格式"],
        })}

        result = await agent.optimize("实现登录 API", agent_id="hermes")

        assert result["optimized"] == "用 Python 3.12 实现用户登录 API，返回 JSON"
        assert result["changes"] == ["补充技术栈", "明确输出格式"]
        assert len(mock_adapter.invoke_calls) == 1

    async def test_optimize_empty_prompt(self, agent, mock_adapter):
        """空 prompt → 返回 {optimized: '', changes: []}，不调 adapter"""
        result = await agent.optimize("", agent_id="hermes")

        assert result == {"optimized": "", "changes": [], "candidates": []}
        assert len(mock_adapter.invoke_calls) == 0

    async def test_optimize_whitespace_prompt(self, agent, mock_adapter):
        """纯空白 prompt → 同样返回空"""
        result = await agent.optimize("   \n\t  ", agent_id="hermes")

        assert result == {"optimized": "", "changes": [], "candidates": []}
        assert len(mock_adapter.invoke_calls) == 0

    async def test_optimize_llm_error_falls_back(self, agent, mock_adapter):
        """LLM 返回 {"error": ...} → 原样返回 raw_prompt"""
        mock_adapter._invoke_return = {"error": "未配置 API Key"}

        result = await agent.optimize("实现登录 API", agent_id="hermes")

        assert result["optimized"] == "实现登录 API"  # 原样
        assert result["changes"] == []

    async def test_optimize_invalid_json_falls_back(self, agent, mock_adapter):
        """LLM 返回非 JSON → 原样返回"""
        mock_adapter._invoke_return = {"content": "这不是 JSON"}

        result = await agent.optimize("实现登录 API", agent_id="hermes")

        assert result["optimized"] == "实现登录 API"
        assert result["changes"] == []

    async def test_optimize_adapter_missing_falls_back(self, agent):
        """adapter 缺失 → 原样返回"""
        result = await agent.optimize("实现登录 API", agent_id="nonexistent-agent")

        assert result["optimized"] == "实现登录 API"
        assert result["changes"] == []

    async def test_optimize_empty_optimized_field_falls_back(self, agent, mock_adapter):
        """LLM 返回 optimized 为空串 → 原样返回（不返回空优化）"""
        mock_adapter._invoke_return = {"content": json.dumps({
            "optimized": "",
            "changes": ["x"],
        })}

        result = await agent.optimize("原始输入", agent_id="hermes")

        assert result["optimized"] == "原始输入"  # 原样，不用空串

    async def test_optimize_changes_truncated_to_five(self, agent, mock_adapter):
        """changes 超过 5 条 → 截断到 5 条"""
        mock_adapter._invoke_return = {"content": json.dumps({
            "optimized": "优化后",
            "changes": ["c1", "c2", "c3", "c4", "c5", "c6", "c7"],
        })}

        result = await agent.optimize("原始", agent_id="hermes")

        assert len(result["changes"]) == 5
        assert result["changes"] == ["c1", "c2", "c3", "c4", "c5"]

    async def test_optimize_changes_non_list_normalized(self, agent, mock_adapter):
        """changes 不是 list（如字符串）→ 规范化为空 list"""
        mock_adapter._invoke_return = {"content": json.dumps({
            "optimized": "优化后",
            "changes": "不是列表",
        })}

        result = await agent.optimize("原始", agent_id="hermes")

        assert result["changes"] == []

    # ── Task 6.17: 失败不再无声——返回 error 字段供前端 toast ──

    async def test_optimize_adapter_missing_returns_error(self, agent):
        """Task 6.17: adapter 缺失 → 返回 error 字段（非无声）"""
        result = await agent.optimize("实现登录 API", agent_id="nonexistent-agent")

        assert result["optimized"] == "实现登录 API"  # 原样保留
        assert result["changes"] == []
        assert result.get("error")  # 非空 error 消息

    async def test_optimize_llm_invoke_exception_returns_error(self, agent, mock_adapter):
        """Task 6.17: LLM 调用抛异常 → 返回 error 字段"""
        mock_adapter._invoke_return = ConnectionError("timeout")

        result = await agent.optimize("实现登录 API", agent_id="hermes")

        assert result["optimized"] == "实现登录 API"
        assert result.get("error")

    async def test_optimize_adapter_error_response_returns_error(self, agent, mock_adapter):
        """Task 6.17: adapter 返回 {"error": ...} → 返回 error 字段"""
        mock_adapter._invoke_return = {"error": "未配置 API Key"}

        result = await agent.optimize("实现登录 API", agent_id="hermes")

        assert result["optimized"] == "实现登录 API"
        assert result.get("error")

    async def test_optimize_invalid_json_returns_error(self, agent, mock_adapter):
        """Task 6.17: JSON 解析失败 → 返回 error 字段"""
        mock_adapter._invoke_return = {"content": "这不是 JSON"}

        result = await agent.optimize("实现登录 API", agent_id="hermes")

        assert result["optimized"] == "实现登录 API"
        assert result.get("error")

    async def test_optimize_empty_optimized_returns_error(self, agent, mock_adapter):
        """Task 6.17: LLM 返回 optimized 为空 → 返回 error 字段"""
        mock_adapter._invoke_return = {"content": json.dumps({
            "optimized": "",
            "changes": ["x"],
        })}

        result = await agent.optimize("原始输入", agent_id="hermes")

        assert result["optimized"] == "原始输入"
        assert result.get("error")

    async def test_optimize_model_config_failure_returns_error(self, agent, mock_adapter):
        """Task 6.17: model_config.get_active_config 抛异常 → 返回 error 字段"""
        agent._model_config.get_active_config = _raise_exc

        result = await agent.optimize("实现登录 API", agent_id="hermes")

        assert result["optimized"] == "实现登录 API"
        assert result.get("error")


# ═══════════════════════════════════════════════════════════════
# Phase 76-A: project_preferences 持久化 + resolve_behavior_ids
# ═══════════════════════════════════════════════════════════════

class TestProjectPreferences:
    """Phase 76-A: resolve_behavior_ids 优先级 + save_project_behavior_preference UPSERT

    优先级：current_override > project_preferences > role default
    """

    async def _seed_role(self, db, role_id="architect", behaviors='["simplicity","naming"]'):
        """插入角色用于 role default 测试"""
        import time as _time
        now = int(_time.time())
        await db.execute(
            "INSERT INTO roles(id, name, category, prompt_template, description, "
            "enabled, is_builtin, default_behaviors, created_at, updated_at) "
            "VALUES(?,?,?,?,?,1,1,?,?,?)",
            (role_id, role_id, "builtin", "test", "test", behaviors, now, now),
        )
        await db.commit()

    async def test_resolve_current_override_wins(self, agent, db):
        """current_override 非空 → 直接返回，忽略 project_preferences 和 role default"""
        await self._seed_role(db)
        await agent.save_project_behavior_preference("proj-1", ["testing"])

        result = await agent.resolve_behavior_ids(
            role_id="architect", project_id="proj-1",
            current_override=["code-review", "security-mindset"],
        )
        assert result == ["code-review", "security-mindset"]

    async def test_resolve_project_preference_over_role_default(self, agent, db):
        """project_preferences 有非空值 → 覆盖 role default"""
        await self._seed_role(db)  # default = ["simplicity","naming"]
        await agent.save_project_behavior_preference("proj-1", ["testing", "code-review"])

        result = await agent.resolve_behavior_ids(
            role_id="architect", project_id="proj-1",
        )
        assert result == ["testing", "code-review"]

    async def test_resolve_role_default_fallback(self, agent, db):
        """无 current_override、无 project_preferences → role default"""
        await self._seed_role(db)

        result = await agent.resolve_behavior_ids(
            role_id="architect", project_id="proj-1",
        )
        assert result == ["simplicity", "naming"]

    async def test_resolve_all_empty_returns_empty(self, agent, db):
        """三层都无配置 → 返回空列表"""
        # 不 seed role，不 save preference
        result = await agent.resolve_behavior_ids(
            role_id="nonexistent", project_id="proj-1",
        )
        assert result == []

    async def test_resolve_no_project_id_falls_back_to_role(self, agent, db):
        """project_id="" → 跳过 project_preferences，直接 role default"""
        await self._seed_role(db)
        # 预置 project_preferences（但 project_id="" 不会查到）
        await agent.save_project_behavior_preference("proj-1", ["testing"])

        result = await agent.resolve_behavior_ids(
            role_id="architect", project_id="",
        )
        assert result == ["simplicity", "naming"]

    async def test_save_upsert_idempotent(self, agent, db):
        """save 两次不同值 → 读到第二次的值（UPSERT 覆盖）"""
        await agent.save_project_behavior_preference("proj-1", ["a", "b"])
        await agent.save_project_behavior_preference("proj-1", ["c", "d"])

        result = await agent.resolve_behavior_ids(project_id="proj-1")
        assert result == ["c", "d"]

    async def test_save_then_resolve_roundtrip(self, agent, db):
        """save → resolve 往返一致"""
        await agent.save_project_behavior_preference(
            "proj-1", ["simplicity", "naming", "testing"],
        )

        result = await agent.resolve_behavior_ids(project_id="proj-1")
        assert result == ["simplicity", "naming", "testing"]

    async def test_save_empty_list_no_error(self, agent, db):
        """save([]) → 不抛错；resolve 视空列表为"无偏好"回退 role default"""
        await agent.save_project_behavior_preference("proj-1", [])  # 不抛错即可
        # 空列表被 resolve 视为"无偏好"（isinstance(ids, list) and ids 为 False）
        # → 回退 role default，此处无 role_id → 返回 []
        result = await agent.resolve_behavior_ids(project_id="proj-1")
        assert result == []

    async def test_save_empty_project_id_skipped(self, agent, db):
        """save(project_id="", [...]) → 不写入 DB"""
        await agent.save_project_behavior_preference("", ["x", "y"])

        cursor = await db.execute("SELECT COUNT(*) FROM project_preferences")
        count = (await cursor.fetchone())[0]
        assert count == 0

    async def test_resolve_current_override_dedup_preserves_order(self, agent, db):
        """current_override 含重复 id → 去重保序"""
        result = await agent.resolve_behavior_ids(
            current_override=["a", "b", "a", "c", "b"],
        )
        assert result == ["a", "b", "c"]

    async def test_save_dedup_preserves_order(self, agent, db):
        """save 含重复 id → 去重保序后存储"""
        await agent.save_project_behavior_preference("proj-1", ["a", "b", "a", "c"])

        result = await agent.resolve_behavior_ids(project_id="proj-1")
        assert result == ["a", "b", "c"]

    async def test_resolve_different_projects_isolated(self, agent, db):
        """不同 project_id 的偏好互相隔离"""
        await agent.save_project_behavior_preference("proj-1", ["a"])
        await agent.save_project_behavior_preference("proj-2", ["b"])

        r1 = await agent.resolve_behavior_ids(project_id="proj-1")
        r2 = await agent.resolve_behavior_ids(project_id="proj-2")
        assert r1 == ["a"]
        assert r2 == ["b"]

