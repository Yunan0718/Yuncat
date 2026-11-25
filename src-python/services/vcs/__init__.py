"""services/vcs/__init__.py —— VCS 客户端包入口"""
from services.vcs.base import BaseVCSClient
from services.vcs.github_client import GitHubClient
from services.vcs.gitlab_client import GitLabClient

VCSProvider = {"github": GitHubClient, "gitlab": GitLabClient}

__all__ = ["BaseVCSClient", "GitHubClient", "GitLabClient", "VCSProvider"]
