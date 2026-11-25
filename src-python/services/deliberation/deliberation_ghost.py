"""services/deliberation_ghost.py —— Ghost Library 验证 + 链式多后端协作。

从 deliberation_service.py 拆分而来，保留全部功能。
模块级函数以 self 为首参，由 DeliberationService 薄包装委托调用。
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import uuid as _uuid

from config import (
    adaptive_truncate, calc_context_budget, DELIBERATION_VISION_TIMEOUT, DEFAULT_AGENT_ID, OPENCLAW_AGENT_ID,
    GHOST_REVIEW_BUDGET_CHARS, MAX_CONTEXT_FALLBACK_CHARS,
)
# 复用 spec 模块的路由解析——避免重复定义
from services.deliberation.deliberation_spec import _resolve_spec_role_routing

_log = logging.getLogger("yuncat.deliberation")

# ═══════════════════════════════════════════════════════════
# Phase 46 E.1: Ghost Library — Spec 可移植性验证
#
# 对标 OpenAI: "扔掉代码，保留 Spec，Agent 随时能重建。"
# 独立的 Spec→实现→审计验证循环:
#   G1: Agent A 只看 Spec → 从零实现
#   G2: Agent B 审计 → 对比原始 vs 重建
#   G3: 差异分析 → Spec 改进建议
# ═══════════════════════════════════════════════════════════

async def ghost_library_verify(
    self, spec_yaml: str, original_impl: dict,
    configured_providers: list[str] | None = None,
    push_callback=None,
    agent_id: str = DEFAULT_AGENT_ID,  # Phase 48-G: 铁律 #1 穿透
) -> dict:
    """Ghost Library 验证: Spec 能否被独立 Agent 精确复现。

    Args:
        spec_yaml: Spec YAML 字符串
        original_impl: 原始实现 {developer_output, files_touched}
        configured_providers: 已配置的 Provider 列表（用于多模型增强）
        push_callback: WS 进度推送

    Returns:
        {ghost_verified, spec_improvement, gaps, recommendation}

    Phase 48-G: agent_id 参数穿透——修复原硬编码 "hermes" 违反铁律 #1。
    """
    task_id = str(_uuid.uuid4())
    _providers = configured_providers or ["deepseek"]

    # Phase 56-P2: deliberation 预算按当前模型窗口动态计算
    model_cfg = {}
    try:
        if self.model_config:
            model_cfg = await self.model_config.get_active_config()
    except Exception:
        _log.debug("获取模型配置失败，使用 fallback 预算", exc_info=True)
    _ghost_impl_budget = calc_context_budget(model_cfg, fraction=0.018, min_chars=1200, max_chars=5000)
    _ghost_improve_budget = calc_context_budget(model_cfg, fraction=0.015, min_chars=800, max_chars=4000)

    await self._push(push_callback, task_id, "spec_phase", {
        "phase": "ghost_start", "task_id": task_id,
    })

    # G1: 重建 — 用不同的 Provider 增强多样性
    dev_task_id = f"{task_id}_ghost_dev"
    try:
        from prompts.spec_prompts import render_developer_prompt
        rebuild_prompt = render_developer_prompt(spec_yaml)
        rebuild_output = await self.agent_service._invoke_with_role_and_tools(
            agent_id=agent_id,  # Phase 48-G: 铁律 #1 穿透
            role_prompt=rebuild_prompt,
            task_input="仅凭上述 Spec 从零实现。不参考任何已有代码。",
            model_key="primary",
            max_tool_rounds=30,  # Phase 48-H: 3→30 对标主流无低上限
            task_id=dev_task_id,
        )
    except Exception as e:
        return {
            "ghost_verified": False,
            "error": f"Ghost 重建失败: {e}",
            "gaps": [],
            "recommendation": "重建 Agent 调用失败——无法验证 Spec 可移植性",
        }

    await self._push(push_callback, task_id, "spec_phase", {
        "phase": "ghost_rebuilt", "task_id": task_id,
    })

    # G2: 审计 — Ghost 审计员对比
    try:
        from prompts.spec_prompts import GHOST_AUDIT_PROMPT
        # 用不同 Provider 的 Reviewer 做审计
        audit_provider_cfg = None
        if len(_providers) >= 2:
            try:
                routing = _resolve_spec_role_routing("reviewer", 1, _providers)
                audit_provider_cfg = await self.agent_service._model_config.get_provider_config(
                    routing["provider_id"]
                )
            except Exception:
                _log.debug("解析 reviewer 路由失败", exc_info=True)

        audit_prompt = GHOST_AUDIT_PROMPT.format(
            spec_yaml=spec_yaml,
            original_impl=adaptive_truncate(
                _json.dumps(original_impl, ensure_ascii=False), MAX_CONTEXT_FALLBACK_CHARS, _ghost_impl_budget, 0.6),
            rebuilt_impl=adaptive_truncate(rebuild_output, MAX_CONTEXT_FALLBACK_CHARS, _ghost_impl_budget, 0.6),
        )
        raw_audit = await self.agent_service._invoke_with_role(
            agent_id, audit_prompt, task_id=task_id, model_key="secondary",
            provider_config=audit_provider_cfg,
        )
    except Exception as e:
        raw_audit = f"[GHOST_FAIL] 审计调用失败: {e}"

    # G3: 解析 + 差异分析
    has_pass = "[GHOST_PASS]" in raw_audit
    has_fail = "[GHOST_FAIL]" in raw_audit
    gaps: list[str] = []
    # 从审计输出中提取具体差距行
    for line in raw_audit.split("\n"):
        stripped = line.strip()
        if stripped.startswith("- ") and any(kw in stripped.lower() for kw in
            ["不一致", "mismatch", "missing", "缺失", "偏差", "不相符"]):
            gaps.append(stripped)

    if has_pass and not has_fail:
        return {
            "ghost_verified": True,
            "message": "Spec 可独立复现——已达到 Ghost Library 标准。代码可以安全扔掉，Spec 保留即可。",
            "gaps": [],
            "raw_audit": raw_audit,
        }

    # 不通过 → 建议 Spec 改进
    spec_improvement = ""
    if gaps:
        try:
            from prompts.spec_prompts import render_architect_prompt
            improve_task = (
                f"{spec_yaml}\n\n"
                f"## Ghost Library 验证失败\n"
                f"独立 Agent 仅凭 Spec 重建的实现与原始存在以下差距:\n"
                + "\n".join(gaps[:5]) +
                "\n\n请修正 Spec 使其能被独立 Agent 精确复现。"
            )
            spec_improvement = await self.agent_service._invoke_with_role(
                agent_id,  # Phase 48-G: 铁律 #1 穿透
                render_architect_prompt(improve_task, ""),
                task_id=task_id, model_key="secondary",
            )
        except Exception:
            _log.debug("调用 Hermes 生成 Spec 改进建议失败", exc_info=True)

    return {
        "ghost_verified": False,
        "spec_improvement": spec_improvement[:_ghost_improve_budget] or "Spec 未能通过可移植性验证",
        "gaps": gaps,
        "raw_audit": raw_audit,
        "recommendation": (
            f"Spec 需要改进——{len(gaps)} 个差距表明描述不够精确。"
            "修正后重新运行 ghost_library_verify()。"
        ),
    }


# ── Phase 43 F3.2: chain_collaborate — 链式多后端协作 ──

async def chain_collaborate(
    self, task_input: str,
    vision_inputs: list[dict] | None = None,
    task_id: str = "",
    agent_id: str = DEFAULT_AGENT_ID,  # Phase 48-G: 铁律 #1 穿透
) -> dict:
    """链式多后端协作: vision→Architect→Developer→Reviewer。

    对标 Claude Code chain-of-agents: 不是并行提案，而是链式传递上下文。
    Step 1 (可选): OpenClaw vision 分析图片/截图/PDF
    Step 2: Hermes Architect 生成 Spec
    Step 3: Hermes Developer 实现代码
    Step 4: Hermes Reviewer 审计 → 返回结果

    vision_inputs: [{"type":"url","value":"..."}, {"type":"file","value":"..."}, ...]

    Phase 48-G: agent_id 参数穿透——修复原硬编码 "hermes" 违反铁律 #1。
    """
    if not self.agent_service:
        return {"error": "agent_service not configured"}

    # Phase 56-P2: deliberation 预算按当前模型窗口动态计算
    model_cfg = {}
    try:
        if self.model_config:
            model_cfg = await self.model_config.get_active_config()
    except Exception:
        _log.debug("获取模型配置失败，使用 fallback 预算", exc_info=True)
    _vision_budget = calc_context_budget(model_cfg, fraction=0.012, min_chars=600, max_chars=3000)
    _spec_to_dev_budget = calc_context_budget(model_cfg, fraction=0.03, min_chars=2500, max_chars=8000)
    _spec_review_budget = calc_context_budget(model_cfg, fraction=0.02, min_chars=1500, max_chars=6000)
    _dev_review_budget = calc_context_budget(model_cfg, fraction=0.02, min_chars=1500, max_chars=6000)
    _result_architect_budget = calc_context_budget(model_cfg, fraction=0.015, min_chars=1200, max_chars=4000)
    _result_dev_budget = calc_context_budget(model_cfg, fraction=0.015, min_chars=1200, max_chars=4000)
    _result_review_budget = calc_context_budget(model_cfg, fraction=0.008, min_chars=600, max_chars=2500)

    ctx_text = task_input

    # Step 1: OpenClaw Vision (如果有视觉输入)
    if vision_inputs:
        oc = self.agent_service.adapters.get(OPENCLAW_AGENT_ID)
        if oc and hasattr(oc, "execute_tool"):
            results = []
            for inp in vision_inputs[:5]:
                try:
                    r = await asyncio.wait_for(
                        oc.execute_tool("openclaw_vision", {
                            "image": inp.get("value", ""),
                            "source_type": inp.get("type", "base64"),
                        }),
                        timeout=DELIBERATION_VISION_TIMEOUT,
                    )
                    results.append(_json.dumps(r.get("data", r), ensure_ascii=False)[:_vision_budget])
                except asyncio.TimeoutError:
                    results.append("[视觉超时]")
                except Exception as e:
                    results.append(f"[视觉失败: {e}]")
            if results:
                ctx_text = f"{ctx_text}\n\n## 视觉分析\n" + "\n---\n".join(results)
        elif vision_inputs and not (oc and hasattr(oc, "execute_tool")):
            ctx_text = f"{ctx_text}\n\n[OpenClaw不可用——请手动描述图片内容]"

    # Step 2: Architect (Hermes)
    architect_prompt = (
        "你是软件架构师。分析以下需求，生成 Spec 文档（YAML格式）。\n"
        f"需求:\n{ctx_text}\n\n"
        "输出格式:\n"
        "```yaml\n"
        "spec:\n"
        "  name: <功能名>\n"
        "  summary: <一句话描述>\n"
        "  files_to_create:\n"
        "    - path: file.py\n"
        "  files_to_modify:\n"
        "    - path: existing.py\n"
        "  api_contracts:\n"
        "    - method: GET\n"
        "      path: /api/...\n"
        "  data_models:\n"
        "    - name: Model\n"
        "```"
    )
    architect_output = await self.agent_service._invoke_with_role(
        agent_id, architect_prompt, ctx_text, model_key="flash",
    )

    # Step 3: Developer (Hermes)
    dev_task_id = f"{task_id}_chain_dev" if task_id else ""
    dev_output = await self.agent_service._invoke_with_role_and_tools(
        agent_id=agent_id,  # Phase 48-G: 铁律 #1 穿透
        role_prompt=(
            "你是高级工程师。严格按照以下 Spec 实现代码。\n"
            f"## Spec\n{architect_output[:_spec_to_dev_budget]}\n\n"
            "用 write_file 创建/修改文件。不偏离 Spec 设计。"
        ),
        task_input="实现上述 Spec。",
        model_key="primary",
        max_tool_rounds=30,  # Phase 48-H: 3→30 对标主流无低上限
        task_id=dev_task_id,
    )

    # Step 4: Reviewer (Hermes)
    reviewer_prompt = "你是代码审查者。对比以下 Spec 和实现，找出不一致。"
    reviewer_output = await self.agent_service._invoke_with_role(
        agent_id, reviewer_prompt,
        f"## Spec\n{architect_output[:_spec_review_budget]}\n\n## 实现\n{dev_output[:_dev_review_budget]}",
        model_key="flash",
    )

    return {
        "architect_spec": architect_output[:_result_architect_budget],
        "developer_output": dev_output[:_result_dev_budget],
        "review": reviewer_output[:_result_review_budget],
        "vision_summary": ctx_text if vision_inputs else None,
    }
