"""services/deliberation_service.py —— 多 Agent 讨论引擎（Phase 9）

单模型多角色并行协作：同一个模型开 N 个并行协程，注入不同角色 system prompt。
流程: 复杂度判定 → 角色选择 → 并行提案 → 交叉审查 → 投票共识

Phase 9 重构: 方法体拆分到子模块，本文件保留薄包装委托调用。
子模块:
  - deliberation_complexity.py    复杂度判定 + 角色选择
  - deliberation_discussion.py    三阶段并行讨论 (提案→交叉审查→共识)
  - deliberation_spec.py          Spec 驱动开发主循环 + 检查点 + 路由
  - deliberation_spec_roles.py    Spec 三阶段角色调用 (Architect/Developer/Auditor)
  - deliberation_goals.py         内置 /goal 条件函数
  - deliberation_ghost.py         Ghost Library 验证 + 链式多后端协作
"""
import asyncio
import logging
from pathlib import Path as _Path

# Phase 48-G: DEFAULT_AGENT_ID 用于 wrapper 方法默认参数（铁律 #1 穿透到 ghost/spec 子模块）
from config import DATA_DIR, DEFAULT_AGENT_ID

# 子模块导入——无循环依赖（子模块不反向导入本文件）
from services.deliberation import (
    deliberation_complexity as _complexity,
    deliberation_discussion as _discussion,
    deliberation_spec as _spec,
    deliberation_spec_roles as _spec_roles,
    deliberation_goals as _goals,
    deliberation_ghost as _ghost,
)

# ═══════════════════════════════════════════════════════════
# 向后兼容重导出——外部代码仍可从本模块导入这些符号
# ═══════════════════════════════════════════════════════════

# 来自 deliberation_complexity
COMPLEXITY_SIGNALS = _complexity.COMPLEXITY_SIGNALS
COMPLEXITY_ROLE_COUNTS = _complexity.COMPLEXITY_ROLE_COUNTS
DEFAULT_ROLE_ORDER = _complexity.DEFAULT_ROLE_ORDER

# 来自 deliberation_discussion
parse_structured_output = _discussion.parse_structured_output
count_divergence = _discussion.count_divergence
ROLE_SYSTEM_TEMPLATE = _discussion.ROLE_SYSTEM_TEMPLATE
REVIEW_PROMPT = _discussion.REVIEW_PROMPT
CONSENSUS_PROMPT = _discussion.CONSENSUS_PROMPT

# 来自 deliberation_spec
SPEC_ROLE_ROUTING = _spec.SPEC_ROLE_ROUTING
_resolve_spec_role_routing = _spec._resolve_spec_role_routing

_log = logging.getLogger("yuncat.deliberation")


class DeliberationService:
    def __init__(self, agent_service=None, model_config=None):
        self.agent_service = agent_service
        self.model_config = model_config
        # Phase 22: Adversarial Reviewer 实例
        self._adversarial = None
        # Phase 49-D6: 讨论流程用户参与——按 task_id 存储用户消息和暂停事件。
        # 设计参考: asyncio.Event 作为暂停闸门（set=运行, clear=暂停），
        # _user_messages 列表作为消息队列，阶段间排空并注入到下一阶段 task_input。
        self._user_messages: dict[str, list[str]] = {}
        self._pause_events: dict[str, asyncio.Event] = {}
        # Phase 49-R1: 运行中讨论的 asyncio.Task 引用——供 cancel_task 取消。
        self._running_tasks: dict[str, asyncio.Task] = {}

    # ── Phase 49-D6: 用户参与接口（WS handler + 阶段间检查点共用） ──

    def register_task(self, task_id: str) -> None:
        """注册新讨论任务——初始化暂停事件为 set（运行中）和空用户消息列表。

        必须在 run_deliberation 启动前调用，确保 WS handler 能找到对应状态。
        """
        self._user_messages[task_id] = []
        event = asyncio.Event()
        event.set()  # 初始为运行态——pause 时 clear，resume 时 set
        self._pause_events[task_id] = event

    def cleanup_task(self, task_id: str) -> None:
        """讨论结束/取消时清理状态——避免内存泄漏。"""
        self._user_messages.pop(task_id, None)
        self._pause_events.pop(task_id, None)
        self._running_tasks.pop(task_id, None)

    def register_running_task(self, task_id: str, task: "asyncio.Task") -> None:
        """保存 asyncio.Task 引用——供 cancel_task 取消讨论。"""
        self._running_tasks[task_id] = task

    def cancel_task(self, task_id: str) -> bool:
        """取消正在运行的讨论任务——返回是否成功找到并取消。

        调用 asyncio.Task.cancel() 会在 _run_deliberation 的 await 点抛出
        CancelledError，由 finally 块清理状态。
        """
        task = self._running_tasks.get(task_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def add_user_message(self, task_id: str, message: str) -> bool:
        """用户作为讨论参与者发送消息——返回是否成功接收。

        消息会在下一阶段开始前排空并注入到 task_input 中。
        """
        msgs = self._user_messages.get(task_id)
        if msgs is None:
            return False
        msgs.append(message)
        return True

    def pause_task(self, task_id: str) -> bool:
        """暂停指定讨论任务——下一阶段间检查点会阻塞等待 resume。"""
        event = self._pause_events.get(task_id)
        if event is None:
            return False
        event.clear()
        return True

    def resume_task(self, task_id: str) -> bool:
        """恢复指定讨论任务——解除阶段间检查点阻塞。"""
        event = self._pause_events.get(task_id)
        if event is None:
            return False
        event.set()
        return True

    # Phase 23: 模型按角色分级——Architect/Reviewer 用 Flash，Editor/Builder 用 Pro
    ROLE_MODEL_TIERS: dict[str, str] = {
        "architect": "flash", "developer": "pro", "security": "flash",
        "product": "flash", "devops": "flash", "reviewer": "flash",
        "tester": "flash", "backend": "pro", "frontend": "pro",
        "dba": "flash", "performance": "pro", "ux": "flash",
    }

    # ── Spec 检查点路径 ──
    # 数据目录只有一处真源（config.DATA_DIR）
    SPEC_CHECKPOINT_DIR = _Path(DATA_DIR) / "workspace" / ".yuncat" / "spec_checkpoints"
    SPEC_CHECKPOINT_SCHEMA_VERSION = 1  # Phase 44 C.2: 检查点格式版本

    MAX_SPEC_ROUNDS = 3       # 默认最大收敛轮次
    # Phase 48-F→48-G: SPEC_ROLE_TIMEOUT 已迁移到 config.DELIBERATION_ROLE_TIMEOUT
    # —— deliberation_spec.py 5 处 timeout 引用此常量（spec loop 各阶段超时）
    # —— deliberation_discussion.py 也共用此常量（并行讨论阶段超时）

    # ═══════════════════════════════════════════════════════════
    # 复杂度判定 + 角色选择 → deliberation_complexity.py
    # ═══════════════════════════════════════════════════════════

    def _get_role_model(self, role_id: str) -> str | None:
        """按角色获取模型级别——flash 用于分析角色，pro 用于构建角色"""
        return _complexity._get_role_model(self, role_id)

    async def _maybe_generate_temp_role(
        self, task_input: str, selected_roles: list[dict],
    ) -> dict | None:
        """Phase 30: LLM 判断是否需要 DB 中没有的临时角色。"""
        return await _complexity._maybe_generate_temp_role(self, task_input, selected_roles)

    def assess_complexity(self, prompt: str) -> dict:
        """复杂度判定——关键词打分 → {level, suggested_roles, effort}"""
        return _complexity.assess_complexity(self, prompt)

    async def select_roles(self, level: str, db=None, count: int | None = None,
                           task_prompt: str = "") -> list[dict]:
        """根据复杂度级别选择角色。Phase 23: LLM 辅助（task_prompt 非空时）。"""
        return await _complexity.select_roles(self, level, db, count, task_prompt)

    async def _llm_rank_roles(self, task_prompt: str, roles: list[dict]) -> dict[str, int]:
        """LLM 对角色进行排序——返回 {role_id: rank}"""
        return await _complexity._llm_rank_roles(self, task_prompt, roles)

    # ═══════════════════════════════════════════════════════════
    # 三阶段并行讨论 → deliberation_discussion.py
    # ═══════════════════════════════════════════════════════════

    async def run_deliberation(
        self,
        task_input: str,
        roles: list[dict],
        task_id: str,
        push_callback=None,
        language: str = "zh",
    ) -> dict:
        """三阶段讨论流程。push_callback(task_id, msg_type, payload) 用于 WS 推送。"""
        return await _discussion.run_deliberation(self, task_input, roles, task_id, push_callback, language=language)

    async def _push(self, callback, task_id: str, msg_type: str, payload: dict) -> None:
        """WS 推送辅助——短小，保留在本文件内联。"""
        if callback:
            try:
                await callback(task_id, msg_type, payload)
            except Exception:
                _log.debug("Unexpected error", exc_info=True)

    # ═══════════════════════════════════════════════════════════
    # Phase 45 D.2: 内置 /goal 条件函数 → deliberation_goals.py
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def goal_all_tests_pass(audit: dict) -> bool:
        """所有验证命令通过 + 零 MISMATCH + 零 MISSING。"""
        return _goals.goal_all_tests_pass(audit)

    @staticmethod
    def goal_mismatch_zero(audit: dict) -> bool:
        """仅要求零 MISMATCH + 零 MISSING——验证命令允许失败。"""
        return _goals.goal_mismatch_zero(audit)

    @staticmethod
    def goal_verification_only(audit: dict) -> bool:
        """仅要求所有验证命令通过——允许 MISMATCH。"""
        return _goals.goal_verification_only(audit)

    # ═══════════════════════════════════════════════════════════
    # Spec 驱动开发: 路由 + 检查点 + 主循环 → deliberation_spec.py
    # ═══════════════════════════════════════════════════════════

    async def _resolve_spec_provider_config(
        self, role_id: str, round_num: int, configured_providers: list[str],
        available_agents: list[str] | None = None,
    ) -> dict | None:
        """解析 Spec 角色的完整 Provider 配置。"""
        return await _spec._resolve_spec_provider_config(
            self, role_id, round_num, configured_providers, available_agents,
        )

    @staticmethod
    def _get_project_structure(workspace_root: str | None = None) -> str:
        """扫描项目目录结构——供 Architect 了解模块划分。"""
        return _spec._get_project_structure(workspace_root)

    @staticmethod
    def _detect_key_imports(root: _Path) -> str:
        """检测关键跨模块 import——供 Architect 理解依赖方向。"""
        return _spec._detect_key_imports(root)

    def _checkpoint_path(self, task_id: str) -> _Path:
        return _spec._checkpoint_path(self, task_id)

    def _save_checkpoint(self, task_id: str, data: dict) -> None:
        _spec._save_checkpoint(self, task_id, data)

    def _load_checkpoint(self, task_id: str) -> dict | None:
        return _spec._load_checkpoint(self, task_id)

    def _delete_checkpoint(self, task_id: str) -> None:
        _spec._delete_checkpoint(self, task_id)

    async def run_spec_loop(
        self,
        task_input: str,
        task_id: str,
        push_callback=None,
        max_rounds: int = 10,  # Phase 48-H: 3→10 Spec 收敛循环放宽
        mode: str = "fixed",
        goal_condition=None,
        preferred_backend: str = "",
        agent_id: str = DEFAULT_AGENT_ID,  # Phase 48-G: 铁律 #1 穿透
        language: str = "zh",
    ) -> dict:
        """Spec 驱动开发主循环——三阶段收敛。"""
        from context import set_current_language, reset_current_language
        lang_token = set_current_language(language)
        try:
            return await _spec.run_spec_loop(
                self, task_input, task_id, push_callback,
                max_rounds, mode, goal_condition, preferred_backend, agent_id,
                language=language,
            )
        finally:
            try:
                reset_current_language(lang_token)
            except Exception:
                pass

    # ═══════════════════════════════════════════════════════════
    # Spec 三阶段角色调用 → deliberation_spec_roles.py
    # ═══════════════════════════════════════════════════════════

    async def _invoke_spec_architect(
        self, task_input: str, project_structure: str,
        configured_providers: list[str], round_num: int,
        previous_audit: dict | None = None,
        agent_id: str = DEFAULT_AGENT_ID,  # Phase 48-G: 铁律 #1 穿透
        language: str = "zh",
    ) -> dict:
        """阶段 1: Architect 起草/修正结构化 YAML Spec。

        Phase 48-G: agent_id 参数穿透——修复原硬编码 "hermes" 违反铁律 #1。
        """
        return await _spec_roles._invoke_spec_architect(
            self, task_input, project_structure, configured_providers,
            round_num, previous_audit, agent_id=agent_id,
            language=language,
        )

    async def _invoke_spec_developer(
        self, spec_yaml: str, spec_dict: dict,
        configured_providers: list[str], round_num: int,
        task_id: str, override_prompt: str | None = None,
        agent_id: str = DEFAULT_AGENT_ID,  # Phase 48-G: 铁律 #1 穿透
        language: str = "zh",
    ) -> dict:
        """阶段 2: Developer 只看 Spec 实现代码——有 3 轮工具循环。

        Phase 48-G: agent_id 参数穿透——修复原硬编码 "hermes" 违反铁律 #1。
        """
        return await _spec_roles._invoke_spec_developer(
            self, spec_yaml, spec_dict, configured_providers,
            round_num, task_id, override_prompt, agent_id=agent_id,
            language=language,
        )

    async def _invoke_spec_auditor(
        self, spec_yaml: str, spec_dict: dict,
        developer_output: str, configured_providers: list[str],
        round_num: int, task_id: str,
        agent_id: str = DEFAULT_AGENT_ID,  # Phase 48-G: 铁律 #1 穿透
        language: str = "zh",
    ) -> dict:
        """阶段 3: Reviewer 双通道审计。

        Phase 48-G: agent_id 参数穿透——修复原硬编码 "hermes" 违反铁律 #1。
        """
        return await _spec_roles._invoke_spec_auditor(
            self, spec_yaml, spec_dict, developer_output,
            configured_providers, round_num, task_id, agent_id=agent_id,
            language=language,
        )

    async def _invoke_spec_verification(
        self, commands: list[str], task_id: str,
        agent_id: str = DEFAULT_AGENT_ID,  # Phase 48-G: 铁律 #1 穿透
    ) -> dict:
        """阶段 3B: 实际运行 Spec 中的 verification.commands。

        Phase 48-G: agent_id 参数穿透——修复原硬编码 "hermes" 违反铁律 #1。
        """
        return await _spec_roles._invoke_spec_verification(
            self, commands, task_id, agent_id=agent_id,
        )

    @staticmethod
    def _parse_audit_labels(audit_text: str) -> dict:
        """解析审计输出中的标准化标签。"""
        return _spec_roles._parse_audit_labels(audit_text)

    async def _escalate_spec_failure(
        self, task_id: str, spec_yaml: str | None,
        audit_report: dict, rounds: int, push_callback=None,
        auto_fix_attempts: int = 0,
    ) -> None:
        """Spec 循环未收敛 → 写入 knowledge_nodes → 等待人工介入。"""
        return await _spec_roles._escalate_spec_failure(
            self, task_id, spec_yaml, audit_report, rounds,
            push_callback, auto_fix_attempts,
        )

    async def _invoke_oc_independent_audit(
        self, spec_yaml: str, audit_report: dict, task_id: str,
    ) -> str:
        """Phase 43 M.5: OC 作为独立第三方审计。"""
        return await _spec_roles._invoke_oc_independent_audit(self, spec_yaml, audit_report, task_id)

    # ═══════════════════════════════════════════════════════════
    # Ghost Library + 链式协作 → deliberation_ghost.py
    # ═══════════════════════════════════════════════════════════

    async def ghost_library_verify(
        self, spec_yaml: str, original_impl: dict,
        configured_providers: list[str] | None = None,
        push_callback=None,
        agent_id: str = DEFAULT_AGENT_ID,  # Phase 48-G: 铁律 #1 穿透
    ) -> dict:
        """Ghost Library 验证: Spec 能否被独立 Agent 精确复现。"""
        return await _ghost.ghost_library_verify(
            self, spec_yaml, original_impl, configured_providers, push_callback,
            agent_id=agent_id,
        )

    async def chain_collaborate(
        self, task_input: str,
        vision_inputs: list[dict] | None = None,
        task_id: str = "",
        agent_id: str = DEFAULT_AGENT_ID,  # Phase 48-G: 铁律 #1 穿透
    ) -> dict:
        """链式多后端协作: vision→Architect→Developer→Reviewer。"""
        return await _ghost.chain_collaborate(
            self, task_input, vision_inputs, task_id, agent_id=agent_id,
        )
