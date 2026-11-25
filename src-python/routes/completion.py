"""routes/completion.py — Phase 49-O: Monaco 行内补全端点

Task 6.1: 双层补全——LLM FIM 优先（若 provider 配置了 fim 段），规则引擎兜底。
"""
from fastapi import APIRouter, Request

from services.completion_service import (
    suggest_inline_completions,
    suggest_inline_completions_llm,
)

router = APIRouter(prefix="/api/v1", tags=["completion"])


@router.post("/inline-completion")
async def inline_completion(payload: dict, request: Request) -> dict:
    """行内补全——Task 6.1: 先试 LLM FIM，失败/无配置则回退规则引擎。

    async 以便非阻塞调用 LLM FIM API；规则引擎是纯 CPU 同步操作，耗时极短。
    """
    language = payload.get("language", "plaintext")
    prefix = payload.get("prefix", "")
    suffix = payload.get("suffix", "")
    filename = payload.get("filename")

    # 1. 尝试 LLM FIM（若 provider 支持）
    app = request.app
    model_config_svc = getattr(app.state, "model_config_service", None) if app else None
    if model_config_svc:
        try:
            model_cfg = await model_config_svc.get_active_config()
            llm_items = await suggest_inline_completions_llm(
                language, prefix, suffix, model_cfg, filename,
            )
            if llm_items:
                return {"items": llm_items, "source": "fim"}
        except Exception:
            # FIM 失败不影响规则引擎兜底
            pass

    # 2. 回退规则引擎
    items = suggest_inline_completions(language, prefix, suffix, filename)
    return {"items": items, "source": "rule"}
