"""repositories/schema_parts.py —— schema 片段的统一出口

各能力域的 DDL 各自放在独立模块（避免 schema.py 继续变大），
这里只做一次汇总，schema.py 只需要一个 import。
"""
from repositories.deliverables_schema import (
    SESSION_DELIVERABLES_MIGRATION,
    SESSION_DELIVERABLES_SCHEMA_SQL,
)
from repositories.compaction_artifacts_schema import (
    COMPACTION_ARTIFACTS_MIGRATION,
    COMPACTION_ARTIFACTS_SCHEMA_SQL,
)
from repositories.session_titles_schema import (
    SESSION_TITLES_MIGRATION,
    SESSION_TITLES_SCHEMA_SQL,
)
from repositories.session_todos_schema import (
    SESSION_TODOS_MIGRATION,
    SESSION_TODOS_SCHEMA_SQL,
)

__all__ = [
    "COMPACTION_ARTIFACTS_MIGRATION",
    "COMPACTION_ARTIFACTS_SCHEMA_SQL",
    "SESSION_DELIVERABLES_MIGRATION",
    "SESSION_DELIVERABLES_SCHEMA_SQL",
    "SESSION_TITLES_MIGRATION",
    "SESSION_TITLES_SCHEMA_SQL",
    "SESSION_TODOS_MIGRATION",
    "SESSION_TODOS_SCHEMA_SQL",
]
