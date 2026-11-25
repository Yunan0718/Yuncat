"""tests/contract/test_adapter_lifecycle_parity.py —— 停止必须真的离开就绪态

2026-10-07 新增。起因：`tests/unit/test_adapters.py::test_stop_clears_ready` 只测了一个
**Fake** adapter，于是三个真实 adapter 的 `stop()` 全部漏清 `_ready` 也没被发现：

  · `BaseProcessAdapter.stop()`  （native 走这条）
  · `ExternalCliAgentAdapter.stop()` （codex / claude）
  · `HttpGatewayAgentAdapter.stop()` （hermes / openclaw）

而 `list_agents()` 的状态判定是 `"running" if adapter._ready.is_set() else "stopped"`。
后果是点「停止」后：进程已经被 kill、`stop_agent` 也推了 `"stopped"`，但界面仍显示
**运行中**，一直和推送状态互相矛盾（实测 stop 后 `_ready` 仍为 True）。

本测试对真实 adapter 类断言"start→running / stop→stopped / 再 start→running"，
即 Docker 的 stop 语义：停止完成必须离开就绪态，且可再次启动。
"""
import asyncio
import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src-python")
sys.path.insert(0, _SRC)


class _CliFake:
    """复用真实 ExternalCliAgentAdapter 的生命周期实现。"""
    cli_name = "codex"

    def build_args(self, prompt, context):
        return [prompt]


class _HttpFake:
    """复用真实 HttpGatewayAgentAdapter 的生命周期实现。"""
    cli_name = "hermes"
    default_port = 8015
    health_path = "/health"


def _adapters():
    from adapters.ext_base import ExternalCliAgentAdapter
    from adapters.ext_http import HttpGatewayAgentAdapter
    from adapters.native import YuncatNativeAdapter

    class Cli(ExternalCliAgentAdapter, _CliFake):
        pass

    class Http(HttpGatewayAgentAdapter, _HttpFake):
        pass

    return [
        ("external-cli", Cli(agent_id="codex")),
        ("http-gateway", Http(agent_id="hermes")),
        ("native", YuncatNativeAdapter(agent_id="yuncat")),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,adapter", _adapters(), ids=lambda v: v if isinstance(v, str) else "")
async def test_stop_leaves_ready_state(name, adapter):
    await adapter.start()
    assert adapter._ready.is_set(), f"{name}: start 后应处于就绪态"
    await adapter.stop()
    assert not adapter._ready.is_set(), (
        f"{name}: stop() 必须清 _ready——否则 UI 会一直显示运行中，"
        "且与 stop_agent 推送的 stopped 永久矛盾"
    )
    # restart 语义：停完还能再起来
    await adapter.start()
    assert adapter._ready.is_set(), f"{name}: stop 后应能再次 start"
    await adapter.stop()


def test_every_stop_implementation_clears_ready():
    """静态兜底：任何重写 stop() 的 adapter 都要清理 _ready（或继承会清理的实现）。"""
    base = os.path.join(_SRC, "adapters")
    offenders = []
    for name in sorted(os.listdir(base)):
        if not name.endswith(".py") or name in ("base.py", "__init__.py"):
            continue
        with open(os.path.join(base, name), encoding="utf-8") as fh:
            src = fh.read()
        if "async def stop(" not in src:
            continue  # 继承基类实现，已覆盖
        body = src[src.index("async def stop("):]
        body = body[: body.find("async def ", 10)] if "async def " in body[10:] else body
        if "_ready.clear()" not in body:
            offenders.append(name)
    assert not offenders, (
        "以下 adapter 的 stop() 没有清理 _ready，停止后状态会失真: " + ", ".join(offenders)
    )
