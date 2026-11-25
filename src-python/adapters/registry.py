"""adapters/registry.py —— Adapter 注册与发现

register_adapter(name, config) 装饰器 + discover_adapters() 自动扫描。
"""
import logging
from typing import Any

from .base import BaseProcessAdapter

_log = logging.getLogger("yuncat.adapter")

_registry: dict[str, tuple[type[BaseProcessAdapter], dict]] = {}
# Phase 31: 全局 adapters 引用——供 delegate_to_agent 工具使用
_global_adapters: dict[str, Any] | None = None

# 已下线的 adapter 名单（当前为空）。
# 设计取舍：模块文件保留在 adapters/ 下（可随时恢复、不破坏 git 历史），
# 在发现阶段即跳过——既不 import 也不实例化。
# 2026-09-10: hermes 曾因提示词未同步而被误下线，现已恢复为标准外部网关 agent
# （与 openclaw/codex/claude 同等地位：默认关、禁自动路由、仅 MAX 模式可显式调用）。
DISABLED_ADAPTERS: set[str] = set()


def set_global_adapters(adapters: dict) -> None:
    global _global_adapters
    _global_adapters = adapters


def get_global_adapters() -> dict[str, Any]:
    return _global_adapters or {}


def register_adapter(name: str, config: dict | None = None):
    """装饰器：注册 Adapter 类"""
    def decorator(cls):
        _registry[name] = (cls, config or {})
        return cls
    return decorator


def discover_adapters() -> dict[str, BaseProcessAdapter]:
    """自动扫描 adapters/ 目录并实例化所有注册的 Adapter"""
    import importlib
    import pkgutil
    import adapters as pkg

    for _, module_name, _ in pkgutil.iter_modules(pkg.__path__):
        # 已下线 adapter：连 import 都跳过（见 DISABLED_ADAPTERS 注释）
        if module_name in DISABLED_ADAPTERS:
            _log.info("Adapter '%s' is disabled — skipping discovery", module_name)
            continue
        try:
            importlib.import_module(f"adapters.{module_name}")
        except ImportError as e:
            _log.warning("Failed to import adapter '%s': %s", module_name, e)

    instances: dict[str, BaseProcessAdapter] = {}
    for name, (cls, conf) in _registry.items():
        # 双保险：即便由其他路径注册进来，已下线名单内的也不实例化
        if name in DISABLED_ADAPTERS:
            continue
        instances[name] = cls(**conf)
    return instances
