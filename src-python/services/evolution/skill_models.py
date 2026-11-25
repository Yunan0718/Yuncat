"""services/evolution/skill_models.py —— Skill 数据模型

Phase 47 修复（阶段 3 拆分）：从 skill_registry.py 抽出。
定义 SkillPrompt / Skill dataclass + to_dict 序列化。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from config import APP_VERSION


@dataclass
class SkillPrompt:
    """单个 prompt 挂载点配置。"""
    text: str
    placement: str = "append"  # append | prepend | replace
    priority: int = 50         # 0-100，越高越前
    scope_tasks: list[str] = field(default_factory=list)
    scope_languages: list[str] = field(default_factory=list)
    scope_frameworks: list[str] = field(default_factory=list)
    scope_modes: list[str] = field(default_factory=list)


@dataclass
class Skill:
    """单个 Skill 的完整描述"""
    id: str                    # 全局唯一 ID
    version: str = "1.0.0"
    title: str = ""
    description: str = ""
    author: str = ""
    tags: list[str] = field(default_factory=list)
    category: str = "general"
    icon: str = "\U0001f4e6"
    requires: list[str] = field(default_factory=list)
    min_yuncat: str = APP_VERSION
    # 四模块
    prompts: list[SkillPrompt] = field(default_factory=list)
    workflows: list[dict] = field(default_factory=list)   # DAG 模板列表
    commands: list[dict] = field(default_factory=list)     # Slash Command 列表
    roles: list[dict] = field(default_factory=list)        # 角色列表
    rules_dir: str = ""                                    # 规则目录路径
    # Phase 29 v3: 安全沙箱
    permissions: dict = field(default_factory=lambda: {
        "allow_file_access": False,
        "allow_command_exec": False,
        "allow_network": False,
        "allow_settings_write": False,
    })
    # 运行时
    installed_at: float = 0.0
    enabled: bool = True
    is_system: bool = False     # 系统预装 Skill 不可卸载
    install_path: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "version": self.version, "title": self.title,
            "description": self.description, "author": self.author,
            "tags": self.tags, "category": self.category, "icon": self.icon,
            "enabled": self.enabled, "is_system": self.is_system,
            "prompts_count": len(self.prompts),
            "workflows_count": len(self.workflows),
            "commands_count": len(self.commands),
            "roles_count": len(self.roles),
            "permissions": self.permissions,
        }
