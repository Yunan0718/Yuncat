"""services/memory/tool_result_pruner.py —— tool 结果裁剪

对标 DSH `packages/compaction/compaction-tool-result-pruner`：

- 超预算的 tool 结果换成「头 + 中间裁剪标记 + 尾」——
  头保留命令/报错开头，尾保留结论（工具输出常在末尾给结论）；
- 裁剪**不调模型**；裁完压力够了就不必再做一次摘要（省一次 LLM 调用）；
- 原文留在会话日志里（写进 task_logs 的 tool_pruned 记录），可回放核对。

字符预算只是 token 的粗估（4 字符≈1 token），DSH 也如此；
真正是否解除了压力，由调用方用真实 token 数判断。
"""
from __future__ import annotations

import logging
import time as _time

_log = logging.getLogger("yuncat.compact.prune")

PRUNED_MARKER = "\n... [tool output pruned: {total} chars, showing {head} head + {tail} tail] ...\n"

DEFAULT_THRESHOLD_CHARS = 4000
DEFAULT_HEAD_CHARS = 1500
DEFAULT_TAIL_CHARS = 800


def _is_tool_message(msg) -> bool:
    return isinstance(msg, dict) and msg.get("role") == "tool"


def prune_tool_results(
    messages: list[dict],
    *,
    threshold_chars: int = DEFAULT_THRESHOLD_CHARS,
    head_chars: int = DEFAULT_HEAD_CHARS,
    tail_chars: int = DEFAULT_TAIL_CHARS,
) -> tuple[list[dict], int, dict[str, str]]:
    """裁剪超预算的 tool 结果。

    Returns: (裁剪后的消息列表, 省下的字符数, {消息 id: 原文})
    """
    pruned: list[dict] = []
    originals: dict[str, str] = {}
    saved = 0
    for msg in messages:
        if not _is_tool_message(msg):
            pruned.append(msg)
            continue
        content = str(msg.get("content") or "")
        if len(content) <= threshold_chars:
            pruned.append(msg)
            continue
        head = content[:head_chars]
        tail = content[-tail_chars:] if tail_chars > 0 else ""
        new_content = head + PRUNED_MARKER.format(
            total=len(content), head=head_chars, tail=tail_chars) + tail
        new_msg = dict(msg)
        new_msg["content"] = new_content
        new_msg["pruned_from_chars"] = len(content)
        pruned.append(new_msg)
        saved += len(content) - len(new_content)
        msg_id = str(msg.get("id") or "")
        if msg_id:
            originals[msg_id] = content
    return pruned, saved, originals


def estimate_saved_tokens(saved_chars: int) -> int:
    """字符→token 粗估（4 字符≈1 token）。"""
    return max(0, int(saved_chars / 4))


def prune_budgets_from_settings() -> dict:
    """从 settings 读裁剪预算（可用 env 覆盖）。"""
    from settings import settings as _settings
    return {
        "threshold_chars": int(_settings.tool_result_prune_threshold_chars),
        "head_chars": int(_settings.tool_result_prune_head_chars),
        "tail_chars": int(_settings.tool_result_prune_tail_chars),
    }


async def persist_pruned_tool_results(
    conn, session_id: str, pruned: list[dict], originals: dict[str, str],
) -> int:
    """把裁剪后的内容写回 conversations；原文留档到 task_logs。

    这样"模型看到的是裁剪版、日志里留的是原文"——对标 DSH 的
    "complete original remains in the session log"。
    """
    if not conn or not originals:
        return 0
    written = 0
    try:
        for msg in pruned:
            msg_id = str(msg.get("id") or "")
            if msg_id not in originals:
                continue
            await conn.execute(
                "UPDATE conversations SET content=? WHERE id=?",
                (str(msg.get("content") or ""), msg_id),
            )
            written += 1
        for msg_id, original in originals.items():
            await conn.execute(
                "INSERT INTO compaction_artifacts(session_id, kind, ref_id, content, created_at) "
                "VALUES(?,?,?,?,?)",
                (session_id, "tool_pruned", msg_id, original[:20000], int(_time.time())),
            )
        await conn.commit()
    except Exception:
        _log.warning("tool 结果裁剪落库失败（不影响本轮压缩）", exc_info=True)
        return 0
    return written
