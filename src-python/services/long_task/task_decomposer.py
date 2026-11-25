"""services/long_task/task_decomposer.py —— Phase 79 任务分解 Agent

长任务生命周期的第二步：在 GoalClarifier 确认目标后，将目标分解为多级子任务树。
- decompose(task_id, confirmed_goal)：调 LLM 输出树形 JSON → 扁平化 → 写入 plan_steps
- regenerate_subtree(task_id, node_id)：删 pending 子孙 → 用节点 title+description 重新分解

设计约束：
- plan_steps 表 level CHECK(0-2)——3 层（目标→阶段→动作），与 Phase 77 设计一致
- 方案文档说"无固定深度"，但 LLM 3 层以上质量衰减严重，prompt 明确要求最多 3 层
- 每个节点必含 title + acceptance_criteria（非空）+ dependencies（list[str] of node id）

依赖（lifespan 注入）：
- adapters: dict[agent_id, adapter]
- model_config: ModelConfigService
- conn: aiosqlite 全局连接
"""
import json
import logging
import uuid as _uuid
from typing import Any

import config
from config import LOG_PREVIEW_CMD_LEN, DEFAULT_AGENT_ID

from repositories import (
    create_plan_tree,
    delete_pending_children,
    get_plan_tree_by_task,
)

logger = logging.getLogger("yuncat.long_task")

# ── 常量（Task 6.12: 移至 config.py；此处为导入时快照，向后兼容现有引用） ──
# TWO_PHASE 在 decompose() 内动态读取 config.TASK_DECOMPOSER_TWO_PHASE——支持运行时切换 + 测试 monkeypatch
_MAX_TOKENS = config.TASK_DECOMPOSER_MAX_TOKENS
_TEMPERATURE = config.TASK_DECOMPOSER_TEMPERATURE
_MAX_LEVEL = config.TASK_DECOMPOSER_MAX_LEVEL
_MAX_NODES = config.TASK_DECOMPOSER_MAX_NODES


class TaskDecomposer:
    """任务分解 Agent——Phase 79

    方案 §8 Phase 79：decompose(confirmed_goal) 输出树形 JSON，
    校验后写入 plan_steps。regenerate_subtree 支持中途重分解。
    """

    def __init__(self, adapters: dict, model_config, conn):
        """
        Args:
            adapters: dict[agent_id, adapter]——hermes/openclaw adapter
            model_config: ModelConfigService——获取激活模型配置
            conn: aiosqlite 全局连接——写 plan_steps
        """
        self._adapters = adapters
        self._model_config = model_config
        self._conn = conn

    # ═══════════════════════════════════════════════════════════
    # decompose —— 首次分解
    # ═══════════════════════════════════════════════════════════

    async def decompose(
        self,
        task_id: str,
        confirmed_goal: str,
        agent_id: str = DEFAULT_AGENT_ID,
        standards_context: str | None = None,
    ) -> dict[str, Any]:
        """将确认目标分解为多级子任务树，写入 plan_steps

        Args:
            task_id: 根任务 id（plan_steps.task_id 字段）
            confirmed_goal: GoalClarifier 提炼的确认目标
            agent_id: 使用的 Agent
            standards_context: Task 6.13——decompose 前置查询的文档摘要（可选），
                              注入 prompt 的「## 参考文档」段，让分解参考最新标准。
                              None 时跳过（向后兼容）。

        Returns:
            {
                "nodes": [{id, parent_id, level, title, ...}],  # 已写入 DB 的扁平节点列表
                "root_count": int,  # level=0 节点数（通常 1）
                "total": int,
                "source": "llm" | "fallback",
            }
        LLM 失败时走 _fallback_decompose——生成单节点「执行 {confirmed_goal}」。
        """
        adapter = self._adapters.get(agent_id)
        if adapter is None:
            return await self._fallback_decompose(task_id, confirmed_goal)

        try:
            cfg = await self._model_config.get_active_config(agent_id)
        except Exception:
            return await self._fallback_decompose(task_id, confirmed_goal)

        # Task 6.12: 两阶段分解开关——True 时粗分解(level 0-1) + 每阶段细分解(level 2)
        # 默认 False（向后兼容）；运行时读 config 支持测试 monkeypatch
        if config.TASK_DECOMPOSER_TWO_PHASE:
            return await self._decompose_two_phase(
                task_id, confirmed_goal, agent_id, adapter, cfg, standards_context,
            )

        prompt = self._build_decompose_prompt(confirmed_goal, standards_context)
        try:
            result = await adapter.invoke(
                prompt,
                context={
                    "model_config": cfg,
                    "max_tokens": _MAX_TOKENS,
                    "temperature": _TEMPERATURE,
                },
            )
        except Exception:
            return await self._fallback_decompose(task_id, confirmed_goal)

        if "error" in result:
            return await self._fallback_decompose(task_id, confirmed_goal)

        content = result.get("content", "")
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, ValueError, TypeError):
            return await self._fallback_decompose(task_id, confirmed_goal)

        # 校验 + 扁平化
        flat_nodes = self._normalize_tree(parsed)
        if not flat_nodes:
            return await self._fallback_decompose(task_id, confirmed_goal)

        # 写入 plan_steps
        written_ids = await create_plan_tree(self._conn, task_id, flat_nodes)
        written = await get_plan_tree_by_task(self._conn, task_id)
        # 只返回本次写入的节点（按 written_ids 过滤）
        written_set = set(written_ids)
        nodes = [n for n in written if n["id"] in written_set]
        root_count = sum(1 for n in nodes if n["level"] == 0)

        return {
            "nodes": nodes,
            "root_count": root_count,
            "total": len(nodes),
            "source": "llm",
        }

    # ═══════════════════════════════════════════════════════════
    # Task 6.12: 两阶段分解（粗→细）
    # ═══════════════════════════════════════════════════════════

    async def _decompose_two_phase(
        self,
        task_id: str,
        confirmed_goal: str,
        agent_id: str,
        adapter,
        cfg: dict,
        standards_context: str | None = None,
    ) -> dict[str, Any]:
        """两阶段分解——粗分解(level 0-1) + 每阶段细分解(level 2)

        流程：
        1. 粗分解：1 次 LLM 调用 → root + phases（level 0-1，不含 actions）
        2. 细分解：对每个 level 1 phase 单独调 LLM → actions（level 2）
           - best-effort：单个细分解失败/解析错误 → 跳过该 phase，不影响其他
        3. 合并 + 统一 remap dependencies（跨阶段共用 counter 避免 id 冲突）

        粗分解失败 → 整体走 _fallback_decompose（单节点兜底）。
        细分解全失败 → 返回 root + phases（无 actions），source 仍为 "llm"。

        设计权衡：
        - 共享 counter=[0] + id_mapping——避免粗/细分解 id 冲突（n1/n2... 连续分配）
        - 粗分解后过滤 level > 1 节点——防御 LLM 误输出 level 2（prompt 要求只输出 0-1）
        - 细分解用 _flatten_node(level=2)——_MAX_LEVEL 截断自动生效，level 3 嵌套被丢弃
        """
        # ── 阶段 1：粗分解 ──
        coarse_prompt = self._build_coarse_prompt(confirmed_goal, standards_context)
        try:
            coarse_result = await adapter.invoke(
                coarse_prompt,
                context={
                    "model_config": cfg,
                    "max_tokens": _MAX_TOKENS,
                    "temperature": _TEMPERATURE,
                },
            )
        except Exception:
            return await self._fallback_decompose(task_id, confirmed_goal)

        if "error" in coarse_result:
            return await self._fallback_decompose(task_id, confirmed_goal)

        try:
            coarse_parsed = json.loads(coarse_result.get("content", ""))
        except (json.JSONDecodeError, ValueError, TypeError):
            return await self._fallback_decompose(task_id, confirmed_goal)

        # 扁平化粗分解——共享 counter 避免 id 冲突
        counter: list[int] = [0]
        id_mapping: dict[str, str] = {}
        coarse_flat: list[dict] = []
        if isinstance(coarse_parsed, dict):
            self._flatten_node(
                coarse_parsed, parent_id=None, level=0, flat=coarse_flat,
                counter=counter, id_mapping=id_mapping,
            )
        # 粗分解只保留 level 0-1（防御 LLM 误输出 level 2）
        coarse_flat = [n for n in coarse_flat if n["level"] <= 1]
        if not coarse_flat:
            return await self._fallback_decompose(task_id, confirmed_goal)

        # ── 阶段 2：对每个 level 1 phase 细分解 ──
        phases = [n for n in coarse_flat if n["level"] == 1]
        fine_flat: list[dict] = []
        for phase in phases:
            # 节点数预算检查
            if len(coarse_flat) + len(fine_flat) >= _MAX_NODES:
                break

            sub_goal = phase["title"]
            if phase.get("description"):
                sub_goal = f"{sub_goal}\n{phase['description']}"

            fine_prompt = self._build_subtree_prompt(sub_goal, parent_level=1)
            try:
                fine_result = await adapter.invoke(
                    fine_prompt,
                    context={
                        "model_config": cfg,
                        "max_tokens": _MAX_TOKENS,
                        "temperature": _TEMPERATURE,
                    },
                )
            except Exception:
                logger.warning(
                    "task_decomposer: phase %s 细分解失败，跳过（best-effort）",
                    phase["id"],
                )
                continue  # best-effort：跳过该 phase，继续下一个
            if "error" in fine_result:
                logger.warning(
                    "task_decomposer: phase %s 细分解返回 error，跳过",
                    phase["id"],
                )
                continue
            try:
                fine_parsed = json.loads(fine_result.get("content", ""))
            except (json.JSONDecodeError, ValueError, TypeError):
                logger.warning(
                    "task_decomposer: phase %s 细分解 JSON 解析失败，跳过",
                    phase["id"],
                )
                continue
            if not isinstance(fine_parsed, list):
                continue

            # 细分解 children——parent_id=phase["id"], level=2
            # _flatten_node 在 level >= _MAX_LEVEL 时不展开 children → level 3 自动截断
            for child in fine_parsed:
                if not isinstance(child, dict):
                    continue
                self._flatten_node(
                    child, parent_id=phase["id"], level=2, flat=fine_flat,
                    counter=counter, id_mapping=id_mapping,
                )
                if len(coarse_flat) + len(fine_flat) >= _MAX_NODES:
                    break

        # ── 合并 + 统一 remap dependencies + 写入 ──
        all_nodes = coarse_flat + fine_flat
        self._remap_dependencies(all_nodes, id_mapping)
        all_nodes = all_nodes[:_MAX_NODES]

        written_ids = await create_plan_tree(self._conn, task_id, all_nodes)
        written = await get_plan_tree_by_task(self._conn, task_id)
        written_set = set(written_ids)
        nodes = [n for n in written if n["id"] in written_set]
        root_count = sum(1 for n in nodes if n["level"] == 0)

        return {
            "nodes": nodes,
            "root_count": root_count,
            "total": len(nodes),
            "source": "llm",
        }

    def _build_coarse_prompt(
        self, confirmed_goal: str, standards_context: str | None = None,
    ) -> str:
        """Task 6.12: 构建粗分解 prompt——只输出 level 0-1（root + phases）

        与 _build_decompose_prompt 区别：明确要求不输出 level 2 actions，
        后续会单独对每个 phase 细分解。这样粗分解 token 预算集中在阶段划分质量上。

        Task 6.13: standards_context 非空时注入「## 参考文档」段。
        """
        standards_section = self._format_standards_section(standards_context)
        return f"""你是任务分解专家。将以下确认目标粗分解为阶段树（只到阶段层，不含具体动作）。

## 确认目标
{confirmed_goal}
{standards_section}
## 分解规则
- 只输出 2 层：level 0（根目标）→ level 1（阶段）
- **不要输出 level 2（动作）**——后续会单独细化每个阶段
- 每个节点必含：title、description、acceptance_criteria（非空 list）、dependencies（list[str] 引用同层前序节点 id）
- dependencies 用相对 id（如 "p1" 引用同 JSON 内的兄弟节点）
- 阶段数建议 2-5 个，粒度对应"可独立验收的子目标"
- 节点总数 ≤ {_MAX_NODES}

## 输出格式（严格 JSON）
{{
  "id": "root",
  "title": "目标简述",
  "description": "目标详细描述",
  "acceptance_criteria": ["验收1", "验收2"],
  "children": [
    {{
      "id": "p1",
      "title": "阶段1",
      "description": "...",
      "acceptance_criteria": ["..."],
      "dependencies": []
    }}
  ]
}}

只输出 JSON，不要其他文字。"""

    # ═══════════════════════════════════════════════════════════
    # regenerate_subtree —— 中途重分解
    # ═══════════════════════════════════════════════════════════

    async def regenerate_subtree(
        self,
        task_id: str,
        node_id: str,
        agent_id: str = DEFAULT_AGENT_ID,
    ) -> dict[str, Any]:
        """重新分解某节点的子树

        流程：
        1. 查 node_id 的 title/description/level——作为子目标
        2. delete_pending_children——删 node_id 的所有 pending 子孙（保留 node_id 本身 + completed/failed 历史）
        3. 调 LLM 重新分解（输入：node 的 title+description）
        4. 写入新子节点（parent_id=node_id, level=node.level+1）

        Args:
            task_id: 根任务 id
            node_id: 待重分解的节点 id
            agent_id: 使用的 Agent

        Returns:
            {
                "nodes": [...],  # 新写入的子节点列表
                "deleted_count": int,  # 删除的 pending 子孙数
                "source": "llm" | "fallback",
            }
        node_id 不存在 → 抛 ValueError。
        """
        # 1. 查 node_id 信息
        tree = await get_plan_tree_by_task(self._conn, task_id)
        target = next((n for n in tree if n["id"] == node_id), None)
        if target is None:
            raise ValueError(f"node not found: {node_id}")

        # 2. 删 pending 子孙
        deleted = await delete_pending_children(self._conn, task_id, node_id)

        # 3. 构造子目标
        sub_goal = target["title"]
        if target.get("description"):
            sub_goal = f"{sub_goal}\n{target['description']}"

        # 4. 调 LLM 重新分解（子目标，不再嵌套 root，直接生成 children）
        adapter = self._adapters.get(agent_id)
        if adapter is None:
            new_nodes = await self._fallback_subtree(task_id, node_id, target["level"], sub_goal)
            return {"nodes": new_nodes, "deleted_count": deleted, "source": "fallback"}

        try:
            cfg = await self._model_config.get_active_config(agent_id)
        except Exception:
            new_nodes = await self._fallback_subtree(task_id, node_id, target["level"], sub_goal)
            return {"nodes": new_nodes, "deleted_count": deleted, "source": "fallback"}

        prompt = self._build_subtree_prompt(sub_goal, parent_level=target["level"])
        try:
            result = await adapter.invoke(
                prompt,
                context={
                    "model_config": cfg,
                    "max_tokens": _MAX_TOKENS,
                    "temperature": _TEMPERATURE,
                },
            )
        except Exception:
            new_nodes = await self._fallback_subtree(task_id, node_id, target["level"], sub_goal)
            return {"nodes": new_nodes, "deleted_count": deleted, "source": "fallback"}

        if "error" in result:
            new_nodes = await self._fallback_subtree(task_id, node_id, target["level"], sub_goal)
            return {"nodes": new_nodes, "deleted_count": deleted, "source": "fallback"}

        content = result.get("content", "")
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, ValueError, TypeError):
            new_nodes = await self._fallback_subtree(task_id, node_id, target["level"], sub_goal)
            return {"nodes": new_nodes, "deleted_count": deleted, "source": "fallback"}

        # 5. 规范化——子树根 level = parent_level + 1
        child_level = min(target["level"] + 1, _MAX_LEVEL)
        flat_children = self._normalize_children(parsed, parent_id=node_id, base_level=child_level)
        if not flat_children:
            flat_children = await self._fallback_subtree(task_id, node_id, target["level"], sub_goal)
            return {"nodes": flat_children, "deleted_count": deleted, "source": "fallback"}

        # 6. 写入
        written_ids = await create_plan_tree(self._conn, task_id, flat_children)
        written = await get_plan_tree_by_task(self._conn, task_id)
        written_set = set(written_ids)
        nodes = [n for n in written if n["id"] in written_set]

        return {"nodes": nodes, "deleted_count": deleted, "source": "llm"}

    # ═══════════════════════════════════════════════════════════
    # 内部辅助
    # ═══════════════════════════════════════════════════════════

    def _build_decompose_prompt(
        self, confirmed_goal: str, standards_context: str | None = None,
    ) -> str:
        """构建首次分解的 prompt——输出嵌套树

        Task 6.13: standards_context 非空时注入「## 参考文档」段。
        """
        standards_section = self._format_standards_section(standards_context)
        return f"""你是任务分解专家。将以下确认目标分解为多级子任务树。

## 确认目标
{confirmed_goal}
{standards_section}
## 分解规则
- 最多 3 层：level 0（根目标）→ level 1（阶段）→ level 2（动作）
- 每个节点必含：title、description、acceptance_criteria（非空 list）、dependencies（list[str] 引用同层前序节点 id）
- dependencies 用相对 id（如 "p1" 引用同 JSON 内的兄弟节点）
- 动作层（level 2）应可独立执行，粒度约 1-3 个文件改动
- 节点总数 ≤ {_MAX_NODES}

## 输出格式（严格 JSON）
{{
  "id": "root",
  "title": "目标简述",
  "description": "目标详细描述",
  "acceptance_criteria": ["验收1", "验收2"],
  "children": [
    {{
      "id": "p1",
      "title": "阶段1",
      "description": "...",
      "acceptance_criteria": ["..."],
      "children": [
        {{
          "id": "a1",
          "title": "动作1",
          "description": "...",
          "acceptance_criteria": ["..."],
          "dependencies": []
        }}
      ]
    }}
  ]
}}

只输出 JSON，不要其他文字。"""

    @staticmethod
    def _format_standards_section(standards_context: str | None) -> str:
        """Task 6.13: 格式化参考文档段——None/空字符串时返回空（不注入）

        注入位置：确认目标之后、分解规则之前。让分解参考最新文档/标准，
        避免使用过期 API 或废弃用法。
        """
        if not standards_context or not standards_context.strip():
            return ""
        return f"""
## 参考文档（最新标准）
分解时请参考以下最新文档/标准，避免使用过期 API 或废弃用法：
{standards_context.strip()}
"""

    def _build_subtree_prompt(self, sub_goal: str, parent_level: int) -> str:
        """构建子树重分解的 prompt——输出 children 数组"""
        remaining_levels = _MAX_LEVEL - parent_level  # 还能分解几层
        return f"""你是任务分解专家。将以下子目标分解为多级子任务树。

## 子目标
{sub_goal}

## 分解规则
- 剩余可分解层数：{remaining_levels}（parent_level={parent_level}，max_level={_MAX_LEVEL}）
- 每个节点必含：title、description、acceptance_criteria（非空 list）、dependencies（list[str] 引用同层前序节点 id）
- 节点总数 ≤ {_MAX_NODES}

## 输出格式（严格 JSON 数组）
[
  {{
    "id": "c1",
    "title": "...",
    "description": "...",
    "acceptance_criteria": ["..."],
    "dependencies": [],
    "children": [...]
  }}
]

只输出 JSON 数组，不要其他文字。"""

    def _normalize_tree(self, parsed) -> list[dict]:
        """规范化嵌套树 → 扁平节点列表（带 level + parent_id）

        - parsed 应为 dict（root 节点）
        - DFS 遍历，分配 level（0-based）+ parent_id
        - level > _MAX_LEVEL 的节点丢弃其 children（不展开）
        - 校验：title 非空、acceptance_criteria 非空 list
        - 重新分配 id（用前缀 + 序号，避免 LLM 输出冲突）
        - P0-1.8: 回填 dependencies（原硬编码清空 [] 导致依赖丢失）
        """
        if not isinstance(parsed, dict):
            return []

        flat: list[dict] = []
        # P0-1.8: 收集 old_id → new_id 映射，用于回填 dependencies
        id_mapping: dict[str, str] = {}
        self._flatten_node(parsed, parent_id=None, level=0, flat=flat, counter=[0],
                           id_mapping=id_mapping)
        # P0-1.8: 第二遍——用 id_mapping 回填 dependencies
        self._remap_dependencies(flat, id_mapping)
        return flat[:_MAX_NODES]

    def _normalize_children(self, parsed, parent_id: str, base_level: int) -> list[dict]:
        """规范化 children 数组 → 扁平节点列表

        - parsed 应为 list（children 数组）
        - base_level = parent_level + 1
        """
        if not isinstance(parsed, list):
            return []
        flat: list[dict] = []
        counter = [0]
        # P0-1.8: 收集 old_id → new_id 映射，用于回填 dependencies
        id_mapping: dict[str, str] = {}
        for child in parsed:
            if not isinstance(child, dict):
                continue
            self._flatten_node(child, parent_id=parent_id, level=base_level, flat=flat,
                               counter=counter, id_mapping=id_mapping)
            if len(flat) >= _MAX_NODES:
                break
        # P0-1.8: 第二遍——用 id_mapping 回填 dependencies
        self._remap_dependencies(flat, id_mapping)
        return flat[:_MAX_NODES]

    def _flatten_node(
        self, node: dict, parent_id: str | None, level: int,
        flat: list[dict], counter: list[int],
        id_mapping: dict[str, str] | None = None,
    ) -> None:
        """DFS 扁平化单个节点

        P0-1.8: 新增 id_mapping 参数——收集 LLM 输出的 old_id → new_id 映射，
        供 _remap_dependencies 回填 dependencies（原硬编码清空 [] 导致依赖丢失）。
        """
        if level > _MAX_LEVEL:
            return
        # 校验必填字段
        title = str(node.get("title", "")).strip()
        if not title:
            return
        criteria = node.get("acceptance_criteria", [])
        if not isinstance(criteria, list) or not criteria:
            criteria = [f"完成: {title}"]
        else:
            criteria = [str(c).strip() for c in criteria if str(c).strip()]
            if not criteria:
                criteria = [f"完成: {title}"]

        # 重新分配 id（避免 LLM 输出 id 冲突）
        counter[0] += 1
        new_id = f"n{counter[0]}"

        description = str(node.get("description", "")).strip()

        # P0-1.8: 收集 old_id → new_id 映射 + 保留原始 dependencies 供回填
        old_id = node.get("id")
        if old_id and id_mapping is not None:
            id_mapping[str(old_id)] = new_id
        raw_deps = node.get("dependencies", [])
        if not isinstance(raw_deps, list):
            raw_deps = []

        flat.append({
            "id": new_id,
            "parent_id": parent_id,
            "level": level,
            "title": title,
            "description": description,
            "acceptance_criteria": criteria,
            # P0-1.8: 暂存原始 dependencies（old_id 列表），由 _remap_dependencies 回填
            "dependencies": [],
            "_raw_deps": [str(d) for d in raw_deps if d],  # 临时字段，回填后删除
            "status": "pending",
            # Task 3.3: metadata 记录依赖映射诊断信息（如 dropped_deps）
            # 不持久化到 plan_steps 表（避免 schema 迁移），仅供调用方/日志观测
            "metadata": {},
        })

        # 递归 children（如果到达 _MAX_LEVEL，不再展开）
        if level >= _MAX_LEVEL:
            return
        children = node.get("children", [])
        if not isinstance(children, list):
            return
        for child in children:
            if not isinstance(child, dict):
                continue
            self._flatten_node(child, parent_id=new_id, level=level + 1, flat=flat,
                               counter=counter, id_mapping=id_mapping)
            if len(flat) >= _MAX_NODES:
                return

    def _remap_dependencies(self, flat: list[dict], id_mapping: dict[str, str]) -> None:
        """P0-1.8 / Task 3.3: 用 id_mapping 把 _raw_deps（old_id）回填到 dependencies（new_id）

        - old_id 在 id_mapping 中找到 → 映射为 new_id 加入 dependencies
        - old_id 找不到（LLM 输出不可靠，或被 _MAX_LEVEL 截断未进 id_mapping）
          → 丢弃该依赖，但记 warning + 在 node["metadata"]["dropped_deps"] 记录
          （原实现静默丢弃，任务可能丢边且无诊断线索）
        - 清理临时 _raw_deps 字段
        """
        if not id_mapping:
            # 无映射（LLM 未输出 id）——直接清理 _raw_deps，dependencies 保持 []
            # 此时 _raw_deps 全部无法映射，记 warning + 记录 dropped_deps
            for node in flat:
                raw_deps = node.pop("_raw_deps", None) or []
                if raw_deps:
                    node.setdefault("metadata", {})["dropped_deps"] = list(raw_deps)
                    logger.warning(
                        "task_decomposer: node %s 丢弃 %d 个未映射依赖（无 id_mapping）: %s",
                        node["id"], len(raw_deps), raw_deps,
                    )
                else:
                    node.pop("metadata", None)  # 无诊断信息则清理空 metadata
            return
        for node in flat:
            raw_deps = node.pop("_raw_deps", None) or []
            mapped = []
            dropped = []
            for old_dep in raw_deps:
                new_dep = id_mapping.get(old_dep)
                if new_dep and new_dep != node["id"] and new_dep not in mapped:
                    mapped.append(new_dep)
                else:
                    # Task 3.3: 未映射 or 自引用 or 重复 → 记录到 dropped_deps
                    dropped.append(old_dep)
            node["dependencies"] = mapped
            if dropped:
                node.setdefault("metadata", {})["dropped_deps"] = dropped
                logger.warning(
                    "task_decomposer: node %s 丢弃 %d 个未映射依赖: %s",
                    node["id"], len(dropped), dropped,
                )
            else:
                node.pop("metadata", None)  # 无诊断信息则清理空 metadata

    async def _fallback_decompose(self, task_id: str, confirmed_goal: str) -> dict[str, Any]:
        """LLM 失败兜底——生成单节点「执行 {confirmed_goal}」"""
        node = {
            "id": f"n{_uuid.uuid4().hex[:8]}",
            "parent_id": None,
            "level": 0,
            "title": confirmed_goal[:80] if confirmed_goal else "执行任务",
            "description": confirmed_goal,
            "acceptance_criteria": ["完成目标"],
            "dependencies": [],
            "status": "pending",
        }
        written_ids = await create_plan_tree(self._conn, task_id, [node])
        written = await get_plan_tree_by_task(self._conn, task_id)
        written_set = set(written_ids)
        nodes = [n for n in written if n["id"] in written_set]
        return {
            "nodes": nodes,
            "root_count": 1,
            "total": 1,
            "source": "fallback",
        }

    async def _fallback_subtree(
        self, task_id: str, parent_id: str, parent_level: int, sub_goal: str,
    ) -> list[dict]:
        """regenerate_subtree LLM 失败兜底——生成单子节点"""
        child_level = min(parent_level + 1, _MAX_LEVEL)
        node = {
            "id": f"n{_uuid.uuid4().hex[:8]}",
            "parent_id": parent_id,
            "level": child_level,
            "title": sub_goal[:LOG_PREVIEW_CMD_LEN] if sub_goal else "执行子任务",
            "description": sub_goal,
            "acceptance_criteria": ["完成子目标"],
            "dependencies": [],
            "status": "pending",
        }
        await create_plan_tree(self._conn, task_id, [node])
        return [node]


# 全局实例引用（lifespan 注入后赋值）
_decomposer: TaskDecomposer | None = None


def get_decomposer() -> TaskDecomposer | None:
    """获取全局 TaskDecomposer 实例（路由层用）"""
    return _decomposer


def set_decomposer(decomposer: TaskDecomposer) -> None:
    """注入全局 TaskDecomposer 实例（lifespan 调用）"""
    global _decomposer
    _decomposer = decomposer
