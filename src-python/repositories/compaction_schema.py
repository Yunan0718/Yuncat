"""会话压缩 checkpoint 的 schema 与增量迁移。"""

COMPACTION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS compaction_checkpoints (
    session_id TEXT PRIMARY KEY,
    checkpoint_id TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    source_message_ids_json TEXT NOT NULL DEFAULT '[]',
    source_message_count INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_compaction_checkpoints_created
    ON compaction_checkpoints(created_at);
"""

COMPACTION_MIGRATION = (
    36,
    "session compaction checkpoint with source message traceability",
    COMPACTION_SCHEMA_SQL,
)
