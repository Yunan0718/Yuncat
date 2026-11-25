/**
 * lib/lspClient.ts —— 极简 LSP 客户端（WebSocket 上的 JSON-RPC）
 *
 * 为什么自己写这几十行而不引 monaco-languageclient：
 *   那个包要绑定 @codingame/monaco-vscode-api 一整套运行时，和仓库现在用的
 *   纯 monaco-editor + @monaco-editor/react 是两套世界，接进来等于换编辑器底座。
 *   我们真正需要的只有「发请求 / 收通知 / 回包」三件事，所以只做这三件。
 *
 * 两个容易踩的点，都在这里处理掉：
 *   1. 服务端也会**反向发请求**（如 workspace/configuration）。不回答它，
 *      pyright 就会卡在等回包上，表现是「补全一直转圈」。这里统一给合法回包。
 *   2. 请求必须能超时失败。语言服务器崩了却不回包时，界面不能永远转圈。
 */

export interface LspStatusMessage {
  type: "auth_ok" | "ready" | "unavailable" | "lsp" | "pong";
  language?: string;
  reason?: string;
  capabilities?: Record<string, unknown>;
  message?: LspJsonRpc;
}

export interface LspJsonRpc {
  jsonrpc?: string;
  id?: number | string;
  method?: string;
  params?: unknown;
  result?: unknown;
  error?: { code?: number; message?: string };
}

/** 单条 LSP 请求的默认超时（人为上限，可调）：语言服务器卡住时要能失败退出 */
export const LSP_REQUEST_TIMEOUT_MS = 15000;
/** WebSocket.OPEN 的数值——不引用全局静态属性，避免测试替身/旧环境下拿不到 */
const SOCKET_OPEN = 1;
/** 连接与初始化（首次可能要下载 pyright）的等待上限 */
export const LSP_CONNECT_TIMEOUT_MS = 90000;

type Pending = {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
};

export class LspClient {
  private socket: WebSocket | null = null;
  private seq = 1;
  private readonly pending = new Map<number, Pending>();
  private readonly notifications = new Set<(method: string, params: unknown) => void>();
  private disposed = false;

  constructor(
    private readonly url: string,
    private readonly token: string,
    private readonly requestTimeoutMs: number = LSP_REQUEST_TIMEOUT_MS,
  ) {}

  /** 连接并完成握手；返回服务端 capabilities。失败时抛出的错误直接可展示给用户。 */
  connect(timeoutMs: number = LSP_CONNECT_TIMEOUT_MS): Promise<Record<string, unknown>> {
    return new Promise((resolve, reject) => {
      let settled = false;
      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        this.dispose();
        reject(new Error("语言服务连接超时"));
      }, timeoutMs);

      const finish = (fn: () => void) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        fn();
      };

      let socket: WebSocket;
      try {
        socket = new WebSocket(this.url);
      } catch (error) {
        clearTimeout(timer);
        reject(new Error(`无法建立语言服务连接：${String(error)}`));
        return;
      }
      this.socket = socket;

      socket.onopen = () => socket.send(JSON.stringify({ type: "auth", token: this.token }));
      socket.onerror = () => finish(() => reject(new Error("语言服务通道连接失败")));
      socket.onclose = () => {
        this.failAllPending(new Error("语言服务通道已关闭"));
        finish(() => reject(new Error("语言服务通道已关闭")));
      };
      socket.onmessage = (event: MessageEvent<string>) => {
        let payload: LspStatusMessage;
        try {
          payload = JSON.parse(event.data) as LspStatusMessage;
        } catch {
          return;
        }
        if (payload.type === "unavailable") {
          finish(() => reject(new Error(payload.reason || "语言服务不可用")));
          this.dispose();
          return;
        }
        if (payload.type === "ready") {
          finish(() => resolve(payload.capabilities || {}));
          return;
        }
        if (payload.type === "lsp" && payload.message) this.handleRpc(payload.message);
      };
    });
  }

  private handleRpc(message: LspJsonRpc): void {
    const isRequestFromServer = message.id !== undefined && message.method !== undefined;
    if (isRequestFromServer) {
      this.answerServerRequest(message);
      return;
    }
    if (message.id !== undefined) {
      const key = typeof message.id === "number" ? message.id : Number(message.id);
      const entry = this.pending.get(key);
      if (!entry) return;
      this.pending.delete(key);
      clearTimeout(entry.timer);
      if (message.error) entry.reject(new Error(message.error.message || "语言服务返回错误"));
      else entry.resolve(message.result);
      return;
    }
    if (message.method) {
      for (const handler of this.notifications) {
        try {
          handler(message.method, message.params);
        } catch {
          // 降级：单个通知处理失败不影响其它订阅者
        }
      }
    }
  }

  /**
   * 回答服务端发来的请求。目前只可能命中配置类请求——
   * 一律回合法结果（不是错误），避免 pyright 在等回包时卡住整个会话。
   */
  private answerServerRequest(message: LspJsonRpc): void {
    let result: unknown = null;
    if (message.method === "workspace/configuration") {
      const items = (message.params as { items?: unknown[] } | undefined)?.items;
      result = Array.isArray(items) ? items.map(() => null) : [];
    }
    this.sendRaw({ jsonrpc: "2.0", id: message.id, result });
  }

  onNotification(handler: (method: string, params: unknown) => void): () => void {
    this.notifications.add(handler);
    return () => this.notifications.delete(handler);
  }

  request(method: string, params: unknown): Promise<unknown> {
    if (this.disposed || !this.socket || this.socket.readyState !== SOCKET_OPEN) {
      return Promise.reject(new Error("语言服务通道未连接"));
    }
    const id = this.seq++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`语言服务请求超时：${method}`));
      }, this.requestTimeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.sendRaw({ jsonrpc: "2.0", id, method, params });
    });
  }

  notify(method: string, params: unknown): void {
    if (this.disposed || !this.socket || this.socket.readyState !== SOCKET_OPEN) return;
    this.sendRaw({ jsonrpc: "2.0", method, params });
  }

  private sendRaw(message: LspJsonRpc): void {
    try {
      this.socket?.send(JSON.stringify({ type: "lsp", message }));
    } catch {
      // 降级：socket 正在关闭时发送失败，由 onclose 统一收尾
    }
  }

  private failAllPending(error: Error): void {
    for (const [, entry] of this.pending) {
      clearTimeout(entry.timer);
      entry.reject(error);
    }
    this.pending.clear();
  }

  dispose(): void {
    this.disposed = true;
    this.failAllPending(new Error("语言服务已释放"));
    this.notifications.clear();
    const socket = this.socket;
    this.socket = null;
    try {
      socket?.close();
    } catch {
      // 降级：关闭已关闭的连接会抛异常，忽略
    }
  }
}
