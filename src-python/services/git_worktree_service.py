"""services/git_worktree_service.py —— Git Worktree 隔离服务（Phase 20）

借鉴 Jean (coollabsio/jean) 的 agent-per-worktree 架构：
- 每个 Agent 任务获得独立 worktree + 分支，并行工作零冲突
- 生命周期：created → in_use → completed/failed → archived → deleted
- 合并策略：fast_forward | squash | discard

DAG 整合：并行节点指定 worktree=true → 引擎自动创建/合并 worktree
"""
import asyncio
import logging
import os
import time as _time
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger("yuncat.worktree")

WORKTREE_PREFIX = "yuncat/agent"
WORKTREE_BASE_DIR = ".worktrees"


@dataclass
class WorktreeInfo:
    task_id: str
    branch: str
    path: str
    status: str  # in_use | completed | failed | archived


class GitWorktreeService:
    """git worktree 管理——封装 add/merge/remove/list 操作。

    所有 worktree 创建在 WORKTREE_BASE_DIR 下，用 task_id 短前缀命名。
    """

    def __init__(self, git_service=None):
        from security.sandbox import get_workspace
        from services.git_service import GitService  # noqa: F811
        ws = get_workspace()
        self._repo_path = os.path.join(str(ws), "shared")
        self._worktree_root = os.path.join(str(ws), WORKTREE_BASE_DIR)
        self._git = git_service or GitService(repo_path=self._repo_path)
        # 使用 git_service 的实际仓库路径（如有）
        if git_service:
            self._repo_path = getattr(git_service, "_repo_path", self._repo_path)
        os.makedirs(self._worktree_root, exist_ok=True)

    async def _run(self, *args: str) -> tuple[int, str]:
        """在仓库目录执行 git 命令"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", *args,
                cwd=self._repo_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            out = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")
            if err.strip():
                # 2026-10-22：stderr 不再混进 stdout（见 git_service._run 的同款说明）
                _log.warning("git %s stderr: %s", " ".join(args), err.strip()[:200])
            code = proc.returncode or 0
            return code, (out if code == 0 or out.strip() else err)
        except FileNotFoundError:
            return (127, "git: command not found")
        except Exception as e:
            return (1, str(e))

    # ---- 核心 API ----

    async def create_worktree(self, task_id: str) -> WorktreeInfo | None:
        """为指定 task 创建独立 worktree。

        1. 创建分支 yuncat/agent/<tid8>
        2. git worktree add .worktrees/<tid8> <branch>
        3. 返回 WorktreeInfo
        """
        if not await self._git.is_repo():
            _log.warning("Cannot create worktree: not a git repository")
            return None

        # 短前缀——防止 Windows 路径过长
        short_id = task_id[:8]
        branch = f"{WORKTREE_PREFIX}/{short_id}"
        worktree_path = os.path.join(self._worktree_root, f"yuncat-{short_id}")

        # 确保分支存在（用 git branch 而非 checkout -b，避免占用当前 repo）
        code, _ = await self._run("rev-parse", "--verify", branch)
        if code != 0:
            code2, out2 = await self._run("branch", branch, "HEAD")
            if code2 != 0:
                _log.error("Failed to create branch %s: %s", branch, out2)
                return None

        # 创建 worktree（如果目录已存在，先移除）
        code, out = await self._run(
            "worktree", "add", worktree_path, branch,
        )
        if code != 0:
            msg = out.lower()
            if "already exists" in msg or "already checked out" in msg:
                _log.info("Worktree %s already exists, removing and retrying", worktree_path)
                await self._run("worktree", "remove", "--force", worktree_path)
                code2, out2 = await self._run("worktree", "add", worktree_path, branch)
                if code2 != 0:
                    _log.error("Failed to create worktree on retry: %s", out2)
                    return None
            else:
                _log.error("Failed to create worktree: %s", out)
                return None

        _log.info("Created worktree: %s → %s", branch, worktree_path)
        return WorktreeInfo(
            task_id=task_id,
            branch=branch,
            path=worktree_path,
            status="in_use",
        )

    async def merge_worktree(
        self, task_id: str, strategy: str = "fast_forward",
    ) -> dict:
        """将 worktree 的修改合并回主分支并清理。

        strategy:
          - fast_forward: git merge --ff-only → 删除分支 + worktree
          - squash: git merge --squash → git commit → 清理
          - discard: git worktree remove --force → git branch -D
        """
        short_id = task_id[:8]
        branch = f"{WORKTREE_PREFIX}/{short_id}"
        worktree_path = os.path.join(self._worktree_root, f"yuncat-{short_id}")

        if strategy == "discard":
            await self._run("worktree", "remove", "--force", worktree_path)
            code, _ = await self._run("branch", "-D", branch)
            _log.info("Discarded worktree for task %s", task_id)
            return {"ok": True, "strategy": "discard", "message": "Changes discarded"}

        if strategy == "squash":
            code, out = await self._run("merge", "--squash", branch)
            if code != 0:
                return {"ok": False, "strategy": "squash", "error": out}
            code2, out2 = await self._run("commit", "-m", f"yuncat: merge {branch}")
            if code2 != 0:
                return {"ok": False, "strategy": "squash", "error": out2}

        elif strategy == "fast_forward":
            code, out = await self._run("merge", "--ff-only", branch)
            if code != 0:
                _log.warning(
                    "Fast-forward merge failed for %s: %s. Falling back to squash.",
                    branch, out,
                )
                # 回退到 squash
                code2, out2 = await self._run("merge", "--squash", branch)
                if code2 != 0:
                    return {"ok": False, "strategy": "fallback", "error": out2}
                code3, out3 = await self._run(
                    "commit", "-m", f"yuncat: merge {branch} (fallback from ff)",
                )
                if code3 != 0:
                    return {"ok": False, "strategy": "fallback", "error": out3}
                strategy = "squash"

        # 清理 worktree
        await self._run("worktree", "remove", worktree_path)
        code, _ = await self._run("branch", "-d", branch)

        _log.info("Merged worktree for task %s (strategy=%s)", task_id, strategy)
        return {"ok": True, "strategy": strategy, "message": f"Merged {branch}"}

    async def list_worktrees(self) -> list[WorktreeInfo]:
        """列出所有活跃 worktree"""
        code, out = await self._run("worktree", "list", "--porcelain")
        if code != 0:
            return []
        result = []
        current: dict = {}
        for line in out.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("worktree "):
                if current:
                    result.append(current)
                current = {"path": line.split(" ", 1)[1]}
            elif line.startswith("branch ") and current:
                raw = line.split(" ", 1)[1] if " " in line else ""
                # Strip refs/heads/ prefix
                current["branch"] = raw.replace("refs/heads/", "")
            elif line.startswith("HEAD ") and current:
                current["head"] = line.split(" ", 1)[1]
        if current:
            result.append(current)

        # 过滤出 yuncat worktrees
        yuncat_wts = []
        for wt in result:
            branch = wt.get("branch", "")
            if branch.startswith(WORKTREE_PREFIX):
                short_id = branch.split("/")[-1]
                yuncat_wts.append(WorktreeInfo(
                    task_id=short_id,
                    branch=branch,
                    path=wt.get("path", ""),
                    status="in_use",
                ))
        return yuncat_wts

    async def cleanup_stale(self, max_age_days: int = 7) -> int:
        """清理过期 worktree——归档超过 max_age_days 天的已完成 worktree。
        返回清理数量。
        """
        cleaned = 0
        wts = await self.list_worktrees()
        now = _time.time()
        for wt in wts:
            wt_path = Path(wt.path)
            if not wt_path.exists():
                continue
            # 检查 mtime——超过 max_age_days 则清理
            mtime = wt_path.stat().st_mtime if wt_path.exists() else 0
            age_days = (now - mtime) / 86400
            if age_days > max_age_days:
                code, _ = await self._run("worktree", "remove", "--force", wt.path)
                if code == 0:
                    await self._run("branch", "-D", wt.branch)
                    cleaned += 1
                    _log.info("Cleaned stale worktree: %s (age=%.1f days)", wt.path, age_days)
        return cleaned
