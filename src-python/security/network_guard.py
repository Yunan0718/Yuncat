"""security/network_guard.py —— Round 1 S7 网络隔离守卫

按场景来（不一刀切禁网）——三档模式：
- off（默认）：不禁网，保持当前行为不破坏用户
- warn：允许但 _log.warning 告警
- block：拒绝网络相关命令

对标主流：
- Claude Code：危险命令二次确认
- Docker/容器：网络隔离策略
- Cursor：扩展网络权限控制

设计要点：
- 模式通过 set_network_mode() 注入（lifespan 从 settings 读取后注入）
- is_network_allowed() 纯函数，不访问 DB——热路径零 IO
- 默认 off：用户可在设置里升级到 warn/block
"""
import logging
import re

_log = logging.getLogger("yuncat.security.network_guard")

# 网络模式：off / warn / block
_network_mode: str = "off"

# 网络相关命令模式（首 token + 子命令判定）
# 这些命令会发起网络请求（下载包 / 推拉代码 / 访问 registry）
_NETWORK_COMMAND_PATTERNS: list[tuple[re.Pattern, str]] = [
    # 包管理器——install/add/download 会从 registry 拉包
    (re.compile(r"^\s*pip\s+(install|download)\b", re.IGNORECASE), "pip install/download（从 PyPI 下载）"),
    (re.compile(r"^\s*pip3\s+(install|download)\b", re.IGNORECASE), "pip3 install/download（从 PyPI 下载）"),
    (re.compile(r"^\s*npm\s+(install|ci|i|add|publish|fetch|update|upgrade)\b", re.IGNORECASE),
     "npm install/ci/add/publish（从 npm registry 拉包）"),
    (re.compile(r"^\s*npx\s+\S", re.IGNORECASE), "npx（可能从 npm 下载包）"),
    (re.compile(r"^\s*yarn\s+(add|install|upgrade|publish)\b", re.IGNORECASE),
     "yarn add/install/publish（从 registry 拉包）"),
    (re.compile(r"^\s*pnpm\s+(add|install|update|publish)\b", re.IGNORECASE),
     "pnpm add/install/publish（从 registry 拉包）"),
    # git 网络操作——push/pull/clone/fetch 访问远程
    (re.compile(r"^\s*git\s+(push|pull|clone|fetch|ls-remote|submodule\s+update)\b", re.IGNORECASE),
     "git push/pull/clone/fetch（访问远程仓库）"),
    # 其他语言包管理器
    (re.compile(r"^\s*go\s+(get|install|mod\s+(download|tidy))\b", re.IGNORECASE),
     "go get/install/mod download（从 proxy 拉模块）"),
    (re.compile(r"^\s*cargo\s+(install|update|fetch|publish)\b", re.IGNORECASE),
     "cargo install/update/fetch（从 crates.io 拉包）"),
    # 容器 registry
    (re.compile(r"^\s*docker\s+(pull|push|login)\b", re.IGNORECASE),
     "docker pull/push/login（访问 registry）"),
    # 直接 HTTP 客户端（虽不在命令白名单，但 python 脚本可能调用）
    (re.compile(r"^\s*(curl|wget)\b", re.IGNORECASE), "curl/wget（直接 HTTP 请求）"),
]


def set_network_mode(mode: str) -> None:
    """设置网络模式——lifespan 从 settings 读取后注入。

    mode: "off" | "warn" | "block"
    """
    global _network_mode
    normalized = mode.lower().strip() if mode else "off"
    if normalized not in ("off", "warn", "block"):
        _log.warning("Invalid network_mode '%s', defaulting to 'off'", mode)
        normalized = "off"
    if normalized != _network_mode:
        _log.info("network_guard mode changed: %s → %s", _network_mode, normalized)
    _network_mode = normalized


def get_network_mode() -> str:
    """返回当前网络模式（供 UI / 调试使用）。"""
    return _network_mode


def _detect_network_command(command: str) -> str | None:
    """检测命令是否涉及网络操作。返回原因描述，非网络命令返回 None。"""
    for pattern, desc in _NETWORK_COMMAND_PATTERNS:
        if pattern.search(command):
            return desc
    return None


def is_network_allowed(command: str) -> tuple[bool, str]:
    """检查命令是否被网络策略允许。

    返回 (allowed, reason):
    - allowed=True, reason=""：放行
    - allowed=False, reason="..."：拒绝（block 模式 + 网络命令）
    """
    if _network_mode == "off":
        return True, ""

    desc = _detect_network_command(command)
    if desc is None:
        return True, ""  # 非网络命令，总是放行

    # warn / block 模式下命中网络命令
    if _network_mode == "warn":
        _log.warning("[NETWORK-GUARD] 网络命令被监控放行: %s → %s", command[:80], desc)
        return True, ""

    # block 模式
    reason = f"网络命令被安全策略阻止（mode=block）: {desc}。命令: {command[:80]}"
    _log.warning("[NETWORK-GUARD] %s", reason)
    return False, reason
