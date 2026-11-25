"""services/spec_orchestrator.py —— Phase 45 D.3 多 Spec 编排器

对标 OpenAI Symphony: 管理有依赖关系的多个 Spec，按拓扑排序分层并行执行。
单 Spec = 单模块时不需要此编排器——等有 10+ Spec 积累后再用。

核心算法:
  - _topological_sort(): Kahn 算法 → 返回分层列表
  - _detect_conflicts():  两个 Spec 改了同一个文件 → 冲突报警
"""
import asyncio
import logging

_log = logging.getLogger("yuncat.spec_orchestrator")


class SpecOrchestrator:
    """多 Spec 编排器——按依赖图拓扑排序并行/串行执行。

    specs 格式:
      [{"id": "spec-auth", "yaml": "...", "priority": 1}, ...]

    dependency_graph 格式:
      {"spec-api": ["spec-auth", "spec-db"]}
        → spec-api 依赖 spec-auth 和 spec-db 先完成
    """

    def __init__(self, deliberation_service, db=None):
        self._ds = deliberation_service
        self._db = db

    async def _route_spec_to_backend(self, spec: dict) -> str:
        """Phase 43 H.5: 按 Spec 内容选择后端。

        vision/UI/设计类 Spec → openclaw
        code/architecture/security → hermes
        无法确定 → hermes (default)
        """
        prompt = spec.get("yaml", spec.get("task_input", ""))[:500]
        if not prompt:
            return "hermes"
        pl = prompt.lower()
        vision_keywords = ["ui", "ux", "视觉", "frontend", "design", "widget", "layout", "style", "界面", "截图", "图片"]
        for kw in vision_keywords:
            if kw in pl:
                return "openclaw"
        return "hermes"

    async def run_spec_project(
        self, specs: list[dict], dependency_graph: dict[str, list[str]],
        push_callback=None,
    ) -> dict:
        """执行一组有依赖关系的 Spec——按拓扑顺序分层并行执行。

        Returns: {results: dict[spec_id → result], total_specs, layers}
        """
        if len(specs) == 1:
            # 单 Spec → 直接跑，不需要拓扑
            spec = specs[0]
            backend = await self._route_spec_to_backend(spec)  # Phase 48-G: 复用 backend 路由作为 agent_id
            result = await self._ds.run_spec_loop(
                spec.get("yaml", spec.get("task_input", "")),
                spec.get("id", spec.get("task_id", "")),
                push_callback=push_callback,
                agent_id=backend,  # Phase 48-G: 铁律 #1 穿透
            )
            return {"results": {spec.get("id"): result}, "total_specs": 1, "layers": 1}

        # 拓扑排序
        try:
            execution_layers = self._topological_sort(specs, dependency_graph)
        except ValueError as e:
            return {"error": f"依赖图有循环——无法拓扑排序: {e}", "results": {}}

        results: dict[str, dict] = {}
        conflicts: list[dict] = []

        for layer_idx, layer in enumerate(execution_layers):
            _log.info("Spec orchestrator: layer %d/%d (%d specs)",
                     layer_idx + 1, len(execution_layers), len(layer))

            # Phase: 同层 Spec 并行执行 — 不同 Spec 路由到不同后端
            tasks = []
            for spec in layer:
                backend = await self._route_spec_to_backend(spec)
                _log.info("Spec '%s' routed to backend '%s'", spec.get("id", "?")[:20], backend)
                tasks.append(
                    self._ds.run_spec_loop(
                        spec.get("yaml", spec.get("task_input", "")),
                        spec.get("id", spec.get("task_id", "")),
                        push_callback=push_callback,
                        preferred_backend=backend,
                        agent_id=backend,  # Phase 48-G: 铁律 #1 穿透——复用 backend 路由结果
                    )
                )
            layer_results = await asyncio.gather(*tasks, return_exceptions=True)

            for spec, result in zip(layer, layer_results):
                spec_id = spec.get("id", spec.get("task_id", ""))
                results[spec_id] = (
                    {"error": str(result)} if isinstance(result, Exception)
                    else result or {}
                )

                # 检查冲突——此 Spec 的依赖是否与它改了相同文件
                deps = dependency_graph.get(spec_id, [])
                for dep_id in deps:
                    if dep_id in results and spec_id in results:
                        _conflicts = self._detect_conflicts(
                            results[dep_id], results[spec_id],
                        )
                        if _conflicts:
                            conflicts.append({
                                "spec_a": dep_id, "spec_b": spec_id,
                                "files": _conflicts,
                            })

        return {
            "results": results,
            "total_specs": len(specs),
            "layers": len(execution_layers),
            "conflicts": conflicts,
        }

    @staticmethod
    def _topological_sort(
        specs: list[dict], dep_graph: dict[str, list[str]],
    ) -> list[list[dict]]:
        """Kahn 算法——拓扑排序，返回分层列表。

        第一层=无依赖的 Spec（可并行），第二层=依赖第一层的 Spec，以此类推。

        Raises ValueError if the graph has a cycle.
        """
        spec_ids = {s.get("id", s.get("task_id", "")) for s in specs}
        spec_map = {s.get("id", s.get("task_id", "")): s for s in specs}

        # 计算入度
        in_degree: dict[str, int] = {sid: 0 for sid in spec_ids}
        for sid, deps in dep_graph.items():
            if sid not in spec_ids:
                continue
            for dep in deps:
                if dep in spec_ids:
                    in_degree[sid] = in_degree.get(sid, 0) + 1

        # 入度 0 → 第一层
        queue = [sid for sid, deg in in_degree.items() if deg == 0]
        if not queue and spec_ids:
            raise ValueError("Graph has a cycle—no nodes with in-degree 0")

        layers: list[list[dict]] = []
        processed: set[str] = set()

        while queue:
            layer = [spec_map[sid] for sid in queue if sid in spec_map]
            layers.append(layer)
            processed.update(queue)

            next_queue: list[str] = []
            for sid in queue:
                for other, deps in dep_graph.items():
                    if sid in deps and other in spec_ids:
                        in_degree[other] -= 1
                        if in_degree[other] == 0 and other not in processed:
                            next_queue.append(other)
            queue = next_queue

        if len(processed) < len(spec_ids):
            remaining = spec_ids - processed
            raise ValueError(f"Graph has a cycle involving: {remaining}")

        return layers

    @staticmethod
    def _detect_conflicts(spec_a_result: dict, spec_b_result: dict) -> list[str]:
        """检测两个 Spec 是否改了同一个文件。

        从 spec result 中提取 files_touched，取交集。
        """
        files_a = set(
            spec_a_result.get("files_touched", [])
        )
        files_b = set(
            spec_b_result.get("files_touched", [])
        )
        # 如果 result 中是 audit_report → 取 files_touched 字段
        _audit_a = spec_a_result.get("audit_report", {})
        _audit_b = spec_b_result.get("audit_report", {})
        if isinstance(_audit_a, dict):
            files_a |= set(_audit_a.get("files_touched", []))
        if isinstance(_audit_b, dict):
            files_b |= set(_audit_b.get("files_touched", []))

        return list(files_a & files_b)
