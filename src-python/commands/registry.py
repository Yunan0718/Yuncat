"""commands/registry.py —— Slash Command 注册表（Phase 27）

借鉴 Claude Code 的 /command 系统——每个命令 YAML 定义：
  name, slash_command, description, agent_routing, workflow_template, arguments

加载逻辑:
  1. 先加载 commands/builtin/*.yaml（12 条内置命令）
  2. 再加载用户自定义目录（YUNCAT_DATA_DIR/commands，默认 ~/.yuncat/commands）
  3. 用户命令覆盖内置命令（同 slash_command 时）
"""
import logging
import os
import re

_log = logging.getLogger("yuncat.commands")

BUILTIN_DIR = os.path.join(os.path.dirname(__file__), "builtin")


class SlashCommand:
    """单个 Slash Command 定义"""
    def __init__(self, data: dict):
        self.name: str = data.get("name", "")
        self.slash_command: str = data.get("slash_command", "")
        self.description: str = data.get("description", "")
        self.agent_routing: dict = data.get("agent_routing", {})
        self.workflow_template: str = data.get("workflow_template", "")
        self.arguments: list[dict] = data.get("arguments", [])
        self.category: str = data.get("category", "general")
        # Phase 64: local=True 表示前端本地拦截（不走后端 start_task）
        # 例如 /clear /compact /model /agent /help
        self.local: bool = data.get("local", False)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "slash_command": self.slash_command,
            "description": self.description,
            "category": self.category,
            "arguments": self.arguments,
            "local": self.local,
        }

    def extract_args(self, input_text: str) -> dict:
        """从输入中提取命名参数和位置参数"""
        args = {}
        # 去掉 slash command 前缀
        text = input_text.replace(self.slash_command, "", 1).strip()
        # 命名参数: --key value 或 -k value
        for m in re.finditer(r'(?:--?)(\w+)\s+([^\s-][^\s]*)', text):
            args[m.group(1)] = m.group(2)
            text = text.replace(m.group(0), "", 1)
        # 剩余文本作为位置参数
        remaining = text.strip()
        if remaining:
            for i, arg_def in enumerate(self.arguments):
                if arg_def.get("name") not in args:
                    parts = remaining.split(None, 1)
                    args[arg_def["name"]] = parts[0] if parts else remaining
                    if parts and len(parts) > 1:
                        remaining = parts[1]
                    else:
                        remaining = ""
        return args


class SlashCommandRegistry:
    """Slash Command 注册表——从 YAML 加载 + 用户自定义覆盖"""

    def __init__(self):
        self._commands: dict[str, SlashCommand] = {}  # slash_command → SlashCommand

    def load_builtin(self) -> None:
        """从 commands/builtin/ 加载 12 条内置命令"""
        if not os.path.isdir(BUILTIN_DIR):
            _log.warning("Builtin commands directory not found: %s", BUILTIN_DIR)
            return
        for fname in sorted(os.listdir(BUILTIN_DIR)):
            if not fname.endswith(".yaml"):
                continue
            try:
                self._load_file(os.path.join(BUILTIN_DIR, fname))
            except Exception:
                _log.exception("Failed to load builtin command: %s", fname)

    def load_user(self, user_dir: str) -> None:
        """从用户目录加载自定义命令（覆盖内置）"""
        if not os.path.isdir(user_dir):
            return
        for fname in sorted(os.listdir(user_dir)):
            if not fname.endswith(".yaml"):
                continue
            try:
                self._load_file(os.path.join(user_dir, fname), override=True)
            except Exception:
                _log.exception("Failed to load user command: %s", fname)

    def _load_file(self, path: str, override: bool = False) -> None:
        import yaml
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        cmd = SlashCommand(data)
        if cmd.slash_command in self._commands and not override:
            _log.debug("Command '%s' already registered, skipping", cmd.slash_command)
            return
        self._commands[cmd.slash_command] = cmd

    def match(self, input_text: str) -> SlashCommand | None:
        """匹配输入中的 slash command——返回最长匹配"""
        best: SlashCommand | None = None
        best_len = 0
        for sc, cmd in self._commands.items():
            if input_text.strip().startswith(sc):
                if len(sc) > best_len:
                    best = cmd
                    best_len = len(sc)
        return best

    def list_all(self) -> list[dict]:
        return [cmd.to_dict() for cmd in self._commands.values()]

    def get(self, slash_command: str) -> SlashCommand | None:
        return self._commands.get(slash_command)


# 全局单例
_registry: SlashCommandRegistry | None = None


def user_commands_dir() -> str:
    """用户自定义命令目录：<YUNCAT_DATA_DIR>/commands（默认 ~/.yuncat/commands）"""
    base = os.environ.get("YUNCAT_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".yuncat")
    return os.path.join(base, "commands")


def get_registry() -> SlashCommandRegistry:
    global _registry
    if _registry is None:
        _registry = SlashCommandRegistry()
        _registry.load_builtin()
        # 2026-09-16: load_user() 之前从没被调用过，用户自定义命令永远加载不进来
        _registry.load_user(user_commands_dir())
    return _registry
