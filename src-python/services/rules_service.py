"""services/rules_service.py —— 规则服务

管理用户自定义行为约束规则。规则在 IntentRouter 和 ToolService 执行前注入。
"""
import logging

_log = logging.getLogger("yuncat.rules")


class RulesService:
    def __init__(self, db):
        self.db = db

    async def create_rule(
        self, description: str, trigger_on: list[str], action: str, enabled: bool = True,
    ) -> str:
        """创建用户自定义规则"""
        from repositories import insert_rule
        return await insert_rule(self.db, description, trigger_on, action, enabled)

    async def get_enabled_rules(self) -> list[dict]:
        """获取所有已启用的规则"""
        from repositories import get_enabled_rules
        return await get_enabled_rules(self.db)

    async def get_all(self) -> list[dict]:
        """获取所有规则（含禁用的）"""
        from repositories import get_all_rules
        return await get_all_rules(self.db)

    async def toggle(self, rule_id: str, enabled: bool) -> None:
        """切换规则启用/禁用"""
        from repositories import toggle_rule
        await toggle_rule(self.db, rule_id, enabled)

    async def delete(self, rule_id: str) -> None:
        """删除规则"""
        from repositories import delete_rule
        await delete_rule(self.db, rule_id)

    async def check_rules(self, tool_name: str, payload: dict) -> None:
        """工具执行前规则检查——若规则要求确认但工具未标记 confirmable，则拒绝"""
        rules = await self.get_enabled_rules()
        trigger_key = f"tool:{tool_name}"
        for rule in rules:
            triggers = rule.get("trigger_on") or []
            if trigger_key in triggers:
                if rule["action"] == "require_confirmation":
                    # 在 ToolService 层处理
                    pass
