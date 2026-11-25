import { describe, it, expect, vi } from "vitest";
import { saveWorkflowGraph } from "../workflowSave";

/**
 * 保存画布的三种分支——模板必须"另存为"，不能覆盖内置模板。
 * 此前从模板加载后 selectedId 指向模板 id，点保存会 PUT 覆盖模板本身。
 */
describe("saveWorkflowGraph", () => {
  it("普通工作流：PUT 原地更新", async () => {
    const api = vi.fn().mockResolvedValue({});
    const result = await saveWorkflowGraph(api, {
      selectedId: "wf-1", isTemplate: false, name: "mine", graph: { nodes: [], edges: [] },
    });
    expect(api).toHaveBeenCalledWith("/api/v1/workflows/wf-1", expect.objectContaining({ method: "PUT" }));
    expect(result).toEqual({ id: "wf-1", created: false, copiedFromTemplate: false });
  });

  it("从模板加载：POST 新建，模板保持不变", async () => {
    const api = vi.fn().mockResolvedValue({ workflow_id: "wf-new" });
    const result = await saveWorkflowGraph(api, {
      selectedId: "tmpl-1", isTemplate: true, name: "copy", graph: { nodes: [], edges: [] },
    });
    expect(api).toHaveBeenCalledWith("/api/v1/workflows", expect.objectContaining({ method: "POST" }));
    expect(api).not.toHaveBeenCalledWith(
      "/api/v1/workflows/tmpl-1", expect.anything(),
    );
    expect(result).toEqual({ id: "wf-new", created: true, copiedFromTemplate: true });
  });

  it("新画布：POST 新建", async () => {
    const api = vi.fn().mockResolvedValue({ workflow_id: "wf-new" });
    const result = await saveWorkflowGraph(api, {
      selectedId: null, isTemplate: false, name: "new", graph: { nodes: [], edges: [] },
    });
    expect(result.created).toBe(true);
    expect(result.copiedFromTemplate).toBe(false);
  });
});
