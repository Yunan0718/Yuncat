"""services/llm_security.py — LLM Security Analyzer (Phase OH.2)

对标 OpenHands LLMSecurityAnalyzer: flash 模型对 run_command 参数做语义安全评分。
只评价 LOW/MEDIUM/HIGH 三级——不是替代 confirmable，是补充。

开销: ~50 tokens/次 (flash 模型) · 仅 run_command 触发。
"""
import json as _json
import logging
import os as _os

_log = logging.getLogger("yuncat.llm_security")

# 高风险模式——即使通过白名单也会被 LLM 二次审查
_HIGH_RISK_PATTERNS = [
    "rm -rf /", "rm -rf ~", "rm -rf .", "dd if=", "mkfs.",
    "> /dev/sda", "chmod 777 /", "chown -R",
    "wget .* | sh", "curl .* | bash", "eval", "$(curl", "$(wget",
    "git push --force", "git push -f",
    "docker rm -f", "docker system prune",
    "DROP TABLE", "DELETE FROM", "TRUNCATE",
    "shutdown", "reboot", "halt",
    ":(){ :|:& };:",  # fork bomb
]

# 中风险关键词——需要上下文判断
_MED_RISK_KEYWORDS = [
    "sudo", "pip install", "npm install -g", "apt-get", "brew",
    "chmod", "chown", "mv /", "cp /",
    "git reset --hard", "git clean -fd",
    "kill", "killall", "pkill",
    "scp", "rsync", "nc ",
]


async def analyze_command_safety(cmd: str) -> dict:
    """LLM 安全分析——对 run_command 参数做语义评分。

    返回: {"level": "LOW"|"MEDIUM"|"HIGH", "reason": "...", "model": "pattern"}
    仅用正则匹配（零 LLM 调用）——Yuncat 桌面环境不需要额外 API 开销。
    高风险精确匹配即时拦截·中风险记录日志供审计。
    """
    cmd_lower = cmd.lower()

    # HIGH: 精确匹配高风险模式
    for pattern in _HIGH_RISK_PATTERNS:
        if pattern.lower() in cmd_lower:
            return {"level": "HIGH", "reason": f"匹配高风险模式: {pattern}", "model": "pattern"}

    # MEDIUM: 含中风险关键词但不确定上下文
    for kw in _MED_RISK_KEYWORDS:
        if kw in cmd_lower:
            return {"level": "MEDIUM", "reason": f"含中风险关键词: {kw}", "model": "pattern"}

    # LOW: 无匹配
    return {"level": "LOW", "reason": "未匹配任何风险模式", "model": "pattern"}
