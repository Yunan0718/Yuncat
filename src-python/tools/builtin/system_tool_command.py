"""system_tool_command.py —— 命令执行工具 + Agent 持久 Shell"""
import asyncio
import contextlib
import logging
import os
import sys
import time as _time
import uuid as _uuid
from pathlib import Path
from typing import NamedTuple

from ..registry import tool
from config import (
    calc_tool_result_limit, MAX_OPEN_SHELLS, SHELL_STREAM_BUFFER_MAX_CHARS,
    SYSTEM_TOOL_COMMAND_WHITELIST_PREVIEW_MAX,
    TASK_HEARTBEAT_INTERVAL_SECONDS,
    TOOL_SIZE_RUN_COMMAND_CHARS,
)
from .system_tool_safety import (
    is_safe_command, SAFE_COMMAND_WHITELIST,
    is_shell_command_allowed, validate_source_command,
)

_log = logging.getLogger("yuncat.tools")


class CommandResult(NamedTuple):
    """命令执行结果：输出文本 + 退出码（-1 = 未知）。"""

    output: str
    exit_code: int

# H7 修复：心跳 Task 强引用集合——Python asyncio 官方推荐模式。
# 事件循环只持有 Task 的弱引用，若调用方栈帧在 await 期间被回收（如异常传播），
# 心跳 Task 可能被 GC 中断。set 持强引用 + add_done_callback 自动清理，防御性兜底。
_heartbeat_tasks: set[asyncio.Task] = set()


async def _heartbeat_loop(task_id: str) -> None:
    """Phase 61: 命令执行期间周期性心跳——独立协程，不依赖输出时序。

    即使命令无输出（如长时间编译），心跳也持续触发，保证崩溃恢复查询
    能反映"仍在运行"。心跳失败静默（不中断工具执行）。
    """
    from context import get_agent_service
    agent_service = get_agent_service()
    if agent_service is None:
        return  # 测试环境或未注入 app——跳过
    while True:
        await asyncio.sleep(TASK_HEARTBEAT_INTERVAL_SECONDS)
        try:
            await agent_service.update_task_heartbeat(task_id)
        except Exception:
            _log.debug("heartbeat failed for task %s", task_id[:8], exc_info=True)


def _preview_gate(command: str, shell_id: str) -> dict | None:
    """预览阶段的安全判定——命中即返回拒绝信息（reason/data/meta），放行返回 None。

    两条路径用两套白名单，判定必须按**将要执行的那条路径**来：
      - 普通命令 → SAFE_COMMAND_WHITELIST（is_safe_command）
      - 持久 Shell → SHELL_BUILTIN_WHITELIST（is_shell_command_allowed，与执行同源）
    否则会出现"预览放行、确认后执行再拒"，用户点完同意才看到失败。
    """
    if shell_id:
        # Shell 被 LRU/TTL 回收后要立刻告诉模型"重新 open_shell"，
        # 而不是等到确认之后才失败。
        if shell_id not in _agent_shells:
            return {
                "reason": f"持久 Shell '{shell_id}' 不存在或已超时回收，请重新调用 open_shell。",
                "data": {"command": command, "shell_id": shell_id},
                "meta": {"shell_missing": True},
            }
        allowed, allow_reason = is_shell_command_allowed(command)
        if not allowed:
            return {
                "reason": allow_reason,
                "data": {"command": command, "shell_id": shell_id},
                "meta": {},
            }
        return None
    if is_safe_command(command):
        return None
    preview_list = ", ".join(SAFE_COMMAND_WHITELIST[:SYSTEM_TOOL_COMMAND_WHITELIST_PREVIEW_MAX])
    return {
        "reason": (
            f"命令不在白名单中或包含禁用字符（|>&`$;）。Phase 1 仅支持: {preview_list} 等。"
        ),
        "data": {"command": command.split()[0] if command else command},
        "meta": {},
    }


@tool(
    name="run_command", agents=["hermes", "openclaw"],
    description=(
        "Execute a SAFE shell command (whitelist only: dir/ls/python/node/git/etc). "
        "No pipes/redirects. Requires user confirmation. "
        "Use write_file+run_command for complex scripts."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
            "timeout_sec": {"type": "integer", "default": 60},
            "shell_id": {"type": "string", "description": "可选: 持久 Shell ID（从 open_shell 获取），在该 Shell 中执行以保持 cd/venv 状态"},
            "justification": {"type": "string", "description": "CodeWhale 借鉴: 命令执行理由——记录到审计日志供追溯。简述为何需要执行此命令。"},
        },
        "required": ["command"],
    },
    confirmable=True,
    tool_timeout=600,  # 10分钟（对标 Claude Code Bash 2min——本地命令可能更久）
    groups=["runtime"],
    max_result_size_chars=TOOL_SIZE_RUN_COMMAND_CHARS,  # Phase 56-P8: 自我声明结果大小
)
async def run_command(command: str, timeout_sec: int = 60,
                      shell_id: str = "", justification: str = "") -> dict:
    """预览阶段：返回命令和安全检查结果，不立即执行。

    shell_id 指向持久 Shell 时改用 shell 白名单（允许 cd/set/export 等内置命令），
    与执行路径 (_run_in_shell) 同一套判定。schema 里声明了 shell_id，实现却没接
    （函数签名缺参数），模型一旦带上 shell_id 就直接 TypeError，
    整个持久 Shell 能力用不了。
    """
    from tools.result import ToolResult, gen_run_preview_observation, gen_run_blocked_observation
    command = command.strip()

    blocked = _preview_gate(command, shell_id)
    if blocked is not None:
        return ToolResult(
            success=False,
            error=blocked["reason"],
            data=blocked["data"],
            observation=gen_run_blocked_observation(command, blocked["reason"]),
            meta={"tool_name": "run_command", "blocked": True, **blocked["meta"]},
        ).to_dict()
    # H1: 调用安全分析，将风险等级透传到前端确认弹窗（对标 Claude Code 命令审批风险提示）
    # 注意: HIGH 风险已在 tool_service._validate_and_prepare 拦截，此处只会是 MEDIUM/LOW
    from services.llm_security import analyze_command_safety
    risk = await analyze_command_safety(command)
    return ToolResult(
        success=True,
        data={
            "command": command, "timeout_sec": timeout_sec,
            "cwd": str(_get_workspace_path()),
            "shell_id": shell_id,
            "risk_level": risk["level"],  # H1: LOW/MEDIUM（HIGH 已在上游拦截）
            "risk_reason": risk["reason"],
        },
        observation=gen_run_preview_observation(command),
        meta={"tool_name": "run_command", "phase": "preview"},
    ).to_dict()


def _get_workspace_path() -> Path:
    """获取工作区路径（供内部使用）"""
    from security.sandbox import get_workspace
    return get_workspace()


# Round 1 S3: 提取为模块级函数——供 _execute_command 和 open_shell 复用，防子进程继承敏感变量
_SECRET_PATTERNS = ("key", "secret", "token", "password", "credential", "auth")
_PROTECTED_VARS = {"PATH", "HOME", "USER", "USERNAME", "TEMP", "TMP", "SYSTEMROOT",
                   "SYSTEMDRIVE", "ProgramFiles", "ProgramFiles(x86)", "CommonProgramFiles",
                   "LANG", "LC_ALL", "PYTHONPATH", "NODE_PATH"}
# git 的 `-c` 注入机制是一套变量：GIT_CONFIG_COUNT + GIT_CONFIG_KEY_n / GIT_CONFIG_VALUE_n，
# 必须成套出现。只过滤 KEY 会把 VALUE 留下，git 直接 fatal:
# "missing config key GIT_CONFIG_KEY_0" —— 表现为「agent 里所有 git 命令都失败」。
# 整套去掉最稳（VALUE 里可能带凭据，例如 http.extraHeader）。
_GIT_CONFIG_PREFIX = "GIT_CONFIG_"


def _build_filtered_env() -> dict:
    """过滤敏感环境变量，防止子进程继承 API keys / tokens。

    YUNCAT_TOKEN / OPENCLAW_DEFAULT_API_KEY 等含 key/secret/token/password/credential/auth
    的变量被移除（除非在 _PROTECTED_VARS 保护列表中）；
    GIT_CONFIG_* 整套移除（见上方说明：拆散会让 git 全部命令失败）。
    """
    filtered_env = {}
    for k, v in os.environ.items():
        if k.upper().startswith(_GIT_CONFIG_PREFIX):
            continue
        kl = k.lower()
        is_sensitive = any(p in kl for p in _SECRET_PATTERNS) and k.upper() not in _PROTECTED_VARS
        if not is_sensitive:
            filtered_env[k] = v
    return filtered_env


# ============================================================
# 命令实际执行
# ============================================================

async def _execute_command(command: str, timeout_sec: int = 60,
                            task_id: str | None = None) -> CommandResult:
    """v47: 命令执行——流式输出 stdout/stderr，通过 EventBus 实时推送。
    对标 Claude Code Bash tool 的实时 streaming。
    返回 CommandResult(输出, 退出码)；退出码未知为 -1。2026-10-22 起交回退出码——
    此前只回传输出字符串，命令失败在 agent 眼里全是"成功"（主流都把退出码交回模型）。
    Phase 61: 执行期间启动独立心跳协程，周期性更新 task.heartbeat_at。
    """
    from security.sandbox import get_workspace
    workspace = get_workspace()
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000200 | 0x08000000  # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW

    # Round 1 S7: 网络隔离守卫——block 模式下拒绝网络命令
    from security.network_guard import is_network_allowed
    allowed, net_reason = is_network_allowed(command)
    if not allowed:
        return CommandResult(f"[错误] {net_reason}", -1)

    # Phase 47 P6.2 / Round 1 S3: 复用模块级 _build_filtered_env（提取自原内联逻辑）
    filtered_env = _build_filtered_env()

    # Phase 61: 启动心跳协程——独立于输出时序，命令无输出也持续心跳
    _hb_task = asyncio.create_task(_heartbeat_loop(task_id)) if task_id else None
    # H7 修复：保存强引用防 GC 回收（见模块顶部 _heartbeat_tasks 说明）
    if _hb_task is not None:
        _heartbeat_tasks.add(_hb_task)
        _hb_task.add_done_callback(_heartbeat_tasks.discard)
    try:
        try:
            if sys.platform == "win32":
                # Windows 内置命令（dir/echo/type）不是独立可执行文件，需通过 cmd /c 调用
                cmd_parts = ["cmd", "/c", command]
            else:
                cmd_parts = command.split()
            proc = await asyncio.create_subprocess_exec(
                *cmd_parts,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workspace),
                env=filtered_env,
                **kwargs,
            )
            # 逐行流式读取 stdout + stderr
            output_parts: list[str] = []
            chunk_buffer: list[str] = []
            last_flush = _time.monotonic()

            async def _flush_chunks():
                nonlocal last_flush
                if chunk_buffer and task_id:
                    try:
                        from services.event_bus import get_event_bus
                        await get_event_bus().publish("tool_output_chunk", {
                            "task_id": task_id, "tool_name": "run_command",
                            "data": "".join(chunk_buffer), "stream": "stdout",
                        })
                    except Exception:
                                _log.debug("推送命令输出分块失败（界面看不到实时输出）", exc_info=True)
                    chunk_buffer.clear()
                last_flush = _time.monotonic()

            async def _read_stream(stream, label: str):
                nonlocal last_flush
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    text = line.decode(errors="replace")
                    output_parts.append(text)
                    if task_id:
                        chunk_buffer.append(text)
                        if _time.monotonic() - last_flush > 0.05 or sum(len(c) for c in chunk_buffer) > SHELL_STREAM_BUFFER_MAX_CHARS:
                            await _flush_chunks()
                await _flush_chunks()

            # 并行读取 stdout 和 stderr
            import asyncio as _aio
            try:
                await _aio.wait_for(
                    _aio.gather(
                        _read_stream(proc.stdout, "stdout"),
                        _read_stream(proc.stderr, "stderr"),
                    ),
                    timeout=timeout_sec,
                )
            except _aio.TimeoutError:
                pass  # 超时后走清理逻辑

            # Task 3.4: proc.wait() 加超时保护——防止子进程僵尸态永久阻塞
            # 对标主流: asyncio.wait_for(proc.wait(), timeout=5)，超时则 kill + 再 wait
            # 原 await proc.wait() 无超时，若子进程忽略 SIGTERM 或卡在内核态会永久阻塞
            try:
                await _aio.wait_for(proc.wait(), timeout=5)
            except _aio.TimeoutError:
                _log.warning("proc.wait() 5s 超时，强制 kill 子进程 (pid=%s)", proc.pid)
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass  # 进程已退出
                except Exception:
                    _log.debug("proc.kill() 失败 (pid=%s)", proc.pid, exc_info=True)
                # kill 后再 wait——确保进程资源回收（kill 后 wait 不会阻塞）
                with contextlib.suppress(Exception):
                    await proc.wait()

            # 组装完整输出
            # P2-2.1: 用 get_default_model_config() 替代硬编码 _DEFAULT_CFG，
            # 让工具结果上限跟随当前默认模型动态派生
            from config import get_default_model_config
            _stdout_limit = calc_tool_result_limit(get_default_model_config())
            _raw_stdout = "".join(output_parts)
            if len(_raw_stdout) > _stdout_limit:
                _tail = _raw_stdout[-int(_stdout_limit * 0.3):]
                output = _raw_stdout[:_stdout_limit] + _tail
                output += f"\n...[output truncated at {_stdout_limit} chars, {len(_raw_stdout)} total bytes]"
            else:
                output = _raw_stdout
            return CommandResult(output or "(no output)", proc.returncode if proc.returncode is not None else -1)
        except asyncio.TimeoutError:
            try:
                if sys.platform == "win32":
                    import signal as _signal
                    os.kill(proc.pid, _signal.CTRL_BREAK_EVENT)
                    await asyncio.sleep(0.5)
                    try:
                        import subprocess as _sp
                        _sp.run(
                            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                            capture_output=True, timeout=10,
                        )
                    except Exception:
                        _log.debug("taskkill 终止子进程失败", exc_info=True)
                proc.kill()
                # Task 3.4: kill 后 wait 加超时——防御性编程（kill 后 wait 通常不阻塞，
                # 但极端场景如内核态卡死仍可能阻塞）
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=3)
            except Exception:
                _log.debug("终止超时进程失败", exc_info=True)
            _log.warning(f"命令超时已终止 (pid={proc.pid})")
            raise  # v56: 重新抛出 TimeoutError，让 _handle_command 正确捕获
    finally:
        # Phase 61: 停止心跳协程
        if _hb_task and not _hb_task.done():
            _hb_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await _hb_task


# ═══════════════════════════════════════════════════════════
# Phase 47 P4.1: Agent 持久 Shell
# 对标 Claude Code 会话级持久 Bash——命令间保持 cd/venv/env 状态
# ═══════════════════════════════════════════════════════════

# Phase 48 T3-2: LRU + TTL（原裸 dict 无上限，open_shell 不 close 时 proc 永久泄漏）
from collections import OrderedDict as _ShellOrderedDict
_agent_shells: _ShellOrderedDict[str, dict] = _ShellOrderedDict()  # shell_id → {proc, cwd, created_at}
_MAX_SHELLS = MAX_OPEN_SHELLS  # Phase 48-H: 放宽上限对标主流
_SHELL_TTL = 1800  # 30 分钟超时回收（对齐 _CONFIRM_TIMEOUT）


def _terminate_shell(entry: dict) -> None:
    """终止 shell 进程（LRU 淘汰 / TTL 回收 / close_shell 共用）。"""
    proc = entry.get("proc")
    if proc is None:
        return
    try:
        proc.stdin.close()
    except Exception:
        _log.debug("关闭 shell stdin 失败", exc_info=True)
    try:
        if proc.returncode is None:
            proc.terminate()
    except Exception:
        _log.debug("终止 shell 进程失败", exc_info=True)


def _evict_expired_shells() -> None:
    """Phase 48 T3-2: 惰性清理超时 shell（open_shell 时调用，无需后台 task）。"""
    now = _time.time()
    expired = [sid for sid, e in _agent_shells.items()
               if now - e.get("created_at", now) > _SHELL_TTL]
    for sid in expired:
        entry = _agent_shells.pop(sid, None)
        if entry:
            _terminate_shell(entry)
            _log.debug("TTL evicted shell: %s", sid)


def _enforce_shell_limit() -> None:
    """Phase 48 T3-2: LRU 淘汰——超过 _MAX_SHELLS 时终止最老的 shell。"""
    while len(_agent_shells) > _MAX_SHELLS:
        _old_sid, _old_entry = _agent_shells.popitem(last=False)
        _terminate_shell(_old_entry)
        _log.debug("LRU evicted shell: %s", _old_sid)


@tool(
    name="open_shell", agents=["hermes", "openclaw"],
    description=(
        "打开一个持久 Shell 会话——后续 run_command 可传入 shell_id 在该 Shell 中执行。"
        "Shell 保持 cd/环境变量/venv 状态跨命令持续。对标 Claude Code 的会话级 Bash。"
        "用完请调用 close_shell 关闭。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "cwd": {"type": "string", "description": "Shell 初始工作目录（默认 workspace）"},
        },
        "required": [],
    },
    is_concurrency_safe=False,
    groups=["runtime"],
)
async def open_shell(cwd: str = "") -> dict:
    """创建持久 Shell 进程——返回 shell_id 供 run_command 使用。"""
    from tools.result import ToolResult
    from security.sandbox import get_workspace
    ws = str(get_workspace())
    shell_cwd = cwd if cwd and os.path.isdir(cwd) else ws
    shell_id = _uuid.uuid4().hex[:12]

    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000200 | 0x08000000
        # cmd.exe: /Q 关回显, /K 保持运行, chcp 65001 设为 UTF-8 避免编码乱码
        shell_cmd = ["cmd.exe", "/Q", "/K", "chcp", "65001", ">nul"]
        line_sep = "\r\n"
    else:
        shell_cmd = ["/bin/sh"]
        line_sep = "\n"

    try:
        proc = await asyncio.create_subprocess_exec(
            *shell_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=shell_cwd,
            env=_build_filtered_env(),  # Round 1 S3: 过滤敏感变量，防 Shell 继承 YUNCAT_TOKEN
            **kwargs,
        )
    except Exception as e:
        return ToolResult(
            success=False, error=f"无法创建 Shell 进程: {e}",
            meta={"tool_name": "open_shell"},
        ).to_dict()

    _agent_shells[shell_id] = {
        "proc": proc, "cwd": shell_cwd,
        "created_at": _time.time(),
    }
    _agent_shells.move_to_end(shell_id)  # Phase 48 T3-2: LRU 语义
    _evict_expired_shells()  # Phase 48 T3-2: 惰性清理超时 shell
    _enforce_shell_limit()  # Phase 48 T3-2: LRU 上限淘汰
    return ToolResult(
        success=True,
        data={"shell_id": shell_id, "cwd": shell_cwd, "platform": sys.platform},
        observation=f"持久 Shell 已打开 (id={shell_id}, cwd={shell_cwd})。"
                     f"后续 run_command 使用 shell_id='{shell_id}' 在此 Shell 中执行命令。",
        meta={"tool_name": "open_shell"},
    ).to_dict()


@tool(
    name="close_shell", agents=["hermes", "openclaw"],
    description="关闭一个持久 Shell 会话，释放资源。",
    parameters={
        "type": "object",
        "properties": {
            "shell_id": {"type": "string", "description": "要关闭的 Shell ID（从 open_shell 获取）"},
        },
        "required": ["shell_id"],
    },
    is_concurrency_safe=False,
    groups=["runtime"],
)
async def close_shell(shell_id: str) -> dict:
    """关闭持久 Shell 进程。"""
    from tools.result import ToolResult
    entry = _agent_shells.pop(shell_id, None)
    if not entry:
        return ToolResult(
            success=False, error=f"Shell '{shell_id}' 未找到或已关闭",
            meta={"tool_name": "close_shell"},
        ).to_dict()
    proc = entry["proc"]
    try:
        proc.stdin.close()
    except Exception:
            _log.debug("关闭 shell stdin 失败", exc_info=True)
    try:
        if proc.returncode is None:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=3)
    except asyncio.TimeoutError:
        proc.kill()
    except Exception:
            _log.debug("强制结束 shell 进程失败", exc_info=True)
    return ToolResult(
        success=True,
        observation=f"Shell {shell_id} 已关闭",
        meta={"tool_name": "close_shell"},
    ).to_dict()


async def _run_in_shell(shell_id: str, command: str, timeout_sec: int = 60,
                        task_id: str | None = None) -> CommandResult:
    """在持久 Shell 中执行命令——通过 stdin 写入 + sentinel 检测输出边界。

    对标 Claude Code 的会话级 Bash：cd / venv / export 等状态跨命令保持。

    Phase 61: 执行期间启动独立心跳协程。
    """
    entry = _agent_shells.get(shell_id)
    if not entry:
        return CommandResult(f"[错误] Shell '{shell_id}' 未找到——请先调用 open_shell", -1)
    _agent_shells.move_to_end(shell_id)  # Phase 48 T3-2: LRU 语义——活跃 shell 不被淘汰
    proc = entry["proc"]

    # Shell 进程存活检查
    if proc.returncode is not None:
        _agent_shells.pop(shell_id, None)
        return CommandResult(f"[错误] Shell '{shell_id}' 已退出 (exit code {proc.returncode})——请重新 open_shell", proc.returncode)
    # 检查 stdin 是否仍然可写
    if proc.stdin.is_closing():
        _agent_shells.pop(shell_id, None)
        return CommandResult(f"[错误] Shell '{shell_id}' stdin 已关闭——请重新 open_shell", -1)

    # Round 1 S3: 持久 Shell 也检查危险字符（防换行注入绕过白名单）
    _DANGEROUS_CHARS = ("|", ">", "<", "&", "`", "$(", ";", "\n", "\r")
    if any(c in command for c in _DANGEROUS_CHARS):
        return CommandResult(f"[错误] 命令包含禁用字符（|><&`$;\\n\\r）: {command[:80]}", -1)

    # Task 7.10: 持久 Shell 命令白名单——shell builtins + safe external commands
    # 对标 Claude Code 会话级 Bash——允许 set/export/source/cd 等 shell 内置命令
    allowed, allow_reason = is_shell_command_allowed(command)
    if not allowed:
        return CommandResult(f"[错误] {allow_reason}", -1)

    # Task 7.10: source/. 命令脚本内容检测——复用 _validate_script_content
    # 相对路径相对于 shell 的 cwd 解析（可能已通过 cd 改变）
    source_safe, source_reason = validate_source_command(command, entry.get("cwd", ""))
    if not source_safe:
        return CommandResult(f"[错误] {source_reason}", -1)

    # 使用唯一 sentinel 标记命令结束（UUID 格式避免与正常输出碰撞）
    sentinel = f"__SHELL_SENTINEL_{_uuid.uuid4().hex[:12]}__"
    if sys.platform == "win32":
        # cmd.exe 需要 \r\n，用 && 链接确保 sentinel 一定输出
        full_cmd = f"{command}\r\necho {sentinel} %ERRORLEVEL%\r\n"
    else:
        full_cmd = f"{command}\necho {sentinel} $?\n"

    try:
        proc.stdin.write(full_cmd.encode())
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError, OSError):
        return CommandResult(f"[错误] Shell '{shell_id}' 已退出——请重新 open_shell", -1)

    # 逐行读取直到 sentinel 或 Shell 退出
    output_parts: list[str] = []
    chunk_buffer: list[str] = []
    last_flush = _time.time()
    exit_code = -1  # 未知——收到 sentinel 解析出真实退出码后才覆盖
    # Phase 61: 启动心跳协程
    _hb_task = asyncio.create_task(_heartbeat_loop(task_id)) if task_id else None
    # H7 修复：保存强引用防 GC 回收（见模块顶部 _heartbeat_tasks 说明）
    if _hb_task is not None:
        _heartbeat_tasks.add(_hb_task)
        _hb_task.add_done_callback(_heartbeat_tasks.discard)
    try:
        try:
            while True:
                line_bytes = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout_sec)
                if not line_bytes:
                    # 管道关闭——Shell 进程可能已退出
                    if proc.returncode is not None:
                        _agent_shells.pop(shell_id, None)
                    break
                line = line_bytes.decode(errors="replace")
                # 检测 sentinel——提取退出码
                if sentinel in line:
                    try:
                        ec_part = line.split(sentinel)[-1].strip()
                        exit_code = int(ec_part) if ec_part.lstrip("-").isdigit() else -1
                    except (ValueError, IndexError):
                            pass  # 回退：退出码保持 -1（未知），不谎报成功
                    break
                output_parts.append(line)
                # 流式推送
                if task_id:
                    chunk_buffer.append(line)
                    if _time.time() - last_flush > 0.05 or sum(len(c) for c in chunk_buffer) > 2048:
                        try:
                            from services.event_bus import get_event_bus
                            await get_event_bus().publish("tool_output_chunk", {
                                "task_id": task_id, "tool_name": "run_command",
                                "data": "".join(chunk_buffer), "stream": "stdout",
                            })
                        except Exception:
                            _log.debug("推送命令输出分块失败（界面看不到实时输出）", exc_info=True)
                        chunk_buffer.clear()
                        last_flush = _time.time()
        except asyncio.TimeoutError:
            exit_code = -1
            output_parts.append(f"\n[超时 {timeout_sec}s——命令仍在后台运行]")
    finally:
        # Phase 61: 停止心跳协程
        if _hb_task and not _hb_task.done():
            _hb_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await _hb_task

    # 刷新剩余缓冲区
    if chunk_buffer and task_id:
        try:
            from services.event_bus import get_event_bus
            await get_event_bus().publish("tool_output_chunk", {
                "task_id": task_id, "tool_name": "run_command",
                "data": "".join(chunk_buffer), "stream": "stdout",
            })
        except Exception:
            _log.debug("推送命令输出分块失败（界面看不到实时输出）", exc_info=True)

    # Task 7.10: 每次执行后通过 pwd/cd 同步 cwd——cd/pushd/popd 等命令可能改变了工作目录
    # 对标 Claude Code 会话级 Bash——shell 状态跨命令保持，cwd 字段需与实际一致
    try:
        if sys.platform == "win32":
            pwd_cmd = b"cd\r\n"
        else:
            pwd_cmd = b"pwd\n"
        proc.stdin.write(pwd_cmd)
        await proc.stdin.drain()
        pwd_line = await asyncio.wait_for(proc.stdout.readline(), timeout=5)
        if pwd_line:
            new_cwd = pwd_line.decode(errors="replace").strip()
            if new_cwd and os.path.isdir(new_cwd):
                entry["cwd"] = new_cwd
    except Exception:
        _log.debug("cwd sync failed for shell %s", shell_id, exc_info=True)

    return CommandResult("".join(output_parts) or "(no output)", exit_code)
