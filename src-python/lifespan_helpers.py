"""lifespan_helpers.py —— 从 lifespan.py 提取的独立辅助函数

6b-2 修复：将以下 4 个无 app.state 依赖的辅助函数从 lifespan.py 提取到本模块，
让 lifespan.py 聚焦于 FastAPI lifespan 编排 + _init_services 工厂。

提取的函数：
  - _insert_builtin_templates: 内置工作流模板注册（幂等）
  - _periodic_cleanup: 周期性状态清理 loop
  - _PromptReloadHandler: watchdog 文件事件处理器（prompt 热加载）
  - _watch_prompts_with_watchdog: 启动 watchdog 观察 prompts/ 目录

向后兼容：lifespan.py 通过 `from lifespan_helpers import *` re-export 保持现有调用不变。
"""
import asyncio
import sys
import time
import logging
from typing import Any

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from config import PROMPT_WATCHDOG_KEEPALIVE_SEC, PROMPT_WATCHDOG_JOIN_TIMEOUT

_log = logging.getLogger("yuncat.lifespan")


async def _insert_builtin_templates(conn) -> None:
    """自动注册内置工作流模板（幂等——按 name 去重）。"""
    import json as _json
    import uuid as _uuid2
    from prompts.workflow_templates import BUILTIN_TEMPLATES

    current_names = {t["name"] for t in BUILTIN_TEMPLATES}
    for tmpl in BUILTIN_TEMPLATES:
        cursor = await conn.execute(
            "SELECT id, description FROM workflows WHERE name=? AND is_template=1",
            (tmpl["name"],),
        )
        existing = await cursor.fetchone()
        display = tmpl.get("display_name", tmpl["name"])
        if existing:
            if not existing[1]:
                await conn.execute(
                    "UPDATE workflows SET description=?, graph=? WHERE id=?",
                    (display, _json.dumps(tmpl["graph"]), existing[0]),
                )
                await conn.commit()
            continue
        wid = str(_uuid2.uuid4())
        await conn.execute(
            "INSERT INTO workflows(id, name, description, graph, is_template) VALUES(?,?,?,?,1)",
            (wid, tmpl["name"], display, _json.dumps(tmpl["graph"])),
        )
        await conn.commit()

    cursor = await conn.execute("SELECT id, name FROM workflows WHERE is_template=1")
    for row in await cursor.fetchall():
        if row[1] not in current_names:
            await conn.execute("DELETE FROM workflows WHERE id=?", (row[0],))
            _log.info("Removed obsolete template: %s", row[1])
    await conn.commit()


async def _periodic_cleanup(conn, clean_fn, interval=None):
    from config import CLEANUP_INTERVAL
    if interval is None:
        interval = CLEANUP_INTERVAL
    while True:
        await asyncio.sleep(interval)
        try:
            await clean_fn(conn)
        except Exception:
            _log.exception("Periodic cleanup failed")


class _PromptReloadHandler(FileSystemEventHandler):
    """watchdog 文件事件处理器：prompts/ 目录 .py 文件变更时触发 reload。"""

    def __init__(self, loop: asyncio.AbstractEventLoop, prompt_service: Any) -> None:
        self._loop = loop
        self._ps = prompt_service

    def _is_prompt_file(self, path: Any) -> bool:
        return isinstance(path, str) and path.endswith(".py")

    def _schedule_reload(self) -> None:
        asyncio.run_coroutine_threadsafe(self._reload(), self._loop)

    async def _reload(self) -> None:
        try:
            self._ps.check_and_reload()
        except Exception:
            _log.exception("Prompt reload from watchdog failed")

    def on_modified(self, event) -> None:
        if not event.is_directory and self._is_prompt_file(event.src_path):
            self._schedule_reload()

    def on_created(self, event) -> None:
        if not event.is_directory and self._is_prompt_file(event.src_path):
            self._schedule_reload()

    def on_deleted(self, event) -> None:
        if not event.is_directory and self._is_prompt_file(event.src_path):
            self._schedule_reload()

    def on_moved(self, event) -> None:
        if event.is_directory:
            return
        if self._is_prompt_file(event.src_path) or self._is_prompt_file(getattr(event, "dest_path", None)):
            self._schedule_reload()


async def _watch_prompts_with_watchdog(prompt_service: Any) -> None:
    """Phase 56: 用 watchdog 事件驱动替代 5s 轮询，实现 prompt 热加载。"""
    from services.prompt_service import PROMPTS_DIR

    loop = asyncio.get_running_loop()
    handler = _PromptReloadHandler(loop, prompt_service)
    observer = Observer()
    observer.schedule(handler, str(PROMPTS_DIR), recursive=False)
    observer.start()
    _log.info("Prompt watchdog started on %s", PROMPTS_DIR)
    try:
        while True:
            await asyncio.sleep(PROMPT_WATCHDOG_KEEPALIVE_SEC)
    finally:
        observer.stop()
        observer.join(timeout=PROMPT_WATCHDOG_JOIN_TIMEOUT)
        _log.info("Prompt watchdog stopped")


def _emit_stage(name: str) -> None:
    """向 stdout 输出启动阶段信号，供 Electron main.js 解析后转发给前端 StartupScreen。

    与 YUNCAT_READY 同机制——必须 stdout 直写（不能用 logging，logging 走 stderr/文件）。
    格式: YUNCAT_STAGE <name>

    2026-09-15 修复：本函数原先定义在 lifespan.py，而 `_timed_stage` 在本模块执行——
    跨模块引用 lifespan 的私有名会形成循环导入，此前直接写成了裸名 → NameError →
    lifespan 启动失败 → 整个后端起不来。下移到本模块后两个函数同源，不再跨模块引用。
    """
    sys.stdout.write(f"YUNCAT_STAGE {name}\n")
    sys.stdout.flush()


async def _timed_stage(name: str, coro):
    """执行 coro 并把「本步耗时」写进启动阶段日志。

    用途：`asyncio.gather` 并发初始化时，只看整段总时长无法定位是哪一步慢；
    每步自报毫秒数后，启动性能问题不需要靠猜测（2026-09-15 实测：
    ext-gather 段 1.69s 里到底是谁慢，单看总时长分辨不出）。
    """
    # 必须用 monotonic：墙钟被 NTP 校时时，相减会出现负数/巨大值（2026-09-15 修复）。
    _t0 = time.monotonic()
    try:
        return await coro
    finally:
        _emit_stage(f"{name}+{int((time.monotonic() - _t0) * 1000)}ms")
