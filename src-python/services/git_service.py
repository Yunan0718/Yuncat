"""services/git_service.py —— Git 集成服务（Phase 13）

封装 git CLI 调用，操作工作区 ~/YuncatWorkspace/shared/ 目录。
所有命令通过 asyncio.subprocess 执行，cwd 固定为 workspace/shared。
"""
import asyncio
import logging
import os
import re
from pathlib import Path

_log = logging.getLogger("yuncat.git")


class GitService:
    def __init__(self, repo_path: str | None = None, model_config_service=None):
        from security.sandbox import get_workspace
        ws = get_workspace()
        self._repo_path = repo_path or os.path.join(str(ws), "shared")
        os.makedirs(self._repo_path, exist_ok=True)
        self._model_config = model_config_service

    async def _run(
        self, *args: str, env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        """执行 git 命令，返回 (returncode, stdout)；stderr 单独接，成功时不混进 stdout。

        env 为额外环境变量，用于 GIT_EDITOR=true 等非交互场景。
        """
        try:
            proc_env = None
            if env:
                proc_env = os.environ.copy()
                proc_env.update(env)
            proc = await asyncio.create_subprocess_exec(
                "git", *args, cwd=self._repo_path, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=proc_env)
            stdout, stderr = await proc.communicate()
            out, err = stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")
            code = proc.returncode or 0
            if err.strip():
                _log.warning("git %s stderr: %s", " ".join(args), err.strip()[:200])
            return code, (out if code == 0 or out.strip() else err)
        except FileNotFoundError:
            return (127, "git: command not found. Please install Git.")
        except Exception as e:
            return (1, str(e))

    async def is_repo(self) -> bool:
        """检测当前目录是否是 Git 仓库"""
        code, _ = await self._run("rev-parse", "--git-dir")
        return code == 0

    async def init(self) -> dict:
        """初始化 Git 仓库（如果尚未初始化）"""
        if await self.is_repo():
            return {"ok": True, "initialized": False, "message": "Already a git repository"}
        code, stdout = await self._run("init")
        if code == 0:
            _log.info("Initialized git repository at %s", self._repo_path)
            return {"ok": True, "initialized": True, "message": stdout.strip()}
        return {"ok": False, "error": stdout.strip()}

    async def status(self) -> dict:
        """返回工作区变更状态。格式:
        {
          ok, branch, ahead, behind,
          files: [{path, status, staged}]  // status: M/A/D/R/??
        }
        """
        if not await self.is_repo():
            return {"ok": False, "error": "Not a git repository", "files": []}

        # 分支名
        _, branch = await self._run("rev-parse", "--abbrev-ref", "HEAD")
        branch = branch.strip()

        # ahead/behind
        ahead = behind = 0
        code, ahead_str = await self._run("rev-list", "--count", f"HEAD..origin/{branch}")
        if code == 0 and ahead_str.strip():
            try: ahead = int(ahead_str.strip())
            except (ValueError, TypeError): pass
        code, behind_str = await self._run("rev-list", "--count", f"origin/{branch}..HEAD")
        if code == 0 and behind_str.strip():
            try: behind = int(behind_str.strip())
            except (ValueError, TypeError): pass

        # status --porcelain
        code, output = await self._run("status", "--porcelain")
        if code != 0:
            return {"ok": False, "error": output.strip(), "files": [], "branch": branch}

        files = []
        for line in output.strip().split("\n"):
            if not line: continue
            staged = line[0] if line[0] != " " else None
            unstaged = line[1] if line[1] != " " else None
            path = line[3:].strip()
            status_code = staged or unstaged or "?"
            files.append({"path": path, "status": status_code, "staged": staged or ""})

        return {"ok": True, "branch": branch, "ahead": ahead, "behind": behind, "files": files}

    async def diff(self, staged: bool = False, path: str = "") -> str:
        """获取文件 Diff"""
        args = ["diff"]
        if staged:
            args.append("--staged")
        if path:
            args.extend(["--", path])
        code, output = await self._run(*args)
        return output if code == 0 else f"Error: {output}"

    async def log(self, count: int = 20) -> dict:
        """获取提交历史"""
        code, output = await self._run(
            "log", f"--oneline", f"-{count}",
            "--format=%h|%s|%ar|%an",
        )
        if code != 0:
            return {"ok": False, "error": output.strip(), "commits": []}
        commits = []
        for line in output.strip().split("\n"):
            if not line: continue
            parts = line.split("|", 3)
            if len(parts) >= 4:
                commits.append({"hash": parts[0], "message": parts[1], "time": parts[2], "author": parts[3]})
        return {"ok": True, "commits": commits}

    async def commit(self, message: str) -> dict:
        """git add . && git commit -m message"""
        if not message.strip():
            return {"ok": False, "error": "Commit message required"}
        # 安全检查：禁止危险字符
        if any(c in message for c in [";", "&&", "||", "`", "$"]):
            return {"ok": False, "error": "Invalid characters in commit message"}

        # git add .
        code, output = await self._run("add", ".")
        if code != 0:
            return {"ok": False, "error": f"git add failed: {output}"}

        # git commit
        code, output = await self._run("commit", "-m", message)
        if code != 0:
            return {"ok": False, "error": output.strip()}

        # 提取 commit hash
        m = re.search(r"\[([^\]]+)\]", output)
        commit_hash = m.group(1) if m else "unknown"
        _log.info("Committed: %s -> %s", message[:50], commit_hash)
        return {"ok": True, "commit_hash": commit_hash, "message": output.strip()}

    async def push(self) -> dict:
        """git push"""
        code, output = await self._run("push")
        if code != 0:
            return {"ok": False, "error": output.strip()}
        return {"ok": True, "message": output.strip() or "Push successful"}

    # ---- Phase 18: 分支管理 ----
    async def branch_list(self) -> dict:
        """列出所有本地分支，标记当前分支"""
        code, output = await self._run("branch")
        if code != 0:
            return {"ok": False, "error": output.strip(), "branches": []}
        branches = []
        for line in output.strip().split("\n"):
            if not line: continue
            line = line.strip()
            current = line.startswith("*")
            name = line.lstrip("* ").strip()
            branches.append({"name": name, "current": current})
        return {"ok": True, "branches": branches}

    async def checkout(self, branch: str) -> dict:
        """切换到指定分支"""
        code, output = await self._run("checkout", branch)
        if code != 0:
            return {"ok": False, "error": output.strip()}
        return {"ok": True, "message": output.strip() or f"Switched to {branch}"}

    async def create_branch(self, branch: str) -> dict:
        """创建新分支并切换"""
        code, output = await self._run("checkout", "-b", branch)
        if code != 0:
            return {"ok": False, "error": output.strip()}
        return {"ok": True, "message": output.strip() or f"Created branch {branch}"}

    async def pull(self) -> dict:
        """git pull"""
        code, output = await self._run("pull")
        if code != 0:
            return {"ok": False, "error": output.strip()}
        return {"ok": True, "message": output.strip() or "Pull successful"}

    async def stash_push(self, message: str = "") -> dict:
        """git stash push"""
        args = ["stash", "push"]
        if message:
            args.extend(["-m", message])
        code, output = await self._run(*args)
        if code != 0:
            return {"ok": False, "error": output.strip()}
        return {"ok": True, "message": output.strip() or "Stashed"}

    async def stash_pop(self) -> dict:
        """git stash pop"""
        code, output = await self._run("stash", "pop")
        if code != 0:
            return {"ok": False, "error": output.strip()}
        return {"ok": True, "message": output.strip() or "Stash popped"}

    async def file_timeline(self, path: str) -> dict:
        """获取文件级 Git 时间线：提交历史 + blame 作者统计"""
        if not await self.is_repo():
            return {"ok": False, "error": "Not a git repository", "commits": [], "blame": []}

        # 提交历史（仅涉及该文件）
        code, output = await self._run(
            "log", "--oneline", "-20", "--format=%h|%s|%ar|%an", "--", path,
        )
        commits = []
        if code == 0:
            for line in output.strip().split("\n"):
                if not line:
                    continue
                parts = line.split("|", 3)
                if len(parts) >= 4:
                    commits.append({"hash": parts[0], "message": parts[1], "time": parts[2], "author": parts[3]})

        # blame 作者统计
        code, output = await self._run("blame", "--porcelain", "--", path)
        authors: dict[str, int] = {}
        if code == 0:
            for line in output.strip().split("\n"):
                if line.startswith("author ") and not line.startswith("author-mail") and not line.startswith("author-time") and not line.startswith("author-tz"):
                    author = line[7:].strip()
                    authors[author] = authors.get(author, 0) + 1

        blame = [{"author": a, "count": c} for a, c in sorted(authors.items(), key=lambda x: -x[1])]
        return {"ok": True, "commits": commits, "blame": blame}

    async def stage(self, path: str) -> dict:
        """暂存指定文件"""
        # Phase 47 修复（2.4 路径遍历防护）：校验 path 在工作区内
        from security.sandbox import validate_path
        try:
            validate_path(path)
        except PermissionError:
            return {"ok": False, "error": f"路径越界，已拒绝: {path}"}
        code, output = await self._run("add", "--", path)
        if code != 0:
            return {"ok": False, "error": output.strip()}
        return {"ok": True}

    async def unstage(self, path: str) -> dict:
        """取消暂存指定文件"""
        from security.sandbox import validate_path
        try:
            validate_path(path)
        except PermissionError:
            return {"ok": False, "error": f"路径越界，已拒绝: {path}"}
        code, output = await self._run("reset", "HEAD", "--", path)
        if code != 0:
            return {"ok": False, "error": output.strip()}
        return {"ok": True}

    async def discard(self, path: str) -> dict:
        """丢弃工作区对指定文件的修改（危险操作）"""
        from security.sandbox import validate_path
        try:
            validate_path(path)
        except PermissionError:
            return {"ok": False, "error": f"路径越界，已拒绝: {path}"}
        code, output = await self._run("checkout", "--", path)
        if code != 0:
            return {"ok": False, "error": output.strip()}
        return {"ok": True}

    async def graph(self) -> dict:
        """获取可视化分支图所需的提交与分支关系"""
        if not await self.is_repo():
            return {"ok": False, "error": "Not a git repository", "commits": [], "branches": []}

        # 提交：hash | message | time | author | parents
        code, output = await self._run(
            "log", "--all", "--oneline", "-50",
            "--format=%h|%s|%ar|%an|%p",
        )
        commits = []
        if code == 0:
            for line in output.strip().split("\n"):
                if not line:
                    continue
                parts = line.split("|", 4)
                commits.append({
                    "hash": parts[0],
                    "message": parts[1] if len(parts) > 1 else "",
                    "time": parts[2] if len(parts) > 2 else "",
                    "author": parts[3] if len(parts) > 3 else "",
                    "parents": parts[4].strip().split() if len(parts) > 4 and parts[4].strip() else [],
                })

        # 分支指向
        code, output = await self._run("for-each-ref", "--format=%(refname:short)|%(objectname:short)", "refs/heads/")
        branches = []
        if code == 0:
            for line in output.strip().split("\n"):
                if not line:
                    continue
                parts = line.split("|", 1)
                if len(parts) == 2:
                    branches.append({"name": parts[0], "hash": parts[1]})

        return {"ok": True, "commits": commits, "branches": branches}

    async def suggest_commit_message(self) -> dict:
        """基于 staged/unstaged diff 调用 LLM 生成提交信息"""
        if not await self.is_repo():
            return {"ok": False, "error": "Not a git repository", "message": ""}
        diff = await self.diff(staged=True)
        if not diff.strip():
            diff = await self.diff(staged=False)
        if not diff.strip():
            return {"ok": False, "error": "No changes to summarize", "message": ""}
        prompt = (
            "根据以下 Git diff，生成一条简洁、符合规范的提交信息（不超过 60 个字符）。"
            "使用中文或英文均可。直接返回提交信息文本，不要解释。\n\n" + diff[:12000]
        )
        message = await self._call_llm(prompt)
        message = message.strip().strip('"').strip("'")
        return {"ok": True, "message": message}

    async def ai_review(self) -> dict:
        """基于 staged/unstaged diff 调用 LLM 生成代码审查意见"""
        if not await self.is_repo():
            return {"ok": False, "error": "Not a git repository", "review": ""}
        staged = await self.diff(staged=True)
        unstaged = await self.diff(staged=False)
        if not staged.strip() and not unstaged.strip():
            return {"ok": False, "error": "No changes to review", "review": ""}
        sections = []
        if staged.strip():
            sections.append("已暂存变更（staged）：\n" + staged[:8000])
        if unstaged.strip():
            sections.append("未暂存变更（unstaged）：\n" + unstaged[:8000])
        prompt = (
            "你是一位资深软件工程师。请审查以下 Git diff，从代码质量、潜在 bug、"
            "性能、可读性、安全性、测试覆盖等方面给出意见。使用中文回复，按条目列出，"
            "每条给出简要说明和改进建议。如果代码没有明显问题，请明确说明。\n\n"
            + "\n\n".join(sections)
        )
        review = await self._call_llm(prompt)
        return {"ok": True, "review": review.strip()}

    # ---- Phase 102: Merge conflict resolution ----

    async def merge_conflict_list(self) -> dict:
        """列出当前合并冲突文件（status U = unmerged）。"""
        if not await self.is_repo():
            return {"ok": False, "error": "Not a git repository", "files": []}
        code, output = await self._run("diff", "--name-only", "--diff-filter=U")
        if code != 0:
            return {"ok": False, "error": output.strip(), "files": []}
        files = [line.strip() for line in output.strip().split("\n") if line.strip()]
        return {"ok": True, "files": files}

    async def merge_conflict_read(self, path: str) -> dict:
        """读取冲突文件的三栏内容 + hunks。

        返回:
          - ours / theirs / base：各分支版本内容
          - current：工作区当前含冲突标记的内容
          - hunks：冲突 hunk 列表，含行号、ours/theirs 内容
        """
        if not await self.is_repo():
            return {"ok": False, "error": "Not a git repository"}
        from pathlib import Path
        safe_path = Path(path).as_posix().lstrip("/")

        # stage 1=base, 2=ours, 3=theirs
        async def _read_stage(stage: int) -> str:
            code, output = await self._run("show", f":{stage}:{safe_path}")
            return output if code == 0 else ""

        base_content = await _read_stage(1)
        ours_content = await _read_stage(2)
        theirs_content = await _read_stage(3)

        # 工作区当前内容（含冲突标记）
        current_path = Path(self._repo_path) / safe_path
        try:
            current_content = current_path.read_text(encoding="utf-8")
        except Exception:
            current_content = ""

        # 解析冲突标记
        hunks = self._parse_conflict_hunks(current_content)

        return {
            "ok": True,
            "path": safe_path,
            "base": base_content,
            "ours": ours_content,
            "theirs": theirs_content,
            "current": current_content,
            "hunks": hunks,
        }

    @staticmethod
    def _parse_conflict_hunks(content: str) -> list[dict]:
        """解析 `<<<<<<< / ======= / >>>>>>>` 冲突标记为 hunks 列表。"""
        hunks: list[dict] = []
        lines = content.split("\n")
        i = 0
        hunk_index = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("<<<<<<<"):
                start_line = i
                hunk_index += 1
                marker_ours = line
                i += 1
                ours_lines: list[str] = []
                while i < len(lines) and not lines[i].startswith("======"):
                    ours_lines.append(lines[i])
                    i += 1
                # skip =======
                i += 1
                theirs_lines: list[str] = []
                while i < len(lines) and not lines[i].startswith(">>>>>>>"):
                    theirs_lines.append(lines[i])
                    i += 1
                marker_theirs = lines[i] if i < len(lines) else ">>>>>>>"
                end_line = i
                hunks.append({
                    "index": hunk_index,
                    "start_line": start_line,
                    "end_line": end_line,
                    "marker_ours": marker_ours,
                    "marker_theirs": marker_theirs,
                    "ours_text": "\n".join(ours_lines),
                    "theirs_text": "\n".join(theirs_lines),
                    "ours_line_count": len(ours_lines),
                    "theirs_line_count": len(theirs_lines),
                })
            i += 1
        return hunks

    async def merge_conflict_resolve(
        self, path: str, resolution: str, mark_resolved: bool = True,
    ) -> dict:
        """将解决后的内容写回工作区并可选 stage 该文件。"""
        if not await self.is_repo():
            return {"ok": False, "error": "Not a git repository"}
        from pathlib import Path
        safe_path = Path(path).as_posix().lstrip("/")
        file_path = Path(self._repo_path) / safe_path
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(resolution, encoding="utf-8")
        except Exception as e:
            return {"ok": False, "error": f"Write failed: {e}"}

        if mark_resolved:
            code, output = await self._run("add", "--", safe_path)
            if code != 0:
                return {"ok": False, "error": f"git add failed: {output.strip()}"}
        return {"ok": True, "path": safe_path}

    # ---- Phase 103: Git 高级操作 ----

    async def blame(self, path: str) -> dict:
        """获取文件行级 blame 信息。"""
        if not await self.is_repo():
            return {"ok": False, "error": "Not a git repository", "lines": []}
        code, output = await self._run("blame", "--porcelain", "--", path)
        if code != 0:
            return {"ok": False, "error": output.strip(), "lines": []}
        lines = self._parse_blame(output)
        return {"ok": True, "path": path, "lines": lines}

    @staticmethod
    def _parse_blame(output: str) -> list[dict]:
        """解析 `git blame --porcelain` 输出为行级列表。"""
        lines: list[dict] = []
        current: dict = {}
        for raw_line in output.split("\n"):
            if not raw_line:
                continue
            # 每行数据以 tab 开头
            if raw_line.startswith("\t"):
                current["content"] = raw_line[1:]
                lines.append(current)
                current = {}
                continue
            # 新 commit 行: hash line_num num_lines
            parts = raw_line.split(" ")
            if len(parts) >= 2 and parts[0] and all(c in "0123456789abcdef" for c in parts[0]):
                current = {"hash": parts[0]}
                continue
            if " " in raw_line:
                key, value = raw_line.split(" ", 1)
                if key in ("author", "author-mail", "author-time", "author-tz", "summary"):
                    current[key] = value
        return lines

    async def rebase_status(self) -> dict:
        """检查是否有进行中的 rebase/merge/cherry-pick/revert。

        即使不是 git 仓库也返回一致的 ``in_progress`` / ``any`` 字段，
        避免调用方因 KeyError 崩溃。
        """
        git_dir = Path(self._repo_path) / ".git"
        in_progress = {
            "rebase": (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists(),
            "merge": (git_dir / "MERGE_HEAD").exists(),
            "cherry_pick": (git_dir / "CHERRY_PICK_HEAD").exists(),
            "revert": (git_dir / "REVERT_HEAD").exists(),
        }
        result = {"in_progress": in_progress, "any": any(in_progress.values())}
        if not await self.is_repo():
            result["ok"] = False
            result["error"] = "Not a git repository"
            return result
        result["ok"] = True
        return result

    @staticmethod
    def _has_dangerous_chars(text: str) -> bool:
        return any(c in text for c in [";", "&&", "||", "`", "$", "|", ">", "<"])

    async def rebase_squash(self, base_ref: str, message: str) -> dict:
        """将 base_ref..HEAD 的提交合并为一个新提交。"""
        if not await self.is_repo():
            return {"ok": False, "error": "Not a git repository"}
        if not base_ref or self._has_dangerous_chars(base_ref) or self._has_dangerous_chars(message):
            return {"ok": False, "error": "Invalid characters in input"}
        status = await self.rebase_status()
        if status.get("any"):
            return {"ok": False, "error": "Another git operation is in progress"}

        code, output = await self._run("reset", "--soft", base_ref)
        if code != 0:
            return {"ok": False, "error": output.strip()}
        code, output = await self._run("commit", "-m", message)
        if code != 0:
            return {"ok": False, "error": output.strip()}
        return {"ok": True}

    async def rebase_fixup(self, target_hash: str) -> dict:
        """将当前 staged 变更 fixup 到指定提交。"""
        if not await self.is_repo():
            return {"ok": False, "error": "Not a git repository"}
        if not target_hash or self._has_dangerous_chars(target_hash):
            return {"ok": False, "error": "Invalid target hash"}
        status = await self.rebase_status()
        if status.get("any"):
            return {"ok": False, "error": "Another git operation is in progress"}

        code, output = await self._run("commit", "--fixup", f"={target_hash}")
        if code != 0:
            return {"ok": False, "error": output.strip()}
        # 非交互式 autosquash rebase
        base = f"{target_hash}^"
        # 检查是否为 root commit
        code, _ = await self._run("rev-parse", base)
        if code != 0:
            base = "--root"
        code, output = await self._run(
            "rebase", "-i", "--autosquash", base,
            env={"GIT_SEQUENCE_EDITOR": "true"},
        )
        if code != 0:
            return {"ok": False, "error": output.strip()}
        return {"ok": True}

    async def rebase_onto(self, upstream: str, branch: str | None = None) -> dict:
        """将当前分支 rebase 到 upstream。"""
        if not await self.is_repo():
            return {"ok": False, "error": "Not a git repository"}
        if not upstream or self._has_dangerous_chars(upstream) or (branch and self._has_dangerous_chars(branch)):
            return {"ok": False, "error": "Invalid ref"}
        status = await self.rebase_status()
        if status.get("any"):
            return {"ok": False, "error": "Another git operation is in progress"}

        args = ["rebase"]
        if branch:
            args.extend(["--onto", upstream, branch])
        else:
            args.append(upstream)
        code, output = await self._run(*args)
        if code != 0:
            return {"ok": False, "error": output.strip()}
        return {"ok": True}

    async def rebase_continue(self) -> dict:
        """继续进行中的 rebase。"""
        if not await self.is_repo():
            return {"ok": False, "error": "Not a git repository"}
        code, output = await self._run("rebase", "--continue", env={"GIT_EDITOR": "true"})
        if code != 0:
            return {"ok": False, "error": output.strip()}
        return {"ok": True}

    async def rebase_abort(self) -> dict:
        """中止进行中的 rebase。"""
        if not await self.is_repo():
            return {"ok": False, "error": "Not a git repository"}
        status = await self.rebase_status()
        if status.get("in_progress", {}).get("rebase"):
            code, output = await self._run("rebase", "--abort")
        elif status.get("in_progress", {}).get("merge"):
            code, output = await self._run("merge", "--abort")
        elif status.get("in_progress", {}).get("cherry_pick"):
            code, output = await self._run("cherry-pick", "--abort")
        elif status.get("in_progress", {}).get("revert"):
            code, output = await self._run("revert", "--abort")
        else:
            return {"ok": True, "message": "No operation to abort"}
        if code != 0:
            return {"ok": False, "error": output.strip()}
        return {"ok": True}

    async def cherry_pick(self, commit_hash: str, no_commit: bool = False) -> dict:
        """cherry-pick 指定提交。"""
        if not await self.is_repo():
            return {"ok": False, "error": "Not a git repository"}
        if not commit_hash or self._has_dangerous_chars(commit_hash):
            return {"ok": False, "error": "Invalid commit hash"}
        args = ["cherry-pick", "--no-edit"]
        if no_commit:
            args.append("--no-commit")
        args.append(commit_hash)
        code, output = await self._run(*args)
        if code != 0:
            return {"ok": False, "error": output.strip()}
        return {"ok": True}

    async def revert(self, commit_hash: str, no_commit: bool = False) -> dict:
        """revert 指定提交。"""
        if not await self.is_repo():
            return {"ok": False, "error": "Not a git repository"}
        if not commit_hash or self._has_dangerous_chars(commit_hash):
            return {"ok": False, "error": "Invalid commit hash"}
        args = ["revert", "--no-edit"]
        if no_commit:
            args.append("--no-commit")
        args.append(commit_hash)
        code, output = await self._run(*args)
        if code != 0:
            return {"ok": False, "error": output.strip()}
        return {"ok": True}

    async def tag_list(self) -> dict:
        """列出所有标签。"""
        if not await self.is_repo():
            return {"ok": False, "error": "Not a git repository", "tags": []}
        code, output = await self._run("tag", "-l", "-n1")
        if code != 0:
            return {"ok": False, "error": output.strip(), "tags": []}
        tags = []
        for line in output.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.strip().split(" ", 1)
            tags.append({"name": parts[0], "message": parts[1] if len(parts) > 1 else ""})
        return {"ok": True, "tags": tags}

    async def tag_create(
        self, name: str, target: str = "", annotate: bool = False, message: str = "",
    ) -> dict:
        """创建标签。"""
        if not await self.is_repo():
            return {"ok": False, "error": "Not a git repository"}
        if not name or self._has_dangerous_chars(name) or self._has_dangerous_chars(message):
            return {"ok": False, "error": "Invalid tag name or message"}
        args = ["tag"]
        if annotate:
            args.extend(["-a", "-m", message or f"Tag {name}"])
        args.append(name)
        if target:
            args.append(target)
        code, output = await self._run(*args)
        if code != 0:
            return {"ok": False, "error": output.strip()}
        return {"ok": True}

    async def tag_delete(self, name: str) -> dict:
        """删除标签。"""
        if not await self.is_repo():
            return {"ok": False, "error": "Not a git repository"}
        if not name or self._has_dangerous_chars(name):
            return {"ok": False, "error": "Invalid tag name"}
        code, output = await self._run("tag", "-d", name)
        if code != 0:
            return {"ok": False, "error": output.strip()}
        return {"ok": True}

    async def remote_list(self) -> dict:
        """列出远程仓库。"""
        if not await self.is_repo():
            return {"ok": False, "error": "Not a git repository", "remotes": []}
        code, output = await self._run("remote", "-v")
        if code != 0:
            return {"ok": False, "error": output.strip(), "remotes": []}
        remotes: dict[str, dict] = {}
        for line in output.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.strip().split()
            if len(parts) >= 3:
                name, url, kind = parts[0], parts[1], parts[2].strip("()")
                if name not in remotes:
                    remotes[name] = {"name": name}
                remotes[name][kind] = url
        return {"ok": True, "remotes": list(remotes.values())}

    async def remote_add(self, name: str, url: str) -> dict:
        """添加远程仓库。"""
        if not await self.is_repo():
            return {"ok": False, "error": "Not a git repository"}
        if not name or not url or self._has_dangerous_chars(name) or self._has_dangerous_chars(url):
            return {"ok": False, "error": "Invalid remote name or url"}
        code, output = await self._run("remote", "add", name, url)
        if code != 0:
            return {"ok": False, "error": output.strip()}
        return {"ok": True}

    async def remote_remove(self, name: str) -> dict:
        """删除远程仓库。"""
        if not await self.is_repo():
            return {"ok": False, "error": "Not a git repository"}
        if not name or self._has_dangerous_chars(name):
            return {"ok": False, "error": "Invalid remote name"}
        code, output = await self._run("remote", "remove", name)
        if code != 0:
            return {"ok": False, "error": output.strip()}
        return {"ok": True}

    async def remote_set_url(self, name: str, url: str) -> dict:
        """修改远程仓库 URL。"""
        if not await self.is_repo():
            return {"ok": False, "error": "Not a git repository"}
        if not name or not url or self._has_dangerous_chars(name) or self._has_dangerous_chars(url):
            return {"ok": False, "error": "Invalid remote name or url"}
        code, output = await self._run("remote", "set-url", name, url)
        if code != 0:
            return {"ok": False, "error": output.strip()}
        return {"ok": True}

    async def get_remote_url(self, name: str = "origin") -> dict:
        """解析远程 URL，返回 provider/owner/repo。"""
        if not await self.is_repo():
            return {"ok": False, "error": "Not a git repository"}
        if not name or self._has_dangerous_chars(name):
            return {"ok": False, "error": "Invalid remote name"}
        code, output = await self._run("remote", "get-url", name)
        if code != 0:
            return {"ok": False, "error": output.strip()}
        url = output.strip()
        parsed = self._parse_remote_url(url)
        if not parsed:
            return {"ok": False, "error": "Unsupported remote URL format"}
        return {"ok": True, **parsed}

    @staticmethod
    def _parse_remote_url(url: str) -> dict | None:
        """解析 Git 远程 URL 为 provider/owner/repo。"""
        url = url.strip().rstrip("/")
        if not url:
            return None

        # SSH: git@github.com:owner/repo.git
        if url.startswith("git@") and ":" in url:
            host_part, path_part = url.split(":", 1)
            host = host_part.replace("git@", "").strip()
            path = path_part
        elif "://" in url:
            # HTTPS: https://github.com/owner/repo.git
            _, rest = url.split("://", 1)
            host, _, path = rest.partition("/")
            if "@" in host:
                _, host = host.split("@", 1)
        else:
            return None

        path = path.replace(".git", "").strip("/")
        parts = [p for p in path.split("/") if p]
        if len(parts) < 2:
            return None

        if "github.com" in host:
            provider = "github"
        elif "gitlab.com" in host:
            provider = "gitlab"
        else:
            provider = "unknown"

        return {
            "provider": provider,
            "owner": parts[-2],
            "repo": parts[-1],
            "host": host,
            "raw_url": url,
        }

    async def _call_llm(self, prompt: str) -> str:
        """调用当前激活 LLM 完成文本生成"""

        if not self._model_config:
            return ""
        try:
            cfg = await self._model_config.get_active_config()
            api_key = cfg.get("api_key", "")
            base_url = cfg.get("base_url", "")
            model = cfg.get("model", "")
            if not api_key or not base_url or not model:
                return ""
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
                async with s.post(
                    f"{base_url}/chat/completions",
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": "You are a senior software engineer doing code review."},
                            {"role": "user", "content": prompt},
                        ],
                        "max_tokens": 2048,
                        "temperature": 0.3,
                    },
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        msg = data["choices"][0]["message"]
                        return msg.get("content") or msg.get("reasoning_content") or ""
                    _log.warning("git llm: LLM returned %d", resp.status)
                    return ""
        except Exception as e:
            _log.warning("git llm: request failed: %s", type(e).__name__)
            return ""
