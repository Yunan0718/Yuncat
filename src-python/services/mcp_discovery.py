"""services/mcp_discovery.py —— Phase 51-D: MCP 本地配置自动发现

扫描常见 MCP 客户端配置文件：
- Claude Desktop: claude_desktop_config.json
- Cursor: .cursor/mcp.json

返回统一格式的候选服务器列表，供用户一键导入 Yuncat。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

_log = logging.getLogger("yuncat.mcp.discovery")


def _home_dir() -> str:
    """跨平台获取用户主目录。"""
    return os.path.expanduser("~")


def _config_paths() -> list[tuple[str, str]]:
    """返回 (source, path) 候选列表。

    支持 YUNCAT_MCP_DISCOVERY_PATHS 环境变量覆盖默认路径，
    格式："source:path;source:path"（用于测试或自定义路径）。
    """
    env_override = os.environ.get("YUNCAT_MCP_DISCOVERY_PATHS")
    if env_override:
        paths: list[tuple[str, str]] = []
        for segment in env_override.split(";"):
            segment = segment.strip()
            if ":" not in segment:
                continue
            source, path = segment.split(":", 1)
            paths.append((source.strip(), path.strip()))
        return paths

    home = _home_dir()
    paths = []

    # Claude Desktop
    claude_name = "claude_desktop_config.json"
    paths.append(("claude_desktop", os.path.join(os.environ.get("APPDATA", ""), "Claude", claude_name)))
    paths.append(("claude_desktop", os.path.join(home, "Library", "Application Support", "Claude", claude_name)))
    paths.append(("claude_desktop", os.path.join(home, ".config", "Claude", claude_name)))

    # Cursor
    paths.append(("cursor", os.path.join(home, ".cursor", "mcp.json")))

    return paths


def _parse_mcp_servers(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 mcpServers 字段为统一服务器列表。"""
    servers: list[dict[str, Any]] = []
    raw_servers = data.get("mcpServers")
    if not isinstance(raw_servers, dict):
        return servers

    for name, cfg in raw_servers.items():
        if not isinstance(cfg, dict):
            continue
        command = cfg.get("command")
        if not command:
            continue
        servers.append({
            "name": name,
            "command": command,
            "args": list(cfg.get("args") or []),
            "env": dict(cfg.get("env") or {}),
        })
    return servers


def _discover_from_path(path: str, source: str) -> dict[str, Any] | None:
    """从单个配置文件加载 MCP 服务器。失败返回 None。"""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        _log.debug("Failed to read MCP config %s: %s", path, exc)
        return None

    servers = _parse_mcp_servers(data)
    if not servers:
        return None

    return {"source": source, "path": path, "servers": servers}


def discover_local_mcp_configs() -> list[dict[str, Any]]:
    """扫描本地 MCP 客户端配置，返回发现的候选服务器分组。"""
    results: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    for source, path in _config_paths():
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        result = _discover_from_path(path, source)
        if result:
            results.append(result)

    return results
