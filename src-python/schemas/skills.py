"""schemas/skills.py —— routes/skills.py 的 Pydantic 请求模型

字段约束用宽松模式（min_length=1, max_length=512 等大边界），
避免拒绝原本通过的前端请求。
"""
from typing import Any, Optional

from pydantic import BaseModel, Field


class InstallSkillRequest(BaseModel):
    """从本地目录安装 Skill。"""
    source_dir: str = Field(..., min_length=1, max_length=1024,
                            description="Skill 源目录的绝对路径")


class GenerateWorkflowTemplateRequest(BaseModel):
    """用 LLM 根据需求描述自动生成 DAG 工作流模板。"""
    description: str = Field(..., min_length=1, max_length=4096,
                             description="需求描述")
    name: str = Field("AI Generated", max_length=128,
                      description="模板名称")


class InstallSkillFromGitRequest(BaseModel):
    """从 Git 仓库安装 Skill。"""
    url: str = Field(..., min_length=1, max_length=2048,
                    description="Git 仓库 URL")
    branch: Optional[str] = Field(None, max_length=256,
                                  description="分支名（可选）")


class InstallSkillFromMarketplaceRequest(BaseModel):
    """从 agentskills.io 安装 Skill。"""
    skill_id: str = Field(..., min_length=1, max_length=256,
                         description="Marketplace 中的 Skill ID")
    source: str = Field("agentskills.io", max_length=256,
                       description="来源（默认 agentskills.io）")


class CheckSkillRequest(BaseModel):
    """安装前检测冲突 / 安全审查——共用相同 schema。

    两个端点（check-conflicts / check-security）都接受 source_dir。
    """
    source_dir: str = Field(..., min_length=1, max_length=1024,
                            description="Skill 源目录路径")


class RateSkillRequest(BaseModel):
    """评分 Skill。"""
    rating: int = Field(..., ge=1, le=5,
                        description="评分 1-5")


class CreateSkillRequest(BaseModel):
    """GUI 创作向导——从表单数据生成 skill.yaml。

    字段宽松（大量 Optional），因为不同类型 Skill 可能只填部分模块。
    嵌套结构（workflow_graph 等）用 dict[str, Any] 而非严格模型，
    避免破坏现有前端提交格式。
    """
    id: Optional[str] = Field(None, max_length=128,
                              description="Skill ID（不填则自动生成 custom-xxx）")
    version: str = Field("1.0.0", max_length=32)
    title: str = Field("", max_length=256)
    description: str = Field("", max_length=2048)
    author: str = Field("Yuncat User", max_length=128)
    category: str = Field("custom", max_length=64)
    icon: str = Field("\U0001f4e6", max_length=16)
    tags: list[str] = Field(default_factory=list)

    # prompt 模块
    prompt_text: Optional[str] = Field(None,
                                       description="prompt.md 内容")
    scope_tasks: list[str] = Field(default_factory=list)
    scope_languages: list[str] = Field(default_factory=list)

    # workflow 模块
    workflow_graph: Optional[dict[str, Any]] = Field(None,
                                                     description="workflow.yaml 内容")

    # commands 模块
    slash_command: Optional[str] = Field(None, max_length=128)
    command_description: str = Field("", max_length=512)
