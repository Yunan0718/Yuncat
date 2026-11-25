"""tests/unit/test_adapter_stream_error.py —— 外部网关流/调用错误处理验证

Phase 融合重构后：HermesAdapter / OpenClawAdapter 均继承 HttpGatewayAgentAdapter，
作为外部 agent 连接用户本地 gateway。本文件验证新的错误处理契约：

1. gateway 不可达（_port=0 或真实连接失败）→ stream() yield stream_error，invoke() 返回 {"error": ...}
2. gateway 可达但 context 未带模型 → 从 /v1/models 兜底模型名（Hermes 自带模型）
3. gateway 可达 + 模型已配 + session.post 抛异常 → stream() yield stream_error（含异常信息）
4. execute_tool 明确返回 unsupported（Hermes 在服务端执行工具，没有单工具端点）
5. stop() 正确关闭 session（避免 ResourceWarning）

核心诉求不变：错误必须以 stream_error / 结构化 error 表达，不能伪装成普通 text，
否则 agent_tool_loop 会把错误当成正常回复继续循环，任务卡在 running。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ───────────────────────── Mock 辅助 ─────────────────────────


class _BoomResponse:
    """async context manager：__aenter__ 抛 RuntimeError。"""
    async def __aenter__(self):
        raise RuntimeError("simulated network failure")

    async def __aexit__(self, *args):
        return False


class _BoomSession:
    """mock aiohttp.ClientSession——post() 返回 _BoomResponse。closed=False 使
    _ensure_session 跳过重建，从而让测试控制的 session 进入 post() 调用路径。"""
    closed = False

    def post(self, *args, **kwargs):
        return _BoomResponse()

    async def close(self):
        self.closed = True


class _StubSession:
    """stub session——仅记录 close 状态，用于清理测试。"""
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


async def _collect(gen):
    """收集 async generator 的所有 chunk 到 list。"""
    out = []
    async for chunk in gen:
        out.append(chunk)
    return out


async def _always_true(*a, **k):
    return True


# ───────────────────────── Hermes stream 错误处理 ─────────────────────────


class TestHermesStreamError:
    """HermesAdapter.stream 错误处理（外部网关契约）。"""

    async def test_gateway_down_yields_stream_error(self):
        from adapters.hermes import HermesAdapter
        a = HermesAdapter()
        a._port = 0  # 短路：_gateway_reachable 直接 False
        chunks = await _collect(a.stream("hi", {}))
        assert chunks, "应至少 yield 一个 chunk"
        assert chunks[0]["type"] == "stream_error"
        assert "网关不可达" in chunks[0]["data"]

    async def test_missing_model_falls_back_to_models_endpoint(self, monkeypatch):
        """外部 Agent 自带模型：Yuncat 没配模型时用 /v1/models 的第一个。"""
        from adapters.hermes import HermesAdapter
        a = HermesAdapter()
        monkeypatch.setattr(a, "_gateway_reachable", _always_true)

        async def _models():
            return ["hermes-agent"]

        monkeypatch.setattr(a, "list_models", _models)
        a._session = _BoomSession()
        chunks = await _collect(a.stream("hi", {}))
        # 不再在入口报「未配置模型」；实际进入了请求阶段（被 boom session 打断）
        assert chunks and chunks[0]["type"] == "stream_error"
        assert "simulated network failure" in chunks[0]["data"]
        assert "未配置模型" not in chunks[0]["data"]

    async def test_post_raises_yields_stream_error(self, monkeypatch):
        from adapters.hermes import HermesAdapter
        a = HermesAdapter()
        monkeypatch.setattr(a, "_gateway_reachable", _always_true)
        a._session = _BoomSession()
        chunks = await _collect(a.stream("hi", {"model": "gpt-4"}))
        assert any(c["type"] == "stream_error" for c in chunks)
        err = [c for c in chunks if c["type"] == "stream_error"][0]
        assert "simulated network failure" in err["data"]
        # 不应出现旧的 [错误] 前缀 text
        assert not any(c["type"] == "text" and "[错误]" in c.get("data", "") for c in chunks)


# ───────────────────────── OpenClaw stream 错误处理 ─────────────────────────


class TestOpenClawStreamError:
    """OpenClawAdapter.stream 错误处理（外部网关契约）。"""

    async def test_gateway_down_yields_stream_error(self):
        from adapters.openclaw import OpenClawAdapter
        a = OpenClawAdapter()
        a._port = 0
        chunks = await _collect(a.stream("hi", {}))
        assert chunks[0]["type"] == "stream_error"
        assert "网关不可达" in chunks[0]["data"]

    async def test_no_model_yields_stream_error(self, monkeypatch):
        from adapters.openclaw import OpenClawAdapter
        a = OpenClawAdapter()
        monkeypatch.setattr(a, "_gateway_reachable", _always_true)
        chunks = await _collect(a.stream("hi", {}))
        assert chunks[0]["type"] == "stream_error"
        assert "未配置模型" in chunks[0]["data"]

    async def test_post_raises_yields_stream_error(self, monkeypatch):
        from adapters.openclaw import OpenClawAdapter
        a = OpenClawAdapter()
        monkeypatch.setattr(a, "_gateway_reachable", _always_true)
        a._session = _BoomSession()
        chunks = await _collect(a.stream("hi", {"model": "gpt-4"}))
        assert any(c["type"] == "stream_error" for c in chunks)
        err = [c for c in chunks if c["type"] == "stream_error"][0]
        assert "simulated network failure" in err["data"]


# ───────────────────────── invoke / execute_tool 优雅降级 ─────────────────────────


class TestGatewayInvokeDegrade:
    """gateway 不可达时 invoke / execute_tool 优雅降级。"""

    async def test_openclaw_invoke_gateway_down_returns_error_dict(self):
        from adapters.openclaw import OpenClawAdapter
        a = OpenClawAdapter()
        a._port = 0
        res = await a.invoke("hi", {"model": "gpt-4"})
        assert "error" in res
        assert "网关不可达" in res["error"]

    async def test_hermes_invoke_gateway_down_returns_error_dict(self):
        from adapters.hermes import HermesAdapter
        a = HermesAdapter()
        a._port = 0
        res = await a.invoke("hi", {"model": "gpt-4"})
        assert "error" in res
        assert "网关不可达" in res["error"]

    async def test_hermes_execute_tool_is_unsupported(self):
        """Hermes 没有「按名字执行单个工具」的端点——必须明确 unsupported。"""
        from adapters.hermes import HermesAdapter
        a = HermesAdapter()
        res = await a.execute_tool("run_command", {"cmd": "ls"})
        assert res.get("ok") is False
        assert res.get("unsupported") is True
        assert "delegate_to_agent" in res.get("error", "")


# ───────────────────────── Session 清理 ─────────────────────────


class TestSessionCleanup:
    """验证 stop() 正确关闭 session——避免 ResourceWarning。"""

    async def test_stop_closes_session(self):
        from adapters.hermes import HermesAdapter
        a = HermesAdapter()
        stub = _StubSession()
        a._session = stub
        await a.stop()
        assert stub.closed is True
        assert a._session is None
