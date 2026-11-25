/**
 * Batch 8.5: useWebSocket outbox 队列测试
 *
 * 验证：
 * 1. WS OPEN 时 send 直接发送，不入队
 * 2. WS 断开时 send 入队，outboxCount 更新
 * 3. 重连后 flushOutbox 清空队列并发送
 *
 * 对标 Discord / Telegram 离线消息队列测试模式。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// ---- Mock stores ----
const _appState: Record<string, unknown> = {};
vi.mock("../../store/appStore", () => ({
  useAppStore: {
    getState: () => _appState,
    setState: (patch: Record<string, unknown>) => Object.assign(_appState, patch),
  },
}));

vi.mock("../../store/taskStore", () => ({
  useTaskStore: {
    getState: () => ({ tasks: {} }),
  },
}));

vi.mock("../wsHandlers", () => ({
  dispatchToStore: vi.fn(),
  getNotifiedSet: () => new Set(),
}));

// ---- Mock WebSocket ----
interface MockWSInstance {
  readyState: number;
  send: ReturnType<typeof vi.fn>;
  addEventListener: ReturnType<typeof vi.fn>;
  close: ReturnType<typeof vi.fn>;
  onopen: ((ev: Event) => void) | null;
  onmessage: ((ev: MessageEvent) => void) | null;
  onclose: (() => void) | null;
  onerror: (() => void) | null;
}

const MockWebSocket = vi.fn().mockImplementation((): MockWSInstance => ({
  readyState: 0, // CONNECTING
  send: vi.fn(),
  addEventListener: vi.fn(),
  close: vi.fn(),
  onopen: null,
  onmessage: null,
  onclose: null,
  onerror: null,
}));

// Patch static constants
(MockWebSocket as unknown as { OPEN: number; CONNECTING: number; CLOSING: number; CLOSED: number }).OPEN = 1;
(MockWebSocket as unknown as { OPEN: number; CONNECTING: number; CLOSING: number; CLOSED: number }).CONNECTING = 0;
(MockWebSocket as unknown as { OPEN: number; CONNECTING: number; CLOSING: number; CLOSED: number }).CLOSING = 2;
(MockWebSocket as unknown as { OPEN: number; CONNECTING: number; CLOSING: number; CLOSED: number }).CLOSED = 3;

import { WsConnection } from "../useWebSocket";

describe("WsConnection outbox (Batch 8.5)", () => {
  let originalWebSocket: typeof WebSocket;

  beforeEach(() => {
    originalWebSocket = global.WebSocket;
    global.WebSocket = MockWebSocket as unknown as typeof WebSocket;
    // 清理 appStore 状态
    Object.keys(_appState).forEach((k) => delete _appState[k]);
    MockWebSocket.mockClear();
  });

  afterEach(() => {
    global.WebSocket = originalWebSocket;
  });

  it("WS OPEN 时 send 直接发送，不入队", () => {
    const conn = new WsConnection();
    // 模拟 ws 已连接且 OPEN
    const mockWs = { readyState: 1, send: vi.fn() } as unknown as WebSocket;
    (conn as unknown as { ws: WebSocket }).ws = mockWs;

    conn.send({ type: "test", data: "hello" });

    expect(mockWs.send).toHaveBeenCalledWith(JSON.stringify({ type: "test", data: "hello" }));
    expect(_appState.outboxCount).toBeUndefined();
  });

  it("WS 断开时 send 入队，outboxCount 更新", () => {
    const conn = new WsConnection();
    // ws 为 null——模拟断开
    (conn as unknown as { ws: WebSocket | null }).ws = null;

    conn.send({ type: "test", data: "msg1" });
    conn.send({ type: "test", data: "msg2" });

    expect(_appState.outboxCount).toBe(2);
  });

  it("重连后 flushOutbox 清空队列并发送", () => {
    const conn = new WsConnection();
    // 先入队（ws = null）
    (conn as unknown as { ws: WebSocket | null }).ws = null;
    conn.send({ type: "test", data: "msg1" });
    conn.send({ type: "test", data: "msg2" });
    expect(_appState.outboxCount).toBe(2);

    // 模拟重连成功——设置 ws 为 OPEN 状态
    const mockWs = { readyState: 1, send: vi.fn() } as unknown as WebSocket;
    (conn as unknown as { ws: WebSocket }).ws = mockWs;

    // 调用私有 flushOutbox
    (conn as unknown as { _flushOutbox: () => void })._flushOutbox();

    expect(mockWs.send).toHaveBeenCalledTimes(2);
    expect(mockWs.send).toHaveBeenCalledWith(JSON.stringify({ type: "test", data: "msg1" }));
    expect(mockWs.send).toHaveBeenCalledWith(JSON.stringify({ type: "test", data: "msg2" }));
    expect(_appState.outboxCount).toBe(0);
  });
});
