"""services/arena_service.py —— Arena 盲测 A/B 对战服务

同一 prompt 并发调用两个不同模型，匿名展示 Model A/B，用户投票后记录到
analytics_events，并汇总成 leaderboard。不新增数据库表。
"""
import asyncio
import hashlib
import json
import logging
import random
import time as _time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from config import (
    ARENA_BATTLE_TIMEOUT_SEC,
    ARENA_BATTLE_MAX_TOKENS,
    ARENA_LEADERBOARD_WINDOW_DAYS,
    ARENA_BATTLE_TTL_HOURS,
    SYSTEM_ARENA_EVENTS_LIMIT,
)

_log = logging.getLogger("yuncat.arena")


@dataclass
class ArenaModel:
    provider: str
    model: str
    display_name: str = ""

    def __post_init__(self):
        if not self.display_name:
            self.display_name = f"{self.provider}/{self.model}"


@dataclass
class ArenaBattle:
    battle_id: str
    prompt: str
    model_a: ArenaModel
    model_b: ArenaModel
    response_a: str = ""
    response_b: str = ""
    status: str = "ready"  # ready | voted | expired
    choice: str | None = None  # "a" | "b" | None
    created_at: float = field(default_factory=_time.time)
    voted_at: float | None = None


class ArenaService:
    """管理 Arena 对战生命周期、直连 provider、读写 analytics_events。"""

    def __init__(
        self,
        db,
        model_config_service,
        analytics_service,
    ):
        self.db = db
        self.model_config = model_config_service
        self.analytics = analytics_service
        self._battles: dict[str, ArenaBattle] = {}

    # ============================================================
    # 模型选择
    # ============================================================

    async def eligible_models(self) -> list[ArenaModel]:
        """列出可用于 Arena 的模型：已配置 API Key 且支持 text 聊天。"""
        providers = await self.model_config.list_providers()
        models: list[ArenaModel] = []
        for p in providers:
            if not p.get("configured"):
                continue
            provider_id = p.get("provider", "")
            for m in p.get("models", []):
                supports = m.get("supports", ["text"])
                if "text" not in supports:
                    continue
                models.append(ArenaModel(
                    provider=provider_id,
                    model=m.get("id", ""),
                    display_name=m.get("name", m.get("id", "")),
                ))
        return models

    def pick_auto_top2(self, models: list[ArenaModel]) -> tuple[ArenaModel, ArenaModel]:
        """随机挑选两个不同模型。"""
        if len(models) < 2:
            raise ValueError("Need at least two eligible models")
        chosen = random.sample(models, 2)
        return chosen[0], chosen[1]

    # ============================================================
    # 对战执行
    # ============================================================

    async def run_battle(
        self,
        prompt: str,
        model_a: ArenaModel | None = None,
        model_b: ArenaModel | None = None,
    ) -> ArenaBattle:
        """运行一次盲测对战。未指定模型时自动挑选。"""
        if not prompt or not prompt.strip():
            raise ValueError("Prompt is required")

        if model_a is None or model_b is None:
            eligible = await self.eligible_models()
            model_a, model_b = self.pick_auto_top2(eligible)

        if model_a.provider == model_b.provider and model_a.model == model_b.model:
            raise ValueError("Model A and Model B must be different")

        response_a, response_b = await asyncio.gather(
            self._call_provider(model_a, prompt),
            self._call_provider(model_b, prompt),
            return_exceptions=True,
        )
        response_a = self._safe_response(response_a)
        response_b = self._safe_response(response_b)

        battle = ArenaBattle(
            battle_id=uuid.uuid4().hex,
            prompt=prompt,
            model_a=model_a,
            model_b=model_b,
            response_a=response_a,
            response_b=response_b,
        )
        self._battles[battle.battle_id] = battle
        self._cleanup_expired()

        await self.analytics.track("arena_blind_battle", {
            "battle_id": battle.battle_id,
            "prompt_hash": self._prompt_hash(prompt),
            "model_a_provider": model_a.provider,
            "model_a": model_a.model,
            "model_b_provider": model_b.provider,
            "model_b": model_b.model,
            "response_a_len": len(response_a),
            "response_b_len": len(response_b),
            "error_a": not response_a,
            "error_b": not response_b,
        })
        return battle

    def _safe_response(self, value: Any) -> str:
        """将并发结果归一为字符串，异常或空值返回空串。"""
        if isinstance(value, Exception):
            _log.warning("Provider call failed: %s", value)
            return ""
        if not isinstance(value, str):
            return ""
        return value

    async def _call_provider(self, model: ArenaModel, prompt: str) -> str:
        """直接调用 provider 的 /chat/completions，返回文本回复。"""
        cfg = await self.model_config.get_provider_config(model.provider)
        if not cfg:
            return ""

        base_url = cfg.get("base_url", "")
        api_key = cfg.get("api_key", "")
        if not base_url or not api_key:
            return ""

        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": ARENA_BATTLE_MAX_TOKENS,
            "temperature": 0.7,
            "stream": False,
        }

        try:
            async with httpx.AsyncClient(timeout=ARENA_BATTLE_TIMEOUT_SEC) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                choices = data.get("choices", [])
                if not choices:
                    return ""
                content = choices[0].get("message", {}).get("content", "")
                return content if isinstance(content, str) else ""
        except Exception as e:
            _log.warning("Arena provider call failed for %s/%s: %s", model.provider, model.model, e)
            return ""

    # ============================================================
    # 投票与揭榜
    # ============================================================

    async def record_battle_vote(self, battle_id: str, choice: str) -> ArenaBattle:
        """记录用户匿名投票。choice 为 'a' 或 'b'。"""
        battle = self._get_battle(battle_id)
        if battle.status == "expired":
            raise ValueError("Battle expired")
        if battle.choice is not None:
            raise ValueError("Battle already voted")
        if choice not in ("a", "b"):
            raise ValueError("Choice must be 'a' or 'b'")

        battle.choice = choice
        battle.voted_at = _time.time()
        battle.status = "voted"

        winner = battle.model_a if choice == "a" else battle.model_b
        loser = battle.model_b if choice == "a" else battle.model_a
        await self.analytics.track("arena_blind_vote", {
            "battle_id": battle_id,
            "choice": choice,
            "prompt_hash": self._prompt_hash(battle.prompt),
            "winner_provider": winner.provider,
            "winner_model": winner.model,
            "loser_provider": loser.provider,
            "loser_model": loser.model,
        })
        return battle

    def reveal(self, battle_id: str) -> ArenaBattle:
        """揭榜：返回包含真实模型信息的对战。需已投票或已过期。"""
        battle = self._get_battle(battle_id)
        if battle.status == "ready":
            raise ValueError("Please vote before reveal")
        return battle

    def _get_battle(self, battle_id: str) -> ArenaBattle:
        self._cleanup_expired()
        battle = self._battles.get(battle_id)
        if not battle:
            raise ValueError("Battle not found")
        return battle

    # ============================================================
    # 排行榜
    # ============================================================

    async def get_leaderboard(self, days: int = ARENA_LEADERBOARD_WINDOW_DAYS) -> list[dict]:
        """按 provider|model 聚合盲测胜率排行榜。"""
        cutoff = int(_time.time()) - days * 86400
        cursor = await self.db.execute(
            "SELECT event_data FROM analytics_events "
            "WHERE event_type='arena_blind_vote' AND created_at > ? "
            f"ORDER BY created_at DESC LIMIT {SYSTEM_ARENA_EVENTS_LIMIT}",
            (cutoff,),
        )
        rows = await cursor.fetchall()

        stats: dict[str, dict] = {}
        for (data_json,) in rows:
            try:
                d = json.loads(data_json or "{}")
            except Exception:
                continue
            winner_key = f"{d.get('winner_provider','')}|{d.get('winner_model','')}"
            loser_key = f"{d.get('loser_provider','')}|{d.get('loser_model','')}"
            for key, is_win in ((winner_key, True), (loser_key, False)):
                if not key or key == "|":
                    continue
                if key not in stats:
                    stats[key] = {"battles": 0, "wins": 0}
                stats[key]["battles"] += 1
                if is_win:
                    stats[key]["wins"] += 1

        entries = []
        for key, s in stats.items():
            provider, model = key.split("|", 1)
            battles = s["battles"]
            wins = s["wins"]
            entries.append({
                "provider": provider,
                "model": model,
                "battles": battles,
                "wins": wins,
                "win_rate": (wins / battles) if battles else 0.0,
            })
        entries.sort(key=lambda e: (e["win_rate"], e["battles"]), reverse=True)
        return entries

    # ============================================================
    # 辅助
    # ============================================================

    def _cleanup_expired(self) -> None:
        """清理超过 TTL 的内存对战。"""
        ttl = ARENA_BATTLE_TTL_HOURS * 3600
        now = _time.time()
        expired = [
            bid for bid, b in self._battles.items()
            if now - b.created_at > ttl
        ]
        for bid in expired:
            battle = self._battles.pop(bid)
            battle.status = "expired"

    @staticmethod
    def _prompt_hash(prompt: str) -> str:
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
