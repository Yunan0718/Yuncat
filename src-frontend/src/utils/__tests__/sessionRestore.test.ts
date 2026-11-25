import { describe, it, expect, vi, beforeEach } from "vitest";
import { switchSessionFromApi, restoreSessionFromApi, type SessionApi } from "../sessionRestore";
import { useTaskStore } from "../../store/taskStore";

/**
 * Batch 4a F1: 会话切换竞态防护测试。
 *
 * 场景：快速切换 A→B，A 的 fetch 慢、B 的快——B 先落地后，A 的旧响应后到，
 * 必须被丢弃（序号过期 + AbortController），不得覆盖 B 的 currentSession/tasks。
 */

const msgsOf = (sid: string) => [
  { role: "user", content: `user-${sid}`, id: `u-${sid}`, agent_id: "hermes" },
  { role: "assistant", content: `reply-${sid}`, id: `a-${sid}`, agent_id: "hermes" },
];

beforeEach(() => {
  useTaskStore.setState({ tasks: {}, currentSessionId: null });
});

describe("Batch 4a F1: switchSessionFromApi 竞态防护", () => {
  it("快速切换 A→B：B 先落地，A 慢响应后到被丢弃", async () => {
    let resolveA!: (v: unknown) => void;
    let resolveB!: (v: unknown) => void;
    const apiMock = vi.fn((path: string, _opts?: RequestInit) =>
      path.includes("sess-a")
        ? new Promise((r) => {
            resolveA = r;
          })
        : new Promise((r) => {
            resolveB = r;
          }),
    );
    const api = apiMock as unknown as SessionApi;

    const pA = switchSessionFromApi("sess-a", api);
    const pB = switchSessionFromApi("sess-b", api);

    // 新调用发起时 abort 上一个未完成请求
    expect((apiMock.mock.calls[0][1] as RequestInit).signal?.aborted).toBe(true);
    expect((apiMock.mock.calls[1][1] as RequestInit).signal?.aborted).toBe(false);

    resolveB(msgsOf("sess-b"));
    await pB;
    expect(useTaskStore.getState().currentSessionId).toBe("sess-b");
    expect(Object.keys(useTaskStore.getState().tasks)).toEqual(["u-sess-b"]);

    resolveA(msgsOf("sess-a"));
    await pA;
    // A 的响应后到——序号已过期，整体丢弃，不覆盖 B
    expect(useTaskStore.getState().currentSessionId).toBe("sess-b");
    expect(Object.keys(useTaskStore.getState().tasks)).toEqual(["u-sess-b"]);
  });

  it("被 abort 的请求错误被静默丢弃（不向上抛）", async () => {
    const api = vi.fn(
      (_path: string, opts?: RequestInit) =>
        new Promise((_res, rej) => {
          opts?.signal?.addEventListener("abort", () => rej(new Error("aborted")));
        }),
    ) as unknown as SessionApi;
    const pA = switchSessionFromApi("sess-a", api); // 挂起，等待被 abort
    const pB = switchSessionFromApi(
      "sess-b",
      vi.fn(async () => msgsOf("sess-b")) as unknown as SessionApi,
    );
    await pA; // abort → 静默 return，不 reject
    await pB;
    expect(useTaskStore.getState().currentSessionId).toBe("sess-b");
  });

  it("真实网络错误向上抛出供调用方记录", async () => {
    const api = vi.fn(async () => {
      throw new Error("network down");
    }) as unknown as SessionApi;
    await expect(switchSessionFromApi("sess-a", api)).rejects.toThrow("network down");
  });
});

describe("Batch 4a F1: restoreSessionFromApi 直接应用入口（保留）", () => {
  it("应用给定消息到 taskStore", async () => {
    await restoreSessionFromApi("sess-c", msgsOf("sess-c"));
    expect(useTaskStore.getState().currentSessionId).toBe("sess-c");
    const task = useTaskStore.getState().tasks["u-sess-c"];
    expect(task?.chunks.join("")).toContain("reply-sess-c");
  });
});
