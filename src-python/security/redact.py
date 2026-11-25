"""security/redact.py —— Task 3.1: 工具结果回传 LLM 前兜底脱敏

对标:
- LangChain SecretStr（读后脱敏）
- Claude Code 在工具结果进入 LLM 上下文前过滤敏感串

设计:
- 路径 deny（system_tool_safety.is_denied_path）是主防线——读前阻止 .env / credentials 等
- 本模块是兜底——若敏感串已通过其他途径（如 read_file 读了非 deny 路径但内容含密钥，
  或 run_command 输出含 env 变量），在结果进入 history 前再扫一遍
- 永不抛异常——脱敏失败则原样返回（best-effort，不阻塞工具链）
- 幂等——对已脱敏文本再跑一次不变

调用点:
- agent_tool_loop._post_process_tool_result: result_str 进 history 前
- hooks.registry._safe_args_summary: 参数摘要进 audit log 前
- repositories.audit_repo._truncate_args: 参数摘要进 DB 前
"""
import re
import logging

_log = logging.getLogger("yuncat.security.redact")

# ============================================================
# 敏感串模式（按厂商精确匹配，避免误杀）
# ============================================================
# OpenAI: sk- 后跟 ≥20 位字母数字（实测最短 20，保守取 20）
# GitHub: ghp_/gho_/ghs_/ghr_ 后跟 36 位字母数字（官方长度）
# AWS: AKIA 后跟 16 位大写字母数字（access key ID 格式）
_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    # 厂商精确模式——统一替换为 [REDACTED]（不泄露类型，对标 LangChain SecretStr）
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "[REDACTED]"),
    (re.compile(r"ghp_[A-Za-z0-9]{36}"), "[REDACTED]"),
    (re.compile(r"gho_[A-Za-z0-9]{36}"), "[REDACTED]"),
    (re.compile(r"ghs_[A-Za-z0-9]{36}"), "[REDACTED]"),
    (re.compile(r"ghr_[A-Za-z0-9]{36}"), "[REDACTED]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED]"),
    # 通用 key=value 模式——匹配 password/passwd/secret/token/api_key/api-key/apiKey/API_KEY
    # 注意: 用 \S+ (非空白) 匹配值，避免贪婪跨行；值后跟空白/换行/引号/行尾即止
    # 保留 key 名便于调试，仅值脱敏（如 DB_PASSWORD=[REDACTED]）
    (re.compile(
        r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S+"
    ), r"\1=[REDACTED]"),
]

# 已脱敏标记——用于幂等检查（避免把 [REDACTED:xxx] 再处理）
_ALREADY_REDACTED = re.compile(r"\[REDACTED[^\]]*\]")


def redact_secrets(text) -> str:
    """对文本中的敏感串做兜底脱敏，返回脱敏后的字符串。

    - 字符串: 扫描所有模式并替换为 [REDACTED] 标记
    - None: 返回 ""
    - 非字符串: 先 str() 再扫描
    - 永不抛异常——出错返回原文本

    幂等: 对已脱敏文本再跑一次结果不变（_ALREADY_REDACTED 保护通用模式的二次处理）。
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            return ""
    if not text:
        return ""
    try:
        result = text
        for pattern, replacement in _SECRET_PATTERNS:
            # 通用 key=value 模式: 跳过已脱敏的行（含 [REDACTED] 的赋值）
            if pattern.flags & re.IGNORECASE and replacement == r"\1=[REDACTED]":
                # 逐行处理，跳过已含 [REDACTED] 的行——防幂等二次处理
                lines = result.split("\n")
                processed_lines = []
                for line in lines:
                    if "[REDACTED]" in line:
                        processed_lines.append(line)
                    else:
                        processed_lines.append(pattern.sub(replacement, line))
                result = "\n".join(processed_lines)
            else:
                result = pattern.sub(replacement, result)
        return result
    except Exception as e:
        _log.debug("redact_secrets failed (best-effort, returning original): %s", e)
        return text
