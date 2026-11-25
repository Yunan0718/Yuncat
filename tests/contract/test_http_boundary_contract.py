"""contract: HTTP 边界不得漏出内部对象 / 不得把协程当值用

两次实测（2026-09-23）打出来的两个 500，根因都在「进程内契约被直接搬到 HTTP 边界」：

1. `/api/v1/tools/preview`
   `ToolService.execute_confirmable()` 会在返回值里挂 `_confirm_future`（asyncio.Future），
   让进程内的工具循环 await 用户确认。路由直接把这个 dict 交给 FastAPI，
   Pydantic 序列化 Future 失败 → 该接口恒 500，二阶段确认按钮永远弹不出来。
   工具循环那条路径自己 pop 了该字段（services/agent/confirmable.py），HTTP 这条没有。

2. `/api/v1/config/export`
   `SettingsService.get_decrypted()` 是 async，导出时漏了 await，
   把协程对象塞进响应 → `TypeError: 'coroutine' object is not iterable`。

本文件把这两条钉死：路由层的返回必须只含可 JSON 序列化的值，
且不得带下划线前缀的内部字段。
"""
import asyncio
import inspect
import json
import os
import re
import sys

import aiosqlite
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from repositories.schema import SCHEMA_SQL  # noqa: E402
from tools.result import ToolResult  # noqa: E402

_SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src-python")


def _read(rel: str) -> str:
    with open(os.path.join(_SRC, rel), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    yield conn
    await conn.close()


async def _preview_result(db) -> dict:
    """走真实 execute_confirmable，拿回它给路由的那份 dict。"""
    from services.tools.tool_service import ToolService
    from tools import registry as _registry

    if "write_file" not in _registry._tools:
        import conftest as _cf  # type: ignore
        _cf.load_all_builtin_tools()
    ts = ToolService()
    return await ts.execute_confirmable(
        db, "write_file", {"path": "boundary_probe.txt", "content": "x"},
    )


async def test_execute_confirmable_attaches_future(db):
    """前提：内部确实会挂 Future——否则下面的剥离断言是空跑。"""
    result = await _preview_result(db)
    assert result.get("success") is True
    assert isinstance(result.get("_confirm_future"), asyncio.Future)


async def test_http_boundary_strips_internal_fields(db):
    """路由层必须剥掉下划线前缀的内部字段（含 asyncio.Future）。"""
    result = await _preview_result(db)
    cleaned = {k: v for k, v in result.items() if not k.startswith("_")}
    # 剥离后必须能 JSON 序列化——这正是原 500 的复现点
    restored = json.loads(json.dumps(cleaned, ensure_ascii=False))
    assert "confirm_token" in restored
    assert not any(k.startswith("_") for k in restored)


async def test_preview_route_filters_before_returning():
    """源码级守卫：preview_tool 必须在 return 前过滤内部字段。"""
    src = _read("routes/tools.py")
    body = src.split("async def preview_tool", 1)[1].split("\n@router", 1)[0]
    assert re.search(r"startswith\([\"']_[\"']\)", body), (
        "preview_tool 未过滤下划线前缀的内部字段——execute_confirmable 会把 "
        "asyncio.Future 放进返回值，直接返回必然 500"
    )


async def test_config_export_awaits_decrypt():
    """源码级守卫：get_decrypted 是 async，导出时必须 await。"""
    src = _read("routes/config.py")
    assert "await svc.get_decrypted(" in src, (
        "config/export 漏了 await —— 协程对象进响应体，接口必 500"
    )
    assert not re.search(r"(?<!await )svc\.get_decrypted\(", src)


def test_get_decrypted_is_async():
    """前提：get_decrypted 确实是协程函数，漏 await 才会出问题。"""
    from services.settings_service import SettingsService

    assert inspect.iscoroutinefunction(SettingsService.get_decrypted)