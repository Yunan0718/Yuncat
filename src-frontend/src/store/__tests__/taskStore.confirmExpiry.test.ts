import { describe, it, expect, beforeEach } from "vitest";
import { useTaskStore } from "../taskStore";

/**
 * Batch 4b P3: 确认弹窗队列过期联动
 *
 * - resolveConfirmDialog 提升下一个弹窗时跳过已过期项（resolve(false) 出队，不弹 404）
 * - dropConfirmByToken（后端 tool_confirm_expired 事件）：
 *   - 移除当前单弹窗并提升下一个
 *   - 从队列移除匹配 token 的单弹窗
 *   - 从批量弹窗 items 中剥离匹配项；剥空则整弹窗出队并提升
 */
describe("Batch 4b: taskStore 确认弹窗过期联动", () => {
  beforeEach(() => {
    useTaskStore.setState({
      tasks: {},
      confirmDialog: null,
      confirmQueue: [],
    });
  });

  function nowSec() {
    return Date.now() / 1000;
  }

  it("resolveConfirmDialog 跳过队列中已过期的项并提升下一个未过期项", async () => {
    // 第一个调用成为当前弹窗，后续进队列（FIFO）
    const currentPromise = useTaskStore.getState().showConfirmDialog(
      "t0", "run_command", {}, "tok-current", nowSec() + 600, "当前任务",
    );
    const expiredPromise = useTaskStore.getState().showConfirmDialog(
      "t1", "write_file", {}, "tok-expired", nowSec() - 5, "过期任务",
    );
    const freshPromise = useTaskStore.getState().showConfirmDialog(
      "t2", "edit_file", {}, "tok-fresh", nowSec() + 600, "后续任务",
    );

    // 关闭当前 → 队列头已过期应被跳过（resolve(false)），直接展示 fresh
    useTaskStore.getState().resolveConfirmDialog(true);

    await expect(currentPromise).resolves.toBe(true);
    await expect(expiredPromise).resolves.toBe(false); // 过期项自动拒绝

    const state = useTaskStore.getState();
    expect(state.confirmDialog?.confirmToken).toBe("tok-fresh");
    expect(state.confirmQueue).toHaveLength(0);

    // 清理：关闭 fresh
    useTaskStore.getState().resolveConfirmDialog(false);
    await expect(freshPromise).resolves.toBe(false);
  });

  it("dropConfirmByToken 移除当前单弹窗（resolve(false)）并提升下一个", async () => {
    const currentPromise = useTaskStore.getState().showConfirmDialog(
      "t1", "write_file", {}, "tok-cur", nowSec() + 600, "a",
    );
    const nextPromise = useTaskStore.getState().showConfirmDialog(
      "t2", "run_command", {}, "tok-next", nowSec() + 600, "b",
    );

    useTaskStore.getState().dropConfirmByToken("tok-cur");

    await expect(currentPromise).resolves.toBe(false);
    const state = useTaskStore.getState();
    expect(state.confirmDialog?.confirmToken).toBe("tok-next");
    expect(state.confirmQueue).toHaveLength(0);

    useTaskStore.getState().resolveConfirmDialog(false);
    await expect(nextPromise).resolves.toBe(false);
  });

  it("dropConfirmByToken 从队列移除匹配的单弹窗，不影响其他", async () => {
    const headPromise = useTaskStore.getState().showConfirmDialog(
      "t0", "run_command", {}, "tok-head", nowSec() + 600, "a",
    );
    const doomedPromise = useTaskStore.getState().showConfirmDialog(
      "t1", "write_file", {}, "tok-doomed", nowSec() + 600, "b",
    );
    const keepPromise = useTaskStore.getState().showConfirmDialog(
      "t2", "edit_file", {}, "tok-keep", nowSec() + 600, "c",
    );

    useTaskStore.getState().dropConfirmByToken("tok-doomed");

    await expect(doomedPromise).resolves.toBe(false);
    const state = useTaskStore.getState();
    expect(state.confirmDialog?.confirmToken).toBe("tok-head");
    expect(state.confirmQueue.map((d) => d.confirmToken)).toEqual(["tok-keep"]);

    // 清理两轮：head → keep
    useTaskStore.getState().resolveConfirmDialog(false);
    useTaskStore.getState().resolveConfirmDialog(false);
    await expect(headPromise).resolves.toBe(false);
    await expect(keepPromise).resolves.toBe(false);
  });

  it("dropConfirmByToken 从批量弹窗 items 剥离匹配 token；剥空则整弹窗出队", async () => {
    const batchPromise = useTaskStore.getState().showConfirmBatchDialog("t1", [
      { toolName: "write_file", preview: {}, confirmToken: "b-1", expiresAt: nowSec() + 600 },
      { toolName: "run_command", preview: {}, confirmToken: "b-2", expiresAt: nowSec() + 600 },
    ]);

    // 剥离 b-1 —— 批量弹窗仍保留 1 项
    useTaskStore.getState().dropConfirmByToken("b-1");
    let state = useTaskStore.getState();
    expect(state.confirmDialog?.items?.map((i) => i.confirmToken)).toEqual(["b-2"]);

    // 再剥离 b-2 —— 批量弹窗整弹出队
    useTaskStore.getState().dropConfirmByToken("b-2");
    state = useTaskStore.getState();
    expect(state.confirmDialog).toBeNull();
    await expect(batchPromise).resolves.toBe(false);
  });

  it("dropConfirmByToken 对不存在的 token 无副作用", async () => {
    const p = useTaskStore.getState().showConfirmDialog(
      "t1", "write_file", {}, "tok-x", nowSec() + 600, "a",
    );
    useTaskStore.getState().dropConfirmByToken("tok-not-exist");
    expect(useTaskStore.getState().confirmDialog?.confirmToken).toBe("tok-x");
    useTaskStore.getState().resolveConfirmDialog(true);
    await expect(p).resolves.toBe(true);
  });
});
