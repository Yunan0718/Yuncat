import { describe, expect, it, beforeEach } from "vitest";

import { useTaskStore } from "../../../store/taskStore";
import { dispatchToStore } from "../index";

/**
 * 回归：状态栏的上下文用量必须是「真实用量」，不能是拍脑袋算出来的（2026-09-16）。
 *
 * 线上症状是状态栏写着 22964.4k / 131k。根因是 context_injected 把 history_limit
 * （预算值 57411）当成消息条数乘了 400。
 */
const TASK_ID = "task-ctx-regression";

describe("contextUsage", () => {
  beforeEach(() => {
    useTaskStore.setState({ tasks: {}, activeTaskId: null, contextUsage: { pct: 0, tokens: 0, window: 131072 } });
    useTaskStore.getState().createTask(TASK_ID, "yuncat", "测试", "用户指定");
  });

  it("context_injected 不再伪造 token 数，只更新窗口", () => {
    dispatchToStore({
      type: "context_injected", task_id: TASK_ID, summary: "会话 3 轮",
      history_limit: 57411, context_window: 131072,
    });
    const usage = useTaskStore.getState().contextUsage;
    expect(usage.tokens).toBe(0);
    expect(usage.window).toBe(131072);
  });

  it("token_usage 用 provider 报的真实 prompt_tokens 刷新占用", () => {
    dispatchToStore({ type: "token_usage", task_id: TASK_ID, prompt_tokens: 13107, completion_tokens: 100 });
    const usage = useTaskStore.getState().contextUsage;
    expect(usage.tokens).toBe(13107);
    expect(Math.round(usage.pct)).toBe(10);
  });

  it("占用比例不会超过 100%", () => {
    dispatchToStore({ type: "token_usage", task_id: TASK_ID, prompt_tokens: 999999, completion_tokens: 0 });
    expect(useTaskStore.getState().contextUsage.pct).toBeLessThanOrEqual(100);
  });
});
