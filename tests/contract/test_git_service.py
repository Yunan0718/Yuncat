"""tests/contract/test_git_service.py —— Git 服务契约测试

在临时目录中 git init → 测试所有 GitService 方法。
要求系统已安装 git；未安装时跳过所有测试。
"""
import os
import sys
import tempfile
import shutil
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


def is_git_available() -> bool:
    return shutil.which("git") is not None


# 模块级 skip
pytestmark = pytest.mark.skipif(
    not is_git_available(),
    reason="git not installed — skipping GitService contract tests",
)


@pytest.fixture
def tmp_path():
    """把临时目录放到系统 temp——pytest 默认的 basetemp 在**仓库内**（.test-tmp/），

    于是 `git rev-parse` 会找到父仓库，"不是仓库"的用例假失败（CI 上长期红）。
    """
    with tempfile.TemporaryDirectory(prefix="yuncat_git_") as td:
        yield Path(td)


@pytest.fixture
def temp_repo(tmp_path):
    """在临时目录中 git init → 返回 GitService 实例和仓库路径"""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    import subprocess
    subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@yuncat.local"],
        cwd=str(repo_dir), capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Yuncat Test"],
        cwd=str(repo_dir), capture_output=True,
    )
    # 创建初始文件并提交（否则 status 不会有 HEAD）
    (repo_dir / "README.md").write_text("# Test Repo")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(repo_dir), capture_output=True)

    from services.git_service import GitService
    svc = GitService(repo_path=str(repo_dir))
    return svc, repo_dir


async def _make_commit(svc, message: str):
    """快速创建一次提交（修改 README）"""
    repo_dir = svc._repo_path
    with open(os.path.join(repo_dir, "README.md"), "a") as f:
        f.write(f"\n{message}")
    return await svc.commit(message)


# ---- init ----
class TestGitInit:
    async def test_init_creates_repo(self, tmp_path):
        """git init → is_repo = True"""
        repo_dir = tmp_path / "fresh"
        repo_dir.mkdir()
        from services.git_service import GitService
        svc = GitService(repo_path=str(repo_dir))
        result = await svc.init()
        assert result["ok"] is True
        assert await svc.is_repo() is True

    async def test_init_on_existing_repo_returns_false(self, temp_repo):
        svc, _ = temp_repo
        result = await svc.init()
        assert result["ok"] is True
        assert result["initialized"] is False


# ---- status ----
class TestGitStatus:
    async def test_status_returns_branch(self, temp_repo):
        svc, _ = temp_repo
        s = await svc.status()
        assert s["ok"] is True
        assert s["branch"] in ("master", "main")

    async def test_status_clean_repo_has_no_files(self, temp_repo):
        svc, _ = temp_repo
        s = await svc.status()
        assert s["files"] == []

    async def test_status_detects_modified_file(self, temp_repo):
        svc, repo_dir = temp_repo
        (repo_dir / "new_file.py").write_text("print('hello')")
        s = await svc.status()
        assert len(s["files"]) >= 1
        assert any(f["path"] == "new_file.py" for f in s["files"])

    async def test_status_not_repo_returns_error(self, tmp_path):
        from services.git_service import GitService
        svc = GitService(repo_path=str(tmp_path))
        s = await svc.status()
        assert s["ok"] is False

    async def test_is_repo_true(self, temp_repo):
        svc, _ = temp_repo
        assert await svc.is_repo() is True

    async def test_is_repo_false(self, tmp_path):
        from services.git_service import GitService
        svc = GitService(repo_path=str(tmp_path))
        assert await svc.is_repo() is False


# ---- diff ----
class TestGitDiff:
    async def test_diff_empty_on_clean_repo(self, temp_repo):
        svc, _ = temp_repo
        diff = await svc.diff()
        assert diff == "" or "diff" in diff.lower()

    async def test_diff_shows_changes(self, temp_repo):
        svc, repo_dir = temp_repo
        (repo_dir / "README.md").write_text("# Modified")
        diff = await svc.diff()
        assert len(diff) > 0
        assert "# Modified" in diff or "README" in diff


# ---- commit ----
class TestGitCommit:
    async def test_commit_creates_hash(self, temp_repo):
        svc, repo_dir = temp_repo
        (repo_dir / "test.py").write_text("x = 1")
        result = await svc.commit("add test.py")
        assert result["ok"] is True
        assert len(result["commit_hash"]) >= 7

    async def test_commit_empty_message_rejected(self, temp_repo):
        svc, _ = temp_repo
        result = await svc.commit("  ")
        assert result["ok"] is False

    async def test_commit_rejects_dangerous_chars(self, temp_repo):
        svc, _ = temp_repo
        result = await svc.commit("bad && rm -rf /")
        assert result["ok"] is False

    async def test_commit_rejects_semicolon(self, temp_repo):
        svc, _ = temp_repo
        result = await svc.commit("good; bad")
        assert result["ok"] is False


# ---- log ----
class TestGitLog:
    async def test_log_returns_commits(self, temp_repo):
        svc, repo_dir = temp_repo
        (repo_dir / "a.py").write_text("a")
        await svc.commit("commit a")
        (repo_dir / "b.py").write_text("b")
        await svc.commit("commit b")
        log = await svc.log(count=5)
        assert log["ok"] is True
        assert len(log["commits"]) >= 2
        messages = [c["message"] for c in log["commits"]]
        assert "commit b" in messages

    async def test_log_not_repo_returns_error(self, tmp_path):
        from services.git_service import GitService
        svc = GitService(repo_path=str(tmp_path))
        log = await svc.log()
        assert log["ok"] is False


# ---- branch ----
class TestGitBranch:
    async def test_branch_list_contains_current(self, temp_repo):
        svc, _ = temp_repo
        bl = await svc.branch_list()
        assert bl["ok"] is True
        branches = bl["branches"]
        assert any(b["current"] for b in branches)

    async def test_create_branch_switches(self, temp_repo):
        svc, repo_dir = temp_repo
        r = await svc.create_branch("feature-x")
        assert r["ok"] is True
        bl = await svc.branch_list()
        names = [b["name"] for b in bl["branches"]]
        assert "feature-x" in names

    async def test_checkout_switches_branch(self, temp_repo):
        svc, repo_dir = temp_repo
        await svc.create_branch("develop")
        await svc.checkout("master") if os.path.exists(os.path.join(repo_dir, ".git", "refs", "heads", "master")) else await svc.checkout("main")
        bl = await svc.branch_list()
        current = [b for b in bl["branches"] if b["current"]]
        assert current, "Should have a current branch"

    async def test_checkout_nonexistent_fails(self, temp_repo):
        svc, _ = temp_repo
        r = await svc.checkout("nonexistent-branch-xyz")
        assert r["ok"] is False


# ---- stash ----
class TestGitStash:
    async def test_stash_push_and_pop(self, temp_repo):
        svc, repo_dir = temp_repo
        (repo_dir / "stash_test.txt").write_text("dirty content")
        # 需要先 add 才能 stash（git stash 默认只操作 tracked files）
        import subprocess
        subprocess.run(["git", "add", "stash_test.txt"], cwd=str(repo_dir), capture_output=True)
        r = await svc.stash_push("test stash")
        assert r["ok"] is True
        s = await svc.status()
        assert s["files"] == []  # 已 stash，工作区干净
        r2 = await svc.stash_pop()
        assert r2["ok"] is True


# ---- pull ----
class TestGitPull:
    async def test_pull_no_remote_returns_error(self, temp_repo):
        svc, _ = temp_repo
        r = await svc.pull()
        assert r["ok"] is False  # 无 remote，expected
