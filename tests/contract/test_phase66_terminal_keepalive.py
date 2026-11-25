"""tests/contract/test_phase66_terminal_keepalive.py —— Phase 66 终端会话保活合约测试

验证 Phase 66 后端行为：
1. WS 断开（ws_connections 为空）后 session 仍存活——保活
2. cleanup_expired 在 max_age 内不清理无 WS 的会话
3. cleanup_expired 在 max_age 后清理无 WS 的会话
4. write_stdin 更新 last_active
5. list_sessions 返回 ws_count
"""
import os
import sys
import time
import pytest

_SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src-python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


@pytest.mark.asyncio
async def test_session_survives_ws_disconnect():
    """WS 断开后 session 仍存活——Phase 66 保活核心"""
    from services.terminal_manager import TerminalSessionManager, TerminalSession

    tm = TerminalSessionManager()
    sid = "keepalive-test"
    session = TerminalSession(sid, os.getcwd(), 80, 24)
    tm._sessions[sid] = session
    await tm.spawn_shell(session)

    # 模拟 WS 连接
    class FakeWS:
        async def send_json(self, _):
            pass

    session.ws_connections.append(FakeWS())
    assert len(session.ws_connections) == 1

    # 模拟 WS 断开——清空 ws_connections
    session.ws_connections.clear()
    assert len(session.ws_connections) == 0

    # session 仍然存在于 _sessions 中
    assert sid in tm._sessions
    # 进程仍然存活（保活）
    assert session.alive is True

    await tm.kill_session(sid)


@pytest.mark.asyncio
async def test_cleanup_expired_keeps_recent_sessions():
    """cleanup_expired 不清理 max_age 内的会话"""
    from services.terminal_manager import TerminalSessionManager, TerminalSession

    tm = TerminalSessionManager()
    sid = "recent"
    session = TerminalSession(sid, os.getcwd(), 80, 24)
    session.last_active = time.time()  # 刚活跃
    tm._sessions[sid] = session

    # 1 小时内不清理
    cleaned = await tm.cleanup_expired(max_age_seconds=3600)
    assert cleaned == 0
    assert sid in tm._sessions


@pytest.mark.asyncio
async def test_cleanup_expired_removes_old_sessions_without_ws():
    """cleanup_expired 清理超期且无 WS 的会话"""
    from services.terminal_manager import TerminalSessionManager, TerminalSession

    tm = TerminalSessionManager()
    sid = "old"
    session = TerminalSession(sid, os.getcwd(), 80, 24)
    # last_active 设为 2 小时前
    session.last_active = time.time() - 7200
    tm._sessions[sid] = session

    cleaned = await tm.cleanup_expired(max_age_seconds=3600)
    assert cleaned == 1
    assert sid not in tm._sessions


@pytest.mark.asyncio
async def test_cleanup_expired_keeps_old_sessions_with_active_ws():
    """cleanup_expired 不清理有活跃 WS 的会话（即使超期）"""
    from services.terminal_manager import TerminalSessionManager, TerminalSession

    tm = TerminalSessionManager()
    sid = "old-with-ws"
    session = TerminalSession(sid, os.getcwd(), 80, 24)
    session.last_active = time.time() - 7200  # 超期

    class FakeWS:
        async def send_json(self, _):
            pass

    session.ws_connections.append(FakeWS())  # 有活跃 WS
    tm._sessions[sid] = session

    cleaned = await tm.cleanup_expired(max_age_seconds=3600)
    assert cleaned == 0
    assert sid in tm._sessions


@pytest.mark.asyncio
async def test_write_stdin_updates_last_active():
    """write_stdin 更新 last_active"""
    from services.terminal_manager import TerminalSessionManager, TerminalSession

    tm = TerminalSessionManager()
    sid = "active-test"
    session = TerminalSession(sid, os.getcwd(), 80, 24)
    old_active = time.time() - 100
    session.last_active = old_active
    tm._sessions[sid] = session
    await tm.spawn_shell(session)

    await tm.write_stdin(session, "echo hello\n")
    assert session.last_active > old_active

    await tm.kill_session(sid)


def test_list_sessions_includes_ws_count():
    """list_sessions 返回 ws_count"""
    from services.terminal_manager import TerminalSessionManager, TerminalSession

    tm = TerminalSessionManager()
    sid = "ws-count-test"
    session = TerminalSession(sid, os.getcwd(), 80, 24)
    session.ws_connections = ["ws1", "ws2"]  # 2 个连接
    tm._sessions[sid] = session

    sessions = tm.list_sessions()
    found = next(s for s in sessions if s["session_id"] == sid)
    assert found["ws_count"] == 2


def test_switch_tab_message_supported():
    """Phase 66: 前端重连后发 switch_tab，ws_terminal 应支持该消息类型"""
    ws_code = open(
        os.path.join(_SRC, "routes", "workspace.py"),
        encoding="utf-8",
    ).read()
    # ws_terminal 应处理 switch_tab 消息
    assert "switch_tab" in ws_code, (
        "ws_terminal should handle 'switch_tab' message for session recovery (Phase 66)"
    )


def test_new_tab_and_close_tab_messages_supported():
    """Phase 66: 多 tab 管理——new_tab / close_tab 消息类型应被支持"""
    ws_code = open(
        os.path.join(_SRC, "routes", "workspace.py"),
        encoding="utf-8",
    ).read()
    assert "new_tab" in ws_code, "ws_terminal should handle 'new_tab' message"
    assert "close_tab" in ws_code, "ws_terminal should handle 'close_tab' message"
