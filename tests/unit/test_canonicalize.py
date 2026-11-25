"""tests/unit/test_canonicalize.py —— canonicalize 纯函数测试矩阵

验收（docs/path-canonicalize-design.md §4/§5）：
  - 14 条边界语义矩阵（../ 穿越 / symlink 逃逸 / 绝对路径 / boundary 收窄 /
    空路径 / must_exist / Windows 大小写）
  - 变异锚点：startswith 前缀伪造 / boundary 默认收窄 / root⊄boundary
  - validate_path 薄壳：base 是锚点、boundary=workspace（反回归锚点）
"""
import os
import pathlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from security.sandbox import canonicalize, validate_path  # noqa: E402


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    """构造 workspace 根：ws/{shared,agents/a,agents/b,shared/secret.txt}"""
    root = tmp_path / "ws"
    (root / "shared").mkdir(parents=True)
    (root / "agents" / "agent-a").mkdir(parents=True)
    (root / "agents" / "agent-b").mkdir(parents=True)
    (root / "agents" / "agent-a" / "secret.txt").write_text("top-secret", encoding="utf-8")
    return root


# ═══════════════════════════════════════════════════════════
# §4 测试矩阵
# ═══════════════════════════════════════════════════════════

def test_normal_relative_path(ws):
    """用例 1：正常相对路径 → root 下绝对路径"""
    result = canonicalize("foo/bar.txt", root=ws, boundary=ws)
    assert result == (ws / "foo" / "bar.txt").resolve()
    assert result.is_absolute()


def test_dotdot_escape_rejected(ws):
    """用例 2：../ 逃逸出 boundary → PermissionError"""
    with pytest.raises(PermissionError, match="超出工作区"):
        canonicalize("../../outside.txt", root=ws / "shared", boundary=ws)


def test_absolute_inside_allowed(ws):
    """用例 3：绝对路径在 boundary 内 → 放行"""
    target = ws / "shared" / "secret.txt"
    result = canonicalize(str(target), root=ws, boundary=ws)
    assert result == target.resolve()


def test_absolute_outside_rejected(ws):
    """用例 4：绝对路径在 boundary 外 → PermissionError"""
    outside = ws.parent / "outside.txt"
    outside.write_text("x")
    with pytest.raises(PermissionError, match="超出工作区"):
        canonicalize(str(outside), root=ws, boundary=ws)


def test_symlink_escape_rejected(ws, monkeypatch):
    """用例 5：symlink 指向 boundary 外 → PermissionError（mock resolve，跨平台）"""
    evil = ws / "shared" / "evil"
    evil.write_text("placeholder")

    original_resolve = pathlib.Path.resolve

    def mock_resolve(self, *args, **kwargs):
        result = original_resolve(self, *args, **kwargs)
        if self.name == "evil" and self.parent.name == "shared":
            return ws.parent / "passwd"  # 模拟 symlink 指向 boundary 外
        return result

    monkeypatch.setattr(pathlib.Path, "resolve", mock_resolve)
    with pytest.raises(PermissionError, match="超出工作区"):
        canonicalize("shared/evil", root=ws, boundary=ws)


def test_symlink_inside_allowed(ws, monkeypatch):
    """用例 6：symlink 指向 boundary 内 → 放行（mock resolve）"""
    legit = ws / "shared" / "legit.txt"
    legit.write_text("content")
    link = ws / "shared" / "link"
    link.write_text("placeholder")

    original_resolve = pathlib.Path.resolve

    def mock_resolve(self, *args, **kwargs):
        result = original_resolve(self, *args, **kwargs)
        if self.name == "link" and self.parent.name == "shared":
            return legit.resolve()
        return result

    monkeypatch.setattr(pathlib.Path, "resolve", mock_resolve)
    assert canonicalize("shared/link", root=ws, boundary=ws) == legit.resolve()


def test_empty_and_dot_mean_boundary(ws):
    """用例 7：空字符串 / '.' → boundary 本身"""
    assert canonicalize("", root=ws, boundary=ws) == ws.resolve()
    assert canonicalize(".", root=ws, boundary=ws) == ws.resolve()


def test_boundary_narrowed_rejects_cross(ws):
    """用例 8：boundary 收窄到 shared，../agents 越出 → PermissionError"""
    with pytest.raises(PermissionError, match="超出工作区"):
        canonicalize("../agents/agent-a/secret.txt", root=ws / "shared", boundary=ws / "shared")


def test_boundary_workspace_allows_cross_from_shared(ws):
    """用例 9（反回归锚点）：root=shared 但 boundary=workspace → 放行。

    锁定「base 是锚点、boundary 才是边界」语义——validate_path 的 base 参数
    传 shared 不代表 shared 是边界（与 Cursor 一致：用户文件夹 = workspace）。
    """
    result = canonicalize("../agents/agent-a/secret.txt", root=ws / "shared", boundary=ws)
    assert result == (ws / "agents" / "agent-a" / "secret.txt").resolve()


def test_root_outside_boundary_raises_valueerror(ws):
    """用例 10：root ⊄ boundary → ValueError（配置错误快速失败）"""
    with pytest.raises(ValueError, match="root 不在 boundary 内"):
        canonicalize("secret.txt", root=ws / "shared", boundary=ws / "agents" / "agent-a")


def test_nonexistent_path_allowed(ws):
    """用例 11：不存在的路径 → 放行（strict=False，写入场景需要）"""
    result = canonicalize("brand-new/file.txt", root=ws, boundary=ws)
    assert result == (ws / "brand-new" / "file.txt").resolve()
    assert not result.exists()


def test_must_exist_raises(ws):
    """用例 12：must_exist=True 且路径不存在 → FileNotFoundError"""
    with pytest.raises(FileNotFoundError):
        canonicalize("brand-new/file.txt", root=ws, boundary=ws, must_exist=True)


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 验证大小写不敏感")
def test_windows_case_insensitive(ws):
    """用例 13：Windows 大小写不敏感 → 放行"""
    (ws / "shared" / "Foo.txt").write_text("x")
    result = canonicalize("shared/foo.txt", root=ws, boundary=ws)
    assert result.exists()  # WindowsPath 比较不敏感，resolve 落回真实大小写


def test_boundary_defaults_to_root(ws):
    """boundary 默认 = root：不传 boundary 时不允许离开 root"""
    with pytest.raises(PermissionError):
        canonicalize("../agents/agent-a/secret.txt", root=ws / "shared")
    assert canonicalize("secret.txt", root=ws / "shared") == (ws / "shared" / "secret.txt").resolve()


def test_relative_root_resolved_first(ws, tmp_path):
    """相对 root 先 resolve 再判边界"""
    cwd = Path.cwd()
    try:
        os.chdir(ws)
        result = canonicalize("agents/agent-a/secret.txt", root=Path("."), boundary=ws)
        assert result == (ws / "agents" / "agent-a" / "secret.txt").resolve()
    finally:
        os.chdir(cwd)


# ═══════════════════════════════════════════════════════════
# §5 变异锚点
# ═══════════════════════════════════════════════════════════

def test_mutation_anchor_startswith_prefix_foiling(ws):
    """变异锚点：boundary 名作为前缀的兄弟目录不被误放行。

    若实现退化成 `str.startswith(str(boundary))`，`ws_evil` 会因前缀匹配
    被放行——is_relative_to 必须拒绝。
    """
    evil_sibling = ws.parent / "ws_evil"  # 名字以 boundary 名作前缀
    evil_sibling.mkdir(exist_ok=True)
    (evil_sibling / "pwn.txt").write_text("pwned")
    with pytest.raises(PermissionError, match="超出工作区"):
        canonicalize("../../../ws_evil/pwn.txt", root=ws / "agents" / "agent-a", boundary=ws)


def test_mutation_anchor_boundary_collapse(ws):
    """变异锚点：boundary 被错误收成 root 时用例 9 必须失败。

    实现把 boundary 默认收窄（或忽略显式 boundary）会导致本用例抛
    PermissionError——锚定「显式 boundary 优先」语义。
    """
    result = canonicalize("../agents/agent-a/secret.txt", root=ws / "shared", boundary=ws)
    assert result == (ws / "agents" / "agent-a" / "secret.txt").resolve()


# ═══════════════════════════════════════════════════════════
# validate_path 薄壳（行为与 v39 一致）
# ═══════════════════════════════════════════════════════════

def _env_ws(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path / "ws"))
    ws = tmp_path / "ws"
    (ws / "shared").mkdir(parents=True, exist_ok=True)
    (ws / "agents" / "agent-a").mkdir(parents=True, exist_ok=True)
    (ws / "agents" / "agent-a" / "secret.txt").write_text("s", encoding="utf-8")
    return ws


def test_validate_path_allows_inside(tmp_path, monkeypatch):
    _env_ws(tmp_path, monkeypatch)
    result = validate_path("agents/agent-a/secret.txt")
    assert result == (tmp_path / "ws" / "agents" / "agent-a" / "secret.txt").resolve()


def test_validate_path_rejects_traversal(tmp_path, monkeypatch):
    _env_ws(tmp_path, monkeypatch)
    with pytest.raises(PermissionError, match="超出工作区"):
        validate_path("../../etc/passwd")


def test_validate_path_base_is_anchor_not_boundary(tmp_path, monkeypatch):
    """薄壳锚点：base=shared 仍可访问 workspace 内他处（read_file 现状语义）"""
    root = _env_ws(tmp_path, monkeypatch)
    result = validate_path("../agents/agent-a/secret.txt", base=root / "shared")
    assert result == (root / "agents" / "agent-a" / "secret.txt").resolve()


def test_validate_path_rejects_absolute_outside(tmp_path, monkeypatch):
    _env_ws(tmp_path, monkeypatch)
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    with pytest.raises(PermissionError, match="超出工作区"):
        validate_path(str(outside))
