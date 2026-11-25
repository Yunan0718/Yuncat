"""services/gc_agent.py —— 后台 GC Agent (每 7 天扫描 workspace)"""
import asyncio
import logging
import re
import time
from pathlib import Path

_log = logging.getLogger("yuncat.gc")

from config import GC_LONG_FUNC_LINES

LONG_FILE = 800   # 行数阈值 (CLAUDE.md 标准)
LONG_FUNC = GC_LONG_FUNC_LINES    # 行数阈值 (CLAUDE.md 标准)
INTERVAL = 604800 # 7 天


class GCAgent:
    """后台垃圾回收 Agent — 扫描 workspace 中的 Python/TypeScript 文件，
    检测超长文件和超长函数，发现的问题写入 knowledge_nodes 供 MemoryTree 管理。
    """

    def __init__(self, db, workspace: str):
        self.db = db
        self.ws = Path(workspace) / "shared"
        self._last: float = 0.0

    async def scan(self) -> dict:
        if not self.ws.exists():
            return {"issues": 0, "error": "workspace not found"}

        issues: list[dict] = []
        py_files = [
            f for f in self.ws.glob("**/*.py")
            if ".venv" not in str(f) and "__pycache__" not in str(f)
        ]

        for fp in py_files:
            try:
                content = fp.read_text(encoding="utf-8", errors="replace")
                lines = content.splitlines()
                if len(lines) > LONG_FILE:
                    issues.append({
                        "type": "long_file",
                        "file": str(fp.relative_to(self.ws)),
                        "detail": f"{len(lines)}行 (>{LONG_FILE})",
                    })

                func_re = re.compile(r"^\s*(?:async\s+)?def\s+")
                prev: int | None = None
                for i, ln in enumerate(lines):
                    if func_re.match(ln):
                        if prev is not None and i - prev > LONG_FUNC:
                            issues.append({
                                "type": "long_function",
                                "file": str(fp.relative_to(self.ws)),
                                "detail": f"~{i - prev}行 (>{LONG_FUNC})",
                            })
                        prev = i
            except Exception:
                continue

        for iss in issues:
            await self.db.execute(
                "INSERT OR REPLACE INTO knowledge_nodes(id, title, content, source, score, tags, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (f"gc_{iss['type']}_{iss['file']}"[:100],
                 f"gc:{iss['type']}:{iss['file'][:60]}",
                 iss["detail"], "gc_agent", 0.4, '["gc","tech-debt"]',
                 int(time.time()), int(time.time())),
            )
        if issues:
            await self.db.commit()

        self._last = time.time()
        _log.info("GC: %d issues in %d files", len(issues), len(py_files))
        return {"issues": len(issues), "files": len(py_files)}

    async def background_loop(self):
        while True:
            await asyncio.sleep(INTERVAL)
            try:
                await self.scan()
            except Exception:
                _log.exception("GC scan failed")
