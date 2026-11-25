"""services/vcs_service.py —— VCS 统一服务层"""
import logging

from services.settings_service import SettingsService
from services.vcs import VCSProvider

_log = logging.getLogger("yuncat.vcs")


class VCSService:
    """管理 VCS Token、平台发现与 Client 分发。"""

    def __init__(self, db):
        self._settings = SettingsService(db)

    def mask_token(self, token: str) -> str:
        """只保留 token 最后 4 位，其余用 *** 替代。"""
        if not token:
            return ""
        visible = token[-4:] if len(token) >= 4 else token
        return f"***{visible}"

    async def get_providers(self) -> dict:
        """返回已配置的平台列表（token 掩码显示）。"""
        providers = []
        all_settings = await self._settings.get_all()

        gh_token = await self._settings.get_decrypted("github_token") or ""
        if gh_token.strip():
            providers.append({
                "provider": "github",
                "token_mask": self.mask_token(gh_token),
                "configured": True,
            })

        gl_token = await self._settings.get_decrypted("gitlab_token") or ""
        gl_base_url = all_settings.get("gitlab_base_url", "")
        if gl_token.strip():
            providers.append({
                "provider": "gitlab",
                "token_mask": self.mask_token(gl_token),
                "base_url": gl_base_url or "https://gitlab.com/api/v4",
                "configured": True,
            })

        return {"ok": True, "providers": providers}

    async def get_client(self, provider: str):
        """根据 provider 名称返回对应 client 实例（未找到返回 None）。"""
        cls = VCSProvider.get(provider)
        if not cls:
            return None

        if provider == "github":
            token = await self._settings.get_decrypted("github_token")
            if not token:
                return None
            return cls(token)

        if provider == "gitlab":
            token = await self._settings.get_decrypted("gitlab_token")
            if not token:
                return None
            base_url = await self._settings.get_raw("gitlab_base_url") or ""
            return cls(token, base_url)

        return None
