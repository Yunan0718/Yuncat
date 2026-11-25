"""repositories/harness_schema.py —— Harness 专属建表 SQL（独立于 schema.py）

设计约束（docs/harness-supervisor-worker.md §3.5）：
  - 不动现有 tasks 表——它被 agent_service 全链路依赖，往里面塞 harness
    字段 = 高危补丁。
  - 只幂等建 5 张 harness_* 表，挂载时机由调用方决定：
    测试直接 ensure_harness_schema；生产后续在 lifespan 启动钩子追加。
  - 时间戳沿用 schema.py 的 strftime('%s','now') 约定。
"""
from __future__ import annotations

import logging

_log = logging.getLogger("yuncat.repositories")

HARNESS_SCHEMA_SQL = """
-- ===== HARNESS TASKS（任务主表）=====
CREATE TABLE IF NOT EXISTS harness_tasks (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE NOT NULL,   -- 幂等：重复 assign 返回既有任务
    supervisor_agent_id TEXT NOT NULL,
    worker_agent_id TEXT,
    mission TEXT NOT NULL,
    acceptance_criteria TEXT DEFAULT '[]',  -- JSON list[str]
    constraints TEXT DEFAULT '[]',
    non_goals TEXT DEFAULT '[]',
    permissions TEXT DEFAULT '{}',          -- JSON（HarnessPermissions）
    execution_budget TEXT DEFAULT '{}',     -- JSON（ExecutionBudget）
    attempt INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    status TEXT DEFAULT 'PENDING',
    created_at INTEGER DEFAULT (strftime('%s','now')),
    updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_harness_tasks_status ON harness_tasks(status);
CREATE INDEX IF NOT EXISTS idx_harness_tasks_idempotency ON harness_tasks(idempotency_key);

-- ===== ATTEMPTS（执行尝试）=====
CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    worker_agent_id TEXT,
    status TEXT DEFAULT 'RUNNING',
    error_message TEXT,
    started_at INTEGER,
    finished_at INTEGER,
    FOREIGN KEY (task_id) REFERENCES harness_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_attempts_task_id ON attempts(task_id);

-- ===== EXECUTIONS（Worker 汇报 + Verifier 取证）=====
CREATE TABLE IF NOT EXISTS executions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    attempt_id TEXT,
    claimed_files_modified TEXT DEFAULT '[]',   -- Worker 主张
    actual_files_modified TEXT DEFAULT '[]',    -- Verifier 取证
    claimed_test_result TEXT DEFAULT '',
    test_exit_code INTEGER,
    verifier_decision TEXT DEFAULT 'UNVERIFIED',
    summary TEXT DEFAULT '',
    created_at INTEGER DEFAULT (strftime('%s','now')),
    FOREIGN KEY (task_id) REFERENCES harness_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_executions_task_id ON executions(task_id);

-- ===== PERMISSION_REQUESTS（L1 审批）=====
CREATE TABLE IF NOT EXISTS permission_requests (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    attempt_id TEXT,
    tool_name TEXT NOT NULL,
    arguments TEXT DEFAULT '{}',
    path_canonical TEXT,
    level INTEGER DEFAULT 3,                -- PermissionLevel 数值（0-6）
    status TEXT DEFAULT 'PENDING',
    decided_by TEXT,
    decided_at INTEGER,
    created_at INTEGER DEFAULT (strftime('%s','now')),
    FOREIGN KEY (task_id) REFERENCES harness_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_permission_requests_task_id ON permission_requests(task_id);
CREATE INDEX IF NOT EXISTS idx_permission_requests_status ON permission_requests(status);

-- ===== STATE_TRANSITIONS（状态迁移审计）=====
CREATE TABLE IF NOT EXISTS state_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason TEXT DEFAULT '',
    created_at INTEGER DEFAULT (strftime('%s','now')),
    FOREIGN KEY (task_id) REFERENCES harness_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_state_transitions_task_id ON state_transitions(task_id);
"""


async def ensure_harness_schema(conn) -> None:
    """幂等建表——可安全重复调用。conn 需为已初始化的 aiosqlite 连接。"""
    await conn.executescript(HARNESS_SCHEMA_SQL)
    await conn.commit()
    _log.debug("Harness schema ensured")
