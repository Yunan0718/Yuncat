"""prompts/rules_formatter.py —— P2: Rules → 自然语言上下文注入"""
__version__ = "1.0.0"


def format_rules_as_context(rules: list[dict]) -> str:
    """将规则列表格式化为注入上下文的中文文本"""
    if not rules:
        return ""
    lines = ["## ⚠ 行为约束（必须遵守）"]
    for i, rule in enumerate(rules, 1):
        if rule.get("enabled", True):
            lines.append(f"{i}. {rule['description']}")
    return "\n".join(lines)
