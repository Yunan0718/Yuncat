import { useTranslation } from "react-i18next";
import { FileText, ChevronDown, CheckCircle2, XCircle, Shield } from "lucide-react";
import { AuditLogPanel } from "./AuditLogPanel";
import { useTaskStore, type TaskState } from "../store/taskStore";
import { useAppStore } from "../store/appStore";
import { Button } from "./ui/button";
import { cn } from "@/lib/utils";

type BottomTab = "output" | "audit";

interface BottomPanelProps {
  collapsed: boolean;
  onToggle: () => void;
  height: number;
  onHeightChange?: (height: number) => void;
}

interface OutputEntry {
  id: string;
  tool: string;
  success: boolean;
  preview: string;
  taskId: string;
}

function OutputLog() {
  const { t } = useTranslation();
  const tasks = useTaskStore((s) => s.tasks);
  const entries = Object.values(tasks)
    .flatMap((task: TaskState) =>
      (task.toolResults || []).map<OutputEntry>((r, i) => ({
        id: `${task.taskId}-${i}`,
        tool: r.tool_name,
        success: r.success,
        preview: r.preview,
        taskId: task.taskId,
      })),
    )
    .slice(-50)
    .reverse();

  if (entries.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-xs text-muted-foreground gap-2">
        <FileText size={20} className="opacity-40" />
        <span>{t("bottomPanel.outputEmpty")}</span>
      </div>
    );
  }

  return (
    <div className="h-full overflow-auto p-3 font-mono text-[11px]">
      <div className="space-y-2">
        {entries.map((entry) => (
          <div
            key={entry.id}
            className={cn(
              "rounded-lg border p-2.5 transition-colors",
              entry.success
                ? "bg-secondary/30 border-border hover:bg-secondary/50"
                : "bg-destructive/5 border-destructive/20 hover:bg-destructive/10",
            )}
          >
            <div className="flex items-center gap-2 mb-1">
              {entry.success ? (
                <CheckCircle2 size={12} className="text-success shrink-0" />
              ) : (
                <XCircle size={12} className="text-destructive shrink-0" />
              )}
              <span className="font-semibold text-foreground">{entry.tool}</span>
              <span className="text-[10px] text-muted-foreground">#{entry.taskId.slice(0, 8)}</span>
            </div>
            <pre className="whitespace-pre-wrap break-all text-muted-foreground leading-snug">{entry.preview}</pre>
          </div>
        ))}
      </div>
    </div>
  );
}

export function BottomPanel({ collapsed, onToggle, height }: BottomPanelProps) {
  const { t } = useTranslation();
  // Phase 98: tab 状态提升到 appStore——启动时恢复上次选中的 tab
  const activeTab = useAppStore((s) => s.chatBottomTab);
  const setActiveTab = useAppStore((s) => s.setChatBottomTab);

  if (collapsed) return null;

  const tabs: { id: BottomTab; label: string; icon: typeof FileText }[] = [
    { id: "output", label: t("bottomPanel.output"), icon: FileText },
    { id: "audit", label: t("bottomPanel.audit"), icon: Shield },
  ];

  return (
    <div className="shrink-0 border-t bg-card flex flex-col" style={{ height }}>
      <div className="h-8 border-b flex items-center px-2 bg-muted/40 shrink-0">
        <div className="flex items-center gap-0.5">
          {tabs.map((tab) => {
            const Icon = tab.icon;
            return (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={cn(
                  "flex items-center gap-1.5 px-2.5 py-1 text-[11px] rounded-md transition-colors",
                  activeTab === tab.id
                    ? "bg-background text-foreground shadow-sm font-medium"
                    : "text-muted-foreground hover:text-foreground hover:bg-secondary",
                )}
              >
                <Icon size={12} />
                <span>{tab.label}</span>
              </button>
            );
          })}
        </div>
        <div className="flex-1" />
        <Button
          variant="ghost"
          size="icon"
          className="h-6 w-6"
          onClick={onToggle}
          aria-label={t("common.close")}
          title={t("common.close")}
        >
          <ChevronDown size={13} />
        </Button>
      </div>

      <div className="flex-1 min-h-0 overflow-hidden">
        {activeTab === "output" && <OutputLog />}
        {activeTab === "audit" && <AuditLogPanel />}
      </div>
    </div>
  );
}
