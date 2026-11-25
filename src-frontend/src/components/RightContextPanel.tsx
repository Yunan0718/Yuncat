import { useTranslation } from "react-i18next";
import { useEffect, useState } from "react";
import { Brain, Activity, X, FileText, Cpu, Target, FileIcon, type LucideIcon } from "lucide-react";
import { useAppStore } from "../store/appStore";
import { useTaskStore, type TaskState } from "../store/taskStore";
import { useApi } from "../hooks/useApi";
import { useResizablePanel } from "../hooks/useResizablePanel";
import { Button } from "./ui/button";
import { cn } from "@/lib/utils";

type RightTab = "context" | "memory";

interface RightContextPanelProps {
  currentModelName?: string;
  openFiles?: string[];
  activeFile?: string | null;
  onOpenFile?: (path: string, name: string) => void;
  onClose?: () => void;
}

interface MemoryNode {
  id: string;
  title: string;
  score: number;
}

function ActiveContext({ modelName }: { modelName?: string }) {
  const { t } = useTranslation();
  const editorContext = useAppStore((s) => s.editorContext);
  const tasks = useTaskStore((s) => s.tasks);
  const currentTask = Object.values(tasks).find((task: TaskState) => task.status === "running");

  return (
    <div className="p-3 space-y-3 text-[11px]">
      {modelName && (
        <div className="rounded-lg bg-secondary/60 p-2.5 space-y-1.5">
          <div className="flex items-center gap-1.5 text-muted-foreground">
            <Cpu size={12} />
            <span>{t("rightPanel.model")}</span>
          </div>
          <div className="font-medium text-foreground truncate">{modelName}</div>
        </div>
      )}

      {currentTask && (
        <div className="rounded-lg bg-secondary/60 p-2.5 space-y-1.5">
          <div className="flex items-center gap-1.5 text-muted-foreground">
            <Target size={12} />
            <span>{t("rightPanel.currentTask")}</span>
          </div>
          <div className="font-medium text-foreground leading-snug line-clamp-2">{currentTask.prompt}</div>
          {currentTask.progress && (
            <div className="text-muted-foreground">
              {t("rightPanel.progress", {
                step: currentTask.progress.step,
                total: currentTask.progress.total,
              })}
            </div>
          )}
        </div>
      )}

      <div className="rounded-lg bg-secondary/60 p-2.5 space-y-1.5">
        <div className="flex items-center gap-1.5 text-muted-foreground">
          <FileText size={12} />
          <span>{t("rightPanel.activeFile")}</span>
        </div>
        {editorContext?.activeFile ? (
          <>
            <div className="font-medium text-foreground truncate">{editorContext.activeFile}</div>
            {editorContext.cursorLine > 0 && (
              <div className="text-muted-foreground">
                {t("rightPanel.cursorLine", { line: editorContext.cursorLine })}
              </div>
            )}
          </>
        ) : (
          <div className="text-muted-foreground">{t("rightPanel.noActiveFile")}</div>
        )}
      </div>
    </div>
  );
}

function MemoryContext() {
  const { t } = useTranslation();
  const { api } = useApi();
  const backendReady = useAppStore((s) => s.backendReady);
  const [memories, setMemories] = useState<MemoryNode[]>([]);

  useEffect(() => {
    if (!backendReady) return;
    api<{ nodes?: MemoryNode[] }>("/api/v1/knowledge/graph?limit=10")
      .then((r) => setMemories(r.nodes || []))
      .catch(() => setMemories([]));
  }, [backendReady, api]);

  if (memories.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-40 text-xs text-muted-foreground gap-2">
        <Brain size={20} className="opacity-40" />
        <span>{t("rightPanel.noMemory")}</span>
      </div>
    );
  }

  return (
    <div className="p-2 space-y-0.5">
      {memories.map((m) => (
        <div
          key={m.id}
          className="group px-2.5 py-2 rounded-md hover:bg-secondary cursor-pointer text-[11px] transition-colors"
          title={m.title}
        >
          <div className="flex items-start gap-2">
            <Brain size={12} className="mt-0.5 text-primary/70 shrink-0" />
            <div className="min-w-0 flex-1">
              <div className="truncate text-foreground">{m.title}</div>
              <div className="text-[10px] text-muted-foreground">
                {t("rightPanel.score", { score: (m.score * 100).toFixed(0) })}
              </div>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

export function RightContextPanel({
  currentModelName,
  openFiles,
  activeFile,
  onOpenFile,
  onClose,
}: RightContextPanelProps) {
  const { t } = useTranslation();
  const [activeTab, setActiveTab] = useState<RightTab>("context");

  // Phase 108: 右侧面板可拖拽调整宽度（对标 VS Code / dsh resizable panels）。
  // 阈值命名常量化，持久化到 localStorage。
  const { size, dragHandleProps } = useResizablePanel({
    storageKey: "yuncat.rightPanel.width",
    initialSize: 240,
    minSize: 200,
    maxSize: 480,
    side: "right",
  });

  const tabs: { id: RightTab; label: string; icon: LucideIcon }[] = [
    { id: "context", label: t("rightPanel.context"), icon: Activity },
    { id: "memory", label: t("rightPanel.memory"), icon: Brain },
  ];

  return (
    <aside
      className="hidden md:flex min-w-[200px] bg-muted/20 flex-col h-full shrink-0 relative"
      style={{ width: size }}
    >
      {/* Phase 108: 可拖拽分隔条——对标 VS Code / dsh 的 resizable 面板 */}
      <div
        {...dragHandleProps}
        role="separator"
        aria-orientation="vertical"
        aria-label={t("rightPanel.resize")}
        title={t("rightPanel.resize")}
        className="absolute left-0 top-0 h-full w-1 cursor-col-resize bg-transparent hover:bg-primary/30 transition-colors z-10"
      />
      <div className="h-9 flex items-center px-3 justify-between bg-muted/30 shrink-0">
        <span className="text-xs font-semibold text-foreground">{t("rightPanel.title")}</span>
        {onClose && (
          <Button
            variant="ghost"
            size="icon"
            className="h-6 w-6"
            onClick={onClose}
            aria-label={t("common.close")}
            title={t("common.close")}
          >
            <X size={13} />
          </Button>
        )}
      </div>

      <div className="flex items-center gap-1 px-2 py-1.5 bg-muted/20 shrink-0">
        {tabs.map((tab) => {
          const Icon = tab.icon;
          return (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={cn(
                "flex-1 flex items-center justify-center gap-1.5 px-1.5 py-1 text-[11px] rounded-md transition-colors",
                activeTab === tab.id
                  ? "bg-background text-foreground shadow-sm font-medium"
                  : "text-muted-foreground hover:text-foreground hover:bg-secondary",
              )}
            >
              <Icon size={12} />
              <span className="hidden lg:inline">{tab.label}</span>
            </button>
          );
        })}
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto">
        {activeTab === "context" && <ActiveContext modelName={currentModelName} />}
        {activeTab === "memory" && <MemoryContext />}
      </div>

      {openFiles && openFiles.length > 0 && (
        <div className="p-3 shrink-0 bg-muted/20">
          <div className="text-[10px] font-semibold text-muted-foreground mb-2 uppercase tracking-wide">
            {t("rightPanel.openFiles")}
          </div>
          <div className="space-y-0.5">
            {openFiles.slice(0, 8).map((f) => {
              const name = f.split(/[/\\]/).pop() || f;
              const isActive = activeFile === f;
              return (
                <button
                  key={f}
                  onClick={() => onOpenFile?.(f, name)}
                  className={cn(
                    "w-full flex items-center gap-1.5 text-[11px] px-1.5 py-1 rounded transition-colors text-left",
                    isActive ? "bg-primary/10 text-primary font-medium" : "text-foreground hover:bg-secondary",
                  )}
                  title={f}
                >
                  <FileIcon size={12} className={isActive ? "text-primary" : "text-muted-foreground"} />
                  <span className="truncate">{name}</span>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </aside>
  );
}
