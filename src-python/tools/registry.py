"""tools/registry.py —— v40 fail-closed 工具工厂

ToolMeta 为唯一注册格式（架构铁律 #6）。
新增工具忘记声明并发安全 → 自动串行执行。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from config import (
    ADAPTER_OC_TOOL_DESC_MAX_CHARS, DEFAULT_AGENT_ID, HERMES_AGENT_ID,
    OPENCLAW_AGENT_ID, YUNCAT_AGENT_ID,
)

_log = logging.getLogger("yuncat.tools")

# 全局工具注册表（唯一来源）
_tools: dict[str, ToolMeta] = {}
# Phase 33: 工具使用频率计数器
_tool_usage_count: dict[str, int] = {}
# Phase 48 T3-17: OC 工具懒加载标志——避免重复读 yaml + 重复注册
_oc_tools_loaded: bool = False


def _make_oc_tool_proxy(tool_name: str):
    """Phase 48 T3-17: 构造 OC 工具代理 handler——转发到 delegate_to_agent(target_agent="openclaw")。

    hermes LLM 看到 OC 工具名后直接调用 → ToolService.execute → 此 handler →
    delegate_to_agent → capability_router.delegate_tool → openclaw adapter。
    """
    async def _proxy(**kwargs):
        from tools.builtin.system_tool_config import delegate_to_agent
        return await delegate_to_agent(
            target_agent=OPENCLAW_AGENT_ID, tool_name=tool_name, arguments=kwargs or {},
        )
    return _proxy


def _load_oc_tools_to_registry() -> None:
    """Phase 48 T3-17: 将 OC yaml 工具注册到 _tools（闭合 get_tool() 找不到的断点）。

    原 list_tools 仅临时构造 OC 工具描述但不写入 _tools → get_tool() miss →
    hermes 调用 OC 工具时走错误的 HA 代理路径（agent_tool_loop.py:170）。
    现统一注册到 _tools，handler 走 delegate_to_agent 转发到 OC 后端。
    """
    global _oc_tools_loaded
    if _oc_tools_loaded:
        return
    _oc_tools_loaded = True
    try:
        import yaml as _yaml, os as _os
        _oc_path = _os.path.join(
            _os.path.dirname(_os.path.dirname(__file__)),
            "config", "tools", "openclaw.yaml",
        )
        if not _os.path.exists(_oc_path):
            return
        with open(_oc_path, encoding="utf-8") as _f:
            _oc_manifest = _yaml.safe_load(_f)
        for _t in (_oc_manifest.get("tools") or []):
            _name = _t["name"]
            if _name in _tools:  # 不覆盖已注册工具
                continue
            _tools[_name] = ToolMeta(
                name=_name,
                func=_make_oc_tool_proxy(_name),
                # 仅 hermes 可见（OC 自己有原生工具系统；原生 yuncat agent 对外能力
                # 走 delegate_to_agent）。注意勿写 DEFAULT_AGENT_ID——它已从 hermes
                # 漂移为 yuncat，曾导致这批工具误挂给原生 agent（见 2026-08-31 修复）。
                agents=[HERMES_AGENT_ID],
                description=f"[OpenClaw网关] {_t.get('description','')[:ADAPTER_OC_TOOL_DESC_MAX_CHARS]}",
                parameters=_t.get("parameters", {"type": "object", "properties": {}}),
                confirmable=False,
                is_read_only=False,
                is_concurrency_safe=True,  # 委托转发，无本地副作用
                groups=_t.get("groups", []),  # Phase 51-B
            )
    except Exception:
        _log.warning("加载 OpenClaw YAML 工具列表失败，跳过", exc_info=True)


@dataclass
class ToolMeta:
    """工具元数据——唯一注册格式。

    架构铁律 #6: 不维护第二套 _tools: dict 注册表。

    concurrency_safety (Phase 45 D.7):
      - "safe":       完全并发安全（如 read_file），可与任何工具并行
      - "safe_with_lock": 需轻量锁（如 search_files），同组串行不同组并行
      - "exclusive":  必须独占执行（如 write_file/run_command），等所有 safe 完成后

    groups (Phase 51-B): 工具分组，用于工具策略层按 group 过滤。
    """
    name: str
    func: Callable
    agents: list[str]
    description: str
    parameters: dict
    confirmable: bool = False
    is_concurrency_safe: bool = False  # v40: 向后兼容，Phase 45 D.7 后优先读 concurrency_safety
    is_read_only: bool = False
    concurrency_safety: str | None = None  # Phase 45 D.7
    always_allow: bool = False  # Phase 43 W.8: 可跳过审批
    tool_timeout: float | None = None  # Phase 47: 对标 Claude Code，None=无超时
    groups: list[str] = field(default_factory=list)  # Phase 51-B: 工具分组
    max_result_size_chars: int | None = None  # Phase 56-P2: 工具自我声明结果大小上限


def tool(
    name: str,
    agents: list[str],
    description: str,
    parameters: dict,
    confirmable: bool = False,
    is_concurrency_safe: bool = False,
    is_read_only: bool = False,
    concurrency_safety: str | None = None,
    always_allow: bool = False,
    tool_timeout: float | None = None,  # Phase 47: 对标 Claude Code per-tool 超时
    groups: list[str] | None = None,  # Phase 51-B: 工具分组
    max_result_size_chars: int | None = None,  # Phase 56-P2: 工具自我声明结果大小上限
):
    """fail-closed 工具注册装饰器。

    必须显式声明 is_concurrency_safe=True 才允许并行执行。
    Phase 45 D.7: concurrency_safety="safe"/"safe_with_lock"/"exclusive"。
    None=继承 is_concurrency_safe 行为（向后兼容）。
    """
    def decorator(func: Callable):
        _tools[name] = ToolMeta(
            name=name,
            func=func,
            agents=agents,
            description=description,
            parameters=parameters,
            confirmable=confirmable,
            is_concurrency_safe=is_concurrency_safe,
            is_read_only=is_read_only,
            concurrency_safety=concurrency_safety,
            always_allow=always_allow,  # Phase 43 W.8
            tool_timeout=tool_timeout,  # Phase 47
            groups=groups or [],  # Phase 51-B
            max_result_size_chars=max_result_size_chars,  # Phase 56-P2
        )
        return func
    return decorator


def register_tool(meta: ToolMeta, *, overwrite: bool = False) -> None:
    """公开 API：运行时注册工具（对标 Claude Code MCP 工具扩展）。

    幂等：同名工具默认拒绝覆盖（fail-safe，防意外覆盖原生工具）。
    overwrite=True 时显式覆盖。

    Phase 47 4.2: 替代 mcp_bridge.py 中 _registry._tools[name] = ... 的私有访问。

    Args:
        meta: ToolMeta 实例（含 name, func, agents, description, parameters, ...）
        overwrite: 是否覆盖同名工具

    Raises:
        ValueError: 同名工具已存在且 overwrite=False
        TypeError: meta 不是 ToolMeta 实例
    """
    if not isinstance(meta, ToolMeta):
        raise TypeError(f"meta must be ToolMeta, got {type(meta).__name__}")
    if not meta.name:
        raise ValueError("ToolMeta.name must not be empty")
    if not meta.func or not callable(meta.func):
        raise ValueError("ToolMeta.func must be a callable")
    if not overwrite and meta.name in _tools:
        existing = _tools[meta.name]
        # 幂等：完全相同的 meta 静默跳过（MCP 重复发现）
        if existing is meta:
            return
        raise ValueError(
            f"Tool '{meta.name}' already registered. "
            f"Use overwrite=True to override."
        )
    _tools[meta.name] = meta
    _log.debug("Registered tool: %s (overwrite=%s)", meta.name, overwrite)


def unregister_tool(name: str) -> bool:
    """公开 API：注销工具（如 MCP 服务器断开时清理其工具）。

    Returns:
        True 如果工具存在且已移除；False 如果工具不存在。
    """
    if name in _tools:
        del _tools[name]
        _tool_usage_count.pop(name, None)
        _log.debug("Unregistered tool: %s", name)
        return True
    return False


def get_tool(name: str) -> ToolMeta | None:
    """获取单个工具元数据"""
    return _tools.get(name)


def resolve_tool_name(name: str) -> str | None:
    """CodeWhale 借鉴: 5 级工具名模糊解析。

    LLM 偶尔会拼写工具名（如 ReadFile / read-file / read_file_tool），
    精确匹配失败时按以下优先级尝试解析:

    1. ASCII 大小写不敏感精确匹配
    2. 连字符/空格 → 下划线 (read-file → read_file)
    3. CamelCase → snake_case (ReadFile → read_file)
    4. 去除 _tool / -tool 后缀 (read_file_tool → read_file)
    5. 模糊前缀匹配 (至少 3 字符，唯一匹配时返回)

    Returns: 匹配到的工具名，或 None（无匹配）。
    """
    if not name or not _tools:
        return None
    # Level 0: 精确匹配（快速路径）
    if name in _tools:
        return name

    name_lower = name.lower()

    # Level 1: ASCII 大小写不敏感
    for key in _tools:
        if key.lower() == name_lower:
            return key

    # Level 2: 连字符/空格 → 下划线
    normalized = name_lower.replace("-", "_").replace(" ", "_")
    if normalized in _tools:
        return normalized
    if normalized != name_lower:
        for key in _tools:
            if key.lower() == normalized:
                return key

    # Level 3: CamelCase → snake_case
    import re as _re
    camel_to_snake = _re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    camel_to_snake = _re.sub(r"([a-z\d])([A-Z])", r"\1_\2", camel_to_snake).lower()
    if camel_to_snake in _tools:
        return camel_to_snake
    if camel_to_snake != name_lower:
        for key in _tools:
            if key.lower() == camel_to_snake:
                return key

    # Level 4: 去除 _tool / -tool 后缀
    for suffix in ("_tool", "-tool", "tool"):
        if name_lower.endswith(suffix) and len(name_lower) > len(suffix):
            stripped = name_lower[:-len(suffix)]
            if stripped in _tools:
                return stripped
            stripped_norm = stripped.replace("-", "_").replace(" ", "_")
            if stripped_norm in _tools:
                return stripped_norm

    # Level 5: 模糊前缀匹配（至少 3 字符，唯一匹配时返回）
    if len(name_lower) >= 3:
        matches = [k for k in _tools if k.lower().startswith(name_lower)]
        if len(matches) == 1:
            return matches[0]
        # 也尝试 normalized 前缀
        if normalized != name_lower:
            matches_norm = [k for k in _tools if k.lower().startswith(normalized)]
            if len(matches_norm) == 1:
                return matches_norm[0]

    return None


def list_tools(agent_id: str | None = None) -> list[dict]:
    """列出可用工具，可按 agent_id 过滤。

    返回前端/Agent 可用的工具描述列表。
    Phase 48 T3-17: OC 工具改为注册到 _tools（统一来源），list_tools 主循环自动包含。
    Phase 51-B: 工具策略在调用方（Service/Adapter 层）应用，tools 层不依赖 services。
    2026-08-31 drift 修复: 原生 agent（DEFAULT_AGENT_ID="yuncat"）是受信宿主，
    可见全部工具——内置工具的 agents 列表成文于 hermes 为主 agent 的时代且无
    "yuncat"，按表过滤会让原生 agent 只剩 OC 工具、0 个内置工具（实测）。
    与 tool_service 执行侧的原生豁免保持同一语义。
    """
    # Phase 48 T3-17: 首次调用时将 OC yaml 工具注册到 _tools（闭合 get_tool() 找不到的断点）
    if agent_id == DEFAULT_AGENT_ID:
        _load_oc_tools_to_registry()

    result = []
    for name, meta in _tools.items():
        # 身份判断用 YUNCAT_AGENT_ID 而非 DEFAULT_AGENT_ID：后者语义是「未指定时取默认」，
        # 一旦漂移（已从 "hermes" 变过一次）就会静默改变过滤结果。
        if agent_id is None or agent_id == YUNCAT_AGENT_ID or agent_id in meta.agents:
            result.append(_meta_to_dict(name, meta))

    return result


def get_all_tools() -> dict[str, ToolMeta]:
    """获取完整工具注册表（仅 ToolService 内部使用）"""
    return dict(_tools)


def record_tool_usage(tool_name: str) -> None:
    """记录工具使用次数（供频率排序用）"""
    _tool_usage_count[tool_name] = _tool_usage_count.get(tool_name, 0) + 1


def get_tool_usage_stats() -> dict[str, int]:
    """获取工具使用频率统计"""
    return dict(_tool_usage_count)


def get_concurrency(tool_name: str) -> str:
    """Phase 45 D.7: 查询工具的并发安全级别。

    新字段 concurrency_safety 优先；None 时 fallback 到 is_concurrency_safe（向后兼容）。
    """
    meta = _tools.get(tool_name)
    if not meta:
        return "exclusive"  # 未知工具 → fail-safe
    if meta.concurrency_safety is not None:
        return meta.concurrency_safety
    if meta.is_concurrency_safe:
        return "safe"  # 旧版 boolean 字段兼容
    return "exclusive"


# ============================================================
# v40: 工具惰性加载（ToolSearch 模式）
# ============================================================
CORE_TOOLS = ["read_file", "write_file", "list_dir", "run_command"]


def get_active_tools(
    agent_id: str | None = None,
    context: dict | None = None,
) -> list[dict]:
    """每次 LLM 调用时动态生成工具列表——核心工具始终注入，扩展工具按需匹配。
    Phase 33: 按使用频率排序（频率高的优先，帮助 LLM 更快找到常用工具）。
    Phase 51-B: 工具策略在调用方（Service/Adapter 层）应用，tools 层不依赖 services。
    """
    query = (context or {}).get("query", "")
    active: list[dict] = []

    for name, meta in _tools.items():
        # 原生 agent 豁免——与 list_tools / tool_service 的执行门保持一致。
        # 内置工具的 agents 列表成文于 hermes 为主 agent 的时代，不含 "yuncat"；
        # 此处若无豁免，原生 agent 会连 read_file/list_dir 都拿不到
        # （即 2026-08-31 修过的「43 个内置工具全部 PermissionError」事故）。
        if (
            agent_id is not None
            and agent_id != YUNCAT_AGENT_ID
            and agent_id not in meta.agents
        ):
            continue
        if name in CORE_TOOLS:
            active.append(_meta_to_dict(name, meta))
            continue
        if _match_extended_tool(name, query):
            active.append(_meta_to_dict(name, meta))

    # Phase 33: 按使用频率降序（常用工具排在前面）
    active.sort(key=lambda t: _tool_usage_count.get(t["name"], 0), reverse=True)

    return active


def _meta_to_dict(name: str, meta: ToolMeta) -> dict:
    """将 ToolMeta 转为 API 友好的 dict"""
    return {
        "name": name,
        "description": meta.description,
        "parameters": meta.parameters,
        "confirmable": meta.confirmable,
        "is_read_only": meta.is_read_only,
        "groups": meta.groups,
        "max_result_size_chars": meta.max_result_size_chars,
    }


def _match_extended_tool(tool_name: str, query: str) -> bool:
    """判断扩展工具是否匹配当前查询。Phase 2 简化版（关键词匹配）。"""
    if not query:
        return False
    keyword_map = {
        "search_files": ["搜索", "查找", "grep", "搜索文件", "找", "搜代码", "在哪里", "search"],
        "docs_lookup": ["文档", "API", "doc", "docs", "怎么用", "参数"],
        "sequential_thinking": ["推理", "分析", "debug", "调试", "复杂", "逐步"],
        "delegate_to_agent": ["委托", "delegate", "另一个agent", "另一个agent"],
    }
    keywords = keyword_map.get(tool_name, [])
    return any(kw in query for kw in keywords)
