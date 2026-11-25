import { describe, expect, it, beforeEach } from "vitest";

import { useTaskStore } from "../../../store/taskStore";
import { dispatchToStore } from "../index";

/**
 * 回归：失败必须留在界面上（2026-09-16）。
 *
 * 线上症状是「发完消息只有一个空的回复框，既没有内容也没有原因」。
 * 根因是后端在 stream_error 之后还会发一个 stream_done（表示流结束了），
 * 而 setTaskDone 会把 failed 改写成 completed —— 失败信息随即从界面上消失。
 * 这里从事件分发层一路断言到 store 状态。
 */
const TASK_ID = "task-error-regression";

function seedTask() {
  useTaskStore.setState({ tasks: {}, activeTaskId: null });
  useTaskStore.getState().createTask(TASK_ID, "yuncat", "测试", "用户指定");
}

describe("stream_error 之后的 stream_done", () => {
  beforeEach(seedTask);

  it("失败状态与原因都要留下，不能被 done 覆盖", () => {
    dispatchToStore({
      type: "stream_error",
      task_id: TASK_ID,
      error: "Authentication Fails, Your api key: ****302c is invalid",
      error_code: "AUTH",
    });
    const afterError = useTaskStore.getState().tasks[TASK_ID];
    expect(afterError.status).toBe("failed");
    expect(afterError.errorMessage).toContain("302c");
    expect(afterError.errorCode).toBe("AUTH");

    dispatchToStore({ type: "stream_done", task_id: TASK_ID });
    const afterDone = useTaskStore.getState().tasks[TASK_ID];
    expect(afterDone.status).toBe("failed");
    expect(afterDone.errorMessage).toContain("302c");
    expect(afterDone.errorCode).toBe("AUTH");
    expect(afterDone.isStreaming).toBe(false);
  });

  it("正常完成仍然会变成 completed（别把修复做成回归）", () => {
    dispatchToStore({ type: "stream_chunk", task_id: TASK_ID, data: "你好" });
    dispatchToStore({ type: "stream_done", task_id: TASK_ID });
    const task = useTaskStore.getState().tasks[TASK_ID];
    expect(task.status).toBe("completed");
    expect(task.errorMessage).toBeNull();
  });
});
