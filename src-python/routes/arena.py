"""routes/arena.py —— Arena 盲测 A/B 端点

提供模型列表、发起对战、投票、揭榜、排行榜查询。
"""
import logging

from fastapi import APIRouter, HTTPException, Request

from config import ARENA_LEADERBOARD_WINDOW_DAYS
from services.arena_service import ArenaModel

router = APIRouter(prefix="/api/v1", tags=["arena"])
_log = logging.getLogger("yuncat.arena_routes")


def _serialize_model(model: ArenaModel) -> dict:
    return {
        "provider": model.provider,
        "model": model.model,
        "display_name": model.display_name,
    }


def _serialize_battle_anonymous(battle) -> dict:
    """匿名视图：不暴露真实模型身份。"""
    return {
        "battle_id": battle.battle_id,
        "status": battle.status,
        "response_a": battle.response_a,
        "response_b": battle.response_b,
        "created_at": battle.created_at,
    }


def _serialize_battle_full(battle) -> dict:
    """完整视图：揭榜后返回真实模型。"""
    return {
        "battle_id": battle.battle_id,
        "status": battle.status,
        "response_a": battle.response_a,
        "response_b": battle.response_b,
        "model_a": _serialize_model(battle.model_a),
        "model_b": _serialize_model(battle.model_b),
        "choice": battle.choice,
        "created_at": battle.created_at,
        "voted_at": battle.voted_at,
    }


@router.get("/arena/models")
async def arena_models(request: Request):
    """列出可用于 Arena 的模型（已配置 API Key 且支持 text）。"""
    service = request.app.state.arena_service
    models = await service.eligible_models()
    return {"models": [_serialize_model(m) for m in models]}


@router.post("/arena/battle")
async def arena_battle(payload: dict, request: Request):
    """发起一次盲测对战。

    Body:
        prompt: str (必填)
        model_a?: {provider, model} — 可选，缺省自动挑选
        model_b?: {provider, model} — 可选，缺省自动挑选

    Returns 匿名视图，仅含 Model A/B 回复，不暴露真实模型。
    """
    prompt = payload.get("prompt", "")
    if not prompt:
        raise HTTPException(400, "Missing 'prompt'")

    service = request.app.state.arena_service
    model_a = _parse_model(payload.get("model_a"))
    model_b = _parse_model(payload.get("model_b"))

    try:
        battle = await service.run_battle(prompt, model_a=model_a, model_b=model_b)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        _log.warning("Arena battle failed: %s", e)
        raise HTTPException(500, f"Battle failed: {e}")

    return _serialize_battle_anonymous(battle)


def _parse_model(raw: dict | None) -> ArenaModel | None:
    if not raw or not isinstance(raw, dict):
        return None
    provider = raw.get("provider", "")
    model = raw.get("model", "")
    if not provider or not model:
        return None
    return ArenaModel(provider=provider, model=model, display_name=raw.get("display_name", ""))


@router.post("/arena/battle/{battle_id}/vote")
async def arena_battle_vote(battle_id: str, payload: dict, request: Request):
    """为对战投票。choice: 'a' | 'b'。"""
    choice = payload.get("choice", "")
    if choice not in ("a", "b"):
        raise HTTPException(400, "choice must be 'a' or 'b'")

    service = request.app.state.arena_service
    try:
        battle = await service.record_battle_vote(battle_id, choice)
    except ValueError as e:
        raise HTTPException(400, str(e))

    return {"ok": True, "battle": _serialize_battle_anonymous(battle)}


@router.get("/arena/battle/{battle_id}/reveal")
async def arena_battle_reveal(battle_id: str, request: Request):
    """揭榜：返回真实模型信息。需先投票。"""
    service = request.app.state.arena_service
    try:
        battle = service.reveal(battle_id)
    except ValueError as e:
        raise HTTPException(403, str(e))

    return _serialize_battle_full(battle)


@router.get("/arena/leaderboard")
async def arena_leaderboard(days: int = ARENA_LEADERBOARD_WINDOW_DAYS, request: Request = None):
    """盲测 A/B 排行榜：按 provider|model 聚合胜率。"""
    service = request.app.state.arena_service
    entries = await service.get_leaderboard(days=days)
    return {"entries": entries, "days": days}
