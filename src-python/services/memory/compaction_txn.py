"""压缩事务：稳定性校验、并发追加保留与 checkpoint 提交。"""
from dataclasses import dataclass
import hashlib
import json
import logging
import time
import uuid

_log = logging.getLogger("yuncat.memory.compaction")


def _signature(message: dict) -> str:
    payload = {
        key: message.get(key)
        for key in ("id", "role", "content", "tool_calls", "tool_call_id", "reasoning_content")
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CompactionSnapshot:
    signatures: tuple[str, ...]
    source_message_ids: tuple[str, ...]

    def unchanged_prefix(self, current: list[dict]) -> bool:
        count = len(self.signatures)
        return count <= len(current) and tuple(
            _signature(message) for message in current[:count]
        ) == self.signatures


def capture_snapshot(messages: list[dict], compacted_count: int) -> CompactionSnapshot:
    if compacted_count < 0 or compacted_count > len(messages):
        raise ValueError("compacted_count outside session message range")
    source_ids = [
        str(message["id"]) for message in messages[:compacted_count]
        if message.get("id") and not str(message["id"]).startswith("compact:")
    ]
    return CompactionSnapshot(
        signatures=tuple(_signature(message) for message in messages),
        source_message_ids=tuple(source_ids),
    )


async def apply_compaction_transaction(
    service, session_id: str, snapshot: CompactionSnapshot,
    summary: dict, summary_text: str, recent: list[dict],
) -> bool:
    """在会话锁内复核快照；保留摘要生成期间追加的新消息。"""
    checkpoint_id = uuid.uuid4().hex
    summary_message = {
        "id": f"compact:{checkpoint_id}", "role": "system", "content": summary_text,
    }
    async with await service._get_session_lock(session_id):
        current = list(service._sessions.get(session_id, []))
        if not snapshot.unchanged_prefix(current):
            _log.warning("Compaction aborted for %s: session changed during summarization", session_id[:8])
            return False
        appended = current[len(snapshot.signatures):]
        next_context = [summary_message, *recent, *appended]
        if service.db is not None:
            if not snapshot.source_message_ids:
                _log.warning("Compaction aborted for %s: no persisted source message ids", session_id[:8])
                return False
            from repositories.compaction_repo import commit_compaction_checkpoint
            from security.sandbox import get_workspace
            await commit_compaction_checkpoint(
                service.db, session_id, checkpoint_id,
                summary,
                list(snapshot.source_message_ids), str(get_workspace()),
            )
        service._sessions[session_id] = next_context
        service._session_last_access[session_id] = time.time()
    return True
