"""services/lsp_manager.py —— 语言服务器进程管理（B 方案：用本机 Node + pyright）

方案选择：pyright 是 Python 补全/类型检查最准的一个，但它需要 Node 运行时。
项目已经从「随包带 Node」改成「用系统 PATH 里的 node」（context7 / codegraph 都是这么干的），
这里保持一致：**有 Node 就用，没有就如实报不可用 + 原因**，前端只在用户真的打开
Python 文件时提示一次，绝不降级成看起来很聪明、实际乱猜的假补全。

进程模型：每个 (language, workspace) 一个 pyright-langserver 子进程，stdio 上跑 LSP。
本模块只做三件事：握手（initialize / initialized）、按帧转发、收尾。
不做语义处理——换语言服务器或升级 pyright 都不需要动这里。

进程回收：Windows 下 npx 会再拉一个 node 子进程，只杀父进程会留孤儿，
所以统一走 taskkill /T（与 Electron 侧处理 Python 后端的方式一致），并在 atexit 兜底。
"""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

_log = logging.getLogger("yuncat.lsp")

# 人为上限（可调）：初始化（npx 首次要下载 pyright，可能要几十秒）/ 单帧 / 同时存活的服务器
LSP_INIT_TIMEOUT_SEC = 90.0
LSP_REQUEST_TIMEOUT_SEC = 30.0
LSP_MAX_MESSAGE_BYTES = 8 * 1024 * 1024
LSP_MAX_SESSIONS = 4
LSP_STDERR_KEEP_CHARS = 4000

# 允许把语言服务器指向本地已下载的 pyright-langserver.js（离线环境 / 自备安装）
ENV_PYRIGHT_LANGSERVER = "YUNCAT_PYRIGHT_LANGSERVER"

SUPPORTED_LANGUAGES: tuple[str, ...] = ("python",)

# 客户端能力：故意把 dynamicRegistration / configuration / workDoneProgress 关掉——
# 否则 pyright 会反向请求客户端（workspace/configuration 等），而我们的转发层
# 只做透传，答不上来就会卡住握手时序。
_CLIENT_CAPABILITIES: dict = {
    "workspace": {"workspaceFolders": True, "configuration": False},
    "window": {"workDoneProgress": False},
    "textDocument": {
        "synchronization": {"dynamicRegistration": False, "didSave": True},
        "completion": {
            "dynamicRegistration": False,
            "completionItem": {
                "snippetSupport": True,
                "documentationFormat": ["markdown", "plaintext"],
                "resolveSupport": {"properties": ["documentation", "detail"]},
            },
        },
        "hover": {"dynamicRegistration": False, "contentFormat": ["markdown", "plaintext"]},
        "definition": {"dynamicRegistration": False},
        "declaration": {"dynamicRegistration": False},
        "references": {"dynamicRegistration": False},
        "rename": {"dynamicRegistration": False, "prepareSupport": True},
        "signatureHelp": {"dynamicRegistration": False},
        "documentSymbol": {"dynamicRegistration": False},
        "publishDiagnostics": {"relatedInformation": True, "versionSupport": True},
    },
}


def _path_to_uri(path: str) -> str:
    """本地路径 → file:// URI（Windows 上必须是 file:///D:/... 三段斜杠）"""
    posix = Path(path).resolve().as_posix()
    if not posix.startswith("/"):
        posix = "/" + posix
    return "file://" + quote(posix, safe="/:")


def resolve_python_server() -> tuple[list[str] | None, str, str]:
    """解析 pyright 的启动命令，返回 (命令, 不可用原因, 原因代码)。

    原因代码是给前端做多语言用的：文案不从这里下发，否则界面语言切了还是中文。
    """
    node = shutil.which("node")
    if not node:
        return None, "未找到 Node.js。Python 智能提示需要本机安装 Node.js 18 或更高版本（安装后重启应用即可）。", "node-missing"
    override = (os.environ.get(ENV_PYRIGHT_LANGSERVER) or "").strip()
    if override:
        if not os.path.isfile(override):
            return None, f"{ENV_PYRIGHT_LANGSERVER} 指向的文件不存在：{override}", "override-missing"
        return [node, override, "--stdio"], "", ""
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    if not npx:
        return None, "未找到 npx（Node.js 安装似乎不完整）。重装 Node.js 后重启应用即可。", "npx-missing"
    return [npx, "--yes", "--package", "pyright", "pyright-langserver", "--stdio"], "", ""


def language_status(language: str) -> dict:
    """某个语言的可用性快照——前端据此决定「静默」还是「提示一次」。"""
    lang = (language or "").lower()
    if lang not in SUPPORTED_LANGUAGES:
        return {"language": lang, "available": False, "reason": f"暂不支持 {lang} 的智能提示",
                "code": "unsupported-language"}
    command, reason, code = resolve_python_server()
    return {"language": lang, "available": command is not None, "reason": reason, "code": code}


def encode_message(payload: dict) -> bytes:
    """LSP 帧：Content-Length 头 + UTF-8 正文（头与正文之间必须有空行）"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


async def _read_message(stream: asyncio.StreamReader) -> dict | None:
    """读一条 LSP 消息；流结束返回 None，帧非法则抛错（不猜、不跳过）"""
    headers: dict[str, str] = {}
    while True:
        line = await stream.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode("latin-1").partition(":")
        headers[key.strip().lower()] = value.strip()
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError as exc:
        raise ValueError(f"LSP 帧头非法：{headers!r}") from exc
    if length <= 0:
        return None
    if length > LSP_MAX_MESSAGE_BYTES:
        raise ValueError(f"LSP 消息过大：{length} > {LSP_MAX_MESSAGE_BYTES}")
    body = await stream.readexactly(length)
    return json.loads(body.decode("utf-8"))


def _kill_process_tree(proc) -> None:
    """杀进程树：Windows 上 npx 的 node 子进程不会被父进程带走。"""
    pid = getattr(proc, "pid", None)
    if not pid:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=10, check=False)
        else:
            proc.terminate()
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("LSP kill failed for pid %s: %s", pid, exc)


class LspSession:
    """一个语言服务器进程 + 它的消息泵。"""

    def __init__(self, language: str, root: str, command: list[str]) -> None:
        self.language = language
        self.root = root
        self.command = command
        self.capabilities: dict = {}
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._listeners: list = []
        self._tasks: list[asyncio.Task] = []
        self._stderr_tail = ""

    @property
    def proc(self):
        return self._proc

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def stderr_tail(self) -> str:
        return self._stderr_tail

    def add_listener(self, callback) -> None:
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _write(self, payload: dict) -> bool:
        proc = self._proc
        if proc is None or proc.returncode is not None or proc.stdin is None:
            return False
        try:
            proc.stdin.write(encode_message(payload))
            await proc.stdin.drain()
            return True
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
            _log.warning("LSP write failed (%s): %s", self.language, exc)
            return False

    async def _pump_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                message = await _read_message(self._proc.stdout)
                if message is None:
                    break
                msg_id = message.get("id")
                if msg_id is not None and msg_id in self._pending:
                    future = self._pending.pop(msg_id)
                    if not future.done():
                        future.set_result(message)
                    continue
                for listener in list(self._listeners):
                    try:
                        await listener(message)
                    except Exception:
                        _log.debug("LSP listener failed", exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.warning("LSP stdout pump stopped (%s): %s", self.language, exc)
            self._fail_pending(f"语言服务器中断：{exc}")
            return
        # 流正常结束 == 进程退出了。这里必须唤醒等待者：否则 pyright 刚起来就崩时，
        # 用户要等满 90 秒握手超时才看到「启动失败」（实测踩到过）。
        self._fail_pending("语言服务器进程已退出")

    def _fail_pending(self, reason: str) -> None:
        """把所有还在等的请求一次性失败掉——不能让调用方一直等到超时。"""
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(RuntimeError(reason))
        self._pending.clear()

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                chunk = await self._proc.stderr.read(4096)
                if not chunk:
                    break
                text = chunk.decode("utf-8", "replace")
                self._stderr_tail = (self._stderr_tail + text)[-LSP_STDERR_KEEP_CHARS:]
                _log.debug("LSP[%s] %s", self.language, text.strip())
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.debug("LSP stderr drain stopped", exc_info=True)

    async def request(self, method: str, params, timeout: float) -> dict:
        """发一个请求并等响应（只用于握手；业务请求由前端直接透传）"""
        msg_id = self._new_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[msg_id] = future
        if not await self._write({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}):
            self._pending.pop(msg_id, None)
            raise RuntimeError("语言服务器进程已退出")
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(msg_id, None)
            raise RuntimeError(f"{method} 超时（{timeout:.0f}s）") from exc

    async def send(self, payload: dict) -> bool:
        """前端透传过来的原始 LSP 消息"""
        return await self._write(payload)

    async def start(self) -> dict:
        _log.info("LSP start %s: %s", self.language, self.command[0])
        self._proc = await asyncio.create_subprocess_exec(
            *self.command, cwd=self.root,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._tasks = [
            asyncio.create_task(self._pump_stdout()),
            asyncio.create_task(self._drain_stderr()),
        ]
        params = {
            "processId": os.getpid(),
            "clientInfo": {"name": "Yuncat"},
            "rootUri": _path_to_uri(self.root),
            "workspaceFolders": [{"uri": _path_to_uri(self.root), "name": Path(self.root).name or "workspace"}],
            "capabilities": _CLIENT_CAPABILITIES,
        }
        try:
            response = await self.request("initialize", params, LSP_INIT_TIMEOUT_SEC)
        except Exception as exc:
            detail = (self._stderr_tail or "").strip()[-400:]
            await self.stop()
            raise RuntimeError(f"启动语言服务器失败：{exc}{(' / ' + detail) if detail else ''}") from exc
        if "error" in response:
            await self.stop()
            raise RuntimeError(f"语言服务器拒绝初始化：{response['error']}")
        self.capabilities = (response.get("result") or {}).get("capabilities") or {}
        await self._write({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        return self.capabilities

    async def stop(self) -> None:
        proc = self._proc
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        for future in list(self._pending.values()):
            if not future.done():
                future.cancel()
        self._pending.clear()
        if proc is not None and proc.returncode is None:
            try:
                await asyncio.wait_for(self._write({"jsonrpc": "2.0", "method": "exit", "params": None}), 2)
            except (asyncio.TimeoutError, RuntimeError):
                _log.debug("LSP exit notify failed", exc_info=True)
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except (asyncio.TimeoutError, RuntimeError):
                _kill_process_tree(proc)
        self._proc = None
        _log.info("LSP stopped %s", self.language)


_sessions: dict[tuple[str, str], LspSession] = {}
_lock: asyncio.Lock | None = None


def _session_lock() -> asyncio.Lock:
    """延迟创建锁：模块导入时可能还没有事件循环。"""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def get_session(language: str, root: str) -> LspSession:
    """取（必要时启动）某个语言在某个工作区里的会话。"""
    lang = (language or "").lower()
    if lang not in SUPPORTED_LANGUAGES:
        raise RuntimeError(f"暂不支持 {lang} 的智能提示")
    key = (lang, root)
    async with _session_lock():
        existing = _sessions.get(key)
        if existing is not None and existing.alive:
            return existing
        if existing is not None:
            _sessions.pop(key, None)
        if len(_sessions) >= LSP_MAX_SESSIONS:
            oldest_key = next(iter(_sessions))
            _log.info("LSP session limit reached, stopping %s", oldest_key)
            await _sessions.pop(oldest_key).stop()
        command, reason, _code = resolve_python_server()
        if command is None:
            raise RuntimeError(reason)
        session = LspSession(lang, root, command)
        await session.start()
        _sessions[key] = session
        return session


async def stop_all() -> None:
    """应用退出时回收所有语言服务器进程（lifespan 调用）。"""
    for key in list(_sessions.keys()):
        session = _sessions.pop(key, None)
        if session is None:
            continue
        try:
            await session.stop()
        except Exception:
            _log.debug("LSP stop failed during shutdown", exc_info=True)


def _kill_all_sync() -> None:
    """atexit 兜底：进程直接退出（没走 lifespan）时也别留下孤儿 node。"""
    for session in list(_sessions.values()):
        proc = session.proc
        if proc is not None and proc.returncode is None:
            _kill_process_tree(proc)


atexit.register(_kill_all_sync)
