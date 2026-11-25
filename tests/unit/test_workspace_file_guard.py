"""tests/unit/test_workspace_file_guard.py —— 工作区读/存的数据安全守卫

覆盖 2026-09-15 新增的两条保护：
  1. 二进制文件按**只读**打开——原实现一律按 utf-8(errors="replace") 读成乱码，
     用户一旦保存就把替换字符写回磁盘，等于毁掉原文件；
  2. 保存携带「打开时的 mtime」（乐观并发）——文件被 Agent / 外部编辑器改过时
     回 409 交用户决定，而不是静默覆盖别人的修改。

直接调用路由函数（不起 HTTP 层），工作区根由 YUNCAT_WORKSPACE 指向临时目录。
"""
import os

import pytest
from fastapi import HTTPException

from routes import workspace as ws_mod


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path))
    (tmp_path / "shared").mkdir(parents=True, exist_ok=True)
    return tmp_path


async def test_read_detects_binary_file(workspace):
    """含 NUL 字节的文件按二进制返回（content 为空 + binary=True）。"""
    target = workspace / "shared" / "logo.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")

    res = await ws_mod.read_workspace_file(path="shared/logo.png")

    assert res["binary"] is True
    assert res["content"] == ""
    assert res["modified"] > 0


async def test_read_text_file_is_not_binary(workspace):
    target = workspace / "shared" / "app.py"
    target.write_text("print('hi')\n", encoding="utf-8")

    res = await ws_mod.read_workspace_file(path="shared/app.py")

    assert res["binary"] is False
    assert "print" in res["content"]


async def test_save_rejects_stale_mtime(workspace):
    """磁盘 mtime 与打开时不一致 → 409，且**不写入**。"""
    target = workspace / "shared" / "conflict.py"
    target.write_text("v1\n", encoding="utf-8")
    opened = await ws_mod.read_workspace_file(path="shared/conflict.py")

    # 模拟外部改写（Agent / 另一个编辑器）——显式推进 mtime，避免秒级精度干扰
    bumped = opened["modified"] + 30
    os.utime(target, (bumped, bumped))
    target.write_text("v2\n", encoding="utf-8")
    os.utime(target, (bumped, bumped))

    with pytest.raises(HTTPException) as exc:
        await ws_mod.save_workspace_file(
            {"path": "shared/conflict.py", "content": "mine\n",
             "expected_mtime": opened["modified"]},
            None,
        )

    assert exc.value.status_code == 409
    assert target.read_text(encoding="utf-8") == "v2\n"  # 磁盘内容未被覆盖


async def test_save_with_matching_mtime_writes_and_returns_new_mtime(workspace):
    target = workspace / "shared" / "ok.py"
    target.write_text("v1\n", encoding="utf-8")
    opened = await ws_mod.read_workspace_file(path="shared/ok.py")

    res = await ws_mod.save_workspace_file(
        {"path": "shared/ok.py", "content": "v2\n", "expected_mtime": opened["modified"]},
        None,
    )

    assert res["status"] == "ok"
    assert res["modified"] >= 1
    assert target.read_text(encoding="utf-8") == "v2\n"


async def test_save_without_expected_mtime_still_writes(workspace):
    """不带 expected_mtime（首次保存/用户确认覆盖）时照常写入。"""
    target = workspace / "shared" / "force.py"
    target.write_text("v1\n", encoding="utf-8")

    res = await ws_mod.save_workspace_file({"path": "shared/force.py", "content": "v2\n"}, None)

    assert res["status"] == "ok"
    assert target.read_text(encoding="utf-8") == "v2\n"
