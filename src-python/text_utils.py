"""src-python/text_utils.py —— Task 6.15 文本截断 + 溯源指针工具

所有截断处统一加 `... [see node {id} for full content]` 溯源指针，
让 LLM / 用户知道截断内容在哪个 node 可查（plan_steps.id / knowledge_nodes.id）。

主流做法对标：
  - Claude Code：长输出截断时附 "truncated, see <ref>" 提示
  - IDE hover preview：截断时附 "..." + 可点击展开
  - 本模块：node_id 可选，None 时不加指针仅截断（日志/纯展示场景向后兼容）

设计决策：
  - max_chars 指「内容字符预算」（不含指针），指针在截断内容后追加
  - len(text) <= max_chars 时原样返回（不截断不加指针）
  - max_chars <= 0 视为无限制（返回原文）——防御性，避免误传 0 导致空串
  - node_id 为 falsy（None / ""）时不加指针，仅 "..."（向后兼容日志场景）

CodeWhale 借鉴 (#15): write_atomic() 原子写工具——temp + fsync + os.replace。
用于非 SQL 状态文件（通知、checkpoint、配置等）防中途崩溃损坏。
"""
from __future__ import annotations

import os
import tempfile

_POINTER_TEMPLATE = "... [see node {node_id} for full content]"
_PLAIN_ELLIPSIS = "..."


def truncate_with_pointer(
    text: str | None,
    max_chars: int,
    node_id: str | None = None,
) -> str:
    """截断文本并附溯源指针

    Args:
        text: 原始文本（None 视为空串）
        max_chars: 内容字符预算（不含指针）；text 长度 <= max_chars 时原样返回。
                   max_chars <= 0 视为无限制（返回原文）。
        node_id: 溯源节点 id（plan_steps.id / knowledge_nodes.id）。
                 为 falsy 时不加指针，仅截断 + "..."（向后兼容日志场景）。

    Returns:
        未超长 → 原文
        超长 + node_id → text[:max_chars] + "... [see node {node_id} for full content]"
        超长 + 无 node_id → text[:max_chars] + "..."
    """
    if text is None:
        return ""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    if node_id:
        return f"{truncated}{_POINTER_TEMPLATE.format(node_id=node_id)}"
    return f"{truncated}{_PLAIN_ELLIPSIS}"


# ============================================================
# CodeWhale 借鉴 #15: 原子写工具
# ============================================================

def write_atomic(path: str, content: str, encoding: str = "utf-8") -> None:
    """原子写入文件——temp + fsync + os.replace。

    对标 CodeWhale write_atomic：先写临时文件并 fsync，再 os.replace 原子重命名。
    崩溃时残留 .tmp（由调用方或后续清理），不会损坏目标文件。

    适用于非 SQL 状态文件（通知 JSON、checkpoint 元数据、配置导出等）。
    SQLite 表数据已有 WAL + 事务保护，无需此函数。

    Args:
        path: 目标文件路径
        content: 写入内容
        encoding: 文本编码（默认 utf-8）

    Raises:
        OSError: 写入或重命名失败（临时文件会被清理）
    """
    import os as _os
    import tempfile as _tempfile

    parent = _os.path.dirname(path)
    if parent:
        _os.makedirs(parent, exist_ok=True)
    fd, tmp = _tempfile.mkstemp(dir=parent or ".", suffix=".tmp")
    try:
        with _os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            _os.fsync(f.fileno())
        _os.replace(tmp, path)
    except Exception:
        try:
            _os.unlink(tmp)
        except OSError:
            pass
        raise
