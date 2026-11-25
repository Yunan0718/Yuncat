"""tests/unit/test_g3_parallel_tools.py —— G3: 并行工具调用支持

对标 Claude Code: LLM 一次返回多个 tool_calls 时，独立资源的工具应并行执行。
同资源（如同一文件路径）的工具仍串行，避免写冲突。

测试覆盖:
  1. _get_resource_key: 资源键提取
  2. ResourceLockManager: 同资源串行 / 不同资源并行
  3. _execute_exclusive_tools: 并行编排逻辑
"""
import json
import pytest
import asyncio
import time

import os
import sys

# 确保 src-python 在 path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ============================================================
# 资源键提取测试
# ============================================================

class TestGetResourceKey:
    """_get_resource_key: 从工具名 + 参数提取资源键"""

    def test_file_tools_use_path(self):
        """文件操作类工具按 path 区分资源"""
        from services.agent.agent_tool_loop import _get_resource_key
        assert _get_resource_key("write_file", {"path": "/a/b.py"}) == "write_file:/a/b.py"
        assert _get_resource_key("edit_file", {"path": "/a/c.py"}) == "edit_file:/a/c.py"
        assert _get_resource_key("delete_file", {"path": "/a/d.py"}) == "delete_file:/a/d.py"

    def test_different_paths_different_keys(self):
        """不同文件路径 → 不同资源键 → 可并行"""
        from services.agent.agent_tool_loop import _get_resource_key
        k1 = _get_resource_key("write_file", {"path": "/a.py"})
        k2 = _get_resource_key("write_file", {"path": "/b.py"})
        assert k1 != k2

    def test_same_path_same_key(self):
        """相同文件路径 → 相同资源键 → 串行"""
        from services.agent.agent_tool_loop import _get_resource_key
        k1 = _get_resource_key("write_file", {"path": "/a.py"})
        k2 = _get_resource_key("write_file", {"path": "/a.py"})
        assert k1 == k2

    def test_shell_tools_use_shell_id(self):
        """Shell 类工具按 shell_id 区分资源"""
        from services.agent.agent_tool_loop import _get_resource_key
        assert _get_resource_key("run_command", {"shell_id": "s1"}) == "run_command:s1"
        assert _get_resource_key("run_command", {"shell_id": "s2"}) == "run_command:s2"

    def test_shell_default_id(self):
        """Shell 无 shell_id → default"""
        from services.agent.agent_tool_loop import _get_resource_key
        assert _get_resource_key("run_command", {}) == "run_command:default"

    def test_git_always_serial(self):
        """Git 操作始终串行（共享仓库状态）"""
        from services.agent.agent_tool_loop import _get_resource_key
        k1 = _get_resource_key("git", {"args": "status"})
        k2 = _get_resource_key("git", {"args": "add ."})
        assert k1 == k2 == "git:*"

    def test_unknown_tool_uses_tool_name(self):
        """未知工具按工具名串行（向后兼容）"""
        from services.agent.agent_tool_loop import _get_resource_key
        k = _get_resource_key("custom_tool", {"x": 1})
        assert k == "custom_tool:*"

    def test_file_tool_missing_path(self):
        """文件工具无 path → 按工具名串行（安全 fallback）"""
        from services.agent.agent_tool_loop import _get_resource_key
        k = _get_resource_key("write_file", {})
        assert k == "write_file:*"

    def test_fs_tools_use_path(self):
        """文件系统类工具（mkdir/delete_dir/rename_file）按 path 区分"""
        from services.agent.agent_tool_loop import _get_resource_key
        assert _get_resource_key("mkdir", {"path": "/a"}) == "mkdir:/a"
        assert _get_resource_key("delete_dir", {"path": "/b"}) == "delete_dir:/b"


# ============================================================
# 资源锁管理器测试
# ============================================================

class TestResourceLockManager:
    """ResourceLockManager: 同资源串行 / 不同资源并行"""

    def test_different_resources_parallel(self):
        """不同资源键的工具并行执行（耗时 < 串行总和）"""
        from services.agent.agent_tool_loop import _ResourceLockManager

        mgr = _ResourceLockManager()
        execution_log: list[str] = []

        async def task(resource_key: str, duration: float, label: str):
            async with mgr.acquire(resource_key):
                execution_log.append(f"{label}:start")
                await asyncio.sleep(duration)
                execution_log.append(f"{label}:end")

        async def runner():
            await asyncio.gather(
                task("write_file:/a.py", 0.1, "A"),
                task("write_file:/b.py", 0.1, "B"),
            )

        start = time.monotonic()
        asyncio.run(runner())
        elapsed = time.monotonic() - start
        # 并行：总耗时 ≈ 0.1s（而非 0.2s）
        assert elapsed < 0.18, f"Expected parallel (<0.18s), got {elapsed:.3f}s"
        # 两个任务都执行了
        assert "A:start" in execution_log
        assert "B:start" in execution_log
        assert "A:end" in execution_log
        assert "B:end" in execution_log

    def test_same_resource_serial(self):
        """相同资源键的工具串行执行（耗时 ≈ 串行总和）"""
        from services.agent.agent_tool_loop import _ResourceLockManager

        mgr = _ResourceLockManager()
        execution_log: list[str] = []

        async def task(resource_key: str, duration: float, label: str):
            async with mgr.acquire(resource_key):
                execution_log.append(f"{label}:start")
                await asyncio.sleep(duration)
                execution_log.append(f"{label}:end")

        async def runner():
            await asyncio.gather(
                task("write_file:/a.py", 0.1, "A"),
                task("write_file:/a.py", 0.1, "B"),
            )

        start = time.monotonic()
        asyncio.run(runner())
        elapsed = time.monotonic() - start
        # 串行：总耗时 ≈ 0.2s
        assert elapsed >= 0.18, f"Expected serial (>=0.18s), got {elapsed:.3f}s"
        # A 完成后 B 才开始
        a_end_idx = execution_log.index("A:end")
        b_start_idx = execution_log.index("B:start")
        assert a_end_idx < b_start_idx, "B should start after A ends for same resource"

    def test_lock_reused_across_calls(self):
        """同一资源键复用同一个 Lock 对象"""
        from services.agent.agent_tool_loop import _ResourceLockManager

        mgr = _ResourceLockManager()
        lock1 = mgr._get_lock("write_file:/a.py")
        lock2 = mgr._get_lock("write_file:/a.py")
        assert lock1 is lock2

    def test_different_keys_different_locks(self):
        """不同资源键用不同的 Lock 对象"""
        from services.agent.agent_tool_loop import _ResourceLockManager

        mgr = _ResourceLockManager()
        lock1 = mgr._get_lock("write_file:/a.py")
        lock2 = mgr._get_lock("write_file:/b.py")
        assert lock1 is not lock2


# ============================================================
# _execute_exclusive_tools 并行编排测试
# ============================================================

class _MockAgentSvcG3:
    """轻量 mock self——只提供 _execute_exclusive_tools 需要的方法/属性"""

    def __init__(self):
        self.adapters = {}
        self.sent: list[dict] = []

    async def _safe_send(self, task_id: str, payload: dict) -> None:
        self.sent.append({"task_id": task_id, **payload})

    async def _should_auto_approve(self) -> bool:
        return False

    async def _analyze_tool_failure_and_send(self, *args, **kwargs):
        return None


class _MockToolServiceG3:
    """mock ts——execute 记录调用 + 模拟耗时"""

    def __init__(self, delay: float = 0.15):
        self._delay = delay
        self.execute_calls: list[dict] = []

    async def execute(self, tool_name: str, arguments: dict, agent_id: str = "", task_id: str = "") -> dict:
        self.execute_calls.append({"tool_name": tool_name, "args": arguments})
        await asyncio.sleep(self._delay)
        return {"success": True, "tool": tool_name, "path": arguments.get("path", "")}


def _make_call(call_id: str, tool_name: str, args: dict) -> dict:
    """构造单个 tool_call"""
    return {
        "id": call_id,
        "function": {"name": tool_name, "arguments": json.dumps(args)},
    }


class TestExecuteExclusiveToolsParallel:
    """G3: _execute_exclusive_tools 并行执行测试"""

    @pytest.mark.asyncio
    async def test_independent_tools_run_in_parallel(self):
        """不同文件路径的 write_file 并行执行——总耗时 ≈ 单个耗时，不是两倍"""
        from services.agent.agent_tool_loop import _execute_exclusive_tools

        agent = _MockAgentSvcG3()
        ts = _MockToolServiceG3(delay=0.15)

        calls = [
            _make_call("call-1", "write_file", {"path": "/a.py", "content": "A"}),
            _make_call("call-2", "write_file", {"path": "/b.py", "content": "B"}),
        ]

        start = time.monotonic()
        results = await _execute_exclusive_tools(
            agent, calls, ts, "hermes", "task-1",
            _confirmable_names=set(),
            _openclaw_tool_names=set(),
            model_cfg={"context": 128000},
            adapter=None, context={},
        )
        elapsed = time.monotonic() - start

        assert len(results) == 2
        # 并行执行——总耗时应 < 0.28s（两个 0.15s 工具串行需要 0.30s）
        assert elapsed < 0.28, f"Expected parallel (<0.28s), got {elapsed:.3f}s"
        # 结果按原始顺序
        assert results[0]["tool_call_id"] == "call-1"
        assert results[1]["tool_call_id"] == "call-2"

    @pytest.mark.asyncio
    async def test_same_resource_tools_run_serially(self):
        """相同文件路径的 write_file 串行执行——总耗时 ≈ 两倍单个耗时"""
        from services.agent.agent_tool_loop import _execute_exclusive_tools

        agent = _MockAgentSvcG3()
        ts = _MockToolServiceG3(delay=0.15)

        calls = [
            _make_call("call-1", "write_file", {"path": "/same.py", "content": "A"}),
            _make_call("call-2", "write_file", {"path": "/same.py", "content": "B"}),
        ]

        start = time.monotonic()
        results = await _execute_exclusive_tools(
            agent, calls, ts, "hermes", "task-1",
            _confirmable_names=set(),
            _openclaw_tool_names=set(),
            model_cfg={"context": 128000},
            adapter=None, context={},
        )
        elapsed = time.monotonic() - start

        assert len(results) == 2
        # 串行执行——总耗时应 >= 0.28s（两个 0.15s 工具串行 ≈ 0.30s）
        assert elapsed >= 0.28, f"Expected serial (>=0.28s), got {elapsed:.3f}s"
        # 结果按原始顺序
        assert results[0]["tool_call_id"] == "call-1"
        assert results[1]["tool_call_id"] == "call-2"

    @pytest.mark.asyncio
    async def test_results_in_original_order(self):
        """3 个并行工具——结果按原始调用顺序返回，不按完成顺序"""
        from services.agent.agent_tool_loop import _execute_exclusive_tools

        agent = _MockAgentSvcG3()

        # 第一个工具最慢（0.3s），后两个快（0.05s）——验证结果不按完成顺序
        class _VariableDelayTS:
            async def execute(self, tool_name: str, arguments: dict, agent_id: str = "", task_id: str = "") -> dict:
                if arguments.get("path") == "/slow.py":
                    await asyncio.sleep(0.3)
                else:
                    await asyncio.sleep(0.05)
                return {"success": True, "tool": tool_name, "path": arguments.get("path", "")}

        ts = _VariableDelayTS()
        calls = [
            _make_call("call-1", "write_file", {"path": "/slow.py", "content": "S"}),
            _make_call("call-2", "write_file", {"path": "/fast1.py", "content": "F1"}),
            _make_call("call-3", "write_file", {"path": "/fast2.py", "content": "F2"}),
        ]

        results = await _execute_exclusive_tools(
            agent, calls, ts, "hermes", "task-1",
            _confirmable_names=set(),
            _openclaw_tool_names=set(),
            model_cfg={"context": 128000},
            adapter=None, context={},
        )

        assert len(results) == 3
        # 结果按原始顺序，不按完成顺序
        assert results[0]["tool_call_id"] == "call-1"
        assert results[1]["tool_call_id"] == "call-2"
        assert results[2]["tool_call_id"] == "call-3"

    @pytest.mark.asyncio
    async def test_degraded_tools_return_instantly(self):
        """已降级工具立即返回失败——不执行实际工具"""
        from services.agent.agent_tool_loop import _execute_exclusive_tools

        agent = _MockAgentSvcG3()
        ts = _MockToolServiceG3(delay=0.15)

        calls = [
            _make_call("call-1", "write_file", {"path": "/a.py", "content": "A"}),
            _make_call("call-2", "write_file", {"path": "/b.py", "content": "B"}),
        ]

        start = time.monotonic()
        results = await _execute_exclusive_tools(
            agent, calls, ts, "hermes", "task-1",
            _confirmable_names=set(),
            _openclaw_tool_names=set(),
            model_cfg={"context": 128000},
            adapter=None, context={},
            degraded_tools={"write_file"},  # write_file 已降级
        )
        elapsed = time.monotonic() - start

        assert len(results) == 2
        # 降级工具立即返回——不执行 ts.execute
        assert len(ts.execute_calls) == 0
        assert elapsed < 0.05, f"Expected instant (<0.05s), got {elapsed:.3f}s"
        # 两个都是降级失败
        for r in results:
            assert "degraded" in r["content"]

    @pytest.mark.asyncio
    async def test_mixed_degraded_and_parallel(self):
        """混合场景——降级工具立即返回 + 非降级工具并行执行"""
        from services.agent.agent_tool_loop import _execute_exclusive_tools

        agent = _MockAgentSvcG3()
        ts = _MockToolServiceG3(delay=0.15)

        calls = [
            _make_call("call-1", "write_file", {"path": "/a.py", "content": "A"}),
            _make_call("call-2", "read_file", {"path": "/b.py"}),  # read_file 不降级
            _make_call("call-3", "write_file", {"path": "/c.py", "content": "C"}),
        ]

        results = await _execute_exclusive_tools(
            agent, calls, ts, "hermes", "task-1",
            _confirmable_names=set(),
            _openclaw_tool_names=set(),
            model_cfg={"context": 128000},
            adapter=None, context={},
            degraded_tools={"write_file"},  # 只有 write_file 降级
        )

        assert len(results) == 3
        # call-1 和 call-3 是降级（write_file），call-2 执行了
        assert "degraded" in results[0]["content"]
        assert "degraded" not in results[1]["content"]
        assert "degraded" in results[2]["content"]
        # 只有 read_file 被实际执行
        assert len(ts.execute_calls) == 1
        assert ts.execute_calls[0]["tool_name"] == "read_file"

    @pytest.mark.asyncio
    async def test_empty_calls_returns_empty(self):
        """空 calls 列表返回空结果"""
        from services.agent.agent_tool_loop import _execute_exclusive_tools

        agent = _MockAgentSvcG3()
        ts = _MockToolServiceG3()

        results = await _execute_exclusive_tools(
            agent, [], ts, "hermes", "task-1",
            _confirmable_names=set(),
            _openclaw_tool_names=set(),
            model_cfg={"context": 128000},
            adapter=None, context={},
        )

        assert results == []

    @pytest.mark.asyncio
    async def test_three_independent_tools_parallel(self):
        """3 个不同资源的工具并行——总耗时 ≈ 单个耗时"""
        from services.agent.agent_tool_loop import _execute_exclusive_tools

        agent = _MockAgentSvcG3()
        ts = _MockToolServiceG3(delay=0.15)

        calls = [
            _make_call("call-1", "write_file", {"path": "/a.py", "content": "A"}),
            _make_call("call-2", "write_file", {"path": "/b.py", "content": "B"}),
            _make_call("call-3", "write_file", {"path": "/c.py", "content": "C"}),
        ]

        start = time.monotonic()
        results = await _execute_exclusive_tools(
            agent, calls, ts, "hermes", "task-1",
            _confirmable_names=set(),
            _openclaw_tool_names=set(),
            model_cfg={"context": 128000},
            adapter=None, context={},
        )
        elapsed = time.monotonic() - start

        assert len(results) == 3
        # 3 个并行——总耗时应 < 0.28s（3 个串行需要 0.45s）
        assert elapsed < 0.28, f"Expected parallel (<0.28s), got {elapsed:.3f}s"
