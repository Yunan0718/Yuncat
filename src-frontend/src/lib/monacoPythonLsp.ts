/**
 * lib/monacoPythonLsp.ts —— 把 pyright 的能力接到 Monaco 上（B 方案）
 *
 * 分工：
 *   lspClient 负责说 LSP；这里负责把 LSP 的补全/悬停/跳转/诊断翻译成 Monaco 的 API。
 *
 * 两个设计点：
 *   1. **Provider 只注册一次**（Monaco 的 provider 是全局的，重复注册会叠出多份建议），
 *      它们通过模块内的 activeClient 取当前连接——比在闭包里钉死某次会话更耐得住编辑器重挂载。
 *   2. 语言服务是**可缺席**的：没装 Node 时状态探测就返回不可用，这里一个 provider 都不注册，
 *      编辑器照常能用，只是没有补全。不假装有。
 */
import type * as Monaco from "monaco-editor";
import { LspClient, LSP_REQUEST_TIMEOUT_MS } from "./lspClient";

type MonacoNs = typeof Monaco;

export interface PythonLspStatus {
  state: "preparing" | "ready" | "unavailable";
  /** 后端给的原始原因（诊断用，界面优先用 code 走多语言文案） */
  reason: string;
  /** 原因代码，例如 node-missing —— 文案交给前端 i18n，不在后端拼中文 */
  code?: string;
}

interface LspPosition { line: number; character: number }
interface LspRange { start: LspPosition; end: LspPosition }
interface LspCompletionItem {
  label: string | { label: string };
  kind?: number;
  detail?: string;
  documentation?: unknown;
  insertText?: string;
  insertTextFormat?: number;
  sortText?: string;
}
interface LspDiagnostic {
  range: LspRange;
  severity?: number;
  message: string;
  source?: string;
  code?: string | number;
}

/** LSP 与 Monaco 的 CompletionItemKind 编号并不一致，必须映射（写错会出现「函数变文件」这类错图标） */
const COMPLETION_KIND: Record<number, number> = {
  1: 18, 2: 0, 3: 1, 4: 2, 5: 3, 6: 4, 7: 5, 8: 7, 9: 8, 10: 9,
  11: 12, 12: 13, 13: 15, 14: 17, 15: 27, 16: 19, 17: 20, 18: 21,
  19: 23, 20: 16, 21: 14, 22: 6, 23: 10, 24: 11, 25: 24,
};

/** LSP severity(1-4) → Monaco MarkerSeverity */
const MARKER_SEVERITY: Record<number, number> = { 1: 8, 2: 4, 3: 2, 4: 1 };

const registeredMonaco = new WeakSet<object>();
let activeClient: LspClient | null = null;

function toLspPosition(position: { lineNumber: number; column: number }): LspPosition {
  return { line: position.lineNumber - 1, character: position.column - 1 };
}

function toRange(monaco: MonacoNs, range: LspRange): Monaco.Range {
  return new monaco.Range(
    range.start.line + 1, range.start.character + 1,
    range.end.line + 1, range.end.character + 1,
  );
}

function documentationText(value: unknown): string | undefined {
  if (!value) return undefined;
  if (typeof value === "string") return value;
  const marked = value as { value?: string };
  return marked.value;
}

/** 统一的「拿不到就退回空结果」——provider 里抛异常会让 Monaco 打红字，这里降级为无提示 */
function ask<T>(method: string, params: unknown, fallback: T): Promise<T> {
  const client = activeClient;
  if (!client) return Promise.resolve(fallback);
  return client.request(method, params).then(
    (result) => (result === null || result === undefined ? fallback : (result as T)),
    () => fallback,
  );
}

function uriOf(model: Monaco.editor.ITextModel): string {
  return model.uri.toString();
}

function registerCompletion(monaco: MonacoNs): void {
  monaco.languages.registerCompletionItemProvider("python", {
    triggerCharacters: [".", "(", "[", ",", "=", " "],
    provideCompletionItems: async (model, position) => {
      const result = await ask<{ items?: LspCompletionItem[] } | LspCompletionItem[]>(
        "textDocument/completion",
        { textDocument: { uri: uriOf(model) }, position: toLspPosition(position), context: { triggerKind: 1 } },
        { items: [] },
      );
      const items = Array.isArray(result) ? result : result.items || [];
      const word = model.getWordUntilPosition(position);
      const range = new monaco.Range(position.lineNumber, word.startColumn, position.lineNumber, word.endColumn);
      return {
        suggestions: items.map((item) => {
          const label = typeof item.label === "string" ? item.label : item.label.label;
          const suggestion: Monaco.languages.CompletionItem = {
            label,
            kind: COMPLETION_KIND[item.kind ?? 1] ?? 18,
            insertText: item.insertText || label,
            detail: item.detail,
            documentation: documentationText(item.documentation),
            sortText: item.sortText,
            range,
          };
          if (item.insertTextFormat === 2) suggestion.insertTextRules = 4; // InsertAsSnippet
          return suggestion;
        }),
      };
    },
  });
}

function registerHover(monaco: MonacoNs): void {
  monaco.languages.registerHoverProvider("python", {
    provideHover: async (model, position) => {
      const result = await ask<{ contents?: unknown; range?: LspRange } | null>(
        "textDocument/hover",
        { textDocument: { uri: uriOf(model) }, position: toLspPosition(position) },
        null,
      );
      if (!result || !result.contents) return null;
      const raw = Array.isArray(result.contents) ? result.contents : [result.contents];
      const values = raw.map(documentationText).filter((text): text is string => !!text);
      if (!values.length) return null;
      return {
        contents: values.map((value) => ({ value })),
        range: result.range ? toRange(monaco, result.range) : undefined,
      };
    },
  });
}

function registerDefinition(monaco: MonacoNs): void {
  monaco.languages.registerDefinitionProvider("python", {
    provideDefinition: async (model, position) => {
      const result = await ask<{ uri: string; range: LspRange } | Array<{ uri: string; range: LspRange }> | null>(
        "textDocument/definition",
        { textDocument: { uri: uriOf(model) }, position: toLspPosition(position) },
        null,
      );
      if (!result) return null;
      const list = Array.isArray(result) ? result : [result];
      return list.map((item) => ({ uri: monaco.Uri.parse(item.uri), range: toRange(monaco, item.range) }));
    },
  });
}

function registerDocumentSymbol(monaco: MonacoNs): void {
  monaco.languages.registerDocumentSymbolProvider("python", {
    provideDocumentSymbols: async (model) => {
      const result = await ask<Array<{ name: string; kind: number; range: LspRange; selectionRange: LspRange }>>(
        "textDocument/documentSymbol",
        { textDocument: { uri: uriOf(model) } },
        [],
      );
      const symbols: Monaco.languages.DocumentSymbol[] = result.map((item) => ({
        name: item.name,
        detail: "",
        kind: COMPLETION_KIND[item.kind] ?? 18,
        tags: [],
        range: toRange(monaco, item.range),
        selectionRange: toRange(monaco, item.selectionRange),
      }));
      return symbols;
    },
  });
}

function registerSignatureHelp(monaco: MonacoNs): void {
  monaco.languages.registerSignatureHelpProvider("python", {
    signatureHelpTriggerCharacters: ["(", ","],
    provideSignatureHelp: async (model, position) => {
      const result = await ask<{
        signatures?: Array<{ label: string; documentation?: unknown; parameters?: Array<{ label: string | [number, number]; documentation?: unknown }> }>;
        activeSignature?: number;
        activeParameter?: number;
      } | null>(
        "textDocument/signatureHelp",
        { textDocument: { uri: uriOf(model) }, position: toLspPosition(position) },
        null,
      );
      if (!result || !result.signatures?.length) return null;
      return {
        value: {
          signatures: result.signatures.map((sig) => ({
            label: sig.label,
            documentation: documentationText(sig.documentation),
            parameters: (sig.parameters || []).map((param) => ({
              label: param.label,
              documentation: documentationText(param.documentation),
            })),
          })),
          activeSignature: result.activeSignature ?? 0,
          activeParameter: result.activeParameter ?? 0,
        },
        dispose: () => undefined,
      };
    },
  });
}

function registerRename(monaco: MonacoNs): void {
  monaco.languages.registerRenameProvider("python", {
    resolveRenameLocation: async (model, position) => {
      const result = await ask<{ range: LspRange; placeholder?: string } | null>(
        "textDocument/prepareRename",
        { textDocument: { uri: uriOf(model) }, position: toLspPosition(position) },
        null,
      );
      if (!result || !result.range) return null;
      return { range: toRange(monaco, result.range), text: result.placeholder ?? model.getValueInRange(toRange(monaco, result.range)) };
    },
    provideRenameEdits: async (model, position, newName) => {
      const result = await ask<{ changes?: Record<string, Array<{ range: LspRange; newText: string }>> } | null>(
        "textDocument/rename",
        { textDocument: { uri: uriOf(model) }, position: toLspPosition(position), newName },
        null,
      );
      if (!result || !result.changes) return { edits: [] };
      // Monaco 的重命名结果是 WorkspaceEdit（带 resource），不是普通的 TextEdit
      const edits: Monaco.languages.IWorkspaceTextEdit[] = [];
      for (const [uri, changes] of Object.entries(result.changes)) {
        const target = monaco.editor.getModel(monaco.Uri.parse(uri));
        if (!target) continue;
        for (const change of changes) {
          edits.push({
            resource: target.uri,
            versionId: undefined,
            textEdit: { range: toRange(monaco, change.range), text: change.newText },
          });
        }
      }
      return { edits };
    },
  });
}

function handleDiagnostics(monaco: MonacoNs, params: unknown): void {
  const payload = params as { uri?: string; diagnostics?: LspDiagnostic[] };
  if (!payload?.uri) return;
  const model = monaco.editor.getModel(monaco.Uri.parse(payload.uri));
  if (!model) return;
  const markers: Monaco.editor.IMarkerData[] = (payload.diagnostics || []).map((diag) => ({
    severity: MARKER_SEVERITY[diag.severity ?? 1] ?? 8,
    message: diag.message,
    source: diag.source || "pyright",
    code: diag.code === undefined ? undefined : String(diag.code),
    ...toRange(monaco, diag.range),
  }));
  monaco.editor.setModelMarkers(model, "pyright", markers);
}

function ensureProviders(monaco: MonacoNs): void {
  if (registeredMonaco.has(monaco as unknown as object)) return;
  registeredMonaco.add(monaco as unknown as object);
  registerCompletion(monaco);
  registerHover(monaco);
  registerDefinition(monaco);
  registerDocumentSymbol(monaco);
  registerSignatureHelp(monaco);
  registerRename(monaco);
}

/** 查后端：这个语言现在能不能用（不可用时带原因，前端只提示一次） */
export interface PythonLspProbe { available: boolean; reason: string; code: string }

export async function probePythonLsp(port: number, token: string): Promise<PythonLspProbe> {
  try {
    const res = await fetch(`http://127.0.0.1:${port}/api/v1/lsp/status?language=python`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) return { available: false, reason: `状态查询失败（HTTP ${res.status}）`, code: "probe-failed" };
    const data = (await res.json()) as { available?: boolean; reason?: string; code?: string };
    return { available: !!data.available, reason: data.reason || "", code: data.code || "" };
  } catch (error) {
    return { available: false, reason: `状态查询失败：${String(error)}`, code: "probe-failed" };
  }
}

export interface AttachOptions {
  monaco: MonacoNs;
  model: Monaco.editor.ITextModel;
  port: number;
  token: string;
  onStatus?: (status: PythonLspStatus) => void;
}

/**
 * 为当前 Python 文档接上语言服务。返回的 dispose 会关连接、发 didClose、清诊断。
 * 任何一步失败都只改变状态，不抛给调用方——编辑器本身必须照常可用。
 */
export async function attachMonacoPythonLsp(options: AttachOptions): Promise<{ dispose: () => void }> {
  const { monaco, model, port, token, onStatus } = options;
  onStatus?.({ state: "preparing", reason: "" });

  const probe = await probePythonLsp(port, token);
  if (!probe.available) {
    onStatus?.({ state: "unavailable", reason: probe.reason, code: probe.code });
    return { dispose: () => undefined };
  }

  const client = new LspClient(`ws://127.0.0.1:${port}/ws/lsp?language=python`, token, LSP_REQUEST_TIMEOUT_MS);
  try {
    await client.connect();
  } catch (error) {
    client.dispose();
    onStatus?.({ state: "unavailable", reason: error instanceof Error ? error.message : String(error) });
    return { dispose: () => undefined };
  }

  activeClient = client;
  ensureProviders(monaco);
  client.onNotification((method, params) => {
    if (method === "textDocument/publishDiagnostics") handleDiagnostics(monaco, params);
  });

  let version = 1;
  client.notify("textDocument/didOpen", {
    textDocument: { uri: uriOf(model), languageId: "python", version, text: model.getValue() },
  });
  const changeSub = model.onDidChangeContent(() => {
    version += 1;
    client.notify("textDocument/didChange", {
      textDocument: { uri: uriOf(model), version },
      contentChanges: [{ text: model.getValue() }],
    });
  });

  onStatus?.({ state: "ready", reason: "" });

  return {
    dispose: () => {
      changeSub.dispose();
      client.notify("textDocument/didClose", { textDocument: { uri: uriOf(model) } });
      monaco.editor.setModelMarkers(model, "pyright", []);
      if (activeClient === client) activeClient = null;
      client.dispose();
    },
  };
}
