/**
 * Batch 4a F3: 终端 WS 重连 guard 状态机测试。
 *
 * 修复背景：原 wsStrictGuard 置 true 后全文件无重置，onclose 的 3s 重连
 * connectWS 被 guard 挡死——后端重启后终端永久失联。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { WsConnectionGuard } from "../workspace/TerminalPanel";

describe("Batch 4a F3: WsConnectionGuard", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("连接尝试在途时拒绝并发尝试", () => {
    const connect = vi.fn();
    const guard = new WsConnectionGuard(connect, 3000);
    expect(guard.tryConnect()).toBe(true);
    expect(guard.tryConnect()).toBe(false);
    expect(connect).toHaveBeenCalledTimes(1);
  });

  it("auth 成功的连接 onclose 后 3s 触发重连（原 bug：guard 挡死永不重连）", () => {
    const connect = vi.fn();
    const guard = new WsConnectionGuard(connect, 3000);
    guard.tryConnect();
    guard.onOpen();
    guard.onClose(true, () => true);

    vi.advanceTimersByTime(2999);
    expect(connect).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(1);
    expect(connect).toHaveBeenCalledTimes(2); // 3s 后重连被调用
  });

  it("重连路径再次受 guard 保护（重连尝试在途时不会叠加连接）", () => {
    const connect = vi.fn();
    const guard = new WsConnectionGuard(connect, 3000);
    guard.tryConnect();
    guard.onOpen();
    guard.onClose(true, () => true);
    vi.advanceTimersByTime(3000); // 触发重连（第 2 次 connect，尝试在途）
    expect(guard.tryConnect()).toBe(false); // 在途时被拒
    expect(connect).toHaveBeenCalledTimes(2);
  });

  it("auth 未成功的连接 onclose 后不重连（握手 403 场景）", () => {
    const connect = vi.fn();
    const guard = new WsConnectionGuard(connect, 3000);
    guard.tryConnect();
    guard.onOpen();
    guard.onClose(false, () => true);
    vi.advanceTimersByTime(6000);
    expect(connect).toHaveBeenCalledTimes(1);
  });

  it("canReconnect 为 false（backendReady=false）时不重连", () => {
    const connect = vi.fn();
    const guard = new WsConnectionGuard(connect, 3000);
    guard.tryConnect();
    guard.onOpen();
    guard.onClose(true, () => false);
    vi.advanceTimersByTime(3000);
    expect(connect).toHaveBeenCalledTimes(1);
  });

  it("dispose 取消 pending 重连并释放尝试槽（StrictMode 重挂载安全）", () => {
    const connect = vi.fn();
    const guard = new WsConnectionGuard(connect, 3000);
    guard.tryConnect();
    guard.onOpen();
    guard.onClose(true, () => true);
    guard.dispose();
    vi.advanceTimersByTime(10000);
    expect(connect).toHaveBeenCalledTimes(1);
    expect(guard.tryConnect()).toBe(true); // 槽位已释放，可再次发起
  });
});
