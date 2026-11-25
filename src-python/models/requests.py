"""models/requests.py —— Pydantic 请求体验证模型

Phase 34: 为关键 POST 端点增加请求体 schema 验证。
"""
from pydantic import BaseModel, Field
from typing import Any


class TaskSubmitRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=50000)
    agent_id: str | None = None
    session_id: str | None = None
    context: dict | None = None
    mode: str = "agent"
    language: str = "zh"


class TaskCancelRequest(BaseModel):
    task_id: str = Field(..., min_length=1)


class TaskPinRequest(BaseModel):
    pinned: int = Field(default=1, ge=0, le=1)


class TaskTagsRequest(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=20)


class WorkflowCreateRequest(BaseModel):
    name: str = Field(default="untitled", max_length=200)
    graph: Any = None
    id: str | None = None


class WorkflowUpdateRequest(BaseModel):
    name: str = Field(default="", max_length=200)
    graph: dict | None = None


class WorkflowRunRequest(BaseModel):
    initial_input: str = Field(default="", max_length=10000)


class WorkflowResumeRequest(BaseModel):
    run_id: str
    action: str = Field(default="retry", pattern="^(retry|skip|retry_with_inputs)$")
    node_overrides: dict[str, dict[str, str]] | None = None


class WorkflowTemplateEditRequest(BaseModel):
    name: str = Field(..., max_length=200)
    graph: dict


class ModelRefreshRequest(BaseModel):
    provider: str = Field(..., min_length=1)


class ModelTestRequest(BaseModel):
    provider: str
    api_key: str | None = None


class ModelActivateRequest(BaseModel):
    provider: str = Field(..., min_length=1)


class SettingsSaveRequest(BaseModel):
    key: str = Field(..., min_length=1)
    value: str


class PromptUpdateRequest(BaseModel):
    variables: dict[str, str]
    context: str | None = None


class DeliberationStartRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    roles: list[str] = Field(..., min_length=1)
    session_id: str | None = None


class RoleCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    prompt_template: str = Field(..., min_length=1)
    description: str = ""
    category: str = "custom"
    id: str | None = None


class RoleUpdateRequest(BaseModel):
    name: str | None = None
    prompt_template: str | None = None
    description: str | None = None
    category: str | None = None
    enabled: bool | None = None


class SessionNewRequest(BaseModel):
    """创建新会话——无请求体参数，但作为 API 文档标记保留"""


class GitCommitRequest(BaseModel):
    message: str = Field(..., min_length=1)


class GitBranchRequest(BaseModel):
    branch: str = Field(..., min_length=1)


class GitCheckoutRequest(BaseModel):
    branch: str = Field(..., min_length=1)


class GitStashRequest(BaseModel):
    message: str = ""


class NotificationReadRequest(BaseModel):
    """标记通知已读——通过 URL 参数传 ID，无请求体"""


class ArenaVoteRequest(BaseModel):
    task_id: str = ""
    agent_id: str = ""
    rating: int = Field(default=0, ge=1, le=5)
    adopted: bool = False
    model: str = ""
    provider: str = ""


class SkillInstallRequest(BaseModel):
    source_dir: str = Field(..., min_length=1)


class SkillRateRequest(BaseModel):
    rating: int = Field(default=0, ge=1, le=5)


class SkillCreateRequest(BaseModel):
    id: str = ""  # auto-generated if empty
    title: str = Field(..., min_length=1, max_length=200)
    description: str = ""
    prompt_text: str = ""
    slash_command: str = ""
    command_description: str = ""
    workflow_graph: dict | None = None
    author: str = "Yuncat User"
    category: str = "custom"
    version: str = "1.0.0"
    tags: list[str] = Field(default_factory=list)
    icon: str = "📦"
    scope_tasks: list[str] = Field(default_factory=list)
    scope_languages: list[str] = Field(default_factory=list)


class ConfigImportRequest(BaseModel):
    version: str
    models: list[dict] = Field(default_factory=list)
    rules: list[dict] = Field(default_factory=list)
    workflows: list[dict] = Field(default_factory=list)
    roles: list[dict] = Field(default_factory=list)
    knowledge_nodes: list[dict] = Field(default_factory=list)
    conversations: list[dict] = Field(default_factory=list)


class ConfigExportRequest(BaseModel):
    """导出配置——无请求体，通过 URL 参数/query 传参"""
    session_id: str | None = None


class CompactRequest(BaseModel):
    session_id: str = Field(..., min_length=1)


class ContextStatsRequest(BaseModel):
    session_id: str = Field(..., min_length=1)


class GenerateWorkflowTemplateRequest(BaseModel):
    description: str = Field(..., min_length=1)
    name: str = "AI Generated"
