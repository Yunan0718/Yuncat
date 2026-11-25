"""adapters/ext_codex.py —— 外部 Codex CLI 连接器

调用用户安装的 OpenAI Codex CLI（`codex exec`，无头模式）。
文档：github.com/openai/codex
"""
from adapters.ext_base import ExternalCliAgentAdapter
from adapters.registry import register_adapter


@register_adapter("codex")
class CodexExternalAdapter(ExternalCliAgentAdapter):
    cli_name = "codex"
    agent_label = "Codex"

    def build_args(self, prompt: str, context: dict | None) -> list[str]:
        """codex exec "<prompt>"。

        --full-auto 跳过审批直接写文件/执行（仅当用户在 context 显式允许）。
        默认仍走 Codex 沙箱（只读/工作区内写文件需确认）。
        """
        full_auto = (context or {}).get("full_auto", True)
        args = ["exec"]
        if full_auto:
            args.append("--full-auto")
        args.append(prompt)
        return args
