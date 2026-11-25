"""services/vcs/base.py —— VCS 客户端抽象基类"""
from abc import ABC, abstractmethod


class BaseVCSClient(ABC):
    """统一封装 GitHub / GitLab 等代码托管平台的 PR/MR 操作。"""

    def __init__(self, token: str):
        self._token = token

    @abstractmethod
    async def list_pull_requests(
        self, owner: str, repo: str, state: str = "open",
    ) -> dict:
        """返回 PR/MR 列表。"""

    @abstractmethod
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
        """创建 PR/MR。"""

    @abstractmethod
    async def add_comment(
        self, owner: str, repo: str, pr_id: str, body: str,
    ) -> dict:
        """为 PR/MR 添加评论。"""

    @abstractmethod
    async def merge_pull_request(
        self,
        owner: str,
        repo: str,
        pr_id: str,
        method: str = "merge",
    ) -> dict:
        """合并 PR/MR。method: merge / squash / rebase"""

    @abstractmethod
    async def close_pull_request(
        self,
        owner: str,
        repo: str,
        pr_id: str,
        comment: str = "",
    ) -> dict:
        """关闭 PR/MR。可选先添加评论再关闭（close with comment）。"""

    @abstractmethod
    async def review_pull_request(
        self,
        owner: str,
        repo: str,
        pr_id: str,
        event: str = "APPROVE",
        body: str = "",
    ) -> dict:
        """提交 PR/MR 审查。event: APPROVE / REQUEST_CHANGES / COMMENT。"""
