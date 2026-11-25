import { describe, it, expect, vi, beforeEach } from "vitest";
import { render } from "@testing-library/react";

// 捕获 Editor 组件的 onMount 回调
let capturedOnMount: ((editor: unknown, monaco: unknown) => void) | null = null;
let capturedOptions: Record<string, unknown> | null = null;

const mockRegisterInlineCompletionsProvider = vi.fn();
const mockDispose = vi.fn();
const mockRangeConstructor = vi.fn();

vi.mock("@monaco-editor/react", () => ({
  __esModule: true,
  default: ({ onMount, options }: { onMount: (editor: unknown, monaco: unknown) => void; options: Record<string, unknown> }) => {
    capturedOnMount = onMount;
    capturedOptions = options;
    return null;
  },
  // Batch 4a F4: MonacoEditor 顶部 loader.config({ monaco }) 需要 loader 导出
  loader: { config: vi.fn() },
}));

// Batch 4a F4: Monaco 本地化后顶部新增 monaco-editor 与 ?worker 导入——测试环境 mock 掉
vi.mock("monaco-editor", () => ({ Environment: Object }));
vi.mock("monaco-editor/esm/vs/editor/editor.worker?worker", () => ({ default: class {} }));
vi.mock("monaco-editor/esm/vs/language/json/json.worker?worker", () => ({ default: class {} }));
vi.mock("monaco-editor/esm/vs/language/css/css.worker?worker", () => ({ default: class {} }));
vi.mock("monaco-editor/esm/vs/language/html/html.worker?worker", () => ({ default: class {} }));
vi.mock("monaco-editor/esm/vs/language/typescript/ts.worker?worker", () => ({ default: class {} }));

vi.mock("../../../store/appStore", () => ({
  useAppStore: (selector: (s: { port: number; theme: string; diagnosticsByFile: Record<string, unknown[]> }) => unknown) =>
    selector({ port: 9876, theme: "dark", diagnosticsByFile: {} }),
}));

import { MonacoEditor } from "../MonacoEditor";

describe("MonacoEditor inline completion", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    capturedOnMount = null;
    capturedOptions = null;
    global.fetch = vi.fn();
  });

  it("挂载时注册 InlineCompletionsProvider", () => {
    const mockEditor = createMockEditor();
    const mockMonaco = createMockMonaco();

    render(
      <MonacoEditor
        filePath="src/app.ts"
        content=""
        onChange={() => {}}
        onSave={() => {}}
        dirty={false}
        token="test-token"
      />,
    );

    expect(capturedOnMount).not.toBeNull();
    capturedOnMount!(mockEditor, mockMonaco);

    expect(mockRegisterInlineCompletionsProvider).toHaveBeenCalledWith("*", expect.any(Object));
  });

  it("provider 调用后端 /api/v1/inline-completion 并返回建议", async () => {
    const mockEditor = createMockEditor();
    const mockMonaco = createMockMonaco();

    (global.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      json: async () => ({
        items: [{ insertText: "    return 42;" }],
      }),
    });

    render(
      <MonacoEditor
        filePath="src/app.ts"
        content="function answer() {\n}\n"
        onChange={() => {}}
        onSave={() => {}}
        dirty={false}
        token="test-token"
      />,
    );

    capturedOnMount!(mockEditor, mockMonaco);
    const provider = mockRegisterInlineCompletionsProvider.mock.calls[0][1];

    const result = await provider.provideInlineCompletions(
      createMockModel("function answer() {\n", "}\n"),
      { lineNumber: 2, column: 1 },
    );

    expect(global.fetch).toHaveBeenCalledWith(
      "http://127.0.0.1:9876/api/v1/inline-completion",
      expect.objectContaining({
        method: "POST",
        headers: {
          Authorization: "Bearer test-token",
          "Content-Type": "application/json",
        },
        body: expect.any(String),
      }),
    );

    const sentBody = JSON.parse((global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][1].body);
    expect(sentBody.language).toBe("typescript");
    expect(sentBody.filename).toBe("app.ts");
    expect(sentBody.prefix).toContain("function answer()");
    expect(sentBody.suffix).toContain("}");

    expect(result.items).toHaveLength(1);
    expect(result.items[0].insertText).toBe("    return 42;");
  });

  it("prefix 过空时直接返回空建议", async () => {
    const mockEditor = createMockEditor();
    const mockMonaco = createMockMonaco();

    render(
      <MonacoEditor
        filePath="src/app.ts"
        content=""
        onChange={() => {}}
        onSave={() => {}}
        dirty={false}
        token="test-token"
      />,
    );

    capturedOnMount!(mockEditor, mockMonaco);
    const provider = mockRegisterInlineCompletionsProvider.mock.calls[0][1];

    const result = await provider.provideInlineCompletions(
      createMockModel("  ", ""),
      { lineNumber: 1, column: 3 },
    );

    expect(global.fetch).not.toHaveBeenCalled();
    expect(result.items).toEqual([]);
  });

  it("Markdown 不对中文标点做歧义字符描边", () => {
    render(
      <MonacoEditor
        filePath="notes.md"
        content="# 中文标题"
        onChange={() => {}}
        onSave={() => {}}
        dirty={false}
        token="test-token"
      />,
    );
    const unicodeHighlight = capturedOptions?.unicodeHighlight as { ambiguousCharacters?: boolean; invisibleCharacters?: boolean };
    expect(unicodeHighlight.ambiguousCharacters).toBe(false);
    expect(unicodeHighlight.invisibleCharacters).toBe(true);
  });
});

function createMockMonaco() {
  return {
    languages: {
      registerInlineCompletionsProvider: mockRegisterInlineCompletionsProvider.mockReturnValue({
        dispose: mockDispose,
      }),
      registerDocumentSymbolProvider: vi.fn().mockReturnValue({ dispose: vi.fn() }),
      registerDefinitionProvider: vi.fn().mockReturnValue({ dispose: vi.fn() }),
      SymbolKind: { Class: 1, Function: 2 },
    },
    Range: mockRangeConstructor.mockImplementation((...args: number[]) => args),
    MarkerSeverity: { Error: 1, Warning: 2 },
    // 编辑器动作注册用到的常量表（真实 Monaco 里恒存在）
    KeyMod: { CtrlCmd: 2048, Shift: 1024, Alt: 512 },
    KeyCode: { KeyF: 36, KeyO: 44, Slash: 90, BracketLeft: 92, BracketRight: 93, F2: 68 },
    editor: {
      setModelMarkers: vi.fn(),
      IMarkerData: {},
    },
  };
}

function createMockEditor() {
  return {
    getModel: vi.fn().mockReturnValue({
      getValue: vi.fn().mockReturnValue(""),
      getValueInRange: vi.fn().mockReturnValue(""),
    }),
    onDidChangeCursorPosition: vi.fn().mockReturnValue({ dispose: vi.fn() }),
    onDidChangeCursorSelection: vi.fn().mockReturnValue({ dispose: vi.fn() }),
    onDidChangeModelContent: vi.fn().mockReturnValue({ dispose: vi.fn() }),
    createDecorationsCollection: vi.fn().mockReturnValue({ clear: vi.fn() }),
    addAction: vi.fn().mockReturnValue({ dispose: vi.fn() }),
    getAction: vi.fn().mockReturnValue({ run: vi.fn() }),
    getOptions: vi.fn().mockReturnValue({ tabSize: 2 }),
    revealLineInCenter: vi.fn(),
    setPosition: vi.fn(),
    focus: vi.fn(),
  };
}

function createMockModel(prefix: string, suffix: string) {
  const full = prefix + suffix;
  const lines = full.split("\n");
  return {
    getLineCount: () => lines.length,
    getLineMaxColumn: (line: number) => (lines[line - 1] || "").length + 1,
    getValueInRange: (range: {
      startLineNumber: number;
      startColumn: number;
      endLineNumber: number;
      endColumn: number;
    }) => {
      const startLine = range.startLineNumber - 1;
      const endLine = range.endLineNumber - 1;
      const selected = lines.slice(startLine, endLine + 1);
      if (selected.length === 0) return "";
      selected[0] = selected[0].slice(range.startColumn - 1);
      const lastIdx = selected.length - 1;
      selected[lastIdx] = selected[lastIdx].slice(0, range.endColumn - 1);
      return selected.join("\n");
    },
  };
}
