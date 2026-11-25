"""adapters/base.py —— Agent 适配器基类 + AgentInvoker Protocol

每个 Adapter 管理一个子进程，通过 HTTP/stdio 协议与 Agent 通信。
"""
import asyncio
import logging
from config import (
    ADAPTER_HEALTH_CHECK_TIMEOUT, ADAPTER_INVOKE_TIMEOUT, QUEUE_MAXSIZE,
    ADAPTER_READY_MAX_WAIT_SECONDS, ADAPTER_READY_POLL_INTERVAL_SECONDS,
    ADAPTER_RESTART_MAX_PER_HOUR, ADAPTER_RESTART_DELAYS,
    ADAPTER_PROCESS_WAIT_TIMEOUT_SEC, ADAPTER_RESTART_BACKOFF_SEC,
    ADAPTER_STREAM_IDLE_TIMEOUT,
)
import os
import subprocess
from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol

_log = logging.getLogger("yuncat.adapter")


def _resolve_history_limit(context: dict | None) -> int:
    """解析 history_limit——优先用 context 显式值，缺省时按 model_config 动态计算。

    Phase 48 C1: 原 fallback 硬编码 200，DeepSeek V4 1M（2000 轮历史）只用 8%。
    现按 model_config 动态计算（calc_history_limit），小模型保守、大模型充分利用。
    """
    ctx = context or {}
    explicit = ctx.get("history_limit")
    if explicit:
        return int(explicit)
    model_cfg = ctx.get("model_config") or {}
    if model_cfg:
        try:
            from config import calc_history_limit
            return calc_history_limit(model_cfg)
        except Exception:
            _log.debug("calc_history_limit failed, fallback to 200", exc_info=True)
    return 200


def _build_image_user_content(
    prompt: str, attachments: list[dict], model_cfg: dict | None = None,
) -> list[dict]:
    """构建含图 user content——预算过滤 + detail 档 + 超限占位（services/image_budget）。

    2026-09-10（对标 deepseek-harness 图像策略，零依赖实现）：
    此前把所有附件原样内联且 detail 恒为 auto，多图/大图会每轮重复计费且无上限。
    现在按「单图字节 / 张数 / 总字节」三级预算过滤，超限图卸载为占位文本。
    """
    from services.image_budget import apply_image_budget, detail_from_model_config

    detail = detail_from_model_config(model_cfg)
    kept, placeholders, tokens = apply_image_budget(attachments, detail=detail)
    content: list[dict] = [{"type": "text", "text": prompt}]
    for att in kept:
        content.append({
            "type": "image_url",
            "image_url": {"url": _attachment_url(att), "detail": att.get("_detail", detail)},
        })
    for ph in placeholders:
        content.append({"type": "text", "text": ph})
    if kept:
        _log.info("image request: %d image(s), ~%d vision tokens (detail=%s)",
                  len(kept), tokens, detail)
    return content


def _attachment_url(att: dict) -> str:
    """将附件 dict 转换为 vision API 可用的图片 URL。

    前端发送 {name, data: base64字符串, type: mime_type}，其中 data 不含 data URL 前缀。
    若 data 已是 data URL 或 HTTP URL，则原样返回；否则按 type 重构成标准 data URL。
    Batch 4.2: 修复前端 base64 裸串直接当 URL 导致的 vision 消息格式错误。
    """
    data = att.get("data", "")
    if isinstance(data, str) and data.startswith(("data:", "http://", "https://")):
        return data
    mime = att.get("type", "image/png")
    return f"data:{mime};base64,{data}"


@dataclass
class AgentCapability:
    supports_stream: bool = True
    supports_tools: bool = False
    supports_browser: bool = False
    supports_interrupt: bool = False
    supports_image: bool = False
    supports_memory: bool = False


class AgentInvoker(Protocol):
    async def invoke(self, prompt: str, context: dict[str, Any] | None = None) -> dict[str, Any]: ...
    async def stream(self, prompt: str, context: dict[str, Any] | None = None) -> AsyncIterator[dict]: ...
    async def health(self) -> bool: ...
    def capabilities(self) -> AgentCapability: ...


class BaseProcessAdapter:
    """Agent 子进程管理基类。
    # 外部 Agent（网关/CLI）标记：Provider 降级链只对原生 Agent 生效。
    is_external: bool = False

    子类只需实现 invoke/stream/health/capabilities。
    进程生命周期（start/stop/watchdog/stdout 读取）由基类统一管理。
    """

    def __init__(self, cmd: list[str], agent_id: str, port_hint: int = 0):
        self.agent_id = agent_id
        self.cmd = [str(x) for x in cmd]
        self.port_hint = port_hint
        self.process: subprocess.Popen | None = None
        self._ready = asyncio.Event()
        self._read_queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._dropped_logs = 0
        # Phase 48 T2-4: 保存后台 task 引用 + done_callback 记录异常
        self._watchdog_task: asyncio.Task | None = None
        self._read_stdout_task: asyncio.Task | None = None
        # P0-1.4: stderr reader task——原 stderr=PIPE 但无 reader，PIPE 缓冲区满会阻塞子进程
        self._read_stderr_task: asyncio.Task | None = None
        self._stopping: bool = False  # Phase 48 T2-4: 主动 stop 标志，watchdog 据此决定是否重启
        # Batch 3a A1: start() 单飞锁——防并发 start() 双 spawn（对标 VS Code LSP client
        # single-flight 启动）。autowarm 超时取消后用户首请求再 start / healer 同时进入时，
        # 无锁会 spawn 第二个进程，前者成孤儿占端口。
        self._start_lock = asyncio.Lock()
        # Phase 67: 人类可读显示名称——子类覆盖。空串时 list_agents() 回退到 agent_id.capitalize()
        self.display_name: str = ""

    async def start(self):
        """启动子进程并等待就绪（幂等 + 单飞锁）"""
        async with self._start_lock:
            # Batch 3a A1: 幂等——已在运行且就绪直接返回，防止重复 spawn 双进程
            if self.process and self.process.returncode is None and self._ready.is_set():
                return
            # Task 6.15: 重置停止标志——stop() 设 _stopping=True 阻止 watchdog 重启，
            # start() 必须重置为 False，否则 stop 后 start 的新 watchdog 在进程崩溃时
            # 检查 _stopping=True 会直接 return 不重启（子进程崩溃后永久不可用）。
            self._stopping = False
            await self._spawn_and_wait()
            # Phase 48 T2-4: 保存 task 引用 + done_callback（原裸 create_task 异常被静默吞咽）
            self._watchdog_task = asyncio.create_task(self._watchdog())
            self._read_stdout_task = asyncio.create_task(self._read_stdout())
            # P0-1.4: 启动 stderr reader——防止 PIPE 缓冲区满阻塞子进程
            self._read_stderr_task = asyncio.create_task(self._read_stderr())
            self._attach_task_callbacks()

    async def _spawn_and_wait(self):
        """Phase 48 T2-4: 创建子进程并等待就绪——start() 与 _watchdog 重启共用。

        提取自 start()，使 _watchdog 重启时不再创建新的 watchdog task（避免嵌套）。
        """
        import sys
        from yuncat_utils.process import find_free_port
        port = self.port_hint if self.port_hint else find_free_port()
        resolved_cmd = [arg.replace("{port}", str(port)) for arg in self.cmd]
        # Windows: .bat 文件必须通过 cmd /c 执行
        if sys.platform == "win32" and resolved_cmd and resolved_cmd[0].endswith(".bat"):
            resolved_cmd = ["cmd", "/c"] + resolved_cmd
        self._port = port
        # 允许子类通过 _get_env() 注入自定义环境变量
        env = {**os.environ, "YUNCAT_PARENT_PID": str(os.getpid())}
        if hasattr(self, "_get_env"):
            custom_env = self._get_env()
            env.update(custom_env)
        self.process = await asyncio.create_subprocess_exec(
            *resolved_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        # Batch 3a A1: spawn 之后 watchdog 尚未创建——取消/超时路径若不清理，
        # 子进程成孤儿占端口（外层 autowarm wait_for 超时即走此路径）。
        try:
            # v56: Windows Job Object 沙箱绑定
            self._apply_job_object()
            self._ready.clear()
            deadline = asyncio.get_event_loop().time() + ADAPTER_READY_MAX_WAIT_SECONDS
            while asyncio.get_event_loop().time() < deadline:
                if await self.health():
                    self._ready.set()
                    return
                await asyncio.sleep(ADAPTER_READY_POLL_INTERVAL_SECONDS)
            await self._terminate_spawned("start timed out")
            raise TimeoutError(
                f"Adapter {self.agent_id} failed to become ready within "
                f"{ADAPTER_READY_MAX_WAIT_SECONDS}s"
            )
        except asyncio.CancelledError:
            await self._terminate_spawned("start cancelled")
            raise

    async def _terminate_spawned(self, reason: str) -> None:
        """Batch 3a A1: best-effort 终止已 spawn 但未纳入 watchdog 管理的子进程。"""
        if not self.process or self.process.returncode is not None:
            return
        _log.warning("Adapter %s %s, terminating orphan process (pid=%s)",
                     self.agent_id, reason, self.process.pid)
        try:
            self.process.terminate()
            await asyncio.wait_for(self.process.wait(),
                                   timeout=ADAPTER_PROCESS_WAIT_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            self.process.kill()

    def _attach_task_callbacks(self):
        """Phase 48 T2-4: 为后台 task 加 done_callback 记录异常（防静默吞咽）。"""
        # P0-1.4: 加入 read_stderr task 的回调
        for name, task in (("watchdog", self._watchdog_task),
                            ("read_stdout", self._read_stdout_task),
                            ("read_stderr", self._read_stderr_task)):
            if task is None or task.done():
                continue

            def _cb(t, _name=name):
                if t.exception():
                    _log.error("Adapter %s %s task crashed: %s",
                               self.agent_id, _name, t.exception())
            task.add_done_callback(_cb)

    async def stop(self):
        """终止子进程"""
        self._stopping = True  # Phase 48 T2-4: 标记主动停止，watchdog 不重启
        # 2026-10-07 修复：
        # stop() 原来只置 _stopping，不清 _ready——而 status 完全由 _ready 派生
        # （list_agents: "running" if adapter._ready.is_set() else "stopped"）。
        # 结果是「点停止」后进程已被 kill、UI 却仍显示运行中，且与 stop_agent
        # 推送的 "stopped" 永久互相矛盾（实测：stop 后 _ready 仍为 True）。
        # 对标 Docker/systemd：停止完成即进入非就绪态。
        self._ready.clear()
        # Batch A 修复: 关闭继承的 aiohttp session，防止资源泄漏
        if self._session and not self._session.closed:
            await self._session.close()
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                # P2-2.3: 用 config 常量替代硬编码 5s（子进程退出等待）
                await asyncio.wait_for(self.process.wait(),
                                       timeout=ADAPTER_PROCESS_WAIT_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                self.process.kill()

    async def wait_ready(self, timeout: float = 60.0):
        """等待 Adapter 就绪，超时抛 TimeoutError"""
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"Adapter {self.agent_id} startup timed out after {timeout}s")

    def _apply_job_object(self) -> None:
        """v56: 将子进程绑定到 Windows Job Object（OS 级沙箱）"""
        if not self.process or not self.process.pid:
            return
        try:
            from security.job_object import create_job_object, assign_process_to_job
            job_name = f"yuncat_{self.agent_id}"
            hjob = create_job_object(job_name)
            if hjob:
                assign_process_to_job(hjob, self.process.pid)
        except ImportError:
            _log.debug("Job Object not applied for %s (non-Windows)", self.agent_id)
        except OSError as e:
            _log.warning("Job Object failed for %s: %s", self.agent_id, e)
        except Exception:
            _log.exception("Job Object unexpected error for %s", self.agent_id)

    async def _watchdog(self):
        """监控进程状态——退出时清除就绪标志并尝试重启（指数退避 + 上限）。

        Phase 48 T2-4: 原 _watchdog 仅 _ready.clear() 不重启，子进程崩溃后永久不可用。
        现按 Docker restart policy / VS Code extension host respawn 模式：
          - 指数退避 5s/10s/20s/40s/60s
          - 滑动窗口 1h 内最多 5 次重启（防崩溃循环）
          - 主动 stop（self._stopping=True）不重启
        重启时调 _spawn_and_wait()（不创建新 watchdog task，避免嵌套）+
        重启 _read_stdout task（指向新 process 的 stdout）。
        """
        import time as _time
        restart_delays = ADAPTER_RESTART_DELAYS
        restart_count = 0
        restart_window_start = _time.time()
        max_restarts_per_hour = ADAPTER_RESTART_MAX_PER_HOUR

        while True:
            # 等待进程退出
            while self.process and self.process.returncode is None:
                # P2-2.3: 用 config 常量替代硬编码 5s（poll interval）
                await asyncio.sleep(ADAPTER_RESTART_BACKOFF_SEC)

            self._ready.clear()
            # P0-1.5: 进程退出后立即失效 health 缓存——
            # 否则 health() 60s 内仍返回旧缓存值（True），watchdog 检测到崩溃但对外仍报健康
            self._last_health_check = 0.0

            # 主动 stop → 不重启
            if self._stopping:
                return
            if self.process is None:
                return  # 无进程可监控

            # 重启上限检查（滑动窗口 1 小时）
            now = _time.time()
            if now - restart_window_start > 3600:
                restart_count = 0
                restart_window_start = now
            if restart_count >= max_restarts_per_hour:
                _log.error("Adapter %s crashed %d times in 1h, giving up restart",
                           self.agent_id, restart_count)
                # P0-1.11: 推送 adapter_degraded WS 事件——前端 toast + 状态栏提示
                # 对标 Docker restart policy 耗尽后状态标记 unhealthy
                try:
                    from context import get_app
                    app = get_app()
                    if app and hasattr(app.state, "ws_manager"):
                        await app.state.ws_manager.broadcast({
                            "type": "adapter_degraded",
                            "agent_id": self.agent_id,
                            "reason": "restart_limit_exhausted",
                            "restart_count": restart_count,
                            "max_restarts_per_hour": max_restarts_per_hour,
                        })
                except Exception:
                    _log.warning("Failed to push adapter_degraded WS event", exc_info=True)
                return

            # 指数退避
            delay = restart_delays[min(restart_count, len(restart_delays) - 1)]
            restart_count += 1
            _log.warning("Adapter %s exited (code=%s), restarting in %ds (attempt %d/%d)",
                         self.agent_id, self.process.returncode, delay,
                         restart_count, max_restarts_per_hour)
            await asyncio.sleep(delay)

            # 重启：重新 spawn + 重启 _read_stdout / _read_stderr task
            try:
                await self._spawn_and_wait()
                # P0-1.5: 重启后清 health 缓存（port 可能变更，旧缓存指向死端口）
                self._last_health_check = 0.0
                if self._read_stdout_task and not self._read_stdout_task.done():
                    self._read_stdout_task.cancel()
                self._read_stdout_task = asyncio.create_task(self._read_stdout())
                # P0-1.4: 同步重启 stderr reader
                if self._read_stderr_task and not self._read_stderr_task.done():
                    self._read_stderr_task.cancel()
                self._read_stderr_task = asyncio.create_task(self._read_stderr())
                self._attach_task_callbacks()
                _log.info("Adapter %s restarted successfully", self.agent_id)
            except Exception as e:
                _log.error("Adapter %s restart failed: %s", self.agent_id, e)

    async def _read_stdout(self):
        """v45: 有界队列 + sentinel 防永久阻塞"""
        try:
            while self.process and self.process.stdout:
                line = await self.process.stdout.readline()
                if not line:
                    break
                decoded = line.decode(errors="replace").strip()
                if not decoded:
                    continue
                try:
                    self._read_queue.put_nowait(decoded)
                except asyncio.QueueFull:
                    try:
                        self._read_queue.get_nowait()
                        self._read_queue.task_done()
                        self._read_queue.put_nowait(decoded)
                    except (asyncio.QueueFull, asyncio.QueueEmpty):
                        if self._dropped_logs == 0:
                            try:
                                self._read_queue.put_nowait(
                                    "[系统提示] Agent 输出过快，部分中间日志已丢弃以保护内存"
                                )
                            except asyncio.QueueFull:
                                pass
                        self._dropped_logs += 1
                        _log.warning(
                            f"Adapter {self.agent_id}: double-drop in buffer overflow "
                            f"(total dropped: {self._dropped_logs})"
                        )
                    if self._dropped_logs % 100 == 0:
                        _log.warning(
                            f"Adapter {self.agent_id}: dropped {self._dropped_logs} log lines"
                        )
        finally:
            try:
                self._read_queue.put_nowait(None)
            except asyncio.QueueFull:
                self._read_queue.get_nowait()
                self._read_queue.put_nowait(None)

    async def _read_stderr(self):
        """P0-1.4: 读取子进程 stderr——防止 PIPE 缓冲区满阻塞子进程。

        原 stderr=subprocess.PIPE 但无 reader task，当 stderr 缓冲区（默认 64KB）
        被写满时，子进程的 stderr 写入会阻塞，导致整个子进程挂起。
        对标 Docker logs / Node.js child_process 的 stderr 消费模式。

        stderr 行不进入 _read_queue（与 stdout 分离），仅记日志——
        避免错误日志污染 stdout 解析逻辑（SSE/JSON 解析）。
        """
        try:
            while self.process and self.process.stderr:
                line = await self.process.stderr.readline()
                if not line:
                    break
                decoded = line.decode(errors="replace").strip()
                if not decoded:
                    continue
                # stderr 通常含诊断信息（stack trace / warning）——记 warning 级别日志
                _log.warning("Adapter %s stderr: %s", self.agent_id, decoded)
        except Exception:
            _log.debug("Adapter %s stderr reader exited", self.agent_id, exc_info=True)


# ============================================================
# OpenAiCompatibleAdapter —— OpenAI 协议 HTTP 适配器基类
# HermesAdapter 和 OpenClawAdapter 共享 SSE 解析 + session 管理
# ============================================================
import json as _json
import aiohttp

# M28 修复：共享 SSE 流解析工具——替代各适配器内联的 buffer+line+data: 逻辑
from adapters.sse_utils import iter_sse_events
from services.llm_usage import normalize_usage


def _parse_db_tool_msg(messages: list, content: str) -> None:
    """从 DB 加载的 tool 消息——content 可能是 JSON 编码的 {tool_call_id, result}。
    解析成功则提取 tool_call_id；否则作为普通 tool 消息追加。"""
    try:
        data = _json.loads(content)
        if isinstance(data, dict) and "tool_call_id" in data:
            messages.append({
                "role": "tool",
                "tool_call_id": data["tool_call_id"],
                "content": data.get("result", data.get("content", "")),
            })
            return
    except (_json.JSONDecodeError, ValueError):
        pass
    messages.append({"role": "tool", "content": content})


class OpenAiCompatibleAdapter(BaseProcessAdapter, AgentInvoker):
    """OpenAI 兼容 HTTP 协议适配器基类。

    子类仅需定义 cmd, agent_id, MODEL, HEALTH_PATH。
    """

    MODEL: str = "default"
    HEALTH_PATH: str = "/health"

    def __init__(self, cmd: list[str], agent_id: str, port_hint: int = 0):
        super().__init__(cmd, agent_id, port_hint)
        self._port: int = port_hint
        self._session: aiohttp.ClientSession | None = None
        # Batch 3a A4: 在飞请求计数——cancel() 据此避免 close 共享 session 误伤并发任务
        self._active_requests = 0
        # cswitch 风格 Token 统计——从 API 响应真实 usage 字段获取
        self.last_usage: dict[str, int] = {}

    @property
    def _base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def health(self) -> bool:
        if not self.process or self.process.returncode is not None:
            return False
        try:
            await self._ensure_session()
            async with self._session.get(
                f"{self._base_url}{self.HEALTH_PATH}",
                timeout=aiohttp.ClientTimeout(total=ADAPTER_HEALTH_CHECK_TIMEOUT),
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _build_messages(self, context: dict[str, Any] | None, prompt: str) -> list[dict]:
        """Batch B M2: 提取自 invoke/stream 的重复消息构建逻辑。

        构建 system + memory + history + user prompt 消息列表。
        图片附件自动转为 Vision API image_url 格式。
        """
        messages: list[dict] = []
        if context and context.get("system"):
            messages.append({"role": "system", "content": context["system"]})
        if context and context.get("memory"):
            messages.append({"role": "system", "content": context["memory"]})
        # Claude Code 风格：全量对话历史 + tool_call_id 正确传递（按上下文窗口动态截断）
        history = (context or {}).get("history", [])
        max_turns = _resolve_history_limit(context)
        # Batch 3a A5: 截断点落在 assistant(tool_calls) 与 tool 结果之间时，
        # 孤立 tool 消息开头会导致 OpenAI 兼容 API 400——其配对 assistant 已被
        # 切断，必须一并丢弃（从头部持续丢弃直到首条非 tool）。
        sliced = history[-max_turns:]
        while sliced and sliced[0].get("role") == "tool":
            sliced = sliced[1:]
        seen = set()
        for h in sliced:
            mid = h.get("id", "")
            if mid and mid in seen: continue
            if mid: seen.add(mid)
            role = h.get("role", "user")
            if role in ("user", "assistant"):
                content = h.get("content", "")
                tool_calls = h.get("tool_calls")
                msg: dict = {"role": role, "content": content}
                if tool_calls:
                    msg["tool_calls"] = tool_calls
                rc = h.get("reasoning_content", "")
                if rc:
                    msg["reasoning_content"] = rc
                messages.append(msg)
            elif role == "tool":
                tc_id = h.get("tool_call_id", "")
                if tc_id:
                    messages.append({"role": "tool", "tool_call_id": tc_id, "content": h.get("content", "")})
                else:
                    _parse_db_tool_msg(messages, h.get("content", ""))
        if prompt:
            # Phase 25: 图片附件 → Vision API image_url 格式
            attachments = (context or {}).get("attachments", [])
            has_images = any(att.get("type", "").startswith("image/") for att in attachments)
            if has_images:
                messages.append({
                    "role": "user",
                    "content": _build_image_user_content(
                        prompt, attachments, (context or {}).get("model_config"),
                    ),
                })
            else:
                messages.append({"role": "user", "content": prompt})
        return messages

    async def invoke(
        self, prompt: str, context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self.wait_ready()
        await self._ensure_session()
        # Phase 31 fix: use model from context config, not hardcoded self.MODEL
        model_cfg = (context or {}).get("model_config", {})
        model_name = model_cfg.get("model", self.MODEL)
        messages = self._build_messages(context, prompt)
        payload = {"model": model_name, "messages": messages, "stream": False}
        # Phase 47: 注入工具定义(如有)——OC Gateway 路径需要 delegate_to_agent
        _tools = (context or {}).get("tools")
        if _tools:
            payload["tools"] = _tools
        # 允许调用方通过 context 覆盖 max_tokens / temperature
        if context:
            if "max_tokens" in context:
                payload["max_tokens"] = context["max_tokens"]
            if "temperature" in context:
                payload["temperature"] = context["temperature"]
        # ---- Batch 8.4: LLM 响应缓存（仅非工具调用，精确哈希，默认关闭）----
        from services.llm_cache import get_global_cache
        cache = get_global_cache()
        if not _tools:
            cached = await cache.get(model_name, messages)
            if cached is not None:
                return cached
        # Batch 3a A4: 在飞请求计数（cache 命中不发请求，不计数）
        self._active_requests += 1
        try:
            async with self._session.post(
                f"{self._base_url}/v1/chat/completions",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=ADAPTER_INVOKE_TIMEOUT),
            ) as resp:
                data = await resp.json()
                message = data.get("choices", [{}])[0].get("message", {})
                # Phase C: 非流式 token 统计——与 stream 路径对称（缺口修复：
                # 原 invoke 不捕获 usage，非流式对话的 token 预算形同虚设）
                if isinstance(data.get("usage"), dict):
                    # 统一口径（OpenAI 嵌套明细 / DeepSeek 平铺），含 cached/reasoning tokens
                    self.last_usage = normalize_usage(data["usage"])
                    try:
                        from harness.budget_manager import accumulate_tokens

                        accumulate_tokens(self.last_usage)
                    except Exception:
                        _log.debug("%s budget accumulate skipped", self.agent_id, exc_info=True)
                # 缓存成功响应（非工具调用、有 message、无 error）
                if not _tools and message and "error" not in data:
                    # Batch B M1: cache.put 失败不应阻止返回响应
                    try:
                        await cache.put(model_name, messages, message)
                    except Exception:
                        _log.warning("%s cache.put failed", self.agent_id, exc_info=True)
                return message
        except Exception as e:
            _log.error(f"{self.agent_id} invoke failed: {e}")
            # DSH 借鉴: 稳定错误码——上层 route on code 而非解析 message
            from services.llm_errors import classify_llm_error
            return {"error": str(e), "error_code": classify_llm_error(str(e))}
        finally:
            self._active_requests -= 1

    async def stream(
        self, prompt: str, context: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict]:
        """Phase 20: stream 输出从 str 升级为结构化 dict。
        chunk 格式: {"type": "text"|"thinking"|"tool_call", "data": ...}
        """
        await self.wait_ready()
        await self._ensure_session()
        # Batch B M2: 使用统一的 _build_messages 替代内联消息构建
        messages = self._build_messages(context, prompt)
        # Phase 31 fix: use model from context config, not hardcoded self.MODEL
        model_cfg = (context or {}).get("model_config", {})
        model_name = model_cfg.get("model", self.MODEL)
        payload = {"model": model_name, "messages": messages, "stream": True}
        # Phase 47: 注入工具定义(如有)——OC Gateway 路径需要 delegate_to_agent
        _tools = (context or {}).get("tools")
        if _tools:
            payload["tools"] = _tools
        # 重置 usage 统计
        self.last_usage = {}
        # Batch 3a A4: 在飞请求计数——覆盖整个 yield 生命周期（finally 在生成器
        # 结束/被关闭时执行），cancel() 据此避免 close 共享 session 误伤并发流。
        self._active_requests += 1
        try:
            async with self._session.post(
                f"{self._base_url}/v1/chat/completions",
                json=payload,
                # Batch 3a A3: 流式用空闲超时（sock_read）而非 total——
                # 长推理整体时长可达数十分钟，total 会整体掐断；chunk 间
                # 120s 无数据即判定连接已死。
                timeout=aiohttp.ClientTimeout(
                    total=None, sock_connect=10,
                    sock_read=ADAPTER_STREAM_IDLE_TIMEOUT),
            ) as resp:
                # M28 修复：SSE 行解析提取到 adapters/sse_utils.iter_sse_events
                async for data in iter_sse_events(resp):
                    if data is None:  # [DONE]
                        return
                    choice = data.get("choices", [{}])[0]
                    delta = choice.get("delta", {})
                    # Phase 20: 思维链（DeepSeek R1/V3/V4 等推理模型）
                    reasoning = delta.get("reasoning_content", "")
                    if reasoning:
                        yield {"type": "thinking", "data": reasoning}
                    # Phase 20: 工具调用声明
                    tool_calls = delta.get("tool_calls", [])
                    if tool_calls:
                        yield {"type": "tool_call", "data": tool_calls}
                    # 普通文本
                    content = delta.get("content", "")
                    if content:
                        yield {"type": "text", "data": content}
                    # Batch 5a C1: finish_reason 透传（[DONE] 前最后 chunk 携带）——
                    # length 时说明 max_tokens 截断，消费者可提示用户，不再静默半截回答
                    finish_reason = choice.get("finish_reason") or ""
                    if finish_reason:
                        yield {"type": "finish", "data": finish_reason}
                    # cswitch 风格: 捕获 usage（最后一个 chunk 通常包含）
                    if "usage" in data:
                        self.last_usage = normalize_usage(data.get("usage"))
                        # Phase C: 预算 token 统计（harness attempt 上下文内生效；非 harness no-op）
                        try:
                            from harness.budget_manager import accumulate_tokens

                            accumulate_tokens(self.last_usage)
                        except Exception:
                            _log.debug("%s budget accumulate skipped", self.agent_id, exc_info=True)
        except Exception as e:
            _log.error(f"{self.agent_id} stream failed: {e}")
            # DSH 借鉴: 稳定错误码——上层 route on code 而非解析 message
            from services.llm_errors import classify_llm_error
            yield {"type": "stream_error", "data": str(e),
                   "error_code": classify_llm_error(str(e))}
        finally:
            self._active_requests -= 1

    async def cancel(self, task_id: str | None = None):
        # Batch 3a A4: 有在飞请求时仅记日志不关闭——共享 session 正被并发任务
        # 使用，直接 close 会误伤同 adapter 上其它在飞请求（断连）。
        # 为 0 时才关闭。stop() 行为不变（强杀，不经过此处）。
        if self._active_requests > 0:
            _log.debug("Adapter %s cancel skipped: %d active request(s) using session",
                       self.agent_id, self._active_requests)
            return
        if self._session and not self._session.closed:
            await self._session.close()


# ============================================================
# Phase 38: 共享消息构建函数 — 从 hermes.py _build_messages 提取
# ============================================================

def build_openai_messages(
    prompt: str | None,
    context: dict | None = None,
    *,
    include_system: bool = True,
    include_memory: bool = True,
    include_history: bool = True,
    inject_tool_calls: bool = True,
) -> list[dict]:
    """构建标准 OpenAI messages 数组。所有 adapter 共享此实现。

    prompt 为 None 时仅注入 history（Tool 轮），不追加 user 消息。
    """
    messages: list[dict] = []
    ctx = context or {}

    if include_system and ctx.get("system"):
        messages.append({"role": "system", "content": ctx["system"]})
    if include_memory and ctx.get("memory"):
        messages.append({"role": "system", "content": ctx["memory"]})

    if include_history:
        history = ctx.get("history", [])
        max_turns = _resolve_history_limit(ctx)
        # Batch 3a A5: 头部对齐——截断后首条若为孤立 tool 消息（配对 assistant
        # 已被切断），OpenAI 兼容 API 会 400，从头部持续丢弃直到首条非 tool。
        sliced = history[-max_turns:]
        while sliced and sliced[0].get("role") == "tool":
            sliced = sliced[1:]
        seen: set[str] = set()
        for h in sliced:
            mid = h.get("id", "")
            if mid and mid in seen:
                continue
            if mid:
                seen.add(mid)
            role = h.get("role", "user")
            if role in ("user", "assistant"):
                content = h.get("content", "")
                tool_calls = h.get("tool_calls") if inject_tool_calls else None
                msg: dict = {"role": role, "content": content}
                if tool_calls:
                    msg["tool_calls"] = tool_calls
                # Phase DS.2: DeepSeek V4 多轮契约——reasoning_content 必须回传
                # 修复: 之前只在有 tool_calls 时注入，纯文本 assistant 消息会丢失 reasoning
                rc = h.get("reasoning_content", "")
                if rc:
                    msg["reasoning_content"] = rc
                messages.append(msg)
            elif role == "tool":
                tc_id = h.get("tool_call_id", "")
                if tc_id:
                    messages.append({"role": "tool", "tool_call_id": tc_id, "content": h.get("content", "")})
                else:
                    _parse_db_tool_msg(messages, h.get("content", ""))

    if prompt:
        attachments = ctx.get("attachments", [])
        image_atts = [a for a in attachments if a.get("type", "").startswith("image/")]
        if image_atts:
            messages.append({
                "role": "user",
                "content": _build_image_user_content(prompt, image_atts, ctx.get("model_config")),
            })
        else:
            messages.append({"role": "user", "content": prompt})

    # M4: 发送前 token 预算强制——超限时从 history 前端裁剪
    # 对标 Codex CLI: 最终校验确保 system + history ≤ window - output
    model_cfg = ctx.get("model_config") or {}
    if model_cfg and messages:
        try:
            from config import enforce_token_budget
            messages = enforce_token_budget(messages, model_cfg)
        except Exception:
            _log.debug("M4 enforce_token_budget failed", exc_info=True)

    return messages
