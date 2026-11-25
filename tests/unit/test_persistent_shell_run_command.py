"""持久 Shell（open_shell → run_command(shell_id=...)）端到端可用。

2026-10-22 实测：run_command 的 schema 声明了 shell_id，实现签名却没有——
模型一旦带上 shell_id 就 TypeError，持久 Shell 从入口就不可用。
另外预览阶段用的是普通命令白名单（禁 shell 内置命令），与执行阶段的
shell 白名单不是同一套判定，导致 `cd` 这类合法内置命令在审批前就被拒。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from tools.builtin.system_tool_command import (
    _agent_shells, close_shell, open_shell, run_command,
)


@pytest.fixture(autouse=True)
async def _clean_shells():
    _agent_shells.clear()
    yield
    # 收尾要等进程真正退出：只 kill 不 wait 的话，子进程 transport 会在事件循环
    # 关闭后才被 GC，pytest 会报 "Event loop is closed" 的 unraisable 警告。
    for entry in list(_agent_shells.values()):
        proc = entry.get("proc")
        if proc is None:
            continue
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await proc.wait()
        except Exception:
            pass
    _agent_shells.clear()


@pytest.mark.asyncio
async def test_run_command_accepts_shell_id():
    """schema 里声明了 shell_id，实现必须接得住（否则 TypeError）。"""
    result = await run_command(command="echo hi", shell_id="")
    assert result["success"] is True
    assert result["meta"]["phase"] == "preview"


@pytest.mark.asyncio
async def test_unknown_shell_is_reported_in_preview():
    """Shell 已被回收时，预览阶段就告诉模型重新 open_shell。"""
    result = await run_command(command="echo hi", shell_id="does-not-exist")
    assert result["success"] is False
    assert result["meta"].get("shell_missing") is True
    assert "open_shell" in result["error"]


@pytest.mark.asyncio
async def test_shell_builtin_allowed_only_in_persistent_shell():
    """`export` 是 shell 内置命令：普通命令路径拒绝，持久 Shell 路径放行。

    两条路径用的是不同白名单（SAFE_COMMAND_WHITELIST vs SHELL_BUILTIN_WHITELIST），
    预览阶段必须按将要执行的那条路径判定，否则会出现"预览放行、执行再拒"。
    """
    plain = await run_command(command="export YUNCAT_T=1", shell_id="")
    assert plain["success"] is False

    opened = await open_shell()
    shell_id = opened["data"]["shell_id"]
    in_shell = await run_command(command="export YUNCAT_T=1", shell_id=shell_id)
    assert in_shell["success"] is True
    assert in_shell["data"]["shell_id"] == shell_id


@pytest.mark.asyncio
async def test_persistent_shell_executes_end_to_end(db):
    """open_shell → 预览 → 确认派发 → 在该 Shell 中执行，输出能回来。"""
    from tools.builtin.system_tool_confirm import (
        create_pending_state, dispatch_confirm,
    )

    opened = await open_shell()
    shell_id = opened["data"]["shell_id"]

    preview = await run_command(command="echo yuncat-persistent-shell", shell_id=shell_id)
    assert preview["success"] is True

    token = await create_pending_state(db, "pending_command", {
        "command": "echo yuncat-persistent-shell",
        "shell_id": shell_id,
        "timeout_sec": 30,
    })
    executed = await dispatch_confirm(db, token)
    assert executed["success"] is True, executed
    assert "yuncat-persistent-shell" in executed["data"]["output"]
    assert executed["data"]["exit_code"] == 0

    # 先抓住进程句柄——close_shell 会把 shell 从注册表里摘掉
    entry = _agent_shells.get(shell_id) or {}
    proc = entry.get("proc")
    closed = await close_shell(shell_id=shell_id)
    assert closed["success"] is True
    # 等 Shell 进程真正退出，避免 transport 在事件循环关闭后被 GC
    if proc is not None:
        try:
            await proc.wait()
        except Exception:
            pass
