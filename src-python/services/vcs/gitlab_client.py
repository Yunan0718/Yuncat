"""services/vcs/gitlab_client.py —— GitLab API 客户端"""
import logging
from urllib.parse import quote

import aiohttp

from services.vcs.base import BaseVCSClient

_log = logging.getLogger("yuncat.vcs.gitlab")


class GitLabClient(BaseVCSClient):
    """封装 GitLab Merge Request 相关 API。"""

    DEFAULT_BASE_URL = "https://gitlab.com/api/v4"

    def __init__(self, token: str, base_url: str = ""):
        super().__init__(token)
        self._base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._headers = {"PRIVATE-TOKEN": token}

    def _project_path(self, owner: str, repo: str) -> str:
        project = quote(f"{owner}/{repo}", safe="")
        return f"/projects/{project}"

    async def _request(
        self, method: str, path: str, json_body: dict | None = None,
    ) -> dict:
        url = f"{self._base_url}{path}"
        try:
            async with aiohttp.ClientSession(headers=self._headers) as session:
                async with session.request(method, url, json=json_body) as resp:
                    text = await resp.text()
                    if not resp.ok:
                        _log.warning("GitLab API error: %s %s -> %s", method, path, resp.status)
                        return {"ok": False, "error": f"GitLab API {resp.status}: {text[:200]}"}
                    try:
                        data = await resp.json() if text else {}
                    except Exception:
                        data = {"raw": text}
                    return {"ok": True, "data": data}
        except aiohttp.ClientError as e:
            _log.warning("GitLab request failed: %s", e)
            return {"ok": False, "error": f"Network error: {e}"}
        except Exception as e:
            _log.warning("GitLab unexpected error: %s", e)
            return {"ok": False, "error": str(e)}

    async def list_pull_requests(
        self, owner: str, repo: str, state: str = "opened",
    ) -> dict:
        gitlab_state = state if state in ("opened", "closed", "merged", "all") else "opened"
        path = f"{self._project_path(owner, repo)}/merge_requests?state={gitlab_state}"
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
            "source_branch": head,
            "target_branch": base,
            "title": title,
            "description": body,
        }
        if draft:
            payload["work_in_progress"] = True
        result = await self._request(
            "POST",
            f"{self._project_path(owner, repo)}/merge_requests",
            payload,
        )
        if not result["ok"]:
            return result
        mr = result.get("data", {})
        mr_id = mr.get("iid")
        if mr_id and reviewers:
            await self._request(
                "PUT",
                f"{self._project_path(owner, repo)}/merge_requests/{mr_id}",
                {"reviewer_ids": reviewers},
            )
        return {"ok": True, "data": mr}

    async def add_comment(
        self, owner: str, repo: str, pr_id: str, body: str,
    ) -> dict:
        path = f"{self._project_path(owner, repo)}/merge_requests/{pr_id}/notes"
        return await self._request("POST", path, {"body": body})

    async def merge_pull_request(
        self,
        owner: str,
        repo: str,
        pr_id: str,
        method: str = "merge",
    ) -> dict:
        path = f"{self._project_path(owner, repo)}/merge_requests/{pr_id}/merge"
        payload = {"squash": method == "squash", "should_remove_source_branch": False}
        return await self._request("PUT", path, payload)

    async def close_pull_request(
        self,
        owner: str,
        repo: str,
        pr_id: str,
        comment: str = "",
    ) -> dict:
        # Task 7.24: 先添加 note（如果有），再关闭 MR
        # 对标 GitLab UI "Close with comment" 行为
        if comment.strip():
            await self.add_comment(owner, repo, pr_id, comment)
        path = f"{self._project_path(owner, repo)}/merge_requests/{pr_id}"
        return await self._request("PUT", path, {"state_event": "close"})

    async def review_pull_request(
        self,
        owner: str,
        repo: str,
        pr_id: str,
        event: str = "APPROVE",
        body: str = "",
    ) -> dict:
        # Task 7.24: GitLab MR approve API
        # APPROVE → POST /projects/{id}/merge_requests/{iid}/approve
        # REQUEST_CHANGES/COMMENT → POST .../notes（GitLab 无直接 "request changes" API，用 note 替代）
        project = self._project_path(owner, repo)
        if event.upper() == "APPROVE":
            path = f"{project}/merge_requests/{pr_id}/approve"
            payload: dict = {}
            return await self._request("POST", path, payload)
        # COMMENT / REQUEST_CHANGES → 添加 note
        note_body = body if event.upper() == "COMMENT" else f"[Request Changes] {body}".strip()
        path = f"{project}/merge_requests/{pr_id}/notes"
        return await self._request("POST", path, {"body": note_body})
