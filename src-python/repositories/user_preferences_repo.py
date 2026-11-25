"""repositories/user_preferences_repo.py —— 用户偏好的存储与检索

为什么在 repositories 而不是 services：
  偏好本质是 knowledge_nodes 上一条带源隔离的记录（source="user_preference"），
  捕获/检索/调和全是数据库读写，没有业务编排。原先它实现在 services/ 下，
  于是工具层（tools/builtin/memory_tool.py 的 remember_preference）不得不
  `from services.user_preferences import ...`——违反分层约定（Tools 层不依赖
  Service 层，守卫见 tests/structural/test_layer_deps.py）。

  2026-09-22：把存储逻辑下沉到这里，工具层与路由都依赖仓库层；
  services/user_preferences.py 保留原有函数签名（对外契约不变），内部转发过来。
  同一份知识只有这一处实现。

约定：
  - 源隔离：只用 source="user_preference"，不与其他来源（auto_dream / memory 等）混
  - 调和：同 category 下「最新获胜」，旧的标 superseded（保留审计，不参与注入）
"""
from __future__ import annotations

import json
import logging
import time
import uuid

from repositories.knowledge_repo import (
    delete_knowledge_subtree,
    get_knowledge_node,
    upsert_knowledge_node,
)

_log = logging.getLogger("yuncat.user_preferences")

SOURCE = "user_preference"
TAG_PREFERENCE = "preference"
TAG_SUPERSEDED = "superseded"


def _pref_id() -> str:
    return "pref_" + uuid.uuid4().hex[:12]


def _row_to_dict(r: tuple) -> dict:
    return {
        "id": r[0],
        "title": r[1],
        "content": r[2],
        "source": r[3],
        "source_ref": r[4],
        "tags": json.loads(r[5]) if isinstance(r[5], str) else (r[5] or []),
        "created_at": r[6],
        "updated_at": r[7],
        "project_id": r[8],
    }


async def capture_preference(
    conn,
    text: str,
    project_id: str | None = None,
    category: str | None = None,
    source_ref: str = "explicit",
) -> dict:
    """写入一条用户偏好，返回写入后的节点。

    project_id 为空表示全局偏好；非空表示仅该项目可见。
    text 为空时抛 ValueError（fail-closed，不写空偏好）。
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("preference text must not be empty")

    tags = [TAG_PREFERENCE]
    if category:
        tags.append(f"cat:{category}")

    node_id = _pref_id()
    await upsert_knowledge_node(
        conn,
        node_id=node_id,
        title=f"{(category or '偏好')}: {text[:40]}",
        content=text,
        source=SOURCE,
        source_ref=source_ref,
        tags=tags,
        score=1.0,
        project_id=project_id,
    )

    if category:
        await _reconcile_category(conn, category, node_id, project_id)

    node = await get_knowledge_node(conn, node_id)
    if node is None:  # 理论不可达：写入成功后立刻读回
        raise RuntimeError("preference node disappeared after write")
    return node


async def _reconcile_category(conn, category: str, new_id: str, project_id: str | None) -> None:
    """同类别旧偏好标记 superseded（保留审计，不参与注入/检索）。

    tags 以 JSON 存储，json.dumps 默认会转义中文——用 LIKE 匹配 cat:中文 匹配不到，
    所以取出后在 Python 端解析比对。
    """
    pid = project_id if project_id else None
    cursor = await conn.execute(
        "SELECT id, tags FROM knowledge_nodes WHERE source=? AND id != ? "
        "AND (project_id IS NULL OR project_id = ?)",
        (SOURCE, new_id, pid),
    )
    rows = await cursor.fetchall()
    target = f"cat:{category}"
    now = int(time.time())
    marked = False
    for (old_id, tags_json) in rows:
        try:
            tags = json.loads(tags_json) if isinstance(tags_json, str) else (tags_json or [])
        except Exception:
            # 回退：tags 不是合法 JSON 时按"无标签"处理，跳过该行
            tags = []
        if target in tags:
            await conn.execute(
                "UPDATE knowledge_nodes SET tags=?, updated_at=? WHERE id=?",
                (json.dumps([TAG_PREFERENCE, target, TAG_SUPERSEDED]), now, old_id),
            )
            marked = True
    if marked:
        await conn.commit()


async def list_preferences(
    conn,
    project_id: str | None = None,
    include_superseded: bool = False,
) -> list[dict]:
    """列出偏好，按 updated_at 降序。

    project_id 非空时返回「该项目 + 全局(NULL)」偏好；None 只返回全局。
    """
    sql = (
        "SELECT id, title, content, source, source_ref, tags, "
        "created_at, updated_at, project_id FROM knowledge_nodes WHERE source=?"
    )
    params: list = [SOURCE]
    if not include_superseded:
        sql += " AND tags NOT LIKE ?"
        params.append(f"%{TAG_SUPERSEDED}%")
    if project_id:
        sql += " AND (project_id IS NULL OR project_id = ?)"
        params.append(project_id)
    sql += " ORDER BY updated_at DESC"
    cursor = await conn.execute(sql, params)
    rows = await cursor.fetchall()
    return [_row_to_dict(r) for r in rows]


async def delete_preference(conn, node_id: str) -> bool:
    """删除一条偏好（叶子节点）。返回是否删除成功。"""
    n = await delete_knowledge_subtree(conn, node_id)
    return n > 0


async def build_preference_snippet(
    conn,
    project_id: str | None = None,
    limit: int = 6,
) -> str:
    """构建注入系统提示词的偏好摘要块；无偏好时返回空串。"""
    prefs = await list_preferences(conn, project_id=project_id)
    if not prefs:
        return ""
    prefs = prefs[: max(1, limit)]
    lines = [
        "## 用户偏好 (User Preferences)",
        "以下为用户明确表达的偏好，请在回复风格与行为中遵循：",
    ]
    for p in prefs:
        lines.append(f"- {p['content']}")
    return "\n".join(lines)
