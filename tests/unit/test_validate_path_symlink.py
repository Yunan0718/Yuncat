"""tests/unit/test_validate_path_symlink.py —— Task 3.6: validate_path symlink 攻击防护

对标:
- OWASP Path Traversal: symlink 变种（ln -s /etc/passwd workspace/evil）
- 主流做法（Cursor/VS Code/Claude Code）: resolve() 跟随 symlink + relative_to 边界检查
- 不用 strict=True（会破坏 write_file 新文件创建——strict 要求路径存在）

分析:
- 当前 validate_path 用 Path.resolve()（默认 strict=False），自动跟随 symlink
- (base / "evil").resolve() 若 evil 是 symlink 指向 /etc/passwd → 解析为 /etc/passwd
- relative_to(workspace) 失败 → PermissionError ✅
- 即：当前实现已正确处理 symlink，本测试固化行为防回归

覆盖:
- symlink 指向 workspace 外（mock resolve 模拟）→ 拒绝
- symlink 指向 workspace 内（mock resolve 模拟）→ 允许
- symlink 链中途跳出（中间组件 mock）→ 拒绝
- 真实 symlink（Linux/开发者模式 Windows，skipif 无权限）→ 验证真实行为
"""
import os
import sys
import pathlib

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


def _can_create_symlink() -> bool:
    """检测当前环境是否可创建 symlink（Windows 需开发者模式或管理员）。

    注意：某些沙箱/文件系统会对 os.symlink 静默降级为普通文件复制
    （islink=False），必须用 os.path.islink 复核，否则探针被假阳性欺骗。
    """
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src")
            dst = os.path.join(td, "dst")
            pathlib.Path(src).write_text("x")
            os.symlink(src, dst)
            return os.path.islink(dst)
    except (OSError, NotImplementedError):
        return False


_CAN_SYMLINK = _can_create_symlink()


class TestValidatePathSymlinkViaMock:
    """用 mock 模拟 symlink 场景——跨平台验证 validate_path 的判断逻辑。

    核心逻辑: validate_path 调用 (base / path).resolve()，若 resolve 结果在 workspace 外
    则抛 PermissionError。mock Path.resolve 模拟 symlink 指向不同目标。
    """

    @pytest.fixture
    def ws(self, tmp_path, monkeypatch):
        """初始化隔离 workspace。"""
        monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path / "ws"))
        from security.sandbox import get_workspace
        return get_workspace()

    def test_symlink_to_outside_rejected(self, ws, monkeypatch):
        """symlink 指向 workspace 外（如 /etc/passwd）→ 拒绝"""
        from security.sandbox import validate_path
        # 创建占位文件（模拟 evil 是 symlink 的载体）
        evil = ws / "shared" / "evil"
        evil.parent.mkdir(parents=True, exist_ok=True)
        evil.write_text("placeholder")

        original_resolve = pathlib.Path.resolve

        def mock_resolve(self, *args, **kwargs):
            result = original_resolve(self, *args, **kwargs)
            # 模拟 evil 是 symlink 指向 workspace 外的 /etc/passwd
            if self.name == "evil" and self.parent.name == "shared":
                return pathlib.Path("/etc/passwd")
            return result

        monkeypatch.setattr(pathlib.Path, "resolve", mock_resolve)
        with pytest.raises(PermissionError, match="超出工作区"):
            validate_path("shared/evil")

    def test_symlink_to_inside_allowed(self, ws, monkeypatch):
        """symlink 指向 workspace 内（如 shared/link -> shared/legit）→ 允许"""
        from security.sandbox import validate_path
        # 创建合法目标文件
        legit = ws / "shared" / "legit.txt"
        legit.parent.mkdir(parents=True, exist_ok=True)
        legit.write_text("content")
        # 创建占位（模拟 link 是 symlink）
        link = ws / "shared" / "link"
        link.write_text("placeholder")

        original_resolve = pathlib.Path.resolve

        def mock_resolve(self, *args, **kwargs):
            result = original_resolve(self, *args, **kwargs)
            # 模拟 link 是 symlink 指向 workspace 内的 legit.txt
            if self.name == "link" and self.parent.name == "shared":
                return legit.resolve()
            return result

        monkeypatch.setattr(pathlib.Path, "resolve", mock_resolve)
        result = validate_path("shared/link")
        assert result == legit.resolve()

    def test_normal_path_no_symlink_unaffected(self, ws, monkeypatch):
        """正常路径（无 symlink）不受影响——mock 不介入"""
        from security.sandbox import validate_path
        # 不 mock，验证正常行为
        result = validate_path("shared/normal.txt")
        assert result == (ws / "shared" / "normal.txt").resolve()


@pytest.mark.skipif(not _CAN_SYMLINK, reason="环境不支持创建 symlink（Windows 需开发者模式/管理员）")
class TestValidatePathRealSymlink:
    """真实 symlink 测试——在 Linux CI 或开发者模式 Windows 上验证真实行为。

    这组测试固化"resolve() 跟随 symlink + relative_to 拒绝 workspace 外目标"的真实行为。
    """

    @pytest.fixture
    def ws(self, tmp_path, monkeypatch):
        monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path / "ws"))
        # 实际验证当前环境可创建 symlink（探针可能在某些 Windows 环境下通过但实际失败）
        probe_dir = tmp_path / "ws" / "shared"
        probe_dir.mkdir(parents=True, exist_ok=True)
        probe_src = probe_dir / "_probe_src"
        probe_src.write_text("probe")
        probe_link = probe_dir / "_probe_link"
        try:
            os.symlink(str(probe_src), str(probe_link))
            real_link = os.path.islink(str(probe_link))
            probe_link.unlink(missing_ok=True)
            probe_src.unlink(missing_ok=True)
        except OSError:
            real_link = False
        if not real_link:
            pytest.skip("环境实际不支持创建 symlink（os.symlink 静默降级为普通文件复制）")
        from security.sandbox import get_workspace
        return get_workspace()

    def test_real_symlink_to_outside_rejected(self, ws):
        """真实 symlink 指向 workspace 外 → validate_path 拒绝"""
        from security.sandbox import validate_path
        evil = ws / "shared" / "evil"
        evil.parent.mkdir(parents=True, exist_ok=True)
        # 创建指向 workspace 外的 symlink
        os.symlink(os.path.join(os.path.expanduser("~"), "external_target"), str(evil))
        with pytest.raises(PermissionError, match="超出工作区"):
            validate_path("shared/evil")

    def test_real_symlink_to_inside_allowed(self, ws):
        """真实 symlink 指向 workspace 内 → validate_path 允许"""
        from security.sandbox import validate_path
        legit = ws / "shared" / "legit.txt"
        legit.parent.mkdir(parents=True, exist_ok=True)
        legit.write_text("content")
        link = ws / "shared" / "link"
        os.symlink(str(legit), str(link))
        result = validate_path("shared/link")
        # resolve() 跟随 symlink 到 legit.txt
        assert result == legit.resolve()

    def test_real_symlink_chain_to_outside_rejected(self, ws, tmp_path):
        """symlink 链：link1 -> link2 -> /outside → 最终 resolve 到 workspace 外，拒绝"""
        from security.sandbox import validate_path
        outside = tmp_path / "outside_target"
        outside.write_text("secret")
        link2 = ws / "shared" / "link2"
        link2.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(outside), str(link2))
        link1 = ws / "shared" / "link1"
        os.symlink(str(link2), str(link1))
        with pytest.raises(PermissionError, match="超出工作区"):
            validate_path("shared/link1")

    def test_real_symlink_in_middle_component_rejected(self, ws, tmp_path):
        """symlink 在中间组件：a 是 symlink 指向 workspace 外，a/b/c → resolve 跟随 a 到外部，拒绝"""
        from security.sandbox import validate_path
        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir()
        (outside_dir / "b" / "c").mkdir(parents=True)
        a_link = ws / "a"
        # a 是 symlink 指向 workspace 外的 outside_dir
        os.symlink(str(outside_dir), str(a_link))
        with pytest.raises(PermissionError, match="超出工作区"):
            validate_path("a/b/c")
