import { describe, expect, it, vi, beforeEach } from "vitest";

const { emit } = vi.hoisted(() => ({ emit: vi.fn() }));

vi.mock("../../../components/common/toast-context", () => ({
  emitGlobalToast: (message: string, type?: string) => emit(message, type),
}));

vi.mock("i18next", () => ({ default: { t: (key: string) => key } }));

import { dispatchToStore } from "../index";

/**
 * 后端推了、前端没接的事件——消息被静默丢弃，用户看不到任何反馈。
 *
 * `memory_saved`：`#记住 xxx` 写完长期记忆后的确认。记忆确实存进 DB 了，
 * 但界面毫无反应，用户只觉得自己"发了条消息没人理"。
 * `slash_command_matched`：服务端斜杠命令被 DAG/工具路由接走时的回执。
 */
describe("后端回执类事件", () => {
  beforeEach(() => {
    emit.mockReset();
  });

  it("memory_saved 给出成功提示", () => {
    dispatchToStore({ type: "memory_saved", message: "已记住: 我喜欢简洁的回答" });
    expect(emit).toHaveBeenCalledTimes(1);
    expect(emit.mock.calls[0][0]).toContain("我喜欢简洁的回答");
    expect(emit.mock.calls[0][1]).toBe("success");
  });

  it("memory_saved 没有文案时也有兜底提示", () => {
    dispatchToStore({ type: "memory_saved" });
    expect(emit).toHaveBeenCalledTimes(1);
    expect(emit.mock.calls[0][0]).toBe("chat.memorySaved");
  });

  it("slash_command_matched 告诉用户命令被谁接走了", () => {
    dispatchToStore({ type: "slash_command_matched", command: "/review", name: "review" });
    expect(emit).toHaveBeenCalledTimes(1);
    expect(emit.mock.calls[0][0]).toBe("chat.slashCommandMatched");
  });
});
