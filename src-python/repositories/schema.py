"""repositories/schema.py —— 数据库初始化、迁移、Schema 定义

架构铁律 #5: SCHEMA_SQL 为建表 SQL 的唯一来源；改表时同步更新 SCHEMA_SQL 和 MIGRATIONS 列表。

Phase 48 T3-10: SEED_ROLES/_seed_roles 拆分到 seed_roles.py，_check_schema_drift 拆分到 schema_checks.py；compaction/session_titles/session_todos 的 DDL 各拆到独立模块。本文件聚焦 DDL + 迁移。
"""
import logging
import time as _time

import aiosqlite

from repositories.seed_roles import SEED_ROLES, _seed_roles
from repositories.compaction_schema import COMPACTION_MIGRATION, COMPACTION_SCHEMA_SQL
from repositories.schema_parts import COMPACTION_ARTIFACTS_MIGRATION, COMPACTION_ARTIFACTS_SCHEMA_SQL, SESSION_DELIVERABLES_MIGRATION, SESSION_DELIVERABLES_SCHEMA_SQL, SESSION_TITLES_MIGRATION, SESSION_TITLES_SCHEMA_SQL, SESSION_TODOS_MIGRATION, SESSION_TODOS_SCHEMA_SQL

logger = logging.getLogger("yuncat.repositories")

# M30: 知识树 FTS5 虚表 + 触发器 DDL——SCHEMA_SQL 和 MIGRATIONS #4 共享单源
# 修改 schema 时只需改这一处，避免两处不同步导致不一致
KNOWLEDGE_FTS_DDL = """CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    node_id UNINDEXED, title, content, tags
);
CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON knowledge_nodes BEGIN
    INSERT INTO knowledge_fts(node_id, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON knowledge_nodes BEGIN
    DELETE FROM knowledge_fts WHERE node_id = old.id;
END;
CREATE TRIGGER IF NOT EXISTS knowledge_au AFTER UPDATE ON knowledge_nodes BEGIN
    DELETE FROM knowledge_fts WHERE node_id = old.id;
    INSERT INTO knowledge_fts(node_id, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
END;
"""

# ============================================================
# Batch 3b B4: conversations FTS5 虚表 + 触发器 DDL——SCHEMA_SQL 和
# MIGRATIONS #32 共享单源（对齐 KNOWLEDGE_FTS_DDL 模式）。
# tokenizer 不指定——与库内既有 memories_fts / knowledge_fts 保持同一选择
# （默认 unicode61）。conversations 是增长最快的表，LIKE '%kw%' 全表扫描
# 随历史膨胀线性劣化；FTS5 倒排索引将搜索降为 O(匹配数)。
# 旧库数据回填由 MIGRATIONS #32 的 INSERT ... SELECT 完成。
# ============================================================
CONVERSATIONS_FTS_DDL = """CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts USING fts5(
    conversation_id UNINDEXED, content
);
CREATE TRIGGER IF NOT EXISTS conversations_ai AFTER INSERT ON conversations BEGIN
    INSERT INTO conversations_fts(conversation_id, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS conversations_ad AFTER DELETE ON conversations BEGIN
    DELETE FROM conversations_fts WHERE conversation_id = old.id;
END;
CREATE TRIGGER IF NOT EXISTS conversations_au AFTER UPDATE ON conversations BEGIN
    DELETE FROM conversations_fts WHERE conversation_id = old.id;
    INSERT INTO conversations_fts(conversation_id, content) VALUES (new.id, new.content);
END;
"""

# ============================================================
# SCHEMA_SQL —— 6.1 节完整建表 SQL（单源真相）
# ============================================================
SCHEMA_SQL = """
-- ===== AGENTS & SKILLS =====
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
    status TEXT DEFAULT 'stopped', capabilities TEXT, config TEXT,
    error_message TEXT, pid INTEGER, port INTEGER,
    created_at INTEGER DEFAULT (strftime('%s','now')),
    updated_at INTEGER DEFAULT (strftime('%s','now'))
);

-- Phase 48 T0-7: Agent ↔ Skill 关联表（loop_repo.py:150-174 使用）
-- PRIMARY KEY(agent_id, skill_name) 保证幂等（INSERT OR IGNORE）
CREATE TABLE IF NOT EXISTS agent_skills (
    agent_id TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    PRIMARY KEY (agent_id, skill_name),
    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS agent_heartbeats (
    agent_id TEXT PRIMARY KEY, last_beat INTEGER,
    cpu_percent REAL, memory_mb REAL,
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);

CREATE TABLE IF NOT EXISTS dream_state (
    id INTEGER PRIMARY KEY DEFAULT 1,
    last_dream_time REAL,
    dream_lock INTEGER DEFAULT 0,
    total_dreams INTEGER DEFAULT 0
);
INSERT OR IGNORE INTO dream_state(id) VALUES(1);

-- ===== TASKS & RUNS =====
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, parent_task_id TEXT, workflow_id TEXT,
    input TEXT NOT NULL, output TEXT, status TEXT DEFAULT 'pending',
    intent_type TEXT, agent_id TEXT, session_id TEXT,
    result_type TEXT DEFAULT 'text', error_message TEXT,
    pinned INTEGER DEFAULT 0,
    tags TEXT DEFAULT '[]',
    task_state TEXT DEFAULT '{}',  -- Phase 43 V.5: 任务状态机 JSON（与 COLUMN_MIGRATIONS 对齐）
    created_at INTEGER DEFAULT (strftime('%s','now')), completed_at INTEGER,
    updated_at INTEGER,             -- Phase 61/62: 最后更新时间（心跳/崩溃恢复）
    resume_count INTEGER DEFAULT 0, -- Phase 62: 崩溃恢复次数
    heartbeat_at REAL,              -- Phase 61: 心跳时间戳（长耗时工具内更新）
    max_budget_tokens INTEGER,      -- AUDIT #13: Token 预算上限（NULL/0=不限）
    used_tokens INTEGER DEFAULT 0   -- AUDIT #13: 累计已用 token（跨轮次累加）
);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_pinned ON tasks(pinned);
CREATE INDEX IF NOT EXISTS idx_tasks_status_updated ON tasks(status, updated_at);  -- Phase 62: 崩溃恢复查询

CREATE TABLE IF NOT EXISTS task_runs (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, run_number INTEGER DEFAULT 1,
    status TEXT DEFAULT 'pending', logs TEXT, error_message TEXT,
    started_at INTEGER, finished_at INTEGER,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_task_runs_task_id ON task_runs(task_id);

CREATE TABLE IF NOT EXISTS task_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL, chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at INTEGER DEFAULT (strftime('%s','now')),
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_task_logs_task_id ON task_logs(task_id, chunk_index);

-- ===== WORKFLOWS =====
CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY, name TEXT NOT NULL,
    description TEXT DEFAULT '',
    graph TEXT NOT NULL,
    is_template INTEGER DEFAULT 0,
    created_at INTEGER DEFAULT (strftime('%s','now')),
    updated_at INTEGER DEFAULT (strftime('%s','now')),
    scope TEXT DEFAULT 'global',           -- Phase 99: 'global' | 'project'
    project_id TEXT,                       -- Phase 99: scope='project' 时关联项目 ID
    category TEXT DEFAULT 'custom',        -- Phase 99: 'automation' | 'template' | 'agent_skill' | 'custom'
    owner_id TEXT                          -- Phase 99: 所有者 ID（agent_id / user_id）
);
CREATE INDEX IF NOT EXISTS idx_workflows_scope_project ON workflows(scope, project_id);  -- Phase 99: 作用域过滤
CREATE INDEX IF NOT EXISTS idx_workflows_category ON workflows(category);                 -- Phase 99: 分类过滤

CREATE TABLE IF NOT EXISTS workflow_runs (
    id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL,
    status TEXT DEFAULT 'pending', current_node_id TEXT,
    error_message TEXT, context TEXT, node_results TEXT,
    snapshot TEXT,
    events_log TEXT,
    parent_run_id TEXT, parent_node_id TEXT,
    started_at INTEGER, finished_at INTEGER,
    created_at INTEGER DEFAULT (strftime('%s','now')),
    updated_at INTEGER DEFAULT (strftime('%s','now')),
    FOREIGN KEY (workflow_id) REFERENCES workflows(id)
);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_status ON workflow_runs(status);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow_id ON workflow_runs(workflow_id);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_parent ON workflow_runs(parent_run_id);

-- Phase 101: 工作流事件触发器——event / cron / manual
CREATE TABLE IF NOT EXISTS workflow_triggers (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    trigger_type TEXT NOT NULL,          -- 'event' | 'cron' | 'manual'
    config TEXT DEFAULT '{}',            -- JSON: event/filter/cron_expr/require_approval/initial_input
    enabled INTEGER DEFAULT 1,
    last_run_at INTEGER,
    next_run_at INTEGER,                 -- cron triggers: 下次到期时间
    run_count INTEGER DEFAULT 0,
    created_at INTEGER DEFAULT (strftime('%s','now')),
    FOREIGN KEY (workflow_id) REFERENCES workflows(id)
);
CREATE INDEX IF NOT EXISTS idx_workflow_triggers_workflow ON workflow_triggers(workflow_id);
CREATE INDEX IF NOT EXISTS idx_workflow_triggers_type_enabled ON workflow_triggers(trigger_type, enabled);
CREATE INDEX IF NOT EXISTS idx_workflow_triggers_next_run ON workflow_triggers(next_run_at);

-- ===== MEMORY & KNOWLEDGE =====
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY, content TEXT NOT NULL, source TEXT, owner TEXT,
    confidence REAL DEFAULT 0.5, visibility TEXT DEFAULT 'private',
    memory_type TEXT DEFAULT 'fact', verified INTEGER DEFAULT 0,
    created_at INTEGER DEFAULT (strftime('%s','now')),
    indexed_at INTEGER,  -- Task 4.4: Outbox pattern — NULL=待索引到 chromadb, timestamp=已索引（NULL 时由 MemoryIndexerWorker 兜底补索引）
    project_id TEXT      -- Task 6.5: 项目级隔离——NULL=global（对所有项目可见，对标 CLAUDE.md global）
);
CREATE INDEX IF NOT EXISTS idx_memories_verified ON memories(verified);
CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at);
CREATE INDEX IF NOT EXISTS idx_memories_owner ON memories(owner);
CREATE INDEX IF NOT EXISTS idx_memories_project_id ON memories(project_id);  -- Task 6.5

-- sqlite-vec 映射表：vec0.rowid → memories.id
-- vec0 虚拟表（memory_vec）在迁移 v26 中创建（需要加载 sqlite-vec 扩展）
CREATE TABLE IF NOT EXISTS memory_vec_map (
    vec_rowid INTEGER PRIMARY KEY,
    memory_id TEXT NOT NULL UNIQUE,
    content_hash TEXT,
    created_at INTEGER DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_memory_vec_map_memory_id ON memory_vec_map(memory_id);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    memory_id UNINDEXED, content
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(memory_id, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    DELETE FROM memories_fts WHERE memory_id = old.id;
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    DELETE FROM memories_fts WHERE memory_id = old.id;
    INSERT INTO memories_fts(memory_id, content) VALUES (new.id, new.content);
END;

CREATE TABLE IF NOT EXISTS memory_tags (
    memory_id TEXT NOT NULL, tag TEXT NOT NULL,
    PRIMARY KEY (memory_id, tag),
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_memory_tags_tag ON memory_tags(tag);

-- v66: Memory Tree 知识图谱（借鉴 OpenHuman——结构化知识存储）
CREATE TABLE IF NOT EXISTS knowledge_nodes (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL,         -- ≤ 3000 token Markdown 块
    parent_id TEXT,
    score REAL DEFAULT 1.0,        -- 衰减评分
    source TEXT,                   -- conversation / workspace / auto_dream
    source_ref TEXT,               -- session_id 或文件路径
    tags TEXT DEFAULT '[]',
    created_at INTEGER DEFAULT (strftime('%s','now')),
    updated_at INTEGER DEFAULT (strftime('%s','now')),
    last_accessed INTEGER,         -- M2: 最后访问时间戳（NULL 时回退 created_at）；命中检索时更新
    indexed_at INTEGER,            -- Task 4.4: Outbox pattern — NULL=待索引到 chromadb, timestamp=已索引
    project_id TEXT,               -- Task 6.5: 项目级隔离——NULL=global（对所有项目可见，对标 CLAUDE.md global）
    FOREIGN KEY (parent_id) REFERENCES knowledge_nodes(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_parent ON knowledge_nodes(parent_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_score ON knowledge_nodes(score);
CREATE INDEX IF NOT EXISTS idx_knowledge_project_id ON knowledge_nodes(project_id);  -- Task 6.5
""" + KNOWLEDGE_FTS_DDL + """
-- sqlite-vec 映射表：vec0.rowid → knowledge_nodes.id
-- vec0 虚拟表（knowledge_vec）在迁移 v26 中创建（需要加载 sqlite-vec 扩展）
CREATE TABLE IF NOT EXISTS knowledge_vec_map (
    vec_rowid INTEGER PRIMARY KEY,
    node_id TEXT NOT NULL UNIQUE,
    content_hash TEXT,
    created_at INTEGER DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_knowledge_vec_map_node_id ON knowledge_vec_map(node_id);

-- ===== CONVERSATIONS =====
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT NOT NULL, token_count INTEGER, forked_from TEXT, agent_id TEXT,
    reasoning_content TEXT,
    tool_calls_json TEXT,
    edited_from TEXT,
    edited_to TEXT,
    variant_of TEXT,
    version_index INTEGER DEFAULT 0,
    created_at INTEGER DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_conversations_session_id ON conversations(session_id);
CREATE INDEX IF NOT EXISTS idx_conversations_agent_id ON conversations(agent_id);
CREATE INDEX IF NOT EXISTS idx_conversations_role ON conversations(role);
CREATE INDEX IF NOT EXISTS idx_conv_session_created ON conversations(session_id, created_at);
""" + CONVERSATIONS_FTS_DDL + SESSION_TITLES_SCHEMA_SQL + SESSION_TODOS_SCHEMA_SQL + SESSION_DELIVERABLES_SCHEMA_SQL + COMPACTION_ARTIFACTS_SCHEMA_SQL + """

-- ===== SYSTEM & RUNTIME =====
CREATE TABLE IF NOT EXISTS analytics_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    event_data TEXT,
    created_at INTEGER DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_analytics_type ON analytics_events(event_type);
CREATE INDEX IF NOT EXISTS idx_analytics_created ON analytics_events(created_at);

CREATE TABLE IF NOT EXISTS rules (
    id TEXT PRIMARY KEY, description TEXT NOT NULL,
    trigger_on TEXT, action TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    created_at INTEGER DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS runtime_states (
    id TEXT PRIMARY KEY,
    state_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL,
    session_id TEXT,
    project_id TEXT,                    -- Phase 107: 项目过滤
    status TEXT DEFAULT 'pending',      -- Phase 107: pending / resolved / expired
    resolved_at REAL,                   -- Phase 107: 批准/拒绝时间戳
    resolution TEXT,                    -- Phase 107: 批准/拒绝说明（含执行结果摘要）
    title TEXT,                         -- Phase 107: Inbox 展示标题
    task_id TEXT                        -- Phase 107: Inbox 按 task 过滤
);
CREATE INDEX IF NOT EXISTS idx_runtime_states_type ON runtime_states(state_type);
CREATE INDEX IF NOT EXISTS idx_runtime_states_expires ON runtime_states(expires_at);
CREATE INDEX IF NOT EXISTS idx_runtime_states_status ON runtime_states(status);                 -- Phase 107: Inbox 查询
CREATE INDEX IF NOT EXISTS idx_runtime_states_task ON runtime_states(task_id);                   -- Phase 107: 按 task 过滤
CREATE INDEX IF NOT EXISTS idx_runtime_states_project ON runtime_states(project_id);             -- Phase 107: 按项目过滤

-- Scoped standing grants（对标 OpenClaw）：重复/定时任务一次审批、长期有效，过期或撤销即
-- 失效（fail-closed）。与 runtime_states 的单次确认互补：runtime_states 是一次性令牌，
-- standing_grants 是按 scope 持久化的复用授权。空串 agent_id / project_id 作通配。
CREATE TABLE IF NOT EXISTS standing_grants (
    id TEXT PRIMARY KEY,
    tool_name TEXT NOT NULL,                     -- 被授权的工具名
    pattern TEXT NOT NULL DEFAULT '',            -- 参数模式（对标 Claude Code 的 Tool(specifier)）：
                                                 --   空串 = 匹配该工具的全部调用（等价裸工具名）
                                                 --   'git diff *' = 前缀匹配；'rm -rf /' = 精确匹配
    rule_type TEXT NOT NULL DEFAULT 'allow',     -- 'allow' | 'deny'——deny 优先于 allow
    agent_id TEXT NOT NULL DEFAULT '',           -- 授权的外部 agent（'' = 全部 agent）
    project_id TEXT NOT NULL DEFAULT '',         -- 项目范围（'' = 全部项目）
    granted_at REAL NOT NULL,                    -- 授予时间戳
    expires_at REAL NOT NULL,                    -- 过期时间戳（fail-closed：过期即拒绝）
    revoked_at REAL,                             -- 撤销时间戳（NULL = 未撤销）
    source TEXT NOT NULL DEFAULT 'user_approval',-- 授权来源
    grant_reason TEXT                            -- 用户备注
);
CREATE INDEX IF NOT EXISTS idx_standing_grants_tool ON standing_grants(tool_name);
CREATE INDEX IF NOT EXISTS idx_standing_grants_expires ON standing_grants(expires_at);
CREATE INDEX IF NOT EXISTS idx_standing_grants_rule ON standing_grants(rule_type);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT, is_encrypted INTEGER DEFAULT 0,
    version INTEGER DEFAULT 1,
    updated_at INTEGER DEFAULT (strftime('%s','now'))  -- H3: key 轮换年龄追踪
);

CREATE TABLE IF NOT EXISTS webhook_nonces (
    nonce TEXT PRIMARY KEY, expires_at INTEGER NOT NULL
);

-- Phase 7b: 消息中心
CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY, type TEXT NOT NULL, title TEXT NOT NULL,
    body TEXT, link TEXT, read INTEGER DEFAULT 0,
    created_at INTEGER DEFAULT (strftime('%s','now')),
    level TEXT DEFAULT 'info', task_id TEXT, agent_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_notifications_read ON notifications(read);

CREATE TABLE IF NOT EXISTS mcp_servers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    command TEXT NOT NULL,
    args TEXT DEFAULT '[]',
    env TEXT DEFAULT '{}',
    enabled INTEGER DEFAULT 1,
    created_at INTEGER DEFAULT (strftime('%s','now')),
    updated_at INTEGER DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS migrations (
    version INTEGER PRIMARY KEY, description TEXT,
    applied_at INTEGER DEFAULT (strftime('%s','now'))
);

-- v58: 角色模板表（多 Agent 讨论引擎——Phase 9）
CREATE TABLE IF NOT EXISTS roles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT DEFAULT 'custom',
    prompt_template TEXT NOT NULL,
    description TEXT,
    enabled INTEGER DEFAULT 1,
    is_builtin INTEGER DEFAULT 0,
    source TEXT DEFAULT 'manual',
    default_behaviors TEXT DEFAULT '[]',
    created_at INTEGER DEFAULT (strftime('%s','now')),
    updated_at INTEGER DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_roles_category ON roles(category);
CREATE INDEX IF NOT EXISTS idx_roles_enabled ON roles(enabled);

-- ===== SAFETY & LOOP SUPERVISOR =====
	-- Phase 19: Side-git 安全网
	CREATE TABLE IF NOT EXISTS snapshots (
	    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, turn_seq INTEGER, phase TEXT,
	    tool_name TEXT, stash_hash TEXT, pre_snapshot_id TEXT,
	    files_affected TEXT DEFAULT '[]', created_at INTEGER DEFAULT (strftime('%s','now'))
	);
	CREATE INDEX IF NOT EXISTS idx_snapshots_task_id ON snapshots(task_id, turn_seq);

	-- Phase 19: Harness 录制回放（无 FK——录制可能引用已删除的 task）
	CREATE TABLE IF NOT EXISTS harness_recordings (
	    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, turn_data TEXT NOT NULL,
	    created_at INTEGER DEFAULT (strftime('%s','now'))
	);
	CREATE INDEX IF NOT EXISTS idx_harness_task_id ON harness_recordings(task_id);

	-- Phase 45: Loop 状态持久化
	CREATE TABLE IF NOT EXISTS loop_registry (
	    loop_id TEXT PRIMARY KEY,
	    loop_type TEXT NOT NULL DEFAULT 'timer',
	    status TEXT NOT NULL DEFAULT 'registered',
	    config TEXT NOT NULL DEFAULT '{}',
	    state TEXT NOT NULL DEFAULT '{}',
	    last_run_at INTEGER,
	    last_ok_at INTEGER,
	    restart_count INTEGER NOT NULL DEFAULT 0,
	    error_count INTEGER NOT NULL DEFAULT 0,
	    last_error TEXT DEFAULT '',
	    created_at INTEGER DEFAULT (strftime('%s','now')),
	    updated_at INTEGER DEFAULT (strftime('%s','now'))
	);
	CREATE INDEX IF NOT EXISTS idx_loop_registry_status ON loop_registry(status);
	CREATE INDEX IF NOT EXISTS idx_loop_registry_type ON loop_registry(loop_type);

	CREATE TABLE IF NOT EXISTS loop_heartbeats (
	    loop_id TEXT PRIMARY KEY REFERENCES loop_registry(loop_id) ON DELETE CASCADE,
	    last_beat INTEGER NOT NULL,
	    iteration_count INTEGER NOT NULL DEFAULT 0
	);

	CREATE TABLE IF NOT EXISTS loop_trigger_queue (
	    id INTEGER PRIMARY KEY AUTOINCREMENT,
	    loop_id TEXT,
	    trigger_type TEXT NOT NULL,
	    payload TEXT NOT NULL DEFAULT '{}',
	    priority INTEGER NOT NULL DEFAULT 0,
	    status TEXT NOT NULL DEFAULT 'pending',
	    claimed_by TEXT,
	    claimed_at INTEGER,
	    completed_at INTEGER,
	    attempts INTEGER NOT NULL DEFAULT 0,
	    error TEXT,
	    created_at INTEGER DEFAULT (strftime('%s','now'))
	);
	CREATE INDEX IF NOT EXISTS idx_trigger_pending ON loop_trigger_queue(status, priority DESC, created_at);
        CREATE INDEX IF NOT EXISTS idx_trigger_type ON loop_trigger_queue(trigger_type);

-- Phase 76-A: 项目级偏好持久化（behavior 选择记忆等）
-- PRIMARY KEY(project_id, key) 保证幂等 UPSERT
CREATE TABLE IF NOT EXISTS project_preferences (
    project_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at INTEGER DEFAULT (strftime('%s','now')),
    PRIMARY KEY (project_id, key)
);

-- Phase 77: 长任务分解树（plan_steps）
-- 与 tasks.parent_task_id 子任务机制并存：plan_steps 是规划分解树，
-- tasks 是可执行运行单元。plan_steps.task_id 关联根 task。
-- level CHECK(0-2)：LLM 3 层以上拆分质量衰减，2 层足以覆盖「目标→阶段→动作」。
CREATE TABLE IF NOT EXISTS plan_steps (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,              -- 关联长任务根 task
    parent_id TEXT,                     -- 父节点 id
    level INTEGER DEFAULT 0 CHECK(level BETWEEN 0 AND 2),
    title TEXT NOT NULL,
    description TEXT,
    dependencies TEXT DEFAULT '[]',     -- [step_id, ...]
    acceptance_criteria TEXT DEFAULT '[]',
    agent_id TEXT,                      -- 指定执行 Agent
    output TEXT,                        -- 节点完成后的结果摘要
    step_output TEXT,                   -- Phase 108: 结构化输出 JSON
    status TEXT DEFAULT 'pending',      -- pending/running/completed/failed/blocked/skipped
    review TEXT DEFAULT '{}',           -- FinalReview / Adversarial 意见 JSON
    created_at INTEGER DEFAULT (strftime('%s','now')),
    updated_at INTEGER DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_plan_steps_task_parent ON plan_steps(task_id, parent_id);
CREATE INDEX IF NOT EXISTS idx_plan_steps_task_status ON plan_steps(task_id, status);

-- AUDIT #9: scheduled_tasks 表——cron 调度任务（替代 loop_trigger_queue 中死数据的 cron 行）
-- next_run_at 由 cron_parser.next_run_at() 计算，_scheduler_tick 按 next_run_at <= now 检索
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id TEXT PRIMARY KEY,
    task_input TEXT NOT NULL,        -- 到点执行的 prompt/任务描述
    agent_id TEXT DEFAULT 'hermes',
    session_id TEXT,                 -- 可复用已有会话（上下文累积）
    cron_expr TEXT NOT NULL,         -- 标准 5 字段 cron 表达式
    next_run_at INTEGER NOT NULL,    -- 下次触发 Unix 时间戳
    status TEXT DEFAULT 'pending',   -- pending / running / completed / paused / failed
    label TEXT DEFAULT '',           -- 任务标签（cron_list 筛选 / cron_delete 批量删除）
    notify TEXT DEFAULT '',          -- 完成时通知渠道(可选)
    created_by TEXT,                 -- 创建者 agent_id 或 task_id
    created_at INTEGER DEFAULT (strftime('%s','now')),
    last_run_at INTEGER,
    run_count INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    max_budget_tokens INTEGER,       -- AUDIT #10/#13: Token 预算上限（NULL/0=不限）
    used_tokens INTEGER DEFAULT 0    -- AUDIT #10/#13: 累计已用 token
);
CREATE INDEX IF NOT EXISTS idx_scheduled_pending ON scheduled_tasks(status, next_run_at);
CREATE INDEX IF NOT EXISTS idx_scheduled_label ON scheduled_tasks(label);

-- Round 1 S6: 防篡改审计日志表——追加式（无 UPDATE/DELETE 接口）
-- 记录所有副作用工具调用（write_file/run_command/delete_file/rename/edit/mkdir 等）
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER DEFAULT (strftime('%s','now')),
    agent_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    args_summary TEXT,          -- 截断 ≤500 字符，防日志膨胀
    success INTEGER,            -- 0/1
    phase TEXT,                 -- preview / execute / blocked
    session_id TEXT,
    task_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_tool ON audit_log(tool_name, ts);

-- M3: 跨 session 记忆继承——持久化结构化摘要（对标 Claude Code --continue）
-- 上下文压缩时写入；新会话首条消息时按 workspace 查询上次摘要注入。
CREATE TABLE IF NOT EXISTS session_summaries (
    session_id TEXT PRIMARY KEY,
    summary_json TEXT NOT NULL,      -- 6-section 结构化摘要 JSON
    workspace TEXT NOT NULL,         -- workspace 路径（项目维度）
    created_at INTEGER DEFAULT (strftime('%s','now')),
    updated_at INTEGER DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_session_summaries_workspace ON session_summaries(workspace, updated_at);

-- Batch 2.6: Self-Evolution 模式持久化表（修复 A-005 _patterns 纯内存导致重启丢计数）
CREATE TABLE IF NOT EXISTS evolution_patterns (
    signature TEXT PRIMARY KEY,
    count INTEGER DEFAULT 0,
    examples TEXT DEFAULT '[]',
    last_seen REAL,
    workflow_generated INTEGER DEFAULT 0,
    skill_generated INTEGER DEFAULT 0,
    skill_exported INTEGER DEFAULT 0,
    created_at INTEGER DEFAULT (strftime('%s','now')),
    updated_at INTEGER DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_evolution_patterns_count ON evolution_patterns(count);
CREATE INDEX IF NOT EXISTS idx_evolution_patterns_last_seen ON evolution_patterns(last_seen);

-- Batch 5.3: Self-Evolution 质量门控——待审核安装记录
CREATE TABLE IF NOT EXISTS evolution_pending_installs (
    id TEXT PRIMARY KEY,
    pattern_signature TEXT NOT NULL,
    task_type TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    skill_dir TEXT NOT NULL,
    examples TEXT DEFAULT '[]',
    sandbox_result TEXT DEFAULT '{}',
    ab_result TEXT DEFAULT '{}',
    evaluation TEXT DEFAULT '{}',
    status TEXT DEFAULT 'pending',
    notification_sent INTEGER DEFAULT 0,
    created_at INTEGER DEFAULT (strftime('%s','now')),
    updated_at INTEGER DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_evolution_pending_status ON evolution_pending_installs(status);
CREATE INDEX IF NOT EXISTS idx_evolution_pending_task_type ON evolution_pending_installs(task_type);

-- Batch 7.4: EventBus 事件持久化 outbox——关键事件写入 DB，崩溃后可重投
CREATE TABLE IF NOT EXISTS event_outbox (
    id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    attempts INTEGER DEFAULT 0,
    created_at INTEGER DEFAULT (strftime('%s','now')),
    processed_at INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_outbox_pending ON event_outbox(processed_at, attempts);
CREATE INDEX IF NOT EXISTS idx_event_outbox_topic ON event_outbox(topic);

-- Batch 7.6: Bearer token 过期/刷新——refresh token 持久化
CREATE TABLE IF NOT EXISTS auth_tokens (
    id TEXT PRIMARY KEY,
    token_type TEXT NOT NULL CHECK(token_type IN ('refresh')),
    token_hash TEXT NOT NULL UNIQUE,
    session_id TEXT,
    expires_at INTEGER,
    revoked_at INTEGER,
    created_at INTEGER DEFAULT (strftime('%s','now')),
    last_used_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_hash ON auth_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_active ON auth_tokens(expires_at, revoked_at);

-- Batch 8.6: 本地错误事件持久化——HTTP 错误响应入库便于排查
-- 不上报外部（CLAUDE.md "数据不出本机"），与 audit_log 语义分离
-- （audit_log 是工具副作用，error_events 是 HTTP 错误响应）
CREATE TABLE IF NOT EXISTS error_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    error_code TEXT NOT NULL,           -- NOT_FOUND / INTERNAL_ERROR / ...
    status_code INTEGER NOT NULL,       -- HTTP 状态码
    message TEXT,                       -- 人类可读消息
    path TEXT,                          -- 请求路径
    method TEXT,                        -- HTTP 方法
    trace_id TEXT,                      -- 关联 trace
    task_id TEXT,                       -- 关联 task（可空）
    agent_id TEXT,                      -- 关联 agent（可空）
    stack_trace TEXT,                   -- 完整堆栈（仅 5xx）
    created_at INTEGER DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_error_events_created ON error_events(created_at);
CREATE INDEX IF NOT EXISTS idx_error_events_code ON error_events(error_code);
""" + COMPACTION_SCHEMA_SQL

# MIGRATIONS —— 增量迁移列表
#
# SCHEMA_SQL 是当前完整 schema 的唯一来源（新库 bootstrap）。
# MIGRATIONS 记录增量变更（旧库升级路径）。
# ============================================================
MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "bootstrap: full schema from SCHEMA_SQL (6.1)", SCHEMA_SQL),
    (2, "Phase 4: add mcp_servers table", """
        CREATE TABLE IF NOT EXISTS mcp_servers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            command TEXT NOT NULL,
            args TEXT DEFAULT '[]',
            env TEXT DEFAULT '{}',
            enabled INTEGER DEFAULT 1,
            created_at INTEGER DEFAULT (strftime('%s','now')),
            updated_at INTEGER DEFAULT (strftime('%s','now'))
        );
    """),
    (3, "Phase 9: add roles table (Deliberation Engine)", """
        CREATE TABLE IF NOT EXISTS roles (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT DEFAULT 'custom',
            prompt_template TEXT NOT NULL,
            description TEXT,
            enabled INTEGER DEFAULT 1,
            is_builtin INTEGER DEFAULT 0,
            created_at INTEGER DEFAULT (strftime('%s','now')),
            updated_at INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_roles_category ON roles(category);
        CREATE INDEX IF NOT EXISTS idx_roles_enabled ON roles(enabled);
    """),
    (4, "v66: add knowledge tree tables (Memory Tree)", """
        CREATE TABLE IF NOT EXISTS knowledge_nodes (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            parent_id TEXT,
            score REAL DEFAULT 1.0,
            source TEXT,
            source_ref TEXT,
            tags TEXT DEFAULT '[]',
            created_at INTEGER DEFAULT (strftime('%s','now')),
            updated_at INTEGER DEFAULT (strftime('%s','now')),
            FOREIGN KEY (parent_id) REFERENCES knowledge_nodes(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_knowledge_parent ON knowledge_nodes(parent_id);
        CREATE INDEX IF NOT EXISTS idx_knowledge_score ON knowledge_nodes(score);
    """ + KNOWLEDGE_FTS_DDL + """
    """),
    (5, "Phase 17: conversations 表性能索引", """
        CREATE INDEX IF NOT EXISTS idx_conversations_agent_id ON conversations(agent_id);
        CREATE INDEX IF NOT EXISTS idx_conversations_role ON conversations(role);
    """),
    (6, "Phase 19: Side-git snapshots + Harness recordings", """
        CREATE TABLE IF NOT EXISTS snapshots (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, turn_seq INTEGER, phase TEXT,
            tool_name TEXT, stash_hash TEXT, pre_snapshot_id TEXT,
            files_affected TEXT DEFAULT '[]', created_at INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_task_id ON snapshots(task_id, turn_seq);
        CREATE TABLE IF NOT EXISTS harness_recordings (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, turn_data TEXT NOT NULL,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_harness_task_id ON harness_recordings(task_id);
    """),
    (7, "Phase 34: conversations 表复合索引 (session_id, created_at)", """
        CREATE INDEX IF NOT EXISTS idx_conv_session_created ON conversations(session_id, created_at);
    """),
    (8, "Phase 45: Loop 状态持久化 (loop_registry + loop_heartbeats + loop_trigger_queue)", """
        CREATE TABLE IF NOT EXISTS loop_registry (
            loop_id TEXT PRIMARY KEY,
            loop_type TEXT NOT NULL DEFAULT 'timer',
            status TEXT NOT NULL DEFAULT 'registered',
            config TEXT NOT NULL DEFAULT '{}',
            state TEXT NOT NULL DEFAULT '{}',
            last_run_at INTEGER,
            last_ok_at INTEGER,
            restart_count INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT DEFAULT '',
            created_at INTEGER DEFAULT (strftime('%s','now')),
            updated_at INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_loop_registry_status ON loop_registry(status);
        CREATE INDEX IF NOT EXISTS idx_loop_registry_type ON loop_registry(loop_type);

        CREATE TABLE IF NOT EXISTS loop_heartbeats (
            loop_id TEXT PRIMARY KEY REFERENCES loop_registry(loop_id) ON DELETE CASCADE,
            last_beat INTEGER NOT NULL,
            iteration_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS loop_trigger_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            loop_id TEXT,
            trigger_type TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            priority INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            claimed_by TEXT,
            claimed_at INTEGER,
            completed_at INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_trigger_pending ON loop_trigger_queue(status, priority DESC, created_at);
        CREATE INDEX IF NOT EXISTS idx_trigger_type ON loop_trigger_queue(trigger_type);
    """),
    (9, "Phase 48 T0-7: agent_skills table + tasks.task_state column + idx_conv_session_created", """
        CREATE TABLE IF NOT EXISTS agent_skills (
            agent_id TEXT NOT NULL,
            skill_name TEXT NOT NULL,
            PRIMARY KEY (agent_id, skill_name),
            FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_conv_session_created ON conversations(session_id, created_at);
    """),
    (10, "Phase 51-E: workflow_runs breakpoint resume timestamps", """
        -- 列添加由 COLUMN_MIGRATIONS 安全处理，此处只建索引避免 duplicate column
        CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow_id ON workflow_runs(workflow_id);
    """),
    (11, "Phase 49-C: conversations message version chain indexes", """
        CREATE INDEX IF NOT EXISTS idx_conversations_variant ON conversations(variant_of);
        CREATE INDEX IF NOT EXISTS idx_conversations_edited_from ON conversations(edited_from);
        CREATE INDEX IF NOT EXISTS idx_conversations_edited_to ON conversations(edited_to);
    """),
    (12, "Phase 61/62: tasks heartbeat + crash recovery indexes", """
        CREATE INDEX IF NOT EXISTS idx_tasks_status_updated ON tasks(status, updated_at);
    """),
    (13, "Phase 76-A: project_preferences table (behavior memory per project)", """
        CREATE TABLE IF NOT EXISTS project_preferences (
            project_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            updated_at INTEGER DEFAULT (strftime('%s','now')),
            PRIMARY KEY (project_id, key)
        );
    """),
    (14, "Phase 77: plan_steps table (long-task decomposition tree, level 0-2)", """
        CREATE TABLE IF NOT EXISTS plan_steps (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            parent_id TEXT,
            level INTEGER DEFAULT 0 CHECK(level BETWEEN 0 AND 2),
            title TEXT NOT NULL,
            description TEXT,
            dependencies TEXT DEFAULT '[]',
            acceptance_criteria TEXT DEFAULT '[]',
            agent_id TEXT,
            output TEXT,
            step_output TEXT,
            status TEXT DEFAULT 'pending',
            review TEXT DEFAULT '{}',
            created_at INTEGER DEFAULT (strftime('%s','now')),
            updated_at INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_plan_steps_task_parent ON plan_steps(task_id, parent_id);
        CREATE INDEX IF NOT EXISTS idx_plan_steps_task_status ON plan_steps(task_id, status);
    """),
    (15, "AUDIT #9: scheduled_tasks table (cron scheduler with real cron expression evaluation)", """
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id TEXT PRIMARY KEY,
            task_input TEXT NOT NULL,
            agent_id TEXT DEFAULT 'hermes',
            session_id TEXT,
            cron_expr TEXT NOT NULL,
            next_run_at INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            label TEXT DEFAULT '',
            notify TEXT DEFAULT '',
            created_by TEXT,
            created_at INTEGER DEFAULT (strftime('%s','now')),
            last_run_at INTEGER,
            run_count INTEGER DEFAULT 0,
            error_count INTEGER DEFAULT 0,
            max_budget_tokens INTEGER,
            used_tokens INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_scheduled_pending ON scheduled_tasks(status, next_run_at);
        CREATE INDEX IF NOT EXISTS idx_scheduled_label ON scheduled_tasks(label);
    """),
    (16, "Phase 99: workflows scope/project_id/category/owner_id + indexes", """
        -- 列添加由 COLUMN_MIGRATIONS 安全处理，此处只建索引避免 duplicate column
        CREATE INDEX IF NOT EXISTS idx_workflows_scope_project ON workflows(scope, project_id);
        CREATE INDEX IF NOT EXISTS idx_workflows_category ON workflows(category);
    """),
    (17, "Phase 107: runtime_states approval inbox indexes (status/task/project)", """
        -- 列添加由 COLUMN_MIGRATIONS 安全处理，此处只建索引避免 duplicate column
        CREATE INDEX IF NOT EXISTS idx_runtime_states_status ON runtime_states(status);
        CREATE INDEX IF NOT EXISTS idx_runtime_states_task ON runtime_states(task_id);
        CREATE INDEX IF NOT EXISTS idx_runtime_states_project ON runtime_states(project_id);
    """),
    (18, "Phase 101: workflow_triggers table (event/cron/manual triggers)", """
        CREATE TABLE IF NOT EXISTS workflow_triggers (
            id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            trigger_type TEXT NOT NULL,
            config TEXT DEFAULT '{}',
            enabled INTEGER DEFAULT 1,
            last_run_at INTEGER,
            next_run_at INTEGER,
            run_count INTEGER DEFAULT 0,
            created_at INTEGER DEFAULT (strftime('%s','now')),
            FOREIGN KEY (workflow_id) REFERENCES workflows(id)
        );
        CREATE INDEX IF NOT EXISTS idx_workflow_triggers_workflow ON workflow_triggers(workflow_id);
        CREATE INDEX IF NOT EXISTS idx_workflow_triggers_type_enabled ON workflow_triggers(trigger_type, enabled);
        CREATE INDEX IF NOT EXISTS idx_workflow_triggers_next_run ON workflow_triggers(next_run_at);
    """),
    (19, "Fix AUDIT #9: ensure scheduled_tasks label/notify columns and index", """
        -- 列由 COLUMN_MIGRATIONS 安全补加；此处仅补建旧版表缺失的索引
        CREATE INDEX IF NOT EXISTS idx_scheduled_label ON scheduled_tasks(label);
    """),
    (20, "Round 1 S6: audit_log table (tamper-evident append-only audit trail)", """
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER DEFAULT (strftime('%s','now')),
            agent_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            args_summary TEXT,
            success INTEGER,
            phase TEXT,
            session_id TEXT,
            task_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
        CREATE INDEX IF NOT EXISTS idx_audit_tool ON audit_log(tool_name, ts);
    """),
    # 21: Round 2 A2 —— settings.version 列由 COLUMN_MIGRATIONS 安全补加，
    # 此处保留版本号占位（旧库已记录 v21，新库经 COLUMN_MIGRATIONS 幂等补列）。
    (21, "Round 2 A2: settings.version column (handled by COLUMN_MIGRATIONS)", """
        SELECT 1;
    """),
    # 22: M3 跨 session 记忆继承——session_summaries 表
    (22, "M3: session_summaries table for cross-session memory inheritance", """
        CREATE TABLE IF NOT EXISTS session_summaries (
            session_id TEXT PRIMARY KEY,
            summary_json TEXT NOT NULL,
            workspace TEXT NOT NULL,
            created_at INTEGER DEFAULT (strftime('%s','now')),
            updated_at INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_session_summaries_workspace ON session_summaries(workspace, updated_at);
    """),
    # 23: H3 API key 轮换提醒——settings.updated_at 列由 COLUMN_MIGRATIONS 安全补加，
    # 此处保留版本号占位（旧库已记录 v23，新库经 COLUMN_MIGRATIONS 幂等补列）。
    (23, "H3: settings.updated_at column (handled by COLUMN_MIGRATIONS)", """
        SELECT 1;
    """),
    # 24: Task 4.4 RAG 索引一致性——memories.indexed_at / knowledge_nodes.indexed_at 列
    # 由 COLUMN_MIGRATIONS 安全补加（幂等），此处保留版本号占位。
    (24, "Task 4.4: indexed_at columns for Outbox pattern (handled by COLUMN_MIGRATIONS)", """
        SELECT 1;
    """),
    # 25: Task 6.5 项目级隔离——memories.project_id / knowledge_nodes.project_id 列
    # 由 COLUMN_MIGRATIONS 安全补加（幂等），此处保留版本号占位 + 索引创建。
    (25, "Task 6.5: project_id columns for project-level isolation (handled by COLUMN_MIGRATIONS)", """
        CREATE INDEX IF NOT EXISTS idx_memories_project_id ON memories(project_id);
        CREATE INDEX IF NOT EXISTS idx_knowledge_project_id ON knowledge_nodes(project_id);
    """),
    # 26: Batch 2.6 Self-Evolution 模式持久化表
    (26, "Batch 2.6: evolution_patterns table for Self-Evolution persistence", """
        CREATE TABLE IF NOT EXISTS evolution_patterns (
            signature TEXT PRIMARY KEY,
            count INTEGER DEFAULT 0,
            examples TEXT DEFAULT '[]',
            last_seen REAL,
            workflow_generated INTEGER DEFAULT 0,
            skill_generated INTEGER DEFAULT 0,
            skill_exported INTEGER DEFAULT 0,
            created_at INTEGER DEFAULT (strftime('%s','now')),
            updated_at INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_evolution_patterns_count ON evolution_patterns(count);
        CREATE INDEX IF NOT EXISTS idx_evolution_patterns_last_seen ON evolution_patterns(last_seen);
    """),
    # 27: Batch 5.3 Self-Evolution 质量门控 pending install 表
    (27, "Batch 5.3: evolution_pending_installs table for quality gate", """
        CREATE TABLE IF NOT EXISTS evolution_pending_installs (
            id TEXT PRIMARY KEY,
            pattern_signature TEXT NOT NULL,
            task_type TEXT NOT NULL,
            workflow_id TEXT NOT NULL,
            skill_dir TEXT NOT NULL,
            examples TEXT DEFAULT '[]',
            sandbox_result TEXT DEFAULT '{}',
            ab_result TEXT DEFAULT '{}',
            evaluation TEXT DEFAULT '{}',
            status TEXT DEFAULT 'pending',
            notification_sent INTEGER DEFAULT 0,
            created_at INTEGER DEFAULT (strftime('%s','now')),
            updated_at INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_evolution_pending_status ON evolution_pending_installs(status);
        CREATE INDEX IF NOT EXISTS idx_evolution_pending_task_type ON evolution_pending_installs(task_type);
    """),
    # 28: Batch 7.4 EventBus 事件持久化 outbox
    (28, "Batch 7.4: event_outbox table for EventBus persistence", """
        CREATE TABLE IF NOT EXISTS event_outbox (
            id TEXT PRIMARY KEY,
            topic TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            attempts INTEGER DEFAULT 0,
            created_at INTEGER DEFAULT (strftime('%s','now')),
            processed_at INTEGER,
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_event_outbox_pending ON event_outbox(processed_at, attempts);
        CREATE INDEX IF NOT EXISTS idx_event_outbox_topic ON event_outbox(topic);
    """),
    # 29: Batch 7.6 Bearer token 过期/刷新
    (29, "Batch 7.6: auth_tokens table for bearer token refresh", """
        CREATE TABLE IF NOT EXISTS auth_tokens (
            id TEXT PRIMARY KEY,
            token_type TEXT NOT NULL CHECK(token_type IN ('refresh')),
            token_hash TEXT NOT NULL UNIQUE,
            session_id TEXT,
            expires_at INTEGER,
            revoked_at INTEGER,
            created_at INTEGER DEFAULT (strftime('%s','now')),
            last_used_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_auth_tokens_hash ON auth_tokens(token_hash);
        CREATE INDEX IF NOT EXISTS idx_auth_tokens_active ON auth_tokens(expires_at, revoked_at);
    """),
    # 30: Batch 8.6 本地错误事件持久化——HTTP 错误响应入库（与 audit_log 语义分离）
    (30, "Batch 8.6: error_events table for local HTTP error reporting", """
        CREATE TABLE IF NOT EXISTS error_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            error_code TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            message TEXT,
            path TEXT,
            method TEXT,
            trace_id TEXT,
            task_id TEXT,
            agent_id TEXT,
            stack_trace TEXT,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_error_events_created ON error_events(created_at);
        CREATE INDEX IF NOT EXISTS idx_error_events_code ON error_events(error_code);
    """),
    # 31: sqlite-vec vec0 虚拟表——替代 chromadb 做向量存储
    # 依赖 sqlite-vec 扩展已加载（在 run_migrations 中加载，best-effort）
    (31, "sqlite-vec: vec0 virtual tables for memory/knowledge vector search", """
        -- vec0 虚拟表：memory 向量索引
        -- embedding 维度在 config.VEC_DIMENSION 中配置（默认 384）
        -- 注意：vec0 仅支持整数 rowid，metadata 通过 memory_vec_map 关联到 memories 表
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(
            embedding FLOAT[384]
        );
        -- vec0 虚拟表：knowledge 向量索引
        CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_vec USING vec0(
            embedding FLOAT[384]
        );
    """),
    # 32: Batch 3b B4 —— conversations FTS5 全文索引（替代搜索路径的 LIKE 全表扫描）
    # DDL 与 SCHEMA_SQL 共享单源（CONVERSATIONS_FTS_DDL）；建表后回填存量数据
    # （回填前先清，防触发器已填充的行重复；DELETE 在建表 DDL 之后故表必存在）
    (32, "Batch 3b B4: conversations_fts for full-text search (replaces LIKE scan)",
        CONVERSATIONS_FTS_DDL + """
        DELETE FROM conversations_fts;
        INSERT INTO conversations_fts(conversation_id, content)
        SELECT id, content FROM conversations;
    """),
    # 33: embedding 升级——bge-small-zh-v1.5（中文专用，512 维）替代 all-MiniLM（384 维）
    # 重建 vec0 表为 512 维（旧 384 维向量作废；memories 极少，重索引成本低）
    # 同时补建映射表——旧库 v1 bootstrap 执行时映射表尚未加入 SCHEMA_SQL，
    # 且后续迁移 v26/v31 均未补建，导致旧库缺 memory_vec_map/knowledge_vec_map。
    (33, "embedding upgrade: rebuild vec0 tables to 512-dim + ensure vec_map tables", """
        DROP TABLE IF EXISTS memory_vec;
        DROP TABLE IF EXISTS knowledge_vec;
        CREATE VIRTUAL TABLE memory_vec USING vec0(
            embedding FLOAT[512]
        );
        CREATE VIRTUAL TABLE knowledge_vec USING vec0(
            embedding FLOAT[512]
        );
        CREATE TABLE IF NOT EXISTS memory_vec_map (
            vec_rowid INTEGER PRIMARY KEY,
            memory_id TEXT NOT NULL UNIQUE,
            content_hash TEXT,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_memory_vec_map_memory_id ON memory_vec_map(memory_id);
        CREATE TABLE IF NOT EXISTS knowledge_vec_map (
            vec_rowid INTEGER PRIMARY KEY,
            node_id TEXT NOT NULL UNIQUE,
            content_hash TEXT,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_knowledge_vec_map_node_id ON knowledge_vec_map(node_id);
    """),
    # 34: 补建 vec 映射表——旧库 v1 bootstrap 执行时映射表尚未加入 SCHEMA_SQL，
    # 且 v31 只建 vec0 虚拟表未建映射表 → 旧库缺 memory_vec_map/knowledge_vec_map，
    # 导致向量 flush 报 "no such table: knowledge_vec_map"。CREATE IF NOT EXISTS 幂等。
    (34, "ensure vec_map tables exist (backfill for old DBs)", """
        CREATE TABLE IF NOT EXISTS memory_vec_map (
            vec_rowid INTEGER PRIMARY KEY,
            memory_id TEXT NOT NULL UNIQUE,
            content_hash TEXT,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_memory_vec_map_memory_id ON memory_vec_map(memory_id);
        CREATE TABLE IF NOT EXISTS knowledge_vec_map (
            vec_rowid INTEGER PRIMARY KEY,
            node_id TEXT NOT NULL UNIQUE,
            content_hash TEXT,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_knowledge_vec_map_node_id ON knowledge_vec_map(node_id);
    """),
    # DDL 与 SCHEMA_SQL 保持一致（新增表须双写：新库 bootstrap + 旧库升级路径）
    (35, "standing grants: scoped persistent pre-authorization for repeated tasks", """
        CREATE TABLE IF NOT EXISTS standing_grants (
            id TEXT PRIMARY KEY,
            tool_name TEXT NOT NULL,
            pattern TEXT NOT NULL DEFAULT '',
            rule_type TEXT NOT NULL DEFAULT 'allow',
            agent_id TEXT NOT NULL DEFAULT '',
            project_id TEXT NOT NULL DEFAULT '',
            granted_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            revoked_at REAL,
            source TEXT NOT NULL DEFAULT 'user_approval',
            grant_reason TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_standing_grants_tool ON standing_grants(tool_name);
        CREATE INDEX IF NOT EXISTS idx_standing_grants_expires ON standing_grants(expires_at);
        CREATE INDEX IF NOT EXISTS idx_standing_grants_rule ON standing_grants(rule_type);
    """),
]
MIGRATIONS.extend([COMPACTION_MIGRATION, SESSION_TITLES_MIGRATION, SESSION_TODOS_MIGRATION, SESSION_DELIVERABLES_MIGRATION, COMPACTION_ARTIFACTS_MIGRATION])

# ============================================================
# COLUMN_MIGRATIONS —— 列级增量迁移（通过 _safe_add_column 执行）
# 新增列时同步更新 SCHEMA_SQL 中对应表的定义。
# ============================================================
# 列级迁移——通过 _safe_add_column 执行。
# Phase 1: 新增 error_message 列（agent_service._mark_task 依赖）
COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("tasks", "error_message", "TEXT"),
    ("tasks", "pinned", "INTEGER DEFAULT 0"),
    ("tasks", "tags", "TEXT DEFAULT '[]'"),
    ("conversations", "forked_from", "TEXT"),
    ("conversations", "agent_id", "TEXT"),
    ("roles", "source", "TEXT DEFAULT 'manual'"),
    ("roles", "default_behaviors", "TEXT DEFAULT '[]'"),  # Phase 75: 角色默认 behavior id 列表（JSON）
    ("workflows", "description", "TEXT DEFAULT ''"),
    ("tasks", "task_state", "TEXT DEFAULT '{}'"),  # Phase 43 V.5
    ("conversations", "reasoning_content", "TEXT"),  # Phase DS.2: DeepSeek多轮契约
    ("conversations", "tool_calls_json", "TEXT"),    # Phase 47: tool_calls 结构化持久化
    ("workflow_runs", "created_at", "INTEGER DEFAULT (strftime('%s','now'))"),  # Phase 51-E: breakpoint resume timestamps
    ("workflow_runs", "updated_at", "INTEGER DEFAULT (strftime('%s','now'))"),  # Phase 51-E: breakpoint resume timestamps
    ("workflow_runs", "parent_run_id", "TEXT"),  # Phase 53: sub-workflow nesting
    ("workflow_runs", "parent_node_id", "TEXT"),  # Phase 53: sub-workflow nesting
    ("conversations", "edited_from", "TEXT"),  # Phase 49-C: message version chain
    ("conversations", "edited_to", "TEXT"),    # Phase 49-C: message version chain
    ("conversations", "variant_of", "TEXT"),   # Phase 49-C: message version chain
    ("conversations", "version_index", "INTEGER DEFAULT 0"),  # Phase 49-C: message version chain
    ("tasks", "updated_at", "INTEGER"),  # Phase 61/62: 心跳/崩溃恢复
    ("tasks", "resume_count", "INTEGER DEFAULT 0"),  # Phase 62: 崩溃恢复次数
    ("tasks", "heartbeat_at", "REAL"),  # Phase 61: 心跳时间戳
    ("tasks", "max_budget_tokens", "INTEGER"),  # AUDIT #13: Token budget
    ("tasks", "used_tokens", "INTEGER DEFAULT 0"),  # AUDIT #13: accumulated token usage
    ("scheduled_tasks", "label", "TEXT DEFAULT ''"),       # AUDIT #9: cron 任务标签
    ("scheduled_tasks", "notify", "TEXT DEFAULT ''"),       # AUDIT #9: 完成通知渠道
    ("scheduled_tasks", "max_budget_tokens", "INTEGER"),  # AUDIT #10: cron task budget
    ("scheduled_tasks", "used_tokens", "INTEGER DEFAULT 0"),  # AUDIT #10: cron task usage
    ("workflows", "scope", "TEXT DEFAULT 'global'"),        # Phase 99: 'global' | 'project'
    ("workflows", "project_id", "TEXT"),                    # Phase 99: scope='project' 时关联项目
    ("workflows", "category", "TEXT DEFAULT 'custom'"),     # Phase 99: 'automation'|'template'|'agent_skill'|'custom'
    ("workflows", "owner_id", "TEXT"),                      # Phase 99: 所有者 ID
    ("runtime_states", "project_id", "TEXT"),               # Phase 107: 项目过滤
    ("runtime_states", "status", "TEXT DEFAULT 'pending'"), # Phase 107: pending/resolved/expired
    ("runtime_states", "resolved_at", "REAL"),              # Phase 107: 批准/拒绝时间戳
    ("runtime_states", "resolution", "TEXT"),               # Phase 107: 批准/拒绝说明
    ("runtime_states", "title", "TEXT"),                    # Phase 107: Inbox 展示标题
    ("runtime_states", "task_id", "TEXT"),                  # Phase 107: 按 task 过滤
    ("settings", "version", "INTEGER DEFAULT 1"),           # Round 2 A2: 乐观锁版本号
    ("settings", "updated_at", "INTEGER DEFAULT (strftime('%s','now'))"),  # H3: key 轮换年龄追踪
    ("knowledge_nodes", "last_accessed", "INTEGER"),        # M2: 命中检索时更新（旧库缺失，SCHEMA_SQL 已有但迁移漏配）
    # Task 4.4: Outbox pattern — indexed_at 列跟踪记忆/知识节点是否已索引到 chromadb
    # NULL=待索引, timestamp=已索引；MemoryIndexerWorker 兜底补索引失败/崩溃未完成的行
    ("memories", "indexed_at", "INTEGER"),
    ("knowledge_nodes", "indexed_at", "INTEGER"),
    # Task 6.5: 项目级隔离——project_id 列（NULL=global，对标 CLAUDE.md global）
    # 旧库升级时为 NULL（global，对所有项目可见）；新库 SCHEMA_SQL 已含此列
    ("memories", "project_id", "TEXT"),
    ("knowledge_nodes", "project_id", "TEXT"),
    # Phase 108: 长任务结构化输出——供后续步骤继承状态
    ("plan_steps", "step_output", "TEXT"),
    # 通知系统：level/task_id/agent_id 落库（修复重启后历史通知丢失级别/关联信息）
    ("notifications", "level", "TEXT DEFAULT 'info'"),
    ("notifications", "task_id", "TEXT"),
    ("notifications", "agent_id", "TEXT"),
]

# ============================================================
# 工具函数
# ============================================================
def _quote_identifier(name: str) -> str:
    """安全引用 SQLite 标识符：双引号包裹，内部双引号转义。"""
    return '"' + name.replace('"', '""') + '"'


async def _safe_add_column(conn, table: str, column: str, definition: str) -> None:
    """安全添加列：表/列已存在则跳过，兼容 SQLite 旧版本升级。

    先检查表是否存在，再 PRAGMA table_info 检查列是否存在，避免新库尚未建表时报错。
    PRAGMA / ALTER TABLE 的标识符部分不支持参数化，调用方均为内部硬编码迁移。
    """
    cursor = await conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    if not await cursor.fetchone():
        return
    # PRAGMA table_info 不支持参数化，使用标识符引用避免 lint 误报并提升安全。
    cursor = await conn.execute(
        "PRAGMA table_info(" + _quote_identifier(table) + ")"
    )
    cols = [row[1] for row in await cursor.fetchall()]
    if column not in cols:
        sql = "ALTER TABLE {} ADD COLUMN {} {}".format(
            _quote_identifier(table), _quote_identifier(column), definition
        )
        await conn.execute(sql)


# ============================================================
# 初始化 / 迁移
# ============================================================
async def init_db(db_path: str) -> aiosqlite.Connection:
    """创建数据库连接，设置 WAL 模式和 PRAGMAs。

    架构铁律 #3: 全局单连接。aiosqlite 后台线程自动序列化写入。
    """
    conn = await aiosqlite.connect(db_path, timeout=30.0)
    conn.row_factory = aiosqlite.Row

    # SQLite 版本校验 —— DELETE RETURNING 需要 3.35.0+
    cursor = await conn.execute("SELECT sqlite_version()")
    version_str = (await cursor.fetchone())[0]
    major, minor = int(version_str.split(".")[0]), int(version_str.split(".")[1])
    if major < 3 or (major == 3 and minor < 35):
        await conn.close()
        raise RuntimeError(
            f"Yuncat requires SQLite >= 3.35.0 (found {version_str}). "
            "Please update your Python/sqlite3 installation."
        )

    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("PRAGMA busy_timeout=30000")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.commit()
    return conn


async def run_migrations(conn: aiosqlite.Connection) -> None:
    """执行增量迁移 + 列级迁移。

    新库：先跑 SCHEMA_SQL bootstrap，再跑增量迁移。
    旧库：跳过已有版本，只跑新迁移。
    """
    # 确保 migrations 表存在
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS migrations (
            version INTEGER PRIMARY KEY, description TEXT,
            applied_at INTEGER DEFAULT (strftime('%s','now'))
        )
    """)

    cursor = await conn.execute("SELECT MAX(version) FROM migrations")
    row = await cursor.fetchone()
    current_version = row[0] if row[0] is not None else 0

    # 加载 sqlite-vec 扩展——best-effort，vec0 虚拟表需要此扩展才能创建和使用
    # 如果加载失败，vector_repo 会降级（向量搜索不可用，FTS5 全文搜索仍正常工作）
    try:
        import sqlite_vec
        await conn.enable_load_extension(True)
        await conn.load_extension(sqlite_vec.loadable_path())
        logger.info("sqlite-vec extension loaded successfully")
    except Exception:
        logger.warning("sqlite-vec extension not available, vector search will be disabled")

    # 列级迁移先执行 —— 确保后续增量迁移依赖的新列已存在（如 Phase 49-C 索引）
    for table, column, definition in COLUMN_MIGRATIONS:
        await _safe_add_column(conn, table, column, definition)

    for version, desc, sql in MIGRATIONS:
        if version > current_version:
            # Task 7.13 (F-128): 用 BEGIN...COMMIT 包裹保证多语句原子性
            # sqlite3.executescript 会先 COMMIT 当前事务（此处为 no-op，前面已 commit），
            # 再执行脚本。脚本内的 BEGIN...COMMIT 形成独立事务。
            # 关键：executescript 中间语句失败时不会自动 ROLLBACK——事务保持开启
            # （in_transaction=True），失败前的语句已生效。必须手动 ROLLBACK 才能回滚，
            # 避免半迁移状态（前 N 条已执行 + 版本号未记录 → 重启后重新执行可能因非幂等失败）。
            # 对标 Django/Prisma 自动事务迁移——每个迁移是一个原子事务。
            # 注：MIGRATIONS 中的 BEGIN...END 均为 CREATE TRIGGER 触发器体语法，
            # 事务控制 `BEGIN;...COMMIT;` 与触发器体 `BEGIN...END` 语法不冲突。
            wrapped_sql = f"BEGIN;\n{sql}\nCOMMIT;"
            try:
                await conn.executescript(wrapped_sql)
            except Exception:
                # executescript 失败时事务仍开启，手动 ROLLBACK 回滚已执行的语句
                if conn.in_transaction:
                    await conn.execute("ROLLBACK")
                raise
            await conn.execute(
                "INSERT OR REPLACE INTO migrations(version, description, applied_at) "
                "VALUES(?, ?, ?)",
                (version, desc, int(_time.time())),
            )
            await conn.commit()
            logger.info("Migration %d: %s", version, desc)

    # Phase 9: 插入内置角色种子数据（幂等）
    await _seed_roles(conn)

    await conn.commit()

    # v66: DDL 漂移检测——SCHEMA_SQL 与 migration 中 knowledge_nodes 表必须一致
    # Phase 48 T3-10: _check_schema_drift 已拆分到 schema_checks.py，延迟导入避免循环依赖
    from repositories.schema_checks import _check_schema_drift
    await _check_schema_drift(conn)
