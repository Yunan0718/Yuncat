"""system_tool_safety.py —— 命令安全（白名单模式）+ 路径 deny 主防线"""
import re
from pathlib import Path

# ============================================================
# 命令安全（白名单模式）
# ============================================================
SAFE_COMMAND_WHITELIST = [
    "dir", "ls", "type", "cat", "echo",
    "pip", "npm", "npx", "yarn", "pnpm",
    "git", "where", "which", "pwd", "cd",
    "mkdir", "rmdir", "copy", "move", "ren",
    "cargo", "make", "go",
    "python", "python3", "node",
]

# Task 3.7: docker/docker-compose 子命令分级——从白名单移除盲目放行
# 原 SAFE_COMMAND_WHITELIST 含 docker/docker-compose，docker run --privileged 等高危子命令直接放行。
# 改为按子命令分级: 只读放行 / 变更放行（走 confirmable + LLM 风险分析）/ 未知拒绝。
# 对标主流: 默认拒绝优于盲目放行（allowlist 原则）。
# 注意: is_safe_command 只做子命令白名单分类，参数风险（--privileged / -v /:/host）由
# services/llm_security.analyze_command_safety 在 tool_service._validate_and_prepare 评分拦截。
DOCKER_READONLY_SUBCOMMANDS = {
    "ps", "images", "logs", "inspect", "stats", "top", "version", "info",
}
DOCKER_ASK_SUBCOMMANDS = {
    "run", "exec", "build", "push", "pull", "create", "start", "stop",
    "restart", "pause", "unpause", "kill", "rm", "rename", "update",
    "save", "load", "tag", "import", "cp",
    # docker-compose 特有变更子命令
    "up", "down", "scale",
}
# docker/docker-compose 允许的子命令集合（只读 + 变更）
DOCKER_ALLOWED_SUBCOMMANDS = DOCKER_READONLY_SUBCOMMANDS | DOCKER_ASK_SUBCOMMANDS

SAFE_INTERPRETERS = {
    "python":   r"^python(3)?\s+[\w\-\./\\]+\.py(\s|$)",
    "python3":  r"^python3\s+[\w\-\./\\]+\.py(\s|$)",
    "node":     r"^node\s+[\w\-\./\\]+\.js(\s|$)",
}

FORBIDDEN_INTERPRETER_FLAGS = ["-c", "-e", "-i", "-m"]

# Round 1 S2: 带路径参数的命令——第 2 个 token 必须在 workspace 内
# 防 `type %APPDATA%\Yuncat\cred.key` / `cat ~/.ssh/id_rsa` 读系统敏感文件
PATH_ARG_COMMANDS = {
    "type", "cat", "cd", "mkdir", "rmdir", "copy", "move", "ren",
    "del", "rm", "ls", "dir",
}

# Phase 47 P6.1: 解释器脚本内容危险模式检测
# Round 1 S8: 扩展网络库检测（防 Agent 经 python script 外泄数据）+ subprocess 全方法
_DANGEROUS_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'import\s+os\s*;?\s*\bos\s*\.\s*(system|popen|exec\w*|spawn\w*)', re.DOTALL),
     "import os + os.system/popen/exec/spawn"),
    (re.compile(r'import\s+subprocess\s*;?\s*\bsubprocess\s*\.\s*(call|Popen|run|check_output|check_call)', re.DOTALL),
     "import subprocess + subprocess.call/Popen/run/check_output"),
    (re.compile(r'__import__\s*\(\s*[\'"]os[\'"]', re.DOTALL),
     "__import__('os')"),
    (re.compile(r'from\s+subprocess\s+import\s+(call|Popen|check_output|check_call|run)', re.DOTALL),
     "from subprocess import call/Popen/check_output/check_call/run"),
    # Round 1 S8: 网络库——数据外泄通道
    (re.compile(r'urllib\.(request|urlopen)', re.DOTALL),
     "urllib.request/urlopen（网络外泄风险）"),
    (re.compile(r'\brequests\s*\.\s*(get|post|put|delete|patch|head|request)\b', re.DOTALL),
     "requests 网络调用（数据外泄风险）"),
    (re.compile(r'socket\s*\.\s*create_connection', re.DOTALL),
     "socket.create_connection（原始网络连接）"),
    (re.compile(r'httpx\s*\.\s*(get|post|put|delete|patch|Client|AsyncClient)', re.DOTALL),
     "httpx 网络调用（数据外泄风险）"),
    (re.compile(r'__import__\s*\(\s*[\'"](urllib|requests|httpx|socket)[\'"]', re.DOTALL),
     "__import__ 动态加载网络库"),
    (re.compile(r'pty\s*\.\s*spawn', re.DOTALL),
     "pty.spawn（伪终端执行任意命令）"),
]

# Phase 47 P6.3: git 子命令白名单（对齐 tools/builtin/system_tool_advanced.py）
GIT_SUBCOMMAND_WHITELIST = {
    "init","add","commit","diff","log","status","branch","checkout",
    "stash","push","pull","remote","restore","reset","tag","show",
}

GIT_BLOCKED_SUBCOMMANDS = {"rebase", "bisect", "filter-branch", "clean", "gc", "prune"}


# ============================================================
# Task 3.1: 路径 deny 主防线——读前阻止敏感文件
# ============================================================
# 对标 Claude Code: 在 read_file/list_dir 调用前阻止读取 .env / credentials / 私钥等
# 主防线；security/redact.py 是兜底（读后脱敏）
# 匹配策略：basename 精确匹配 + 路径子串匹配（小写）
_DENY_BASENAME_PATTERNS = [
    re.compile(r"^\.env$", re.IGNORECASE),           # .env
    re.compile(r"^\.env\..*$", re.IGNORECASE),       # .env.local / .env.production / .env.dev
    re.compile(r"^.*\.pem$", re.IGNORECASE),         # *.pem 证书
    re.compile(r"^.*\.key$", re.IGNORECASE),         # *.key 私钥
    re.compile(r"^.*\.pfx$", re.IGNORECASE),         # *.pfx PFX 证书
    re.compile(r"^.*\.p12$", re.IGNORECASE),         # *.p12 P12 证书
    re.compile(r"^id_rsa$", re.IGNORECASE),          # SSH 私钥
    re.compile(r"^id_ed25519$", re.IGNORECASE),      # SSH 私钥
    re.compile(r"^id_ecdsa$", re.IGNORECASE),        # SSH 私钥
]
# 路径任意位置含这些关键字（小写子串匹配）——覆盖 credentials.json / secrets.yaml 等
_DENY_PATH_KEYWORDS = ("credentials", "secrets")


def is_denied_path(path: str) -> tuple[bool, str]:
    """路径 deny 主防线——检查路径是否命中敏感文件模式。

    在 read_file / list_dir 调用前检查，命中则拒绝读取。
    对标 Claude Code 的 disallowed-paths——读前阻止优于读后脱敏。

    返回: (denied: bool, reason: str)
      - denied=True: 路径被拒绝，reason 说明命中模式
      - denied=False: 路径放行，reason 为空
    """
    if not path:
        return False, ""
    try:
        p = str(path)
        # basename 提取——兼容 Windows 反斜杠和 Unix 正斜杠
        # 不用 Path.name 因为路径可能含 ~ 等未展开符号
        separators = ("/", "\\")
        basename = p
        for sep in separators:
            idx = p.rfind(sep)
            if idx >= 0 and idx + 1 < len(p):
                basename = p[idx + 1:]
                break
        # basename 精确模式匹配
        for pat in _DENY_BASENAME_PATTERNS:
            if pat.match(basename):
                return True, f"路径命中敏感文件模式: {basename}（deny 主防线阻止）"
        # 路径子串关键字匹配（小写）
        lower_path = p.lower()
        for kw in _DENY_PATH_KEYWORDS:
            if kw in lower_path:
                return True, f"路径含敏感关键字: {kw}（deny 主防线阻止）"
        return False, ""
    except Exception:
        # best-effort——检查失败则放行（validate_path 会做二次校验）
        return False, ""


def _validate_script_content(script_path: str) -> tuple[bool, str]:
    """读取脚本前 500 字符做启发式危险检测。
    返回 (safe, blocked_reason)。
    """
    try:
        path = Path(script_path)
        if not path.exists():
            return True, ""  # 脚本路径可能由 shell 解析，不做误杀
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read(65536)  # Round 1 S8: 500→64KB 全文检测，防第 501 字符后藏代码
        for pattern, desc in _DANGEROUS_PATTERNS:
            if pattern.search(content):
                return False, f"脚本包含危险模式: {desc}。请改用 run_command 直接调用。"
    except (OSError, UnicodeDecodeError):
        pass  # 读不了就不拦截
    return True, ""


def is_safe_command(cmd: str, workspace_ok: bool = True) -> bool:
    """v47 参数级沙箱：首 token 白名单 + 解释器参数约束 + 脚本内容检测 + git 子命令对齐"""
    cmd_stripped = cmd.strip()
    # Round 1 S1: 追加 \n \r——cmd.exe 把换行当命令分隔符，防 `python x.py\nmove cred.key shared/`
    for dangerous_char in ["|", ">", "<", "&", "`", "$(", ";", "\n", "\r"]:
        if dangerous_char in cmd_stripped:
            return False

    tokens = cmd_stripped.split()
    if not tokens:
        return False

    first_token = tokens[0].lower()

    if first_token in SAFE_INTERPRETERS:
        for flag in FORBIDDEN_INTERPRETER_FLAGS:
            if flag in tokens:
                return False
        if not re.match(SAFE_INTERPRETERS[first_token], cmd_stripped, re.IGNORECASE):
            return False
        if workspace_ok and len(tokens) >= 2:
            from security.sandbox import validate_path
            try:
                resolved = validate_path(tokens[1])
            except PermissionError:
                return False
            # P6.1 / S8: 脚本内容危险检测——用解析后的绝对路径
            # 原 bug: 传 tokens[1]（相对路径）导致 Path.exists() 检查 CWD 而非 workspace，检测失效
            safe, _reason = _validate_script_content(str(resolved))
            if not safe:
                return False
        return True

    # P6.3: git 子命令白名单对齐
    if first_token == "git" and len(tokens) >= 2:
        sub = tokens[1].lower()
        if sub in GIT_BLOCKED_SUBCOMMANDS:
            return False
        if sub not in GIT_SUBCOMMAND_WHITELIST:
            return False

    # Round 1 S2: 带路径参数的命令——校验第 2 token 在 workspace 内
    # 防 `type %APPDATA%\Yuncat\cred.key` / `cat ~/.ssh/id_rsa` 读系统敏感文件
    if first_token in PATH_ARG_COMMANDS and workspace_ok and len(tokens) >= 2:
        from security.sandbox import validate_path
        try:
            validate_path(tokens[1])
        except PermissionError:
            return False

    # Task 3.7: docker/docker-compose 子命令分级——已从白名单移除，此处按子命令分类
    # 只读子命令（ps/images/logs 等）+ 变更子命令（run/exec/build 等）→ 放行（走 confirmable）
    # 未知子命令 / 无子命令 → 拒绝（默认拒绝优于盲目放行）
    if first_token in ("docker", "docker-compose"):
        if len(tokens) < 2:
            return False
        return tokens[1].lower() in DOCKER_ALLOWED_SUBCOMMANDS

    return first_token in SAFE_COMMAND_WHITELIST


# ============================================================
# Task 7.10: 持久 Shell 白名单 + source 脚本检测
# ============================================================
# 对标 Claude Code 会话级 Bash——允许 shell builtins（set/export/source/cd 等）
# 但仍检查危险字符 + 脚本内容。持久 Shell 需支持 shell 内置命令，
# 这些命令不是外部可执行文件，不在 SAFE_COMMAND_WHITELIST 中。
SHELL_BUILTIN_WHITELIST = frozenset({
    # 目录操作
    "cd", "chdir", "pushd", "popd", "dirs",
    # 环境变量
    "set", "unset", "export", "env",
    # 脚本加载
    "source", ".",
    # 别名
    "alias", "unalias",
    # 信息查询
    "pwd", "history", "type", "hash", "help",
    # Shell 选项
    "umask", "ulimit", "shopt", "setopt",
    # Windows cmd 内置
    "call", "setlocal", "endlocal", "chcp", "title", "prompt", "color",
    "cls", "clear",
    # 测试 / 条件
    "test", "true", "false",
    # 读取输入
    "read",
})


def is_shell_command_allowed(command: str) -> tuple[bool, str]:
    """Task 7.10: 检查命令是否在持久 Shell 白名单中。

    持久 Shell 允许 shell 内置命令（SHELL_BUILTIN_WHITELIST）
    + 安全外部命令（SAFE_COMMAND_WHITELIST）。

    返回: (allowed: bool, error_reason: str)
      - allowed=True: 命令在白名单中
      - allowed=False: 命令不在白名单中，error_reason 说明原因
    """
    cmd_stripped = command.strip()
    if not cmd_stripped:
        return False, "空命令"
    tokens = cmd_stripped.split()
    if not tokens:
        return False, "空命令"
    first_token = tokens[0].lower()
    if first_token in SHELL_BUILTIN_WHITELIST or first_token in SAFE_COMMAND_WHITELIST:
        return True, ""
    return (
        False,
        f"命令 '{first_token}' 不在持久 Shell 白名单中。"
        f"允许: shell 内置命令（cd/set/export/source 等）"
        f"+ 安全外部命令（python/git/npm 等）。"
    )


def validate_source_command(command: str, shell_cwd: str) -> tuple[bool, str]:
    """Task 7.10: 检查 source/. 命令的目标脚本是否安全。

    复用 _validate_script_content 检测脚本危险模式（subprocess/os.system/
    requests/urllib/socket/httpx/pty 等）。

    相对路径相对于 shell_cwd 解析（而非 workspace），因为持久 Shell
    的 cwd 可能已通过 cd 改变。

    返回: (safe: bool, error_reason: str)
      - safe=True: 非 source 命令，或脚本内容安全/不存在（不误杀）
      - safe=False: 脚本包含危险模式
    """
    tokens = command.strip().split()
    if not tokens or tokens[0] not in ("source", "."):
        return True, ""
    if len(tokens) < 2:
        return False, "source/. 需要脚本路径参数"
    script_path = Path(tokens[1])
    if not script_path.is_absolute():
        script_path = Path(shell_cwd) / script_path
    safe, reason = _validate_script_content(str(script_path))
    if not safe:
        return False, reason
    return True, ""
