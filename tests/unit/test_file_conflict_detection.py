"""tests/unit/test_file_conflict_detection.py —— Task 6.4: 文件外部修改冲突检测

对标 Claude Code content hash / mtime 检查——乐观并发控制（Optimistic Concurrency Control）。

覆盖:
- compute_content_hash: SHA-256 计算正确性（已知内容→已知哈希 / 空串 / 不同内容不同哈希）
- read_file: 返回 content_hash + mtime（供 agent 传给 edit_file/write_file）
- edit_file 预览: expected_hash 匹配→成功 / 不匹配→拒绝 / 未传→向后兼容 /
                  expected_mtime 匹配/不匹配 / 返回 content_hash+mtime / 冲突时不写入
- write_file 预览: expected_hash 匹配/不匹配/未传 / 新文件跳过校验 / 返回 content_hash+mtime
- _handle_edit 执行: expected_hash 匹配→写入 / 不匹配→拒绝不写入 / 未传→兼容 /
                     expected_mtime 匹配/不匹配
- _handle_write 执行: expected_hash 匹配→写入 / 不匹配→拒绝不写入 / 未传→兼容 /
                      新文件跳过校验
"""
import hashlib
import os
import sys
import time

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


def _sha256(content: str) -> str:
    """测试辅助：计算 SHA-256 哈希（与实现中的 compute_content_hash 一致）。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _mtime(ws, rel: str) -> float:
    """测试辅助：获取文件 mtime。"""
    return os.path.getmtime(str(ws / "shared" / rel))


# ============================================================
# compute_content_hash 辅助函数
# ============================================================

class TestComputeContentHash:
    """Task 6.4: compute_content_hash——SHA-256 哈希计算"""

    def test_known_content_known_hash(self):
        """已知内容 → 已知 SHA-256 哈希（可复现）"""
        from tools.builtin.system_tool_file_ops import compute_content_hash
        # sha256("hello world") 的标准值
        assert compute_content_hash("hello world") == (
            "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
        )

    def test_empty_string(self):
        """空字符串 → SHA-256 空哈希（已知值）"""
        from tools.builtin.system_tool_file_ops import compute_content_hash
        assert compute_content_hash("") == (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )

    def test_different_content_different_hash(self):
        """不同内容 → 不同哈希"""
        from tools.builtin.system_tool_file_ops import compute_content_hash
        h1 = compute_content_hash("content A")
        h2 = compute_content_hash("content B")
        assert h1 != h2

    def test_same_content_same_hash(self):
        """相同内容 → 相同哈希（确定性）"""
        from tools.builtin.system_tool_file_ops import compute_content_hash
        assert compute_content_hash("same") == compute_content_hash("same")

    def test_unicode_content(self):
        """Unicode 内容（中文/emoji）正确哈希"""
        from tools.builtin.system_tool_file_ops import compute_content_hash
        # 不抛异常，返回 64 字符 hex
        h = compute_content_hash("你好 🌍")
        assert len(h) == 64
        assert h == hashlib.sha256("你好 🌍".encode("utf-8")).hexdigest()


# ============================================================
# read_file 返回 content_hash + mtime
# ============================================================

class TestReadFileReturnsHash:
    """Task 6.4: read_file 返回 content_hash + mtime——供 agent 传给 edit_file/write_file"""

    async def test_returns_content_hash(self, workspace):
        """read_file 结果含 content_hash（SHA-256）"""
        _write_shared_file(workspace, "foo.txt", "hello world")
        from tools.builtin.system_tool_file_ops import read_file
        result = await read_file("foo.txt")
        assert result["success"] is True
        assert result["data"]["content_hash"] == _sha256("hello world")

    async def test_returns_mtime(self, workspace):
        """read_file 结果含 mtime（float, epoch seconds）"""
        _write_shared_file(workspace, "foo.txt", "hello world")
        from tools.builtin.system_tool_file_ops import read_file
        result = await read_file("foo.txt")
        assert "mtime" in result["data"]
        assert isinstance(result["data"]["mtime"], float)
        assert result["data"]["mtime"] > 0

    async def test_hash_matches_actual_content(self, workspace):
        """读取后修改文件 → hash 变化（验证 hash 反映当前内容）"""
        _write_shared_file(workspace, "foo.txt", "original")
        from tools.builtin.system_tool_file_ops import read_file
        r1 = await read_file("foo.txt")
        # 修改文件
        _write_shared_file(workspace, "foo.txt", "modified")
        r2 = await read_file("foo.txt")
        assert r1["data"]["content_hash"] != r2["data"]["content_hash"]


# ============================================================
# edit_file 预览阶段——expected_hash / expected_mtime 校验
# ============================================================

class TestEditFilePreviewConflict:
    """Task 6.4: edit_file 预览阶段——外部修改冲突检测"""

    async def test_expected_hash_match_succeeds(self, workspace):
        """expected_hash 匹配当前文件 → 预览成功"""
        _write_shared_file(workspace, "foo.txt", "hello world")
        from tools.builtin.system_tool_advanced import edit_file
        result = await edit_file(
            "foo.txt",
            [{"search": "hello", "replace": "goodbye"}],
            expected_hash=_sha256("hello world"),
        )
        assert result["success"] is True
        assert result["meta"]["phase"] == "preview"

    async def test_expected_hash_mismatch_rejects(self, workspace):
        """expected_hash 不匹配（文件已被外部修改）→ 拒绝编辑"""
        _write_shared_file(workspace, "foo.txt", "hello world")
        from tools.builtin.system_tool_advanced import edit_file
        result = await edit_file(
            "foo.txt",
            [{"search": "hello", "replace": "goodbye"}],
            expected_hash=_sha256("DIFFERENT CONTENT"),
        )
        assert result["success"] is False
        assert "外部修改" in result["error"] or "modified" in result["error"].lower()
        assert result["meta"].get("conflict") == "hash_mismatch"

    async def test_no_expected_hash_backward_compat(self, workspace):
        """未传 expected_hash → 向后兼容，不校验（现有行为不变）"""
        _write_shared_file(workspace, "foo.txt", "hello world")
        from tools.builtin.system_tool_advanced import edit_file
        result = await edit_file(
            "foo.txt",
            [{"search": "hello", "replace": "goodbye"}],
        )
        assert result["success"] is True
        assert "conflict" not in result.get("meta", {})

    async def test_expected_mtime_match_succeeds(self, workspace):
        """expected_mtime 匹配 → 预览成功"""
        _write_shared_file(workspace, "foo.txt", "hello world")
        mtime = _mtime(workspace, "foo.txt")
        from tools.builtin.system_tool_advanced import edit_file
        result = await edit_file(
            "foo.txt",
            [{"search": "hello", "replace": "goodbye"}],
            expected_mtime=mtime,
        )
        assert result["success"] is True

    async def test_expected_mtime_mismatch_rejects(self, workspace):
        """expected_mtime 不匹配（文件被外部 touch/修改）→ 拒绝"""
        _write_shared_file(workspace, "foo.txt", "hello world")
        from tools.builtin.system_tool_advanced import edit_file
        # 传一个明显错误的 mtime（1小时前）
        result = await edit_file(
            "foo.txt",
            [{"search": "hello", "replace": "goodbye"}],
            expected_mtime=time.time() - 3600,
        )
        assert result["success"] is False
        assert result["meta"].get("conflict") == "mtime_mismatch"

    async def test_hash_takes_precedence_over_mtime(self, workspace):
        """同时传 expected_hash（匹配）+ expected_mtime（不匹配）→ hash 优先，成功"""
        _write_shared_file(workspace, "foo.txt", "hello world")
        from tools.builtin.system_tool_advanced import edit_file
        result = await edit_file(
            "foo.txt",
            [{"search": "hello", "replace": "goodbye"}],
            expected_hash=_sha256("hello world"),
            expected_mtime=time.time() - 3600,  # mtime 不匹配但 hash 匹配
        )
        assert result["success"] is True  # hash 优先

    async def test_preview_returns_content_hash_and_mtime(self, workspace):
        """预览结果含 content_hash + mtime（供 execute 阶段或下次调用使用）"""
        _write_shared_file(workspace, "foo.txt", "hello world")
        from tools.builtin.system_tool_advanced import edit_file
        result = await edit_file(
            "foo.txt",
            [{"search": "hello", "replace": "goodbye"}],
        )
        assert result["data"]["content_hash"] == _sha256("hello world")
        assert "mtime" in result["data"]

    async def test_hash_mismatch_does_not_modify_file(self, workspace):
        """hash 不匹配时拒绝编辑 → 文件内容不变（不写入）"""
        _write_shared_file(workspace, "foo.txt", "hello world")
        from tools.builtin.system_tool_advanced import edit_file
        await edit_file(
            "foo.txt",
            [{"search": "hello", "replace": "goodbye"}],
            expected_hash=_sha256("WRONG"),
        )
        # 文件未被修改
        assert (workspace / "shared" / "foo.txt").read_text() == "hello world"


# ============================================================
# write_file 预览阶段——expected_hash / expected_mtime 校验
# ============================================================

class TestWriteFilePreviewConflict:
    """Task 6.4: write_file 预览阶段——外部修改冲突检测"""

    async def test_expected_hash_match_succeeds(self, workspace):
        """expected_hash 匹配现有文件 → 预览成功"""
        _write_shared_file(workspace, "foo.txt", "old content")
        from tools.builtin.system_tool_file_ops import write_file
        result = await write_file(
            "foo.txt", "new content",
            expected_hash=_sha256("old content"),
        )
        assert result["success"] is True
        assert result["meta"]["phase"] == "preview"

    async def test_expected_hash_mismatch_rejects(self, workspace):
        """expected_hash 不匹配 → 拒绝（文件已被外部修改）"""
        _write_shared_file(workspace, "foo.txt", "old content")
        from tools.builtin.system_tool_file_ops import write_file
        result = await write_file(
            "foo.txt", "new content",
            expected_hash=_sha256("DIFFERENT"),
        )
        assert result["success"] is False
        assert result["meta"].get("conflict") == "hash_mismatch"

    async def test_no_expected_hash_backward_compat(self, workspace):
        """未传 expected_hash → 向后兼容"""
        _write_shared_file(workspace, "foo.txt", "old content")
        from tools.builtin.system_tool_file_ops import write_file
        result = await write_file("foo.txt", "new content")
        assert result["success"] is True

    async def test_new_file_skips_hash_validation(self, workspace):
        """新文件（不存在）+ expected_hash → 跳过校验（新文件无冲突可能）"""
        from tools.builtin.system_tool_file_ops import write_file
        result = await write_file(
            "newfile.txt", "content",
            expected_hash=_sha256("anything"),  # 文件不存在，应跳过
        )
        assert result["success"] is True
        assert result["data"]["is_new"] is True

    async def test_expected_mtime_mismatch_rejects(self, workspace):
        """expected_mtime 不匹配 → 拒绝"""
        _write_shared_file(workspace, "foo.txt", "old content")
        from tools.builtin.system_tool_file_ops import write_file
        result = await write_file(
            "foo.txt", "new content",
            expected_mtime=time.time() - 3600,
        )
        assert result["success"] is False
        assert result["meta"].get("conflict") == "mtime_mismatch"

    async def test_preview_returns_content_hash_and_mtime(self, workspace):
        """预览结果含 content_hash + mtime"""
        _write_shared_file(workspace, "foo.txt", "old content")
        from tools.builtin.system_tool_file_ops import write_file
        result = await write_file("foo.txt", "new content")
        assert result["data"]["content_hash"] == _sha256("old content")
        assert "mtime" in result["data"]


# ============================================================
# _handle_edit 执行阶段——expected_hash / expected_mtime 校验
# ============================================================

class TestHandleEditExecuteConflict:
    """Task 6.4: _handle_edit 执行阶段——检测 preview→confirm 间隙的外部修改"""

    async def test_expected_hash_match_writes_file(self, workspace):
        """expected_hash 匹配 → 正确写入文件"""
        _write_shared_file(workspace, "foo.txt", "hello world")
        from tools.builtin.system_tool_confirm import _handle_edit
        payload = {
            "path": str(workspace / "shared" / "foo.txt"),
            "edits": [{"search": "hello", "replace": "goodbye"}],
            "expected_hash": _sha256("hello world"),
        }
        result = await _handle_edit(None, payload)
        assert result["success"] is True
        assert result["meta"]["phase"] == "executed"
        assert (workspace / "shared" / "foo.txt").read_text() == "goodbye world"

    async def test_expected_hash_mismatch_rejects_without_writing(self, workspace):
        """expected_hash 不匹配（文件在 confirm 间隙被外部修改）→ 拒绝，不写入"""
        _write_shared_file(workspace, "foo.txt", "hello world")
        # 模拟 confirm 间隙外部修改
        _write_shared_file(workspace, "foo.txt", "EXTERNAL MODIFICATION")
        from tools.builtin.system_tool_confirm import _handle_edit
        payload = {
            "path": str(workspace / "shared" / "foo.txt"),
            "edits": [{"search": "hello", "replace": "goodbye"}],
            "expected_hash": _sha256("hello world"),  # agent 预期的是原始内容
        }
        result = await _handle_edit(None, payload)
        assert result["success"] is False
        assert result["meta"].get("conflict") == "hash_mismatch"
        # 文件保持外部修改后的内容（未被覆盖）
        assert (workspace / "shared" / "foo.txt").read_text() == "EXTERNAL MODIFICATION"

    async def test_no_expected_hash_backward_compat(self, workspace):
        """未传 expected_hash → 向后兼容（现有行为不变）"""
        _write_shared_file(workspace, "foo.txt", "hello world")
        from tools.builtin.system_tool_confirm import _handle_edit
        payload = {
            "path": str(workspace / "shared" / "foo.txt"),
            "edits": [{"search": "hello", "replace": "goodbye"}],
        }
        result = await _handle_edit(None, payload)
        assert result["success"] is True
        assert "conflict" not in result.get("meta", {})

    async def test_expected_mtime_match_writes_file(self, workspace):
        """expected_mtime 匹配 → 写入成功"""
        _write_shared_file(workspace, "foo.txt", "hello world")
        mtime = _mtime(workspace, "foo.txt")
        from tools.builtin.system_tool_confirm import _handle_edit
        payload = {
            "path": str(workspace / "shared" / "foo.txt"),
            "edits": [{"search": "hello", "replace": "goodbye"}],
            "expected_mtime": mtime,
        }
        result = await _handle_edit(None, payload)
        assert result["success"] is True

    async def test_expected_mtime_mismatch_rejects(self, workspace):
        """expected_mtime 不匹配 → 拒绝"""
        _write_shared_file(workspace, "foo.txt", "hello world")
        from tools.builtin.system_tool_confirm import _handle_edit
        payload = {
            "path": str(workspace / "shared" / "foo.txt"),
            "edits": [{"search": "hello", "replace": "goodbye"}],
            "expected_mtime": time.time() - 3600,
        }
        result = await _handle_edit(None, payload)
        assert result["success"] is False
        assert result["meta"].get("conflict") == "mtime_mismatch"

    async def test_hash_takes_precedence_over_mtime(self, workspace):
        """同时传 hash（匹配）+ mtime（不匹配）→ hash 优先，成功"""
        _write_shared_file(workspace, "foo.txt", "hello world")
        from tools.builtin.system_tool_confirm import _handle_edit
        payload = {
            "path": str(workspace / "shared" / "foo.txt"),
            "edits": [{"search": "hello", "replace": "goodbye"}],
            "expected_hash": _sha256("hello world"),
            "expected_mtime": time.time() - 3600,
        }
        result = await _handle_edit(None, payload)
        assert result["success"] is True


# ============================================================
# _handle_write 执行阶段——expected_hash / expected_mtime 校验
# ============================================================

class TestHandleWriteExecuteConflict:
    """Task 6.4: _handle_write 执行阶段——检测 preview→confirm 间隙的外部修改"""

    async def test_expected_hash_match_writes_file(self, workspace):
        """expected_hash 匹配 → 正确写入"""
        _write_shared_file(workspace, "foo.txt", "old content")
        from tools.builtin.system_tool_confirm import _handle_write
        payload = {
            "path": str(workspace / "shared" / "foo.txt"),
            "content": "new content",
            "mode": "overwrite",
            "expected_hash": _sha256("old content"),
        }
        result = await _handle_write(None, payload)
        assert result["success"] is True
        assert (workspace / "shared" / "foo.txt").read_text() == "new content"

    async def test_expected_hash_mismatch_rejects_without_writing(self, workspace):
        """expected_hash 不匹配（confirm 间隙外部修改）→ 拒绝，不覆盖外部修改"""
        _write_shared_file(workspace, "foo.txt", "old content")
        # 模拟 confirm 间隙外部修改
        _write_shared_file(workspace, "foo.txt", "USER EDITED THIS")
        from tools.builtin.system_tool_confirm import _handle_write
        payload = {
            "path": str(workspace / "shared" / "foo.txt"),
            "content": "new content",
            "mode": "overwrite",
            "expected_hash": _sha256("old content"),
        }
        result = await _handle_write(None, payload)
        assert result["success"] is False
        assert result["meta"].get("conflict") == "hash_mismatch"
        # 文件保持用户的外部修改（未被静默覆盖）
        assert (workspace / "shared" / "foo.txt").read_text() == "USER EDITED THIS"

    async def test_no_expected_hash_backward_compat(self, workspace):
        """未传 expected_hash → 向后兼容"""
        _write_shared_file(workspace, "foo.txt", "old content")
        from tools.builtin.system_tool_confirm import _handle_write
        payload = {
            "path": str(workspace / "shared" / "foo.txt"),
            "content": "new content",
            "mode": "overwrite",
        }
        result = await _handle_write(None, payload)
        assert result["success"] is True
        assert "conflict" not in result.get("meta", {})

    async def test_new_file_skips_hash_validation(self, workspace):
        """新文件（不存在）+ expected_hash → 跳过校验，直接写入"""
        from tools.builtin.system_tool_confirm import _handle_write
        payload = {
            "path": str(workspace / "shared" / "brand_new.txt"),
            "content": "content",
            "mode": "overwrite",
            "expected_hash": _sha256("anything"),
        }
        result = await _handle_write(None, payload)
        assert result["success"] is True
        assert (workspace / "shared" / "brand_new.txt").read_text() == "content"

    async def test_expected_mtime_mismatch_rejects(self, workspace):
        """expected_mtime 不匹配 → 拒绝"""
        _write_shared_file(workspace, "foo.txt", "old content")
        from tools.builtin.system_tool_confirm import _handle_write
        payload = {
            "path": str(workspace / "shared" / "foo.txt"),
            "content": "new content",
            "mode": "overwrite",
            "expected_mtime": time.time() - 3600,
        }
        result = await _handle_write(None, payload)
        assert result["success"] is False
        assert result["meta"].get("conflict") == "mtime_mismatch"

    async def test_append_mode_validates_existing_content(self, workspace):
        """append 模式也校验现有内容 hash（防止追加到被修改的文件）"""
        _write_shared_file(workspace, "foo.txt", "original")
        # 模拟外部修改
        _write_shared_file(workspace, "foo.txt", "MODIFIED")
        from tools.builtin.system_tool_confirm import _handle_write
        payload = {
            "path": str(workspace / "shared" / "foo.txt"),
            "content": " appended",
            "mode": "append",
            "expected_hash": _sha256("original"),  # agent 预期原始内容
        }
        result = await _handle_write(None, payload)
        assert result["success"] is False
        assert result["meta"].get("conflict") == "hash_mismatch"
