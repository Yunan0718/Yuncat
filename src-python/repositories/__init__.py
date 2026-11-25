"""repositories/__init__.py —— 数据库访问层统一入口（re-export shim）

本模块原为 1269 行单文件，已按领域拆分为子模块：
- schema.py: SCHEMA_SQL / MIGRATIONS / init_db / run_migrations 等
- roles_repo.py: 角色模板 CRUD
- conversations_repo.py: 对话记录 CRUD + 会话分支
- memories_repo.py: 语义记忆 CRUD + FTS5 检索
- rules_repo.py: 用户自定义规则 CRUD
- mcp_repo.py: MCP 服务器配置 CRUD
- knowledge_repo.py: Memory Tree 知识图谱 CRUD
- tasks_repo.py: 任务级联删除 + Plan 子任务 CRUD

为保证向后兼容，所有公开 API 仍可通过 `from repositories import xxx` 访问。
新增功能请直接写入对应子模块，并在下方 re-export。
"""
import logging

# 保持向后兼容：原 __init__.py 暴露的 logger
logger = logging.getLogger("yuncat.repositories")

# ============================================================
# Schema + 迁移 + 初始化
# ============================================================
from repositories.schema import (
    SCHEMA_SQL,
    MIGRATIONS,
    COLUMN_MIGRATIONS,
    _safe_add_column,
    init_db,
    run_migrations,
)
# Phase 48 T3-10: SEED_ROLES/_seed_roles 拆分到 seed_roles.py
from repositories.seed_roles import SEED_ROLES, _seed_roles
# Phase 48 T3-10: _check_schema_drift 拆分到 schema_checks.py
from repositories.schema_checks import _check_schema_drift

# ============================================================
# 角色模板 CRUD（Phase 9）
# ============================================================
from repositories.roles_repo import (
    list_roles,
    get_role,
    create_role,
    update_role,
    delete_role,
    get_enabled_roles,
)

# ============================================================
# 对话记录 CRUD + 会话分支
# ============================================================
from repositories.conversations_repo import (
    insert_conversation,
    flush_conversations,
    get_conversation,
    fork_conversation,
    edit_conversation,
    get_conversation_versions,
    delete_conversations_by_ids,
)
from repositories.compaction_repo import (
    get_compaction_checkpoint,
    commit_compaction_checkpoint,
)

# ============================================================
# 语义记忆 CRUD + FTS5 检索
# ============================================================
from repositories.memories_repo import (
    insert_memory,
    delete_memory,
    update_memory,
    find_memory_conflicts,
    list_recent_memories,       # MemoryExplorer 浏览视图
    search_memories_fts,
    verify_memory_record,
    mark_memory_indexed,        # Task 4.4: Outbox pattern
    list_pending_memory_index,  # Task 4.4: MemoryIndexerWorker 兜底扫描
)

# ============================================================
# 用户自定义规则 CRUD
# ============================================================
from repositories.rules_repo import (
    insert_rule,
    get_enabled_rules,
    get_all_rules,
    toggle_rule,
    delete_rule,
)

# ============================================================
# MCP 服务器配置 CRUD（Phase 4）
# ============================================================
from repositories.mcp_repo import (
    create_mcp_server,
    list_mcp_servers,
    get_mcp_server,
    update_mcp_server,
    delete_mcp_server,
)

# ============================================================
# Memory Tree 知识图谱 CRUD（v66）
# ============================================================
from repositories.knowledge_repo import (
    upsert_knowledge_node,
    adjust_knowledge_score,
    get_knowledge_node,
    search_knowledge_tree,
    _sanitize_fts5_query,
    decay_knowledge_scores,
    get_knowledge_children,
    list_knowledge_roots,
    list_knowledge_leaves,
    list_recent_knowledge_nodes,  # MemoryExplorer 知识浏览视图
    delete_knowledge_subtree,
    update_knowledge_node,
    _safe_tags,  # tags 列容错解析——脏数据不得让整个列表接口 500
)

# ============================================================
# 任务级联删除 + Plan 子任务 CRUD
# ============================================================
from repositories.tasks_repo import (
    delete_task_cascade,
    delete_workflow_cascade,
    create_plan_steps,
    get_plan_steps,
    update_task_state,
    update_task_heartbeat,
)

# ============================================================
# Phase 77: 长任务分解树 plan_steps CRUD（plan_step_repo）
# Task 3.3: 补导出 create_plan_tree / get_plan_tree_by_task / delete_pending_children
#           原 __init__.py 漏导出导致 task_decomposer 和 test_task_decomposer import 失败
# ============================================================
from repositories.plan_step_repo import (
    create_plan_node,
    create_plan_tree,
    get_plan_tree_by_task,
    delete_pending_children,
    delete_pending_subtree,
    update_plan_node_status,
    get_pending_leaves,
    get_ready_nodes,
)

# ============================================================
# Loop trigger queue + 路由辅助（Phase 47）
# ============================================================
from repositories.loop_repo import (
    insert_loop_trigger,
    create_deliberation_task,
    complete_task,
    fail_task,
    upsert_workflow,
    delete_session_messages,
    list_sessions_summary,
    list_pending_temp_roles,
    promote_temp_role,
    reject_temp_role,
    insert_agent_skill,
    remove_agent_skill,
    get_agent_skills,
)

# 会话标题持久化（最新生效 + 用户置顶）
from repositories.session_titles_repo import (
    get_session_title,
    set_session_title,
    delete_session_title,
    claim_auto_attempt,
)

# 会话任务清单（todo_write 整体替换）
from repositories.session_todos_repo import (
    get_session_todos,
    set_session_todos,
    delete_session_todos,
)

# 会话交付物（present 声明 + 观测到的文件改动）
from repositories.deliverables_repo import (
    add_session_deliverable,
    list_session_deliverables,
    delete_session_deliverables,
)

# Batch 2.6: Self-Evolution 模式持久化
from repositories.evolution_repo import (
    load_patterns as load_evolution_patterns,
    upsert_pattern as upsert_evolution_pattern,
    delete_pattern as delete_evolution_pattern,
    delete_stale_patterns as delete_stale_evolution_patterns,
)


__all__ = [
    # Schema + 迁移
    "SCHEMA_SQL",
    "MIGRATIONS",
    "COLUMN_MIGRATIONS",
    "SEED_ROLES",
    "init_db",
    "run_migrations",
    # 角色模板
    "list_roles",
    "get_role",
    "create_role",
    "update_role",
    "delete_role",
    "get_enabled_roles",
    # 对话记录
    "insert_conversation",
    "flush_conversations",
    "get_conversation",
    "fork_conversation",
    "edit_conversation",
    "get_conversation_versions",
    "get_compaction_checkpoint",
    "commit_compaction_checkpoint",
    # 语义记忆
    "insert_memory",
    "delete_memory",
    "update_memory",
    "find_memory_conflicts",
    "list_recent_memories",       # MemoryExplorer 浏览视图
    "search_memories_fts",
    "verify_memory_record",
    "mark_memory_indexed",        # Task 4.4
    "list_pending_memory_index",  # Task 4.4
    # 规则
    "insert_rule",
    "get_enabled_rules",
    "get_all_rules",
    "toggle_rule",
    "delete_rule",
    # MCP 服务器
    "create_mcp_server",
    "list_mcp_servers",
    "get_mcp_server",
    "update_mcp_server",
    "delete_mcp_server",
    # 知识图谱
    "upsert_knowledge_node",
    "adjust_knowledge_score",
    "get_knowledge_node",
    "search_knowledge_tree",
    "decay_knowledge_scores",
    "get_knowledge_children",
    "list_knowledge_roots",
    "list_knowledge_leaves",
    "list_recent_knowledge_nodes",  # MemoryExplorer 知识浏览视图
    "delete_knowledge_subtree",
    "update_knowledge_node",
    # 任务
    "delete_task_cascade",
    "delete_workflow_cascade",
    "create_plan_steps",
    "get_plan_steps",
    "update_task_state",
    "update_task_heartbeat",
    # Phase 77: plan_steps 分解树（plan_step_repo）
    "create_plan_node",
    "create_plan_tree",
    "get_plan_tree_by_task",
    "delete_pending_children",
    "delete_pending_subtree",
    "update_plan_node_status",
    "get_pending_leaves",
    "get_ready_nodes",
    # Batch 2.6: Self-Evolution 模式持久化
    "load_evolution_patterns",
    "upsert_evolution_pattern",
    "delete_evolution_pattern",
    "delete_stale_evolution_patterns",
    # 会话标题
    "get_session_title",
    "set_session_title",
    "delete_session_title",
    "claim_auto_attempt",
    "get_session_todos",
    "set_session_todos",
    "delete_session_todos",
    "add_session_deliverable",
    "list_session_deliverables",
    "delete_session_deliverables",
]
