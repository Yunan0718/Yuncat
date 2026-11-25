"""services/vcs/github_client.py —— GitHub REST API 客户端"""
import logging

import aiohttp

from services.vcs.base import BaseVCSClient

_log = logging.getLogger("yuncat.vcs.github")


class GitHubClient(BaseVCSClient):
    """封装 GitHub Pull Request 相关 API。"""

    BASE_URL = "https://api.github.com"

    def __init__(self, token: str):
        super().__init__(token)
        self._headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _repo_path(self, owner: str, repo: str) -> str:
        return f"/repos/{owner}/{repo}"

    async def _request(
        self, method: str, path: str, json_body: dict | None = None,
    ) -> dict:
        url = f"{self.BASE_URL}{path}"
        try:
            async with aiohttp.ClientSession(headers=self._headers) as session:
                async with session.request(method, url, json=json_body) as resp:
                    text = await resp.text()
                    if not resp.ok:
                        _log.warning("GitHub API error: %s %s -> %s", method, path, resp.status)
                        return {"ok": False, "error": f"GitHub API {resp.status}: {text[:200]}"}
                    try:
                        data = await resp.json() if text else {}
                    except Exception:
                        data = {"raw": text}
                    return {"ok": True, "data": data}
        except aiohttp.ClientError as e:
            _log.warning("GitHub request failed: %s", e)
            return {"ok": False, "error": f"Network error: {e}"}
        except Exception as e:
            _log.warning("GitHub unexpected error: %s", e)
            return {"ok": False, "error": str(e)}

    async def list_pull_requests(
        self, owner: str, repo: str, state: str = "open",
    ) -> dict:
        path = f"{self._repo_path(owner, repo)}/pulls?state={state}"
        return await self._request("GET", path)

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False,
        reviewers: list[str] | None = None,
    ) -> dict:
        payload = {
            "title": title,
            "body": body,
            "head": head,
            "base": base,
            "draft": draft,
        }
        result = await self._request(
            "POST", f"{self._repo_path(owner, repo)}/pulls", payload,
        )
        if not result["ok"]:
            return result
        pr = result.get("data", {})
        pr_number = pr.get("number")
        if pr_number and reviewers:
            await self._request(
                "POST",
                f"{self._repo_path(owner, repo)}/pulls/{pr_number}/requested_reviewers",
                {"reviewers": reviewers},
            )
        return {"ok": True, "data": pr}

    async def add_comment(
        self, owner: str, repo: str, pr_id: str, body: str,
    ) -> dict:
        path = f"{self._repo_path(owner, repo)}/issues/{pr_id}/comments"
        return await self._request("POST", path, {"body": body})

    async def merge_pull_request(
        self,
        owner: str,
        repo: str,
        pr_id: str,
        method: str = "merge",
    ) -> dict:
        path = f"{self._repo_path(owner, repo)}/pulls/{pr_id}/merge"
        return await self._request("PUT", path, {"merge_method": method})

    async def close_pull_request(
        self,
        owner: str,
        repo: str,
        pr_id: str,
        comment: str = "",
    ) -> dict:
        # Task 7.24: 先添加评论（如果有），再关闭 PR
        # 对标 GitHub UI "Close with comment" 行为
        if comment.strip():
            await self.add_comment(owner, repo, pr_id, comment)
        path = f"{self._repo_path(owner, repo)}/pulls/{pr_id}"
        return await self._request("PATCH", path, {"state": "closed"})

    async def review_pull_request(
        self,
        owner: str,
        repo: str,
        pr_id: str,
        event: str = "APPROVE",
        body: str = "",
    ) -> dict:
        # Task 7.24: 提交 PR review——APPROVE / REQUEST_CHANGES / COMMENT
        # GitHub API: POST /repos/{owner}/{repo}/pulls/{pr_id}/reviews
        path = f"{self._repo_path(owner, repo)}/pulls/{pr_id}/reviews"
        payload: dict = {"event": event.upper()}
        if body.strip():
            payload["body"] = body
        return await self._request("POST", path, payload)
