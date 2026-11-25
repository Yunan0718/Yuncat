"""services/prompt_library.py —— Phase 72: 统一 PromptLibrary

聚合 5 个数据源为统一目录：
1. DB roles 表（roles_repo.list_roles/get_role）——用户可编辑，DB 优先
2. YAML roles（prompts/roles/*.yaml）——内置，DB 无同 id 时补充
3. YAML behaviors（prompts/behaviors/{general,python,golang,typescript}.yaml）——flat mapping
4. Workflow templates（prompts/workflow_templates.BUILTIN_TEMPLATES）——23 个 DAG 模板
5. Python prompt 模块（prompts/*.py）——由 PromptService 管理，此处仅编目

去重规则（方案 §Phase 72）：
- DB 角色与 YAML 角色同 id 时，DB 版本优先（允许用户覆盖内置）
- YAML 新增角色 id 未在 DB 存在时，启动时自动种子到 DB（source='builtin'）——见 lifespan._seed_yaml_roles_to_db
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from repositories.roles_repo import list_roles as _list_db_roles, get_role as _get_db_role
from prompts.workflow_templates import BUILTIN_TEMPLATES

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_YAML_ROLES_DIR = _PROMPTS_DIR / "roles"
_BEHAVIORS_DIR = _PROMPTS_DIR / "behaviors"

# flat mapping 结构的 behavior 文件（key: prompt_text）——hermes.yaml/openclaw.yaml 是 agent 规则，不纳入
_BEHAVIOR_FILES = ["general.yaml", "python.yaml", "golang.yaml", "typescript.yaml"]


class PromptLibrary:
    """统一 Prompt 目录服务——聚合 DB + YAML + 内置模板"""

    def __init__(self, conn):
        self._conn = conn

    # ── 目录 ──

    async def catalog(self) -> dict:
        """返回分类目录 + 各类计数"""
        roles = await self.list_roles()
        behaviors = self.list_behaviors()
        workflows = self.list_workflows()
        return {
            "categories": [
                {"id": "roles", "name": "角色", "count": len(roles)},
                {"id": "behaviors", "name": "行为约束", "count": len(behaviors)},
                {"id": "workflows", "name": "工作流模板", "count": len(workflows)},
            ]
        }

    # ── 角色（DB + YAML 合并，DB 优先）──

    async def list_roles(self) -> list[dict]:
        """列出所有角色——DB 优先，YAML 补充（同 id 去重）"""
        db_roles = await _list_db_roles(self._conn)
        db_ids = {r["id"] for r in db_roles}
        merged = list(db_roles)
        for yr in self._load_yaml_roles():
            if yr["id"] not in db_ids:
                merged.append(yr)
        return merged

    async def get_role(self, role_id: str) -> dict | None:
        """获取角色——DB 优先，YAML 回退"""
        db_role = await _get_db_role(self._conn, role_id)
        if db_role:
            return db_role
        for yr in self._load_yaml_roles():
            if yr["id"] == role_id:
                return yr
        return None

    # ── 行为约束（YAML flat mapping）──

    def list_behaviors(self) -> list[dict]:
        """列出所有行为约束"""
        return self._load_all_behaviors()

    def get_behavior(self, behavior_id: str) -> dict | None:
        for b in self._load_all_behaviors():
            if b["id"] == behavior_id:
                return b
        return None

    # ── 工作流模板（BUILTIN_TEMPLATES）──

    def list_workflows(self) -> list[dict]:
        """列出工作流模板（仅元数据，不含 graph）"""
        return [
            {
                "id": t["name"],
                "type": "workflow",
                "category": "workflows",
                "name": t.get("display_name", t["name"]),
                "description": t.get("description", ""),
                "source": "builtin",
                "is_builtin": True,
            }
            for t in BUILTIN_TEMPLATES
        ]

    def get_workflow_template(self, name: str) -> dict | None:
        """获取工作流模板（含 graph 完整定义）"""
        for t in BUILTIN_TEMPLATES:
            if t["name"] == name:
                return t
        return None

    # ── 跨类型搜索 ──

    async def search(self, q: str = "", category: str = "", type: str = "") -> list[dict]:
        """全文搜索——跨类型过滤

        Args:
            q: 搜索关键词（大小写不敏感，匹配 name/description/id/content）
            category: 分类过滤（roles/behaviors/workflows）
            type: 类型过滤（role/behavior/workflow）
        """
        q_lower = q.lower().strip()
        results: list[dict] = []

        if (not type or type == "role") and (not category or category == "roles"):
            for r in await self.list_roles():
                text = f"{r.get('name', '')} {r.get('description', '')} {r.get('id', '')}".lower()
                if not q_lower or q_lower in text:
                    results.append({
                        "id": r["id"],
                        "type": "role",
                        "category": "roles",
                        "name": r.get("name", r["id"]),
                        "description": r.get("description", ""),
                        "source": r.get("source", "db"),
                        "is_builtin": r.get("is_builtin", False),
                    })

        if (not type or type == "behavior") and (not category or category == "behaviors"):
            for b in self.list_behaviors():
                text = f"{b.get('name', '')} {b.get('content', '')} {b.get('id', '')}".lower()
                if not q_lower or q_lower in text:
                    results.append(b)

        if (not type or type == "workflow") and (not category or category == "workflows"):
            for w in self.list_workflows():
                text = f"{w.get('name', '')} {w.get('description', '')} {w.get('id', '')}".lower()
                if not q_lower or q_lower in text:
                    results.append(w)

        return results

    # ── YAML 加载（内部）──

    @staticmethod
    def _load_yaml_roles() -> list[dict]:
        """加载 YAML 角色文件（prompts/roles/*.yaml）"""
        roles: list[dict] = []
        if not _YAML_ROLES_DIR.exists():
            return roles
        for f in sorted(_YAML_ROLES_DIR.glob("*.yaml")):
            try:
                with open(f, encoding="utf-8") as fh:
                    data = yaml.safe_load(fh)
                if not isinstance(data, dict) or "id" not in data:
                    continue
                roles.append({
                    "id": data["id"],
                    "name": data.get("name", data["id"]),
                    "category": "yaml",
                    "prompt_template": data.get("prompt_template", ""),
                    "description": data.get("description", ""),
                    "enabled": True,
                    "is_builtin": True,
                    "source": "yaml",
                    "agent": data.get("agent", ""),
                })
            except Exception:
                continue
        return roles

    @staticmethod
    def _load_all_behaviors() -> list[dict]:
        """加载所有行为约束（flat mapping 文件）"""
        behaviors: list[dict] = []
        for fname in _BEHAVIOR_FILES:
            fpath = _BEHAVIORS_DIR / fname
            if not fpath.exists():
                continue
            try:
                with open(fpath, encoding="utf-8") as fh:
                    data = yaml.safe_load(fh)
                if not isinstance(data, dict):
                    continue
                lang = fname.replace(".yaml", "")
                for key, content in data.items():
                    if not isinstance(content, str):
                        continue
                    # 描述取首行（去掉 # 注释和 ## 标题标记）
                    first_line = content.split("\n")[0].lstrip("# ").strip()
                    behaviors.append({
                        "id": key,
                        "type": "behavior",
                        "category": "behaviors",
                        "name": key.replace("-", " ").title(),
                        "description": first_line,
                        "content": content,
                        "language": lang,
                        "source": "builtin",
                        "is_builtin": True,
                    })
            except Exception:
                continue
        return behaviors


async def seed_yaml_roles_to_db(conn) -> int:
    """启动时将 YAML 角色种子到 DB（幂等——同 id 跳过，source='builtin'）

    方案去重规则：YAML 新增角色 id 未在 DB 存在时，自动种子到 DB。
    Returns: 实际插入的条数
    """
    import time as _time
    yaml_roles = PromptLibrary._load_yaml_roles()
    if not yaml_roles:
        return 0
    inserted = 0
    now = int(_time.time())
    for yr in yaml_roles:
        # 检查 DB 是否已有同 id
        cursor = await conn.execute("SELECT id FROM roles WHERE id=?", (yr["id"],))
        if await cursor.fetchone():
            continue  # DB 已有——DB 优先，跳过
        await conn.execute(
            "INSERT INTO roles(id, name, category, prompt_template, description, "
            "enabled, is_builtin, source, created_at, updated_at) "
            "VALUES(?,?,?,?,?,1,1,'builtin',?,?)",
            (yr["id"], yr["name"], yr["category"], yr["prompt_template"],
             yr["description"], now, now),
        )
        inserted += 1
    if inserted:
        await conn.commit()
    return inserted
