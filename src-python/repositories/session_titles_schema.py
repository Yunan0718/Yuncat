"""会话标题的 schema 与增量迁移（与 compaction_schema.py 同模式）。

DDL 与 MIGRATIONS 共用同一份常量，避免"新库 bootstrap"与"旧库升级"两处不同步。
"""

SESSION_TITLES_SCHEMA_SQL = """
-- 会话标题的**当前生效值**（latest-wins）。对标 deepseek-harness 的 session/title
-- 日志事件：source=user 的标题被置顶，自动生成不得覆盖；auto_attempted 记录
-- 自动生成是否已认领过，避免每个任务都去调一次模型。
CREATE TABLE IF NOT EXISTS session_titles (
    session_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'fallback',
    provider TEXT DEFAULT '',
    model TEXT DEFAULT '',
    source_message_id TEXT DEFAULT '',
    auto_attempted INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER DEFAULT (strftime('%s','now'))
);
"""

SESSION_TITLES_MIGRATION = (
    37,
    "session titles: persisted latest-wins title with user pin",
    SESSION_TITLES_SCHEMA_SQL,
)
