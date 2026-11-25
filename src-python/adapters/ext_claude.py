"""adapters/ext_claude.py —— 外部 Claude Code CLI 连接器

调用用户安装的 Anthropic Claude Code CLI（`claude -p`，无头 print 模式）。
文档：github.com/anthropics/claude-code
"""
import asyncio
import json
import os

from adapters.ext_base import EXTERNAL_INVOKE_TIMEOUT, ExternalCliAgentAdapter
from adapters.registry import register_adapter


@register_adapter("claude")
class ClaudeExternalAdapter(ExternalCliAgentAdapter):
    cli_name = "claude"
    agent_label = "Claude Code"

    def build_args(self, prompt: str, context: dict | None) -> list[str]:
        """claude -p --output-format stream-json --permission-mode <mode> "<prompt>"

        - print 模式（无交互）
        - stream-json 输出便于逐行流式解析
        - permission-mode 默认 acceptEdits：允许改文件但需确认高风险；
          若 context["full_auto"] 为真则 bypassPermissions（需用户显式授权）。
        """
        ctx = context or {}
        full_auto = ctx.get("full_auto", False)
        mode = "bypassPermissions" if full_auto else "acceptEdits"
        return [
            "-p",
            "--output-format", "stream-json",
            "--permission-mode", mode,
            prompt,
        ]

    async def stream(self, prompt: str, context: dict | None = None) -> "asyncio.AsyncIterator[dict]":
        """重写 stream：解析 Claude 的 stream-json 行协议。"""
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
                try:
                    evt = json.loads(line.decode(errors="replace").strip())
                except json.JSONDecodeError:
                    yield {"type": "text", "data": line.decode(errors="replace")}
                    continue
                for chunk in self._translate_event(evt):
                    yield chunk
            await proc.wait()
            yield {"type": "finish",
                   "data": f"exit_{proc.returncode}" if proc.returncode else "stop"}
        except Exception as e:  # noqa: BLE001
            yield {"type": "stream_error", "data": str(e)}

    @staticmethod
    def _translate_event(evt: dict):
        """把 Claude stream-json 事件翻译为 Yuncat 的流式片段。"""
        etype = evt.get("type")
        if etype == "assistant":
            msg = evt.get("message", {})
            for block in msg.get("content", []):
                if block.get("type") == "text":
                    yield {"type": "text", "data": block.get("text", "")}
                elif block.get("type") == "tool_use":
                    yield {"type": "tool_call",
                           "data": f"{block.get('name')}({json.dumps(block.get('input', {}), ensure_ascii=False)})"}
        elif etype == "result":
            yield {"type": "text", "data": evt.get("result", "")}
        # system / stream_event 等无需翻译
