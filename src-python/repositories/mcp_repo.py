"""repositories/mcp_repo.py —— MCP 服务器配置 CRUD（Phase 4）"""
import json
import time
import uuid


async def create_mcp_server(
    conn, name: str, command: str, args: list[str] | None = None,
    env: dict[str, str] | None = None, enabled: bool = True,
) -> str:
    """创建 MCP 服务器配置"""
    sid = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO mcp_servers(id, name, command, args, env, enabled) "
        "VALUES(?,?,?,?,?,?)",
        (sid, name, command, json.dumps(args or []), json.dumps(env or {}),
         1 if enabled else 0),
    )
    await conn.commit()
    return sid


async def list_mcp_servers(conn) -> list[dict]:
    """列出所有 MCP 服务器配置"""
    cursor = await conn.execute(
        "SELECT id, name, command, args, env, enabled, created_at, updated_at "
        "FROM mcp_servers ORDER BY created_at DESC"
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": row[0], "name": row[1], "command": row[2],
            "args": json.loads(row[3]), "env": json.loads(row[4]),
            "enabled": bool(row[5]), "created_at": row[6], "updated_at": row[7],
        }
        for row in rows
    ]


async def get_mcp_server(conn, server_id: str) -> dict | None:
    """获取单个 MCP 服务器配置"""
    cursor = await conn.execute(
        "SELECT id, name, command, args, env, enabled, created_at, updated_at "
        "FROM mcp_servers WHERE id=?", (server_id,)
    )
    row = await cursor.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "name": row[1], "command": row[2],
        "args": json.loads(row[3]), "env": json.loads(row[4]),
        "enabled": bool(row[5]), "created_at": row[6], "updated_at": row[7],
    }


async def update_mcp_server(
    conn, server_id: str, **fields,
) -> bool:
    """更新 MCP 服务器配置。只更新传入的字段。"""
    allowed = {"name", "command", "args", "env", "enabled"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return False
    if "args" in updates:
        updates["args"] = json.dumps(updates["args"])
    if "env" in updates:
        updates["env"] = json.dumps(updates["env"])
    if "enabled" in updates:
        updates["enabled"] = 1 if updates["enabled"] else 0
    updates["updated_at"] = int(time.time())
    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [server_id]
    cursor = await conn.execute(
        f"UPDATE mcp_servers SET {set_clause} WHERE id=?", values,
    )
    await conn.commit()
    return cursor.rowcount > 0


async def delete_mcp_server(conn, server_id: str) -> bool:
    """删除 MCP 服务器配置"""
    cursor = await conn.execute("DELETE FROM mcp_servers WHERE id=?", (server_id,))
    await conn.commit()
    return cursor.rowcount > 0
