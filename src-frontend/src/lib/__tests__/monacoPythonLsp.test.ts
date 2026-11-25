import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

import { attachMonacoPythonLsp, probePythonLsp } from "../monacoPythonLsp";

/** 极简假 Monaco：只提供这条链路真正碰到的那几个 API */
class FakeRange {
  constructor(
    public startLineNumber: number, public startColumn: number,
    public endLineNumber: number, public endColumn: number,
  ) {}
}

function makeMonaco() {
  return {
    Range: FakeRange,
    Uri: { parse: (value: string) => ({ toString: () => value }) },
    languages: {
      registerCompletionItemProvider: vi.fn(() => ({ dispose: vi.fn() })),
      registerHoverProvider: vi.fn(() => ({ dispose: vi.fn() })),
      registerDefinitionProvider: vi.fn(() => ({ dispose: vi.fn() })),
      registerDocumentSymbolProvider: vi.fn(() => ({ dispose: vi.fn() })),
      registerSignatureHelpProvider: vi.fn(() => ({ dispose: vi.fn() })),
      registerRenameProvider: vi.fn(() => ({ dispose: vi.fn() })),
    },
    editor: { getModel: vi.fn(() => null), setModelMarkers: vi.fn() },
  };
}

function makeModel(uri = "file:///w/a.py") {
  return {
    uri: { toString: () => uri },
    getValue: () => "x = 1",
    onDidChangeContent: () => ({ dispose: vi.fn() }),
  };
}

class FakeSocket {
  static instances: FakeSocket[] = [];
  readyState = 0;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  constructor(public url: string) { FakeSocket.instances.push(this); }
  send(data: string) { this.sent.push(data); }
  close() { this.readyState = 3; }
  open() { this.readyState = 1; this.onopen?.(); }
  emit(payload: unknown) { this.onmessage?.({ data: JSON.stringify(payload) }); }
  frames() { return this.sent.map((raw) => JSON.parse(raw)); }
}

/**
 * 等到 attach 内部**新**建出一个 WebSocket（探测请求要跨若干微任务）。
 * 必须比对数量：只看 at(-1) 会在第二次 attach 时拿到上一个还没关闭的连接，
 * 于是握手信号发错对象、测试挂死（踩过一次）。
 */
async function waitForNewSocket(previousCount: number, timeoutMs = 500): Promise<FakeSocket> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (FakeSocket.instances.length > previousCount) return FakeSocket.instances.at(-1)!;
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
  throw new Error("attach 没有创建新的 WebSocket");
}

describe("probePythonLsp", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("可用时返回 available", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ available: true, code: "" }) })));
    await expect(probePythonLsp(1234, "tok")).resolves.toEqual({ available: true, reason: "", code: "" });
  });

  it("不可用时把原因与代码一起带回来（前端据此出多语言文案）", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true, json: async () => ({ available: false, reason: "no node", code: "node-missing" }),
    })));
    await expect(probePythonLsp(1234, "tok")).resolves.toEqual({
      available: false, reason: "no node", code: "node-missing",
    });
  });

  it("HTTP 出错与网络异常都要落到不可用，而不是抛给调用方", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 500 })));
    expect((await probePythonLsp(1, "t")).available).toBe(false);
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("net down"); }));
    const result = await probePythonLsp(1, "t");
    expect(result.available).toBe(false);
    expect(result.code).toBe("probe-failed");
  });
});

describe("attachMonacoPythonLsp", () => {
  beforeEach(() => {
    FakeSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeSocket as unknown as typeof WebSocket);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("语言服务不可用时：不建连接、不注册 provider，只回报状态", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true, json: async () => ({ available: false, reason: "no node", code: "node-missing" }),
    })));
    const monaco = makeMonaco();
    const statuses: unknown[] = [];
    await attachMonacoPythonLsp({
      monaco: monaco as never, model: makeModel() as never, port: 1, token: "t",
      onStatus: (status) => statuses.push(status),
    });
    expect(FakeSocket.instances).toHaveLength(0);
    expect(monaco.languages.registerHoverProvider).not.toHaveBeenCalled();
    expect(statuses[0]).toEqual({ state: "preparing", reason: "" });
    expect(statuses[1]).toEqual({ state: "unavailable", reason: "no node", code: "node-missing" });
  });

  it("可用时：发 didOpen、注册 provider（重复挂载不重复注册）、诊断进 markers、释放发 didClose", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ available: true }) })));
    const monaco = makeMonaco();
    const model = makeModel();
    const statuses: Array<{ state: string }> = [];

    const before = FakeSocket.instances.length;
    const attach = attachMonacoPythonLsp({
      monaco: monaco as never, model: model as never, port: 1, token: "t",
      onStatus: (status) => statuses.push(status),
    });
    const socket = await waitForNewSocket(before);
    socket.open();
    socket.emit({ type: "ready", capabilities: {} });
    const handle = await attach;

    expect(socket.frames().some((f) => f.message?.method === "textDocument/didOpen")).toBe(true);
    expect(monaco.languages.registerHoverProvider).toHaveBeenCalledTimes(1);
    expect(statuses.at(-1)).toEqual({ state: "ready", reason: "" });

    // 第二次挂载（同一 monaco）：provider 不应该被再注册一遍
    const beforeSecond = FakeSocket.instances.length;
    const second = attachMonacoPythonLsp({
      monaco: monaco as never, model: model as never, port: 1, token: "t",
    });
    const secondSocket = await waitForNewSocket(beforeSecond);
    secondSocket.open();
    secondSocket.emit({ type: "ready", capabilities: {} });
    const secondHandle = await second;
    expect(monaco.languages.registerHoverProvider).toHaveBeenCalledTimes(1);

    // 诊断要落到具体 model 上：假 Monaco 默认没有 model，这里补上
    monaco.editor.getModel.mockReturnValue(model as never);
    socket.emit({
      type: "lsp",
      message: {
        jsonrpc: "2.0", method: "textDocument/publishDiagnostics",
        params: { uri: "file:///w/a.py", diagnostics: [
          { range: { start: { line: 0, character: 0 }, end: { line: 0, character: 1 } }, severity: 1, message: "boom" },
        ] },
      },
    });
    expect(monaco.editor.setModelMarkers).toHaveBeenCalled();
    const [, source, markers] = monaco.editor.setModelMarkers.mock.calls.at(-1) as [unknown, string, Array<{ severity: number }>];
    expect(source).toBe("pyright");
    expect(markers[0].severity).toBe(8);

    handle.dispose();
    secondHandle.dispose();
    expect(socket.frames().some((f) => f.message?.method === "textDocument/didClose")).toBe(true);
  });
});

