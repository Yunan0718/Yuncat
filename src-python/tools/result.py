"""tools/result.py —— v56 统一工具返回格式（借鉴 agent-harness-construction）

Phase 3: 所有工具统一返回 ToolResult，Agent 从 observation 获取结构化反馈。
"""
from __future__ import annotations

import re as _re
from dataclasses import dataclass, field
from typing import Any


# P2-2.2: 工具结果摘要魔数提取为模块常量（对标 Claude Code tool result summary）
# 这些是行数/计数上限，与字符级 calc_tool_result_limit 互补
_RESULT_SUMMARY_HEAD_LINES = 15          # 头部预览行数
_RESULT_SUMMARY_TAIL_LINES = 15          # 尾部预览行数（total > MAX_TOTAL 时附加）
_RESULT_SUMMARY_MAX_TOTAL = 30           # 触发尾部预览的总行数阈值
_RESULT_SUMMARY_ERROR_LINES_CAP = 20     # 错误/警告行去重后保留上限
_RESULT_SUMMARY_PER_FILE_CAP = 5         # 搜索结果每文件匹配行上限
_RESULT_SUMMARY_NO_GROUP_CAP = 20        # 搜索结果无文件分组的行上限


@dataclass
class ToolResult:
    """统一工具返回格式——所有 8 个内置工具以此为契约。

    observation 字段告诉 Agent "下一步可以做什么"，而非仅描述发生了什么。
    每条 observation ≤ 200 字。
    """
    success: bool
    data: Any = None
    observation: str = ""
    error: str = ""
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """转为 API 友好的 dict（向前兼容旧格式）"""
        d = {
            "success": self.success,
            "observation": self.observation,
            "meta": self.meta,
        }
        if self.success:
            d["data"] = self.data
        else:
            d["error"] = self.error
        return d


# ============================================================
# observation 生成规则
# ============================================================

def gen_read_observation(path: str, truncated: bool, size_bytes: int) -> str:
    """read_file 的 observation——告诉 Agent 文件大小和截断状态"""
    if truncated:
        return (
            f"文件 {path} 已读取（{size_bytes} bytes，已截断至前 50KB）。"
            f"若需完整内容，可使用 read_file 分段读取或 grep 搜索关键字。"
        )
    return f"文件 {path} 已读取（{size_bytes} bytes）。可继续分析或修改此文件。"


def gen_read_error_observation(path: str, error: str) -> str:
    if "不存在" in error:
        return f"文件 {path} 不存在。请检查路径是否正确，或使用 list_dir 查看目录内容。"
    if "目录" in error:
        return f"{path} 是目录而非文件。请使用 list_dir 查看其内容，或指定具体文件路径。"
    return f"读取 {path} 失败：{error}。请检查文件权限或路径。"


def gen_write_observation(path: str, bytes_written: int) -> str:
    return (
        f"文件 {path} 写入预览（{bytes_written} bytes）——等待用户确认。"
        f"确认后将实际写入文件。"
    )


def gen_write_error_observation(path: str, error: str) -> str:
    if "占用" in error or "Permission" in error:
        return (
            f"写入 {path} 失败：文件被占用。请关闭占用程序后重试，"
            f"或使用 write_file 写入到其他路径。"
        )
    return f"写入 {path} 失败：{error}。请检查磁盘空间和目录权限。"


def gen_list_observation(path: str, count: int) -> str:
    if count == 0:
        return f"目录 {path} 为空。可以创建新文件或子目录。"
    return f"目录 {path} 包含 {count} 个条目。可使用 read_file 查看具体文件内容。"


def gen_run_preview_observation(command: str) -> str:
    return (
        f"即将执行: {command}。请确认执行。"
        f"如有疑问可取消后修改命令参数。"
    )


def gen_run_blocked_observation(command: str, reason: str) -> str:
    return (
        f"命令 {command} 被拒绝：{reason}。"
        f"请使用 write_file 生成脚本文件后通过 run_command 执行，"
        f"或使用白名单中的安全命令。"
    )


def gen_run_executed_observation(command: str, output_len: int) -> str:
    return (
        f"命令 {command} 已执行（输出 {output_len} 字符）。"
        f"可分析输出结果，或继续执行后续操作。"
    )


def gen_run_error_observation(command: str, error: str) -> str:
    if "超时" in error or "timeout" in error.lower():
        return (
            f"命令 {command} 执行超时。请检查是否有死循环或网络阻塞，"
            f"可增加 timeout_sec 参数或拆分为多个小命令。"
        )
    return f"命令 {command} 执行失败：{error}。请检查命令语法和工作区路径。"


def gen_generic_error_observation(tool_name: str, error: str) -> str:
    return f"{tool_name} 执行失败：{error}。请检查参数后重试，或尝试其他替代方案。"


# ============================================================
# G2: 工具结果结构感知摘要 —— summarize_for_llm
# ============================================================
# 对标 Claude Code 工具结果摘要：长输出压缩为 LLM 友好的结构化预览，
# 保留结构（签名/错误/统计/分组）而非简单 head+tail。静态规则，0 API 成本。
#
# 修复现有 _execute_exclusive_tools 截断路径的缺陷：
#   现状 = protect_critical_info(head+tail) → 仅保留 error/def/class/import 行，
#          对正常输出经常返回空，丢失正文结构，LLM 无法推理。
#   G2  = summarize_for_llm → 按工具类型保留结构。

_CODE_SIG_RE = _re.compile(r'^\s*(import |from .+ import |async def |def |class |@)')
_ERROR_LINE_RE = _re.compile(r'(error|fail(ed|ure)?|traceback|exception|warning|warn\b|fatal)', _re.IGNORECASE)

# 工具分类（按 registry 实际 name 归集）
_CODE_TOOLS = frozenset({"read_file", "edit_file", "diff", "codegraph_context", "write_file", "delete_file"})
_COMMAND_TOOLS = frozenset({"run_command", "open_shell", "close_shell", "execute_command", "git"})
_SEARCH_TOOLS = frozenset({"search_files", "grep", "list_dir"})


def summarize_for_llm(tool_name: str, content: str, max_chars: int) -> str:
    """G2: 结构感知的工具结果摘要——长输出压缩为 LLM 友好结构化预览。

    短内容（≤ max_chars）原样返回。超长内容按工具类型生成结构化摘要：
    - 代码类（read_file/edit_file/diff）: imports + 函数/类签名，丢弃函数体
    - 命令类（run_command/open_shell）: 首 N + 尾 N + 所有 error/warning + 行数统计
    - 搜索类（search_files/grep）: 按文件分组去重，每文件最多 5 行
    - 通用: head + tail + 行数/字符数统计

    返回值长度保证 ≤ max_chars（硬性约束）。
    """
    if not content or len(content) <= max_chars:
        return content
    if tool_name in _CODE_TOOLS:
        summary = _summarize_code(content)
    elif tool_name in _COMMAND_TOOLS:
        summary = _summarize_command(content)
    elif tool_name in _SEARCH_TOOLS:
        summary = _summarize_search(content)
    else:
        summary = _summarize_generic(content)
    return _enforce_budget(summary, max_chars)


def _enforce_budget(summary: str, max_chars: int) -> str:
    """硬性预算约束——超限时截断并附加提示。"""
    if len(summary) <= max_chars:
        return summary
    notice = f"\n[...truncated: {max_chars} chars]"
    if len(notice) >= max_chars:
        return summary[:max_chars]  # 预算极小，连提示都放不下
    return summary[:max_chars - len(notice)] + notice


def _summarize_code(content: str) -> str:
    """代码摘要：保留 imports + 函数/类签名 + 装饰器 + 首尾实现上下文。

    Task 7.12 (F-048): 原实现仅保留签名丢弃全部函数体——Agent 需理解实现逻辑时
    压缩结果完全无信息。改为 signatures + head + tail 三段式，对标 Claude Code：
    - signatures 段给结构总览（imports/def/class/decorator）
    - head 段给首部实现上下文（首个函数体开头）
    - tail 段给尾部实现上下文（末尾函数体结尾 / main 块）
    三段互补：签名回答"有哪些定义"，head/tail 回答"实现长什么样"。
    """
    lines = content.split("\n")
    total = len(lines)
    kept_sigs = [ln.rstrip() for ln in lines if _CODE_SIG_RE.match(ln)]
    parts = [f"[code structure: {len(kept_sigs)} signatures from {total} lines]"]
    # 1. 签名段——结构总览
    if kept_sigs:
        parts.append("--- signatures ---")
        parts.extend(kept_sigs)
    # 2. head 段——首部实现上下文（Task 7.12: 对标 Claude Code head+tail）
    parts.append("--- head ---")
    parts.extend(lines[:_RESULT_SUMMARY_HEAD_LINES])
    # 3. tail 段——尾部实现上下文（total > 阈值时附加）
    if total > _RESULT_SUMMARY_MAX_TOTAL:
        omitted = total - _RESULT_SUMMARY_HEAD_LINES - _RESULT_SUMMARY_TAIL_LINES
        if omitted > 0:
            parts.append(f"[...{omitted} lines omitted...]")
        parts.append("--- tail ---")
        parts.extend(lines[-_RESULT_SUMMARY_TAIL_LINES:])
    return "\n".join(parts)


def _summarize_command(content: str) -> str:
    """命令摘要：首 15 + 尾 15 + 错误行（去重，最多 20）+ 行数统计。"""
    lines = content.split("\n")
    total = len(lines)
    # 错误/警告行去重
    seen: set[str] = set()
    error_lines: list[str] = []
    for ln in lines:
        if _ERROR_LINE_RE.search(ln) and ln not in seen:
            seen.add(ln)
            error_lines.append(ln)
            if len(error_lines) >= _RESULT_SUMMARY_ERROR_LINES_CAP:
                break
    head = lines[:_RESULT_SUMMARY_HEAD_LINES]
    parts = [f"[command output: {total} lines]", "--- head ---"]
    parts.extend(head)
    if error_lines:
        parts.append("--- errors/warnings ---")
        parts.extend(error_lines)
    if total > _RESULT_SUMMARY_MAX_TOTAL:
        omitted = total - _RESULT_SUMMARY_MAX_TOTAL
        parts.append(f"[...{omitted} lines omitted...]")
        parts.append("--- tail ---")
        parts.extend(lines[-_RESULT_SUMMARY_TAIL_LINES:])
    return "\n".join(parts)


def _summarize_search(content: str) -> str:
    """搜索摘要：按文件分组（filepath:lineno:content 格式），每文件最多 5 行。"""
    lines = content.split("\n")
    groups: dict[str, list[str]] = {}
    no_group: list[str] = []
    for ln in lines:
        idx = ln.find(":")
        if idx > 0:
            filepath = ln[:idx]
            # 验证像路径：含 . 或 / 或 \ 且无空格
            looks_path = (("." in filepath) or ("/" in filepath) or ("\\" in filepath)) and " " not in filepath
            if looks_path:
                groups.setdefault(filepath, []).append(ln)
                continue
        no_group.append(ln)
    per_file_cap = _RESULT_SUMMARY_PER_FILE_CAP
    parts = [f"[search results: {len(lines)} matches in {len(groups)} files]"]
    for filepath, matches in groups.items():
        if len(matches) > per_file_cap:
            parts.append(f"--- {filepath} ({len(matches)} matches, showing {per_file_cap}) ---")
            parts.extend(matches[:per_file_cap])
        else:
            parts.append(f"--- {filepath} ---")
            parts.extend(matches)
    if no_group:
        parts.append("--- other ---")
        parts.extend(no_group[:_RESULT_SUMMARY_NO_GROUP_CAP])
    return "\n".join(parts)


def _summarize_generic(content: str) -> str:
    """通用摘要：head 15 + tail 15 + 行数/字符数统计。"""
    lines = content.split("\n")
    total = len(lines)
    parts = [f"[output: {total} lines, {len(content)} chars]"]
    parts.extend(lines[:_RESULT_SUMMARY_HEAD_LINES])
    if total > _RESULT_SUMMARY_MAX_TOTAL:
        parts.append(f"[...{total - _RESULT_SUMMARY_MAX_TOTAL} lines omitted...]")
        parts.extend(lines[-_RESULT_SUMMARY_TAIL_LINES:])
    return "\n".join(parts)
