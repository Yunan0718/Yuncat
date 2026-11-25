import { useCallback, useEffect, useRef, useState } from "react";
import Editor, { loader, type OnMount, type Monaco } from "@monaco-editor/react";
// Batch 4a F4: Monaco 本地化（官方 Electron 离线方案）——@monaco-editor/react 默认从
// cdn.jsdelivr.net 拉取 monaco-editor，桌面应用离线不可用且版本不受 lockfile 锁定。
// 注入本地打包的 monaco-editor（package.json 锁定版本），模块级执行一次。
import * as monaco from "monaco-editor";
import editorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";
import jsonWorker from "monaco-editor/esm/vs/language/json/json.worker?worker";
import cssWorker from "monaco-editor/esm/vs/language/css/css.worker?worker";
import htmlWorker from "monaco-editor/esm/vs/language/html/html.worker?worker";
import tsWorker from "monaco-editor/esm/vs/language/typescript/ts.worker?worker";
import { useAppStore } from "../../store/appStore";
import { detectLanguage } from "../../lib/editorLanguage";
import { attachMonacoPythonLsp, type PythonLspStatus } from "../../lib/monacoPythonLsp";
import { PythonLspBadge } from "./PythonLspBadge";
import { useTranslation } from "react-i18next";
import { Check } from "lucide-react";
import type { BlameLine } from "../GitBlameView";

loader.config({ monaco });

// Vite worker 配置——monaco 的 web worker（语言诊断等）从本地 bundle 创建，
// 不再请求 CDN/默认路径（@monaco-editor/react 文档惯例写法）
(self as typeof self & { MonacoEnvironment?: monaco.Environment }).MonacoEnvironment = {
  getWorker(_workerId: string, label: string): Worker {
    switch (label) {
      case "typescript":
      case "javascript":
        return new tsWorker();
      case "json":
        return new jsonWorker();
      case "css":
      case "scss":
      case "less":
        return new cssWorker();
      case "html":
        return new htmlWorker();
      default:
        return new editorWorker();
    }
  },
};

interface LintResult {
  severity?: string;
  line: number;
  col: number;
  code?: string;
  message: string;
}
interface CursorPosEvt {
  position: { lineNumber: number; column: number };
}
interface CursorSelEvt {
  selection: { startLineNumber: number; startColumn: number; endLineNumber: number; endColumn: number };
}

// 语言识别见 lib/editorLanguage（2026-09-15 抽出并补全常见格式）

interface InlineCompletionItem {
  insertText: string;
  replaceRange?: {
    startLineNumber: number;
    startColumn: number;
    endLineNumber: number;
    endColumn: number;
  };
}

interface IInlineCompletionModel {
  getLineCount(): number;
  getLineMaxColumn(line: number): number;
  getValueInRange(range: object): string;
}

interface IInlineCompletionPosition {
  lineNumber: number;
  column: number;
}

// Batch D H11: 从 handleMount 中抽出的纯函数——提取光标位置的单词（供 Go to Definition 使用）
function extractWordAtPosition(
  model: { getValueInRange(range: object): string },
  position: { lineNumber: number; column: number },
): {
  word: string;
  range: { startLineNumber: number; startColumn: number; endLineNumber: number; endColumn: number };
} | null {
  const lineContent = model.getValueInRange({
    startLineNumber: position.lineNumber,
    startColumn: 1,
    endLineNumber: position.lineNumber,
    endColumn: 1000,
  });
  const col = position.column - 1;
  const re = /[A-Za-z_]\w*/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(lineContent)) !== null) {
    if (col >= m.index && col <= m.index + m[0].length) {
      return {
        word: m[0],
        range: {
          startLineNumber: position.lineNumber,
          startColumn: m.index + 1,
          endLineNumber: position.lineNumber,
          endColumn: m.index + m[0].length + 1,
        },
      };
    }
  }
  return null;
}

const LANG_TO_LINT: Record<string, string> = {
  python: "python",
  javascript: "javascript",
  typescript: "typescript",
  tsx: "typescript",
  jsx: "javascript",
};

export function MonacoEditor({
  filePath,
  content,
  onChange,
  onSave,
  onRun,
  dirty,
  token,
  enableInlineCompletion,
  gotoLine,
}: {
  filePath: string;
  content: string;
  onChange: (v: string) => void;
  onSave: () => void;
  onRun?: () => void;
  dirty: boolean;
  token?: string;
  enableInlineCompletion?: boolean;
  /** 打开文件后要定位到的行号（问题面板点击跳转用） */
  gotoLine?: number;
}) {
  const { t } = useTranslation();
  const theme = useAppStore((s) => s.theme);
  const port = useAppStore((s) => s.port);
  const resolvedTheme = theme === "dark" ? "vs-dark" : theme === "light" ? "light" : undefined;
  // Phase 47: 编辑器设置（可配置——对标 VSCode editor.fontSize/tabSize）
  const editorFontSize = (() => {
    try {
      const v = localStorage.getItem("yuncat-editor-fontSize");
      return v ? Number(v) : 13;
    } catch {
      return 13;
    }
  })();
  const editorTabSize = (() => {
    try {
      const v = localStorage.getItem("yuncat-editor-tabSize");
      return v ? Number(v) : 2;
    } catch {
      return 2;
    }
  })();
  const fileName = filePath.split(/[/\\]/).pop() || filePath;
  const lang = detectLanguage(filePath);
  const monacoRef = useRef<Parameters<OnMount>[1] | null>(null);
  const editorRef = useRef<Parameters<OnMount>[0] | null>(null);
  // Task 2.5: 收集所有 disposable——原 handleMount 返回的 cleanup 函数被 @monaco-editor/react
  // 忽略（OnMount 签名返回 void），导致 provider/disposable 全部泄漏。对标 VS Code disposable
  // pattern：注册时收集，组件卸载时统一 dispose。
  const disposablesRef = useRef<Array<{ dispose(): void }>>([]);

  // Task 7.21: 内联 Git Blame 注解——对标 GitLens
  // glyphMargin 显示 commit 色块 + 行末内联 "author • date • summary" + hover 详情
  const [blameEnabled, setBlameEnabled] = useState(false);
  const [blameLines, setBlameLines] = useState<BlameLine[]>([]);
  const [blameLoading, setBlameLoading] = useState(false);
  // Python 智能提示状态（B 方案：本机 Node + pyright）。不可用时只显示徽标，不挡编辑。
  const [pythonLsp, setPythonLsp] = useState<PythonLspStatus | null>(null);
  const pythonLspDisposedRef = useRef(false);
  const blameDecorationsRef = useRef<Monaco["editor"]["IEditorDecorationsCollection"] | null>(null);

  const formatBlameDate = useCallback((ts: string | undefined): string => {
    if (!ts) return "";
    const num = parseInt(ts, 10);
    if (isNaN(num)) return "";
    return new Date(num * 1000).toLocaleDateString(undefined, {
      year: "numeric", month: "short", day: "numeric",
    });
  }, []);

  const loadBlame = useCallback(async () => {
    if (!port || !token) return;
    setBlameLoading(true);
    try {
      const res = await fetch(
        `http://127.0.0.1:${port}/api/v1/git/blame?path=${encodeURIComponent(filePath)}`,
        { headers: { Authorization: `Bearer ${token}` } },
      );
      const data = await res.json() as { ok: boolean; lines?: BlameLine[]; error?: string };
      if (data.ok && data.lines) {
        setBlameLines(data.lines);
      } else {
        setBlameLines([]);
      }
    } catch {
      setBlameLines([]);
    }
    setBlameLoading(false);
  }, [port, token, filePath]);

  // 文件切换或 blame 开关变化时自动加载/清除
  useEffect(() => {
    if (blameEnabled) {
      loadBlame();
    } else {
      setBlameLines([]);
    }
  }, [blameEnabled, loadBlame]);

  const lintFile = useCallback(async () => {
    if (!port || !token || !monacoRef.current || !editorRef.current) return;
    const lintLang = LANG_TO_LINT[lang];
    if (!lintLang) return;
    try {
      const res = await fetch(`http://127.0.0.1:${port}/api/v1/lint`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ path: filePath, language: lintLang }),
      });
      const data = await res.json();
      const monaco = monacoRef.current;
      const model = editorRef.current.getModel();
      if (!model) return;
      const markers = (data.results || []).map((r: LintResult) => ({
        severity: r.severity === "error" ? monaco.MarkerSeverity.Error : monaco.MarkerSeverity.Warning,
        startLineNumber: r.line,
        startColumn: r.col,
        endLineNumber: r.line,
        endColumn: (r.col || 0) + 1,
        message: `${r.code ? `[${r.code}] ` : ""}${r.message}`,
      }));
      monaco.editor.setModelMarkers(model, "lint", markers as Monaco["editor"]["IMarkerData"][]);
    } catch {
      // lint not available
    }
  }, [port, token, lang, filePath]);

  // E3: 订阅 store 中 WS 推送的诊断 → 实时渲染 Monaco marker（对标 Cursor/LSP 实时错误提示）
  // 与 lintFile 保存触发的 marker 使用不同 owner（"lint-push" vs "lint"），Monaco 会合并显示
  const pushedDiags = useAppStore((s) => s.diagnosticsByFile[filePath.replace(/\\/g, "/")]);
  useEffect(() => {
    if (!monacoRef.current || !editorRef.current) return;
    const model = editorRef.current.getModel();
    if (!model) return;
    const monaco = monacoRef.current;
    if (!pushedDiags || pushedDiags.length === 0) {
      monaco.editor.setModelMarkers(model, "lint-push", []);
      return;
    }
    const markers = pushedDiags.map((r) => ({
      severity: r.severity === "error" ? monaco.MarkerSeverity.Error : monaco.MarkerSeverity.Warning,
      startLineNumber: r.line,
      startColumn: r.col,
      endLineNumber: r.line,
      endColumn: (r.col || 0) + 1,
      message: `${r.code ? `[${r.code}] ` : ""}${r.message}`,
    }));
    monaco.editor.setModelMarkers(model, "lint-push", markers as Monaco["editor"]["IMarkerData"][]);
  }, [pushedDiags]);

  // Task 7.21: 内联 blame 装饰——blameLines 变化时创建/更新 Monaco decorations
  // 对标 GitLens: 行末内联文本 "author • date • summary" + glyphMargin 色块 + hover 详情
  useEffect(() => {
    const editor = editorRef.current;
    const monaco = monacoRef.current;
    if (!editor || !monaco) return;

    // 清除旧装饰
    if (blameDecorationsRef.current) {
      blameDecorationsRef.current.clear();
      blameDecorationsRef.current = null;
    }

    if (!blameEnabled || blameLines.length === 0) return;

    const decorations = blameLines.map((line, index) => {
      const lineNumber = index + 1;
      const author = line.author || "—";
      const date = formatBlameDate(line["author-time"]);
      const summary = line.summary ? ` • ${line.summary.slice(0, 60)}` : "";
      const hash = line.hash ? line.hash.slice(0, 7) : "";
      const inlineText = `  ${author}${date ? ` • ${date}` : ""}${summary}`;
      return {
        range: new monaco.Range(lineNumber, 1, lineNumber, 1),
        options: {
          isWholeLine: true,
          after: {
            content: inlineText,
            inlineClassName: "blame-inline-text",
          },
          glyphMarginClassName: "blame-glyph",
          glyphMarginHoverMessage: {
            value: `**${author}**\n${date}\n${hash}\n${line.summary || ""}`,
          },
        },
      };
    });

    const collection = editor.createDecorationsCollection(decorations);
    blameDecorationsRef.current = collection;
  }, [blameLines, blameEnabled, formatBlameDate]);

  // Batch D H11: handleMount 拆分为 4 个子函数——原 227 行超过 CLAUDE.md 函数 <50 行规则。
  // Phase 49-O: Monaco 行内补全（Copilot 式灰色幽灵文本）
  const setupInlineCompletion = useCallback(
    (monaco: Monaco): { dispose(): void } | null => {
      const localCompletionDefault = (() => {
        try {
          return localStorage.getItem("yuncat-editor-inlineCompletion") !== "false";
        } catch {
          return true;
        }
      })();
      const effectiveInlineCompletion =
        typeof enableInlineCompletion === "boolean" ? enableInlineCompletion : localCompletionDefault;
      if (!effectiveInlineCompletion) return null;
      const fileName = filePath.split(/[/\\]/).pop() || filePath;
      // Monaco 的 inline completions API 类型复杂，这里整体 cast 一次以简化实现
      return monaco.languages.registerInlineCompletionsProvider("*", {
        provideInlineCompletions: async (model: IInlineCompletionModel, position: IInlineCompletionPosition) => {
          if (!port || !token) return { items: [] };
          const lineCount = model.getLineCount();
          const lastColumn = model.getLineMaxColumn(lineCount);
          const prefix = model.getValueInRange({
            startLineNumber: 1,
            startColumn: 1,
            endLineNumber: position.lineNumber,
            endColumn: position.column,
          });
          const suffix = model.getValueInRange({
            startLineNumber: position.lineNumber,
            startColumn: position.column,
            endLineNumber: lineCount,
            endColumn: lastColumn,
          });
          if (prefix.trim().length < 2) return { items: [] };
          try {
            const res = await fetch(`http://127.0.0.1:${port}/api/v1/inline-completion`, {
              method: "POST",
              headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
              body: JSON.stringify({ language: lang, prefix, suffix, filename: fileName }),
            });
            if (!res.ok) return { items: [] };
            const data = (await res.json()) as { items?: InlineCompletionItem[] };
            const items = (data.items || []).map((it) => {
              const completion: { insertText: string; range?: unknown } = { insertText: it.insertText };
              if (it.replaceRange) {
                completion.range = new monaco.Range(
                  it.replaceRange.startLineNumber,
                  it.replaceRange.startColumn,
                  it.replaceRange.endLineNumber,
                  it.replaceRange.endColumn,
                );
              }
              return completion;
            });
            return { items };
          } catch {
            return { items: [] };
          }
        },
      } as Monaco["languages"]["InlineCompletionsProvider"]);
    },
    [port, token, lang, filePath, enableInlineCompletion],
  );

  // Phase 43 V.1: 编辑器上下文 — 对标 Cursor agent context
  const setupCursorTracking = useCallback(
    (ed: Parameters<OnMount>[0]): Array<{ dispose(): void }> => {
      const cursorDisposable = ed.onDidChangeCursorPosition((e: CursorPosEvt) => {
        try {
          const { setEditorContext } = useAppStore.getState();
          setEditorContext({
            activeFile: filePath,
            cursorLine: e.position.lineNumber,
            cursorColumn: e.position.column,
          });
        } catch {
          // editor context update best-effort
        }
      });
      const selectionDisposable = ed.onDidChangeCursorSelection((e: CursorSelEvt) => {
        try {
          const sel = ed.getModel()?.getValueInRange(e.selection) || "";
          const { setEditorContext } = useAppStore.getState();
          setEditorContext({
            activeFile: filePath,
            selection: sel.slice(0, 500),
            cursorLine: e.selection.startLineNumber,
            cursorColumn: e.selection.startColumn,
          });
        } catch {
          // editor context update best-effort
        }
      });
      const modelDisposable = ed.onDidChangeModelContent(() => {
        try {
          const { setEditorContext } = useAppStore.getState();
          setEditorContext({ activeFile: filePath, lastEditAt: Date.now() });
        } catch {
          // editor context update best-effort
        }
      });
      return [cursorDisposable, selectionDisposable, modelDisposable];
    },
    [filePath],
  );

  // Phase 43 WS.1: 符号导航 — document symbol provider
  const setupSymbolProvider = useCallback(
    (monaco: Monaco): { dispose(): void } => {
      return monaco.languages.registerDocumentSymbolProvider(lang, {
        provideDocumentSymbols: (model: { getValue(): string }) => {
          const symbols: Array<{
            name: string;
            kind: number;
            range: { startLineNumber: number };
            selectionRange: { startLineNumber: number; startColumn: number; endColumn: number };
            containerName?: string;
          }> = [];
          const text = model.getValue();
          const re = /^(\s*)(def |class |async def |export (const|function|class|interface) |func )([A-Za-z_]\w*)/gm;
          let match;
          while ((match = re.exec(text)) !== null) {
            const name = match[4];
            const lineNum = text.slice(0, match.index).split("\n").length - 1;
            const indent = match[1]?.length || 0;
            symbols.push({
              name,
              kind: match[0].includes("class")
                ? monaco.languages.SymbolKind.Class
                : monaco.languages.SymbolKind.Function,
              range: new monaco.Range(lineNum + 1, 1, lineNum + 1, 1),
              selectionRange: new monaco.Range(lineNum + 1, 1, lineNum + 1, name.length + 1),
              containerName:
                indent > 0
                  ? symbols.findLast((s) => s.range.startLineNumber < lineNum + 1 && !s.name.startsWith("_"))?.name ||
                    ""
                  : "",
            });
          }
          return symbols;
        },
      });
    },
    [lang],
  );

  // Phase 43 WS.1: Go to Definition — 当前文件内定义跳转（F12 / Ctrl+Click）
  const setupDefinitionProvider = useCallback(
    (monaco: Monaco): { dispose(): void } => {
      return monaco.languages.registerDefinitionProvider(lang, {
        provideDefinition: (
          model: { getValue(): string; getValueInRange(range: object): string; uri?: unknown },
          position: { lineNumber: number; column: number },
        ) => {
          const wordInfo = extractWordAtPosition(model, position);
          if (!wordInfo) return null;
          const word = wordInfo.word;
          // 跳过语言关键字和常见内置词
          const BUILTINS = new Set([
            "def", "class", "async", "await", "return", "if", "else", "elif", "for", "while",
            "import", "from", "as", "try", "except", "finally", "with", "yield", "lambda",
            "True", "False", "None", "self", "cls", "const", "let", "var", "function", "export",
            "interface", "type", "enum", "extends", "implements", "new", "this", "super",
          ]);
          if (BUILTINS.has(word)) return null;
          const text = model.getValue();
          const defRe = new RegExp(
            `^(\\s*)(def |class |async def |export (?:const|function|class|interface) |func )${word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?:\\s*\\(|\\s*=|\\s*:)`,
            "gm",
          );
          let m: RegExpExecArray | null;
          while ((m = defRe.exec(text)) !== null) {
            const lineNum = text.slice(0, m.index).split("\n").length - 1;
            const col = m[0].indexOf(word) + 1;
            // 不跳转到自己（光标已在定义处）
            if (lineNum + 1 === position.lineNumber && col === wordInfo.range.startColumn) continue;
            return [
              {
                uri: (model.uri as unknown) || { path: filePath },
                range: {
                  startLineNumber: lineNum + 1,
                  startColumn: col,
                  endLineNumber: lineNum + 1,
                  endColumn: col + word.length,
                },
              },
            ];
          }
          return null;
        },
      });
    },
    [lang, filePath],
  );

  const handleMount: OnMount = useCallback(
    (ed, monaco) => {
      monacoRef.current = monaco;
      editorRef.current = ed;
      const provider = setupInlineCompletion(monaco);
      const cursorDisposables = setupCursorTracking(ed);
      // 2026-09-15: 注册常用编辑动作——键位对齐 VS Code（Shift+Alt+F 格式化、Ctrl+Shift+O 符号、
      // F2 重命名、Ctrl+/ 注释、Ctrl+Shift+[ / ] 折叠）。动作名本地化后会出现在 Monaco 自己的
      // 命令面板（F1）里，等价于主流 IDE 的编辑器命令入口。
      // 注册失败只降级（少几个快捷键），不能让整个编辑器挂载失败——Monaco 版本升级或
      // 测试替身缺 API 都可能命中这里。
      let actions: Array<{ dispose(): void }> = [];
      try {
        actions = [
        { id: "yuncat.formatDocument", label: t("editor.formatDocument"),
          keys: monaco.KeyMod.Shift | monaco.KeyMod.Alt | monaco.KeyCode.KeyF,
          inner: "editor.action.formatDocument" },
        { id: "yuncat.goToSymbol", label: t("editor.goToSymbol"),
          keys: monaco.KeyMod.CtrlCmd | monaco.KeyMod.Shift | monaco.KeyCode.KeyO,
          inner: "editor.action.quickOutline" },
        { id: "yuncat.renameSymbol", label: t("editor.renameSymbol"),
          keys: monaco.KeyCode.F2, inner: "editor.action.rename" },
        { id: "yuncat.toggleComment", label: t("editor.toggleComment"),
          keys: monaco.KeyMod.CtrlCmd | monaco.KeyCode.Slash,
          inner: "editor.action.commentLine" },
        { id: "yuncat.foldAll", label: t("editor.foldAll"),
          keys: monaco.KeyMod.CtrlCmd | monaco.KeyMod.Shift | monaco.KeyCode.BracketLeft,
          inner: "editor.foldAll" },
        { id: "yuncat.unfoldAll", label: t("editor.unfoldAll"),
          keys: monaco.KeyMod.CtrlCmd | monaco.KeyMod.Shift | monaco.KeyCode.BracketRight,
          inner: "editor.unfoldAll" },
      ].map((a) =>
        ed.addAction({
          id: a.id,
          label: a.label,
          keybindings: [a.keys],
          run: (editor) => editor.getAction(a.inner)?.run(),
        }),
      );
      } catch (e) {
        console.warn("[MonacoEditor] 注册编辑器动作失败（快捷键不可用）:", e);
      }
      // 语言/缩进上报——状态栏显示（对标 VS Code 状态栏的 language mode / 缩进）
      try {
        const model = ed.getModel();
        if (model) {
          useAppStore.getState().setEditorContext({
            activeFile: filePath,
            language: model.getLanguageId(),
            indentSize: model.getOptions().tabSize ?? 0,
          });
        }
      } catch {
        // 语言上报 best-effort：取不到模型不影响编辑
      }
      const symbolProvider = setupSymbolProvider(monaco);
      const definitionProvider = setupDefinitionProvider(monaco);
      // Task 2.5: 收集所有 disposable 到 ref——组件卸载时统一 dispose
      const collected: Array<{ dispose(): void }> = [
        provider,
        ...cursorDisposables,
        symbolProvider,
        definitionProvider,
        ...actions,
      ].filter((d): d is { dispose(): void } => !!d);
      disposablesRef.current = collected;

      // Python 智能提示：只在 python 文件、且拿到端口与 token 时接入；
      // 失败（没装 Node、语言服务器起不来）只更新徽标，编辑器本身照常可用。
      pythonLspDisposedRef.current = false;
      const lspModel = ed.getModel();
      if (lang === "python" && port && token && lspModel) {
        attachMonacoPythonLsp({ monaco, model: lspModel, port, token, onStatus: setPythonLsp })
          .then((handle) => {
            // 异步连接回来时组件可能已经卸载——那就直接释放，别留悬挂的 WebSocket
            if (pythonLspDisposedRef.current) handle.dispose();
            else collected.push(handle);
          })
          .catch((error: unknown) => setPythonLsp({ state: "unavailable", reason: String(error) }));
      } else {
        setPythonLsp(null);
      }
    },
    [t, filePath, lang, port, token, setupInlineCompletion, setupCursorTracking, setupSymbolProvider, setupDefinitionProvider],
  );

  // 问题面板点击 → 跳到指定行（对标编辑器里点问题时定位到行列）
  useEffect(() => {
    const ed = editorRef.current;
    if (!ed || !gotoLine || gotoLine < 1) return;
    try {
      const lineCount = ed.getModel()?.getLineCount() ?? gotoLine;
      const line = Math.min(gotoLine, lineCount);
      ed.revealLineInCenter(line);
      ed.setPosition({ lineNumber: line, column: 1 });
      ed.focus();
    } catch {
      // 定位失败（模型未就绪等）不阻塞渲染
    }
  }, [gotoLine, filePath]);

  // Task 2.5: 组件卸载时清理所有 disposable——防止 Monaco provider/editor listener 内存泄漏
  // Task 7.21: 同时清理 blame decorations collection
  useEffect(() => {
    return () => {
      pythonLspDisposedRef.current = true;
      for (const d of disposablesRef.current) {
        try {
          d.dispose();
        } catch {
          // disposable 已失效或已 dispose——best-effort
        }
      }
      disposablesRef.current = [];
      if (blameDecorationsRef.current) {
        try {
          blameDecorationsRef.current.clear();
        } catch {
          // best-effort
        }
        blameDecorationsRef.current = null;
      }
    };
  }, []);

  const handleSave = useCallback(() => {
    onSave();
    setTimeout(lintFile, 300);
  }, [onSave, lintFile]);

  return (
    <div className="h-full flex flex-col bg-background">
      {/* Task 7.21: blame 内联注解样式——对标 GitLens 的灰色半透明行末文本 + glyphMargin 色块 */}
      <style>{`
        .blame-inline-text {
          color: var(--muted-foreground, #888);
          font-style: italic;
          font-size: 0.85em;
          opacity: 0.7;
          margin-left: 16px;
        }
        .blame-glyph {
          background: var(--primary, #007acc);
          opacity: 0.25;
          border-radius: 2px;
          margin: 2px 4px;
        }
      `}</style>
      <div className="h-9 border-b px-3 flex items-center gap-2 shrink-0 bg-secondary">
        <span className="text-xs font-mono font-semibold">{fileName}</span>
        {dirty && <span className="text-[10px] text-warning">{t("monacoEditor.unsaved")}</span>}
        <div className="flex-1" />
        {/* Task 7.21: 内联 blame 开关——对标 GitLens toggle */}
        <button
          onClick={() => setBlameEnabled((v) => !v)}
          className={`px-2 py-0.5 text-[10px] border rounded hover:bg-secondary ${blameEnabled ? "bg-primary text-primary-foreground border-primary" : ""}`}
          title={t("monacoEditor.blame")}
          disabled={blameLoading}
        >
          {blameLoading ? (
            "…"
          ) : (
            <span className="inline-flex items-center gap-1">
              Blame
              {blameEnabled && <Check size={12} />}
            </span>
          )}
        </button>
        <PythonLspBadge status={pythonLsp} />
        <button
          onClick={() => editorRef.current?.getAction("editor.action.formatDocument")?.run()}
          className="px-2 py-0.5 text-[10px] border rounded hover:bg-secondary"
          title={t("editor.formatDocument")}
        >
          {t("editor.formatShort")}
        </button>
        <button
          onClick={handleSave}
          className="px-2 py-0.5 text-[10px] bg-primary text-primary-foreground rounded font-semibold hover:opacity-90"
        >
          {t("legacy.wf_be5f")}
        </button>
        {onRun && (
          <button onClick={onRun} className="px-2 py-0.5 text-[10px] border rounded hover:bg-secondary">
            {t("legacy.wf_4c76")}
          </button>
        )}
      </div>
      <div className="flex-1">
        <Editor
          height="100%"
          language={lang}
          value={content}
          theme={resolvedTheme}
          onChange={(v) => onChange(v || "")}
          onMount={handleMount}
          options={{
            readOnly: false,
            minimap: { enabled: true, scale: 1, showSlider: "mouseover" },
            lineNumbers: "on",
            glyphMargin: true, // Task 7.21: blame 色块 + hover 需要_glyphMargin
            scrollBeyondLastLine: false,
            fontSize: editorFontSize,
            wordWrap: "on",
            tabSize: editorTabSize,
            suggest: { showWords: true, showSnippets: false },
            inlineSuggest: { enabled: true },
            // Markdown/纯文本是自然语言内容；Monaco 的“歧义字符”描边会把中文标点画成
            // 密集橙色方框。代码文件仍保留歧义字符检查，不可见字符检查始终开启。
            unicodeHighlight: {
              ambiguousCharacters: !["markdown", "plaintext", "text"].includes(lang),
              nonBasicASCII: false,
              invisibleCharacters: true,
            },
          }}
          loading={
            <div className="flex items-center justify-center h-full text-xs text-muted-foreground">{t("monacoEditor.loading")}</div>
          }
        />
      </div>
    </div>
  );
}
