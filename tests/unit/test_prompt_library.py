"""tests/unit/test_prompt_library.py —— Phase 72 统一 PromptLibrary 单元测试

验证：
1. catalog 合并（DB+YAML roles，behaviors，workflows 三类目录）
2. list_roles DB 优先去重（同 id 时 DB 版本胜出）
3. get_role DB 优先 + YAML 回退
4. list_behaviors 加载 YAML flat mapping
5. list_workflows 仅元数据；get_workflow_template 含 graph
6. search 跨类型过滤（q / category / type）
7. seed_yaml_roles_to_db 幂等（同 id 跳过）+ 插入新 id
8. 响应 schema 必需字段存在
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────

@pytest.fixture
async def db(tmp_path):
    """内存 aiosqlite + SCHEMA_SQL——每个测试独立 DB"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_prompt_lib.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    yield conn
    await conn.close()


@pytest.fixture
def library(db):
    """PromptLibrary 实例——注入 db 连接"""
    from services.prompt_library import PromptLibrary
    return PromptLibrary(db)


# ─────────────────────────────────────────────────────────────
# catalog
# ─────────────────────────────────────────────────────────────

class TestCatalog:
    """catalog() 返回顶层分类目录"""

    async def test_returns_three_categories(self, library):
        """返回 roles/behaviors/workflows 三个分类"""
        cat = await library.catalog()
        ids = [c["id"] for c in cat["categories"]]
        assert "roles" in ids
        assert "behaviors" in ids
        assert "workflows" in ids
        assert len(cat["categories"]) == 3

    async def test_each_category_has_count(self, library):
        """每个分类条目含 count 字段（整数）"""
        cat = await library.catalog()
        for c in cat["categories"]:
            assert isinstance(c["count"], int)
            assert c["count"] >= 0

    async def test_counts_match_list_methods(self, library):
        """catalog 计数与对应 list_* 方法返回长度一致"""
        cat = await library.catalog()
        counts = {c["id"]: c["count"] for c in cat["categories"]}
        assert counts["roles"] == len(await library.list_roles())
        assert counts["behaviors"] == len(library.list_behaviors())
        assert counts["workflows"] == len(library.list_workflows())

    async def test_category_has_name_field(self, library):
        """每个分类含非空 name 字段（用于前端显示）"""
        cat = await library.catalog()
        for c in cat["categories"]:
            assert c.get("name"), f"category {c.get('id')} name 为空"


# ─────────────────────────────────────────────────────────────
# list_roles / get_role —— DB + YAML 合并去重
# ─────────────────────────────────────────────────────────────

class TestRolesMerge:
    """list_roles DB+YAML 合并，DB 优先；get_role DB 优先 + YAML 回退"""

    async def test_list_roles_includes_yaml_when_db_empty(self, library):
        """DB 为空时 list_roles 返回 YAML 角色列表"""
        roles = await library.list_roles()
        # 项目至少有 architect.yaml 一个 YAML 角色
        assert len(roles) > 0
        ids = [r["id"] for r in roles]
        assert "architect" in ids

    async def test_list_roles_db_priority_when_id_conflict(self, library, db):
        """DB 与 YAML 同 id 时，DB 版本胜出（不重复）"""
        from repositories.roles_repo import create_role
        # 插入 DB 版本 architect（prompt_template 与 YAML 不同）
        await create_role(
            db, "architect", "DB 架构师（覆盖）",
            prompt_template="DB 版本的架构师 prompt",
            description="从 DB 来", category="custom",
        )
        roles = await library.list_roles()
        architects = [r for r in roles if r["id"] == "architect"]
        assert len(architects) == 1, "同 id 不应重复"
        assert architects[0]["name"] == "DB 架构师（覆盖）"
        assert "DB 版本" in architects[0]["prompt_template"]

    async def test_list_roles_includes_db_only_ids(self, library, db):
        """DB 新增 id（YAML 无）应出现在结果中"""
        from repositories.roles_repo import create_role
        await create_role(
            db, "custom-role-1", "自定义角色",
            prompt_template="custom prompt",
        )
        roles = await library.list_roles()
        ids = [r["id"] for r in roles]
        assert "custom-role-1" in ids
        assert "architect" in ids  # YAML 仍提供

    async def test_get_role_db_priority(self, library, db):
        """get_role DB 优先返回 DB 版本"""
        from repositories.roles_repo import create_role
        await create_role(
            db, "architect", "DB 版本",
            prompt_template="DB 版本 prompt",
        )
        role = await library.get_role("architect")
        assert role is not None
        assert role["name"] == "DB 版本"

    async def test_get_role_fallback_to_yaml(self, library):
        """DB 无此 id 时 get_role 回退到 YAML"""
        role = await library.get_role("architect")
        assert role is not None
        assert role["id"] == "architect"
        assert role.get("prompt_template"), "YAML 角色应有 prompt_template"

    async def test_get_role_returns_none_for_unknown(self, library):
        """未知 id 返回 None"""
        role = await library.get_role("nonexistent-xyz-12345")
        assert role is None

    async def test_role_has_required_schema_fields(self, library):
        """角色响应含必需字段：id/name/prompt_template"""
        roles = await library.list_roles()
        for r in roles:
            assert "id" in r
            assert "name" in r
            assert "prompt_template" in r


# ─────────────────────────────────────────────────────────────
# behaviors —— YAML flat mapping
# ─────────────────────────────────────────────────────────────

class TestBehaviors:
    """list_behaviors 加载 YAML flat mapping"""

    def test_returns_at_least_general_behaviors(self, library):
        """至少有 general.yaml 的 14 条 behavior"""
        behaviors = library.list_behaviors()
        assert len(behaviors) >= 14, f"应至少 14 条 behavior，实际 {len(behaviors)}"

    def test_includes_git_workflow_behavior(self, library):
        """包含 git-workflow 这条 behavior（来自 general.yaml）"""
        behaviors = library.list_behaviors()
        ids = [b["id"] for b in behaviors]
        assert "git-workflow" in ids

    def test_get_behavior_by_id(self, library):
        """按 id 获取单个 behavior"""
        b = library.get_behavior("git-workflow")
        assert b is not None
        assert b["id"] == "git-workflow"
        assert "Git 工作流" in b.get("content", "")

    def test_get_behavior_returns_none_for_unknown(self, library):
        """未知 id 返回 None"""
        assert library.get_behavior("nonexistent-xyz") is None

    def test_behavior_has_required_schema_fields(self, library):
        """behavior 响应含必需字段：id/type/category/name/content"""
        behaviors = library.list_behaviors()
        for b in behaviors:
            assert b["id"]
            assert b["type"] == "behavior"
            assert b["category"] == "behaviors"
            assert b["name"]
            assert b["content"], "behavior content 不应为空"

    def test_behavior_has_language_field(self, library):
        """behavior 含 language 字段（general/python/golang/typescript）"""
        behaviors = library.list_behaviors()
        langs = {b["language"] for b in behaviors}
        assert "general" in langs


# ─────────────────────────────────────────────────────────────
# workflows —— BUILTIN_TEMPLATES
# ─────────────────────────────────────────────────────────────

class TestWorkflows:
    """list_workflows 仅元数据；get_workflow_template 含 graph"""

    def test_list_workflows_returns_at_least_one(self, library):
        """至少有 1 个工作流模板"""
        workflows = library.list_workflows()
        assert len(workflows) >= 1

    def test_list_workflows_excludes_graph_field(self, library):
        """list_workflows 不应包含 graph 字段（仅元数据）"""
        workflows = library.list_workflows()
        for w in workflows:
            assert "graph" not in w, f"workflow {w['id']} 不应含 graph"

    def test_workflow_has_required_schema_fields(self, library):
        """workflow 元数据含必需字段：id/type/category/name/description"""
        workflows = library.list_workflows()
        for w in workflows:
            assert w["id"]
            assert w["type"] == "workflow"
            assert w["category"] == "workflows"
            assert "name" in w
            assert "description" in w

    def test_get_workflow_template_includes_graph(self, library):
        """get_workflow_template 返回完整定义含 graph"""
        # 取第一个 workflow name 作为查询
        workflows = library.list_workflows()
        if not workflows:
            pytest.skip("无 BUILTIN_TEMPLATES")
        first_id = workflows[0]["id"]
        tpl = library.get_workflow_template(first_id)
        assert tpl is not None
        assert "graph" in tpl
        assert "nodes" in tpl["graph"]
        assert "edges" in tpl["graph"]

    def test_get_workflow_template_returns_none_for_unknown(self, library):
        """未知 name 返回 None"""
        assert library.get_workflow_template("nonexistent-xyz-12345") is None


# ─────────────────────────────────────────────────────────────
# search —— 跨类型过滤
# ─────────────────────────────────────────────────────────────

class TestSearch:
    """search 跨类型搜索 + 过滤"""

    async def test_no_filter_returns_all_types(self, library):
        """无过滤时返回所有类型（至少含 role/behavior/workflow 各一条）"""
        results = await library.search()
        types = {r["type"] for r in results}
        assert "role" in types
        assert "behavior" in types
        assert "workflow" in types

    async def test_q_keyword_filters(self, library):
        """q 关键词过滤——只返回匹配项"""
        results = await library.search(q="git")
        assert len(results) > 0
        for r in results:
            text = f"{r.get('name', '')} {r.get('description', '')} {r.get('id', '')} {r.get('content', '')}".lower()
            assert "git" in text, f"未匹配的关键词在结果中: {r.get('id')}"

    async def test_category_roles_only(self, library):
        """category=roles 只返回角色"""
        results = await library.search(category="roles")
        for r in results:
            assert r["category"] == "roles"

    async def test_category_behaviors_only(self, library):
        """category=behaviors 只返回行为约束"""
        results = await library.search(category="behaviors")
        for r in results:
            assert r["category"] == "behaviors"

    async def test_category_workflows_only(self, library):
        """category=workflows 只返回工作流"""
        results = await library.search(category="workflows")
        for r in results:
            assert r["category"] == "workflows"

    async def test_type_role_filter(self, library):
        """type=role 等价于 category=roles"""
        results = await library.search(type="role")
        for r in results:
            assert r["type"] == "role"

    async def test_combined_q_and_category(self, library):
        """q + category 组合过滤"""
        results = await library.search(q="git", category="behaviors")
        assert len(results) > 0
        for r in results:
            assert r["category"] == "behaviors"
            text = f"{r.get('name', '')} {r.get('content', '')} {r.get('id', '')}".lower()
            assert "git" in text

    async def test_search_result_has_unified_schema(self, library):
        """搜索结果含统一字段：id/type/category/name"""
        results = await library.search()
        for r in results:
            assert "id" in r
            assert "type" in r
            assert "category" in r
            assert "name" in r


# ─────────────────────────────────────────────────────────────
# seed_yaml_roles_to_db —— 幂等种子
# ─────────────────────────────────────────────────────────────

class TestSeedYamlRoles:
    """seed_yaml_roles_to_db 幂等插入 YAML 角色到 DB"""

    async def test_inserts_new_roles(self, db):
        """首次调用应插入 YAML 角色"""
        from services.prompt_library import seed_yaml_roles_to_db
        inserted = await seed_yaml_roles_to_db(db)
        # 至少插入 architect 一个
        assert inserted >= 1

    async def test_idempotent_on_second_call(self, db):
        """第二次调用应返回 0（已存在的 id 跳过）"""
        from services.prompt_library import seed_yaml_roles_to_db
        first = await seed_yaml_roles_to_db(db)
        second = await seed_yaml_roles_to_db(db)
        assert first >= 1
        assert second == 0, f"第二次应返回 0，实际 {second}"

    async def test_seeded_roles_have_source_builtin(self, db):
        """种子后的角色 source='builtin'"""
        from services.prompt_library import seed_yaml_roles_to_db
        await seed_yaml_roles_to_db(db)
        cursor = await db.execute(
            "SELECT id, source, is_builtin FROM roles WHERE id=?", ("architect",)
        )
        row = await cursor.fetchone()
        assert row is not None, "architect 应已被种子到 DB"
        assert row[1] == "builtin", f"source 应为 'builtin'，实际 '{row[1]}'"
        assert row[2] == 1, "is_builtin 应为 1"

    async def test_seed_does_not_overwrite_existing_db_role(self, db):
        """DB 已有同 id 时 seed 应跳过（保留 DB 版本）"""
        from repositories.roles_repo import create_role
        from services.prompt_library import seed_yaml_roles_to_db
        # 先插入 DB 版本
        await create_role(
            db, "architect", "原 DB 版本",
            prompt_template="原 DB prompt",
        )
        # seed 应跳过
        inserted = await seed_yaml_roles_to_db(db)
        # architect 已存在，不应被覆盖
        from repositories.roles_repo import get_role
        role = await get_role(db, "architect")
        assert role["name"] == "原 DB 版本", "seed 不应覆盖已有 DB 角色"

    async def test_seed_makes_db_priority_in_list_roles(self, db):
        """种子后，list_roles 返回的是 DB 版本（source=builtin）"""
        from services.prompt_library import seed_yaml_roles_to_db, PromptLibrary
        await seed_yaml_roles_to_db(db)
        lib = PromptLibrary(db)
        roles = await lib.list_roles()
        architects = [r for r in roles if r["id"] == "architect"]
        assert len(architects) == 1
        # DB 种子后应只有 1 条（DB 优先，YAML 不重复）
        # 验证 source 为 builtin（DB 版本）
        assert architects[0].get("source") == "builtin" or architects[0].get("is_builtin") is True


# ─────────────────────────────────────────────────────────────
# 端到端：seed → catalog 计数一致
# ─────────────────────────────────────────────────────────────

class TestEndToEnd:
    """端到端：种子后 catalog 计数与 list 一致"""

    async def test_after_seed_catalog_roles_count_matches(self, db):
        """seed 后 catalog roles count == len(list_roles())"""
        from services.prompt_library import seed_yaml_roles_to_db, PromptLibrary
        await seed_yaml_roles_to_db(db)
        lib = PromptLibrary(db)
        cat = await lib.catalog()
        counts = {c["id"]: c["count"] for c in cat["categories"]}
        assert counts["roles"] == len(await lib.list_roles())

    async def test_after_seed_no_duplicate_roles(self, db):
        """seed 后 list_roles 不应出现重复 id"""
        from services.prompt_library import seed_yaml_roles_to_db, PromptLibrary
        await seed_yaml_roles_to_db(db)
        lib = PromptLibrary(db)
        roles = await lib.list_roles()
        ids = [r["id"] for r in roles]
        assert len(ids) == len(set(ids)), f"角色 id 重复: {[i for i in ids if ids.count(i) > 1]}"
