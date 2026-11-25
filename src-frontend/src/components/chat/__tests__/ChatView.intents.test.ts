/**
 * Batch 4a F7: new-chat / deep-link CustomEvent 桥接测试。
 *
 * 链路：主进程 IPC（preload onNewChat/onDeepLink）→ App.tsx dispatch
 * CustomEvent → ChatView attachChatIntents 消费。此处直接测消费端注册函数。
 */
import { describe, it, expect, vi } from "vitest";
import { attachChatIntents } from "../ChatView";

describe("Batch 4a F7: attachChatIntents", () => {
  it("yuncat:new-chat 事件触发 handleNewChat，detach 后不再触发", () => {
    const onNewChat = vi.fn();
    const detach = attachChatIntents({ onNewChat, onDeepLink: vi.fn() });

    window.dispatchEvent(new CustomEvent("yuncat:new-chat"));
    expect(onNewChat).toHaveBeenCalledTimes(1);

    detach();
    window.dispatchEvent(new CustomEvent("yuncat:new-chat"));
    expect(onNewChat).toHaveBeenCalledTimes(1);
  });

  it("yuncat:deep-link 事件携带 payload 触发回调；无 detail 不触发", () => {
    const onDeepLink = vi.fn();
    const detach = attachChatIntents({ onNewChat: vi.fn(), onDeepLink });

    window.dispatchEvent(
      new CustomEvent("yuncat:deep-link", { detail: { prompt: "hi yuncat", agent: "hermes" } }),
    );
    expect(onDeepLink).toHaveBeenCalledWith({ prompt: "hi yuncat", agent: "hermes" });

    window.dispatchEvent(new CustomEvent("yuncat:deep-link")); // 无 detail
    expect(onDeepLink).toHaveBeenCalledTimes(1);

    detach();
  });
});
