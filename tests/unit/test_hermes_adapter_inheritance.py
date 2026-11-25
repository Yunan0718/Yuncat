"""tests/unit/test_hermes_adapter_inheritance.py — HermesAdapter 外部网关契约

Phase 融合重构后：Hermes 不再 vendored 子进程 / HA subprocess。HermesAdapter 现在
继承 HttpGatewayAgentAdapter，作为「外部 agent」连接用户本地启动的 Hermes API server
（默认 http://127.0.0.1:8642，可用 HERMES_API_PORT 覆盖）。

本测试验证新的外部网关契约：
1. HermesAdapter 是 HttpGatewayAgentAdapter / BaseProcessAdapter 的子类
2. 类属性 cli_name / default_port / port_env / health_path 正确
3. _ha_available 外部模式下恒为 False
4. _ha_process property 代理到基类 self.process（向后兼容 agent_tool_loop.py）
5. display_name == "Hermes Agent"
6. capabilities() 返回 supports_stream=True
7. health() 不抛异常，gateway 不可达时返回 False
8. 静态检查: 不再定义 _get_env / _ha_port / _find_free_port / _ha_watchdog，
   但保留 _ha_process property
"""
import os
import sys
import asyncio

_SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src-python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from adapters.hermes import HermesAdapter
from adapters.ext_http import HttpGatewayAgentAdapter
from adapters.base import BaseProcessAdapter, AgentCapability

HERMES_DEFAULT_PORT = 8642


# ============================================================
# 1. 继承关系
# ============================================================

def test_hermes_is_subclass_of_http_gateway():
    """HermesAdapter 是 HttpGatewayAgentAdapter 的子类。"""
    assert issubclass(HermesAdapter, HttpGatewayAgentAdapter)


def test_hermes_is_subclass_of_base_process_adapter():
    """HermesAdapter 间接继承 BaseProcessAdapter（通过 HttpGatewayAgentAdapter）。"""
    assert issubclass(HermesAdapter, BaseProcessAdapter)


def test_hermes_instance_is_http_gateway():
    """HermesAdapter 实例是 HttpGatewayAgentAdapter / BaseProcessAdapter 实例。"""
    adapter = HermesAdapter()
    assert isinstance(adapter, HttpGatewayAgentAdapter)
    assert isinstance(adapter, BaseProcessAdapter)


# ============================================================
# 2. 类属性（外部网关配置）
# ============================================================

def test_cli_name():
    assert HermesAdapter.cli_name == "hermes"


def test_default_port():
    assert HermesAdapter.default_port == HERMES_DEFAULT_PORT


def test_port_env():
    assert HermesAdapter.port_env == "HERMES_API_PORT"


def test_health_path():
    assert HermesAdapter.health_path == "/health"


def test_display_name():
    """display_name 实例属性 = 'Hermes Agent'。"""
    adapter = HermesAdapter()
    assert adapter.display_name == "Hermes Agent"


# ============================================================
# 3. 外部模式标志 + _ha_process property 代理
# ============================================================

def test_ha_available_is_false_external_mode():
    """外部模式下 _ha_available 恒为 False（无内置 HA 子进程）。"""
    adapter = HermesAdapter()
    assert adapter._ha_available is False


def test_ha_process_property_proxies_to_base():
    """_ha_process property 读取基类 self.process。"""
    adapter = HermesAdapter()
    assert adapter._ha_process is None
    assert adapter.process is None
    sentinel = object()
    adapter.process = sentinel
    assert adapter._ha_process is sentinel


def test_ha_process_setter_writes_to_base():
    """_ha_process setter 写入基类 self.process。"""
    adapter = HermesAdapter()
    sentinel = object()
    adapter._ha_process = sentinel
    assert adapter.process is sentinel


# ============================================================
# 4. capabilities
# ============================================================

def test_capabilities_stream():
    adapter = HermesAdapter()
    cap = adapter.capabilities()
    assert isinstance(cap, AgentCapability)
    assert cap.supports_stream is True


# ============================================================
# 5. health() 不抛异常
# ============================================================

async def test_health_returns_bool_and_false_when_down():
    """gateway 不可达（_port=0）→ health() 返回 False 且不抛异常。"""
    adapter = HermesAdapter()
    adapter._port = 0  # 短路：_gateway_reachable 直接 False
    result = await adapter.health()
    assert result is False


# ============================================================
# 6. 静态检查: 移除了 vendored 子进程相关方法
# ============================================================

def _read_hermes_source() -> str:
    path = os.path.join(_SRC, "adapters", "hermes.py")
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_no_vendored_subprocess_methods():
    """静态检查: hermes.py 不再定义 _get_env / _ha_port / _find_free_port / _ha_watchdog。"""
    source = _read_hermes_source()
    for banned in ("def _get_env", "def _ha_port", "def _find_free_port", "def _ha_watchdog"):
        assert banned not in source, f"HermesAdapter must not define {banned} (external gateway mode)"


def test_ha_process_is_property():
    """静态检查: _ha_process 是 @property（代理到基类 self.process）。"""
    import ast
    source = _read_hermes_source()
    tree = ast.parse(source)
    cls_node = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.ClassDef) and n.name == "HermesAdapter")
    property_names = set()
    for item in cls_node.body:
        if isinstance(item, ast.FunctionDef):
            for dec in item.decorator_list:
                if isinstance(dec, ast.Name) and dec.id == "property":
                    property_names.add(item.name)
                if isinstance(dec, ast.Attribute) and dec.attr == "setter":
                    property_names.add(dec.value.id)
    assert "_ha_process" in property_names, "_ha_process must be a @property"
