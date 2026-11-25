"""tests/contract/test_workflow_templates.py —— 内置工作流模板契约测试"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class TestBuiltinTemplates:
    """内置模板定义完整性"""

    def test_three_templates_defined(self):
        """应该有 3 个内置模板"""
        from prompts.workflow_templates import BUILTIN_TEMPLATES
        names = [t["name"] for t in BUILTIN_TEMPLATES]
        assert "zh_data_analysis" in names
        assert "zh_code_review" in names
        assert "zh_doc_generation" in names

    def test_templates_have_valid_structure(self):
        """每个模板有 name + graph.nodes + graph.edges"""
        from prompts.workflow_templates import BUILTIN_TEMPLATES
        for tmpl in BUILTIN_TEMPLATES:
            assert "name" in tmpl
            assert "graph" in tmpl
            assert "nodes" in tmpl["graph"]
            assert "edges" in tmpl["graph"]
            assert len(tmpl["graph"]["nodes"]) > 0

    def test_templates_are_valid_dags(self):
        """内置模板的图是无环的"""
        from prompts.workflow_templates import BUILTIN_TEMPLATES
        from workflow.engine import WorkflowEngine
        engine = WorkflowEngine()
        for tmpl in BUILTIN_TEMPLATES:
            nodes = {n["id"]: n for n in tmpl["graph"]["nodes"]}
            engine._validate_graph(nodes, tmpl["graph"]["edges"])
            # 不抛异常即为通过

    def test_data_analysis_has_chain(self):
        """data_analysis 模板有顺序依赖"""
        from prompts.workflow_templates import BUILTIN_TEMPLATES
        tmpl = next(t for t in BUILTIN_TEMPLATES if t["name"] == "zh_data_analysis")
        node_ids = {n["id"] for n in tmpl["graph"]["nodes"]}
        assert "read_csv" in node_ids
        assert "write_report" in node_ids

    def test_code_review_has_parallel(self):
        """code_review 模板有 lint+review 并行"""
        from prompts.workflow_templates import BUILTIN_TEMPLATES
        tmpl = next(t for t in BUILTIN_TEMPLATES if t["name"] == "zh_code_review")
        edges = tmpl["graph"]["edges"]
        # lint → generate_suggestions 和 review → generate_suggestions 并行
        targets = [e["to"] for e in edges if e["to"] == "generate_suggestions"]
        assert len(targets) == 2

    # ── Phase 76-B: zh_api_design 垂直模板验证 ──

    def test_zh_api_design_exists(self):
        """Phase 76-B: zh_api_design 模板存在于 BUILTIN_TEMPLATES"""
        from prompts.workflow_templates import BUILTIN_TEMPLATES
        names = [t["name"] for t in BUILTIN_TEMPLATES]
        assert "zh_api_design" in names

    def test_zh_api_design_has_chain_structure(self):
        """Phase 76-B: zh_api_design 是 5 节点链式 DAG
        requirement → contract → implement → document → test
        """
        from prompts.workflow_templates import BUILTIN_TEMPLATES
        tmpl = next(t for t in BUILTIN_TEMPLATES if t["name"] == "zh_api_design")
        node_ids = {n["id"] for n in tmpl["graph"]["nodes"]}
        # 5 个节点全部存在
        assert node_ids == {"requirement", "contract", "implement", "document", "test"}
        # 4 条边形成链
        edges = tmpl["graph"]["edges"]
        assert len(edges) == 4
        edge_pairs = {(e["from"], e["to"]) for e in edges}
        assert ("requirement", "contract") in edge_pairs
        assert ("contract", "implement") in edge_pairs
        assert ("implement", "document") in edge_pairs
        assert ("document", "test") in edge_pairs

    def test_zh_api_design_is_valid_dag(self):
        """Phase 76-B: zh_api_design 通过 DAG 校验（无环）"""
        from prompts.workflow_templates import BUILTIN_TEMPLATES
        from workflow.engine import WorkflowEngine
        engine = WorkflowEngine()
        tmpl = next(t for t in BUILTIN_TEMPLATES if t["name"] == "zh_api_design")
        nodes = {n["id"]: n for n in tmpl["graph"]["nodes"]}
        engine._validate_graph(nodes, tmpl["graph"]["edges"])  # 不抛异常即通过

    def test_all_template_names_unique(self):
        """Phase 76-B: 所有 BUILTIN_TEMPLATES 的 name 唯一（无重复）"""
        from prompts.workflow_templates import BUILTIN_TEMPLATES
        names = [t["name"] for t in BUILTIN_TEMPLATES]
        assert len(names) == len(set(names)), f"Duplicate template names: {names}"
