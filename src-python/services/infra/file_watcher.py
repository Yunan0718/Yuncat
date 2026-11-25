"""services/file_watcher.py —— 工作区文件监控 (Phase 47)

对标 Claude Code: 检测外部文件修改 → 重新读取 → Agent 获得最新内容。

利用 pyproject.toml 已有的 watchdog>=4.0 依赖。
通过 EventBus 发布 file_changed 事件 → lifespan WS 转发 → 前端刷新 FileTree。

F2 修复（Round 2）:
- _pending 加 threading.Lock 保护——on_any_event 在 watchdog observer 线程调用，
  _flush 在 asyncio loop 线程调用，跨线程 dict 读写需锁保护（避免 check-then-act 竞态）。
- _flush 后检查 _pending 是否非空，非空则重新调度 flush——修复"事件在 clear() 后到达
  但 flush task 已 done 导致 stuck-path 无限延迟"的 bug。
- on_moved 同时记录 src_path 和 dest_path——前端 FileTree 需知道新文件路径。
"""
import asyncio
import logging
import os
import threading
import time as _time
from pathlib import Path

from config import FILE_WATCHER_MAX_PATHS_LOG

_log = logging.getLogger("yuncat.file_watcher")

# 跳过监控的目录/文件模式（对标 Claude Code: 忽略 .git / node_modules 等）
_IGNORE_PATTERNS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', '.yuncat',
                    '.backup', 'compactions', '.DS_Store', 'Thumbs.db'}
_IGNORE_PREFIXES = ('.',)  # 跳过隐藏文件


class DebouncedHandler:
    """watchdog 事件处理器——500ms 防抖（对标 Claude Code 的批量变更合并）

    F2: _pending 用 threading.Lock 保护跨线程访问（watchdog 线程 vs asyncio loop）。
    用 _flushing 标志 + 锁内 create_task 避免 stuck-path 和 double-flush。
    """

    def __init__(self, event_bus, workspace: str):
        self._event_bus = event_bus
        self._workspace = workspace
        self._pending: dict[str, float] = {}  # path → first_seen_at
        self._lock = threading.Lock()  # F2: 保护 _pending 跨线程读写
        self._debounce_s = 0.5
        self._flush_task: asyncio.Task | None = None
        self._flushing = False  # F2: flush 进行中标志，防止 on_any_event 与 _flush 间的 stuck/double
        # Phase 48 T3-16: asyncio.get_event_loop() 在 Python 3.12 弃用；
        # DebouncedHandler 在 FileWatcherService.start() 协程内构造，可安全用 get_running_loop()
        self._loop = asyncio.get_running_loop()

    def on_any_event(self, event):
        """watchdog 回调——同步，仅记录待处理路径

        F2: 由 watchdog observer 线程调用，与 asyncio loop 线程并发访问 _pending，
        用 _lock 保护 check-then-act（if not in → set）的原子性。
        _flushing=True 时依赖 _flush 的 re-arm 拾取新事件，不重复建 task。
        """
        if event.is_directory:
            return
        src = event.src_path
        # F2: on_moved 同时记录 src_path 和 dest_path
        paths_to_record = [src]
        dest = getattr(event, "dest_path", None)
        if dest:
            paths_to_record.append(dest)

        with self._lock:
            for p in paths_to_record:
                # 跳过忽略的目录
                parts = set(Path(p).parts)
                if parts & _IGNORE_PATTERNS:
                    continue
                name = os.path.basename(p)
                if name.startswith(_IGNORE_PREFIXES):
                    continue
                # 防抖：记录首次发现时间
                if p not in self._pending:
                    self._pending[p] = _time.monotonic()
            # F2: 仅当无 flush 进行中时才调度——_flush 进行中会自己 re-arm
            if self._pending and not self._flushing:
                self._flushing = True
                self._flush_task = self._loop.create_task(self._flush_soon())

    async def _flush_soon(self):
        """等待防抖窗口后，批量发布变更事件"""
        await asyncio.sleep(self._debounce_s)
        await self._flush()

    async def _flush(self):
        """批量发布 pending 路径。

        F2: flush 后在锁内检查 _pending——若 publish 期间有新事件到达，
        立即 re-arm 下一轮 flush；否则清除 _flushing 标志。
        锁内 create_task 保证 on_any_event 不会 double-schedule。
        """
        with self._lock:
            paths = list(self._pending.keys())
            self._pending.clear()
        if not paths:
            with self._lock:
                self._flushing = False
            return
        try:
            await self._event_bus.publish("file_changed", {
                "paths": paths[:FILE_WATCHER_MAX_PATHS_LOG],  # 最多 N 个路径
                "count": len(paths),
                "workspace": self._workspace,
            })
        except Exception:
            _log.debug("file_changed publish failed", exc_info=True)

        # F2: publish 后在锁内决定 re-arm 还是结束——避免 stuck-path 与 double-flush
        with self._lock:
            if self._pending:
                # 新事件在 flush 期间到达——保持 _flushing=True，调度下一轮
                self._flush_task = self._loop.create_task(self._flush_soon())
            else:
                self._flushing = False


class FileWatcherService:
    """工作区文件监控服务——对标 Claude Code 的自动文件重读。

    在 lifespan 中启动，随应用生命周期。
    watch_files_enabled settings 键控制开关（默认启用）。
    """

    def __init__(self):
        self._observer = None
        self._handler = None

    async def start(self, workspace: str, event_bus) -> bool:
        """启动文件监控。返回 True 表示成功启动。"""
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except ImportError:
            _log.warning("watchdog not installed, file watching disabled")
            return False

        ws = workspace
        if not ws or not os.path.isdir(ws):
            _log.debug("No workspace directory, file watching skipped")
            return False

        # 创建一个简单的事件处理器子类
        handler = DebouncedHandler(event_bus, ws)

        class _Handler(FileSystemEventHandler):
            def on_modified(self2, event): handler.on_any_event(event)
            def on_created(self2, event): handler.on_any_event(event)
            def on_deleted(self2, event): handler.on_any_event(event)
            def on_moved(self2, event): handler.on_any_event(event)

        self._observer = Observer()
        self._observer.schedule(_Handler(), ws, recursive=True)
        self._observer.start()
        self._handler = handler
        _log.info("File watcher started on %s", ws[:80])
        return True

    async def stop(self):
        """P2-6 修复: 优雅关闭——停 observer + 取消 in-flight flush + 清空 pending。

        原代码只停 watchdog observer，_flush_task 仍挂在 event loop，
        stop 后可能触发孤儿 publish 到已关闭的 event_bus。
        对标 watchdog 官方优雅关闭示例 + asyncio task cleanup 标准模式。
        """
        # 1. 先停 watchdog observer（不再产生新事件）
        if self._observer and self._observer.is_alive():
            self._observer.stop()
            self._observer.join(timeout=3)
        # 2. 取消 in-flight flush task
        if self._handler and self._handler._flush_task:
            self._handler._flush_task.cancel()
            try:
                await self._handler._flush_task
            except (asyncio.CancelledError, Exception):
                pass
        # 3. 清空 pending + 重置标志（避免下次 start 残留）
        if self._handler:
            with self._handler._lock:
                self._handler._pending.clear()
                self._handler._flushing = False
                self._handler._flush_task = None
        self._observer = None
        self._handler = None
        _log.info("File watcher stopped")
