"""repositories/roles_repo.py —— 角色模板 CRUD（Phase 9 讨论引擎）"""
import time


async def list_roles(conn, category: str | None = None) -> list[dict]:
    """列出角色模板——可按 category 过滤"""
    if category:
        cursor = await conn.execute(
            "SELECT id, name, category, prompt_template, description, enabled, is_builtin, "
            "created_at, updated_at FROM roles WHERE category=? ORDER BY is_builtin DESC, name",
            (category,),
        )
    else:
        cursor = await conn.execute(
            "SELECT id, name, category, prompt_template, description, enabled, is_builtin, "
            "created_at, updated_at FROM roles ORDER BY is_builtin DESC, name",
        )
    rows = await cursor.fetchall()
    return [
        {"id": r[0], "name": r[1], "category": r[2], "prompt_template": r[3],
         "description": r[4], "enabled": bool(r[5]), "is_builtin": bool(r[6]),
         "created_at": r[7], "updated_at": r[8]}
        for r in rows
    ]


async def get_role(conn, role_id: str) -> dict | None:
    cursor = await conn.execute(
        "SELECT id, name, category, prompt_template, description, enabled, is_builtin, "
        "created_at, updated_at FROM roles WHERE id=?", (role_id,),
    )
    r = await cursor.fetchone()
    if not r:
        return None
    return {"id": r[0], "name": r[1], "category": r[2], "prompt_template": r[3],
            "description": r[4], "enabled": bool(r[5]), "is_builtin": bool(r[6]),
            "created_at": r[7], "updated_at": r[8]}


async def create_role(conn, rid: str, name: str, prompt_template: str,
                      description: str = "", category: str = "custom") -> str:
    await conn.execute(
        "INSERT INTO roles(id, name, category, prompt_template, description, "
        "enabled, is_builtin, created_at, updated_at) VALUES(?,?,?,?,?,1,0,?,?)",
        (rid, name, category, prompt_template, description, int(time.time()), int(time.time())),
    )
    await conn.commit()
    return rid


async def update_role(conn, role_id: str, **fields) -> bool:
    allowed = {"name", "prompt_template", "description", "enabled"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return False
    if "enabled" in updates:
        updates["enabled"] = 1 if updates["enabled"] else 0
    updates["updated_at"] = int(time.time())
    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [role_id]
    cursor = await conn.execute(f"UPDATE roles SET {set_clause} WHERE id=?", values)
    await conn.commit()
    return cursor.rowcount > 0


async def delete_role(conn, role_id: str) -> bool:
    """删除角色——内置角色不可删除"""
    row = await get_role(conn, role_id)
    if not row or row.get("is_builtin"):
        return False
    cursor = await conn.execute("DELETE FROM roles WHERE id=?", (role_id,))
    await conn.commit()
    return cursor.rowcount > 0


async def get_enabled_roles(conn) -> list[dict]:
    cursor = await conn.execute(
        "SELECT id, name, category, prompt_template, description FROM roles "
        "WHERE enabled=1 ORDER BY is_builtin DESC, name",
    )
    rows = await cursor.fetchall()
    return [{"id": r[0], "name": r[1], "category": r[2], "prompt_template": r[3],
             "description": r[4]} for r in rows]
