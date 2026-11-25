"""services/host_bridge.py —— 桌面宿主桥（Python 侧唯一出口）

Electron 主进程在 loopback 上开了一个只认随机 token 的端点（src-electron/browser-bridge.js），
把「内置浏览器」的动作以固定白名单暴露给 Python。本模块是它的**唯一**客户端。

设计要点：
- 端口与 token 每次启动随机生成，由 Electron 通过环境变量交给自己的 Python 子进程；
  不落盘、不写死，因此不存在「端口冲突导致功能整体失效」这一类问题。
- 没有环境变量时（命令行单独跑后端、CI 跑测试）所有调用统一返回 ok=False，
  由工具层翻译成用户能看懂的话——不抛异常，也不假装成功。
- 响应体大小设上限：截图走 base64，超大响应直接拒绝，避免内存被一张图打爆。

超时是**可调的人为上限**：常规动作 30 秒；导航、截图这类可能慢的动作由调用方传入更长超时。
数值集中在本模块，改一个地方即可，不散落到各工具里。
"""
from __future__ import annotations

import logging
import os

import httpx

_log = logging.getLogger("yuncat.host_bridge")

ENV_HOST_BRIDGE_PORT = "YUNCAT_HOST_BRIDGE_PORT"
ENV_HOST_BRIDGE_TOKEN = "YUNCAT_HOST_BRIDGE_TOKEN"

# 人为上限（可调）：常规动作 / 慢动作 / 单次响应体
HOST_BRIDGE_TIMEOUT_SEC = 30.0
HOST_BRIDGE_LONG_TIMEOUT_SEC = 120.0
HOST_BRIDGE_MAX_RESPONSE_BYTES = 32 * 1024 * 1024

UNAVAILABLE_REASON = (
    "当前进程没有连到 Yuncat 桌面端的内置浏览器。"
    "browser_* 工具只在 Yuncat 桌面应用里可用（命令行单独启动后端时属于正常现象）。"
)


def bridge_endpoint() -> tuple[int, str] | None:
    """从环境变量读宿主桥地址；未注入或格式非法时返回 None。"""
    raw_port = (os.environ.get(ENV_HOST_BRIDGE_PORT) or "").strip()
    token = (os.environ.get(ENV_HOST_BRIDGE_TOKEN) or "").strip()
    if not raw_port or not token:
        return None
    try:
        port = int(raw_port)
    except ValueError:
        _log.warning("host bridge port is not a number: %r", raw_port)
        return None
    if not (1 <= port <= 65535):
        return None
    return port, token


def is_available() -> bool:
    """内置浏览器当前是否可用（仅看环境是否就绪，不做网络探测）。"""
    return bridge_endpoint() is not None


async def call(action: str, params: dict | None = None, *, timeout: float | None = None) -> dict:
    """调用一个宿主桥动作，返回 {"ok", "data", "error"}。

    绝不抛异常：网络不可达、返回非 JSON、响应过大都折算成 ok=False + 可读原因，
    调用方（工具层）据此生成用户能懂的提示。
    """
    endpoint = bridge_endpoint()
    if endpoint is None:
        return {"ok": False, "data": None, "error": UNAVAILABLE_REASON}
    port, token = endpoint
    url = f"http://127.0.0.1:{port}/host/invoke"
    payload = {"action": action, "params": params or {}}
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=timeout or HOST_BRIDGE_TIMEOUT_SEC) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        _log.warning("host bridge request failed (%s): %s", action, exc)
        return {"ok": False, "data": None, "error": f"内置浏览器通道不可达：{exc}"}

    if response.status_code != 200:
        _log.warning("host bridge returned HTTP %s for %s", response.status_code, action)
        return {"ok": False, "data": None, "error": f"内置浏览器通道返回 HTTP {response.status_code}"}
    if len(response.content) > HOST_BRIDGE_MAX_RESPONSE_BYTES:
        _log.warning("host bridge response too large (%s bytes) for %s", len(response.content), action)
        return {"ok": False, "data": None, "error": "内置浏览器返回内容过大，已拒绝处理"}

    try:
        body = response.json()
    except ValueError as exc:
        _log.warning("host bridge returned non-JSON for %s: %s", action, exc)
        return {"ok": False, "data": None, "error": "内置浏览器通道返回了非 JSON 内容"}

    if not isinstance(body, dict):
        return {"ok": False, "data": None, "error": "内置浏览器通道返回了非预期结构"}
    return {
        "ok": bool(body.get("ok")),
        "data": body.get("data"),
        "error": str(body.get("error") or ""),
    }
