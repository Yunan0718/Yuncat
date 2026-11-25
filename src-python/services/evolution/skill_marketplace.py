"""services/evolution/skill_marketplace.py —— 外部 Skill 市场连接

Phase 47 修复（阶段 3 拆分）：从 skill_registry.py 抽出。
连接 ClawHub / agentskills.io / GitHub topics 搜索 Skill。
"""
from __future__ import annotations

import logging
import time as _time

_log = logging.getLogger("yuncat.skill.marketplace")


class SkillMarketplace:
    """连接外部 Skill 源——ClawHub / agentskills.io / GitHub topics。

    用法:
        mp = SkillMarketplace()
        results = await mp.search("code review")
        detail = await mp.get_detail("code-review-pro")
    """

    CLAWHUB_API = "https://clawhub.ai/api/v1/skills"
    AGENTSKILLS_API = "https://agentskills.io/api/skills"

    def __init__(self, cache_ttl: int = 3600):
        self._cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, list[dict]]] = {}

    async def search(self, query: str, category: str | None = None,
                     page: int = 1, per_page: int = 20) -> dict:
        """搜索外部 Skill 市场。ClawHub 优先，不可用时搜索 GitHub topics。"""
        cache_key = f"{query}|{category}|{page}"
        cached = self._cache.get(cache_key)
        if cached and _time.time() - cached[0] < self._cache_ttl:
            return {"source": "cache", "results": cached[1]}

        # Try ClawHub
        try:
            results = await self._search_clawhub(query, category, page, per_page)
            if results:
                self._cache[cache_key] = (_time.time(), results)
                return {"source": "clawhub", "results": results}
        except Exception:
            _log.debug("ClawHub search unavailable, falling back to GitHub")

        # Fallback: GitHub code search for skill.yaml files
        try:
            results = await self._search_github(query, page, per_page)
            if results:
                self._cache[cache_key] = (_time.time(), results)
                return {"source": "github", "results": results}
        except Exception:
            _log.debug("GitHub search unavailable")

        return {"source": "offline", "results": [],
                "message": "Skill marketplace unavailable. Install from local directory with POST /api/v1/skills/install"}

    async def _search_github(self, query: str, page: int, per_page: int) -> list[dict]:
        """GitHub code search for skill.yaml files (fallback when ClawHub is down)."""
        import aiohttp
        encoded = query.replace(" ", "+")
        url = f"https://api.github.com/search/code?q=skill.yaml+{encoded}+in:path&per_page={min(per_page, 30)}&page={page}"
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers={"Accept": "application/vnd.github.v3+json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                items = data.get("items", [])
                return [{
                    "id": f"github-{item.get('sha', '')[:8]}",
                    "title": item.get("repository", {}).get("full_name", ""),
                    "description": f"skill.yaml in {item.get('path', '')}",
                    "source": "github",
                    "url": item.get("html_url", ""),
                } for item in items[:per_page]]

    async def get_detail(self, skill_id: str) -> dict | None:
        """获取市场 Skill 详情"""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.CLAWHUB_API}/{skill_id}",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
        except Exception:
            _log.debug("Unexpected error", exc_info=True)
        return None

    async def _search_clawhub(self, query: str, category: str | None,
                              page: int, per_page: int) -> list[dict]:
        import aiohttp
        params = {"q": query, "page": page, "per_page": per_page}
        if category:
            params["category"] = category
        async with aiohttp.ClientSession() as session:
            async with session.get(
                self.CLAWHUB_API,
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("skills", data.get("results", []))
        return []


# 全局单例
_marketplace: SkillMarketplace | None = None


def get_marketplace() -> SkillMarketplace:
    global _marketplace
    if _marketplace is None:
        _marketplace = SkillMarketplace()
    return _marketplace
