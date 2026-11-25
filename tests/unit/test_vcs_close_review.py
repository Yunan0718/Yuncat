"""tests/unit/test_vcs_close_review.py —— Task 7.24: VCS close/review 操作测试

覆盖:
  - GitHubClient.close_pull_request: 无评论 / 带评论
  - GitHubClient.review_pull_request: APPROVE / REQUEST_CHANGES / COMMENT
  - GitLabClient.close_pull_request: 无评论 / 带评论
  - GitLabClient.review_pull_request: APPROVE / REQUEST_CHANGES → note

对标 GitHub/GitLab REST API 文档——验证正确的 path + payload 构造。
"""
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ============================================================
# GitHubClient 测试
# ============================================================

class TestGitHubClientCloseReview:
    """Task 7.24: GitHubClient close/review 操作"""

    @pytest.mark.asyncio
    async def test_close_pr_without_comment(self):
        """关闭 PR 不带评论——只发 PATCH 请求"""
        from services.vcs.github_client import GitHubClient
        client = GitHubClient("fake-token")

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"ok": True, "data": {"state": "closed"}}
            result = await client.close_pull_request("owner", "repo", "42")
            assert result["ok"] is True
            # 只调了 _request 一次（PATCH close），没有 add_comment
            assert mock_req.call_count == 1
            method, path = mock_req.call_args[0][:2]
            assert method == "PATCH"
            assert "/repos/owner/repo/pulls/42" in path
            assert mock_req.call_args[0][2] == {"state": "closed"}

    @pytest.mark.asyncio
    async def test_close_pr_with_comment(self):
        """关闭 PR 带评论——先 POST 评论，再 PATCH close"""
        from services.vcs.github_client import GitHubClient
        client = GitHubClient("fake-token")

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"ok": True, "data": {}}
            await client.close_pull_request("owner", "repo", "42", comment="关闭原因")
            # 调了两次 _request：POST comment + PATCH close
            assert mock_req.call_count == 2
            # 第一次：评论
            method1, path1, body1 = mock_req.call_args_list[0][0][:3]
            assert method1 == "POST"
            assert "/issues/42/comments" in path1
            assert body1 == {"body": "关闭原因"}
            # 第二次：关闭
            method2, path2, body2 = mock_req.call_args_list[1][0][:3]
            assert method2 == "PATCH"
            assert "/pulls/42" in path2
            assert body2 == {"state": "closed"}

    @pytest.mark.asyncio
    async def test_review_approve(self):
        """PR 审查 APPROVE——POST /pulls/{pr_id}/reviews with event=APPROVE"""
        from services.vcs.github_client import GitHubClient
        client = GitHubClient("fake-token")

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"ok": True, "data": {"state": "APPROVED"}}
            await client.review_pull_request("owner", "repo", "42", event="APPROVE")
            method, path, payload = mock_req.call_args[0][:3]
            assert method == "POST"
            assert "/pulls/42/reviews" in path
            assert payload["event"] == "APPROVE"

    @pytest.mark.asyncio
    async def test_review_request_changes_with_body(self):
        """PR 审查 REQUEST_CHANGES——带 body 评论"""
        from services.vcs.github_client import GitHubClient
        client = GitHubClient("fake-token")

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"ok": True, "data": {}}
            await client.review_pull_request("owner", "repo", "42", event="REQUEST_CHANGES", body="请修复")
            method, path, payload = mock_req.call_args[0][:3]
            assert method == "POST"
            assert "/pulls/42/reviews" in path
            assert payload["event"] == "REQUEST_CHANGES"
            assert payload["body"] == "请修复"

    @pytest.mark.asyncio
    async def test_review_approve_without_body(self):
        """APPROVE 不带 body——payload 不含 body 字段"""
        from services.vcs.github_client import GitHubClient
        client = GitHubClient("fake-token")

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"ok": True, "data": {}}
            await client.review_pull_request("owner", "repo", "42", event="APPROVE", body="")
            payload = mock_req.call_args[0][2]
            assert "body" not in payload


# ============================================================
# GitLabClient 测试
# ============================================================

class TestGitLabClientCloseReview:
    """Task 7.24: GitLabClient close/review 操作"""

    @pytest.mark.asyncio
    async def test_close_mr_without_comment(self):
        """关闭 MR 不带评论——只发 PUT 请求"""
        from services.vcs.gitlab_client import GitLabClient
        client = GitLabClient("fake-token")

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"ok": True, "data": {"state": "closed"}}
            result = await client.close_pull_request("owner", "repo", "42")
            assert result["ok"] is True
            assert mock_req.call_count == 1
            method, path = mock_req.call_args[0][:2]
            assert method == "PUT"
            assert "/merge_requests/42" in path
            assert mock_req.call_args[0][2] == {"state_event": "close"}

    @pytest.mark.asyncio
    async def test_close_mr_with_comment(self):
        """关闭 MR 带评论——先 POST note，再 PUT close"""
        from services.vcs.gitlab_client import GitLabClient
        client = GitLabClient("fake-token")

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"ok": True, "data": {}}
            await client.close_pull_request("owner", "repo", "42", comment="关闭原因")
            assert mock_req.call_count == 2
            # 第一次：note
            method1, path1, body1 = mock_req.call_args_list[0][0][:3]
            assert method1 == "POST"
            assert "/merge_requests/42/notes" in path1
            assert body1 == {"body": "关闭原因"}
            # 第二次：关闭
            method2, path2, body2 = mock_req.call_args_list[1][0][:3]
            assert method2 == "PUT"
            assert "/merge_requests/42" in path2
            assert body2 == {"state_event": "close"}

    @pytest.mark.asyncio
    async def test_review_approve(self):
        """MR 审查 APPROVE——POST /merge_requests/{iid}/approve"""
        from services.vcs.gitlab_client import GitLabClient
        client = GitLabClient("fake-token")

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"ok": True, "data": {}}
            await client.review_pull_request("owner", "repo", "42", event="APPROVE")
            method, path = mock_req.call_args[0][:2]
            assert method == "POST"
            assert "/merge_requests/42/approve" in path

    @pytest.mark.asyncio
    async def test_review_request_changes_as_note(self):
        """MR REQUEST_CHANGES——GitLab 无直接 API，用 note 替代"""
        from services.vcs.gitlab_client import GitLabClient
        client = GitLabClient("fake-token")

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"ok": True, "data": {}}
            await client.review_pull_request("owner", "repo", "42", event="REQUEST_CHANGES", body="请修复")
            method, path, payload = mock_req.call_args[0][:3]
            assert method == "POST"
            assert "/merge_requests/42/notes" in path
            assert "[Request Changes]" in payload["body"]
            assert "请修复" in payload["body"]
