import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

import { LspClient } from "../lspClient";

/**
 * LSP 客户端的边界：握手、请求响应配对、服务端反向请求、通知、超时、释放。
 * 这几条任意一条错，用户看到的就是「补全一直转圈」或「弹出一堆报错」。
 */

class FakeSocket {
  static instances: FakeSocket[] = [];
  static readonly OPEN = 1;
  readyState = 0;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;

  constructor(public url: string) {
    FakeSocket.instances.push(this);
  }
  send(data: string) { this.sent.push(data); }
  close() { this.readyState = 3; this.onclose?.(); }
  open() { this.readyState = FakeSocket.OPEN; this.onopen?.(); }
  emit(payload: unknown) { this.onmessage?.({ data: JSON.stringify(payload) }); }
  frames() { return this.sent.map((raw) => JSON.parse(raw)); }
}

describe("LspClient", () => {
  beforeEach(() => {
    FakeSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeSocket as unknown as typeof WebSocket);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function connectClient(timeoutMs = 1000) {
    const client = new LspClient("ws://127.0.0.1:1/ws/lsp?language=python", "tok", timeoutMs);
    const promise = client.connect(1000);
    const socket = FakeSocket.instances.at(-1)!;
    return { client, promise, socket };
  }

  it("先发 auth，收到 ready 才算连上，并带回 capabilities", async () => {
    const { promise, socket } = connectClient();
    socket.open();
    expect(socket.frames()[0]).toEqual({ type: "auth", token: "tok" });
    socket.emit({ type: "auth_ok" });
    socket.emit({ type: "ready", capabilities: { hoverProvider: true } });
    await expect(promise).resolves.toEqual({ hoverProvider: true });
  });

  it("服务端说不可用时要带上原因，而不是空错误", async () => {
    const { promise, socket } = connectClient();
    socket.open();
    socket.emit({ type: "unavailable", reason: "未找到 Node.js" });
    await expect(promise).rejects.toThrow("未找到 Node.js");
  });

  it("请求与响应按 id 配对", async () => {
    const { client, promise, socket } = connectClient();
    socket.open();
    socket.emit({ type: "ready", capabilities: {} });
    await promise;

    const hover = client.request("textDocument/hover", { line: 1 });
    const frame = socket.frames().at(-1);
    expect(frame.type).toBe("lsp");
    expect(frame.message.method).toBe("textDocument/hover");

    socket.emit({ type: "lsp", message: { jsonrpc: "2.0", id: frame.message.id, result: { ok: 1 } } });
    await expect(hover).resolves.toEqual({ ok: 1 });
  });

  it("错误响应会变成可读异常", async () => {
    const { client, promise, socket } = connectClient();
    socket.open();
    socket.emit({ type: "ready", capabilities: {} });
    await promise;

    const request = client.request("textDocument/rename", {});
    const id = socket.frames().at(-1).message.id;
    socket.emit({ type: "lsp", message: { jsonrpc: "2.0", id, error: { message: "boom" } } });
    await expect(request).rejects.toThrow("boom");
  });

  it("服务端反向请求必须被回包（不回会卡住整个会话）", async () => {
    const { promise, socket } = connectClient();
    socket.open();
    socket.emit({ type: "ready", capabilities: {} });
    await promise;

    socket.emit({
      type: "lsp",
      message: {
        jsonrpc: "2.0", id: "srv-1", method: "workspace/configuration",
        params: { items: [{ section: "python" }, { section: "pyright" }] },
      },
    });
    const answer = socket.frames().at(-1);
    expect(answer.message.id).toBe("srv-1");
    expect(answer.message.result).toEqual([null, null]);
  });

  it("通知分发给订阅者", async () => {
    const { client, promise, socket } = connectClient();
    socket.open();
    socket.emit({ type: "ready", capabilities: {} });
    await promise;

    const seen: Array<[string, unknown]> = [];
    client.onNotification((method, params) => seen.push([method, params]));
    socket.emit({
      type: "lsp",
      message: { jsonrpc: "2.0", method: "textDocument/publishDiagnostics", params: { uri: "a" } },
    });
    expect(seen).toEqual([["textDocument/publishDiagnostics", { uri: "a" }]]);
  });

  it("请求超时要失败，不能永远转圈", async () => {
    const { client, promise, socket } = connectClient(20);
    socket.open();
    socket.emit({ type: "ready", capabilities: {} });
    await promise;
    await expect(client.request("textDocument/hover", {})).rejects.toThrow(/超时/);
  });

  it("释放后未完成的请求会立刻失败", async () => {
    const { client, promise, socket } = connectClient();
    socket.open();
    socket.emit({ type: "ready", capabilities: {} });
    await promise;
    const request = client.request("textDocument/hover", {});
    client.dispose();
    await expect(request).rejects.toThrow();
  });
});
