"""services/memory_sync.py —— MemoryTree ↔ MEMORY.md 双向同步桥 (Phase 41)

Phase 43 F1.5: 跨后端记忆桥——同步 OpenClaw MEMORY.md 和 Hermes Agent L4 记忆。
Yuncat MemoryTree 是枢纽——聚合双后端记忆，双向同步。
"""
import asyncio
import glob as _glob
import hashlib
import json as _json
import logging
import re
import time
from pathlib import Path

from config import (
    MEMORY_SYNC_FACTS_LIMIT,
    MEMORY_SYNC_TITLE_MAX_CHARS,
    MEMORY_SYNC_EXPORT_CONTENT_PREVIEW_CHARS,
    MEMORY_SYNC_IMPORT_CONTENT_MAX_CHARS,
    MEMORY_SYNC_INTERVAL_SECONDS,
)

_log = logging.getLogger("yuncat.memory_sync")
SYNC_INTERVAL = MEMORY_SYNC_INTERVAL_SECONDS


def _stable_id(prefix: str, key: str) -> str:
    """跨进程稳定的短哈希 ID（sha1 前 12 位）。

    替代 abs(hash(...)) % 1000000 —— Python 3.3+ 启用 PYTHONHASHSEED 随机化，
    重启进程后同一字符串 hash 不同，导致 INSERT OR REPLACE 永远插入新行而非更新。
    """
    return f"{prefix}_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]}"


class MemorySyncBridge:
    """MemoryTree ↔ MEMORY.md 双向同步。

    导出: 评分 ≥ 0.7 的知识节点 → MEMORY.md (OpenClaw 兼容格式)
    导入: MEMORY.md 变更 → knowledge_nodes
    Phase 43 F1.5: 跨后端记忆桥——OC/HA 记忆 ←→ MemoryTree 枢纽。
    """

    def __init__(self, db, memory_tree, workspace: str):
        self.db = db
        self.memory_tree = memory_tree
        self.memory_path = Path(workspace) / ".yuncat" / "MEMORY.md"
        # Phase 43 F1.5: 后端记忆路径
        self.openclaw_memory_path = (
            Path.home() / ".openclaw" / "agents" / "main" / "MEMORY.md"
        )
        self.hermes_memory_path = (
            Path.home() / ".hermes-agent" / "memory" / "L4-long-term.md"
        )
        self._last_export: float = 0.0
        self._last_import_mtime: float = 0.0
        self._last_oc_sync: float = 0.0
        self._last_ha_sync: float = 0.0

    async def export_to_memory_md(self) -> int:
        """评分 ≥ 0.7 的知识节点 → MEMORY.md"""
        cursor = await self.db.execute(
            "SELECT title, content, score, source FROM knowledge_nodes "
            f"WHERE score >= 0.7 ORDER BY score DESC LIMIT {MEMORY_SYNC_FACTS_LIMIT}"
        )
        rows = await cursor.fetchall()
        if not rows:
            return 0
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# Yuncat Memory (synced from MemoryTree)", ""]
        cur_section = ""
        for title, content, score, source in rows:
            section = source or "general"
            if section != cur_section:
                lines.append(f"## {section}")
                cur_section = section
            lines.append(f"- **{title}** ({score:.2f}): {content[:MEMORY_SYNC_EXPORT_CONTENT_PREVIEW_CHARS]}")
        self.memory_path.write_text("\n".join(lines), encoding="utf-8")
        self._last_export = time.time()
        _log.info("Exported %d nodes to %s", len(rows), self.memory_path)
        return len(rows)

    async def import_from_memory_md(self) -> int:
        """MEMORY.md 变更 → knowledge_nodes（按 mtime 检测）"""
        if not self.memory_path.exists():
            return 0
        mtime = self.memory_path.stat().st_mtime
        if mtime <= self._last_import_mtime:
            return 0
        content = self.memory_path.read_text(encoding="utf-8")
        self._last_import_mtime = mtime
        added = 0
        for line in content.splitlines():
            m = re.match(r'- \*\*(.+?)\*\*\s*(?:\((\d+\.\d+)\))?:\s*(.+)', line)
            if m:
                node_id = _stable_id("mem_sync", m.group(1))
                await self.db.execute(
                    "INSERT OR REPLACE INTO knowledge_nodes(id, title, content, source, "
                    "score, tags, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (node_id, m.group(1)[:MEMORY_SYNC_TITLE_MAX_CHARS],
                     m.group(3)[:MEMORY_SYNC_IMPORT_CONTENT_MAX_CHARS], "memory_md_import",
                     0.6, '["synced"]', int(time.time()), int(time.time())))
                added += 1
        if added:
            await self.db.commit()
            _log.info("Imported %d entries from MEMORY.md", added)
        return added

    # ── Phase 43 F1.5: 跨后端记忆桥 ──

    async def export_to_openclaw_memory(self) -> int:
        """Yuncat knowledge_nodes (score≥0.7) → OpenClaw MEMORY.md 写回。

        OpenClaw 读取此文件作为其长期记忆——双向同步的关键链路。
        """
        try:
            self.openclaw_memory_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            _log.warning("Cannot create OC memory dir: %s", self.openclaw_memory_path.parent)
            return 0

        cursor = await self.db.execute(
            "SELECT title, content, score, source FROM knowledge_nodes "
            f"WHERE score >= 0.7 ORDER BY score DESC LIMIT {MEMORY_SYNC_FACTS_LIMIT}"
        )
        rows = await cursor.fetchall()
        if not rows:
            return 0

        lines = ["# Yuncat Memory (synced for OpenClaw)", ""]
        cur_section = ""
        for title, content, score, source in rows:
            section = source or "general"
            if section != cur_section:
                lines.append(f"## {section}")
                cur_section = section
            lines.append(f"- **{title}** ({score:.2f}): {content[:MEMORY_SYNC_EXPORT_CONTENT_PREVIEW_CHARS]}")
        self.openclaw_memory_path.write_text("\n".join(lines), encoding="utf-8")
        _log.info("Exported %d nodes to OC MEMORY.md", len(rows))
        return len(rows)

    async def _sync_oc_memory(self) -> int:
        """读 OpenClaw MEMORY.md → knowledge_nodes INSERT OR REPLACE。

        OC 的自主决策、用户偏好、长期洞察以 MEMORY.md 格式存储——
        同步到 Yuncat MemoryTree 后，Hermes 侧的对话也能引用。
        """
        if not self.openclaw_memory_path.exists():
            return 0
        mtime = self.openclaw_memory_path.stat().st_mtime
        if mtime <= self._last_oc_sync:
            return 0
        self._last_oc_sync = mtime

        content = self.openclaw_memory_path.read_text(encoding="utf-8")
        added = 0
        for line in content.splitlines():
            m = re.match(
                r'- \*\*(.+?)\*\*\s*(?:\((\d+\.\d+)\))?:\s*(.+)', line,
            )
            if m:
                node_id = _stable_id("oc_mem", m.group(1))
                await self.db.execute(
                    "INSERT OR REPLACE INTO knowledge_nodes(id, title, content, "
                    "source, score, tags, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (node_id, m.group(1)[:MEMORY_SYNC_TITLE_MAX_CHARS],
                     m.group(3)[:MEMORY_SYNC_IMPORT_CONTENT_MAX_CHARS],
                     "openclaw_memory", 0.7, '["oc_synced"]',
                     int(time.time()), int(time.time())),
                )
                added += 1
        if added:
            await self.db.commit()
            _log.info("Synced %d entries from OC MEMORY.md", added)
        return added

    async def _sync_ha_memory(self) -> int:
        """读 Hermes Agent L4 长期记忆 → knowledge_nodes INSERT OR REPLACE。

        HA 的 L4 长期记忆 (home/.hermes-agent/memory/L4-long-term.md) 包含
        用户画像、决策历史、偏好演变——同步到 Yuncat 枢纽后 OC 侧也能引用。
        """
        if not self.hermes_memory_path.exists():
            # try glob pattern if exact file not found
            pattern = str(Path.home() / ".hermes-agent" / "memory" / "L4-*.md")
            matches = sorted(_glob.glob(pattern))
            if not matches:
                return 0
            path = Path(matches[-1])  # use latest
        else:
            path = self.hermes_memory_path

        mtime = path.stat().st_mtime
        if mtime <= self._last_ha_sync:
            return 0
        self._last_ha_sync = mtime

        content = path.read_text(encoding="utf-8")
        added = 0
        for line in content.splitlines():
            m = re.match(
                r'- \*\*(.+?)\*\*\s*(?:\((\d+\.\d+)\))?:\s*(.+)', line,
            )
            if m:
                node_id = _stable_id("ha_mem", m.group(1))
                await self.db.execute(
                    "INSERT OR REPLACE INTO knowledge_nodes(id, title, content, "
                    "source, score, tags, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (node_id, m.group(1)[:MEMORY_SYNC_TITLE_MAX_CHARS],
                     m.group(3)[:MEMORY_SYNC_IMPORT_CONTENT_MAX_CHARS],
                     "hermes_agent_memory", 0.7, '["ha_synced"]',
                     int(time.time()), int(time.time())),
                )
                added += 1
        if added:
            await self.db.commit()
            _log.info("Synced %d entries from HA memory", added)
        return added

    async def background_loop(self):
        while True:
            await asyncio.sleep(SYNC_INTERVAL)
            try:
                exported = await self.export_to_memory_md()
                imported = await self.import_from_memory_md()
                # Phase 43 F1.5: 跨后端同步
                oc_exported = await self.export_to_openclaw_memory()
                oc_synced = await self._sync_oc_memory()
                ha_synced = await self._sync_ha_memory()
                if exported or imported or oc_exported or oc_synced or ha_synced:
                    _log.info(
                        "Memory sync: %d exp·%d imp·%d oc_exp·%d oc_sync·%d ha_sync",
                        exported, imported, oc_exported, oc_synced, ha_synced,
                    )
                    # Phase 43 E.1: 跨后端记忆同步完成后发布融合事件
                    try:
                        from services.event_bus import get_event_bus, Events
                        eb = get_event_bus()
                        if hasattr(eb, "publish"):
                            await eb.publish(
                                Events.MEMORY_SYNC_COMPLETED,
                                {
                                    "exported": exported,
                                    "imported": imported,
                                    "oc_exported": oc_exported,
                                    "oc_synced": oc_synced,
                                    "ha_synced": ha_synced,
                                    "timestamp": int(time.time()),
                                },
                            )
                    except Exception:
                        _log.debug("发布 memory_sync 完成事件失败", exc_info=True)
            except Exception:
                _log.exception("Memory sync failed")
