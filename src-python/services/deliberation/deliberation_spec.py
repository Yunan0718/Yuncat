"""services/deliberation_spec.py —— Spec 驱动开发主循环。

从 deliberation_service.py 拆分而来，保留全部功能。
模块级函数以 self 为首参，由 DeliberationService 薄包装委托调用。
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import os as _os
import time as _time
from pathlib import Path as _Path

# Phase 48-G: DEFAULT_AGENT_ID（agent_id 穿透默认值）+ DELIBERATION_ROLE_TIMEOUT（替代已删的 SPEC_ROLE_TIMEOUT 类属性）
from config import (
    DEFAULT_AGENT_ID, DELIBERATION_ROLE_TIMEOUT,
    DELIBERATION_AUDIT_LIST_MAX_ITEMS,
    DELIBERATION_AUDIT_VERIFICATION_RESULTS_MAX_ITEMS,
    DELIBERATION_SPEC_FILES_PREVIEW_LIMIT,
    DELIBERATION_SPEC_VERIFY_OUTPUT_PREVIEW_CHARS,
    DELIBERATION_SPEC_AUTO_FIX_ITEMS_LIMIT,
    DELIBERATION_SPEC_ESCALATION_ISSUES_LIMIT,
)

_log = logging.getLogger("yuncat.deliberation")

# ═══════════════════════════════════════════════════════════
# Phase 43 P1-SPEC B.3: Spec 模式三层路由表
# ═══════════════════════════════════════════════════════════

# role_id → (agent_id, provider_id, model_tier)
# provider_id=None → 由 _resolve_spec_role_routing() 动态决定
SPEC_ROLE_ROUTING: dict[str, tuple[str, str | None, str]] = {
    "architect":         ("hermes",   None, "flash"),
    "developer":         ("hermes",   None, "pro"),
    "reviewer":          ("hermes",   None, "flash"),
    # Phase 43 M.3: OpenClaw 原生角色
    "vision_analyst":    ("openclaw", None, "primary"),   # Peekaboo v3
    "search_expert":     ("openclaw", None, "flash"),     # web_search
    "messaging_specialist": ("openclaw", None, "flash"),  # 30+渠道
}


def _resolve_spec_role_routing(
    role_id: str, round_num: int, configured_providers: list[str],
    available_agents: list[str] | None = None,
) -> dict:
    """解析 Spec 模式下的角色→Agent→Provider→模型 完整路由。

    统一处理 N Provider 轮换策略:
    - N=1: 降级 — 全角色同一模型
    - N=2: Reviewer 走第二个 Provider
    - N=3: 三角色各走不同 Provider
    - N≥4: Reviewer 轮换池 — 每轮换一个 Provider 审查

    Phase 43 G1.4: 跨后端审查——当 ``available_agents`` 含 openclaw 时，
    Reviewer 路由到 openclaw agent（与 Provider 轮换正交），用独立后端
    做代码审查，避免单后端盲区。Architect/Developer 仍走 hermes。
    """
    n = len(configured_providers)
    base_agent, base_provider, base_tier = SPEC_ROLE_ROUTING.get(
        role_id, ("hermes", None, "flash"),
    )

    provider_id = base_provider or (configured_providers[0] if configured_providers else "deepseek")
    cross_backend_agent: str | None = None  # G1.4: 跨后端审查 agent（仅 reviewer）

    if role_id == "reviewer":
        if n >= 4:
            # 轮换池: Round 1→provider[1], Round 2→provider[2], ...
            idx = 1 + ((round_num - 1) % (n - 1))
            provider_id = configured_providers[idx]
        elif n == 3:
            provider_id = configured_providers[2]
        elif n == 2:
            provider_id = configured_providers[1]
        # n==1: 保持 base_provider 或 configured_providers[0]

        # G1.4: 跨后端审查——reviewer 用不同 agent（openclaw）提供独立视角。
        # 与 Provider 轮换正交：agent 多样性（不同后端）+ provider 多样性（不同模型）。
        # 仅当 openclaw 后端可用时启用；否则保持 hermes（向后兼容）。
        if available_agents and len(available_agents) >= 2 and "openclaw" in available_agents:
            cross_backend_agent = "openclaw"

    return {
        "agent_id": cross_backend_agent or base_agent,
        "provider_id": provider_id,
        "model_tier": base_tier,
        "reviewer_diversity": (
            len(configured_providers) >= 2 and role_id == "reviewer"
        ),
        "cross_backend_review": cross_backend_agent is not None,
        "reviewer_agent_id": cross_backend_agent,  # None → 用 base_agent（hermes）
    }


# ═══════════════════════════════════════════════════════════
# D.0: Provider 配置解析
# ═══════════════════════════════════════════════════════════

async def _resolve_spec_provider_config(
    self, role_id: str, round_num: int, configured_providers: list[str],
    available_agents: list[str] | None = None,
) -> dict | None:
    """解析 Spec 角色的完整 Provider 配置。

    Pipeline: SPEC_ROLE_ROUTING → get_provider_config() → 解密 API Key → 注入上下文。
    Fallback: 指定 Provider 不可用 → 自动降级到活跃配置。

    Phase 43 G1.4: 跨后端审查——reviewer 路由到 openclaw 时，用 openclaw 自己的
    Provider 配置（独立模型），并在 cfg 中标记 ``_reviewer_agent_id`` 供
    ``_invoke_spec_auditor`` 切换调用 agent。
    """
    routing = _resolve_spec_role_routing(role_id, round_num, configured_providers, available_agents)
    provider_id = routing["provider_id"]
    model_tier = routing["model_tier"]
    reviewer_agent_id = routing.get("reviewer_agent_id")  # G1.4: None 或 "openclaw"

    # G1.4: 跨后端审查——reviewer 用 openclaw 自己的 Provider 配置（独立模型/独立后端）
    if reviewer_agent_id and reviewer_agent_id != "hermes":
        try:
            mc = self.agent_service._model_config
            oc_provider = mc.get_agent_provider(reviewer_agent_id) or mc.get_active_provider()
            cfg = await mc.get_provider_config(oc_provider)
            if cfg and cfg.get("api_key"):
                cfg["_model_tier"] = model_tier
                cfg["_spec_role"] = role_id
                cfg["_reviewer_agent_id"] = reviewer_agent_id  # 供 auditor 切换 agent
                _log.info("Spec role '%s' → cross-backend agent '%s' provider '%s' (round %d)",
                         role_id, reviewer_agent_id, oc_provider, round_num)
                return cfg
        except Exception:
            _log.debug("Cross-backend provider config failed for %s, falling back to base routing",
                       reviewer_agent_id, exc_info=True)

    try:
        # 优先用多 Provider 路由（get_provider_config 已解密 API Key ✅）
        cfg = await self.agent_service._model_config.get_provider_config(provider_id)
        if cfg and cfg.get("api_key"):
            cfg["_model_tier"] = model_tier
            cfg["_spec_role"] = role_id
            _log.info("Spec role '%s' → provider '%s' (tier=%s, diversity=%s)",
                     role_id, provider_id, model_tier, routing["reviewer_diversity"])
            return cfg
    except Exception:
        _log.debug("Provider %s not available for role %s", provider_id, role_id)

    # Fallback: 无独立 Provider → 降级到活跃配置
    try:
        model_key = "primary" if model_tier == "pro" else "secondary"
        cfg = await self.agent_service._get_model_config(model_key)
        cfg["_model_tier"] = model_tier
        cfg["_spec_role"] = role_id
        return cfg
    except Exception:
        _log.error("No model config available for Spec role '%s'", role_id)
        return None


# ═══════════════════════════════════════════════════════════
# D.0b: 项目结构扫描
# Phase 48 T3-5: 拆分到 deliberation_project_scan.py（纯工具函数，无 self 依赖）
# ═══════════════════════════════════════════════════════════

from .deliberation_project_scan import (  # noqa: E402
    _get_project_structure,  # noqa: F401 — re-export: deliberation_service.py 通过 _spec._get_project_structure 引用
    _detect_key_imports,  # noqa: F401 — re-export: deliberation_service.py 通过 _spec._detect_key_imports 引用
)


# ═══════════════════════════════════════════════════════════
# D.0c: 检查点持久化
# ═══════════════════════════════════════════════════════════

def _checkpoint_path(self, task_id: str) -> _Path:
    self.SPEC_CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    return self.SPEC_CHECKPOINT_DIR / f"{task_id}.json"


def _save_checkpoint(self, task_id: str, data: dict) -> None:
    try:
        data["_timestamp"] = int(_time.time())
        data["_schema_version"] = self.SPEC_CHECKPOINT_SCHEMA_VERSION
        data["_app_version"] = _os.environ.get("YUNCAT_VERSION", "0.1.3")
        self._checkpoint_path(task_id).write_text(
            _json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    except Exception:
        _log.debug("Failed to save checkpoint for %s", task_id[:12])


def _load_checkpoint(self, task_id: str) -> dict | None:
    cp = self._checkpoint_path(task_id)
    if not cp.exists():
        return None
    try:
        data = _json.loads(cp.read_text(encoding="utf-8"))
        # Phase 44 C.2: schema_version 兼容性检查
        saved_version = data.get("_schema_version", 0)
        if saved_version < 1:
            _log.warning("Checkpoint %s has obsolete schema v%d, discarding",
                       task_id[:12], saved_version)
            self._delete_checkpoint(task_id)
            return None
        # v1→v1: 兼容。未来 v2+ 在此做迁移
        if saved_version > self.SPEC_CHECKPOINT_SCHEMA_VERSION:
            _log.warning("Checkpoint %s has future schema v%d (current v%d), skipping",
                       task_id[:12], saved_version, self.SPEC_CHECKPOINT_SCHEMA_VERSION)
            return None
        return data
    except Exception:
        _log.debug("Failed to load checkpoint for %s", task_id[:12])
        return None


def _delete_checkpoint(self, task_id: str) -> None:
    cp = self._checkpoint_path(task_id)
    if cp.exists():
        try:
            cp.unlink()
        except Exception:
            _log.debug("删除 Spec checkpoint 失败", exc_info=True)


# ═══════════════════════════════════════════════════════════
# Phase 48 T3-5: 从 run_spec_loop 拆分的子函数（模块级，self 首参）
# ═══════════════════════════════════════════════════════════


async def _run_auditor_phase(
    self, spec_yaml, spec_dict, developer_output, configured_providers,
    round_num, task_id, push_callback,
    agent_id: str = DEFAULT_AGENT_ID,  # Phase 48-G: 铁律 #1 穿透
    language: str = "zh",
):
    """阶段 3: Reviewer 双通道审计——返回 audit_report dict。

    Phase 48-G: agent_id 参数穿透——修复原硬编码 "hermes" 违反铁律 #1。
    """
    await self._push(push_callback, task_id, "spec_phase", {
        "phase": "auditor", "round": round_num,
    })
    try:
        audit_report = await asyncio.wait_for(
            self._invoke_spec_auditor(
                spec_yaml=spec_yaml,
                spec_dict=spec_dict,
                developer_output=developer_output,
                configured_providers=configured_providers,
                round_num=round_num,
                task_id=task_id,
                agent_id=agent_id,  # Phase 48-G: 穿透
                language=language,
            ),
            timeout=DELIBERATION_ROLE_TIMEOUT * 2,  # 双通道=自然语言+验证
        )
        await self._push(push_callback, task_id, "spec_phase", {
            "phase": "auditor_done", "round": round_num,
            "matches": audit_report.get("matches", 0),
            "mismatches": audit_report.get("mismatches", 0),
            "missing": audit_report.get("missing", 0),
            "verification_passed": audit_report.get("verification_passed", False),
            "mismatches_list": audit_report.get("mismatches_list", [])[:DELIBERATION_AUDIT_LIST_MAX_ITEMS],
            "missing_list": audit_report.get("missing_list", [])[:DELIBERATION_AUDIT_LIST_MAX_ITEMS],
            "verification_results": audit_report.get("verification_results", [])[:DELIBERATION_AUDIT_VERIFICATION_RESULTS_MAX_ITEMS],
        })
    except asyncio.TimeoutError:
        _log.error("Auditor timeout round %d", round_num)
        audit_report = {"error": "Auditor 超时", "matches": 0,
                       "mismatches": 1, "missing": 0}
    return audit_report


async def _run_developer_phase(
    self, spec_yaml, spec_dict, configured_providers,
    round_num, task_id, push_callback,
    agent_id: str = DEFAULT_AGENT_ID,  # Phase 48-G: 铁律 #1 穿透
    language: str = "zh",
):
    """阶段 2: Developer 按 Spec 实现——返回 (developer_output, dev_files)。

    Phase 48-G: agent_id 参数穿透——修复原硬编码 "hermes" 违反铁律 #1。
    """
    await self._push(push_callback, task_id, "spec_phase", {
        "phase": "developer", "round": round_num,
    })
    developer_output = ""
    dev_files: list = []
    try:
        dev_result = await asyncio.wait_for(
            self._invoke_spec_developer(
                spec_yaml=spec_yaml,
                spec_dict=spec_dict,
                configured_providers=configured_providers,
                round_num=round_num,
                task_id=task_id,
                agent_id=agent_id,  # Phase 48-G: 穿透
                language=language,
            ),
            timeout=DELIBERATION_ROLE_TIMEOUT * 3,  # 3轮工具循环，放宽
        )
        developer_output = dev_result.get("output", "")
        dev_files = dev_result.get("files_touched", [])
        # Phase 45 D.6: Gate 违规检查 — Developer 篡改Gate→本轮无效
        if not dev_result.get("gate_compliant", True):
            _log.warning("Gate violations for round %d: %s", round_num,
                       dev_result.get("gate_violations", []))
            developer_output = (
                f"[Gate违规] Developer 实现被拒绝——"
                f"{'; '.join(dev_result.get('gate_violations', []))}"
            )
        await self._push(push_callback, task_id, "spec_phase", {
            "phase": "developer_done", "round": round_num,
            "files_count": len(dev_files),
            "files": dev_files[:DELIBERATION_SPEC_FILES_PREVIEW_LIMIT],
        })
    except asyncio.TimeoutError:
        _log.error("Developer timeout round %d", round_num)
        developer_output = "[Developer 超时——工具循环未在限定时间内完成]"
    return developer_output, dev_files


async def _run_architect_phase(
    self, task_input, project_structure, configured_providers,
    round_num, previous_audit, push_callback, task_id,
    agent_id: str = DEFAULT_AGENT_ID,  # Phase 48-G: 铁律 #1 穿透
    language: str = "zh",
):
    """阶段 1: Architect 起草/修正 Spec。

    Returns: dict — {"ok": True, "spec_yaml", "spec_dict", "architect_output"}
                    或 {"ok": False, "error": msg}（失败/超时）。

    Phase 48-G: agent_id 参数穿透——修复原硬编码 "hermes" 违反铁律 #1。
    """
    await self._push(push_callback, task_id, "spec_phase", {
        "phase": "architect", "round": round_num,
    })
    try:
        spec_result = await asyncio.wait_for(
            self._invoke_spec_architect(
                task_input=task_input,
                project_structure=project_structure,
                configured_providers=configured_providers,
                round_num=round_num,
                previous_audit=previous_audit,
                agent_id=agent_id,  # Phase 48-G: 穿透
                language=language,
            ),
            timeout=DELIBERATION_ROLE_TIMEOUT,
        )
        if not spec_result.get("ok"):
            error_msg = spec_result.get("error", "Architect 未能生成有效 Spec")
            _log.error("Architect failed round %d: %s", round_num, error_msg)
            await self._push(push_callback, task_id, "spec_phase", {
                "phase": "architect_failed", "round": round_num,
                "error": error_msg,
            })
            return {"ok": False, "error": error_msg}
        spec_yaml = spec_result["yaml"]
        spec_dict = spec_result["spec"]
        architect_output = spec_result.get("raw_output", "")
        await self._push(push_callback, task_id, "spec_phase", {
            "phase": "architect_done", "round": round_num,
            "spec_name": spec_dict.get("spec", {}).get("name", ""),
            "fr_count": spec_result.get("index", {}).get("fr_count", 0),
        })
        return {"ok": True, "spec_yaml": spec_yaml, "spec_dict": spec_dict,
                "architect_output": architect_output}
    except asyncio.TimeoutError:
        _log.error("Architect timeout round %d", round_num)
        return {"ok": False, "error": "Architect 超时——模型响应过慢"}


# ═══════════════════════════════════════════════════════════
# Phase 48 T3-5: 自动修复回环子函数（从 run_spec_loop 拆分）
# ═══════════════════════════════════════════════════════════


def _build_auto_fix_prompt(spec_yaml: str, audit_report: dict) -> str:
    """构造自动修复 prompt — 含完整的失败上下文。"""
    _fix_lines: list[str] = []
    _fix_lines.append("## 验证失败，请修复以下问题\n")
    _fix_lines.append("### Spec 要求\n```yaml\n" + (spec_yaml or "") + "\n```\n")

    _vr = audit_report.get("verification_results", [])
    _failed_vr = [r for r in _vr if not r.get("passed", True)]
    if _failed_vr:
        _fix_lines.append("### 验证失败\n")
        for r in _failed_vr:
            _fix_lines.append(
                f"- [FAIL] `{r.get('command', '?')}`\n"
                f"  输出: {str(r.get('output', ''))[:DELIBERATION_SPEC_VERIFY_OUTPUT_PREVIEW_CHARS]}"
            )

    _ml = audit_report.get("mismatches_list", [])
    if _ml:
        _fix_lines.append("\n### 实现与 Spec 不一致 (MISMATCH)\n")
        for m in _ml[:DELIBERATION_SPEC_AUTO_FIX_ITEMS_LIMIT]:
            _fix_lines.append(f"- [MISMATCH] {m}")

    _missing = audit_report.get("missing_list", [])
    if _missing:
        _fix_lines.append("\n### 未实现 (MISSING)\n")
        for m in _missing[:DELIBERATION_SPEC_AUTO_FIX_ITEMS_LIMIT]:
            _fix_lines.append(f"- [MISSING] {m}")

    _fix_lines.append("\n请用工具修复上述所有问题。**只修改实现代码，不要改 Spec。**")
    return "\n".join(_fix_lines)


async def _try_auto_fix_once(
    self, task_id, spec_yaml, spec_dict, audit_report,
    configured_providers, round_num, push_callback, fix_attempt,
    agent_id: str = DEFAULT_AGENT_ID,  # Phase 48-G: 铁律 #1 穿透
) -> dict | None:
    """单次自动修复尝试——Developer 修复 + 重新验证。

    Returns: dict — {"audit_report", "developer_output", "token_cost"} 或 None（失败/超时）。

    Phase 48-G: agent_id 参数穿透——修复原硬编码 "hermes" 违反铁律 #1。
    """
    fix_prompt = _build_auto_fix_prompt(spec_yaml, audit_report)
    attempt_num = fix_attempt + 1

    # Developer 执行修复 (有工具循环，override_prompt 激活)
    try:
        fix_result = await asyncio.wait_for(
            self._invoke_spec_developer(
                spec_yaml=spec_yaml,
                spec_dict=spec_dict,
                configured_providers=configured_providers,
                round_num=round_num,
                task_id=f"{task_id}_fix{fix_attempt}",
                override_prompt=fix_prompt,
                agent_id=agent_id,  # Phase 48-G: 穿透
            ),
            timeout=DELIBERATION_ROLE_TIMEOUT * 2,
        )
        fix_output = fix_result.get("output", "")
        from config import count_tokens
        prompt_counted = await count_tokens(fix_prompt)
        output_counted = await count_tokens(fix_output)
        token_cost = prompt_counted["tokens"] + output_counted["tokens"]
        _log.info("Auto-fix attempt %d completed (%d chars output)", attempt_num, len(fix_output))
    except asyncio.TimeoutError:
        _log.warning("Auto-fix attempt %d timed out", attempt_num)
        return None
    except Exception as e:
        _log.error("Auto-fix attempt %d failed: %s", attempt_num, e)
        return None

    # 重新验证
    await self._push(push_callback, task_id, "spec_phase", {
        "phase": "auto_fix_reverify", "round": round_num, "fix_attempt": attempt_num,
    })
    try:
        new_audit = await asyncio.wait_for(
            self._invoke_spec_auditor(
                spec_yaml=spec_yaml, spec_dict=spec_dict, developer_output=fix_output,
                configured_providers=configured_providers, round_num=round_num, task_id=task_id,
                agent_id=agent_id,  # Phase 48-G: 穿透
            ),
            timeout=DELIBERATION_ROLE_TIMEOUT * 2,
        )
    except Exception:
        _log.warning("Auto-fix re-verification failed for attempt %d", attempt_num)
        return None

    await self._push(push_callback, task_id, "spec_phase", {
        "phase": "auto_fix_result", "round": round_num, "fix_attempt": attempt_num,
        "mismatches_after": new_audit.get("mismatches", 0),
        "missing_after": new_audit.get("missing", 0),
        "verification_passed_after": new_audit.get("verification_passed", False),
    })
    return {"audit_report": new_audit, "developer_output": fix_output, "token_cost": token_cost}


async def _run_auto_fix_round(
    self, task_id, spec_yaml, spec_dict, audit_report,
    configured_providers, round_num, push_callback,
    developer_output, mismatches, missing, verification_passed,
    agent_id: str = DEFAULT_AGENT_ID,  # Phase 48-G: 铁律 #1 穿透
) -> dict:
    """阶段 3.5: 自动修复回环（最多 3 次）。对标 OpenAI Codex。

    Returns: dict — {"converged", "audit_report", "developer_output", "attempts", "token_cost"}

    Phase 48-G: agent_id 参数穿透——修复原硬编码 "hermes" 违反铁律 #1。
    """
    await self._push(push_callback, task_id, "spec_phase", {
        "phase": "auto_fix", "round": round_num,
        "mismatches_before": mismatches, "missing_before": missing,
        "verification_passed_before": verification_passed,
    })

    auto_fix_attempts = 0
    auto_fix_token_cost = 0

    from config import MAX_SPEC_FIX_ATTEMPTS
    for fix_attempt in range(MAX_SPEC_FIX_ATTEMPTS):
        auto_fix_attempts = fix_attempt + 1
        _log.info("Spec auto-fix attempt %d/3 (round %d)", auto_fix_attempts, round_num)

        result = await _try_auto_fix_once(
            self, task_id, spec_yaml, spec_dict, audit_report,
            configured_providers, round_num, push_callback, fix_attempt,
            agent_id=agent_id,  # Phase 48-G: 穿透
        )
        if result is None:
            continue

        auto_fix_token_cost += result["token_cost"]
        new_audit = result["audit_report"]
        new_mismatches = new_audit.get("mismatches", 0)
        new_missing = new_audit.get("missing", 0)
        new_verification = new_audit.get("verification_passed", False)

        if new_verification and new_mismatches == 0 and new_missing == 0:
            _log.info("Auto-fix CONVERGED after %d attempt(s) (round %d)", auto_fix_attempts, round_num)
            self._delete_checkpoint(task_id)
            return {"converged": True, "audit_report": new_audit,
                    "developer_output": result["developer_output"],
                    "attempts": auto_fix_attempts, "token_cost": auto_fix_token_cost}

        _log.info("Auto-fix attempt %d: still %d mismatches, %d missing, verification=%s",
                 auto_fix_attempts, new_mismatches, new_missing, new_verification)

    _log.warning("Auto-fix exhausted (%d attempts) for round %d, continuing to next round",
                auto_fix_attempts, round_num)
    return {"converged": False, "audit_report": audit_report,
            "developer_output": developer_output,
            "attempts": auto_fix_attempts, "token_cost": auto_fix_token_cost}


# ═══════════════════════════════════════════════════════════
# D.1: run_spec_loop() — 主循环
# ═══════════════════════════════════════════════════════════

async def run_spec_loop(
    self,
    task_input: str,
    task_id: str,
    push_callback=None,
    max_rounds: int = 10,  # Phase 48-H: 3→10 Spec 收敛循环放宽对标主流
    mode: str = "fixed",
    goal_condition=None,
    preferred_backend: str = "",  # Phase: SpecOrchestrator 按内容路由
    agent_id: str = DEFAULT_AGENT_ID,  # Phase 48-G: 铁律 #1 穿透——OC 后端任务以 openclaw 身份调工具
    language: str = "zh",
) -> dict:
    """Spec 驱动开发主循环——三阶段收敛。

    Args:
        task_input: 用户的功能需求描述
        task_id: 任务 ID（用于 WS 推送和检查点持久化）
        push_callback: async fn(task_id, msg_type, payload) — WS 进度推送
        max_rounds: 最大收敛轮次（Phase 48-H: 默认 10；mode="goal" 时按 max_rounds*10 放宽，无 20 硬上限）
        mode: "fixed"=固定轮次上限; "goal"=持续运行直到 goal_condition 成立
        goal_condition: fn(audit) → bool | str — 仅 mode="goal" 时使用。
            fn(audit)→bool 或 内置条件名: "all_tests_pass" | "mismatch_zero" | "verification_only"

    Returns:
        {converged, rounds, spec_yaml, implementation, audit_report,
         architect_output, developer_output, escalated, checkpoint_resumed}

    对标 OpenAI Ghost Library:
      - Architect: Spec 起草者——只看需求和项目结构
      - Developer: 构建者——只看 Spec，从零实现到验证通过
      - Reviewer:  审计者——不同模型审查（N≥2 时）；N≥4 时轮换
    """
    if not self.agent_service:
        return {"error": "agent_service not configured", "converged": False}
    if not self.agent_service.adapters:
        return {"error": "no agents available", "converged": False}

    # ── 启动: Provider 检测 ──
    configured_providers: list[str] = []
    try:
        all_providers = await self.agent_service._model_config.list_providers()
        configured_providers = [
            p.get("id", "") for p in all_providers
            if p.get("api_key_configured") or p.get("active")
        ]
    except Exception:
        configured_providers = ["deepseek"]  # fallback

    if not configured_providers:
        return {
            "error": "未配置任何模型 Provider——请在设置 > 模型配置中填写 API Key",
            "converged": False,
        }

    # ── 启动: 检查点恢复 (Phase 44 C.2) ──
    checkpoint = self._load_checkpoint(task_id)
    start_round = 1
    spec_yaml = None
    architect_output = ""
    developer_output = ""
    if checkpoint:
        spec_yaml = checkpoint.get("spec_yaml")
        start_round = checkpoint.get("round", 0) + 1
        architect_output = checkpoint.get("architect_output", "")
        developer_output = checkpoint.get("developer_output", "")
        _log.info("Resumed Spec loop from round %d (checkpoint: %s)",
                 start_round, task_id[:12])
        await self._push(push_callback, task_id, "spec_phase", {
            "phase": "checkpoint_resumed", "round": start_round,
        })

    # ── mode="goal" 放宽轮次上限 + 内置条件解析 (Phase 45 D.2 / Phase 48-H) ──
    # Phase 48-H: 删除 min(...,20) 人为上限——goal 模式持续运行直到 goal_condition 成立，
    # 安全网由 max_rounds*10 提供对标主流（Claude Code/OpenAI Ghost 无硬上限收敛）。
    effective_max_rounds = max_rounds if mode == "fixed" else max_rounds * 10

    # Phase 45 D.2: 内置 goal condition 名称解析
    if mode == "goal" and isinstance(goal_condition, str):
        _original_name = goal_condition
        _condition_map = {
            "all_tests_pass": self.goal_all_tests_pass,
            "mismatch_zero": self.goal_mismatch_zero,
            "verification_only": self.goal_verification_only,
        }
        goal_condition = _condition_map.get(_original_name)
        if not goal_condition:
            _log.warning("Unknown goal condition '%s', defaulting to mismatch_zero",
                       _original_name)
            goal_condition = self.goal_mismatch_zero
    elif mode == "goal" and goal_condition is None:
        goal_condition = self.goal_all_tests_pass  # 默认目标: 全通过+零错误

    audit_report: dict = {}
    converged = False
    round_num = start_round - 1

    # ── 自动修复状态 ──
    auto_fix_converged = False
    auto_fix_attempts = 0

    # ── 阶段 0: 获取项目结构（仅 Round 1） ──
    project_structure = self._get_project_structure() if start_round == 1 else ""

    # ========================================================
    # 主循环
    # ========================================================
    while round_num < effective_max_rounds:
        round_num += 1
        _log.info("Spec loop round %d/%d (mode=%s)", round_num, effective_max_rounds, mode)

        await self._push(push_callback, task_id, "spec_phase", {
            "phase": "round_start", "round": round_num,
            "max_rounds": effective_max_rounds, "mode": mode,
        })

        # ── 阶段 1: Architect 起草/修正 Spec ──
        # Phase 48 T3-5: 内联代码已拆分到 _run_architect_phase
        previous_audit = audit_report if round_num > 1 and not converged else None
        _arch = await _run_architect_phase(
            self, task_input, project_structure, configured_providers,
            round_num, previous_audit, push_callback, task_id,
            agent_id=agent_id,  # Phase 48-G: 穿透
            language=language,
        )
        if not _arch.get("ok"):
            if round_num == 1:
                return {"error": _arch["error"], "converged": False}
            # 非首轮失败 → 继续用上一轮的 Spec
            continue
        spec_yaml = _arch["spec_yaml"]
        spec_dict = _arch["spec_dict"]
        architect_output = _arch["architect_output"]

        # ── 阶段 2: Developer 按 Spec 实现 ──
        # Phase 48 T3-5: 内联代码已拆分到 _run_developer_phase
        developer_output, dev_files = await _run_developer_phase(
            self, spec_yaml, spec_dict, configured_providers,
            round_num, task_id, push_callback,
            agent_id=agent_id,  # Phase 48-G: 穿透
            language=language,
        )

        # ── 阶段 3: Reviewer 双通道审计 ──
        # Phase 48 T3-5: 内联代码已拆分到 _run_auditor_phase
        audit_report = await _run_auditor_phase(
            self, spec_yaml, spec_dict, developer_output, configured_providers,
            round_num, task_id, push_callback,
            agent_id=agent_id,  # Phase 48-G: 穿透
            language=language,
        )

        # ── 收敛判定 ──
        mismatches = audit_report.get("mismatches", 0)
        missing = audit_report.get("missing", 0)
        verification_passed = audit_report.get("verification_passed", True)
        total_issues = mismatches + missing

        if mode == "goal" and goal_condition:
            converged = goal_condition(audit_report)
        else:
            converged = (total_issues == 0 and verification_passed)

        await self._push(push_callback, task_id, "spec_phase", {
            "phase": "convergence_check", "round": round_num,
            "converged": converged, "mismatches": mismatches,
            "missing": missing, "verification_passed": verification_passed,
        })

        if converged:
            _log.info("Spec loop CONVERGED after %d rounds", round_num)
            self._delete_checkpoint(task_id)

            # Phase 46 E.1: Ghost Library — 收敛后运行可移植性验证
            try:
                _ghost_result = await self.ghost_library_verify(
                    spec_yaml=spec_yaml,
                    original_impl={"output": developer_output,
                                   "files_touched": dev_files},
                    configured_providers=configured_providers,
                    push_callback=push_callback,
                    agent_id=agent_id,  # Phase 48-G: 穿透
                )
                await self._push(push_callback, task_id, "spec_phase", {
                    "phase": "ghost_done",
                    "ghost_verified": _ghost_result.get("ghost_verified", False),
                    "gaps": len(_ghost_result.get("gaps", [])),
                })
            except Exception:
                _log.debug("Ghost verification skipped for %s", task_id[:12])

            break

        # ═══════════════════════════════════════════════════════════
        # Phase 44 C.1: 阶段 3.5 — 自动修复回环
        # Phase 48 T3-5: 内联代码已拆分到 _run_auto_fix_round
        #
        # 对标 OpenAI Codex: exit code ≠ 0 → 回循环。
        # 验证失败或存在 MISMATCH → Developer 自动修复 → 重新验证。
        # 最多 3 次修复尝试，全部失败 → 继续下一轮 Spec 循环。
        # ═══════════════════════════════════════════════════════════
        _auto_fix = await _run_auto_fix_round(
            self, task_id, spec_yaml, spec_dict, audit_report,
            configured_providers, round_num, push_callback,
            developer_output, mismatches, missing, verification_passed,
            agent_id=agent_id,  # Phase 48-G: 穿透
        )
        auto_fix_attempts = _auto_fix["attempts"]
        if _auto_fix["converged"]:
            # 用修复后的结果更新审计报告
            audit_report = _auto_fix["audit_report"]
            developer_output = _auto_fix["developer_output"]
            converged = True
            auto_fix_converged = True
            # 记录 token 成本到 analytics_events
            try:
                db = getattr(self.agent_service, 'db', None)
                if db:
                    await db.execute(
                        "INSERT INTO analytics_events(event_type, event_data, created_at) "
                        "VALUES(?,?,?)",
                        ("spec_auto_fix",
                         _json.dumps({
                             "task_id": task_id,
                             "round": round_num,
                             "attempts": _auto_fix["attempts"],
                             "estimated_tokens": _auto_fix["token_cost"],
                         }),
                         int(_time.time())),
                    )
                    await db.commit()
            except Exception:
                _log.debug("写入自动修复知识节点失败", exc_info=True)
            break  # 跳出主循环

        # ── 检查点持久化（未收敛时，含自动修复上下文） ──
        self._save_checkpoint(task_id, {
            "spec_yaml": spec_yaml,
            "spec_dict": spec_dict,
            "architect_output": architect_output,
            "developer_output": developer_output,
            "audit_report": audit_report,
            "round": round_num,
            "task_input": task_input,
            "mode": mode,
            "auto_fix_attempts": auto_fix_attempts,
        })

    # ========================================================
    # 循环结束——构造返回结果
    # ========================================================
    if not converged and round_num >= effective_max_rounds:
        # 未收敛 → 升级通知写入 knowledge_nodes（含自动修复历史）
        await self._escalate_spec_failure(
            task_id, spec_yaml, audit_report, round_num, push_callback,
            auto_fix_attempts=auto_fix_attempts,
        )
        await self._push(push_callback, task_id, "spec_phase", {
            "phase": "escalated", "round": round_num,
            "remaining_issues": audit_report.get("mismatches", 0) + audit_report.get("missing", 0),
        })

    await self._push(push_callback, task_id, "spec_loop_done", {
        "converged": converged,
        "rounds": round_num,
        "spec_name": (spec_dict or {}).get("spec", {}).get("name", "") if spec_dict else "",
        "auto_fix_attempts": auto_fix_attempts,
        "auto_fix_converged": auto_fix_converged,
        "audit_summary": {
            "matches": audit_report.get("matches", 0),
            "mismatches": audit_report.get("mismatches", 0),
            "missing": audit_report.get("missing", 0),
            "verification_passed": audit_report.get("verification_passed", False),
            "mismatches_list": audit_report.get("mismatches_list", [])[:DELIBERATION_AUDIT_LIST_MAX_ITEMS],
            "missing_list": audit_report.get("missing_list", [])[:DELIBERATION_AUDIT_LIST_MAX_ITEMS],
            "verification_results": audit_report.get("verification_results", [])[:DELIBERATION_AUDIT_VERIFICATION_RESULTS_MAX_ITEMS],
        },
    })

    return {
        "converged": converged,
        "rounds": round_num,
        "spec_yaml": spec_yaml,
        "spec_dict": spec_dict,
        "architect_output": architect_output,
        "developer_output": developer_output,
        "audit_report": audit_report,
        "auto_fix_attempts": auto_fix_attempts,
        "auto_fix_converged": auto_fix_converged,
        "checkpoint_resumed": checkpoint is not None,
        "mode": mode,
        "providers_used": len(configured_providers),
    }
