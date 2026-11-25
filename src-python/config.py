"""src-python/config.py —— Yuncat 全局配置常量

Phase 0：仅包含路径解析和基本常量。
后续 Phase 追加更多配置项时不改变此文件的结构约定。

Phase 44 工程化: env-var 驱动的配置迁移至 settings.py (pydantic-settings)，
本模块保持向后兼容——常量和辅助函数不变，TOKEN/DATA_DIR 从 settings 读取。
"""
import hashlib
import logging
import os

# ---- pydantic-settings (类型安全配置) ----
# 每次导入创建新实例——支持 importlib.reload(config) 读取最新环境变量
from settings import Settings
_settings = Settings()

# ---- 版本 ----
APP_VERSION = "0.1.3"

# ---- Token ----
# C10: 读取后立即从环境变量中删除——防止子进程/孙子进程继承
# Batch A 修复: settings.py 不 pop（注释已修正），在此处统一 pop
TOKEN = os.environ.pop("YUNCAT_TOKEN", "") or _settings.token

# ---- 路径 ----
DATA_DIR = _settings.get_data_dir()
DB_PATH = _settings.get_db_path()

# ---- 网络 ----
# TAURI_ORIGINS 已移除 (Phase 42.13: Tauri→Electron 迁移完成，无兼容需求)
OC_PORT_HINT = 18789  # Phase 48-F: OpenClaw 默认端口（openclaw.py / agents.py 共用）

# ---- 超时 ----
STARTUP_TIMEOUT_SEC = 30
HEALTH_CHECK_INTERVAL_SEC = 15
CONFIRM_TIMEOUT = 1800  # Phase 48-F: 用户确认工具调用超时 30min（tool_service / agent_tool_loop 共用）
# Batch 5a C6: resolved/rejected 审批凭证留存天数（对标审计留存惯例——审计日志默认保留期 30 天）
CONFIRM_AUDIT_RETENTION_DAYS = 30
# Task 3.5: pending confirmation LRU 上限——原硬编码 _PENDING_CONFIRM_MAX=1000
# 桌面应用单用户场景，1000 并发确认已远超实际需求（正常 < 10）。
# 设 200 既覆盖极端场景，又避免无界增长内存泄漏。
PENDING_CONFIRM_MAX = 200

# ---- Batch 8.4: LLM 响应缓存（精确哈希，默认关闭）----
LLM_CACHE_ENABLED = _settings.llm_cache_enabled
LLM_CACHE_TTL_SEC = _settings.llm_cache_ttl_sec
LLM_CACHE_MAX_ENTRIES = _settings.llm_cache_max_entries

# ---- Deliberation 超时 ----（Phase 48-F: 从 deliberation_*.py 抽取）
DELIBERATION_ROLE_TIMEOUT = 120    # 单角色 LLM 调用
DELIBERATION_CMD_TIMEOUT = 60      # spec 单命令执行
DELIBERATION_VISION_TIMEOUT = 30   # OC 视觉工具调用

# ---- Workspace 命令超时 ----（Phase 48-F: 从 routes/workspace.py 抽取）
WORKSPACE_GIT_TIMEOUT = 10   # git blame/status
WORKSPACE_LINT_TIMEOUT = 15  # ruff lint
WORKSPACE_TSC_TIMEOUT = 30   # tsc 类型检查

# ---- 后端调参 ----
AGENT_HEALTH_CHECK_INTERVAL = 30   # Agent 健康检查间隔（秒）
QUEUE_MAXSIZE = 1000  # Phase 48-F: asyncio.Queue 背压上限（base.py / event_bus.py 共用）
EVENT_BUS_MAX_RESTART_DELAY_SEC = 30.0  # Phase 5 P2: EventBus 消费者崩溃重启最大退避（event_bus.py）

# ---- Agent 默认值 ----（Phase 48-F: 兜底后端，对标 Claude Code general-purpose 兜底）
DEFAULT_AGENT_ID = "yuncat"  # Phase N: 默认改为 Yuncat 原生 agent（自身强 agent）
YUNCAT_AGENT_ID = "yuncat"  # Phase N: Yuncat 自身原生 agent（进程内直连 Provider，无子进程）
OPENCLAW_AGENT_ID = "openclaw"  # Phase 56: 避免代码中散落 openclaw 字符串
HERMES_AGENT_ID = "hermes"      # Phase 56: 避免代码中散落 hermes 字符串
SUPERVISOR_AGENT_ID = "supervisor"  # Harness: Supervisor 角色（4 工具白名单 agents=[...] 的锚点）
WORKER_AGENT_ID = "worker"  # 长任务窗口翻滚：Worker 执行窗口（被 Supervisor 监督）

# 长任务窗口翻滚涉及的"内部 Yuncat 角色"集合（区别于 EXTERNAL_AGENT_IDS 的兄弟后端）。
# 这些角色由主 Yuncat Agent 派生，共享同一套原生工具与平台基础设施。
INTERNAL_ROLE_AGENT_IDS = {SUPERVISOR_AGENT_ID, WORKER_AGENT_ID}

# 长任务窗口翻滚总开关 (默认开启; 关闭则沿用旧的执行/压缩路径, 不影响既有流程)
LONG_TASK_WINDOW_ROLLOVER_ENABLED = True

# ---- 外部 Agent 自动路由（Phase 9-X: 默认不调用，需用户显式选择）----
# 外部 Agent 由用户自行启动，Yuncat 不托管其生命周期。
# 自动路由（用户未指定 agent 时）一律落回原生 Yuncat，绝不静默把整任务交给外部后端；
# 仅在用户于下拉框显式选择 / @mention 时才调用外部 Agent。
#
# 2026-09-10: 移除 "hermes"——Hermes 已下线，全部能力回归 Yuncat 原生。
# 保留 openclaw：视觉/消息/实时信息等能力确实依赖外部网关。
# 2026-09-10: hermes 曾因提示词未同步被误下线，现已恢复——外部网关（hermes gateway run，
# API server 默认 127.0.0.1:8642，需 API_SERVER_KEY），与 openclaw/codex/claude 同等地位：默认不自动路由、
# 默认关闭、且仅在 MAX（全功能）模式可被显式调用（见 ws.py 提交路径的 max-mode 门控）。
EXTERNAL_AGENT_IDS = {"openclaw", "codex", "claude", "hermes"}
EXTERNAL_AGENT_AUTO_ROUTE = False

# ---- 能力档位（Phase 108+ / 2026-09-17 收敛为两档）----
# 只有两套能力集：**默认（非 MAX）与 MAX**。
#   默认：基础能力全开——联网、视觉、浏览器、记忆写入、单个子 Agent、长任务
#   MAX：在上面加“增强集”——更大并发与轮次、对抗验证、无人值守长任务、多角色讨论、
#          外部 Agent 点名、自检复盘、先出计划、记忆自动整理、自动安装入库
# 归类原则：这里只管“能力开不开”，不管“安全”——桌面操控 / 消息外发 / 后台任务 / MCP
# 创建这些高风险动作属于权限档与审批（security/profiles.py、approval.py），MAX 不等于免审批。
# 数值上限（并发/轮次/翻滚代数等）在 services/capability_limits.py，不在这里写死。
DEFAULT_CAPABILITY_LEVEL = 1
MAX_CAPABILITY_LEVEL = 2

BASE_CAPABILITY_FLAGS: dict[str, bool] = {
    "web_search": True,
    "subagent_spawn": True,
    "vision": True,
    "long_task": True,
    "browser": True,
    "memory_write": True,
}

MAX_ONLY_CAPABILITY_FLAGS: dict[str, bool] = {
    # 每个 flag 都有真实消费点（注释里标出）——不录“声明了但没人读”的字段    "parallel_team": True,          # spawn_subagent_team（CAPABILITY_TOOL_MAP）
    "adversarial_verify": True,     # review_hooks._adversarial_challenge + capability_limits.adversarial_replans
    "multi_role_discussion": True,  # routes/agents.py deliberation_start 门槛
    "external_agent": True,         # routes/ws.py _gate_external_agent
    "self_review_loop": True,       # prompts/commander.py 按档注入“执行后自检”
    "plan_first": True,             # prompts/commander.py 按档注入“先出计划”
}

def is_max_level(level) -> bool:
    """能力档是否为 MAX（只有两档：>=2 即 MAX，其余一律默认）"""
    try:
        return int(level) >= MAX_CAPABILITY_LEVEL
    except (TypeError, ValueError):
        return False


def capability_flags_for_level(level) -> dict[str, bool]:
    """能力档位 → 功能开关集合（唯一真源：默认档 = 基础集；MAX = 基础 + 增强）"""
    flags = dict(BASE_CAPABILITY_FLAGS)
    if is_max_level(level):
        flags.update(MAX_ONLY_CAPABILITY_FLAGS)
    return flags


# ---- 工具 → 所需能力开关（Phase 108+: 硬门控于 ToolService.execute）----
# 能力策略的核心是「该 Agent 被允许调用哪些功能」。以下映射把具体工具名归并到
# 对应的能力开关，使 ToolService 在分发前可 fail-closed 拦截被禁用的功能。
# 精确匹配优先，子串匹配兜底（覆盖 OpenClaw 网关动态注入的工具，如
# openclaw_vision / hermes_web_search / *_browser 等命名不固定的工具）。
CAPABILITY_TOOL_MAP: dict[str, str] = {
    "docs_lookup": "web_search",          # 本地文档查询，降级等价于 web_search
    "vision_analyze": "vision",
    "spawn_subagent": "subagent_spawn",
    "spawn_subagent_team": "parallel_team",
    "delegate_to_agent": "subagent_spawn",
    "store_memory": "memory_write",
    "remember_preference": "memory_write",
}
# (capability, substring) —— 小写子串匹配，覆盖网关/扩展工具
_CAPABILITY_TOOL_SUBSTR: list[tuple[str, str]] = [
    ("web_search", "web_search"),
    ("web_search", "web_browse"),
    ("browser", "browser"),
    ("browser", "browse"),
    ("vision", "vision"),
    ("subagent_spawn", "subagent"),
    ("memory_write", "memory_write"),
]


def capability_required_by_tool(tool_name: str) -> str | None:
    """返回工具所需的能力开关名；无映射（核心工具）返回 None 表示不受策略约束。

    精确匹配优先，子串匹配兜底。None 结果意味着该工具任何能力等级都可用
    （如 read_file / write_file / run_command 等基础文件与命令工具）。
    """
    if not tool_name:
        return None
    if tool_name in CAPABILITY_TOOL_MAP:
        return CAPABILITY_TOOL_MAP[tool_name]
    low = tool_name.lower()
    for cap, sub in _CAPABILITY_TOOL_SUBSTR:
        if sub in low:
            return cap
    return None


# ---- Agent 启用开关（Phase X: 默认仅原生 Yuncat 启用；外部 Agent 需用户显式启用）----
# 写入 settings 的 key 形如 `agent_enabled:{id}`，值为 "true"/"false"。
AGENT_ENABLED_DEFAULTS = {"yuncat": True}
DESIGN_STYLE_DEFAULT = "modern-minimal"
COLOR_SCHEME_DEFAULT = "neutral"

# ---- 资源保护类上限（Phase 54: 统一 LIMIT / max_tokens / 截断常量） ----
# 路由列表默认值
SESSION_LIST_DEFAULT_LIMIT = 20
SESSION_MESSAGES_DEFAULT_LIMIT = 50
SESSION_CONVERSATION_DEFAULT_LIMIT = 50
SESSION_EXPORT_MESSAGE_LIMIT = 500

TASK_LIST_DEFAULT_LIMIT = 50
TASK_SEARCH_DEFAULT_LIMIT = 20

NOTIFICATION_LIST_DEFAULT_LIMIT = 50
SNAPSHOT_RECORDINGS_LIMIT = 20
WORKFLOW_RUNS_LIST_LIMIT = 20

# ---- 备份（Batch 5.6: 自动调度 + 增量）----
BACKUP_DEFAULT_RETENTION_COUNT = 7           # 默认保留备份数
BACKUP_DEFAULT_CRON_EXPR = "0 3 * * *"       # 默认每天 3:00 触发
BACKUP_SHA256_CHUNK_SIZE = 65536             # sha256 分块读取大小（字节）
BACKUP_LIST_DEFAULT_LIMIT = 50               # 历史列表分页

# 知识 / 记忆
MEMORY_SEARCH_DEFAULT_LIMIT = 20
MEMORY_SEARCH_CONV_PREVIEW_CHARS = 200    # 每条会话消息预览字符数（memory_search.py）
MEMORY_SEARCH_FALLBACK_PREVIEW_CHARS = 2000  # summarizer 失败时 fallback 截断长度（memory_search.py）
HYBRID_QUERY_EXPANSION_DEFAULT = True
HYBRID_DIVERSIFY_DEFAULT = False
HYBRID_LATENCY_BUDGET_MS_DEFAULT = 500.0
MEMORY_CONFLICTS_LIMIT = 10
KNOWLEDGE_GRAPH_DEFAULT_LIMIT = 100
KNOWLEDGE_GRAPH_MAX_LIMIT = 200

AGENT_CONTEXT_FTS_MEMORIES_LIMIT = 5
AGENT_CONTEXT_MEMORY_TREE_LIMIT = 5
AGENT_CONTEXT_VECTOR_SEARCH_LIMIT = 3
AGENT_CONTEXT_PERSONALIZATION_LIMIT = 3
AGENT_CONTEXT_VECTOR_CONTENT_PREVIEW = 200  # 向量记忆内容预览字符数
AGENT_CONTEXT_TABS_LIMIT = 5
AGENT_CONTEXT_DIAGNOSTICS_LIMIT = 5
AGENT_CONTEXT_REATTACH_FILES_LIMIT = 3
AGENT_CONTEXT_REATTACH_FILE_MAX_BYTES = 50_000
AGENT_CONTEXT_SKILL_PREVIEW_CHARS = 500
AGENT_CONTEXT_ACTIVE_SKILLS_LIMIT = 3
AGENT_CONTEXT_SKILL_TOTAL_BUDGET_CHARS = 5000
AGENT_CONTEXT_RULES_BUDGET_CHARS = 2000
AGENT_CONTEXT_RECENT_FILES_LIST_LIMIT = 10

# Batch 2.7: agent_context 三条检索路径（memories vector/FTS + knowledge tree）
# 统一上下文预算——按模型窗口动态计算，避免各自为政导致 token 浪费。
AGENT_CONTEXT_MEMORY_TOTAL_BUDGET_FRACTION = 0.02
AGENT_CONTEXT_MEMORY_TOTAL_BUDGET_MIN_CHARS = 1500
AGENT_CONTEXT_MEMORY_TOTAL_BUDGET_MAX_CHARS = 6000
MEMORY_CONTEXT_SESSION_BUDGET_FRACTION = 0.25
MEMORY_CONTEXT_ITEM_MIN_CHARS = 80

COMMANDER_LANGUAGE_DETECT_MAX_FILES = 200
COMMANDER_IMPORT_FILE_BUDGET_CHARS = 2000

WORKFLOW_FROM_CHAT_MAX_STEPS = 10
WORKFLOW_FROM_CHAT_STEP_USER_INPUT_BUDGET = 300
WORKFLOW_FROM_CHAT_ANALYZE_INPUT_BUDGET = 500
WORKFLOW_FROM_CHAT_IMPLEMENT_OUTPUT_BUDGET = 800
WORKFLOW_FROM_CHAT_NAME_PREVIEW_CHARS = 50
WORKFLOW_FROM_CHAT_DESCRIPTION_PREVIEW_CHARS = 100

FEEDBACK_LEARNER_KNOWLEDGE_LIMIT = 3
FEEDBACK_LEARNER_KEYWORD_KNOWLEDGE_LIMIT = 3
FEEDBACK_LEARNER_FTS_LIMIT = 5
FEEDBACK_LEARNER_PERSONALIZATION_LIMIT = 5
# 进入「## 你的偏好（已学习）」的最低评分。节点从 0.3 起，接受 +0.10，
# 故需 5 次接受才够格——刻意偏高，避免一次性反馈就被当成稳定偏好。
PERSONALIZATION_SCORE_THRESHOLD = 0.8

# 优先来源：用户**明确表达或反复确认**过的知识。注入 system prompt 时排在
# 会话沉淀事实之前——否则会话摘要（memory_tree 索引时直接给 score=1.0）
# 会把用户反复确认才爬到 0.9 的偏好挤出 limit（实测 6 次确认仍进不了前 5）。
PERSONALIZATION_PRIORITY_SOURCES = frozenset({
    "agent_delegation",      # 用户对工具/委托的接受与拒绝
    "memory_md_import",      # 用户自己维护的 MEMORY.md
    "openclaw_memory",       # 外部 agent 同步过来的用户记忆
    "hermes_agent_memory",
    "imported",              # 用户显式导入
})

# 结构化会话摘要（session_summaries）的「空节」哨兵值。
# 各节必填，缺失时填此值而非空串，故**所有读取端都必须把它当空处理**。
# 现状：_format_compact_boundary / _summary_size 已判 "无"，
# 但 agent_context._format_inherited_summary 漏判——结果每个新会话都注入
# 7 行「- 目标: 无」，既浪费上下文，又让模型把「无」当成既成事实。
# 用共享常量而非散落的字面量，避免再有新的读取端漏判。
SUMMARY_EMPTY_SENTINEL = "无"

# knowledge_nodes.score 取值域。评分被多处当作「置信度 0~1」使用：
#   - capability_router._query_learned_from_db 用 AVG(score) 当 success_rate，
#     与 _get_learned_preference 的 0.7 阈值比较——不设上限 AVG 会无界增长，
#     阈值将永久成立（"用过就永远偏好"），退化成假学习。
#   - get_personalization_context 用 score >= 0.8 筛选注入 system prompt 的偏好。
# 因此上界必须是 1.0，下界保持历史值 0.01（不为 0，避免节点被永久判死）。
KNOWLEDGE_SCORE_FLOOR = 0.01
KNOWLEDGE_SCORE_CEILING = 1.0

# get_personalization_context 的 source 排除表——这些是 Yuncat 的内部记账节点，
# 不是用户偏好。不加过滤时它们会以「## 你的偏好（已学习）」的标题被拼进
# system prompt（实测 workflow_summary / spec_loop_escalation / agent_perf 均泄漏）。
# 用排除表而非白名单：新增的用户向知识来源默认可见，只有内部记账类才需登记。
# ⚠️ 新增内部记账类 source 时，记得登记到这里。
PERSONALIZATION_EXCLUDED_SOURCES = frozenset({
    "workflow_summary",       # 工作流执行摘要
    "workflow_run",           # 工作流运行结果
    "bug_fix_lesson",         # 工具失败教训（调试日志，非偏好）
    "spec_loop_escalation",   # Spec 升级告警
    "escalation_loop",        # Loop 失败告警
    "gc_agent",               # 技术债扫描结果
    "self_evolution",         # 自进化模式统计
    "mixed_deliberation",     # agent_perf 路由统计
    "deliberation_tmp_role",  # 临时讨论角色产物
    "raptor_cluster",         # RAPTOR 聚类摘要
    "auto_dream",             # upsert_knowledge_node 的默认 source——来源不明，
                              # 且默认 score=1.0 会立刻越过 0.8 门槛，风险最高
})

MEMORY_TREE_DEDUP_LIMIT = 3
MEMORY_DREAM_FACTS_LIMIT = 100
MEMORY_DREAM_OC_FACTS_LIMIT = 20

# Phase 56-P9: Memory Dream 内联截断数字常量化
MEMORY_DREAM_TITLE_PREVIEW_CHARS = 80
MEMORY_DREAM_CONTENT_MAX_CHARS = 2000
MEMORY_DREAM_FACT_CONTENT_PREVIEW_CHARS = 500
MEMORY_DREAM_OC_FACT_CONTENT_PREVIEW_CHARS = 200
MEMORY_DREAM_OC_FACT_MIN_CHARS = 50

# 工作区
WORKSPACE_FILE_SEARCH_DEFAULT_LIMIT = 100
WORKSPACE_FILE_SEARCH_MAX_LIMIT = 200
WORKSPACE_FILENAME_SEARCH_LIMIT = 5
WORKSPACE_MAX_READ_SIZE = 50 * 1024 * 1024  # 50MB
WORKSPACE_BINARY_SNIFF_BYTES = 8 * 1024  # 二进制嗅探字节数：命中 NUL 即按只读打开（2026-09-15）

# LLM 输出 token 预算
MAX_TOKENS_MODEL_VALIDATION = 1
MAX_TOKENS_SKILL_TEST = 800
MAX_TOKENS_WORKFLOW_NODE = 150
MAX_TOKENS_AGENT_TOOL_RESULT = 120
MAX_TOKENS_INTENT_CLASSIFY = 200
INTENT_CLASSIFY_INPUT_MAX_CHARS = 1000
# Batch 5a C7: LLM 意图分类结果缓存 TTL——重复/相似 prompt 不再二次调 LLM
INTENT_LLM_CACHE_TTL_SEC = 600.0
MAX_TOKENS_HERMES_MEMORY = 200
MAX_TOKENS_SELF_EVOLUTION = 600
MAX_TOKENS_SKILL_REGISTRY = 300
SKILL_REGISTRY_REVIEW_INPUT_MAX = 3000
MAX_TOKENS_LIFESPAN_HEALTH = 300
MAX_TOKENS_MEMORY_COMPACT = 30
LIFESPAN_HEALTH_SUMMARY_INPUT_MAX = 4000

# 微型压缩字符预算（Phase 56: 替代 agent_compact.py 硬编码 200/1800/2000）
MICRO_COMPACT_TOOL_HEAD_BUDGET = 200
MICRO_COMPACT_TOOL_TAIL_BUDGET = 200
MICRO_COMPACT_THINKING_BUDGET = 1800
MICRO_COMPACT_REASONING_CONTENT_BUDGET = 2000

# 审议 / 对抗验证字符预算（Phase 56: 替代 deliberation/dynamic_orchestrator 硬编码）
ADVERSARIAL_REVIEW_BUDGET_CHARS = 4000
GHOST_REVIEW_BUDGET_CHARS = 6000
SPEC_ROLE_REVIEW_BUDGET_CHARS = 4000
DELIBERATION_DISCUSSION_BUDGET_CHARS = 2000
SUB_AGENT_OUTPUT_PREVIEW_CHARS = 2000
SUB_AGENT_PROMPT_PREVIEW_CHARS = 200
SUB_AGENT_PROMPT_LOG_PREVIEW_CHARS = 60
SUB_AGENT_GENERATED_RESPONSE_MAX_CHARS = 2000
SUB_AGENT_ERROR_PREVIEW_CHARS = 500
SUB_AGENT_FULL_RESPONSE_LOG_PREVIEW_CHARS = 5000
SUB_AGENT_PLAN_STEP_OUTPUT_PREVIEW_CHARS = 500
# Task 7.8: spawn_subagent 工具递归深度上限——对标 Claude Code Task tool（max_depth=3-4）。
# depth 0=主 Agent, 1=子 Agent, 2=孙 Agent, 3=曾孙 Agent（不可再 spawn）
MAX_SUBAGENT_DEPTH = 3

# 全局资源硬上限
MAX_CACHE_TURNS_GLOBAL = 10_000
MAX_EVENT_LOG_ENTRIES = 1_000
EVENT_LOG_CONTEXT_SLICE_SIZE = 100  # Phase 5 P2: 上下文恢复时切片条数（event_log.py slice_for_context 默认值）
MAX_CONTEXT_TOKENS_SAFETY_CAP = 2_097_152  # 2M tokens 绝对安全上限
MAX_CONCURRENT_TASKS = 20
MAX_OPEN_SHELLS = 20
MAX_SEARCH_FILES = 1_000
# Task 3.8: delegate_to_agent 全局并发上限——防主 Agent 一轮发 50 个委托耗尽 OC 后端连接。
# 桌面单用户场景 10 并发委托已足够（OC/HA 后端通常单进程，过多并发会排队超时）。
# 对标主流: 连接池上限（如 DB pool max_connections）。
DELEGATE_CONCURRENCY_MAX = 10

# Task 6.7 (F-053): delegate_to_agent 重试 + fallback 配置——对标 Temporal 自动重试。
# 重试次数（1 = 首次失败后重试 1 次，共 2 次尝试）；退避间隔避免瞬时故障下连续冲击。
DELEGATE_RETRY_MAX = 1
DELEGATE_RETRY_BACKOFF_SEC = 0.5
# 备选 Agent 映射——委托失败后尝试的备选 Agent（仅当备选 Agent 存在且支持 execute_tool 时才尝试）。
# 2026-09-10: 移除 "hermes"——Hermes 已下线。OpenClaw 委托失败时不再回退到 Hermes，
# 留空字典表示无备选（fail-closed，避免静默委托到不存在的后端）。
DELEGATE_FALLBACK_MAP: dict[str, str] = {}

# Phase 108: 委派递归深度上限——对标 dsh delegationDepth (maxDepth=3, 0=禁止委派)。
# 防 delegate_to_agent 链失控（A→B→C→…）。ContextVar 跨 create_task 自动传播。
MAX_DELEGATION_DEPTH = 3

# Phase 108: 工具结果"展示用"预览上限——仅前端折叠长结果时使用。
# 重要: 绝不用于截断喂给 AI 模型的内容（模型必须看到完整工具结果）。
TOOL_RESULT_PREVIEW_CHARS = 2000

# Phase 108: 并行工具调用上限——替换 agent_tool_loop 中硬编码的 8。
MAX_PARALLEL_TOOL_CALLS = 8

# SQL / Repository 内联 LIMIT（Phase 56: 从硬编码提取）
SEARCH_GLOBAL_LIMIT = 50
TASK_RUNNING_CLEANUP_LIMIT = 5
LOOP_CANDIDATE_ROLES_LIMIT = 20
MEMORY_TREE_RECENT_DREAMS_LIMIT = 30
SYSTEM_NOTIFICATIONS_LIMIT = 20
REVIEW_PIPE_RECENT_LIMIT = 10

# 代码索引上限（Phase 56: 原 500/200/50 等魔法数字）
# Task 4.7: MAX_FILES 500→5000（Yuncat 自身 ~700 文件，原值静默丢弃 200+）；
#           移除 MAX_SYMBOLS 持久化截断（对标 Aider --map 全量索引，注入时由 load_summary 动态预算）；
#           SUMMARY_LIMIT 保留作为 model_cfg 不可用时的 fallback。
CODE_INDEX_MAX_FILES = 5000
CODE_INDEX_SUMMARY_LIMIT = 50
CODE_INDEX_CLASS_METHODS_PREVIEW = 3
CODE_INDEX_FUNCTION_ARGS_PREVIEW = 5

# Phase 56-P5: 各模块仍在使用的硬编码预览/截断/长度上限集中配置
KNOWLEDGE_NODE_TITLE_PREVIEW_CHARS = 80
KNOWLEDGE_NODE_CONTENT_PREVIEW_CHARS = 300
KNOWLEDGE_NODE_SEARCH_CONTENT_PREVIEW_CHARS = 500
KNOWLEDGE_LIKE_QUERY_MAX_CHARS = 50
# Batch 2.4: LIKE fallback 候选池放大倍数——先取 limit*N 个再按相关性重排
KNOWLEDGE_LIKE_FALLBACK_OVERFETCH = 3

SESSION_TITLE_MAX_CHARS = 200
SESSION_TITLE_PREVIEW_CHARS = 40
CONFIG_EXPORT_CONVERSATION_LIMIT = 1000
CONFIG_IMPORT_CONVERSATION_LIMIT = 500
CONFIG_IMPORT_MAX_KNOWLEDGE_NODES = 500

SEARCH_CONTEXT_PREVIEW_CHARS = 500
SEARCH_EXCERPT_CHARS = 200

MEMORY_TREE_TOKEN_JUICE_MAX_LINE_LEN = 500
MEMORY_SYNC_FACTS_LIMIT = 50
MEMORY_REPO_UNCONSOLIDATED_FACTS_LIMIT = 100

GIT_COMMIT_MESSAGE_MAX_CHARS = 200
GIT_STDOUT_PREVIEW_MAX_CHARS = 2000
GIT_STDERR_PREVIEW_MAX_CHARS = 500
GIT_OBSERVATION_PREVIEW_MAX_CHARS = 300

INSTALL_README_PREVIEW_MAX_CHARS = 5000

FILE_OPS_READ_MAX_LINE_LEN = 500
FILE_OPS_LIST_MAX_ENTRIES_NOTE = 200

WORKSPACE_DIFF_SUFFIX_PREVIEW_CHARS = 500
WORKSPACE_DIFF_LINE_PREVIEW_CHARS = 200

CI_ERROR_LOG_PREVIEW_MAX_CHARS = 8000

FILE_WATCHER_MAX_PATHS_LOG = 50

PROJECT_SCAN_FILES_PER_DIR_LIMIT = 10
PROJECT_SCAN_TOTAL_FILES_LIMIT = 50
PROJECT_SCAN_FILES_LIST_LIMIT = 5
PROJECT_SCAN_OUTPUT_MAX_CHARS = 3000
PROJECT_SCAN_TRUNCATE_THRESHOLD_CHARS = 2500  # 项目结构提前中断阈值（预留余量给后续 import 摘要）

WORKFLOW_NODE_PROMPT_PREVIEW_CHARS = 500
WORKFLOW_NODE_ERROR_PREVIEW_CHARS = 300
WORKFLOW_NODE_SUMMARY_MAX_CHARS = 300
WORKFLOW_EVENT_OUTPUT_MAX_CHARS = 200
# Phase D W.2: 工作流快照预压缩阈值（字节）
WORKFLOW_SNAPSHOT_PRECOMPACT_THRESHOLD_BYTES = 200 * 1024

CAPABILITY_ROUTER_ARGUMENTS_PREVIEW_CHARS = 100

FEEDBACK_LEARNER_TITLE_PREVIEW_CHARS = 50
FEEDBACK_LEARNER_CONTENT_PREVIEW_CHARS = 300
FEEDBACK_LEARNER_TAGS_PREVIEW_LIMIT = 3
FEEDBACK_LEARNER_LOG_KEYWORD_LIMIT = 3
FEEDBACK_LEARNER_FTS_KEYWORD_LIMIT = 5
FEEDBACK_LEARNER_EXTRACT_KEYWORD_LIMIT = 5

INTENT_REASONING_PREVIEW_CHARS = 200

# Shell / Adapter / Hook 可配置阈值
SHELL_STREAM_BUFFER_MAX_CHARS = 2048
ADAPTER_READY_MAX_WAIT_SECONDS = 30
ADAPTER_READY_POLL_INTERVAL_SECONDS = 1.0
ADAPTER_RESTART_MAX_PER_HOUR = 5
ADAPTER_RESTART_DELAYS = [5, 10, 20, 40, 60]
HA_STARTUP_MAX_WAIT_SECONDS = 15
HA_STARTUP_POLL_INTERVAL_SECONDS = 0.5
# Phase 60: HA 子进程 HTTP 健康探测
HA_HEALTH_CHECK_TIMEOUT = 2          # 单次 HTTP 探测超时
GATEWAY_TOOLS_TIMEOUT_SEC = 5        # 网关附属端点（/tools/schema、/skills）探测超时
VERSION_PROBE_TIMEOUT_SEC = 10       # CLI 版本探测（agent --version）超时
HA_HTTP_READY_TIMEOUT = 10           # start() 等 HTTP 就绪总超时
HA_HTTP_READY_POLL_INTERVAL = 0.5    # start() HTTP 就绪轮询间隔
HA_WATCHDOG_POLL_INTERVAL = 5        # watchdog 检查进程存活间隔
SUB_AGENT_POLL_MAX_SECONDS = 120
SUB_AGENT_POLL_INTERVAL_SECONDS = 1.0

# Phase 61: Task 心跳持久化——长耗时工具执行期间周期性更新 heartbeat_at
# 不覆盖完整 task_state（与 _write_task_snapshot 互不干扰）
TASK_HEARTBEAT_INTERVAL_SECONDS = 10  # 心跳更新间隔（对标主流 10s 粒度）
# P0-4.2: 心跳超时阈值——超过此值未更新 heartbeat_at 视为 stall
# 对标 LangGraph 5min heartbeat timeout / Temporal activity timeout
# heartbeat_at 仅在 run_command 等长耗时工具内更新，普通 LLM 调用期间不更新
# 5 分钟足够覆盖单次 LLM 调用（通常 10-60s）+ 工具执行（最长 600s 由 tool_timeout 约束）
TASK_HEARTBEAT_TIMEOUT_SECONDS = 300

# Phase 62: Task 崩溃恢复——重启后自动恢复 running 任务
MAX_TASK_RESUME_COUNT = 3              # 最大恢复次数（超过则标记 failed）
TASK_CRASH_GRACE_SECONDS = 60          # 崩溃判定宽限期（updated_at < now - 此值 才算崩溃）
TASK_RESUME_IDEMPOTENCY_SECONDS = 300  # 幂等性窗口（5 分钟内不重复恢复同一任务）

HOOKS_MAX_PENDING_DIAG = 50
HOOKS_MAX_REACTIVATIONS = 3

# CodeWhale 借鉴: 诊断严重度过滤 + 按文件截断
LINT_INCLUDE_WARNINGS = False          # 默认只注入 error，不注入 warning（减少上下文噪音）
LINT_MAX_DIAGS_PER_FILE = 20           # 每文件最多保留 20 条诊断（按 severity 排序后截断）
LINT_TS_ENABLED = True                 # TypeScript lint 开关（tsc --noEmit --incremental）

# 工具结果缓存——截断的工具输出 side-store（request_more_context 检索）
TOOL_RESULT_CACHE_MAX = 100            # LRU 上限——超过时淘汰最久未访问的

# 工具执行全局默认超时——per-tool tool_timeout 为 None 时使用此兜底值
# 对标 Claude Code Bash 120s + 文件操作余量；300s 覆盖大文件读写 + 网络驱动器场景
TOOL_DEFAULT_TIMEOUT_SEC = 300

# extract_recent_files 从对话历史中提取的最近文件路径上限
EXTRACT_RECENT_FILES_LIMIT = 10

FILE_CHECKPOINT_MAX_PER_FILE = 50
FILE_CHECKPOINT_MAX_AGE_SECONDS = 604800

GC_LONG_FUNC_LINES = 50
COMPLETION_SUGGESTION_MAX_LEN = 200

# 固定重试 / 循环次数（Phase 56: 从业务代码提取）
MAX_SPEC_FIX_ATTEMPTS = 3
COMPACT_QUALITY_MAX_ATTEMPTS = 3

# Supervisor / 路由 / AgentService 阈值（Phase 56: 从业务代码提取）
LOOP_SHUTDOWN_TIMEOUT_SECONDS = 10.0
# Batch 5a C2: loop_trigger_queue claimed 回收——处理失败重投上限（对标 event_outbox attempts）
LOOP_TRIGGER_MAX_ATTEMPTS = 3
# Batch 5a C2: 重启恢复时 claimed 超过该秒数视为孤儿（进程已死），重置回 pending
LOOP_TRIGGER_CLAIM_STALE_SEC = 600
MODEL_REFRESH_TIMEOUT_SECONDS = 15.0
MODEL_REFRESH_MAX_MODELS = 50
MODEL_REFRESH_LARGE_CONTEXT_FALLBACK = 262_144
AGENT_SERVICE_MSG_INDEX_MAX = 5000
AGENT_SERVICE_MSG_INDEX_PER_SESSION = 200

# 上下文窗口 fallback（Phase 56: 替代 adaptive_truncate 中到处写的 131072）
MAX_CONTEXT_FALLBACK_CHARS = 131_072

# Agent 核心循环截断（Phase 56: 从 agent_tool_loop / agent_roles / agent_compact 等提取）
TOOL_LOOP_TOOL_RESULT_PREVIEW_CHARS = 500  # 建议改用 calc_tool_result_limit(model_cfg)
TOOL_LOOP_ROUND_TEXT_LOG_PREVIEW_CHARS = 5000
TOOL_LOOP_ASSISTANT_MSG_MAX_CHARS = 20000
TOOL_LOOP_REASONING_MSG_MAX_CHARS = 32000
TOOL_LOOP_SNAPSHOT_GENERATED_PREVIEW_CHARS = 2000
TOOL_LOOP_SNAPSHOT_THINKING_PREVIEW_CHARS = 2000
AGENT_ROLES_TOOL_RESULT_PREVIEW_CHARS = 500
AGENT_ROLES_DELEGATION_RESULT_PREVIEW_CHARS = 500
AGENT_ROLES_TOOL_CALLS_SEEN_LIMIT = 8
AGENT_COMPACT_DEDUP_CONTENT_PREVIEW_CHARS = 50
AGENT_COMPACT_RECOVERABLE_FILES_LIST_LIMIT = 10
AGENT_COMPACT_RECOVERABLE_FILES_REFS_LIMIT = 20
AGENT_COMPACT_TOOL_TRUNCATE_THRESHOLD_CHARS = 800
AGENT_COMPACT_ASSISTANT_STRIP_THRESHOLD_CHARS = 2000
AGENT_TOOL_HELPERS_FALLBACK_CHAIN_LIMIT = 3
AGENT_TOOL_HELPERS_ERROR_PREVIEW_CHARS = 200
AGENT_TOOL_HELPERS_STDOUT_PREVIEW_CHARS = 500
AGENT_TOOL_HELPERS_STDOUT_TYPECHECK_PREVIEW_CHARS = 300
AGENT_TOOL_HELPERS_PROTECTED_RESULT_PREVIEW_CHARS = 300
AGENT_TOOL_HELPERS_BUG_ID_MAX_LEN = 100

# 日志 / 调试 preview（Phase 56: 统一避免各文件各自为政）
LOG_PREVIEW_TASK_ID_LEN = 8
LOG_PREVIEW_SESSION_ID_LEN = 8
LOG_PREVIEW_CMD_LEN = 80
LOG_PREVIEW_ERROR_LEN = 200

# 后台 Agent 循环：每次恢复 pending 任务数（原硬编码 3，Codex 等主流工具无此限制）
BACKGROUND_AGENT_MAX_PENDING = 20
BACKGROUND_AGENT_INTERVAL_SECONDS = 60

# ═══════════════════════════════════════════════════════════
# Phase 5 Y: Magic Numbers 集中提取——loop intervals / confidence thresholds / LLM temperature
# ═══════════════════════════════════════════════════════════

# ---- Loop intervals（lifespan.py 后台定时任务间隔，秒）----
LOOP_INTERVAL_WORKFLOW_CRON_SEC = 60        # 工作流 Cron 调度器
LOOP_INTERVAL_HARNESS_RECOVERY_SEC = 60     # Phase C: harness 崩溃恢复扫描（重置残留 RUNNING）
LOOP_INTERVAL_HEALER_SEC = 30               # 健康检查/自愈
LOOP_INTERVAL_MEMORY_TREE_SEC = 900         # MemoryTree 索引（15min，兜底批量）
MEMORY_INDEX_DEBOUNCE_SEC = 30.0            # Task 6.6: 事件驱动即时提取去抖窗口（秒）—
                                            # 每轮 assistant 完成后 debounce 30s 再提取，
                                            # 避免连续对话 LLM 调用风暴；15min 兜底仍保留
LOOP_INTERVAL_AUTO_DREAM_SEC = 600           # 自动 Dream 固化（10min）
LOOP_INTERVAL_EVOLUTION_SEC = 600            # SelfEvolution 扫描（10min）
LOOP_INTERVAL_MEMORY_SYNC_SEC = 900          # MemoryTree ↔ MEMORY.md 同步（15min）
LOOP_INTERVAL_GC_AGENT_SEC = 604800          # GC Agent 周期清理（7 天）
LOOP_INTERVAL_CLEANUP_SEC = 600              # 过期 session/任务清理（10min）
LOOP_INTERVAL_APPROVAL_EXPIRE_SEC = 60       # 审批超时检查
LOOP_INTERVAL_FILE_CHECKPOINT_SEC = 60       # 文件检查点清理
LOOP_INTERVAL_CAPABILITY_ROUTER_SEC = 60     # Capability 路由反馈学习
LOOP_INTERVAL_FEEDBACK_LEARNING_SEC = 60     # 反馈学习闭环
LOOP_INTERVAL_SELF_EVOLUTION_SEC = 30        # SelfEvolution 短周期
LOOP_INTERVAL_FILE_WATCHER_SEC = 30          # 文件监控
LOOP_INTERVAL_KNOWLEDGE_DECAY_SEC = 60       # 知识衰减
LOOP_INTERVAL_GC_TEMP_FILE_SEC = 30          # 临时文件 GC
LOOP_INTERVAL_BACKUP_SCHEDULER_SEC = 60      # 备份调度器检查间隔（cron 评估）

# ---- Adapter 自动启动延迟与超时（秒）----
OC_AUTOSTART_DELAY_SEC = 2
# Batch 3a A1: 外层 wait_for 超时必须 ≥ 内部就绪窗口（ADAPTER_READY_MAX_WAIT_SECONDS），
# 否则外层先超时取消 start()——子进程已 spawn 但 watchdog 未创建，成孤儿占端口。
# +15s 余量覆盖进程 spawn 本身；超时后由调用方按 TimeoutError 降级。
OC_AUTOSTART_TIMEOUT_SEC = ADAPTER_READY_MAX_WAIT_SECONDS + 15
HA_AUTOSTART_DELAY_SEC = 3
# Batch 3a A1: 同 OC——HA 子进程就绪窗口同为 ADAPTER_READY_MAX_WAIT_SECONDS（基类
# _spawn_and_wait），外层超时必须 ≥ 内部窗口，否则 cancel 路径产生孤儿进程。
HA_AUTOSTART_TIMEOUT_SEC = ADAPTER_READY_MAX_WAIT_SECONDS + 15
ADAPTER_PROCESS_WAIT_TIMEOUT_SEC = 5         # 子进程退出等待
ADAPTER_RESTART_BACKOFF_SEC = 5              # 重启退避
OC_FILE_READ_GRACE_SEC = 2.0                  # OC 读取 API key 文件宽限

# ---- HTTP 限制 ----
HTTP_MAX_BODY_SIZE = 10 * 1024 * 1024        # 10MB，对标 nginx client_max_body_size
WORKSPACE_MAX_UPLOAD_SIZE = HTTP_MAX_BODY_SIZE  # 上传上限=全局请求体上限（同一份知识，改这里一处）

# ---- 置信度 / 分数阈值（多处重复，必须统一）----
ROUTE_AUTO_SWITCH_CONFIDENCE_THRESHOLD = 0.70  # 路由自动切换置信度（dynamic_orchestrator + agent_lifecycle + loop_repo）
DELEGATION_DEFAULT_SUCCESS_RATE = 0.8          # 委托默认成功率
LEARNED_PREF_SUCCESS_THRESHOLD = 0.7           # 学习偏好成功阈值
LEARNED_PREF_MIN_COUNT = 3                     # 学习偏好最小样本数
FEEDBACK_ACCEPT_DELTA = 0.10                   # 反馈接受分数增量
FEEDBACK_REJECT_DELTA = -0.15                  # 反馈拒绝分数增量
KNOWLEDGE_INITIAL_SCORE = 0.3                  # 新知识节点初始分数
KNOWLEDGE_HIGH_SCORE_THRESHOLD = 0.8           # 高分知识阈值
KNOWLEDGE_LOW_SCORE_THRESHOLD = 0.3            # 低分知识阈值（SQL WHERE）
KNOWLEDGE_PRUNE_THRESHOLD = 0.1                # 知识剪枝阈值
KNOWLEDGE_SCORE_MAX = 1.0                      # 知识分数上限
KNOWLEDGE_DECAY_FACTOR = 0.95                  # 知识衰减因子
MEMORY_DEFAULT_CONFIDENCE = 0.5                # 记忆默认置信度
MEMORY_UNCONSOLIDATED_CONFIDENCE_THRESHOLD = 0.8  # 未固化记忆置信度阈值
MEMORY_ORPHAN_CONFIDENCE_THRESHOLD = 0.3       # 孤立记忆置信度阈值
MEMORY_CONFLICT_DELTA_THRESHOLD = 0.4          # 记忆冲突差异阈值
TMP_ROLE_PROMOTION_THRESHOLD = 0.7             # 临时角色晋升阈值
TMP_ROLE_REWARD_DELTA = 0.2                    # 临时角色奖励增量
ESCALATION_SCORE_DEFAULT = 0.9                 # 升级默认分数
SPEC_ESCALATION_SCORE = 0.8                    # Spec 循环升级分数
MIXED_DELIBERATION_SCORE = 0.5                 # 混合审议分数
GC_AGENT_SCORE = 0.4                           # GC Agent 分数
BUG_FIX_LESSON_SCORE = 0.5                     # Bug 修复经验分数
WORKFLOW_SUMMARY_SCORE = 0.8                   # 工作流总结分数
WS_FACT_CONFIDENCE_DEFAULT = 0.8               # WS 事实置信度默认
PROPOSAL_DEDUP_JACCARD_THRESHOLD = 0.5         # 提案去重 Jaccard 阈值

# ---- LLM temperature 预设（集中管理，对标 MAX_TOKENS_* 模式）----
TEMPERATURE_WORKFLOW_NODE = 0.2
TEMPERATURE_FAILURE_ANALYSIS = 0.1
TEMPERATURE_SKILL_TEST = 0.3
TEMPERATURE_SELF_EVOLUTION = 0.3
TEMPERATURE_WORKSPACE_LLM = 0.1
TEMPERATURE_CODE_REVIEW = 0.3
TEMPERATURE_SKILL_REGISTRY = 0.0
TEMPERATURE_INTENT_CLASSIFY = 0.0
TEMPERATURE_MEMORY_COMPACT = 0.0
TEMPERATURE_PROMPT_AGENT_RECOMMEND = 0.0
TEMPERATURE_PROMPT_AGENT_OPTIMIZE = 0.2

# ---- Prompt Agent 模块常量 ----
PROMPT_AGENT_CACHE_TTL_SEC = 300.0
PROMPT_AGENT_TASK_INPUT_PREFIX_LEN = 80
PROMPT_AGENT_RECOMMEND_MAX_TOKENS = 800
PROMPT_AGENT_OPTIMIZE_MAX_TOKENS = 800
PROMPT_AGENT_OPTIMIZE_MAX_CHANGES = 5
PROMPT_AGENT_LOW_CONFIDENCE_THRESHOLD = 0.6
PROMPT_AGENT_CHARS_PER_TOKEN = 3

# Batch 5.2: Prompt Optimizer 多候选生成与自评
PROMPT_AGENT_OPTIMIZE_CANDIDATE_COUNT = 3
PROMPT_AGENT_OPTIMIZE_MAX_TOKENS_PER_CANDIDATE = 400
PROMPT_AGENT_OPTIMIZE_JSON_OVERHEAD_TOKENS = 200

# ---- Provider 退避 / 冷却 ----
PROVIDER_FALLBACK_INITIAL_BACKOFF_SEC = 2
PROVIDER_DEGRADE_COOLDOWN_SEC = 300

# ---- Dream 退避 ----
DREAM_BACKOFF_BASE_SEC = 3600
DREAM_BACKOFF_MAX_MULTIPLIER = 24

# ---- EventBus 重启 ----
EVENTBUS_RESTART_INITIAL_DELAY = 0.5
EVENTBUS_RESTART_MAX_DELAY = 30.0

# Batch 7.4: EventBus outbox persistence —— 关键事件崩溃后可重投
EVENT_BUS_PERSISTED_TYPES = {
    "task_submitted",
    "task_completed",
    "task_failed",
    "task_cancelled",
    "approval_pending",
    "assistant_turn_complete",
}
EVENT_BUS_OUTBOX_MAX_DELIVERY_ATTEMPTS = 3
EVENT_BUS_OUTBOX_POLL_INTERVAL_SEC = 5.0
EVENT_BUS_OUTBOX_REDELIVERY_GRACE_SEC = 2.0
EVENT_BUS_OUTBOX_BATCH_SIZE = 100
# Batch 5a C5: 消费者连续 N 次无异常分发后退避重置为初始值（对标断路器 half-open）
EVENT_BUS_BACKOFF_RESET_SUCCESSES = 10
# Batch 5a C5: 单 handler 分发耗时超过该秒数记 warning（只观察，不超时中断）
EVENT_BUS_SLOW_HANDLER_SEC = 5.0

# Batch 7.9: 热点服务 TTL 缓存（秒）
SETTINGS_SERVICE_CACHE_TTL_SEC = 5.0
MODEL_CONFIG_CACHE_TTL_SEC = 5.0

# Batch 7.5: API 限流——内存 Token Bucket（单用户桌面应用，无需 Redis）
RATE_LIMIT_ENABLED = True
RATE_LIMIT_READ_RPS = 10.0
RATE_LIMIT_READ_BURST = 20
RATE_LIMIT_WRITE_RPS = 2.0
RATE_LIMIT_WRITE_BURST = 5
RATE_LIMIT_STRICT_RPS = 1.0
RATE_LIMIT_STRICT_BURST = 3
# 严格限流路径前缀（METHOD:PATH，METHOD 为空表示匹配所有方法）
RATE_LIMIT_STRICT_PATHS = (
    "POST:/api/v1/tasks",
    "POST:/api/v1/tools",
    "POST:/api/v1/approvals",
    "POST:/api/v1/hitl",
    "POST:/api/v1/completion",
    "POST:/api/v1/sessions/",
    "POST:/api/v1/dream",
    "POST:/api/v1/evolution",
)
# 豁免路径前缀（精确匹配 health*，以及 auth/docs/redoc/openapi）
RATE_LIMIT_EXEMPT_PATHS = (
    "/api/v1/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/auth/token",
    "/auth/refresh",
)

# Batch 7.6: Bearer token 过期/刷新
ACCESS_TOKEN_TTL_SECONDS = 15 * 60          # 15 分钟
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 天
AUTH_SECRET_SALT_KEY = hashlib.sha256(TOKEN.encode()).hexdigest()[:16] if TOKEN else "auth_salt_dev_only"
# Batch G M5h: OWASP 2023 推荐 PBKDF2-HMAC-SHA256 ≥ 600,000 次迭代
AUTH_TOKEN_PBKDF2_ITERATIONS = 600_000

# ---- HITL 审批 ----
HITL_DEFAULT_TIMEOUT_SEC = 3600

# Scoped standing grants（对标 OpenClaw）：重复/定时任务一次审批后的复用授权有效期。
# 到期自动失效（fail-closed，不默认放行），需重新授权；用户可随时主动撤销。
STANDING_GRANT_TTL_SEC = 30 * 24 * 3600        # 默认 30 天
STANDING_GRANT_MAX_TTL_SEC = 365 * 24 * 3600   # 上限，防止误设成事实上的永久授权

# ---- Terminal / MCP / Shell 超时 ----
TERMINAL_CLOSE_TIMEOUT_SEC = 2
TERMINAL_SESSION_MAX_AGE_SEC = 3600
MCP_PROCESS_CLOSE_TIMEOUT_SEC = 5
MCP_REQUEST_TIMEOUT_SEC = 30
SHELL_CLOSE_TIMEOUT_SEC = 3

# ---- WebSocket send 超时 ----
# P2-4 修复: WS send 无超时会导致慢客户端阻塞整个 task 推送。
# 对标 FastAPI/aiohttp WS 最佳实践: send 加 wait_for 超时，超时即清理连接。
WS_SEND_TIMEOUT_SEC = 5.0

# ---- WebSocket 消息防护 (Batch C M17-M19) ----
# M17: 单条 WS 消息最大字节数（对标 FastAPI default 1MB）
WS_MESSAGE_MAX_BYTES = 1_048_576
# M18: WS 消息速率限制——滑动窗口内最多消息数
WS_RATE_LIMIT_WINDOW_SEC = 10.0
WS_RATE_LIMIT_MAX_MESSAGES = 60  # 10s 内最多 60 条（6 msg/s）
# M19: prompt 最大字符数——防止超长 prompt 耗尽内存/上下文
WS_PROMPT_MAX_CHARS = 512_000  # ~512KB，覆盖大多数代码场景

# ---- 文件监控 ----
FILE_WATCHER_DEBOUNCE_SEC = 0.5
FILE_WATCHER_JOIN_TIMEOUT_SEC = 3

# ---- Prompt Watchdog ----
PROMPT_WATCHDOG_KEEPALIVE_SEC = 3600
PROMPT_WATCHDOG_JOIN_TIMEOUT = 2

# ---- 子进程超时 ----
DEP_CHECK_TIMEOUT_SEC = 5
ICACLS_TIMEOUT_SEC = 5
PLAN_STEP_CMD_TIMEOUT_SEC = 30
STOP_HOOK_PYTEST_TIMEOUT_SEC = 30
SHUTDOWN_DELAY_SEC = 0.1

# ---- SQLite ----
SQLITE_CONNECT_TIMEOUT_SEC = 30.0
SQLITE_BUSY_TIMEOUT_MS = 30000

# Phase 47: 对标 Claude Code — 不设人工限制
ADAPTER_HEALTH_CHECK_TIMEOUT = 5    # HTTP 健康检查超时
ADAPTER_INVOKE_TIMEOUT = 600        # LLM HTTP 调用超时
# Batch 3a A3: 流式连接空闲超时（秒）——chunk 间最大等待。流式 POST 用
# total=None + sock_read=此值：不限制整体时长（长推理可达数十分钟），
# SSE 正常持续有数据/心跳，120s 无 chunk 说明连接已死。
ADAPTER_STREAM_IDLE_TIMEOUT = 120
# Batch 5a C1: SSE finish_reason=length（max_tokens 截断）时追加给用户的提示文案
FINISH_REASON_LENGTH_NOTICE = "⚠ 回答因 max_tokens 上限被截断，可续问「继续」或调大 max_tokens"
ADAPTER_HA_INVOKE_TIMEOUT_SEC = 60  # HA 工具调用超时（execute_tool 默认）
# OC 工具调用超时（execute_tool 默认）。300s 会让「抓 GitHub 失败后委托 OC」空等 5 分钟+
# 才超时（用户感知为"一直等待"）；GitHub GraphQL / web search / notify 等秒级工具，
# 即便 vision/browser 也 <120s。对标 Claude Code 工具超时（~120s）。
OC_INVOKE_TIMEOUT_SEC = 120
INTERNAL_LIMIT = -1                 # 内部查询不设上限

# Phase 63: OC Gateway 恢复探测——cooldown 期间定期探测，成功则立即恢复
OC_GATEWAY_PROBE_INTERVAL_SECONDS = 15  # 探测间隔（对标主流 circuit breaker half-open 探测）
OC_GATEWAY_PROBE_TIMEOUT = 2            # 探测超时（短超时，快速判断）

COMPACT_TRIGGER_MICRO = 0.65       # G1: 主动压缩——65% 触发微型压缩（对标 Claude Code 主动压缩，原 0.80 被动）
COMPACT_TRIGGER_STANDARD = 0.82    # G1: 82% 触发 LLM 摘要压缩（原 0.90，保留余量防超限）
COMPACT_TRIGGER_EMERGENCY = 0.98
COMPACT_KEEP_RECENT_EMERGENCY = 30
COMPACT_MICRO_COOLDOWN_SEC = 120
COMPACT_COOLDOWN_SEC = 300

MAX_ACTIVE_SESSIONS = 50
SESSION_IDLE_TTL = 86400            # 24h — 支持长任务
CLEANUP_INTERVAL = 600

# 后台索引/同步间隔（秒）
CODE_INDEX_INTERVAL_SECONDS = 1800   # 代码符号索引：30 分钟
MEMORY_SYNC_INTERVAL_SECONDS = 900   # MemoryTree ↔ MEMORY.md 同步：15 分钟

DEFAULT_USER_ID = "default"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# ═══════════════════════════════════════════════════════════
# Phase 56-P6: 路由/仓库/服务中仍在使用的硬编码截断与列表上限集中配置
# ═══════════════════════════════════════════════════════════

# 搜索/知识结果展示
SEARCH_RESULT_TITLE_MAX_CHARS = 80

# 会话标题/预览/导出
SESSION_PREVIEW_CHARS = 50
SESSION_EXPORT_CONTENT_CHARS = 8000
LOOP_SESSION_TITLE_MAX_CHARS = 30
LOOP_SESSION_PREVIEW_CHARS = 50

# 错误/日志预览
ERROR_PREVIEW_CHARS = 500
LOG_PREVIEW_CHARS = 200

# 导出/导入限制（与 P5 已有常量语义对齐）
CONFIG_EXPORT_KNOWLEDGE_LIMIT = 500

# Hook / 系统列表限制
HOOK_RULES_LIMIT = 20
HOOK_SENDER_PREVIEW_CHARS = 20
SYSTEM_ANALYTICS_EVENTS_LIMIT = 500
SYSTEM_ARENA_EVENTS_LIMIT = 1000

# Arena 盲测 A/B 常量（Batch 5.1）
ARENA_BATTLE_TIMEOUT_SEC = 60
ARENA_BATTLE_MAX_TOKENS = 1024
ARENA_LEADERBOARD_WINDOW_DAYS = 30
ARENA_BATTLE_TTL_HOURS = 24

# Adapter 工具描述截断与列表上限
ADAPTER_TOOL_DESC_MAX_CHARS = 300
ADAPTER_TOOL_DESC_SHORT_CHARS = 100
ADAPTER_TOOL_LIST_MAX = 15
ADAPTER_OC_TOOL_DESC_MAX_CHARS = 280
ADAPTER_GATEWAY_ERROR_PREVIEW_CHARS = 200

# 角色/技能展示列表上限
AGENT_ROLE_LIST_MAX = 15
SKILL_STRENGTHS_MAX = 5
SKILL_TOOLS_MAX = 8
CAPABILITY_STRENGTHS_MAX = SKILL_STRENGTHS_MAX
CAPABILITY_TOOLS_MAX = SKILL_TOOLS_MAX
CAPABILITY_DELEGATE_STRENGTHS_MAX = 3
CAPABILITY_DELEGATE_TOOLS_MAX = 3

# 工作区 Git/代码/补全预览
WORKSPACE_GIT_STATUS_PREVIEW_CHARS = 200
WORKSPACE_CODE_PREVIEW_CHARS = 500

# 记忆上下文预览
MEMORY_CONTEXT_PREVIEW_CHARS = 300
MEMORY_SEMANTIC_PREVIEW_CHARS = 200
MEMORY_SEMANTIC_MAX = 5

# 记忆仓库辅助上限
MEMORY_FACT_KEYWORDS_MAX = 3
VECTOR_TAGS_MAX = 5

# sqlite-vec 向量维度——与 embedding 模型输出维度一致
# bge-small-zh-v1.5 = 512；all-MiniLM-L6-v2 = 384；paraphrase-multilingual-MiniLM-L12-v2 = 768
# 注意：VEC_DIMENSION 必须与 EMBEDDING_MODEL 输出维度一致，否则 vec0 表维度不匹配。
VEC_DIMENSION = int(os.environ.get("YUNCAT_VEC_DIMENSION", "512"))

# Task 7.2: EmbeddingProvider 配置——支持热切换不同 embedding 后端
# 对标 LangChain Embeddings / LlamaIndex BaseEmbedding
EMBEDDING_PROVIDER = os.environ.get("YUNCAT_EMBEDDING_PROVIDER", "sentence_transformer")
# 默认用 bge-small-zh-v1.5（512 维，中文专用，与 VEC_DIMENSION=512 匹配）。
# 相比 all-MiniLM-L6-v2（英文为主），中文语义检索质量显著提升；96MB 已内置。
EMBEDDING_MODEL = os.environ.get(
    "YUNCAT_EMBEDDING_MODEL",
    "BAAI/bge-small-zh-v1.5",
)

# Commander / Deliberation / Skill
COMMANDER_ACTIVE_SKILL_PROMPTS_LIMIT = 5
DELIBERATION_TASK_PROMPT_PREVIEW_CHARS = 500
DELIBERATION_ROLE_LIST_MAX = AGENT_ROLE_LIST_MAX
SKILL_GENERATED_DESCRIPTION_PREVIEW_CHARS = 100

# Analytics 预览
ANALYTICS_ERROR_PREVIEW_CHARS = ERROR_PREVIEW_CHARS
ANALYTICS_LOG_DATA_PREVIEW_CHARS = LOG_PREVIEW_CHARS
ANALYTICS_REASON_PREVIEW_CHARS = 200
ANALYTICS_FLUSH_BATCH_SIZE = 20  # 缓冲 flush 阈值（analytics_service.py 批量通道）

# 本地补全建议
COMPLETION_SUGGESTION_MAX_LINES = 3
COMPLETION_SUGGESTION_MAX_ITEMS = 3

# Task 6.1: FIM (Fill-in-the-Middle) LLM 内联补全——对标 Cursor / GitHub Copilot / Continue。
# 仅当激活的 provider 配置了 fim 段时启用；否则回退到规则引擎。
FIM_TIMEOUT_SEC = 2.0           # 单次 FIM 请求超时——过长会阻塞用户打字体验
FIM_MAX_TOKENS = 64             # FIM 补全最大 token 数——单行/短块补全足够
FIM_MIN_PREFIX_CHARS = 10       # 最小 prefix 长度——过短不触发 LLM（省 token）
FIM_MAX_PREFIX_CHARS = 4096     # prefix 截断上限——避免大文件全量发送耗 token
FIM_MAX_SUFFIX_CHARS = 2048     # suffix 截断上限
FIM_CACHE_TTL_SEC = 30          # 相同 prefix+suffix 缓存 TTL（减少重复请求）

# MemoryTree ↔ MEMORY.md 同步预览
MEMORY_SYNC_TITLE_MAX_CHARS = 80
MEMORY_SYNC_EXPORT_CONTENT_PREVIEW_CHARS = 200
MEMORY_SYNC_IMPORT_CONTENT_MAX_CHARS = 500

# 上下文压缩模块 (services/memory/memory_compact) 截断/预览常量
COMPACT_MARKER_MESSAGE_PREVIEW_CHARS = 200      # 压缩 marker 单条消息预览
COMPACT_MARKER_TOTAL_CHARS = 8000               # marker 写入 DB 总长度上限
COMPACT_RECENT_CONTEXT_MAX_MESSAGES = 6         # 最近上下文摘要消息条数
COMPACT_RECENT_CONTEXT_PREVIEW_CHARS = 150      # 最近上下文摘要单条长度
COMPACT_DUMP_CONTENT_CHARS = 2000               # workspace dump 单条消息长度
COMPACT_FALLBACK_INTENT_CHARS = 100             # fallback 摘要用户意图长度
COMPACT_FALLBACK_FILES_CHARS = 100              # fallback 摘要文件列表长度
COMPACT_PROBE_CONTENT_PREVIEW_CHARS = 200       # 探针问题引用原话长度
COMPACT_PROBE_MAX_COUNT = 3                     # 探针问题数量上限
COMPACT_SECTION_MAX_CHARS = 150                 # 结构化摘要每节字数上限（提示词约束）

# Phase 56-P8: 工具自我声明结果大小上限（配合 calc_tool_result_limit_with_declaration）
TOOL_SIZE_READ_FILE_CHARS = 51200               # read_file 默认返回 50KB
TOOL_SIZE_LIST_DIR_CHARS = 10000                # list_dir 大目录 JSON 输出
TOOL_SIZE_SEARCH_FILES_CHARS = 30000            # search_files 多结果 excerpts
TOOL_SIZE_CODEGRAPH_CONTEXT_CHARS = 50000       # codegraph_context symbols
TOOL_SIZE_RUN_COMMAND_CHARS = 50000             # run_command stdout/stderr 合并
TOOL_SIZE_CONVERSATION_SEARCH_CHARS = 20000     # search_conversation_history 多结果 preview

# Phase 56-P9: MemoryTree 内部截断/上限常量
MEMORY_TREE_INDEX_CONTENT_CHARS = 2000          # 索引到 vector DB 的内容长度

# Task 4.4: RAG 索引一致性 — MemoryIndexerWorker 后台兜底配置
# Outbox pattern: store_memory/write_knowledge 同步索引 chromadb 失败时，worker 兜底补索引
MEMORY_INDEXER_INTERVAL_SEC = 30.0              # worker 扫描间隔（秒）— 主流值 30s（pgvector CDC / ES reindex 类似）
MEMORY_INDEXER_BATCH_SIZE = 50                  # 每轮扫描的 pending 行数上限
MEMORY_INDEXER_INITIAL_DELAY_SEC = 5.0          # 启动后延迟（等 vector_repo initialize 完成）
MEMORY_TREE_CONVERSATION_PREVIEW_CHARS = 500    # 会话拼接预览长度
MEMORY_TREE_SUMMARY_PROMPT_CHARS = 4000         # LLM 摘要提示词长度
MEMORY_TREE_NODE_TITLE_CHARS = 80               # 节点标题长度
MEMORY_TREE_RESULT_TITLE_CHARS = 60             # 检索结果标题长度
MEMORY_TREE_RESULT_CONTENT_CHARS = 300          # 检索结果内容长度
MEMORY_TREE_SYMBOL_SIGNATURE_CHARS = 300        # 代码符号签名长度
MEMORY_TREE_TAG_CONTENT_CHARS = 2000            # 标签推断内容长度
MEMORY_TREE_DEDUP_TITLE_CHARS = 50              # 去重搜索标题长度
MEMORY_TREE_DEDUP_COMPARE_TITLE_CHARS = 30      # 去重标题比较长度
MEMORY_TREE_DEDUP_WORDS_MAX = 100               # 去重内容分词数
MEMORY_TREE_SYMBOLS_MAX = 30                    # 代码符号数上限
MEMORY_TREE_LINE_MAX_CHARS = 500                # 输出行最大长度
MEMORY_TREE_FACT_LINE_CHARS = 200               # 事实提取单行长度
MEMORY_TREE_FACTS_MAX = 10                      # 事实条数上限
MEMORY_TREE_FALLBACK_PREVIEW_CHARS = 500        # fallback 文本预览长度
MEMORY_TREE_CHUNK_TITLE_CHARS = 50              # chunk 标题长度
MEMORY_TREE_CHUNK_CONTENT_CHARS = 500           # chunk 内容长度
MEMORY_TREE_CHUNKS_MAX = 20                     # chunk 数量上限
MEMORY_TREE_TAGS_MAX = 3                        # 标签数量上限
MEMORY_TREE_CHUNK_TOKENS = 3000                 # 知识块最大 token 数

# M2: Memory Tree 时间衰减策略（Ebbinghaus 遗忘曲线）
# 对标人类记忆：旧知识自然失权，重新访问则增强（修复原 score*0.95 假衰减）
MEMORY_TREE_DECAY_RATE = 0.02                   # 衰减率——exp(-0.02 * age_days)，90 天≈0.16，30 天≈0.55
MEMORY_TREE_ACCESS_BOOST = 0.15                 # 命中检索时 score 提升量（capped at 1.0）
MEMORY_TREE_MAX_AGE_DAYS = 90                   # 超过此天数未访问的节点被 age-based prune

# Batch 2.5: 语义记忆 Ebbinghaus 时间衰减
# 检索时按 created_at 计算 age，将 BM25 分数乘以衰减因子；旧记忆自然失权
MEMORY_DECAY_RATE = 0.02                        # 与 knowledge tree 同量级
MEMORY_DECAY_OVERFETCH = 3                      # 候选池放大倍数——先取 limit*N 再按衰减后分数重排

# Task 7.2 子任务1: RAPTOR 聚类配置
# 对标 RAPTOR 原论文 + LangChain/LlamaIndex RAPTOR 实现
# 将语义相似的叶子节点聚合为父节点（LLM 摘要），构建多级知识树
RAPTOR_SIMILARITY_THRESHOLD = 0.75              # cosine similarity 阈值——>= 此值归入同簇
RAPTOR_MIN_CLUSTER_SIZE = 3                     # 最小簇大小——< 此值不创建父节点
RAPTOR_MAX_CLUSTERS_PER_RUN = 10                # 单次聚类最大簇数——安全上限，防 LLM 调用风暴
RAPTOR_MAX_LEAVES_TO_CONSIDER = 100             # 单次考虑的最大叶子数——O(n²) 聚类，100 已够用
RAPTOR_SUMMARY_MAX_CHARS = 1000                 # 父节点摘要内容字符预算
RAPTOR_AUTO_TRIGGER_LEAF_COUNT = 50             # 自动触发阈值——未分组的叶子数 >= 此值时触发
RAPTOR_MAX_LEVELS = 3                           # 递归最大层级——防无限递归


# ═══════════════════════════════════════════════════════════
# Phase 56-P7: 剩余不合理硬编码截断收敛
# ═══════════════════════════════════════════════════════════

# dynamic_orchestrator
DYNAMIC_ORCHESTRATOR_LAST_RESULT_PREVIEW_CHARS = SUB_AGENT_OUTPUT_PREVIEW_CHARS  # 2000

# deliberation_spec / deliberation_spec_roles
DELIBERATION_AUDIT_LIST_MAX_ITEMS = 15
DELIBERATION_AUDIT_VERIFICATION_RESULTS_MAX_ITEMS = 10
DELIBERATION_SPEC_FILES_PREVIEW_LIMIT = 10
DELIBERATION_SPEC_VERIFY_OUTPUT_PREVIEW_CHARS = 300
DELIBERATION_SPEC_AUTO_FIX_ITEMS_LIMIT = 10
DELIBERATION_SPEC_ESCALATION_ISSUES_LIMIT = 10
DELIBERATION_SPEC_GATE_VIOLATIONS_MAX = 3

# self_evolution
SELF_EVOLUTION_TOOL_SEQUENCE_MAX_LEN = 5
SELF_EVOLUTION_PATTERN_EXAMPLE_PREVIEW_CHARS = 200
SELF_EVOLUTION_PATTERN_TITLE_MAX_CHARS = 80
SELF_EVOLUTION_PATTERN_ID_MAX_CHARS = 100
SELF_EVOLUTION_PATTERN_CONTENT_PREVIEW_CHARS = 2000
SELF_EVOLUTION_OC_INTERACTION_PREVIEW_CHARS = 200

# Batch 5.3: Self-Evolution 质量门控
SELF_EVOLUTION_QUALITY_GATE_ENABLED = True
SELF_EVOLUTION_DRY_RUN_SAMPLE_COUNT = 3
SELF_EVOLUTION_AB_BASELINE_NODE_LIMIT = 1
SELF_EVOLUTION_EVALUATION_PASS_THRESHOLD = 0.6
SELF_EVOLUTION_PENDING_INSTALL_MAX_AGE_DAYS = 30

# skill_registry
SKILL_REGISTRY_ACTIVE_PROMPTS_LIMIT = 12
SKILL_REGISTRY_PROMPT_MOUNT_PREVIEW_CHARS = 60
SKILL_REGISTRY_DEEP_REVIEW_PROMPT_CHARS = 1000

# system_tool_command / system_tool_advanced / system_tool_file_ops / system_tool_fs / system_tool_context
SYSTEM_TOOL_COMMAND_WHITELIST_PREVIEW_MAX = 12
SYSTEM_TOOL_ADVANCED_EDIT_SEARCH_PREVIEW_CHARS = 40
SYSTEM_TOOL_ADVANCED_DELEGATE_RESULT_PREVIEW_CHARS = 1000
SYSTEM_TOOL_ADVANCED_BEST_OF_N_PROMPT_PREVIEW_CHARS = 200
SYSTEM_TOOL_ADVANCED_CRON_TASK_PREVIEW_CHARS = 100
SYSTEM_TOOL_ADVANCED_CRON_TASK_OBSERVATION_CHARS = 80
SYSTEM_TOOL_ADVANCED_CRON_LIST_TASK_PREVIEW_CHARS = 150
SYSTEM_TOOL_ADVANCED_CRON_RAW_PREVIEW_CHARS = 200
FILE_OPS_DIFF_CONTENT_PREVIEW_CHARS = 8192
FILE_OPS_WRITE_PREVIEW_LINES = 80
FILE_OPS_DELETE_PREVIEW_CHARS = 500
FILE_OPS_EDIT_OLD_PREVIEW_CHARS = 3000
FILE_OPS_EDIT_NEW_PREVIEW_CHARS = 3000
SYSTEM_TOOL_FS_DIR_LIST_PREVIEW_MAX = 5
SYSTEM_TOOL_CONTEXT_PROBLEM_PREVIEW_CHARS = 100
SYSTEM_TOOL_CONFIG_ERROR_PREVIEW_CHARS = 300

# P0-1.12: memory_tool 截断上限（search_memory / store_memory 工具）
# 对标 Letta archival_memory_search limit=10 / core_memory_append；
# KNOWLEDGE_NODE_CONTENT_MAX_CHARS 已存在（见 knowledge 段），此处仅约束工具层
MEMORY_TOOL_SEARCH_LIMIT = 20          # search_memory 返回结果上限（混合检索后取 top-N）
MEMORY_TOOL_STORE_MAX_CHARS = 4000     # store_memory 单条内容上限（防止单条记忆占用过多索引空间）

# Task 6.12: 长任务分解器配置——原 task_decomposer.py 硬编码常量提取到 config
# 主流做法参考：MetaGPT / OpenHands 将分解参数作为可配置项，便于不同项目调优
TASK_DECOMPOSER_MAX_LEVEL = 2          # plan_steps 表 CHECK(0-2)：目标(0)→阶段(1)→动作(2)
TASK_DECOMPOSER_MAX_NODES = 30         # 单次分解节点上限，防止 LLM 失控
TASK_DECOMPOSER_MAX_TOKENS = 2000      # 分解需要较长输出
TASK_DECOMPOSER_TEMPERATURE = 0.2      # 分解需要确定性输出
# Task 6.12: 两阶段分解开关（粗→细）
# False（默认）= 单阶段：一次 LLM 调用生成完整 3 层树（向后兼容）
# True = 两阶段：阶段1 粗分解只生成 level 0-1，阶段2 对每个 level 1 节点单独调 LLM 细分解 level 2
# 主流对标：MetaGPT "先写高层任务列表，再逐个细化为具体步骤"——减少 LLM 一次性生成深层树的质量衰减
TASK_DECOMPOSER_TWO_PHASE = False

# Task 6.14: RAG rerank + 阈值过滤——BGE cross-encoder 重排配置
# 主流对标：LlamaIndex SentenceTransformerRerank / LangChain ContextualCompressionRetriever
# rerank 流程：初检（vector+FTS+RRF）→ 取 top RERANKER_TOP_K → cross-encoder 重排 → min_score 过滤 → 返回 top N
# 已禁用：bge-reranker-v2-m3 体积 2.27GB 且需联网下载，本地未内置，关闭后直接返回 RRF 结果。
RERANKER_ENABLED = False              # 全局开关；False 时跳过 rerank（不加载/不联网下载 reranker 模型）
RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"  # 多语言 cross-encoder（50+ 语言，中/英/日/韩）——当前禁用
RERANKER_TOP_K = 50                   # 送入 rerank 的候选数上限（初检多取，rerank 后截断到 limit）
RERANKER_MIN_SCORE = 0.0              # sigmoid 归一化后的最低分数阈值（0.0=不过滤，向后兼容；0.5=过滤低相关）
RERANKER_BATCH_SIZE = 32              # cross-encoder predict 批量大小（防 OOM）


# ═══════════════════════════════════════════════════════════
# 6b-1 修复: Token 计数 / 上下文预算派生函数已提取到 token_budget.py
# config.py 聚焦于静态常量；token_budget.py 聚焦于模型上下文感知的动态计算。
# 通过 re-export 保持 102 个消费者的 `from config import calc_xxx` 路径不变。
# ═══════════════════════════════════════════════════════════
from token_budget import (  # noqa: E402,F401
    L4_LONG_OUTPUT_THRESHOLD_CHARS,
    _SYSTEM_PROMPT_FALLBACK,
    _AVG_TURN_FALLBACK,
    _CHARS_PER_TOKEN_ESTIMATE,
    _DEFAULT_CFG_FALLBACK,
    _MODEL_CONTEXT_DEFAULTS,
    estimate_tokens_cjk,
    estimate_tokens,
    estimate_tokens_for_messages,
    count_tokens_online,
    count_tokens,
    get_context_window_for_model,
    resolve_context_window,
    get_default_model_config,
    estimate_avg_turn_tokens,
    calc_history_limit,
    calc_compaction_threshold,
    calc_max_turns_for_cache,
    calc_tool_result_limit,
    calc_tool_result_limit_with_declaration,
    calc_file_reattach_budget,
    calc_max_tool_rounds,
    calc_max_parallel_roles,
    DEFAULT_MAX_TOOL_ROUNDS_HARD_LIMIT,
    USER_TOOL_ROUNDS_MIN,
    USER_TOOL_ROUNDS_MAX,
    calc_context_budget,
    calc_list_limit,
    calc_context_used_percent,
    calc_micro_compact_threshold,
    calc_micro_compact_preview_budget,
    calc_recent_context_budget,
    calc_three_zone_budget,
    enforce_token_budget,
    protect_critical_info,
    adaptive_truncate,
)
# _log 保留给本模块其他用途（如未来 stop_hooks 包装）；token_budget 内部用自己的 logger
import logging as _logging_mod
_log = _logging_mod.getLogger("yuncat.config")


# ═══════════════════════════════════════════════════════════
# Phase 56-P4: Stop Hooks 按技术栈动态选择验证命令
# ═══════════════════════════════════════════════════════════

STOP_HOOK_TIMEOUT_SEC = 60  # 单条验证命令超时


def detect_project_tech_stacks(workspace: str | os.PathLike) -> list[str]:
    """[已抽到 config_stop_hooks.py] 保留 wrapper 向后兼容。"""
    from config_stop_hooks import detect_project_tech_stacks as _impl
    return _impl(workspace)


def build_stop_hook_commands(workspace: str | os.PathLike) -> list[dict]:
    """[已抽到 config_stop_hooks.py] 保留 wrapper 向后兼容。"""
    from config_stop_hooks import build_stop_hook_commands as _impl
    return _impl(workspace)
