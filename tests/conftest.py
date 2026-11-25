"""tests/conftest.py —— pytest 共享配置和 fixtures

提供:
  - 标记注册 (unit/integration)
  - db fixture: 内存 aiosqlite + SCHEMA_SQL
  - agent_svc fixture: AgentService + mock adapters
"""
import asyncio
import os
import sys

import pytest

# 确保 src-python 在 path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src-python"))


def pytest_configure(config):
    r"""注册自定义标记 + 把数据目录隔离到临时位置。

    2026-09-24：在此之前没有任何地方重定向 DATA_DIR，于是直接跑
    test_settings_service / test_h3_key_rotation 这类用例会读写**真实用户**的
    %APPDATA%\Yuncat\cred.key（settings_service.save('api_key', ...) →
    security.credentials.encrypt_value → _create_new_key 写盘）。CI 上恰好可写所以
    没暴露；受限环境下则表现为 16 个用例必然失败——两边都不是产品该有的行为。

    隔离发生在导入期（早于任何测试模块 import config），用环境变量而非 setattr：
    config.py 在模块加载时就固化 DATA_DIR/DB_PATH，晚于导入的 monkeypatch 只能改
    一个副本。个别用例（如 test_config.py 的 DATA_DIR 解析用例）自带 monkeypatch，
    按 pytest 的 fixture 优先级仍然覆盖得动。临时根取 pytest 的 basetemp
    （--basetemp 可控），不用系统 temp：受限环境下系统 temp 往往不可写，
    会把「隔离」变成另一种必然失败。
    """
    config.addinivalue_line("markers", "unit: 单元测试")
    config.addinivalue_line("markers", "integration: 集成测试")
    _isolated = os.environ.get("YUNCAT_TEST_DATA_DIR")
    if not _isolated:
        factory = getattr(config, "_tmp_path_factory", None)
        _base = str(factory.getbasetemp()) if factory else ""
        if not _base:
            import tempfile
            _base = tempfile.mkdtemp(prefix="yuncat-test-base-")
        _isolated = os.path.join(_base, "data")
        os.makedirs(_isolated, exist_ok=True)
        os.environ["YUNCAT_TEST_DATA_DIR"] = _isolated
    os.environ["YUNCAT_DATA_DIR"] = _isolated


# ═══════════════════════════════════════════════════════════
# Mock 工具
# ═══════════════════════════════════════════════════════════

class MockAdapter:
    """模拟 Adapter——不启动子进程，纯内存。"""
    def __init__(self, agent_id: str, fail_health: bool = False):
        self.agent_id = agent_id
        self._ready = asyncio.Event()
        self._ready.set()
        self._dropped_logs = 0
        self.process = None
        self.fail_health = fail_health
        self.stopped = False
        self.cancelled_tasks: list[str] = []

    async def health(self) -> bool:
        return not self.fail_health

    async def start(self):
        self._ready.set()

    async def stop(self):
        self.stopped = True

    async def wait_ready(self):
        await self._ready.wait()

    async def cancel(self, task_id: str):
        self.cancelled_tasks.append(task_id)

    async def stream(self, prompt: str, context: dict | None = None):
        yield f"回复: {prompt[:30]}"
        await asyncio.sleep(0.01)
        yield "...完成"

    def capabilities(self):
        from adapters.base import AgentCapability
        return AgentCapability(supports_stream=True)


class FakeWS:
    """模拟 WebSocket 连接——记录发送的消息。"""
    def __init__(self):
        self.messages: list[dict] = []
        self.closed = False

    async def send_json(self, payload: dict):
        self.messages.append(payload)

    async def close(self, code=1000):
        self.closed = True


# ═══════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════

# 契约用例依赖的内置工具：缺任一个就说明注册表被污染了
_REQUIRED_BUILTIN_TOOLS = ("read_file", "write_file", "run_command", "docs_lookup")


def load_all_builtin_tools() -> None:
    """重新执行 tools/builtin 下每个模块，把 @tool 注册回 registry。

    为什么不能用 importlib.import_module：模块已 import 过时它是空操作。
    tool_service / mcp_bridge 这类用例会 _tools.clear() 做隔离，若它们恢复的
    快照恰好是空的，注册表就一直空着，后面的契约用例会
    随执行顺序变红。reload 才会重跑装饰器。
    """
    import importlib
    import pkgutil

    import tools.builtin as _builtin

    for module_info in pkgutil.iter_modules(_builtin.__path__):
        name = "tools.builtin." + module_info.name
        try:
            if name in sys.modules:
                importlib.reload(sys.modules[name])
            else:
                importlib.import_module(name)
        except Exception:
            # best-effort：某个可选依赖缺失时跳过该模块，不影响其他工具注册
            continue


@pytest.fixture
async def db(tmp_path):
    """内存 aiosqlite + SCHEMA_SQL——每个测试独立 DB。"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    yield conn
    await conn.close()


@pytest.fixture(autouse=True)
def _restore_builtin_tools_after_clear():
    """用例把工具注册表清空后，后面的用例不该跟着遭殃（顺序相关的假红）"""
    yield
    from tools import registry as _registry

    # 缺任一个必备内置工具就重新注册：前面的用例可能把注册表清成
    # 一个不完整的快照（非空但少工具），只看“是不是空”会漏掉
    missing = [name for name in _REQUIRED_BUILTIN_TOOLS if name not in _registry._tools]
    if missing:
        load_all_builtin_tools()


@pytest.fixture
async def agent_svc(db):
    """AgentService + 2 个 mock adapter。"""
    from services.agent.agent_service import AgentService
    adapters = {
        "hermes": MockAdapter("hermes"),
        "openclaw": MockAdapter("openclaw", fail_health=False),
    }
    svc = AgentService(db, adapters=adapters)
    # mock model config so API key check passes
    async def fake_config(k="", **kw):
        return {"api_key": "sk-test", "model": "test"}
    svc._get_model_config = fake_config
    yield svc
