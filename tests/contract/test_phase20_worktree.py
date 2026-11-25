"""tests/contract/test_phase20_worktree.py —— Phase 20 GitWorktreeService 合约测试"""
import os
import sys
import tempfile
import pytest
from pathlib import Path


@pytest.fixture
def temp_git_repo():
    """创建临时 git 仓库用于测试 worktree"""
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        # init git repo
        import subprocess
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@yuncat.dev"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "config", "user.name", "Yuncat Test"], cwd=str(repo), capture_output=True)
        # 创建初始提交（worktree 要求至少有一个 commit）
        (repo / "README.md").write_text("# Test Repo")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=str(repo), capture_output=True)
        yield repo


def test_worktree_info_dataclass():
    """验证 WorktreeInfo dataclass"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.git_worktree_service import WorktreeInfo, WORKTREE_PREFIX

    wt = WorktreeInfo(task_id="abc12345", branch=f"{WORKTREE_PREFIX}/abc12345",
                       path="/tmp/.worktrees/yuncat-abc12345", status="in_use")
    assert wt.status == "in_use"
    assert wt.branch.startswith(WORKTREE_PREFIX)


@pytest.mark.asyncio
async def test_list_worktrees_empty(temp_git_repo):
    """空仓库无 worktree"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    ws_root = str(temp_git_repo.parent)
    os.environ["YUNCAT_WORKSPACE"] = ws_root

    from services.git_worktree_service import GitWorktreeService
    from services.git_service import GitService

    svc = GitWorktreeService(git_service=GitService(repo_path=str(temp_git_repo)))
    wts = await svc.list_worktrees()
    assert wts == []
    # 清理
    await svc.cleanup_stale(max_age_days=0)


@pytest.mark.asyncio
async def test_worktree_create_and_discard(temp_git_repo):
    """创建 worktree → discard 删除：完整生命周期"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    ws_root = str(temp_git_repo.parent)
    (temp_git_repo.parent / "shared").mkdir(exist_ok=True)
    os.environ["YUNCAT_WORKSPACE"] = ws_root

    from services.git_worktree_service import GitWorktreeService
    from services.git_service import GitService

    svc = GitWorktreeService(git_service=GitService(repo_path=str(temp_git_repo)))

    # 清理之前测试可能遗留的 worktree
    stale = await svc.list_worktrees()
    for wt_stale in stale:
        try:
            await svc._run("worktree", "remove", "--force", wt_stale.path)
            await svc._run("branch", "-D", wt_stale.branch)
        except Exception:
            pass

    # 使用 UUID 避免 worktree 名称冲突
    import uuid
    task_id = str(uuid.uuid4())
    wt = await svc.create_worktree(task_id)
    assert wt is not None, f"create_worktree returned None for {task_id}"
    assert wt.status == "in_use"

    # 通过 git worktree list 验证（不依赖 fs exists——路径可能因 git 差异而不同）
    import subprocess as _sp
    result = _sp.run(
        ["git", "worktree", "list"], cwd=str(temp_git_repo),
        capture_output=True, text=True,
    )
    assert task_id[:8] in result.stdout, \
        f"Worktree for {task_id[:8]} not found in:\n{result.stdout}"

    # discard 清理
    result = await svc.merge_worktree(task_id, strategy="discard")
    assert result["ok"] is True
    assert result["strategy"] == "discard"


@pytest.mark.asyncio
async def test_create_worktree_invalid_repo():
    """非 git 仓库创建 worktree 返回 None"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    with tempfile.TemporaryDirectory() as td:
        ws_root = td
        not_repo = os.path.join(td, "not-a-repo")
        os.makedirs(not_repo, exist_ok=True)
        os.environ["YUNCAT_WORKSPACE"] = ws_root
        from services.git_worktree_service import GitWorktreeService
        from services.git_service import GitService

        svc = GitWorktreeService(git_service=GitService(repo_path=not_repo))
        wt = await svc.create_worktree("task-x")
        assert wt is None
