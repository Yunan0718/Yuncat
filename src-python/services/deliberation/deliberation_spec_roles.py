"""services/deliberation_spec_roles.py —— Spec 三阶段角色调用。

从 deliberation_service.py 拆分而来，保留全部功能。
模块级函数以 self 为首参，由 DeliberationService 薄包装委托调用。
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import os as _os
import re
import time as _time

from config import (
    adaptive_truncate, calc_context_budget, calc_list_limit,
    DELIBERATION_CMD_TIMEOUT, DEFAULT_AGENT_ID, OPENCLAW_AGENT_ID,
    SPEC_ROLE_REVIEW_BUDGET_CHARS,
    DELIBERATION_SPEC_GATE_VIOLATIONS_MAX,
    DELIBERATION_SPEC_ESCALATION_ISSUES_LIMIT,
)

_log = logging.getLogger("yuncat.deliberation")


async def _get_model_cfg(self) -> dict:
    """获取当前模型配置，失败时返回空 dict。"""
    try:
        if self.model_config:
            return await self.model_config.get_active_config()
    except Exception:
        _log.debug("获取模型配置失败", exc_info=True)
    return {}


def _calc_spec_budgets(model_cfg: dict) -> dict[str, int]:
    """Phase 56-P2: Spec 流程各阶段预算按模型窗口动态计算。"""
    return {
        "mismatch_items": calc_list_limit(model_cfg, 10, fraction_per_100k=1, max_limit=20),
        "missing_items": calc_list_limit(model_cfg, 10, fraction_per_100k=1, max_limit=20),
        "error_items": calc_list_limit(model_cfg, 3, fraction_per_100k=1, max_limit=8),
        "dev_review": calc_context_budget(model_cfg, fraction=0.02, min_chars=1500, max_chars=6000),
        "file_list": calc_list_limit(model_cfg, 15, fraction_per_100k=2, max_limit=25),
        "read_files": calc_list_limit(model_cfg, 8, fraction_per_100k=2, max_limit=16),
        "small_file": calc_context_budget(model_cfg, fraction=0.006, min_chars=600, max_chars=2500),
        "large_file_head": calc_context_budget(model_cfg, fraction=0.004, min_chars=400, max_chars=1500),
        "large_file_tail": calc_context_budget(model_cfg, fraction=0.004, min_chars=400, max_chars=1500),
        "verify_output": calc_context_budget(model_cfg, fraction=0.004, min_chars=400, max_chars=1200),
        "oc_audit": calc_context_budget(model_cfg, fraction=0.006, min_chars=600, max_chars=2500),
        "spec_for_oc": calc_context_budget(model_cfg, fraction=0.02, min_chars=1500, max_chars=6000),
    }


# ═══════════════════════════════════════════════════════════
# D.2: _invoke_spec_architect() — 起草 Spec
# ═══════════════════════════════════════════════════════════

async def _invoke_spec_architect(
    self, task_input: str, project_structure: str,
    configured_providers: list[str], round_num: int,
    previous_audit: dict | None = None,
    agent_id: str = DEFAULT_AGENT_ID,  # Phase 48-G: 铁律 #1 穿透
    language: str = "zh",
) -> dict:
    """阶段 1: Architect 起草/修正结构化 YAML Spec。

    返回: {ok, spec, yaml, raw_output, index, error}

    Phase 48-G: agent_id 参数穿透——修复原硬编码 "hermes" 违反铁律 #1。
    """
    from prompts.spec_prompts import render_architect_prompt
    from services.spec_parser import parse_spec_yaml, format_validation_errors

    # 解析 Provider 路由
    provider_cfg = await self._resolve_spec_provider_config(
        "architect", round_num, configured_providers,
    )
    if not provider_cfg:
        return {"ok": False, "error": "Architect 角色没有可用的模型配置"}

    # Phase 56-P2: 按当前模型窗口动态计算预算
    model_cfg = await _get_model_cfg(self)
    budgets = _calc_spec_budgets(model_cfg)

    # 构建 Prompt——含上一轮审计发现（修正轮次）
    full_input = task_input
    if previous_audit:
        mismatches = previous_audit.get("mismatches_list", [])
        missing = previous_audit.get("missing_list", [])
        if mismatches or missing:
            correction_context = (
                "\n\n## ⚠️ 上一轮审计发现——请修正 Spec\n\n"
            )
            if mismatches:
                correction_context += "### 实现不一致 (MISMATCH)\n" + "\n".join(
                    f"- {m}" for m in mismatches[:budgets["mismatch_items"]]
                ) + "\n"
            if missing:
                correction_context += "### 未实现 (MISSING)\n" + "\n".join(
                    f"- {m}" for m in missing[:budgets["missing_items"]]
                ) + "\n"
            correction_context += (
                "\n请修正 Spec 使上述不一致项对齐。"
                "如果实现是正确的，更新 Spec 的描述使其更精确。"
            )
            full_input += correction_context

    prompt = render_architect_prompt(full_input, project_structure)

    # 调用 Architect（Flash 模型，只需文本输出）
    try:
        raw_output = await self.agent_service._invoke_with_role(
            agent_id, prompt, task_input, model_key="secondary",
            provider_config=provider_cfg, language=language,
        )
    except Exception as e:
        return {"ok": False, "error": f"Architect 调用失败: {e}"}

    if not raw_output or not raw_output.strip():
        return {"ok": False, "error": "Architect 返回空响应"}

    # 解析 YAML
    result = parse_spec_yaml(raw_output, strict=False)

    if not result["ok"]:
        # 第 1 次失败——给 Architect 一次修正机会（含具体错误）
        errors_text = format_validation_errors(result["errors"])
        retry_prompt = (
            f"{prompt}\n\n"
            f"## ⚠️ 你的上一次输出有结构问题——请修正后重新生成\n\n"
            f"{errors_text}\n\n"
            f"请只输出修正后的 YAML 块。"
        )
        try:
            raw_output2 = await self.agent_service._invoke_with_role(
                agent_id, retry_prompt, task_input, model_key="secondary",
                provider_config=provider_cfg, language=language,
            )
            result2 = parse_spec_yaml(raw_output2, strict=False)
            if result2["ok"]:
                result2["raw_output"] = raw_output2
                result2["retried"] = True
                return result2
        except Exception:
            _log.debug("Architect 重试生成 Spec 失败", exc_info=True)
        # 两次都失败
        return {
            "ok": False,
            "error": f"Architect 两次尝试均未生成有效 Spec: {result['errors'][:budgets['error_items']]}",
            "raw_output": raw_output,
        }

    result["raw_output"] = raw_output
    return result


# ═══════════════════════════════════════════════════════════
# D.3: _invoke_spec_developer() — 按 Spec 实现
# ═══════════════════════════════════════════════════════════

async def _invoke_spec_developer(
    self, spec_yaml: str, spec_dict: dict,
    configured_providers: list[str], round_num: int,
    task_id: str, override_prompt: str | None = None,
    agent_id: str = DEFAULT_AGENT_ID,  # Phase 48-G: 铁律 #1 穿透
    language: str = "zh",
) -> dict:
    """阶段 2: Developer 只看 Spec 实现代码——有 3 轮工具循环。

    Phase 44 C.1 预留: override_prompt 参数——自动修复模式时替代默认 Prompt。

    返回: {output, files_touched, error}

    Phase 48-G: agent_id 参数穿透——修复原硬编码 "hermes" 违反铁律 #1。
    """
    from prompts.spec_prompts import render_developer_prompt

    provider_cfg = await self._resolve_spec_provider_config(
        "developer", round_num, configured_providers,
    )
    if not provider_cfg:
        return {"output": "", "files_touched": [], "error": "Developer 角色没有可用的模型配置"}

    prompt = render_developer_prompt(spec_yaml, override_prompt=override_prompt)

    # Phase 44 C.8: Worktree 工作隔离 — Developer 在独立 git worktree 中实现
    _wt = None
    _wt_info = None
    try:
        import sys as _sys_mod
        for _mn, _mod in _sys_mod.modules.items():
            if hasattr(_mod, "app") and hasattr(_mod.app, "state"):
                _wt = getattr(_mod.app.state, "worktree_service", None)
                if _wt:
                    break
    except Exception:
        _log.debug("查找 worktree_service 失败", exc_info=True)

    # Phase 45 D.6: Gate 模式 — Spec YAML 写入 worktree 作为不可变 Gate
    _gate = None
    if _wt:
        try:
            _wt_info = await _wt.create_worktree(task_id)
            if _wt_info:
                _log.info("Developer working in isolated worktree: %s", _wt_info.path)
                # Phase 45 D.6: 写入 Gate 文件
                from services.gate_manager import GateManager
                _gate = GateManager(spec_dict, str(_wt_info.path))
        except Exception:
            _log.debug("Worktree or Gate creation failed, falling back to main workspace", exc_info=True)
            _wt = None
            _gate = None

    # Developer 走 _invoke_with_role_and_tools——有工具循环，clean context
    dev_task_id = f"{task_id}_dev_r{round_num}"
    try:
        output = await self.agent_service._invoke_with_role_and_tools(
            agent_id=agent_id,
            role_prompt=prompt,
            task_input="实现上述 Spec。严格按 Spec 的 files_to_create/modify 路径创建文件。",
            model_key="primary",  # Pro tier
            max_tool_rounds=30,  # Phase 48-H: 3→30 对标主流无低上限
            task_id=dev_task_id,
            provider_config=provider_cfg,
            language=language,
        )
    except Exception as e:
        output = ""
        _log.error("Developer invocation failed: %s", e)
    finally:
        # Phase 44 C.8: 清理 worktree（如果创建了）
        if _wt and _wt_info:
            try:
                await _wt.merge_worktree(task_id, strategy="discard")
            except Exception:
                _log.debug("Worktree cleanup failed for %s", task_id[:12])

    if not output:
        return {"output": "", "files_touched": [], "error": "Developer 调用失败——无输出"}

    # 收集实现文件清单——从 Spec 的 files_to_create/modify 检测实际文件
    files_touched: list[str] = []
    inner = spec_dict.get("spec", {}) if isinstance(spec_dict, dict) else {}
    for field in ("files_to_create", "files_to_modify"):
        fpaths = inner.get(field, [])
        if isinstance(fpaths, list):
            for fp in fpaths:
                if fp and fp not in files_touched:
                    files_touched.append(fp)

    # Phase 43 D.3 增强: 文件系统验证——检查 Spec 列出的文件是否实际存在
    try:
        from security.sandbox import get_workspace
        ws = str(get_workspace())
        for fp in list(files_touched):
            actual_path = _os.path.join(ws, fp.lstrip("/\\"))
            if not _os.path.exists(actual_path):
                files_touched.remove(fp)
                _log.debug("Developer file not found on disk: %s", fp)
    except Exception:
        _log.debug("文件系统不可达，退回到 Spec dict", exc_info=True)

    # Phase 45 D.6: Gate 合规检查——Developer 是否篡改了 Gate 或受保护文件
    _gate_violations: list[str] = []
    if _gate:
        _compliance = _gate.check_builder_compliance(files_touched)
        if not _compliance["compliant"]:
            _gate_violations = _compliance["violations"]
            _log.warning("Gate violations for task %s: %s", task_id[:12], _gate_violations)

    return {
        "output": output,
        "files_touched": files_touched,
        "error": None if not _gate_violations else f"Gate违规: {'; '.join(_gate_violations[:DELIBERATION_SPEC_GATE_VIOLATIONS_MAX])}",
        "gate_compliant": len(_gate_violations) == 0,
        "gate_violations": _gate_violations,
    }


# ═══════════════════════════════════════════════════════════
# D.4: _invoke_spec_auditor() — 双通道审计
# ═══════════════════════════════════════════════════════════

async def _invoke_spec_auditor(
    self, spec_yaml: str, spec_dict: dict,
    developer_output: str, configured_providers: list[str],
    round_num: int, task_id: str,
    agent_id: str = DEFAULT_AGENT_ID,  # Phase 48-G: 铁律 #1 穿透
    language: str = "zh",
) -> dict:
    """阶段 3: Reviewer 双通道审计。

    通道 A: 自然语言审查——LLM 对比 Spec vs 实现，输出 [MATCH]/[MISMATCH]/[MISSING]。
    通道 B: 自动验证——实际运行 Spec 中的 verification.commands，输出 [PASS]/[FAIL]。

    返回: {matches, mismatches, missing, mismatches_list, missing_list,
           verification_passed, verification_results, raw_audit, auditor_model}

    Phase 48-G: agent_id 参数穿透——修复原硬编码 "hermes" 违反铁律 #1。
    """
    from prompts.spec_prompts import render_auditor_prompt

    # ── 通道 A: 自然语言审查 ──
    # G1.4: 传入 available_agents 启用跨后端审查——reviewer 路由到 openclaw 时
    # provider_cfg 含 _reviewer_agent_id，auditor 据此切换调用 agent。
    _available_agents = list(self.agent_service.adapters.keys()) if self.agent_service else []
    provider_cfg = await self._resolve_spec_provider_config(
        "reviewer", round_num, configured_providers,
        available_agents=_available_agents,
    )
    if not provider_cfg:
        return {"error": "Reviewer 角色没有可用的模型配置",
               "matches": 0, "mismatches": 1, "missing": 0}

    # G1.4: 跨后端审查——reviewer 切换到 openclaw agent（独立后端视角）
    _reviewer_agent_id = provider_cfg.get("_reviewer_agent_id") or agent_id
    _cross_backend = provider_cfg.get("_reviewer_agent_id") is not None

    # Phase 56-P2: 按当前模型窗口动态计算预算
    model_cfg = await _get_model_cfg(self)
    budgets = _calc_spec_budgets(model_cfg)

    # 构建实现摘要——Developer 的输出 + 真实文件内容
    inner = spec_dict.get("spec", {}) if isinstance(spec_dict, dict) else {}
    files_list = []
    for field in ("files_to_create", "files_to_modify"):
        fpaths = inner.get(field, [])
        if isinstance(fpaths, list):
            files_list.extend(fpaths)

    # Phase 43 P0-DYN / Phase 56-P2: 自适应截断 Developer 输出（按模型窗口动态）
    from config import MAX_CONTEXT_FALLBACK_CHARS
    _dev_summary = adaptive_truncate(developer_output, MAX_CONTEXT_FALLBACK_CHARS, budgets["dev_review"], 0.6)
    implementation_summary = (
        f"### Developer 输出（摘要）\n{_dev_summary}\n\n"
        f"### 预期创建/修改的文件\n" + "\n".join(f"- `{f}`" for f in files_list[:budgets["file_list"]])
    )

    # Phase 43 Stage D 关键增强: 预读实现文件→注入到 Auditor Prompt
    # Auditor 当前走 _invoke_with_role（无工具循环），不能自己 read_file。
    # 但 Auditor 需要看实际代码才能做逐字段 schema 检查和逐行 constraint 验证。
    # 解决方案: 在调用前预读文件，注入内容到 implementation_summary。
    try:
        from security.sandbox import get_workspace
        ws = str(get_workspace())
        injected_files: list[str] = []
        _small_file = budgets["small_file"]
        _head = budgets["large_file_head"]
        _tail = budgets["large_file_tail"]
        for fp in files_list[:budgets["read_files"]]:  # 最多预读 N 个文件，防止 Token 爆炸
            actual_path = _os.path.join(ws, fp.lstrip("/\\"))
            if _os.path.exists(actual_path):
                try:
                    raw = open(actual_path, encoding="utf-8", errors="replace").read()
                    # 小文件全量注入，大文件截断
                    if len(raw) <= _small_file:
                        injected_files.append(f"#### `{fp}`\n```\n{raw}\n```")
                    else:
                        injected_files.append(
                            f"#### `{fp}` ({len(raw)} 字符，头尾保留)\n"
                            f"```\n{raw[:_head]}\n"
                            f"... [{len(raw) - _head - _tail} 字符省略] ...\n"
                            f"{raw[-_tail:]}\n```"
                        )
                except Exception:
                    injected_files.append(f"#### `{fp}`\n（文件存在但无法读取）")
            else:
                injected_files.append(f"#### `{fp}`\n⚠️ 文件不存在于工作区")
        if injected_files:
            implementation_summary += (
                "\n\n### 实现文件实际内容（供审查用）\n\n"
                + "\n\n".join(injected_files)
            )
    except Exception:
        _log.debug("Failed to pre-read implementation files for audit", exc_info=True)

    _route_desc = (
        f"Round {round_num} — Reviewer 路由到 Provider '{provider_cfg.get('_spec_role', 'unknown')}'"
        + (f" + 跨后端 agent '{_reviewer_agent_id}'（独立审查）" if _cross_backend else "")
    )
    auditor_prompt = render_auditor_prompt(
        spec_yaml, implementation_summary,
        additional_context=_route_desc,
    )

    # 调用 Reviewer（可能是不同的 Provider 或不同后端 agent——实现审查多样性）
    # G1.4: 跨后端审查时用 _reviewer_agent_id（openclaw）替代 task 的 agent_id
    try:
        raw_audit = await self.agent_service._invoke_with_role(
            _reviewer_agent_id, auditor_prompt, task_id=task_id, model_key="secondary",
            provider_config=provider_cfg, language=language,
        )
    except Exception as e:
        _log.error("Auditor channel A failed (agent=%s): %s", _reviewer_agent_id, e)
        # G1.4: 跨后端调用失败 → 回退到 task 原生 agent（hermes）保证审计不中断
        if _cross_backend and _reviewer_agent_id != agent_id:
            try:
                raw_audit = await self.agent_service._invoke_with_role(
                    agent_id, auditor_prompt, task_id=task_id, model_key="secondary",
                    provider_config=provider_cfg, language=language,
                )
                _log.info("Auditor fell back to agent '%s' after cross-backend failure", agent_id)
            except Exception as e2:
                _log.error("Auditor fallback also failed: %s", e2)
                raw_audit = f"[MISMATCH] 审查通道调用失败: {e2}"
        else:
            raw_audit = f"[MISMATCH] 审查通道调用失败: {e}"

    # 解析审查标签
    parsed = _parse_audit_labels(raw_audit)

    # ── 通道 B: 自动验证 ──
    verification_passed = True
    verification_results: list[dict] = []
    verification_commands = inner.get("verification", {}).get("commands", []) \
        if isinstance(inner, dict) else []

    if verification_commands:
        try:
            v_result = await _invoke_spec_verification(
                self, verification_commands, task_id, agent_id=agent_id,
            )
            verification_passed = v_result.get("passed", True)
            verification_results = v_result.get("results", [])
            # 将验证结果追加到审计报告中
            parsed.setdefault("verification_passed", verification_passed)
            parsed.setdefault("verification_results", verification_results)
        except Exception:
            _log.debug("Unhandled exception in verification", exc_info=True)

    parsed["raw_audit"] = raw_audit
    parsed["auditor_provider"] = provider_cfg.get("_spec_role", "unknown")
    # G1.4: 记录跨后端审查信息（供收敛判定/升级决策参考）
    parsed["cross_backend_review"] = _cross_backend
    if _cross_backend:
        parsed["auditor_agent_id"] = _reviewer_agent_id
    return parsed


# ═══════════════════════════════════════════════════════════
# D.5: _invoke_spec_verification() — 运行验证命令
# ═══════════════════════════════════════════════════════════

async def _invoke_spec_verification(
    self, commands: list[str], task_id: str,
    agent_id: str = DEFAULT_AGENT_ID,  # Phase 48-G: 铁律 #1 穿透
) -> dict:
    """阶段 3B: 实际运行 Spec 中的 verification.commands。

    对标 OpenAI Ghost Library: exit code ≠ 0 → 自动修复循环。
    Yuncat v1: 运行验证命令，报告结果，不自动修复（自动修复在 Phase 44 C.1）。

    返回: {passed, results: [{command, passed, output, exit_code}]}

    Phase 48-G: agent_id 参数穿透——修复原硬编码 "hermes" 违反铁律 #1
    （OC 后端任务的验证命令原本也以 hermes 身份执行 ToolService）。
    """
    results: list[dict] = []
    all_passed = True

    # 通过 ToolService 执行命令——走安全白名单
    ts = getattr(self.agent_service, '_tool_service', None)
    if ts is None:
        return {"passed": False, "results": [],
                "error": "ToolService 不可用——无法运行验证命令"}

    # Phase 56-P2: 按当前模型窗口动态计算验证输出预算
    model_cfg = await _get_model_cfg(self)
    budgets = _calc_spec_budgets(model_cfg)
    _verify_output_budget = budgets["verify_output"]

    for cmd in commands:
        try:
            result = await asyncio.wait_for(
                ts.execute("run_command", {"command": cmd},
                          agent_id=agent_id, task_id=task_id),  # Phase 48-G: 铁律 #1 穿透
                timeout=DELIBERATION_CMD_TIMEOUT,  # Phase 48-F: 从 config 抽取
            )
            # run_command 返回 {success, data: {stdout, stderr, exit_code}}
            success = result.get("success", False) if isinstance(result, dict) else False
            data = result.get("data", {}) if isinstance(result, dict) else {}
            output = str(data.get("stdout", ""))[:_verify_output_budget]
            exit_code = data.get("exit_code", -1)

            if not success:
                all_passed = False

            results.append({
                "command": cmd,
                "passed": success,
                "output": output,
                "exit_code": exit_code,
            })
        except asyncio.TimeoutError:
            all_passed = False
            results.append({
                "command": cmd, "passed": False,
                "output": "命令执行超时（60s）", "exit_code": -1,
            })
        except Exception as e:
            all_passed = False
            results.append({
                "command": cmd, "passed": False,
                "output": str(e)[:_verify_output_budget], "exit_code": -1,
            })

    return {
        "passed": all_passed,
        "results": results,
        "summary": f"{sum(1 for r in results if r['passed'])}/{len(results)} 验证通过",
    }


# ═══════════════════════════════════════════════════════════
# 辅助: 审计标签解析
# ═══════════════════════════════════════════════════════════

def _parse_audit_labels(audit_text: str) -> dict:
    """解析审计输出中的标准化标签。

    从 SPEC_AUDITOR_PROMPT 的输出格式中提取:
      [MATCH] ...  — 实现正确
      [MISMATCH] ... — 实现偏离
      [MISSING] ...  — 未实现
      [PASS] ...     — 验证通过
      [FAIL] ...     — 验证失败

    返回: {matches, mismatches, missing, mismatches_list, missing_list,
           verification_results}
    """
    match_lines = re.findall(r'\[MATCH\]\s*(.+)', audit_text, re.IGNORECASE)
    mismatch_lines = re.findall(r'\[MISMATCH\]\s*(.+)', audit_text, re.IGNORECASE)
    missing_lines = re.findall(r'\[MISSING\]\s*(.+)', audit_text, re.IGNORECASE)
    pass_lines = re.findall(r'\[PASS\]\s*(.+)', audit_text, re.IGNORECASE)
    fail_lines = re.findall(r'\[FAIL\]\s*(.+)', audit_text, re.IGNORECASE)

    verification_results = []
    for line in pass_lines:
        verification_results.append({"passed": True, "detail": line.strip()})
    for line in fail_lines:
        verification_results.append({"passed": False, "detail": line.strip()})

    return {
        "matches": len(match_lines),
        "mismatches": len(mismatch_lines),
        "missing": len(missing_lines),
        "matches_list": [m.strip() for m in match_lines],
        "mismatches_list": [m.strip() for m in mismatch_lines],
        "missing_list": [m.strip() for m in missing_lines],
        "verification_passed": len(fail_lines) == 0,
        "verification_results": verification_results,
    }


# ═══════════════════════════════════════════════════════════
# 辅助: Spec 失败升级
# ═══════════════════════════════════════════════════════════

# G1.4: OC 独立审计裁决分类关键词
_OC_ACCEPT_SIGNALS = ("接受", "acceptable", "实现正确", "无问题", "可以接受", "通过审查", "no issue", "looks good")
_OC_REVISE_SIGNALS = ("缺陷", "错误", "不符合", "mismatch", "missing", "建议修改", "需修复", "bug", "缺陷", "不正确", "逻辑错误")


def _classify_oc_verdict(oc_text: str) -> str:
    """G1.4: 将 OC 独立审计文本启发式分类为 accept/revise/inconclusive。

    用于让第二后端的裁决成为升级记录的结构化字段，而非纯文本诊断。
    """
    if not oc_text:
        return "inconclusive"
    _low = oc_text.lower()
    has_revise = any(s in _low for s in _OC_REVISE_SIGNALS) or any(s in oc_text for s in _OC_REVISE_SIGNALS)
    has_accept = any(s in oc_text for s in _OC_ACCEPT_SIGNALS) or any(s in _low for s in _OC_ACCEPT_SIGNALS)
    if has_revise:
        return "revise"
    if has_accept:
        return "accept"
    return "inconclusive"


async def _escalate_spec_failure(
    self, task_id: str, spec_yaml: str | None,
    audit_report: dict, rounds: int, push_callback=None,
    auto_fix_attempts: int = 0,
) -> None:
    """Spec 循环未收敛 → 写入 knowledge_nodes → 仪表盘可见 → 等待人工介入。

    对标 OpenAI Ghost Library: Agent 自解决不了 → 升级到人 + 完整诊断上下文。

    Phase 44 C.1: auto_fix_attempts 记录自动修复次数，供人工审查时参考。
    Phase 43 G1.4: 收敛失败时先咨询第二后端（OC 独立审计），结构化裁决
    （accept/revise/inconclusive）纳入升级记录——升级到人前先经第二后端独立审计。
    """
    # ── G1.4: 先咨询第二后端（OC 独立审计）——裁决纳入升级记录 ──
    oc_verdict: str = ""
    oc_recommendation: str = "not_consulted"
    second_backend_consulted = False
    if rounds >= 3 and self.agent_service:
        oc = self.agent_service.adapters.get(OPENCLAW_AGENT_ID)
        if oc and hasattr(oc, "invoke"):
            try:
                oc_audit = await _invoke_oc_independent_audit(
                    self, spec_yaml or "", audit_report, task_id,
                )
                if oc_audit:
                    second_backend_consulted = True
                    _oc_budget = _calc_spec_budgets({}).get("oc_audit", 500)
                    oc_verdict = oc_audit[:_oc_budget]
                    oc_recommendation = _classify_oc_verdict(oc_audit)
                    await self._push(push_callback, task_id, "spec_phase", {
                        "phase": "oc_audit",
                        "data": f"OC 独立审计: {oc_verdict}",
                        "recommendation": oc_recommendation,
                    })
                    _log.info("OC independent audit for %s (recommendation=%s)",
                             task_id[:12], oc_recommendation)
            except Exception:
                _log.debug("OC independent audit failed", exc_info=True)

    try:
        db = getattr(self.agent_service, 'db', None)
        if not db:
            return

        inner: dict = {}
        if spec_yaml:
            try:
                import yaml as _yaml
                parsed = _yaml.safe_load(spec_yaml)
                if isinstance(parsed, dict):
                    inner = parsed.get("spec", parsed)
            except Exception:
                _log.debug("解析 Spec YAML 失败", exc_info=True)

        spec_name = inner.get("name", "未命名 Spec") if isinstance(inner, dict) else "未命名 Spec"

        # G1.4: 根据 OC 裁决调整建议措辞——第二后端认可 → 倾向接受实现
        _oc_hint = ""
        if oc_recommendation == "accept":
            _oc_hint = "\n第二后端（OpenClaw）独立审计认为实现可接受，建议优先考虑接受当前实现或微调 Spec。"
        elif oc_recommendation == "revise":
            _oc_hint = "\n第二后端（OpenClaw）独立审计发现问题，建议重点检查 Spec 逻辑缺陷。"
        elif second_backend_consulted:
            _oc_hint = "\n第二后端（OpenClaw）独立审计未给出明确裁决，请人工综合判断。"

        escalation_data = {
            "task_id": task_id,
            "spec_name": spec_name,
            "rounds_attempted": rounds,
            "auto_fix_attempts": auto_fix_attempts,
            "remaining_mismatches": audit_report.get("mismatches_list", [])[:DELIBERATION_SPEC_ESCALATION_ISSUES_LIMIT],
            "remaining_missing": audit_report.get("missing_list", [])[:DELIBERATION_SPEC_ESCALATION_ISSUES_LIMIT],
            "verification_passed": audit_report.get("verification_passed", False),
            "second_backend_consulted": second_backend_consulted,  # G1.4
            "oc_verdict": oc_verdict,  # G1.4
            "oc_recommendation": oc_recommendation,  # G1.4
            "suggested_action": (
                f"Spec 循环在 {rounds} 轮后未收敛"
                + (f"（含 {auto_fix_attempts} 次自动修复尝试）。\n" if auto_fix_attempts else "。\n")
                + f"MISMATCH: {audit_report.get('mismatches', 0)} 项\n"
                + f"MISSING: {audit_report.get('missing', 0)} 项\n"
                + "建议人工检查 Spec YAML 和审计报告，决定是修正 Spec 还是接受当前实现。"
                + _oc_hint
            ),
        }

        await db.execute(
            "INSERT OR REPLACE INTO knowledge_nodes(id, title, content, source, "
            "score, tags, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (f"escalation_spec_{task_id[:12]}",
             f"Spec 升级: {spec_name}",
             _json.dumps(escalation_data, ensure_ascii=False),
             "spec_loop_escalation", 0.8, '["escalation","needs_review","spec"]',
             int(_time.time()), int(_time.time())),
        )
        await db.commit()
        _log.info("Spec escalation written to knowledge_nodes for task %s", task_id[:12])

        await self._push(push_callback, task_id, "spec_escalated", escalation_data)
    except Exception:
        _log.debug("Failed to escalate spec failure", exc_info=True)


async def _invoke_oc_independent_audit(
    self, spec_yaml: str, audit_report: dict, task_id: str,
) -> str:
    """Phase 43 M.5: OC 作为独立第三方审计——不同后端可能有不同发现。"""
    oc = self.agent_service.adapters.get(OPENCLAW_AGENT_ID)
    if not oc or not hasattr(oc, "invoke"):
        return ""

    # Phase 56-P2: 按当前模型窗口动态计算预算
    model_cfg = await _get_model_cfg(self)
    budgets = _calc_spec_budgets(model_cfg)
    _spec_budget = budgets["spec_for_oc"]
    _mismatch_items = budgets["mismatch_items"]
    _missing_items = budgets["missing_items"]

    prompt = (
        f"## 独立审计任务\n以下 Spec 在 Hermes 的审查循环中无法收敛。"
        f"作为独立后端，从你的视角重新审视:\n\n"
        f"### Spec\n{spec_yaml[:_spec_budget]}\n\n"
        f"### Hermes 的审计报告\n"
        f"MISMATCH: {audit_report.get('mismatches', 0)} · "
        f"MISSING: {audit_report.get('missing', 0)} · "
        f"Verification: {audit_report.get('verification_passed', False)}\n"
        f"Mismatches: {audit_report.get('mismatches_list', [])[:_mismatch_items]}\n"
        f"Missing: {audit_report.get('missing_list', [])[:_missing_items]}\n\n"
        f"## 问题\n1. 是否有 Hermes 审查者遗漏的问题？\n"
        f"2. Spec 本身是否有逻辑缺陷？\n"
        f"3. 是否有更简单的替代实现方案？"
    )
    try:
        result = await oc.invoke(prompt, {"system": "你是独立代码审计者。"})
        return result.get("content", "") if isinstance(result, dict) else str(result)
    except Exception:
        return ""
