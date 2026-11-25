"""repositories/rules_repo.py —— 用户自定义规则 CRUD"""
import json
import uuid


async def insert_rule(
    conn, description: str, trigger_on: list[str], action: str, enabled: bool = True
) -> str:
    """插入用户自定义规则"""
    rule_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO rules(id, description, trigger_on, action, enabled) VALUES(?,?,?,?,?)",
        (rule_id, description, json.dumps(trigger_on), action, 1 if enabled else 0),
    )
    await conn.commit()
    return rule_id


async def get_enabled_rules(conn) -> list[dict]:
    """获取所有已启用的规则"""
    cursor = await conn.execute(
        "SELECT id, description, trigger_on, action, enabled FROM rules WHERE enabled=1"
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": row[0], "description": row[1],
            "trigger_on": json.loads(row[2]), "action": row[3], "enabled": bool(row[4]),
        }
        for row in rows
    ]


async def get_all_rules(conn) -> list[dict]:
    """获取所有规则（含禁用的）"""
    cursor = await conn.execute(
        "SELECT id, description, trigger_on, action, enabled FROM rules"
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": row[0], "description": row[1],
            "trigger_on": json.loads(row[2]), "action": row[3], "enabled": bool(row[4]),
        }
        for row in rows
    ]


async def toggle_rule(conn, rule_id: str, enabled: bool) -> None:
    """切换规则启用/禁用"""
    await conn.execute(
        "UPDATE rules SET enabled=? WHERE id=?", (1 if enabled else 0, rule_id)
    )
    await conn.commit()


async def delete_rule(conn, rule_id: str) -> None:
    """删除规则"""
    await conn.execute("DELETE FROM rules WHERE id=?", (rule_id,))
    await conn.commit()
