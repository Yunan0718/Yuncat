/** lib/workflowSave.ts —— 保存工作流画布：模板一律"另存为"

为什么单独一个模块：
- 工作流编辑器从模板加载时会 `selectedId = 模板 id`，此时直接"保存"会
  用 PUT 覆盖**内置模板本身**——用户只是想基于模板做一份自己的流程。
- 这段分支逻辑放回 WorkflowEditor 会让超大页面继续增长（尺寸棘轮会拦），
  抽成纯函数也更好测。

规则：
| 场景 | 动作 |
|---|---|
| 普通工作流（非模板） | PUT 原地更新 |
| 从模板加载 | POST 新建（模板保持不变） |
| 新画布（无 selectedId） | POST 新建 |
*/

export interface WorkflowSaveResult {
  id: string;
  created: boolean;
  copiedFromTemplate: boolean;
}

type ApiFn = <T>(path: string, options?: RequestInit) => Promise<T>;

export async function saveWorkflowGraph(
  api: ApiFn,
  opts: {
    selectedId: string | null;
    isTemplate: boolean;
    name: string;
    graph: unknown;
  },
): Promise<WorkflowSaveResult> {
  const body = JSON.stringify({ name: opts.name, graph: opts.graph });
  if (opts.selectedId && !opts.isTemplate) {
    await api(`/api/v1/workflows/${opts.selectedId}`, { method: "PUT", body });
    return { id: opts.selectedId, created: false, copiedFromTemplate: false };
  }
  const result = await api<{ workflow_id: string }>("/api/v1/workflows", {
    method: "POST",
    body,
  });
  return {
    id: result.workflow_id,
    created: true,
    copiedFromTemplate: Boolean(opts.selectedId && opts.isTemplate),
  };
}
