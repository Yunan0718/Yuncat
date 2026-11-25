import { useEffect, useState, useCallback } from "react";
import { useApi } from "../hooks/useApi";
import { useTranslation } from "react-i18next";
import { useAppStore } from "../store/appStore";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "./ui/dialog";
import { Button } from "./ui/button";
import { ScrollArea } from "./ui/scroll-area";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "./ui/tabs";
import { Editor } from "@monaco-editor/react";
import { AlertTriangle, Check, FileText, GitMerge } from "lucide-react";

interface ConflictHunk {
  index: number;
  start_line: number;
  end_line: number;
  marker_ours: string;
  marker_theirs: string;
  ours_text: string;
  theirs_text: string;
  ours_line_count: number;
  theirs_line_count: number;
}

interface ConflictFile {
  ok: boolean;
  path: string;
  base: string;
  ours: string;
  theirs: string;
  current: string;
  hunks: ConflictHunk[];
  error?: string;
}

interface MergeConflictPanelProps {
  open: boolean;
  onClose: () => void;
  filePath: string;
  onResolved?: () => void;
}

/** Phase 102: Git Merge 冲突三栏解决面板
 *
 * 三栏对比 ours / base / theirs，支持整块接受或按 hunk 选择，
 * 底部编辑器让用户手动合并，最终写回工作区并 stage。
 */
export function MergeConflictPanel({
  open,
  onClose,
  filePath,
  onResolved,
}: MergeConflictPanelProps) {
  const { t } = useTranslation();
  const { api } = useApi();
  const theme = useAppStore((s) => s.theme);
  const resolvedTheme = theme === "dark" ? "vs-dark" : theme === "light" ? "light" : undefined;

  const [loading, setLoading] = useState(false);
  const [file, setFile] = useState<ConflictFile | null>(null);
  const [resolution, setResolution] = useState("");
  const [statusMsg, setStatusMsg] = useState("");

  const loadConflict = useCallback(async () => {
    if (!filePath) return;
    setLoading(true);
    try {
      const r = await api<ConflictFile>(`/api/v1/git/merge-conflicts/${encodeURIComponent(filePath)}`);
      setFile(r);
      if (r.ok) {
        setResolution(r.current);
      } else {
        setStatusMsg(r.error || t("mergeConflict.loadFailed"));
      }
    } catch {
      setStatusMsg(t("mergeConflict.loadFailed"));
    }
    setLoading(false);
  }, [api, filePath, t]);

  useEffect(() => {
    if (open) loadConflict();
  }, [open, loadConflict]);

  const acceptVersion = (version: "ours" | "theirs" | "base") => {
    if (!file) return;
    setResolution(file[version]);
    setStatusMsg(t("mergeConflict.acceptedVersion", { version }));
  };

  const acceptHunk = (hunk: ConflictHunk, side: "ours" | "theirs") => {
    if (!file) return;
    const lines = resolution.split("\n");
    const before = lines.slice(0, hunk.start_line);
    const after = lines.slice(hunk.end_line + 1);
    const replacement = side === "ours" ? hunk.ours_text : hunk.theirs_text;
    const newLines = [...before, replacement, ...after];
    setResolution(newLines.join("\n"));
    setStatusMsg(t("mergeConflict.acceptedHunk", { index: hunk.index, side }));
  };

  const doResolve = async () => {
    if (!file) return;
    setLoading(true);
    try {
      const r = await api<{ ok: boolean; error?: string }>(
        `/api/v1/git/merge-conflicts/${encodeURIComponent(file.path)}/resolve`,
        {
          method: "POST",
          body: JSON.stringify({ resolution, mark_resolved: true }),
        },
      );
      if (r.ok) {
        setStatusMsg(t("mergeConflict.resolved"));
        onResolved?.();
        setTimeout(() => onClose(), 500);
      } else {
        setStatusMsg(r.error || t("mergeConflict.resolveFailed"));
      }
    } catch {
      setStatusMsg(t("mergeConflict.resolveFailed"));
    }
    setLoading(false);
  };

  const language = filePath.split(".").pop() || "text";

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-[95vw] w-[1200px] h-[85vh] flex flex-col p-0">
        <DialogHeader className="px-4 py-3 border-b shrink-0">
          <DialogTitle className="flex items-center gap-2 text-sm">
            <GitMerge size={14} />
            {t("mergeConflict.title")}: <span className="font-mono text-xs">{filePath}</span>
            {file && file.hunks.length > 0 && (
              <span className="ml-2 text-[10px] bg-warning/10 text-warning px-1.5 py-0.5 rounded flex items-center gap-1">
                <AlertTriangle size={10} />
                {t("mergeConflict.conflictCount", { count: file.hunks.length })}
              </span>
            )}
          </DialogTitle>
        </DialogHeader>

        <Tabs defaultValue="three-way" className="flex-1 flex flex-col min-h-0">
          <div className="px-4 pt-2 border-b shrink-0">
            <TabsList className="h-7">
              <TabsTrigger value="three-way" className="text-[10px] px-2">
                {t("mergeConflict.tabs.threeWay")}
              </TabsTrigger>
              <TabsTrigger value="hunks" className="text-[10px] px-2">
                {t("mergeConflict.tabs.hunks")}
              </TabsTrigger>
            </TabsList>
          </div>

          <TabsContent value="three-way" className="flex-1 min-h-0 flex flex-col m-0 px-4 pb-2">
            {file && file.ok && (
              <>
                {/* 三栏对比 */}
                <div className="flex-1 min-h-0 grid grid-cols-3 gap-2 pt-2">
                  <VersionColumn
                    title={t("mergeConflict.ours")}
                    content={file.ours}
                    theme={resolvedTheme}
                    language={language}
                    onAccept={() => acceptVersion("ours")}
                    color="bg-primary/10 border-primary/30"
                  />
                  <VersionColumn
                    title={t("mergeConflict.base")}
                    content={file.base}
                    theme={resolvedTheme}
                    language={language}
                    onAccept={() => acceptVersion("base")}
                    color="bg-muted/30 border-border"
                  />
                  <VersionColumn
                    title={t("mergeConflict.theirs")}
                    content={file.theirs}
                    theme={resolvedTheme}
                    language={language}
                    onAccept={() => acceptVersion("theirs")}
                    color="bg-success/10 border-success/30"
                  />
                </div>

                {/* 合并编辑区 */}
                <div className="h-48 shrink-0 mt-2 border rounded overflow-hidden flex flex-col">
                  <div className="flex items-center justify-between px-3 py-1.5 border-b bg-secondary">
                    <span className="text-[10px] font-semibold flex items-center gap-1">
                      <FileText size={10} />
                      {t("mergeConflict.resolution")}
                    </span>
                    <div className="flex items-center gap-1">
                      <Button variant="outline" size="sm" onClick={() => setResolution(file.current)} className="h-6 text-[10px]">
                        {t("mergeConflict.reset")}
                      </Button>
                      <Button size="sm" onClick={doResolve} disabled={loading} className="h-6 text-[10px]">
                        <Check size={10} className="mr-1" />
                        {t("mergeConflict.markResolved")}
                      </Button>
                    </div>
                  </div>
                  <div className="flex-1 min-h-0">
                    <Editor
                      height="100%"
                      language={language}
                      value={resolution}
                      onChange={(v) => setResolution(v || "")}
                      theme={resolvedTheme}
                      options={{
                        minimap: { enabled: false },
                        scrollBeyondLastLine: false,
                        fontSize: 12,
                        wordWrap: "on",
                      }}
                    />
                  </div>
                </div>
              </>
            )}
            {loading && !file && (
              <div className="flex-1 flex items-center justify-center text-xs text-muted-foreground">
                {t("mergeConflict.loading")}
              </div>
            )}
          </TabsContent>

          <TabsContent value="hunks" className="flex-1 min-h-0 m-0 px-4 pb-2 pt-2 overflow-hidden">
            {file && file.ok && file.hunks.length === 0 && (
              <div className="h-full flex items-center justify-center text-xs text-muted-foreground">
                {t("mergeConflict.noHunks")}
              </div>
            )}
            <ScrollArea className="h-full">
              <div className="space-y-2 pr-2">
                {file?.hunks.map((hunk) => (
                  <div key={hunk.index} className="border rounded overflow-hidden">
                    <div className="px-3 py-1.5 border-b bg-secondary flex items-center justify-between">
                      <span className="text-[10px] font-semibold">
                        {t("mergeConflict.hunk")} #{hunk.index}
                      </span>
                      <div className="flex items-center gap-1">
                        <Button variant="outline" size="sm" onClick={() => acceptHunk(hunk, "ours")} className="h-6 text-[10px]">
                          {t("mergeConflict.acceptOurs")}
                        </Button>
                        <Button variant="outline" size="sm" onClick={() => acceptHunk(hunk, "theirs")} className="h-6 text-[10px]">
                          {t("mergeConflict.acceptTheirs")}
                        </Button>
                      </div>
                    </div>
                    <div className="grid grid-cols-2 gap-px bg-border">
                      <div className="bg-primary/5 p-2">
                        <div className="text-[10px] text-primary mb-1">{t("mergeConflict.ours")}</div>
                        <pre className="text-[10px] font-mono whitespace-pre-wrap">{hunk.ours_text || t("mergeConflict.empty")}</pre>
                      </div>
                      <div className="bg-success/5 p-2">
                        <div className="text-[10px] text-success mb-1">{t("mergeConflict.theirs")}</div>
                        <pre className="text-[10px] font-mono whitespace-pre-wrap">{hunk.theirs_text || t("mergeConflict.empty")}</pre>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </ScrollArea>
          </TabsContent>
        </Tabs>

        {statusMsg && (
          <div className="px-4 py-2 text-[10px] font-mono border-t bg-secondary/50 shrink-0">
            {statusMsg}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function VersionColumn({
  title,
  content,
  theme,
  language,
  onAccept,
  color,
}: {
  title: string;
  content: string;
  theme: string | undefined;
  language: string;
  onAccept: () => void;
  color: string;
}) {
  const { t } = useTranslation();
  return (
    <div className={`flex flex-col border rounded overflow-hidden ${color}`}>
      <div className="flex items-center justify-between px-2 py-1 border-b bg-secondary/50">
        <span className="text-[10px] font-semibold">{title}</span>
        <Button variant="ghost" size="sm" onClick={onAccept} className="h-5 text-[10px] px-1.5">
          {t("mergeConflict.acceptAll")}
        </Button>
      </div>
      <div className="flex-1 min-h-0">
        <Editor
          height="100%"
          language={language}
          value={content}
          theme={theme}
          options={{
            readOnly: true,
            minimap: { enabled: false },
            scrollBeyondLastLine: false,
            fontSize: 11,
            wordWrap: "on",
          }}
        />
      </div>
    </div>
  );
}
