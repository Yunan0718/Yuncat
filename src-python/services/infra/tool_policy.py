"""services/infra/tool_policy.py —— Phase 51-B: 工具策略层

按 agent/provider 过滤可用工具，支持 group 与具名 allow/deny。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

_log = logging.getLogger("yuncat.tool_policy")

_POLICY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "config", "tools", "tools_policy.yaml",
)


@dataclass
class ToolPolicy:
    """工具策略：default + byProvider 覆盖。"""

    profile: str = "default"
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    by_provider: dict[str, "ToolPolicy"] = field(default_factory=dict)
    group_map: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | None = None) -> "ToolPolicy":
        """从 YAML 加载策略；文件不存在时返回空策略（全部放行）。"""
        path = path or _POLICY_PATH
        if not os.path.exists(path):
            _log.debug("Tool policy file not found: %s", path)
            return cls()
        try:
            import yaml as _yaml
            with open(path, encoding="utf-8") as f:
                data = _yaml.safe_load(f) or {}
        except Exception:
            _log.warning("Failed to load tool policy, falling back to allow-all", exc_info=True)
            return cls()

        default = data.get("default") or {}
        by_provider: dict[str, ToolPolicy] = {}
        for provider, cfg in (data.get("byProvider") or {}).items():
            by_provider[provider] = cls(
                profile=cfg.get("profile", provider),
                allow=list(cfg.get("allow") or []),
                deny=list(cfg.get("deny") or []),
            )

        return cls(
            profile=default.get("profile", "default"),
            allow=list(default.get("allow") or []),
            deny=list(default.get("deny") or []),
            by_provider=by_provider,
            group_map={k: list(v) for k, v in (data.get("groups") or {}).items()},
        )

    def for_provider(self, provider: str) -> "ToolPolicy":
        """获取 provider 级别的策略（继承 default 后覆盖）。"""
        override = self.by_provider.get(provider)
        if not override:
            return self
        # 合并：override 非空则完全覆盖对应字段；否则继承 default
        return ToolPolicy(
            profile=override.profile or self.profile,
            allow=override.allow if override.allow else self.allow,
            deny=override.deny if override.deny else self.deny,
            by_provider=self.by_provider,
            group_map=self.group_map,
        )

    def is_allowed(
        self,
        tool_name: str,
        tool_groups: list[str] | None = None,
        provider: str | None = None,
    ) -> bool:
        """判断工具是否允许使用。"""
        policy = self.for_provider(provider) if provider else self

        # 1. 显式 deny 最高优先级
        if tool_name in policy.deny:
            return False
        for group in tool_groups or []:
            if f"group:{group}" in policy.deny:
                return False

        # 2. 若 tool_meta 分组为空，尝试从 group_map 反推
        resolved_groups = set(tool_groups or [])
        for group, members in policy.group_map.items():
            if tool_name in members:
                resolved_groups.add(group)

        # 3. allow 列表为空表示全部放行（除 deny 外）
        if not policy.allow:
            return True

        # 4. 工具名在 allow 中
        if tool_name in policy.allow:
            return True

        # 5. 任意 group 在 allow 中
        for group in resolved_groups:
            if f"group:{group}" in policy.allow:
                return True

        return False

    def filter_tools(
        self,
        tools: list[dict],
        provider: str | None = None,
    ) -> list[dict]:
        """过滤工具列表。工具 dict 需包含 name，可选 groups。"""
        return [
            t for t in tools
            if self.is_allowed(
                tool_name=t.get("name", ""),
                tool_groups=t.get("groups") or [],
                provider=provider,
            )
        ]


def _global_policy() -> ToolPolicy:
    """全局策略单例（延迟加载）。"""
    if not hasattr(_global_policy, "_instance"):
        _global_policy._instance = ToolPolicy.load()  # type: ignore[attr-defined]
    return _global_policy._instance  # type: ignore[attr-defined]


def get_policy() -> ToolPolicy:
    """获取当前工具策略。"""
    return _global_policy()


def reload_policy() -> ToolPolicy:
    """重新加载工具策略。"""
    _global_policy._instance = ToolPolicy.load()  # type: ignore[attr-defined]
    return _global_policy._instance  # type: ignore[attr-defined]
