"""services/event_log.py — Event Sourcing 事件日志 (Phase OH.1)

对标 OpenHands: 所有操作以不可变事件流持久化为 JSONL。
可重放·可恢复·可时间旅行调试·可审计。

Phase OH.5: 从 EventLog 重建 context["history"] + 恢复 tool_loop 状态。
"""
import json as _json
import logging
import time as _time
from pathlib import Path as _Path

from config import DATA_DIR, calc_context_budget
from config import EVENT_LOG_CONTEXT_SLICE_SIZE  # Phase 5 P2: 上下文切片条数统一管理

_log = logging.getLogger("yuncat.event_log")

# 数据目录只有一处真源（config.DATA_DIR ← settings.get_data_dir，含未展开变量校验）
EVENT_LOG_DIR = _Path(DATA_DIR) / "events"


class EventLog:
    """不可变事件流——每次工具调用/结果/思考都追加一行 JSONL。"""

    @staticmethod
    def _log_path(task_id: str) -> _Path:
        return EVENT_LOG_DIR / f"{task_id}.jsonl"

    @staticmethod
    async def append(
        task_id: str, event_type: str,
        agent_id: str = "", tool_name: str = "",
        payload: dict | None = None,
        seq: int = 0,
    ) -> None:
        """追加密不可变事件到 JSONL 文件。"""
        try:
            EVENT_LOG_DIR.mkdir(parents=True, exist_ok=True)
            entry = {
                "seq": seq,
                "type": event_type,
                "agent_id": agent_id,
                "tool_name": tool_name,
                "payload": payload or {},
                "timestamp": int(_time.time()),
            }
            with open(EventLog._log_path(task_id), "a", encoding="utf-8") as f:
                f.write(_json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            _log.debug("EventLog append failed for %s", task_id[:8])

    @staticmethod
    async def replay(task_id: str) -> list[dict]:
        """重放事件——从 JSONL 重建完整历史。用于崩溃恢复和审计。"""
        path = EventLog._log_path(task_id)
        if not path.exists():
            return []
        events = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(_json.loads(line))
                    except _json.JSONDecodeError:
                        continue
        except OSError:
            pass
        return events

    @staticmethod
    async def slice_for_context(
        task_id: str, max_events: int = EVENT_LOG_CONTEXT_SLICE_SIZE,
    ) -> list[dict]:
        """取最近 N 个事件——用于恢复 context['history']。"""
        events = await EventLog.replay(task_id)
        return events[-max_events:]

    @staticmethod
    async def rebuild_history(task_id: str, model_cfg: dict | None = None) -> list[dict]:
        """从事件流重建对话历史——对标 OpenHands EventStream → messages。

        将 tool_call/tool_result/text 事件转换为 OpenAI 格式的 messages 列表。
        Phase 56-P2: content 预算按模型窗口动态计算。
        """
        events = await EventLog.slice_for_context(task_id, max_events=200)
        _content_budget = calc_context_budget(model_cfg, fraction=0.002, min_chars=200, max_chars=800)
        messages: list[dict] = []
        for e in events:
            et = e["type"]
            if et == "text":
                messages.append({"role": "assistant", "content": e["payload"].get("text", "")[:_content_budget]})
            elif et == "tool_call":
                messages.append({
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": f"ev_{e['seq']}",
                        "type": "function",
                        "function": {
                            "name": e.get("tool_name", ""),
                            "arguments": _json.dumps(e.get("payload", {})),
                        },
                    }],
                })
            elif et == "tool_result":
                messages.append({
                    "role": "tool",
                    "tool_call_id": f"ev_{e['seq']}",
                    "content": _json.dumps(e.get("payload", {}), ensure_ascii=False)[:_content_budget],
                })
        return messages

    @staticmethod
    async def purge(task_id: str) -> None:
        """清理事件日志（对话结束或用户主动清理）。"""
        path = EventLog._log_path(task_id)
        try:
            if path.exists():
                path.unlink()
        except OSError as e:
            # Phase 48 T3-15: 审计日志删除失败必上报（原 except: pass 静默吞咽 → 日志残留无日志）
            _log.warning("EventLog delete failed (%s): %s", path, e)
