"""tests/unit/test_workflow_service.py —— 工作流服务单元测试"""
import pytest
import os
import tempfile
import aiosqlite

from repositories import init_db, run_migrations


@pytest.mark.unit
class TestWorkflowService:
    @pytest.fixture(autouse=True)
    async def setup_db(self):
        from services.workflow.workflow_service import WorkflowService
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = await init_db(path)
        await run_migrations(self.db)
        self.svc = WorkflowService(self.db)
        yield
        await self.db.close()
        os.unlink(path)

    async def test_create_workflow(self):
        wid = await self.svc.create({"name": "test-wf", "graph": {"nodes": [], "edges": []}})
        assert wid
        wf = await self.svc.get(wid)
        assert wf["name"] == "test-wf"
        assert wf["nodes"] == []
        assert wf["edges"] == []

    async def test_list_all(self):
        await self.svc.create({"name": "wf1", "graph": {"nodes": [], "edges": []}})
        await self.svc.create({"name": "wf2", "graph": {"nodes": [], "edges": []}})
        all_wfs = await self.svc.list_all()
        assert len(all_wfs) == 2

    async def test_get_nonexistent_returns_none(self):
        wf = await self.svc.get("nonexistent-id")
        assert wf is None

    async def test_delete_workflow(self):
        wid = await self.svc.create({"name": "to-delete", "graph": {"nodes": [], "edges": []}})
        await self.svc.delete(wid)
        assert await self.svc.get(wid) is None

    async def test_create_with_graph_string(self):
        """前端可能传 JSON 字符串而非对象"""
        wid = await self.svc.create({"name": "string-graph", "graph": '{"nodes":[],"edges":[]}'})
        wf = await self.svc.get(wid)
        assert wf["nodes"] == []
        assert wf["edges"] == []

    async def test_list_templates(self):
        # 插入一个模板
        import json
        tmpl_id = os.urandom(8).hex()
        await self.db.execute(
            "INSERT INTO workflows(id, name, graph, is_template) VALUES(?,?,?,1)",
            (tmpl_id, "tmpl1", json.dumps({"nodes": [{"id":"a"}], "edges": []})),
        )
        await self.db.commit()
        templates = await self.svc.list_templates()
        assert any(t["name"] == "tmpl1" for t in templates)

    async def test_run_template_not_found(self):
        result = await self.svc.run_template("nonexistent-template", "")
        assert result.startswith("template-not-found:")

    async def test_update_snapshot(self):
        import json
        wid = await self.svc.create({"name": "snap-test", "graph": {"nodes": [], "edges": []}})
        # Create a run row first
        run_id = os.urandom(8).hex()
        await self.db.execute(
            "INSERT INTO workflow_runs(id, workflow_id, status, started_at) VALUES(?,?,?,?)",
            (run_id, wid, "running", 1234567890),
        )
        await self.db.commit()

        snap = json.dumps({"context": {}, "node_states": {}, "node_results": {}})
        await self.svc.update_snapshot(run_id, snap)

        cursor = await self.db.execute("SELECT snapshot FROM workflow_runs WHERE id=?", (run_id,))
        row = await cursor.fetchone()
        assert row[0] == snap

    # ============================================================
    # Phase 99: 工作流作用域与分类
    # ============================================================
    async def test_create_with_scope_and_project_id(self):
        """Phase 99: 创建 project 作用域工作流"""
        wid = await self.svc.create({
            "name": "proj-wf", "graph": {"nodes": [], "edges": []},
            "scope": "project", "project_id": "proj-1",
        })
        wf = await self.svc.get(wid)
        assert wf["scope"] == "project"
        assert wf["project_id"] == "proj-1"

    async def test_create_with_category_and_owner(self):
        """Phase 99: 创建带分类和所有者的工作流"""
        wid = await self.svc.create({
            "name": "auto-wf", "graph": {"nodes": [], "edges": []},
            "category": "automation", "owner_id": "hermes",
        })
        wf = await self.svc.get(wid)
        assert wf["category"] == "automation"
        assert wf["owner_id"] == "hermes"

    async def test_create_default_scope_is_global(self):
        """Phase 99: 不传 scope 默认为 'global'，category 默认为 'custom'"""
        wid = await self.svc.create({"name": "default-wf", "graph": {"nodes": [], "edges": []}})
        wf = await self.svc.get(wid)
        assert wf["scope"] == "global"
        assert wf["category"] == "custom"
        assert wf["project_id"] is None
        assert wf["owner_id"] is None

    async def test_list_filter_by_scope(self):
        """Phase 99: 按 scope 过滤——只返回 project 作用域"""
        await self.svc.create({"name": "g1", "graph": {"nodes": [], "edges": []}})
        await self.svc.create({
            "name": "p1", "graph": {"nodes": [], "edges": []},
            "scope": "project", "project_id": "proj-1",
        })
        project_wfs = await self.svc.list_all(scope="project")
        assert len(project_wfs) == 1
        assert project_wfs[0]["name"] == "p1"
        assert project_wfs[0]["scope"] == "project"

    async def test_list_filter_by_project_id(self):
        """Phase 99: 按 project_id 过滤——只返回该项目的"""
        await self.svc.create({
            "name": "p1", "graph": {"nodes": [], "edges": []},
            "scope": "project", "project_id": "proj-1",
        })
        await self.svc.create({
            "name": "p2", "graph": {"nodes": [], "edges": []},
            "scope": "project", "project_id": "proj-2",
        })
        proj1_wfs = await self.svc.list_all(project_id="proj-1")
        assert len(proj1_wfs) == 1
        assert proj1_wfs[0]["name"] == "p1"

    async def test_list_filter_by_category(self):
        """Phase 99: 按 category 过滤"""
        await self.svc.create({
            "name": "auto1", "graph": {"nodes": [], "edges": []},
            "category": "automation",
        })
        await self.svc.create({
            "name": "tmpl1", "graph": {"nodes": [], "edges": []},
            "category": "template",
        })
        auto_wfs = await self.svc.list_all(category="automation")
        assert len(auto_wfs) == 1
        assert auto_wfs[0]["name"] == "auto1"

    async def test_list_combined_filter(self):
        """Phase 99: 组合过滤 scope + project_id + category"""
        await self.svc.create({
            "name": "match", "graph": {"nodes": [], "edges": []},
            "scope": "project", "project_id": "proj-1", "category": "automation",
        })
        await self.svc.create({
            "name": "no-match-1", "graph": {"nodes": [], "edges": []},
            "scope": "project", "project_id": "proj-1", "category": "template",
        })
        await self.svc.create({
            "name": "no-match-2", "graph": {"nodes": [], "edges": []},
            "scope": "global", "category": "automation",
        })
        result = await self.svc.list_all(
            scope="project", project_id="proj-1", category="automation",
        )
        assert len(result) == 1
        assert result[0]["name"] == "match"

    async def test_list_all_no_filter_returns_all(self):
        """Phase 99: 无过滤返回全部（回归）"""
        await self.svc.create({"name": "g1", "graph": {"nodes": [], "edges": []}})
        await self.svc.create({
            "name": "p1", "graph": {"nodes": [], "edges": []},
            "scope": "project", "project_id": "proj-1",
        })
        all_wfs = await self.svc.list_all()
        assert len(all_wfs) == 2
        # 验证返回字段含新字段
        for w in all_wfs:
            assert "scope" in w
            assert "project_id" in w
            assert "category" in w
            assert "owner_id" in w

    async def test_get_returns_new_fields(self):
        """Phase 99: get 返回新字段"""
        wid = await self.svc.create({
            "name": "full-wf", "graph": {"nodes": [], "edges": []},
            "scope": "project", "project_id": "proj-1",
            "category": "agent_skill", "owner_id": "openclaw",
        })
        wf = await self.svc.get(wid)
        assert wf["scope"] == "project"
        assert wf["project_id"] == "proj-1"
        assert wf["category"] == "agent_skill"
        assert wf["owner_id"] == "openclaw"
