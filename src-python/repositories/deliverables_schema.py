"""会话交付物的 schema 与增量迁移（与 session_titles/session_todos 同模式）。"""

SESSION_DELIVERABLES_SCHEMA_SQL = """
-- 会话交付物：present 工具声明的最终文件（source=present），
-- 以及从会话历史里还原出的"这一轮动过哪些文件"（source=observed）。
CREATE TABLE IF NOT EXISTS session_deliverables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    task_id TEXT DEFAULT '',
    file_path TEXT NOT NULL,
    rel_path TEXT DEFAULT '',
    description TEXT DEFAULT '',
    source TEXT NOT NULL DEFAULT 'present',
    created_at INTEGER DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_session_deliverables_session
    ON session_deliverables(session_id, id);
"""

SESSION_DELIVERABLES_MIGRATION = (
    39,
    "session deliverables: present declarations and observed file changes",
    SESSION_DELIVERABLES_SCHEMA_SQL,
)
