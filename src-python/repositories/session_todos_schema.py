"""会话任务清单的 schema 与增量迁移（与 session_titles_schema 同模式）。"""

SESSION_TODOS_SCHEMA_SQL = """
-- 会话级任务清单：Agent 用 todo_write 整体替换，模型（经工具结果与会话历史）
-- 与 UI 读同一份数据。source of truth 在这张表，不在内存。
CREATE TABLE IF NOT EXISTS session_todos (
    session_id TEXT PRIMARY KEY,
    todos_json TEXT NOT NULL DEFAULT '[]',
    owner_agent_id TEXT DEFAULT '',
    updated_at INTEGER DEFAULT (strftime('%s','now'))
);
"""

SESSION_TODOS_MIGRATION = (
    38,
    "session todos: whole-list replacement task list per chat session",
    SESSION_TODOS_SCHEMA_SQL,
)
