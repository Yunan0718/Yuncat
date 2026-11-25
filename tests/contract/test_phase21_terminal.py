"""tests/contract/test_phase21_terminal.py —— Phase 21 终端升级合约测试"""
import asyncio
import os
import sys
import time
import pytest


# ---- TerminalSessionManager ----

def test_terminal_session_create():
    """创建会话并验证属性"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.terminal_manager import TerminalSessionManager
    tm = TerminalSessionManager()
    session = tm.create_session("/tmp/test", 100, 30)
    assert session.session_id is not None
    assert session.cwd == "/tmp/test"
    assert session.cols == 100
    assert session.rows == 30
    assert session.alive is False  # 还未 spawn


def test_terminal_session_list():
    """列出所有会话"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.terminal_manager import TerminalSessionManager
    tm = TerminalSessionManager()
    s1 = tm.create_session("/tmp/a")
    s2 = tm.create_session("/tmp/b")
    sessions = tm.list_sessions()
    assert len(sessions) == 2
    assert any(s["session_id"] == s1.session_id for s in sessions)


@pytest.mark.asyncio
async def test_terminal_spawn_pipe():
    """Pipe 模式启动 shell"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.terminal_manager import TerminalSessionManager, HAS_PTY
    tm = TerminalSessionManager()
    session = tm.create_session(os.getcwd(), 120, 40)
    await tm.spawn_shell(session)
    # Pipe 模式（无 PTY）应有 proc
    if not HAS_PTY:
        assert session.proc is not None
        assert session.proc.returncode is None
    # 清理
    await tm.kill_session(session.session_id)


@pytest.mark.asyncio
async def test_terminal_kill_session():
    """杀会话释放资源"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.terminal_manager import TerminalSessionManager, TerminalSession
    tm = TerminalSessionManager()
    sid = "test-kill"
    session = TerminalSession(sid, os.getcwd(), 80, 24)
    tm._sessions[sid] = session
    await tm.spawn_shell(session)
    await tm.kill_session(sid)
    assert sid not in tm._sessions


def test_terminal_resize_noop_when_no_proc():
    """resize 在无 proc 时不抛异常"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.terminal_manager import TerminalSessionManager
    tm = TerminalSessionManager()
    session = tm.create_session(os.getcwd(), 80, 24)
    # 无 proc 时 resize 静默返回
    import asyncio
    asyncio.run(tm.resize(session, 120, 40))
    assert session.cols == 120
    assert session.rows == 40


@pytest.mark.asyncio
async def test_winpty_interactive_round_trip():
    """回归：Windows 交互式终端必须能出提示符、能回显并执行输入。

    2026-09-17 之前 Windows 的 PTY 通道从来没真正跑通过——spawn 传了不存在的
    ``desktop_output``（TypeError）、返回值没接住（NameError）、还拿 socket 句柄当
    CRT fd 读写（EBADF）。三重故障叠加的结果是终端要么直接报错、要么静默降级成
    管道模式：没有提示符、没有回显、交互式命令用不了。这里跑一次真实输入回环，
    把"能不能用"钉死，而不是只断言对象字段。
    """
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.terminal_manager import TerminalSessionManager, pty_backend_name

    if pty_backend_name() != "winpty":
        pytest.skip("need Windows + pywinpty")

    class _FakeWs:
        def __init__(self):
            self.chunks = []

        async def send_json(self, msg):
            if msg.get("type") == "output":
                self.chunks.append(msg.get("data", ""))

        @property
        def text(self):
            return "".join(self.chunks)

    async def _wait_for(ws, predicate, timeout=25.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate(ws.text):
                return True
            await asyncio.sleep(0.2)
        return False

    tm = TerminalSessionManager()
    session = tm.create_session(os.getcwd(), 100, 30)
    await tm.spawn_shell(session)
    assert session.pty_kind == "winpty", "Windows must use winpty, not pipe fallback"
    assert session.alive is True

    ws = _FakeWs()
    reader = asyncio.create_task(tm.read_stdout(session, ws))
    try:
        assert await _wait_for(ws, lambda t: "PS " in t), \
            f"no prompt from PTY, got {ws.text[:200]!r}"

        marker = "YUNCAT_PTY_ROUNDTRIP"
        await tm.write_stdin(session, f"echo {marker}\r\n")
        # ConPTY repaints per keystroke, so the marker shows up at least twice
        # (once echoed, once as command output)
        assert await _wait_for(ws, lambda t: t.count(marker) >= 2), \
            "PTY did not echo/execute the input"

        await tm.resize(session, 120, 40)
        assert (session.cols, session.rows) == (120, 40)
    finally:
        reader.cancel()
        await tm.kill_session(session.session_id)
    assert session.session_id not in tm._sessions, "kill_session must reclaim the session"


# ---- 双通道分离 ----

def test_human_terminal_no_is_safe_command():
    """验证人类终端 ws_terminal 代码中不含 is_safe_command 调用"""
    ws_code = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "routes", "workspace.py"),
        encoding="utf-8",
    ).read()
    # 在 ws_terminal 函数体内不应有 is_safe_command
    fn_start = ws_code.index("async def ws_terminal")
    # 找到下一个同缩进的顶级函数定义
    rest = ws_code[fn_start:]
    lines = rest.split("\n")
    fn_lines = []
    for line in lines[1:]:  # 跳过定义行
        if line.startswith("async def ") or line.startswith("@router.") or line.startswith("def "):
            break
        fn_lines.append(line)
    fn_body = "\n".join(fn_lines)
    # 排除注释行后的代码中不应有 is_safe_command 调用
    code_lines = [l for l in fn_lines if not l.strip().startswith("#")]
    code_body = "\n".join(code_lines)
    assert "is_safe_command" not in code_body, (
        "Human terminal should NOT call is_safe_command — Phase 21 dual-channel separation"
    )


def test_agent_tool_service_keeps_is_safe_command():
    """Agent 的 ToolService.execute('run_command') 仍走 is_safe_command"""
    st_code = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "tools", "builtin", "system_tools.py"),
        encoding="utf-8",
    ).read()
    assert "is_safe_command" in st_code, (
        "Agent run_command MUST still use is_safe_command whitelist"
    )


# ---- TerminalSessionManager 单例 + lifespan 注入 ----

def test_terminal_manager_in_lifespan():
    """lifespan 注入了 terminal_manager（M1 拆分后注入点在 lifespan_postinit.py）"""
    lifespan_dir = os.path.join(os.path.dirname(__file__), "..", "..", "src-python")
    # M1: lifespan.py 已拆分为薄编排层 + 4 个子模块，terminal_manager 注入点
    # 可能在 lifespan_postinit.py._init_orchestrator_skills 中
    lifespan_files = [
        "lifespan.py",
        "lifespan_services.py",
        "lifespan_postinit.py",
        "lifespan_helpers.py",
        "lifespan_event_handlers.py",
    ]
    combined_code = ""
    for fname in lifespan_files:
        fpath = os.path.join(lifespan_dir, fname)
        if os.path.exists(fpath):
            with open(fpath, encoding="utf-8") as f:
                combined_code += f.read()
    assert "terminal_manager" in combined_code, "lifespan 文件群应注入 terminal_manager"
    assert "get_terminal_manager" in combined_code


# ---- Agent 回传 ----

def test_hooks_registry_has_terminal_broadcast():
    """hooks/registry.py 在 PostToolUse hook 中包含 terminal broadcast"""
    hook_code = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "hooks", "registry.py"),
        encoding="utf-8",
    ).read()
    assert "broadcast_agent_block" in hook_code, (
        "PostToolUse hook should broadcast Agent commands to terminal"
    )
    assert "terminal_manager" in hook_code


# ---- 合约回归: Phase 19 snapshot hook 仍存在 ----

def test_snapshot_hooks_still_registered():
    """Phase 19 snapshot hooks 未被 Phase 21 改动影响"""
    hook_code = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "hooks", "registry.py"),
        encoding="utf-8",
    ).read()
    assert "snapshot_pre_tool" in hook_code
    assert "snapshot_and_lint_post_tool" in hook_code
    assert "_run_lint_diagnostic" in hook_code
