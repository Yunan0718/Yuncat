import { useEffect, useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import Editor from "@monaco-editor/react";
import { useApi, isAbortedError } from "../hooks/useApi";
import { useAppStore } from "../store/appStore";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "../components/ui/tabs";
import { EmptyState } from "../components/common/EmptyState";
import { PromptLibraryList } from "../components/prompts/PromptLibraryList";

interface PromptInfo {
  name: string;
  version: string;
  file_path: string;
  variables: string[];
}

interface PromptDetail {
  name: string;
  version: string;
  variables: Record<string, string>;
}

interface PreviewResult {
  name: string;
  preview: Record<string, string>;
}

export function PromptManager() {
  const { t } = useTranslation();
  const { api } = useApi();
  const backendReady = useAppStore((s) => s.backendReady);
  const theme = useAppStore((s) => s.theme);
  // Phase 49-P2: Monaco 主题对齐 MonacoEditor.tsx——dark→vs-dark / light→light / undefined 跟随系统
  const resolvedTheme = theme === "dark" ? "vs-dark" : theme === "light" ? "light" : undefined;
  // Phase 49-P2: 字体/缩进复用 MonacoEditor.tsx 的 localStorage 配置，保持全局一致
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
  const [prompts, setPrompts] = useState<PromptInfo[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<PromptDetail | null>(null);
  const [editing, setEditing] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState<PreviewResult | null>(null);
  const [previewCtx, setPreviewCtx] = useState<string>("{}");
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadList = useCallback(() => {
    if (!backendReady) return;
    api<PromptInfo[]>("/api/v1/prompts")
      .then(setPrompts)
      .catch((e) => {
        if (isAbortedError(e)) return;
        setError(e.message);
      });
  }, [backendReady, api]);

  useEffect(() => {
    loadList();
  }, [backendReady, loadList]);

  const selectPrompt = (name: string) => {
    setSelected(name);
    setPreview(null);
    setStatusMsg(null);
    api<PromptDetail>(`/api/v1/prompts/${name}`)
      .then((d) => {
        setDetail(d);
        setEditing({ ...d.variables });
      })
      .catch((e) => {
        if (isAbortedError(e)) return;
        setError(e.message);
      });
  };

  const saveOverrides = async () => {
    if (!selected) return;
    try {
      await api(`/api/v1/prompts/${selected}`, { method: "PUT", body: JSON.stringify({ variables: editing }) });
      setStatusMsg(`${t("phase49.prompt.ok")} ${t("common.saved")}`);
      selectPrompt(selected);
    } catch (e: unknown) {
      setStatusMsg(`${t("phase49.prompt.err")} ${t("common.saveFailed")}: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const resetOverrides = async () => {
    if (!selected) return;
    try {
      await api(`/api/v1/prompts/${selected}/reset`, { method: "POST" });
      setStatusMsg(`${t("phase49.prompt.ok")} ${t("prompts.resetOk")}`);
      selectPrompt(selected);
    } catch (e: unknown) {
      setStatusMsg(
        `${t("phase49.prompt.err")} ${t("prompts.resetFailed")}: ${e instanceof Error ? e.message : String(e)}`,
      );
    }
  };

  const doPreview = async () => {
    if (!selected) return;
    let ctx: Record<string, string> = {};
    try {
      ctx = JSON.parse(previewCtx);
    } catch {
      setStatusMsg(`${t("phase49.prompt.warn")} ${t("prompts.ctxJsonError")}`);
      return;
    }
    try {
      const result = await api<PreviewResult>(`/api/v1/prompts/${selected}/preview`, {
        method: "POST",
        body: JSON.stringify({ context: ctx }),
      });
      setPreview(result);
      setStatusMsg(`${t("phase49.prompt.ok")} ${t("prompts.previewOk")}`);
    } catch (e: unknown) {
      setStatusMsg(
        `${t("phase49.prompt.err")} ${t("prompts.previewFailed")}: ${e instanceof Error ? e.message : String(e)}`,
      );
    }
  };

  const forceReload = async () => {
    try {
      const result = await api<{ reloaded: string[] }>("/api/v1/prompts/reload", { method: "POST" });
      setStatusMsg(`${t("phase49.prompt.ok")} ${t("prompts.reloaded")}: ${result.reloaded.join(", ")}`);
      loadList();
      if (selected) selectPrompt(selected);
    } catch (e: unknown) {
      setStatusMsg(
        `${t("phase49.prompt.err")} ${t("prompts.resetFailed")}: ${e instanceof Error ? e.message : String(e)}`,
      );
    }
  };

  if (!backendReady) return <div className="p-8 text-muted-foreground">{t("common.back")}</div>;

  return (
    <div className="page-shell space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold tracking-tight">{t("prompts.title")}</h1>
        <button
          onClick={forceReload}
          className="px-3 py-1.5 text-sm border rounded hover:bg-secondary transition-colors"
        >
          {t("prompts.reload")}
        </button>
      </div>
      {error && (
        <div className="px-4 py-2 rounded bg-destructive/10 text-sm text-destructive">
          {t("common.error")}: {error}
        </div>
      )}
      {statusMsg && <div className="px-4 py-2 rounded bg-secondary text-sm font-mono">{statusMsg}</div>}
      {/* Phase 73: Tabs 布局——模板库（PromptLibrary）+ 模块变量（现有编辑器） */}
      <Tabs defaultValue="library">
        <TabsList>
          <TabsTrigger value="library">{t("promptLibrary.tabLibrary")}</TabsTrigger>
          <TabsTrigger value="variables">{t("promptLibrary.tabVariables")}</TabsTrigger>
        </TabsList>
        <TabsContent value="library">
          <PromptLibraryList />
        </TabsContent>
        <TabsContent value="variables">
          <div className="grid grid-cols-3 gap-6">
            <div className="col-span-1 border rounded divide-y">
              {prompts.map((p) => (
                <button
                  key={p.name}
                  onClick={() => selectPrompt(p.name)}
                  className={`w-full text-left px-4 py-3 hover:bg-secondary transition-colors ${selected === p.name ? "bg-secondary font-semibold" : ""}`}
                >
                  <div className="text-sm">{p.name}</div>
                  <div className="text-xs text-muted-foreground">v{p.version}</div>
                </button>
              ))}
            </div>
            <div className="col-span-2 space-y-4">
              {!detail ? (
                <EmptyState compact title={t("prompts.selectHint")} />
              ) : (
                <>
                  <div className="flex items-center gap-3">
                    <h2 className="text-lg font-semibold">{detail.name}</h2>
                    <span className="text-xs bg-secondary px-2 py-0.5 rounded">v{detail.version}</span>
                  </div>
                  {/* L8.4: 结构化字段编辑——按类型区分 template 和变量 */}
                  {/* Phase 49-P2: 模板变量改用 Monaco 编辑器（对标 LangSmith/Langflow/PromptLayer） */}
                  {/* 普通变量保留 textarea——短文本无需 Monaco 重量级实例，避免性能浪费 */}
                  <div className="space-y-3">
                    {Object.entries(detail.variables).map(([varName]) => {
                      const isTemplate =
                        varName.includes("TEMPLATE") || varName.includes("PROMPT") || varName.includes("template");
                      return (
                        <div key={varName} className={isTemplate ? "border-l-2 border-primary/30 pl-3" : ""}>
                          <label className="text-xs font-semibold text-muted-foreground block mb-1">
                            {isTemplate ? t("phase49.prompt.templateLabel") : t("phase49.prompt.variableLabel")}
                            {varName}
                          </label>
                          {isTemplate ? (
                            <div className="border rounded overflow-hidden">
                              <Editor
                                height="300px"
                                language="markdown"
                                value={editing[varName] ?? ""}
                                theme={resolvedTheme}
                                onChange={(v) => setEditing((prev) => ({ ...prev, [varName]: v || "" }))}
                                options={{
                                  minimap: { enabled: false },
                                  lineNumbers: "on",
                                  scrollBeyondLastLine: false,
                                  fontSize: editorFontSize,
                                  wordWrap: "on",
                                  tabSize: editorTabSize,
                                  padding: { top: 8, bottom: 8 },
                                  scrollbar: { verticalScrollbarSize: 8, horizontalScrollbarSize: 8 },
                                }}
                                loading={
                                  <div className="flex items-center justify-center h-full text-xs text-muted-foreground">
                                    {t("common.loading")}
                                  </div>
                                }
                              />
                            </div>
                          ) : (
                            <textarea
                              value={editing[varName] ?? ""}
                              onChange={(e) => setEditing((prev) => ({ ...prev, [varName]: e.target.value }))}
                              rows={3}
                              className="w-full px-3 py-2 border rounded text-xs font-mono resize-y bg-background"
                              spellCheck={false}
                            />
                          )}
                        </div>
                      );
                    })}
                  </div>
                  <div className="flex gap-2">
                    <button onClick={saveOverrides} className="px-4 py-2 bg-primary text-white rounded text-sm">
                      {t("prompts.saveOverrides")}
                    </button>
                    <button onClick={resetOverrides} className="px-4 py-2 border rounded text-sm hover:bg-secondary">
                      {t("prompts.resetDefaults")}
                    </button>
                  </div>
                  <details className="border rounded">
                    <summary className="px-4 py-2 cursor-pointer text-sm font-semibold hover:bg-secondary">
                      {t("prompts.preview")}
                    </summary>
                    <div className="p-4 space-y-3 border-t">
                      <div>
                        <label className="text-xs text-muted-foreground block mb-1">{t("prompts.previewCtx")}</label>
                        <textarea
                          value={previewCtx}
                          onChange={(e) => setPreviewCtx(e.target.value)}
                          rows={3}
                          className="w-full px-3 py-2 border rounded text-xs font-mono resize-y"
                        />
                      </div>
                      <button onClick={doPreview} className="px-3 py-1.5 border rounded text-sm hover:bg-secondary">
                        {t("prompts.previewRun")}
                      </button>
                      {preview && (
                        <div className="space-y-3">
                          {Object.entries(preview.preview).map(([k, v]) => (
                            <div key={k}>
                              <div className="text-xs font-semibold text-muted-foreground mb-1">{k}</div>
                              <pre className="text-xs whitespace-pre-wrap bg-secondary p-3 rounded max-h-64 overflow-auto">
                                {v}
                              </pre>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  </details>
                </>
              )}
            </div>
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}
