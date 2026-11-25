"""tests/unit/test_route_pydantic_models.py — Pydantic 模型校验测试

Phase 2.3 修复: routes/skills.py + routes/roles.py 高频端点用
await request.json() + 手动校验，缺乏类型检查。改为 FastAPI 标准的
Pydantic 模型注入 + _parse_request helper。

本测试验证:
1. schemas/skills.py 7 个模型的字段约束
2. schemas/roles.py 2 个模型的字段约束
3. _parse_request helper 的错误处理（无效 JSON / 校验失败）
4. 校验失败返回 {"ok": False, "error": "Field '...': ..."} 格式
"""
import os
import sys
import pytest

_SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src-python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from pydantic import ValidationError

from schemas.skills import (
    InstallSkillRequest,
    GenerateWorkflowTemplateRequest,
    InstallSkillFromGitRequest,
    InstallSkillFromMarketplaceRequest,
    CheckSkillRequest,
    RateSkillRequest,
    CreateSkillRequest,
)
from schemas.roles import PromoteTempRoleRequest, RejectTempRoleRequest


# ============================================================
# 1. schemas/skills.py 模型校验
# ============================================================

class TestInstallSkillRequest:
    def test_valid(self):
        req = InstallSkillRequest(source_dir="/path/to/skill/")
        assert req.source_dir == "/path/to/skill/"

    def test_missing_source_dir(self):
        with pytest.raises(ValidationError):
            InstallSkillRequest()

    def test_empty_source_dir(self):
        with pytest.raises(ValidationError):
            InstallSkillRequest(source_dir="")

    def test_too_long_source_dir(self):
        with pytest.raises(ValidationError):
            InstallSkillRequest(source_dir="x" * 1025)


class TestGenerateWorkflowTemplateRequest:
    def test_valid(self):
        req = GenerateWorkflowTemplateRequest(description="Build a REST API")
        assert req.description == "Build a REST API"
        assert req.name == "AI Generated"  # 默认值

    def test_missing_description(self):
        with pytest.raises(ValidationError):
            GenerateWorkflowTemplateRequest()

    def test_empty_description(self):
        with pytest.raises(ValidationError):
            GenerateWorkflowTemplateRequest(description="")

    def test_custom_name(self):
        req = GenerateWorkflowTemplateRequest(description="test", name="Custom Name")
        assert req.name == "Custom Name"


class TestInstallSkillFromGitRequest:
    def test_valid(self):
        req = InstallSkillFromGitRequest(url="https://github.com/user/repo")
        assert req.url == "https://github.com/user/repo"
        assert req.branch is None  # 默认

    def test_with_branch(self):
        req = InstallSkillFromGitRequest(url="https://github.com/user/repo", branch="develop")
        assert req.branch == "develop"

    def test_missing_url(self):
        with pytest.raises(ValidationError):
            InstallSkillFromGitRequest()

    def test_empty_url(self):
        with pytest.raises(ValidationError):
            InstallSkillFromGitRequest(url="")


class TestInstallSkillFromMarketplaceRequest:
    def test_valid(self):
        req = InstallSkillFromMarketplaceRequest(skill_id="my-skill")
        assert req.skill_id == "my-skill"
        assert req.source == "agentskills.io"  # 默认

    def test_custom_source(self):
        req = InstallSkillFromMarketplaceRequest(skill_id="x", source="custom.io")
        assert req.source == "custom.io"

    def test_missing_skill_id(self):
        with pytest.raises(ValidationError):
            InstallSkillFromMarketplaceRequest()


class TestCheckSkillRequest:
    def test_valid(self):
        req = CheckSkillRequest(source_dir="/path/to/skill/")
        assert req.source_dir == "/path/to/skill/"

    def test_missing_source_dir(self):
        with pytest.raises(ValidationError):
            CheckSkillRequest()

    def test_empty_source_dir(self):
        with pytest.raises(ValidationError):
            CheckSkillRequest(source_dir="")


class TestRateSkillRequest:
    def test_valid_min(self):
        req = RateSkillRequest(rating=1)
        assert req.rating == 1

    def test_valid_max(self):
        req = RateSkillRequest(rating=5)
        assert req.rating == 5

    def test_below_range(self):
        with pytest.raises(ValidationError):
            RateSkillRequest(rating=0)

    def test_above_range(self):
        with pytest.raises(ValidationError):
            RateSkillRequest(rating=6)

    def test_missing_rating(self):
        with pytest.raises(ValidationError):
            RateSkillRequest()

    def test_non_integer(self):
        with pytest.raises(ValidationError):
            RateSkillRequest(rating="not-a-number")


class TestCreateSkillRequest:
    def test_valid_minimal(self):
        req = CreateSkillRequest()
        assert req.version == "1.0.0"
        assert req.category == "custom"
        assert req.tags == []

    def test_with_all_fields(self):
        req = CreateSkillRequest(
            id="custom-test",
            title="Test Skill",
            description="A test",
            tags=["python", "test"],
            prompt_text="You are a test assistant",
            scope_tasks=["agent"],
        )
        assert req.id == "custom-test"
        assert req.title == "Test Skill"
        assert "python" in req.tags
        assert req.prompt_text == "You are a test assistant"


# ============================================================
# 2. schemas/roles.py 模型校验
# ============================================================

class TestPromoteTempRoleRequest:
    def test_valid(self):
        req = PromoteTempRoleRequest(role_name="architect")
        assert req.role_name == "architect"
        assert req.node_id == ""  # 默认（可选）

    def test_with_node_id(self):
        req = PromoteTempRoleRequest(node_id="node-123", role_name="reviewer")
        assert req.node_id == "node-123"

    def test_missing_role_name(self):
        with pytest.raises(ValidationError):
            PromoteTempRoleRequest()

    def test_empty_role_name(self):
        with pytest.raises(ValidationError):
            PromoteTempRoleRequest(role_name="")


class TestRejectTempRoleRequest:
    def test_valid(self):
        req = RejectTempRoleRequest(node_id="node-456")
        assert req.node_id == "node-456"

    def test_missing_node_id(self):
        with pytest.raises(ValidationError):
            RejectTempRoleRequest()

    def test_empty_node_id(self):
        with pytest.raises(ValidationError):
            RejectTempRoleRequest(node_id="")


# ============================================================
# 3. _parse_request helper 测试
# ============================================================

class TestParseRequestHelper:
    """测试 routes/skills.py 的 _parse_request helper。"""

    def _make_mock_request(self, body_dict=None, body_json=None, invalid_json=False):
        """构造 mock Request 对象。"""
        import json
        from unittest.mock import AsyncMock, MagicMock

        request = MagicMock()
        if invalid_json:
            request.json = AsyncMock(side_effect=Exception("Invalid JSON"))
        else:
            data = body_json if body_json is not None else body_dict
            request.json = AsyncMock(return_value=data if data is not None else {})
        return request

    @pytest.mark.asyncio
    async def test_valid_request_returns_model(self):
        """有效请求返回 (model_instance, None)。"""
        from routes.skills import _parse_request
        request = self._make_mock_request(body_dict={"source_dir": "/path/to/skill/"})
        model, err = await _parse_request(request, InstallSkillRequest)
        assert err is None
        assert isinstance(model, InstallSkillRequest)
        assert model.source_dir == "/path/to/skill/"

    @pytest.mark.asyncio
    async def test_invalid_json_returns_error(self):
        """无效 JSON 返回 (None, {"ok": False, "error": "Invalid JSON"})。"""
        from routes.skills import _parse_request
        request = self._make_mock_request(invalid_json=True)
        model, err = await _parse_request(request, InstallSkillRequest)
        assert model is None
        assert err is not None
        assert err["ok"] is False
        assert "Invalid JSON" in err["error"]

    @pytest.mark.asyncio
    async def test_validation_error_returns_field_info(self):
        """校验失败返回 (None, {"ok": False, "error": "Field '...': ..."})。"""
        from routes.skills import _parse_request
        # 缺少必填字段 source_dir
        request = self._make_mock_request(body_dict={})
        model, err = await _parse_request(request, InstallSkillRequest)
        assert model is None
        assert err is not None
        assert err["ok"] is False
        # 错误信息应包含字段名
        assert "source_dir" in err["error"]

    @pytest.mark.asyncio
    async def test_validation_error_for_empty_string(self):
        """空字符串触发校验错误并返回字段信息。"""
        from routes.skills import _parse_request
        request = self._make_mock_request(body_dict={"source_dir": ""})
        model, err = await _parse_request(request, InstallSkillRequest)
        assert model is None
        assert err is not None
        assert err["ok"] is False
        assert "source_dir" in err["error"]

    @pytest.mark.asyncio
    async def test_rate_skill_out_of_range_returns_error(self):
        """评分超出 1-5 范围返回校验错误。"""
        from routes.skills import _parse_request
        request = self._make_mock_request(body_dict={"rating": 10})
        model, err = await _parse_request(request, RateSkillRequest)
        assert model is None
        assert err is not None
        assert "rating" in err["error"]


# ============================================================
# 4. 静态检查: routes 使用 Pydantic 模型
# ============================================================

def test_skills_routes_uses_pydantic_models():
    """静态检查: routes/skills.py 导入并使用 schemas.skills 模型。"""
    path = os.path.join(_SRC, "routes", "skills.py")
    with open(path, encoding="utf-8") as f:
        source = f.read()
    # 应导入所有 7 个模型
    for model in (
        "InstallSkillRequest",
        "GenerateWorkflowTemplateRequest",
        "InstallSkillFromGitRequest",
        "InstallSkillFromMarketplaceRequest",
        "CheckSkillRequest",
        "RateSkillRequest",
        "CreateSkillRequest",
    ):
        assert model in source, f"routes/skills.py must import {model}"


def test_roles_routes_uses_pydantic_models():
    """静态检查: routes/roles.py 导入并使用 schemas.roles 模型。"""
    path = os.path.join(_SRC, "routes", "roles.py")
    with open(path, encoding="utf-8") as f:
        source = f.read()
    assert "PromoteTempRoleRequest" in source
    assert "RejectTempRoleRequest" in source


def test_skills_routes_uses_parse_request_helper():
    """静态检查: routes/skills.py 定义并使用 _parse_request helper。"""
    path = os.path.join(_SRC, "routes", "skills.py")
    with open(path, encoding="utf-8") as f:
        source = f.read()
    assert "async def _parse_request" in source
    # 应在多个路由中调用 _parse_request
    assert source.count("_parse_request(request,") >= 5, (
        "routes/skills.py should call _parse_request in at least 5 routes"
    )
