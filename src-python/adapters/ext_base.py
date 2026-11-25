"""adapters/ext_base.py —— 外部 agent CLI 连接器基类

外部 agent（Codex / Claude Code / Hermes 等）由用户自行安装并在本机可用，
Yuncat 通过**无头 CLI** 调用它们完成委派任务，捕获其输出作为结果返回。

设计要点：
- 不维持长驻子进程——每次 invoke/stream 拉起一个短生命周期 CLI 进程。
- 可用性由 health() 经 shutil.which 检测；不可用时委派自动跳过该 agent。
- 危险操作由外部 agent 自身负责（它们有各自审批/沙箱）；Yuncat 在此层只做编排。
- 外部 agent 使用它们自己的密钥（从继承的环境变量读取），Yuncat 不注入密钥。
"""
import asyncio
import logging
import os
import shutil
from typing import Any, AsyncIterator

from adapters.base import AgentCapability, AgentInvoker, BaseProcessAdapter

_log = logging.getLogger("yuncat.adapter.external")

EXTERNAL_INVOKE_TIMEOUT = 600  # 外部 agent 完整执行任务可能耗时较长（秒）


class ExternalCliAgentAdapter(BaseProcessAdapter, AgentInvoker):
    """通用外部 CLI agent 基类。子类只需定义 cli_name 与 build_args()。"""

    is_external: bool = True
    cli_name: str = ""     # 可执行文件名，如 "codex" / "claude" / "hermes"
    agent_label: str = ""  # 显示名

    def __init__(self, cmd=None, agent_id: str = "", port_hint: int = 0):
        super().__init__(cmd or [], agent_id or self.cli_name, port_hint)
        self.display_name = self.agent_label or self.cli_name
        self._active_requests = 0

    # ---- 子类实现 ----
    def build_args(self, prompt: str, context: dict | None) -> list[str]:
        raise NotImplementedError

    def _cwd(self, context: dict | None) -> str:
        return (context or {}).get("cwd") or os.getcwd()

    # ---- 生命周期（无长驻进程）----
    async def start(self) -> None:
        async with self._start_lock:
            self._stopping = False
            self._ready.set()  # 适配器自身就绪；实际可用性由 health() 决定
            _log.info("External agent '%s' adapter ready (CLI=%s)", self.agent_id, self.cli_name)

    async def stop(self) -> None:
        self._stopping = True
        # 2026-10-07 修复：同 BaseProcessAdapter.stop——不清 _ready 会让
        # 「已停止」在 UI 上仍显示运行中（status 由 _ready 派生）。
        self._ready.clear()

    async def health(self) -> bool:
        return bool(self.cli_name) and shutil.which(self.cli_name) is not None

    def capabilities(self) -> AgentCapability:
        return AgentCapability(
            supports_stream=True, supports_tools=False,
            supports_browser=False, supports_interrupt=False,
            supports_image=False, supports_memory=False,
        )

    # ---- 调用 ----
    async def invoke(self, prompt: str, context: dict | None = None) -> dict[str, Any]:
        if not await self.health():
            return {"error": f"外部 agent '{self.cli_name}' 未安装或不在 PATH 中"}
        args = self.build_args(prompt, context)
        try:
            proc = await asyncio.create_subprocess_exec(
                self.cli_name, *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd(context), env={**os.environ},
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=EXTERNAL_INVOKE_TIMEOUT)
            out = (stdout or b"").decode(errors="replace").strip()
            err = (stderr or b"").decode(errors="replace").strip()
            if proc.returncode != 0 and not out:
                return {"error": f"{self.cli_name} 退出码 {proc.returncode}: {err[:500]}"}
            return {"role": "assistant", "content": out or err}
        except asyncio.TimeoutError:
            return {"error": f"{self.cli_name} 执行超时（>{EXTERNAL_INVOKE_TIMEOUT}s）"}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    async def stream(self, prompt: str, context: dict | None = None) -> AsyncIterator[dict]:
        if not await self.health():
            yield {"type": "stream_error",
                   "data": f"外部 agent '{self.cli_name}' 未安装或不在 PATH 中"}
            return
        args = self.build_args(prompt, context)
        try:
            proc = await asyncio.create_subprocess_exec(
                self.cli_name, *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd(context), env={**os.environ},
            )
            assert proc.stdout
            while True:
                line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=EXTERNAL_INVOKE_TIMEOUT)
                if not line:
                    break
                yield {"type": "text", "data": line.decode(errors="replace")}
            await proc.wait()
            yield {"type": "finish",
                   "data": f"exit_{proc.returncode}" if proc.returncode else "stop"}
        except Exception as e:  # noqa: BLE001
            yield {"type": "stream_error", "data": str(e)}

    async def cancel(self, task_id: str | None = None) -> None:
        return None
