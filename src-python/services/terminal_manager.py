"""services/terminal_manager.py —— 终端会话管理器（Phase 21）

双通道分离 + PTY + 会话保活 + 多 Tab + Block 输出 + Agent 回传。
借鉴 Warp Blocks / VS Code Terminal / Jean 终端多路复用。

核心设计:
  - 人类终端: ws_terminal → TerminalSessionManager → PTY → shell
    不走 is_safe_command，完全自由
  - Agent 命令: ToolService.execute("run_command") → 走 is_safe_command（不变）
  - 会话保活: WS 断开不杀进程，reopen 时检查存活并重新绑定
  - Block 输出: 每个命令一个 Block（block_start → block_stdout* → block_done）
"""
import asyncio
import logging
import os
import sys
import time as _time
import uuid as _uuid
from concurrent.futures import ThreadPoolExecutor

from config import TERMINAL_SESSION_MAX_AGE_SEC  # Phase 5 P2: 会话过期阈值统一管理

_log = logging.getLogger("yuncat.terminal")

# ---- 平台检测 ----
IS_WIN = sys.platform == "win32"

# Batch 5a C4: PTY 阻塞读取专用线程池——避免每个 PTY 的 os.read 占满 asyncio 默认线程池
#（默认线程池需服务 run_in_executor 的其他调用方，如 DNS/文件 IO）。
_PTY_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="yuncat-pty")


def load_win_pty():
    """加载 Windows PTY 实现。pywinpty 2.x 之后导入名是 ``winpty``（1.x 是 ``pywinpty``）。

    2026-09-17 修：原来只试 ``import pywinpty``，而现代 pywinpty（2.x/3.x）提供的是 ``winpty``
    包——于是 pywinpty 明明装了却进口失败，终端静默降级成管道模式（没提示符、
    没回显、交互式命令用不了）。两个名字都试，都不行返回 None。
    """
    try:
        from winpty import PtyProcess  # pywinpty >= 2.x

        return PtyProcess
    except ImportError:
        try:
            from pywinpty import PtyProcess  # pywinpty 1.x

            return PtyProcess
        except ImportError:
            return None


def _check_pty() -> bool:
    """检查 PTY 是否可用——每次调用都重新检查，支持运行时补装 pywinpty"""
    if IS_WIN:
        return load_win_pty() is not None
    else:
        try:
            import pty  # noqa: F401
            import termios  # noqa: F401

            return True
        except ImportError:
            return False


HAS_PTY = _check_pty()


def pty_backend_name() -> str:
    """当前 PTY 后端名——前端据此提示"交互式终端是否可用"（winpty / unix / pipe）"""
    if IS_WIN:
        return "winpty" if load_win_pty() is not None else "pipe"
    return "unix" if HAS_PTY else "pipe"


class TerminalSession:
    """单个终端会话——管理一个 shell 进程 + 输入输出流"""

    def __init__(self, session_id: str, cwd: str, cols: int = 120, rows: int = 40):
        self.session_id = session_id
        self.cwd = cwd
        self.cols = cols
        self.rows = rows
        self.proc: asyncio.subprocess.Process | None = None
        self.master_fd: int | None = None  # PTY master fd
        self.reader_task: asyncio.Task | None = None
        self.ws_connections: list = []  # 当前活跃的 WS 连接
        self.block_id: str | None = None  # 当前 Block ID
        self.created_at = _time.time()
        self.last_active = _time.time()
        self.is_pty = False
        # "winpty" / "unix" / None——winpty 的 fd 是 socket 句柄，不能当 CRT fd 读写
        self.pty_kind: str | None = None

    @property
    def alive(self) -> bool:
        if self.pty_kind == "winpty":
            proc = getattr(self, "_pty_proc", None)
            return proc is not None and proc.isalive()
        return self.proc is not None and self.proc.returncode is None


class TerminalSessionManager:
    """终端会话管理器——创建/销毁/多 Tab/Block 输出/Agent 回传"""

    def __init__(self):
        self._sessions: dict[str, TerminalSession] = {}
        self._tab_counter = 0

    def create_session(self, cwd: str, cols: int = 120, rows: int = 40) -> TerminalSession:
        sid = str(_uuid.uuid4())[:8]
        session = TerminalSession(sid, cwd, cols, rows)
        self._sessions[sid] = session
        self._tab_counter += 1
        return session

    async def spawn_shell(self, session: TerminalSession) -> None:
        """启动 shell 进程——每次运行时检测 PTY（支持实例安装 pywinpty 后不重启）"""
        ws_dir = session.cwd
        os.makedirs(ws_dir, exist_ok=True)
        shell_cmd = "powershell.exe -NoLogo" if IS_WIN else "/bin/bash"
        has_pty = _check_pty()

        if has_pty and IS_WIN:
            await self._spawn_winpty(session, ws_dir)
        elif has_pty and not IS_WIN:
            await self._spawn_unix_pty(session, ws_dir)
        else:
            # Fallback: pipe 模式（无 PTY）
            await self._spawn_pipe(session, shell_cmd, ws_dir)

    async def _spawn_unix_pty(self, session, ws_dir: str):
        import pty as _pty
        import termios as _termios
        import struct
        import fcntl
        master_fd, slave_fd = _pty.openpty()
        _termios.tcsetwinsize(slave_fd, struct.pack("HHHH", session.rows, session.cols, 0, 0))
        session.master_fd = master_fd
        session.is_pty = True
        session.pty_kind = "unix"
        session.proc = await asyncio.create_subprocess_exec(
            "/bin/bash", stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            env={**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"},
            cwd=ws_dir, start_new_session=True,
        )
        os.close(slave_fd)

    async def _spawn_winpty(self, session, ws_dir: str):
        """用 pywinpty 起交互式 shell（Windows）。

        2026-09-17 修——旧代码有三个问题，任意一个都让 Windows 终端不可用：
        1. ``desktop_output=False`` 不是 pywinpty 的形参，spawn 直接 TypeError；
        2. spawn 的返回值没接住，紧接着的 ``pty_proc.fd`` 是 NameError；
        3. pywinpty 的 ``fd`` 只是读线程的 socket 句柄，``os.read/os.write`` 会 EBADF；
           读写必须走 ``PtyProcess.read/.write``（见 pty_kind == "winpty" 的分支）。
        """
        PtyProcess = load_win_pty()
        if PtyProcess is None:
            raise RuntimeError("Windows PTY 不可用（pywinpty 未装）")
        pty_proc = PtyProcess.spawn(
            "powershell.exe -NoLogo",
            dimensions=(session.rows, session.cols),
            env={**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"},
            cwd=ws_dir,
        )
        session._pty_proc = pty_proc
        session.master_fd = None  # winpty 不走 os.read/os.write
        session.is_pty = True
        session.pty_kind = "winpty"

    async def _spawn_pipe(self, session, shell_cmd: str, ws_dir: str):
        """PTY 不可用时的降级：管道模式（没有提示符/回显，只够跑非交互命令）"""
        _log.warning("Terminal %s: PTY 不可用，降级为管道模式", session.session_id)
        session.pty_kind = None
        kwargs = {"cwd": ws_dir, "start_new_session": True}
        if IS_WIN:
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        session.proc = await asyncio.create_subprocess_shell(
            shell_cmd, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            **kwargs,
        )

    async def resize(self, session: TerminalSession, cols: int, rows: int) -> None:
        session.cols = cols
        session.rows = rows
        if session.pty_kind == "winpty":
            # winpty 的 master_fd 恒为 None，旧分支因此整个跳过 Windows 的 resize
            proc = getattr(session, "_pty_proc", None)
            if proc is None:
                return
            try:
                proc.setwinsize(rows, cols)
            except Exception:
                _log.debug("winpty setwinsize 失败", exc_info=True)
            return
        if session.is_pty and session.master_fd is not None:
            try:
                import termios, struct
                termios.tcsetwinsize(session.master_fd, struct.pack("HHHH", rows, cols, 0, 0))
            except Exception:
                _log.debug("Unexpected error", exc_info=True)

    async def write_stdin(self, session: TerminalSession, data: str) -> None:
        session.last_active = _time.time()
        if session.pty_kind == "winpty":
            proc = getattr(session, "_pty_proc", None)
            if proc is not None and proc.isalive():
                proc.write(data)
            return
        if session.is_pty and session.master_fd is not None:
            os.write(session.master_fd, data.encode("utf-8"))
        elif session.proc and session.proc.stdin:
            session.proc.stdin.write(data.encode("utf-8"))
            await session.proc.stdin.drain()

    async def read_stdout(self, session: TerminalSession, ws):
        """持续读取 shell 输出——PTY 从 master_fd 读，pipe 从 proc.stdout 读"""
        # Batch 5a C4: 记录读取协程，供 kill_session cancel（先 cancel 再收 fd）
        session.reader_task = asyncio.current_task()
        try:
            if session.pty_kind == "winpty":
                await self._read_winpty(session, ws)
            elif session.is_pty and session.master_fd is not None:
                # PTY 模式——每次读 4096 字节
                # Phase 48 T3-16: asyncio.get_event_loop() Python 3.12 弃用 → get_running_loop()
                loop = asyncio.get_running_loop()
                while session.alive or (session.is_pty and session.master_fd is not None):
                    try:
                        data = await loop.run_in_executor(
                            _PTY_EXECUTOR, lambda: os.read(session.master_fd, 4096),
                        )
                        if not data:
                            break
                        chunk = data.decode("utf-8", errors="replace")
                        await self._send_output(session, chunk, ws)
                    except OSError:
                        break
            else:
                # Pipe 模式——逐行读
                assert session.proc and session.proc.stdout
                while True:
                    line = await session.proc.stdout.readline()
                    if not line:
                        break
                    chunk = line.decode("utf-8", errors="replace")
                    await self._send_output(session, chunk, ws)
        except asyncio.CancelledError:
            pass
        except Exception:
            _log.exception("Terminal read error for session %s", session.session_id)

    async def _read_winpty(self, session: TerminalSession, ws) -> None:
        """从 winpty 读输出——pywinpty 只给阻塞式 ``read()``，放专用线程池跑"""
        proc = getattr(session, "_pty_proc", None)
        if proc is None:
            return
        loop = asyncio.get_running_loop()
        while True:
            try:
                chunk = await loop.run_in_executor(_PTY_EXECUTOR, proc.read, 4096)
            except (EOFError, OSError):
                break  # shell 退出或 pty 已关闭
            if not chunk:
                break
            await self._send_output(session, chunk, ws)

    async def _send_output(self, session: TerminalSession, data: str, ws):
        """发送输出到 WS——Block 模式下按 Block 分帧"""
        try:
            await ws.send_json({"type": "output", "data": data})
        except Exception:
            _log.debug("Unexpected error", exc_info=True)

    async def broadcast_agent_block(self, agent_id: str, command: str,
                                     output_snippet: str, exit_code: int,
                                     task_id: str) -> None:
        """Agent 命令执行完成后，向所有活跃终端广播 agent_block 消息"""
        block = {
            "type": "agent_block",
            "agent_id": agent_id,
            "command": command,
            "output_snippet": output_snippet[:500],
            "exit_code": exit_code,
            "task_id": task_id,
        }
        for sid, session in list(self._sessions.items()):
            for ws in session.ws_connections:
                try:
                    await ws.send_json(block)
                except Exception:
                    _log.debug("Unexpected error", exc_info=True)

    async def kill_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if not session:
            return
        # Batch 5a C4: 先 cancel 读取协程再收 fd——避免 os.close 后 reader 仍阻塞读已关闭 fd。
        # 注：Windows 下 cancel 无法中断线程池中阻塞的 os.read；专用 _PTY_EXECUTOR
        # 已避免默认线程池被占满，reader 最终因 fd 关闭触发 OSError 退出。
        if session.reader_task and not session.reader_task.done():
            session.reader_task.cancel()
        if session.pty_kind == "winpty":
            proc = getattr(session, "_pty_proc", None)
            if proc is not None:
                try:
                    proc.terminate(force=True)
                except Exception:
                    _log.debug("winpty terminate 失败", exc_info=True)
                try:
                    proc.close()  # 关掉读线程的 socket，否则线程常驻
                except Exception:
                    _log.debug("winpty close 失败", exc_info=True)
        elif session.is_pty and hasattr(session, "_pty_proc"):
            try:
                session._pty_proc.terminate()
            except Exception:
                _log.debug("Unexpected error", exc_info=True)
        elif session.proc and session.proc.returncode is None:
            try:
                session.proc.terminate()
                await asyncio.wait_for(session.proc.wait(), timeout=2)
            except Exception:
                session.proc.kill()
        if session.master_fd is not None:
            try:
                os.close(session.master_fd)
            except Exception:
                _log.debug("Unexpected error", exc_info=True)

    async def cleanup_expired(self, max_age_seconds: int = TERMINAL_SESSION_MAX_AGE_SEC) -> int:
        """清理超过 max_age 且无 WS 连接的会话"""
        now = _time.time()
        expired = [
            sid for sid, s in self._sessions.items()
            if not s.ws_connections and (now - s.last_active) > max_age_seconds
        ]
        for sid in expired:
            await self.kill_session(sid)
        return len(expired)

    def list_sessions(self) -> list[dict]:
        return [
            {"session_id": s.session_id, "cwd": s.cwd,
             "alive": s.alive, "cols": s.cols, "rows": s.rows,
             "ws_count": len(s.ws_connections)}
            for s in self._sessions.values()
        ]


# 全局单例
_terminal_manager: TerminalSessionManager | None = None


def get_terminal_manager() -> TerminalSessionManager:
    global _terminal_manager
    if _terminal_manager is None:
        _terminal_manager = TerminalSessionManager()
    return _terminal_manager
