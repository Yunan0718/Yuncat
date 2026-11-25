"""tests/unit/test_commander_behaviors.py —— Phase 75 Behavior 组合接入 Commander

覆盖：
1. render_commander_prompt 的 selected_behaviors 参数
2. _load_behaviors(selected_ids) 按 manifest priority 排序
3. _load_selected_behaviors 未知 id 跳过 / 超长截断
4. _load_manifest_priorities 加载 manifest
5. schema.py roles 表 default_behaviors 列 + COLUMN_MIGRATIONS
6. seed_roles.py 元组 7 元素 + default_behaviors 配置 + _seed_roles 回填逻辑
7. agent_context 传递 selected_behaviors
8. manifest.yaml 所有 id 在 yaml 文件中能找到 content
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

# 用 commander 模块的 __file__ 算 beh_dir，避免测试文件相对路径错误
import prompts.commander as _cmd
_BEH_DIR = os.path.join(os.path.dirname(_cmd.__file__), "behaviors")


# ═══════════════════════════════════════════════════════════════
# render_commander_prompt + selected_behaviors
# ═══════════════════════════════════════════════════════════════

class TestRenderCommanderSelectedBehaviors:
    """render_commander_prompt 的 selected_behaviors 参数"""

    def test_render_without_selected_behaviors_returns_nonempty(self):
        """无 selected_behaviors → 沿用 Phase 27 旧逻辑，返回非空 prompt"""
        from prompts.commander import render_commander_prompt
        prompt = render_commander_prompt(agent_id="hermes")
        assert len(prompt) > 100
        # 应包含 commander 静态前缀关键词
        assert "research_first" in prompt or "truthfulness" in prompt

    def test_render_with_selected_behaviors_includes_content(self):
        """selected_behaviors=["simplicity"] → prompt 包含 simplicity 的 content"""
        from prompts.commander import render_commander_prompt
        prompt = render_commander_prompt(
            agent_id="hermes", selected_behaviors=["simplicity"],
        )
        # simplicity 的 content 含 "KISS" 或 "简单"
        assert "KISS" in prompt or "简单" in prompt

    def test_render_with_empty_selected_behaviors_falls_back(self):
        """selected_behaviors=[] → 等同 None，沿用旧逻辑"""
        from prompts.commander import render_commander_prompt
        prompt_default = render_commander_prompt(agent_id="hermes")
        prompt_empty = render_commander_prompt(agent_id="hermes", selected_behaviors=[])
        # 两者都非空（旧逻辑会加载 general.yaml 等内容）
        assert len(prompt_default) > 100
        assert len(prompt_empty) > 100

    def test_render_with_unknown_behavior_id_skipped(self):
        """selected_behaviors=["unknown-id-xxx"] → 未知 id 跳过，不抛错"""
        from prompts.commander import render_commander_prompt
        # 不应抛错
        prompt = render_commander_prompt(
            agent_id="hermes", selected_behaviors=["unknown-id-xxx"],
        )
        assert len(prompt) > 100  # commander 基础 prompt 仍存在

    def test_render_with_multiple_behaviors_sorted_by_priority(self):
        """多个 behavior → 按 manifest priority 排序（simplicity 100 > naming 95）"""
        from prompts.commander import render_commander_prompt, _load_behaviors
        # 直接测 _load_behaviors 的排序
        text = _load_behaviors(selected_ids=["naming", "simplicity"])
        # simplicity priority=100 应排在 naming priority=95 前面
        simplicity_pos = text.find("KISS")
        naming_pos = text.find("命名")
        assert simplicity_pos >= 0
        assert naming_pos >= 0
        assert simplicity_pos < naming_pos  # simplicity 在前


# ═══════════════════════════════════════════════════════════════
# _load_selected_behaviors
# ═══════════════════════════════════════════════════════════════

class TestLoadSelectedBehaviors:
    """_load_behaviors(selected_ids) 显式选择分支"""

    def test_load_selected_single_behavior(self):
        """单个 behavior → 返回其 content"""
        from prompts.commander import _load_behaviors
        text = _load_behaviors(selected_ids=["security-mindset"])
        assert "安全" in text or "security" in text.lower()

    def test_load_selected_multiple_behaviors(self):
        """多个 behavior → 全部包含"""
        from prompts.commander import _load_behaviors
        text = _load_behaviors(selected_ids=["simplicity", "naming", "testing"])
        assert "KISS" in text or "简单" in text
        assert "命名" in text
        assert "测试" in text or "test" in text.lower()

    def test_load_selected_unknown_id_skipped(self):
        """未知 id 跳过，已知 id 仍加载"""
        from prompts.commander import _load_behaviors
        text = _load_behaviors(selected_ids=["simplicity", "totally-unknown-xxx"])
        assert "KISS" in text or "简单" in text

    def test_load_selected_all_unknown_returns_empty(self):
        """全部未知 id → 返回空串（不抛错）"""
        from prompts.commander import _load_behaviors
        text = _load_behaviors(selected_ids=["unknown-1", "unknown-2"])
        assert text == ""

    def test_load_selected_vertical_fragment(self):
        """vertical.yaml 的片段也能加载"""
        from prompts.commander import _load_behaviors
        text = _load_behaviors(selected_ids=["api-design"])
        assert "API" in text or "api" in text.lower()

    def test_load_selected_truncation_at_max_chars(self):
        """超 5000 字符截断——最终文本 ≤ 5000 + 容差"""
        from prompts.commander import _load_behaviors
        # 选大量 behavior 强制超长
        all_ids = [
            "simplicity", "naming", "code-review", "testing", "security-mindset",
            "input-validation", "error-communication", "documentation", "performance",
            "immutability", "file-organization", "communication", "git-workflow",
            "dependency-management",
        ]
        text = _load_behaviors(selected_ids=all_ids)
        # 截断后不应远超 5000（允许一定容差因最后一个块可能略超）
        assert len(text) <= 5500


# ═══════════════════════════════════════════════════════════════
# _load_manifest_priorities
# ═══════════════════════════════════════════════════════════════

class TestLoadManifestPriorities:
    """_load_manifest_priorities 加载 manifest.yaml"""

    def test_manifest_loads_known_ids(self):
        """manifest 含已知 behavior id"""
        from prompts.commander import _load_manifest_priorities
        priorities = _load_manifest_priorities(_BEH_DIR)
        assert "simplicity" in priorities
        assert "naming" in priorities
        assert "security-mindset" in priorities

    def test_manifest_simplicity_has_highest_priority(self):
        """simplicity priority=100 是通用 behavior 中最高的"""
        from prompts.commander import _load_manifest_priorities
        priorities = _load_manifest_priorities(_BEH_DIR)
        assert priorities["simplicity"] == 100
        assert priorities["naming"] == 95
        assert priorities["code-review"] == 90

    def test_manifest_language_specific_behaviors_present(self):
        """manifest 含语言特定 behavior"""
        from prompts.commander import _load_manifest_priorities
        priorities = _load_manifest_priorities(_BEH_DIR)
        assert "python-coding-standards" in priorities
        assert "ts-react" in priorities
        assert "go-idioms" in priorities


# ═══════════════════════════════════════════════════════════════
# manifest.yaml 完整性——所有 id 在 yaml 文件中能找到 content
# ═══════════════════════════════════════════════════════════════

class TestManifestIntegrity:
    """manifest.yaml 所有注册的 id 都能在 yaml 文件中找到 content"""

    def test_all_manifest_ids_have_content(self):
        """manifest 中每个 id 都能在 general/python/golang/typescript.yaml 找到 content"""
        try:
            import yaml
        except ImportError:
            pytest.skip("yaml not installed")
        # 加载 manifest
        manifest_path = os.path.join(_BEH_DIR, "manifest.yaml")
        with open(manifest_path, encoding="utf-8") as f:
            manifest = yaml.safe_load(f) or {}
        manifest_ids = {b["id"] for b in manifest.get("behaviors", []) if "id" in b}

        # 加载所有 content（不含 vertical.yaml——那是片段，不在 manifest）
        content_ids: set[str] = set()
        for fname in ["general.yaml", "python.yaml", "golang.yaml", "typescript.yaml"]:
            path = os.path.join(_BEH_DIR, fname)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if isinstance(data, dict):
                content_ids.update(data.keys())

        # manifest 中每个 id 都应有对应 content
        missing = manifest_ids - content_ids
        assert not missing, f"manifest 注册但 yaml 无 content 的 id: {missing}"


# ═══════════════════════════════════════════════════════════════
# schema.py + seed_roles.py
# ═══════════════════════════════════════════════════════════════

class TestSchemaAndSeedRoles:
    """schema.py roles 表 + seed_roles.py default_behaviors 字段"""

    def test_schema_sql_roles_has_default_behaviors(self):
        """SCHEMA_SQL 的 roles 表含 default_behaviors 列"""
        from repositories.schema import SCHEMA_SQL
        assert "default_behaviors" in SCHEMA_SQL

    def test_column_migrations_has_default_behaviors(self):
        """COLUMN_MIGRATIONS 含 roles.default_behaviors"""
        from repositories.schema import COLUMN_MIGRATIONS
        found = any(
            t == "roles" and c == "default_behaviors"
            for t, c, _ in COLUMN_MIGRATIONS
        )
        assert found

    def test_seed_roles_tuple_has_7_elements(self):
        """SEED_ROLES 每个元组 7 元素（含 default_behaviors）"""
        from repositories.seed_roles import SEED_ROLES
        for entry in SEED_ROLES:
            assert len(entry) == 7, f"元组不是 7 元素: {entry[0]}"

    def test_seed_roles_architect_has_behaviors(self):
        """architect 角色配置了 ["simplicity","naming"]"""
        from repositories.seed_roles import SEED_ROLES
        architect = next(e for e in SEED_ROLES if e[0] == "architect")
        behaviors = json.loads(architect[6])
        assert "simplicity" in behaviors
        assert "naming" in behaviors

    def test_seed_roles_security_has_behaviors(self):
        """security 角色配置了 ["security-mindset"]"""
        from repositories.seed_roles import SEED_ROLES
        security = next(e for e in SEED_ROLES if e[0] == "security")
        behaviors = json.loads(security[6])
        assert "security-mindset" in behaviors

    def test_seed_roles_default_behaviors_is_valid_json(self):
        """所有 default_behaviors 字段是合法 JSON 字符串"""
        from repositories.seed_roles import SEED_ROLES
        for entry in SEED_ROLES:
            behaviors_str = entry[6]
            behaviors = json.loads(behaviors_str)  # 不抛错即合法
            assert isinstance(behaviors, list)


# ═══════════════════════════════════════════════════════════════
# _seed_roles 回填逻辑——模块级 db fixture（避免类级 fixture 作用域问题）
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
async def db(tmp_path):
    """aiosqlite + SCHEMA_SQL——每个测试独立 DB"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_commander_behaviors.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    yield conn
    await conn.close()


class TestSeedRolesBackfill:
    """_seed_roles 回填逻辑——已存在 role + DB 为 '[]' + seed 非空 → UPDATE"""

    async def test_seed_roles_inserts_new_role_with_behaviors(self, db):
        """新 DB——_seed_roles INSERT 含 default_behaviors"""
        from repositories.seed_roles import _seed_roles
        await _seed_roles(db)
        cursor = await db.execute("SELECT default_behaviors FROM roles WHERE id=?", ("architect",))
        row = await cursor.fetchone()
        assert row is not None
        behaviors = json.loads(row[0])
        assert "simplicity" in behaviors

    async def test_seed_roles_backfills_existing_empty_behaviors(self, db):
        """已存在 role + DB default_behaviors='[]' + seed 非空 → UPDATE"""
        import time
        from repositories.seed_roles import _seed_roles
        # 先手动插入一个 architect，default_behaviors 为 '[]'
        await db.execute(
            "INSERT INTO roles(id, name, category, prompt_template, description, "
            "enabled, is_builtin, default_behaviors, created_at, updated_at) "
            "VALUES(?,?,?,?,?,1,1,'[]',?,?)",
            ("architect", "测试", "test", "prompt", "desc", int(time.time()), int(time.time())),
        )
        await db.commit()
        # 执行 _seed_roles
        await _seed_roles(db)
        # 验证 default_behaviors 被回填
        cursor = await db.execute("SELECT default_behaviors FROM roles WHERE id=?", ("architect",))
        row = await cursor.fetchone()
        behaviors = json.loads(row[0])
        assert "simplicity" in behaviors
        assert "naming" in behaviors

    async def test_seed_roles_does_not_overwrite_user_custom(self, db):
        """已存在 role + DB default_behaviors 非空（用户自定义）→ 不覆盖"""
        import time
        from repositories.seed_roles import _seed_roles
        custom_behaviors = '["my-custom-behavior"]'
        await db.execute(
            "INSERT INTO roles(id, name, category, prompt_template, description, "
            "enabled, is_builtin, default_behaviors, created_at, updated_at) "
            "VALUES(?,?,?,?,?,1,1,?,?,?)",
            ("architect", "测试", "test", "prompt", "desc",
             custom_behaviors, int(time.time()), int(time.time())),
        )
        await db.commit()
        await _seed_roles(db)
        cursor = await db.execute("SELECT default_behaviors FROM roles WHERE id=?", ("architect",))
        row = await cursor.fetchone()
        # 用户自定义不被覆盖
        assert row[0] == custom_behaviors

    async def test_seed_roles_idempotent(self, db):
        """_seed_roles 幂等——连续执行两次结果相同"""
        from repositories.seed_roles import _seed_roles
        await _seed_roles(db)
        cursor1 = await db.execute("SELECT COUNT(*) FROM roles")
        count1 = (await cursor1.fetchone())[0]
        await _seed_roles(db)
        cursor2 = await db.execute("SELECT COUNT(*) FROM roles")
        count2 = (await cursor2.fetchone())[0]
        assert count1 == count2


# ═══════════════════════════════════════════════════════════════
# agent_context 传递 selected_behaviors
# ═══════════════════════════════════════════════════════════════

class TestAgentContextPassesBehaviors:
    """agent_context.build_agent_context 从 extra_context 读 selected_behaviors 传给 commander"""

    def test_agent_context_source_reads_selected_behaviors(self):
        """agent_context.py 源码含 selected_behaviors 读取逻辑"""
        src_path = os.path.join(
            os.path.dirname(os.path.dirname(_cmd.__file__)),
            "services", "agent", "agent_context.py",
        )
        with open(src_path, encoding="utf-8") as f:
            src = f.read()
        assert "selected_behaviors" in src
        assert 'extra_context' in src and 'selected_behaviors' in src

    def test_commander_render_accepts_selected_behaviors_param(self):
        """render_commander_prompt 签名含 selected_behaviors 参数"""
        import inspect
        from prompts.commander import render_commander_prompt
        sig = inspect.signature(render_commander_prompt)
        assert "selected_behaviors" in sig.parameters
        # 默认值应为 None
        assert sig.parameters["selected_behaviors"].default is None
