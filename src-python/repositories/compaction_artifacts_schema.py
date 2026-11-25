"""压缩留档的 schema 与增量迁移。

task_logs 的列结构是 (task_id, chunk_index, content) 且 task_id 外键指向 tasks(id)，
装不下"按会话留档"的原文——之前的压缩 marker 就是因此**静默写失败**的。
这里给压缩产物一张专表：会话级、无外键、按 kind 区分用途。
"""

COMPACTION_ARTIFACTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS compaction_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',     -- compact_snapshot | tool_pruned
    ref_id TEXT DEFAULT '',            -- 关联消息 id（tool_pruned 用）
    content TEXT NOT NULL,
    created_at INTEGER DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_compaction_artifacts_session
    ON compaction_artifacts(session_id, id);
"""

COMPACTION_ARTIFACTS_MIGRATION = (
    40,
    "compaction artifacts: session-scoped original text for markers and pruned tool output",
    COMPACTION_ARTIFACTS_SCHEMA_SQL,
)
