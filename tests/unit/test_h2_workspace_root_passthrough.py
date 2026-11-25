"""tests/unit/test_h2_workspace_root_passthrough.py —— H2: 文件操作预览 workspace_root 透传测试

验证 write_file / edit_file / delete_file 预览阶段返回的 data 中包含 workspace_root 字段，
供前端确认弹窗可视化安全边界。对标 Claude Code 文件审批弹窗的工作区路径展示。

H2 范围:
- write_file 预览结果包含 workspace_root（绝对路径字符串）
- edit_file 预览结果包含 workspace_root
- delete_file 预览结果包含 workspace_root
- workspace_root 值与 get_workspace() 一致
- 缺失 workspace_root 时不影响原有功能（向后兼容）
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """隔离的工作区——文件操作工具内部依赖 get_workspace / get_shared_workspace"""
    ws = str(tmp_path / "workspace")
    shared = os.path.join(ws, "shared")
    os.makedirs(shared, exist_ok=True)
    monkeypatch.setenv("YUNCAT_WORKSPACE", ws)
    # 触发 sandbox 重新读取 YUNCAT_WORKSPACE env（与 H1 测试一致的模式）
    from security.sandbox import get_workspace, get_shared_workspace
    get_workspace()
    get_shared_workspace()
    return ws


class TestH2WorkspaceRootPassthrough:
    """H2: 文件操作预览阶段透传 workspace_root"""

    @pytest.mark.asyncio
    async def test_write_file_preview_includes_workspace_root(self, workspace):
        """write_file 预览 data 应包含 workspace_root 字段"""
        # 先创建一个已存在的文件（测试 overwrite 模式的 diff）
        test_file = os.path.join(workspace, "shared", "test_write.txt")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("old content")

        from tools.builtin.system_tool_file_ops import write_file
        result = await write_file(path="test_write.txt", content="new content")

        assert result["success"] is True, f"write_file 预览失败: {result.get('error')}"
        data = result["data"]
        assert "workspace_root" in data, "write_file 预览缺少 workspace_root 字段"
        assert data["workspace_root"] == workspace, (
            f"workspace_root 不匹配: expected {workspace}, got {data['workspace_root']}"
        )

    @pytest.mark.asyncio
    async def test_edit_file_preview_includes_workspace_root(self, workspace):
        """edit_file 预览 data 应包含 workspace_root 字段"""
        test_file = os.path.join(workspace, "shared", "test_edit.txt")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("hello world\nfoo bar\n")

        from tools.builtin.system_tool_advanced import edit_file
        result = await edit_file(
            path="test_edit.txt",
            edits=[{"search": "hello", "replace": "hi"}],
        )

        assert result["success"] is True, f"edit_file 预览失败: {result.get('error')}"
        data = result["data"]
        assert "workspace_root" in data, "edit_file 预览缺少 workspace_root 字段"
        assert data["workspace_root"] == workspace, (
            f"workspace_root 不匹配: expected {workspace}, got {data['workspace_root']}"
        )

    @pytest.mark.asyncio
    async def test_delete_file_preview_includes_workspace_root(self, workspace):
        """delete_file 预览 data 应包含 workspace_root 字段"""
        test_file = os.path.join(workspace, "shared", "test_delete.txt")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("to be deleted")

        from tools.builtin.system_tool_file_ops import delete_file
        result = await delete_file(path="shared/test_delete.txt")

        assert result["success"] is True, f"delete_file 预览失败: {result.get('error')}"
        data = result["data"]
        assert "workspace_root" in data, "delete_file 预览缺少 workspace_root 字段"
        assert data["workspace_root"] == workspace, (
            f"workspace_root 不匹配: expected {workspace}, got {data['workspace_root']}"
        )

    @pytest.mark.asyncio
    async def test_write_file_new_file_includes_workspace_root(self, workspace):
        """write_file 新建文件场景也应包含 workspace_root"""
        from tools.builtin.system_tool_file_ops import write_file
        result = await write_file(path="brand_new.txt", content="fresh content")

        assert result["success"] is True
        assert "workspace_root" in result["data"]
        assert result["data"]["workspace_root"] == workspace

    @pytest.mark.asyncio
    async def test_workspace_root_is_absolute_path(self, workspace):
        """workspace_root 应为绝对路径字符串"""
        from tools.builtin.system_tool_file_ops import write_file
        result = await write_file(path="abs_check.txt", content="x")

        ws_root = result["data"]["workspace_root"]
        assert isinstance(ws_root, str)
        assert os.path.isabs(ws_root), f"workspace_root 不是绝对路径: {ws_root}"

    @pytest.mark.asyncio
    async def test_three_tools_consistent_workspace_root(self, workspace):
        """三个文件操作工具返回的 workspace_root 应一致"""
        # 手动创建文件（write_file 预览阶段不实际写入文件）
        test_file = os.path.join(workspace, "shared", "consistency.txt")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("hello world")

        from tools.builtin.system_tool_file_ops import write_file, delete_file
        from tools.builtin.system_tool_advanced import edit_file

        wf_result = await write_file(path="consistency.txt", content="hello world")
        assert wf_result["data"]["workspace_root"] == workspace

        ef_result = await edit_file(
            path="consistency.txt",
            edits=[{"search": "hello", "replace": "hi"}],
        )
        assert ef_result["success"] is True, f"edit_file 失败: {ef_result.get('error')}"
        assert ef_result["data"]["workspace_root"] == workspace

        df_result = await delete_file(path="shared/consistency.txt")
        assert df_result["data"]["workspace_root"] == workspace
