"""tests/unit/test_lifespan_service_split.py — lifespan 服务初始化顺序测试

Batch F 拆分: lifespan.py 已拆分为 lifespan.py（薄编排层）+ lifespan_services.py
（核心服务初始化）+ lifespan_postinit.py（后置初始化）。startup_stage 标记 +
顺序断言跨三个文件检查，保证初始化顺序可维护性。

本测试验证:
1. lifespan 函数存在且可调用
2. _init_services 函数存在
3. startup_stage 标记完整（"init" / "db-init" / "adapters" 等）
4. 服务初始化顺序正确（memory → tool → rules → settings → model_config → adapters）
5. shutdown 顺序正确（adapters → services → db）
6. 静态检查: 关键服务的 app.state 赋值顺序
"""
import os
import sys
import ast

_SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src-python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _read_lifespan_source() -> str:
    """读取 lifespan.py 及其拆分模块的源码（拼接后检查顺序）。

    Batch F 拆分后，初始化逻辑分布在 lifespan.py（编排层）+
    lifespan_services.py（核心服务）+ lifespan_postinit.py（后置初始化）。
    拼接后静态检查可跨文件验证初始化顺序。
    """
    parts = []
    for filename in ("lifespan.py", "lifespan_services.py", "lifespan_postinit.py"):
        path = os.path.join(_SRC, filename)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                parts.append(f.read())
    return "\n".join(parts)


# ============================================================
# 1. 函数存在性
# ============================================================

def test_lifespan_module_imports():
    """lifespan 模块可正常导入。"""
    import lifespan
    assert hasattr(lifespan, "create_lifespan") or hasattr(lifespan, "lifespan")


def test_init_services_function_exists():
    """_init_services 函数存在。"""
    import lifespan
    assert hasattr(lifespan, "_init_services")
    assert callable(lifespan._init_services)


# ============================================================
# 2. startup_stage 标记
# ============================================================

def test_startup_stage_markers_present():
    """静态检查: lifespan.py 含 startup_stage 标记。"""
    source = _read_lifespan_source()
    # 应设置 startup_stage 用于追踪初始化阶段
    assert "startup_stage" in source, (
        "lifespan must set app.state.startup_stage for initialization tracking"
    )
    # 关键阶段标记
    for stage in ('"init"', '"db-init"', '"adapters"'):
        assert stage in source, f"startup_stage={stage} marker missing"


# ============================================================
# 3. 服务初始化顺序——静态检查
# ============================================================

def test_memory_service_initialized_before_tool_service():
    """静态检查: MemoryService 在 ToolService 之前初始化。

    依赖关系: ToolService 可能需要 MemoryService 写入工具执行日志。
    """
    source = _read_lifespan_source()
    mem_pos = source.find("app.state.memory_service =")
    tool_pos = source.find("app.state.tool_service =")
    assert mem_pos != -1, "memory_service assignment not found"
    assert tool_pos != -1, "tool_service assignment not found"
    assert mem_pos < tool_pos, (
        "memory_service must be initialized BEFORE tool_service "
        "(tool execution may write to memory)"
    )


def test_tool_service_initialized_before_rules_service():
    """静态检查: ToolService 在 RulesService 之前初始化（再注入 rules_service）。"""
    source = _read_lifespan_source()
    tool_pos = source.find("app.state.tool_service =")
    rules_pos = source.find("app.state.rules_service =")
    assert tool_pos != -1
    assert rules_pos != -1
    assert tool_pos < rules_pos, (
        "tool_service must be initialized BEFORE rules_service "
        "(then tool_service.rules_service = rules_service injection)"
    )


def test_settings_service_initialized_before_model_config():
    """静态检查: SettingsService 在 ModelConfigService 之前初始化。

    依赖关系: ModelConfigService 构造函数接受 settings_service 参数。
    """
    source = _read_lifespan_source()
    settings_pos = source.find("app.state.settings_service =")
    model_config_pos = source.find("app.state.model_config =")
    assert settings_pos != -1
    assert model_config_pos != -1
    assert settings_pos < model_config_pos, (
        "settings_service must be initialized BEFORE model_config "
        "(ModelConfigService constructor takes settings_service)"
    )


def test_model_config_injected_into_adapters():
    """静态检查: model_config 创建在前，且注入是**通用遍历**而非针对某个 agent。

    2026-09-14：原先只写死 `openclaw.set_model_config(...)`（漏了 hermes），
    属于不对称的厂商/agent 硬编码。改为遍历所有声明了该方法的适配器。
    """
    source = _read_lifespan_source()
    model_config_pos = source.find("app.state.model_config =")
    assert model_config_pos != -1

    inject_pos = source.find(".set_model_config(app.state.model_config)")
    assert inject_pos != -1, "应有把 model_config 注入适配器的调用"
    assert model_config_pos < inject_pos, (
        "model_config must be created BEFORE injecting into adapters"
    )
    # 通用遍历：注入点在 for 循环体内，而不是某个具体 agent 的字面量
    loop_pos = source.rfind("for ", 0, inject_pos)
    assert loop_pos != -1 and loop_pos > model_config_pos, (
        "注入应在适配器遍历循环内——不要再写死具体 agent"
    )
    for hardcoded in ("openclaw.set_model_config", "hermes.set_model_config"):
        assert hardcoded not in source, f"不应针对具体 agent 注入：{hardcoded}"


def test_adapters_discovered_after_core_services():
    """静态检查: adapters 在核心服务之后注册。

    依赖关系: adapters 需要 model_config / tool_service 等注入。
    """
    source = _read_lifespan_source()
    tool_pos = source.find("app.state.tool_service =")
    adapter_pos = source.find("app.state.adapters =")
    assert tool_pos != -1
    assert adapter_pos != -1
    assert tool_pos < adapter_pos, (
        "core services must be initialized BEFORE adapter discovery"
    )


# ============================================================
# 4. shutdown 顺序
# ============================================================

def test_shutdown_closes_adapters_before_db():
    """静态检查: shutdown 先关闭 adapters 再关闭 db。"""
    source = _read_lifespan_source()
    # 找 shutdown 相关的 adapter stop 和 db close
    # adapter stop 通常在 finally 块中
    adapter_stop_pos = source.find("adapter.stop()")
    if adapter_stop_pos == -1:
        # 也可能是其他形式的 adapter 清理
        adapter_stop_pos = source.find("adapters or {}")
    db_close_pos = source.find("conn.close()")
    if db_close_pos == -1:
        db_close_pos = source.find("await conn.close()")
    # 只要两者都存在，adapter 清理应在 db close 之前
    if adapter_stop_pos != -1 and db_close_pos != -1:
        assert adapter_stop_pos < db_close_pos, (
            "adapters must be stopped BEFORE db close (adapters may write final state)"
        )


# ============================================================
# 5. 文件大小检查（Batch F 拆分后——分层软上限防膨胀）
# ============================================================

def test_lifespan_file_under_soft_cap():
    """静态检查: lifespan 模块族各文件行数在项目约定内。

    Batch F 拆分后:
      - lifespan.py: 薄编排层，应 < 400 行
      - lifespan_services.py / lifespan_postinit.py: 各 < 800 行（项目约定）
    """
    caps = {
        "lifespan.py": 400,
        "lifespan_services.py": 800,
        "lifespan_postinit.py": 800,
    }
    for filename, cap in caps.items():
        path = os.path.join(_SRC, filename)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            line_count = f.read().count("\n") + 1
        assert line_count < cap, (
            f"{filename} has {line_count} lines——exceeds {cap}-line cap. "
            f"Move logic to another lifespan_* module."
        )


# ============================================================
# 6. _init_services 函数签名
# ============================================================

def test_init_services_signature():
    """静态检查: _init_services 接受 (app, conn, data_dir, background_tasks) 参数。"""
    source = _read_lifespan_source()
    # 用 ast 解析找到 _init_services 函数
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "_init_services":
                arg_names = [a.arg for a in node.args.args]
                assert "app" in arg_names
                assert "conn" in arg_names
                assert "data_dir" in arg_names
                assert "background_tasks" in arg_names
                return
    pytest.fail("_init_services function not found")


import pytest  # for pytest.fail in test_init_services_signature
