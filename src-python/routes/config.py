"""routes/config.py —— 配置导出/导入（设备迁移）

从 main.py 提取（原 ~180 行内联端点），Phase 19 P3。
"""
import json as _json
import logging
import time
import uuid as _uuid

from fastapi import APIRouter, HTTPException, Request

from config import (
    CONFIG_EXPORT_CONVERSATION_LIMIT,
    CONFIG_EXPORT_KNOWLEDGE_LIMIT,
    CONFIG_IMPORT_CONVERSATION_LIMIT,
    CONFIG_IMPORT_MAX_KNOWLEDGE_NODES,
)

router = APIRouter(prefix="/api/v1", tags=["config"])
_log = logging.getLogger("yuncat.config")


@router.get("/config/export")
async def export_config(request: Request):
    """一键导出完整配置——用于设备迁移。

    导出内容: models(含API密钥·明文)+ rules + workflows(含完整graph) + roles + conversations + knowledge_nodes
    注: API 密钥在导出文件中为明文——请安全保管导出文件，导入后删除。
    """
    db = request.app.state.db
    from services.settings_service import SettingsService
    svc = SettingsService(db)

    # models — 解密 API Key（用于设备迁移）
    cursor = await db.execute(
        "SELECT key, value FROM settings WHERE key LIKE 'api_key:%' "
        "OR key IN ('active_model','primary_model','design_style','color_scheme',"
        "'security_profile','auto_approve','watch_files_enabled')"
    )
    models = []
    for r in await cursor.fetchall():
        val = r[1]
        if r[0].startswith("api_key:") and val:
            try:
                decrypted = await svc.get_decrypted(r[0])
                val = decrypted if decrypted else val
            except Exception:
                _log.debug("解密 API Key 失败", exc_info=True)
        models.append({"key": r[0], "value": val})

    # rules
    cursor2 = await db.execute(
        "SELECT id, description, trigger_on, action, enabled FROM rules")
    rules = [{"id": r[0], "description": r[1], "trigger_on": r[2],
              "action": r[3], "enabled": r[4]} for r in await cursor2.fetchall()]

    # workflows — 含完整 graph
    cursor3 = await db.execute(
        "SELECT id, name, graph, is_template FROM workflows")
    workflows = [{"id": r[0], "name": r[1], "graph": r[2],
                  "is_template": r[3]} for r in await cursor3.fetchall()]

    # roles (用户自建的非 builtin)
    cursor4 = await db.execute(
        "SELECT id, name, category, prompt_template, description, enabled, is_builtin, source "
        "FROM roles WHERE is_builtin=0")
    roles = [{"id": r[0], "name": r[1], "category": r[2], "prompt_template": r[3],
              "description": r[4], "enabled": r[5], "is_builtin": r[6], "source": r[7]}
             for r in await cursor4.fetchall()]

    # knowledge nodes (score >= 0.5, 最近 CONFIG_EXPORT_KNOWLEDGE_LIMIT 条)
    cursor5 = await db.execute(
        "SELECT id, title, content, source, score, tags, created_at "
        f"FROM knowledge_nodes WHERE score >= 0.3 ORDER BY score DESC LIMIT {CONFIG_EXPORT_KNOWLEDGE_LIMIT}")
    knowledge = [{"id": r[0], "title": r[1], "content": r[2], "source": r[3],
                  "score": r[4], "tags": r[5], "created_at": r[6]}
                 for r in await cursor5.fetchall()]

    # conversations (最近 CONFIG_EXPORT_CONVERSATION_LIMIT 条——完整对话历史)
    cursor6 = await db.execute(
        "SELECT c.session_id, c.role, c.content, c.created_at, "
        "s.id AS s_id FROM conversations c "
        "LEFT JOIN (SELECT id, session_id FROM conversations GROUP BY session_id) s "
        "ON c.session_id = s.session_id "
        f"ORDER BY c.created_at DESC LIMIT {CONFIG_EXPORT_CONVERSATION_LIMIT}")
    conversations = [{"session_id": r[0], "role": r[1], "content": r[2],
                       "created_at": r[3]} for r in await cursor6.fetchall()]

    return {
        "version": "0.3.0",
        "exported_at": int(time.time()),
        "_WARNING": "This export contains API keys in PLAINTEXT. Handle with care.",
        "_encrypted": False,
        "models": models, "rules": rules, "workflows": workflows,
        "roles": roles, "knowledge_nodes": knowledge,
        "conversations": conversations,
        "total_memories": len(knowledge),
        "total_conversations": len(conversations),
    }


@router.post("/config/import")
async def import_config(request: Request):
    """导入配置——从 export 文件恢复到新设备。

    Body: 完整的 export JSON（version + models + rules + workflows + roles + knowledge_nodes + conversations）

    策略:
      - models(API keys) → 覆盖写入 settings 表（加密存储）
      - rules → 幂等 upsert
      - workflows(templates) → 幂等 upsert
      - roles(custom) → 幂等 upsert
      - knowledge_nodes → 幂等 upsert
      - conversations → 只恢复最近 500 条

    返回: {ok, imported: {counts...}}
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON. Body should be the full export JSON.")

    db = request.app.state.db
    counts: dict[str, int] = {}

    # 1. Models/API keys — 加密写入
    from services.settings_service import SettingsService
    svc = SettingsService(db)
    for m in body.get("models", []):
        key, val = m.get("key", ""), m.get("value", "")
        if not key or not val:
            continue
        await svc.save(key, val)
        counts["models"] = counts.get("models", 0) + 1

    # 2. Rules — 幂等 upsert
    for r in body.get("rules", []):
        await db.execute(
            "INSERT OR REPLACE INTO rules(id, description, trigger_on, action, enabled) "
            "VALUES(?,?,?,?,?)",
            (r.get("id", str(_uuid.uuid4())[:12]),
             r.get("description", ""),
             r.get("trigger_on", "[]"),
             r.get("action", "require_confirmation"),
             r.get("enabled", 1)),
        )
        counts["rules"] = counts.get("rules", 0) + 1
    await db.commit()

    # 3. Workflows — 幂等 upsert（保留 graph 完整数据）
    for w in body.get("workflows", []):
        wid = w.get("id", str(_uuid.uuid4())[:12])
        graph = w.get("graph", "{}")
        await db.execute(
            "INSERT OR REPLACE INTO workflows(id, name, graph, is_template) VALUES(?,?,?,?)",
            (wid, w.get("name", ""), graph if isinstance(graph, str) else _json.dumps(graph),
             w.get("is_template", 0)),
        )
        counts["workflows"] = counts.get("workflows", 0) + 1
    await db.commit()

    # 4. Roles (custom only) — 幂等 upsert
    for r in body.get("roles", []):
        await db.execute(
            "INSERT OR REPLACE INTO roles(id, name, category, prompt_template, description, "
            "enabled, is_builtin, source) VALUES(?,?,?,?,?,?,?,?)",
            (r.get("id", str(_uuid.uuid4())[:12]), r.get("name", ""),
             r.get("category", "custom"), r.get("prompt_template", ""),
             r.get("description", ""), r.get("enabled", 1),
             r.get("is_builtin", 0), r.get("source", "imported")),
        )
        counts["roles"] = counts.get("roles", 0) + 1
    await db.commit()

    # 5. Knowledge nodes — 幂等 upsert（最多 CONFIG_IMPORT_MAX_KNOWLEDGE_NODES 条）
    for kn in body.get("knowledge_nodes", [])[:CONFIG_IMPORT_MAX_KNOWLEDGE_NODES]:
        await db.execute(
            "INSERT OR REPLACE INTO knowledge_nodes(id, title, content, source, score, "
            "tags, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (kn.get("id", str(_uuid.uuid4())[:12]), kn.get("title", ""),
             kn.get("content", ""), kn.get("source", "imported"),
             kn.get("score", 0.5), kn.get("tags", "[]"),
             kn.get("created_at", int(time.time())),
             int(time.time())),
        )
        counts["knowledge_nodes"] = counts.get("knowledge_nodes", 0) + 1
    await db.commit()

    # 6. Conversations — 只恢复最近 CONFIG_IMPORT_CONVERSATION_LIMIT 条，按 session 分组
    convs = body.get("conversations", [])[:CONFIG_IMPORT_CONVERSATION_LIMIT]
    for c in convs:
        await db.execute(
            "INSERT OR IGNORE INTO conversations(id, session_id, role, content, created_at) "
            "VALUES(?,?,?,?,?)",
            (str(_uuid.uuid4()), c.get("session_id", str(_uuid.uuid4())[:12]),
             c.get("role", "user"), c.get("content", ""),
             c.get("created_at", int(time.time()))),
        )
        counts["conversations"] = counts.get("conversations", 0) + 1
    await db.commit()

    return {"ok": True, "imported": counts}


@router.post("/data/clear")
async def clear_data(payload: dict, request: Request):
    """按范围清除用户数据（ conversations / tasks / analytics / knowledge_nodes / file_cache ）"""
    scopes = payload.get("scopes", [])
    if not scopes:
        raise HTTPException(400, "No scopes provided")

    db = request.app.state.db
    cleared: dict[str, int] = {}

    async def _count(table: str) -> int:
        # 防御性白名单——即使未来误传用户输入也不会注入
        allowed_tables = {
            "conversations", "tasks", "analytics_events",
            "knowledge_nodes", "file_cache",
        }
        if table not in allowed_tables:
            return 0
        try:
            cursor = await db.execute(f"SELECT COUNT(*) FROM {table}")
            row = await cursor.fetchone()
            return row[0] if row else 0
        except Exception:
            # 留痕：返回 0 会被当成"本来就没有数据"，用户看不到清理其实失败了
            _log.warning("统计待清理表 %s 行数失败，按 0 处理", table, exc_info=True)
            return 0

    if "conversations" in scopes:
        cleared["conversations"] = await _count("conversations")
        await db.execute("DELETE FROM conversations")
    if "tasks" in scopes:
        cleared["tasks"] = await _count("tasks")
        await db.execute("DELETE FROM tasks")
    if "analytics" in scopes:
        cleared["analytics_events"] = await _count("analytics_events")
        await db.execute("DELETE FROM analytics_events")
    if "memories" in scopes:
        cleared["knowledge_nodes"] = await _count("knowledge_nodes")
        await db.execute("DELETE FROM knowledge_nodes")
    if "file_cache" in scopes:
        cleared["file_cache"] = await _count("file_cache")
        await db.execute("DELETE FROM file_cache")

    await db.commit()
    _log.info("Data cleared: %s", cleared)
    return {"ok": True, "cleared": cleared}


@router.post("/config/import-vscode")
async def import_vscode_config(payload: dict, request: Request):
    """导入 VS Code / Cursor settings.json 中的部分偏好到 Yuncat 设置"""
    raw = payload.get("settings", {})
    if not isinstance(raw, dict):
        raise HTTPException(400, "Invalid settings JSON")

    db = request.app.state.db
    from services.settings_service import SettingsService
    svc = SettingsService(db)

    mappings = {
        "workbench.colorTheme": "vscode_color_theme",
        "workbench.preferredDarkColorTheme": "vscode_dark_theme",
        "editor.fontSize": "ui_font_size",
        "editor.fontFamily": "ui_font_family",
        "editor.lineHeight": "ui_line_height",
        "editor.wordWrap": "editor_word_wrap",
        "window.zoomLevel": "ui_zoom_level",
    }

    imported: dict[str, str] = {}
    for vscode_key, yuncat_key in mappings.items():
        val = raw.get(vscode_key)
        if val is not None:
            await svc.save(yuncat_key, str(val))
            imported[yuncat_key] = str(val)

    # 如果 VS Code 主题为暗色，可同步 design_style
    theme = raw.get("workbench.colorTheme", "")
    if isinstance(theme, str) and ("dark" in theme.lower() or " Dark".lower() in theme.lower()):
        await svc.save("design_style", "dark-luxury")
        imported["design_style"] = "dark-luxury"

    await db.commit()
    return {"ok": True, "imported": imported}
