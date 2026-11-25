"""tests/unit/test_e1_edit_file_preview.py —— E1: edit_file inline diff 预览 + 确认流

覆盖:
- edit_file 预览阶段: 返回 phase=preview / old_content / new_content / unified_diff / rel_path
- edit_file 不写入文件（预览只读）
- edit_file 错误: 文件不存在 / 无匹配
- _handle_edit 确认 handler: 重新应用 SEARCH/REPLACE 并写入文件
- _handle_edit 错误: 文件不存在 / 文件已改无匹配 / 路径校验
- 端到端: preview → dispatch_confirm → 文件正确写入
"""
import os
import sys
import asyncio
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """创建临时工作区，shared 子目录可写文件。"""
    ws = tmp_path / "ws"
    (ws / "shared").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("YUNCAT_WORKSPACE", str(ws))
    return ws


def _write_shared_file(ws, rel: str, content: str) -> str:
    """在 shared/ 下写文件，返回绝对路径。"""
    p = ws / "shared" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return str(p)


# ============================================================
# edit_file 预览阶段
# ============================================================

class TestEditFilePreview:
    """edit_file 预览阶段——返回 diff 数据但不写入文件。"""

    async def test_returns_preview_phase(self, workspace):
        """meta.phase == 'preview'，触发 execute_confirmable 进入确认流。"""
        _write_shared_file(workspace, "foo.txt", "hello world")
        from tools.builtin.system_tool_advanced import edit_file
        result = await edit_file("foo.txt", [{"search": "hello", "replace": "goodbye"}])
        assert result["meta"]["phase"] == "preview"
        assert result["success"] is True

    async def test_returns_old_and_new_content(self, workspace):
        """data 含 old_content / new_content 供前端 InlineDiff 渲染。"""
        _write_shared_file(workspace, "foo.txt", "hello world")
        from tools.builtin.system_tool_advanced import edit_file
        result = await edit_file("foo.txt", [{"search": "hello", "replace": "goodbye"}])
        data = result["data"]
        assert data["old_content"] == "hello world"
        assert data["new_content"] == "goodbye world"

    async def test_returns_unified_diff(self, workspace):
        """data 含 unified_diff 文本。"""
        _write_shared_file(workspace, "foo.txt", "line1\nline2\nline3\n")
        from tools.builtin.system_tool_advanced import edit_file
        result = await edit_file("foo.txt", [{"search": "line2", "replace": "LINE2"}])
        diff = result["data"]["unified_diff"]
        assert "line2" in diff
        assert "LINE2" in diff

    async def test_returns_rel_path_to_workspace_root(self, workspace):
        """data.path 相对 workspace 根（如 'shared/foo.txt'）——与 write_file 一致。

        这是 _resolve_pending_path 正确解析绝对路径的前提。
        注意: Path.relative_to 在 Windows 返回反斜杠——与 write_file 行为一致。
        """
        _write_shared_file(workspace, "foo.txt", "hello")
        from tools.builtin.system_tool_advanced import edit_file
        result = await edit_file("foo.txt", [{"search": "hello", "replace": "hi"}])
        # 跨平台: 统一替换分隔符后比较
        rel = result["data"]["path"].replace("\\", "/")
        assert rel == "shared/foo.txt"

    async def test_returns_matches_and_diff_summary(self, workspace):
        """data 含 matches / total_edits / diff_summary。"""
        _write_shared_file(workspace, "foo.txt", "aaa bbb aaa")
        from tools.builtin.system_tool_advanced import edit_file
        result = await edit_file("foo.txt", [{"search": "aaa", "replace": "XXX"}])
        assert result["data"]["matches"] == 1  # 只替换第一次出现
        assert result["data"]["total_edits"] == 1
        assert "1/1" in result["data"]["diff_summary"]

    async def test_does_not_write_file(self, workspace):
        """预览阶段不写入文件——原内容不变。"""
        _write_shared_file(workspace, "foo.txt", "hello world")
        from tools.builtin.system_tool_advanced import edit_file
        await edit_file("foo.txt", [{"search": "hello", "replace": "goodbye"}])
        # 文件内容应保持不变
        assert (workspace / "shared" / "foo.txt").read_text(encoding="utf-8") == "hello world"

    async def test_multiple_edits_some_failed(self, workspace):
        """多个 edit 块，部分匹配部分未匹配——返回 matches + failed 列表。"""
        _write_shared_file(workspace, "foo.txt", "alpha beta")
        from tools.builtin.system_tool_advanced import edit_file
        result = await edit_file("foo.txt", [
            {"search": "alpha", "replace": "ALPHA"},
            {"search": "gamma", "replace": "GAMMA"},  # 未匹配
        ])
        assert result["success"] is True
        assert result["data"]["matches"] == 1
        assert result["data"]["total_edits"] == 2
        assert result["data"]["failed"] is not None
        assert len(result["data"]["failed"]) == 1


# ============================================================
# edit_file 错误处理
# ============================================================

class TestEditFileErrors:
    """edit_file 预览阶段错误处理。"""

    async def test_file_not_found(self, workspace):
        """文件不存在返回失败。"""
        from tools.builtin.system_tool_advanced import edit_file
        result = await edit_file("nonexistent.txt", [{"search": "a", "replace": "b"}])
        assert result["success"] is False
        assert "不存在" in result["error"]

    async def test_no_matches_returns_failure(self, workspace):
        """所有 edit 块均未匹配返回失败。"""
        _write_shared_file(workspace, "foo.txt", "hello world")
        from tools.builtin.system_tool_advanced import edit_file
        result = await edit_file("foo.txt", [{"search": "xyz", "replace": "abc"}])
        assert result["success"] is False
        assert "未找到匹配" in result["error"]

    async def test_path_traversal_rejected(self, workspace):
        """路径穿越被 validate_path 拒绝。"""
        from tools.builtin.system_tool_advanced import edit_file
        result = await edit_file("../../../etc/passwd", [{"search": "a", "replace": "b"}])
        assert result["success"] is False


# ============================================================
# _handle_edit 确认 handler
# ============================================================

class TestHandleEditConfirm:
    """_handle_edit——确认后重新应用编辑并写入文件。"""

    async def test_writes_file_on_confirm(self, workspace):
        """确认后正确写入文件。"""
        _write_shared_file(workspace, "foo.txt", "hello world")
        from tools.builtin.system_tool_confirm import _handle_edit
        payload = {
            "path": str(workspace / "shared" / "foo.txt"),
            "edits": [{"search": "hello", "replace": "goodbye"}],
        }
        result = await _handle_edit(None, payload)
        assert result["success"] is True
        assert result["meta"]["phase"] == "executed"
        # 文件实际被写入
        written = (workspace / "shared" / "foo.txt").read_text(encoding="utf-8")
        assert written == "goodbye world"

    async def test_reapplies_multiple_edits(self, workspace):
        """多个 edit 块按顺序重新应用。"""
        _write_shared_file(workspace, "foo.txt", "alpha beta gamma")
        from tools.builtin.system_tool_confirm import _handle_edit
        payload = {
            "path": str(workspace / "shared" / "foo.txt"),
            "edits": [
                {"search": "alpha", "replace": "ALPHA"},
                {"search": "gamma", "replace": "GAMMA"},
            ],
        }
        result = await _handle_edit(None, payload)
        assert result["success"] is True
        assert result["data"]["matches"] == 2
        written = (workspace / "shared" / "foo.txt").read_text(encoding="utf-8")
        assert written == "ALPHA beta GAMMA"

    async def test_file_not_found_on_confirm(self, workspace):
        """确认时文件已被删除——返回失败。"""
        from tools.builtin.system_tool_confirm import _handle_edit
        payload = {
            "path": str(workspace / "shared" / "deleted.txt"),
            "edits": [{"search": "a", "replace": "b"}],
        }
        result = await _handle_edit(None, payload)
        assert result["success"] is False
        assert "不存在" in result["error"]

    async def test_file_changed_no_match_on_confirm(self, workspace):
        """preview→confirm 间文件被改，重新应用无匹配——返回失败（避免覆盖用户改动）。"""
        _write_shared_file(workspace, "foo.txt", "hello world")
        # 模拟文件在 preview 后被修改
        (workspace / "shared" / "foo.txt").write_text("completely different", encoding="utf-8")
        from tools.builtin.system_tool_confirm import _handle_edit
        payload = {
            "path": str(workspace / "shared" / "foo.txt"),
            "edits": [{"search": "hello", "replace": "goodbye"}],
        }
        result = await _handle_edit(None, payload)
        assert result["success"] is False
        assert "重新应用" in result["error"] or "失败" in result["error"]
        # 文件保持用户改动，未被覆盖
        assert (workspace / "shared" / "foo.txt").read_text(encoding="utf-8") == "completely different"

    async def test_path_validation_failure(self, workspace, tmp_path):
        """payload path 不在 shared/ 下——路径校验失败。"""
        # 在 shared 外创建文件
        outside = tmp_path / "outside.txt"
        outside.write_text("hello", encoding="utf-8")
        from tools.builtin.system_tool_confirm import _handle_edit
        payload = {
            "path": str(outside),
            "edits": [{"search": "hello", "replace": "world"}],
        }
        result = await _handle_edit(None, payload)
        assert result["success"] is False
        assert "路径验证" in result["error"]


# ============================================================
# 端到端: preview → dispatch_confirm
# ============================================================

class TestEditFileEndToEnd:
    """端到端: edit_file preview → create_pending_state → dispatch_confirm → 文件写入。"""

    async def test_full_confirm_flow(self, workspace, tmp_path):
        """完整确认流: preview 生成 token → dispatch_confirm 执行写入。"""
        _write_shared_file(workspace, "foo.txt", "hello world")
        # 1. preview
        from tools.builtin.system_tool_advanced import edit_file
        preview = await edit_file("foo.txt", [{"search": "hello", "replace": "goodbye"}])
        assert preview["meta"]["phase"] == "preview"
        assert preview["success"] is True

        # 2. 模拟 execute_confirmable 的 payload 构建
        from security.sandbox import get_workspace
        ws = get_workspace()
        data = preview["data"]
        resolved_path = str((ws / data["path"]).resolve())
        payload = {
            "path": resolved_path,
            "edits": [{"search": "hello", "replace": "goodbye"}],
        }

        # 3. 创建 pending state + dispatch
        import aiosqlite
        from repositories import SCHEMA_SQL
        db_path = str(tmp_path / "test_e1.db")
        db = await aiosqlite.connect(db_path)
        await db.executescript(SCHEMA_SQL)
        await db.commit()
        try:
            from tools.builtin.system_tool_confirm import create_pending_state, dispatch_confirm
            token = await create_pending_state(db, "pending_edit_file", payload)
            executed = await dispatch_confirm(db, token)
            assert executed["success"] is True
            assert executed["meta"]["phase"] == "executed"
            # 文件实际被写入
            written = (workspace / "shared" / "foo.txt").read_text(encoding="utf-8")
            assert written == "goodbye world"
        finally:
            await db.close()
            for _ in range(5):
                try:
                    os.unlink(db_path)
                    break
                except PermissionError:
                    await asyncio.sleep(0.05)

    async def test_state_type_map_includes_edit_file(self):
        """_STATE_TYPE_MAP 包含 edit_file → pending_edit_file 映射。

        Batch B M6: 映射已重构为模块级单一 dict（消除 auto-approve /
        非 auto-approve 两处重复定义），断言 1 处即可。
        """
        import re
        fp = os.path.join(os.path.dirname(__file__), "..", "..", "src-python",
                          "services", "tools", "tool_service.py")
        content = open(fp, encoding="utf-8").read()
        count = content.count('"edit_file": "pending_edit_file"')
        assert count == 1, f"edit_file 映射应在 _STATE_TYPE_MAP 单一来源出现，实际 {count} 次"
        # 且 edit_file 仍在映射表中（未被重构剥掉）
        assert '"edit_file": "pending_edit_file"' in content

    async def test_confirm_handler_registered(self):
        """CONFIRM_HANDLERS 注册了 pending_edit_file → _handle_edit。"""
        from tools.builtin.system_tool_confirm import CONFIRM_HANDLERS
        assert "pending_edit_file" in CONFIRM_HANDLERS
        assert CONFIRM_HANDLERS["pending_edit_file"].__name__ == "_handle_edit"


# ============================================================
# Task 6.12 (F-042): edit_file replace_all 参数
# ============================================================

class TestEditFileReplaceAll:
    """Task 6.12: edit_file replace_all=True 替换所有匹配（对标 Claude Code）。

    默认 replace_all=False 只替换第一个匹配（向后兼容）。
    """

    async def test_preview_replace_all_replaces_all_matches(self, workspace):
        """preview 阶段 replace_all=True 替换所有匹配。"""
        _write_shared_file(workspace, "foo.txt", "aaa bbb aaa ccc aaa")
        from tools.builtin.system_tool_advanced import edit_file
        result = await edit_file("foo.txt", [
            {"search": "aaa", "replace": "XXX", "replace_all": True},
        ])
        assert result["success"] is True
        assert result["data"]["matches"] == 3  # 3 个 aaa 都替换
        assert result["data"]["new_content"] == "XXX bbb XXX ccc XXX"

    async def test_preview_default_only_first_match(self, workspace):
        """默认（replace_all 不传或 False）只替换第一个匹配——向后兼容。"""
        _write_shared_file(workspace, "foo.txt", "aaa bbb aaa ccc aaa")
        from tools.builtin.system_tool_advanced import edit_file
        # 不传 replace_all
        result1 = await edit_file("foo.txt", [{"search": "aaa", "replace": "XXX"}])
        assert result1["data"]["matches"] == 1
        assert result1["data"]["new_content"] == "XXX bbb aaa ccc aaa"
        # 显式 replace_all=False
        result2 = await edit_file("foo.txt", [{"search": "aaa", "replace": "XXX", "replace_all": False}])
        assert result2["data"]["matches"] == 1

    async def test_preview_replace_all_no_match_fails(self, workspace):
        """replace_all=True 但无匹配 → 失败（与默认行为一致）。"""
        _write_shared_file(workspace, "foo.txt", "hello world")
        from tools.builtin.system_tool_advanced import edit_file
        result = await edit_file("foo.txt", [
            {"search": "xyz", "replace": "XXX", "replace_all": True},
        ])
        assert result["success"] is False
        assert "未找到匹配" in result["error"]

    async def test_preview_mixed_replace_all_and_default(self, workspace):
        """混合：一个 edit replace_all=True，另一个默认 False。"""
        _write_shared_file(workspace, "foo.txt", "foo bar foo baz foo")
        from tools.builtin.system_tool_advanced import edit_file
        result = await edit_file("foo.txt", [
            {"search": "foo", "replace": "FOO", "replace_all": True},  # 替换所有 foo
            {"search": "bar", "replace": "BAR"},  # 只替换第一个 bar（默认）
        ])
        assert result["success"] is True
        # foo 替换 3 个 + bar 替换 1 个 = 4
        assert result["data"]["matches"] == 4
        assert result["data"]["new_content"] == "FOO BAR FOO baz FOO"

    async def test_confirm_handle_replace_all_writes_file(self, workspace):
        """_handle_edit 确认阶段支持 replace_all=True 并写入文件。"""
        _write_shared_file(workspace, "foo.txt", "aaa bbb aaa ccc aaa")
        from tools.builtin.system_tool_confirm import _handle_edit
        payload = {
            "path": str(workspace / "shared" / "foo.txt"),
            "edits": [{"search": "aaa", "replace": "XXX", "replace_all": True}],
        }
        result = await _handle_edit(None, payload)
        assert result["success"] is True
        assert result["data"]["matches"] == 3
        written = (workspace / "shared" / "foo.txt").read_text(encoding="utf-8")
        assert written == "XXX bbb XXX ccc XXX"

    async def test_confirm_handle_default_only_first_match(self, workspace):
        """_handle_edit 默认（无 replace_all）只替换第一个——向后兼容。"""
        _write_shared_file(workspace, "foo.txt", "aaa bbb aaa")
        from tools.builtin.system_tool_confirm import _handle_edit
        payload = {
            "path": str(workspace / "shared" / "foo.txt"),
            "edits": [{"search": "aaa", "replace": "XXX"}],  # 无 replace_all
        }
        result = await _handle_edit(None, payload)
        assert result["success"] is True
        assert result["data"]["matches"] == 1
        written = (workspace / "shared" / "foo.txt").read_text(encoding="utf-8")
        assert written == "XXX bbb aaa"

    async def test_e2e_replace_all_preview_to_confirm(self, workspace, tmp_path):
        """端到端：preview（replace_all=True）→ dispatch_confirm → 文件写入。"""
        _write_shared_file(workspace, "foo.txt", "old old old")
        # 1. preview
        from tools.builtin.system_tool_advanced import edit_file
        preview = await edit_file("foo.txt", [
            {"search": "old", "replace": "new", "replace_all": True},
        ])
        assert preview["success"] is True
        assert preview["data"]["matches"] == 3

        # 2. 构建 payload 并 dispatch
        from security.sandbox import get_workspace
        ws = get_workspace()
        data = preview["data"]
        resolved_path = str((ws / data["path"]).resolve())
        payload = {
            "path": resolved_path,
            "edits": [{"search": "old", "replace": "new", "replace_all": True}],
        }

        import aiosqlite
        from repositories import SCHEMA_SQL
        db_path = str(tmp_path / "test_replace_all.db")
        db = await aiosqlite.connect(db_path)
        await db.executescript(SCHEMA_SQL)
        await db.commit()
        try:
            from tools.builtin.system_tool_confirm import create_pending_state, dispatch_confirm
            token = await create_pending_state(db, "pending_edit_file", payload)
            executed = await dispatch_confirm(db, token)
            assert executed["success"] is True
            written = (workspace / "shared" / "foo.txt").read_text(encoding="utf-8")
            assert written == "new new new"
        finally:
            await db.close()
            for _ in range(5):
                try:
                    os.unlink(db_path)
                    break
                except PermissionError:
                    await asyncio.sleep(0.05)
