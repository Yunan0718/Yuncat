import { useEffect, useState, useCallback, useRef } from "react";
import { useTranslation } from "react-i18next";
import { RefreshCw } from "lucide-react";
import { DetailDrawer } from "./DetailDrawer";
import { LongTaskTree } from "./LongTaskTree";
import { ArbitrationTab } from "./longTask/ArbitrationTab";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "./ui/tabs";
import { CostPanel } from "./CostPanel";
import { useAppStore } from "../store/appStore";
import { useTaskStore, type PlanTreeNode, type SupervisorView, type TaskState } from "../store/taskStore";
import { useApi } from "../hooks/useApi";
import { useConfirmDialog } from "../hooks/useConfirmDialog";
import { emitGlobalToast } from "./common/toast-context";
import { useToast } from "../hooks/use-toast";

/** Phase 82: 任务状态 → 徽章色标 */
function statusBadge(status: string): string {
  const map: Record<string, string> = {
    running: "bg-primary/10 text-primary",
    paused: "bg-warning/10 text-warning",
    completed: "bg-success/10 text-success",
    failed: "bg-destructive/10 text-destructive",
    cancelled: "bg-muted text-muted-foreground",
  };
  return map[status] || "bg-muted text-muted-foreground";
}

/** Task 6.13: 状态文本翻译，屏幕阅读器可读 */
function statusLabel(status: string, t: (k: string, opts?: Record<string, unknown>) => string): string {
  const key = `supervisor.status.${status}`;
  const translated = t(key);
  return translated === key ? status : translated;
}

function resolveRoleFromPhase(phase?: string): NonNullable<TaskState["role"]> | null {
  if (!phase) return null;
  // Task 6.10: 加 "analyzing"（clarifying 和 decomposing 之间的 5 维度分析阶段，仍属 PM 职责）
  if (["clarifying", "analyzing", "decomposing", "architect", "pm"].includes(phase)) return "pm";
  if (["executing", "developer", "dev"].includes(phase)) return "dev";
  if (["spec_review", "auditor", "review", "reviewer"].includes(phase)) return "reviewer";
  if (["convergence", "convergence_check", "merge"].includes(phase)) return "merge";
  return null;
}

function RoleCards({ activeRole, t }: { activeRole: TaskState["role"] | null; t: (k: string, opts?: Record<string, unknown>) => string }) {
  const roles: NonNullable<TaskState["role"]>[] = ["pm", "dev", "reviewer", "merge"];
  const clsBase = "flex-1 flex flex-col items-center gap-1 p-2 rounded border text-2xs transition-colors";
  const clsInactive = "border-border bg-card text-muted-foreground";
  const clsActive: Record<NonNullable<TaskState["role"]>, string> = {
    pm: "border-purple-300 bg-purple-50 text-purple-700 dark:bg-purple-950 dark:text-purple-200",
    dev: "border-blue-300 bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-200",
    reviewer: "border-orange-300 bg-orange-50 text-orange-700 dark:bg-orange-950 dark:text-orange-200",
    merge: "border-green-300 bg-green-50 text-green-700 dark:bg-green-950 dark:text-green-200",
  };
  return (
    <div className="grid grid-cols-4 gap-2 mb-3" role="list" aria-label={t("supervisor.title")}>
      {roles.map((r) => (
        <div
          key={r}
          className={`${clsBase} ${activeRole === r ? clsActive[r] : clsInactive}`}
          role="listitem"
          aria-current={activeRole === r ? "step" : undefined}
        >
          <span className="font-semibold">{t(`supervisor.roles.${r}`)}</span>
          <span>{t(`supervisor.roles.${r}Desc`)}</span>
        </div>
      ))}
    </div>
  );
}

function OverviewTab({
  tasks,
  activeTaskId,
  onSelect,
}: {
  tasks: TaskState[];
  activeTaskId: string | null;
  onSelect: (id: string) => void;
}) {
  const { t } = useTranslation();
  const activeTask = tasks.find((tk) => tk.taskId === activeTaskId);
  const activeRole = resolveRoleFromPhase(activeTask?.lifecycle?.phase);
  if (tasks.length === 0)
    return <div className="text-sm text-muted-foreground py-4">{t("supervisor.noLongTasks")}</div>;
  return (
    <div className="flex flex-col gap-2">
      <RoleCards activeRole={activeRole} t={t} />
      {tasks.map((tk) => (
        <button
          key={tk.taskId}
          onClick={() => onSelect(tk.taskId)}
          className="text-left p-2 rounded border border-border hover:bg-secondary transition-colors"
        >
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs truncate flex-1">{(tk.prompt || "").slice(0, 60) || tk.taskId.slice(0, 8)}</span>
            <span className={`text-2xs px-1.5 py-0.5 rounded ${statusBadge(tk.status)}`}>
              {statusLabel(tk.status, t)}
            </span>
          </div>
        </button>
      ))}
    </div>
  );
}

function GoalTab({ view }: { view: SupervisorView | undefined }) {
  const { t } = useTranslation();
  if (!view) return null;
  const levels = view.grading_levels || [];
  const assumptions = view.assumptions || [];
  return (
    <div className="flex flex-col gap-3 text-xs">
      <section>
        <div className="font-semibold mb-1">{t("supervisor.confirmedGoal")}</div>
        <div className="text-muted-foreground whitespace-pre-wrap">{view.confirmed_goal || "—"}</div>
      </section>
      <section>
        <div className="font-semibold mb-1">{t("supervisor.gradingLevels")}</div>
        {levels.length > 0 ? (
          <ul className="list-disc ml-4 text-muted-foreground">
            {levels.map((g, i) => (
              <li key={i}>{g}</li>
            ))}
          </ul>
        ) : (
          <div className="text-muted-foreground">—</div>
        )}
      </section>
      <section>
        <div className="font-semibold mb-1">{t("supervisor.assumptions")}</div>
        {assumptions.length > 0 ? (
          <ul className="list-disc ml-4 text-muted-foreground">
            {assumptions.map((a, i) => (
              <li key={i}>{a}</li>
            ))}
          </ul>
        ) : (
          <div className="text-muted-foreground">—</div>
        )}
      </section>
    </div>
  );
}

function ReviewTab({ view }: { view: SupervisorView | undefined }) {
  const { t } = useTranslation();
  if (!view) return null;
  const log = view.review_log || [];
  if (log.length === 0)
    return <div className="text-sm text-muted-foreground py-4">{t("supervisor.noLongTasks")}</div>;
  return (
    <div className="flex flex-col gap-2 text-xs">
      {log.map((r, i) => (
        <div key={i} className="p-2 rounded border border-border">
          <div className="font-semibold mb-1">{r.step_title}</div>
          <pre className="text-2xs text-muted-foreground whitespace-pre-wrap overflow-x-auto">
            {JSON.stringify(r.review, null, 2)}
          </pre>
        </div>
      ))}
    </div>
  );
}

function ModifyTab({
  view,
  loading,
  onCall,
  askHistory,
  setAskHistory,
}: {
  view: SupervisorView | undefined;
  loading: boolean;
  onCall: (path: string, body?: unknown) => Promise<unknown>;
  askHistory: { q: string; a: string }[];
  setAskHistory: (updater: (prev: { q: string; a: string }[]) => { q: string; a: string }[]) => void;
}) {
  const { t } = useTranslation();
  const { toast } = useToast();
  const [constraint, setConstraint] = useState("");
  const [newGoal, setNewGoal] = useState("");
  const [question, setQuestion] = useState("");
  if (!view) return null;
  const isRunning = view.status === "running";
  const isPaused = view.status === "paused";
  const inputCls = "w-full text-xs p-2 rounded border border-border bg-background resize-y min-h-[60px]";
  const btnCls = "mt-1 px-2 py-1 rounded bg-primary text-primary-foreground disabled:opacity-50";

  // 从 review_log 提取已注入的消息列表
  const injectedMessages: string[] = [];
  (view.review_log || []).forEach((r: { review?: { constraints?: { constraint?: string }[] } }) => {
    const cs = r.review?.constraints || [];
    cs.forEach((c: { constraint?: string }) => {
      if (c.constraint) injectedMessages.push(c.constraint);
    });
  });

  return (
    <div className="flex flex-col gap-3 text-xs">
      <div className="flex gap-2">
        {isRunning && (
          <button disabled={loading} onClick={() => onCall("/pause")} className="flex-1 px-2 py-1 rounded border border-border hover:bg-secondary disabled:opacity-50">
            {t("supervisor.pause")}
          </button>
        )}
        {isPaused && (
          <button disabled={loading} onClick={() => onCall("/resume")} className="flex-1 px-2 py-1 rounded border border-border hover:bg-secondary disabled:opacity-50">
            {t("supervisor.resume")}
          </button>
        )}
      </div>
      <section>
        <div className="font-semibold mb-1">{t("supervisor.inject")}</div>
        <textarea value={constraint} onChange={(e) => setConstraint(e.target.value)} placeholder={t("supervisor.injectPlaceholder")} className={inputCls} />
        <button
          disabled={loading || !constraint.trim()}
          onClick={async () => {
            await onCall("/inject", { constraint });
            toast(t("supervisor.injectSuccess"), "success");
            setConstraint("");
          }}
          className={btnCls}
        >
          {t("supervisor.inject")}
        </button>
        {injectedMessages.length > 0 && (
          <div className="mt-2 p-2 rounded border border-border bg-secondary/30">
            <div className="font-semibold text-2xs mb-1">
              {t("supervisor.injectedMessages")} ({injectedMessages.length})
            </div>
            <ul className="list-disc ml-4 text-2xs text-muted-foreground space-y-0.5">
              {injectedMessages.map((c, i) => (
                <li key={i}>{c}</li>
              ))}
            </ul>
          </div>
        )}
      </section>
      <section>
        <div className="font-semibold mb-1">{t("supervisor.replan")}</div>
        <textarea value={newGoal} onChange={(e) => setNewGoal(e.target.value)} placeholder={t("supervisor.newGoalPlaceholder")} className={inputCls} />
        <button
          disabled={loading}
          onClick={() => {
            onCall("/spec", newGoal.trim() ? { new_goal: newGoal } : undefined);
            setNewGoal("");
          }}
          className={btnCls}
        >
          {t("supervisor.replan")}
        </button>
      </section>
      <section>
        <div className="font-semibold mb-1">{t("supervisor.ask")}</div>
        <textarea value={question} onChange={(e) => setQuestion(e.target.value)} placeholder={t("supervisor.askPlaceholder")} className={inputCls} />
        <button
          disabled={loading || !question.trim()}
          onClick={async () => {
            const res = (await onCall("/ask", { question })) as { answer?: string } | null;
            const answer = res?.answer || "";
            setAskHistory((prev) => [...prev, { q: question, a: answer }]);
            setQuestion("");
          }}
          className={btnCls}
        >
          {t("supervisor.ask")}
        </button>
        {askHistory.length > 0 && (
          <div className="mt-2 space-y-2 max-h-[200px] overflow-y-auto">
            {askHistory.slice().reverse().map((item, i) => (
              <div key={i} className="p-2 rounded bg-secondary">
                <div className="font-semibold text-2xs">{t("supervisor.askQuestion")}: {item.q}</div>
                <div className="whitespace-pre-wrap text-2xs mt-1">{item.a}</div>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

/** Phase 82: 长任务 Supervisor 驾驶舱抽屉——5 Tab（概览/目标/分解树/审查/修改目标）+ WebSocket 增量推送 */
export function LongTaskSupervisorDrawer() {
  const { t } = useTranslation();
  const open = useAppStore((s) => s.supervisorDrawerOpen);
  const setOpen = useAppStore((s) => s.setSupervisorDrawerOpen);
  const tasks = useTaskStore((s) => s.tasks);
  const activeTaskId = useTaskStore((s) => s.supervisorActiveTaskId);
  const setActive = useTaskStore((s) => s.setSupervisorActiveTask);
  const setView = useTaskStore((s) => s.setSupervisorView);
  const view = useTaskStore((s) => (activeTaskId ? s.supervisorViews[activeTaskId] : undefined));
  const { api } = useApi();
  const [loading, setLoading] = useState(false);
  const [askHistory, setAskHistory] = useState<{ q: string; a: string }[]>([]);
  const fetchedFor = useRef<string>("");
  const lastLifecycleKey = useRef<string>("");

  const longTasks = Object.values(tasks).filter((tk) => tk.isLongTask);
  const { confirm, confirmDialog } = useConfirmDialog();

  const refreshView = useCallback(async () => {
    if (!activeTaskId) return;
    try {
      const v = await api<SupervisorView>(`/api/v1/tasks/${activeTaskId}/supervisor`);
      setView(activeTaskId, v);
    } catch {
      /* 事件驱动容错——静默失败 */
    }
  }, [activeTaskId, api, setView]);

  /** 重分解某个节点的子树：会删除它下面所有 pending 子任务，必须先确认。 */
  const decomposeNode = useCallback(async (node: PlanTreeNode) => {
    if (!activeTaskId) return;
    const ok = await confirm({
      title: t("supervisor.decomposeTitle"),
      description: t("supervisor.decomposeConfirm", { title: node.title }),
      variant: "destructive",
      confirmText: t("supervisor.decompose"),
    });
    if (!ok) return;
    try {
      const result = await api<{ deleted_count?: number; nodes?: unknown[] }>(
        `/api/v1/tasks/${activeTaskId}/nodes/${node.id}/decompose`,
        { method: "POST", body: JSON.stringify({}) },
      );
      emitGlobalToast(
        t("supervisor.decomposed", {
          deleted: result.deleted_count ?? 0,
          added: result.nodes?.length ?? 0,
        }),
        "success",
      );
      await refreshView();
    } catch (e) {
      emitGlobalToast(
        `${t("supervisor.decomposeFailed")}: ${e instanceof Error ? e.message : String(e)}`,
        "error",
      );
    }
  }, [activeTaskId, api, confirm, refreshView, t]);

  // Task 6.6: 抽屉打开/活跃任务变化时全量拉取；生命周期 phase/stepIndex 变化时 reconciliation 拉取
  useEffect(() => {
    if (!open || !activeTaskId) {
      fetchedFor.current = "";
      return;
    }
    const lifecycle = tasks[activeTaskId]?.lifecycle;
    const key = `${lifecycle?.phase ?? ""}|${lifecycle?.stepIndex ?? 0}`;
    const contextKey = `${open}|${activeTaskId}`;
    if (contextKey !== fetchedFor.current) {
      // 抽屉打开或切换活跃任务：全量拉取
      refreshView();
      fetchedFor.current = contextKey;
      lastLifecycleKey.current = key;
    } else if (key !== lastLifecycleKey.current) {
      // 生命周期关键字段变化：reconciliation 拉取
      lastLifecycleKey.current = key;
      refreshView();
    }
  }, [open, activeTaskId, tasks, refreshView]);

  const onCall = useCallback(
    async (path: string, body?: unknown) => {
      if (!activeTaskId) return null;
      setLoading(true);
      try {
        const res = await api(`/api/v1/tasks/${activeTaskId}/supervisor${path}`, {
          method: "POST",
          body: body ? JSON.stringify(body) : undefined,
        });
        await refreshView();
        return res;
      } finally {
        setLoading(false);
      }
    },
    [activeTaskId, api, refreshView],
  );

  return (
    <DetailDrawer
      open={open}
      title={t("supervisor.title")}
      onClose={() => setOpen(false)}
      width="w-[480px]"
      headerAction={
        <button
          onClick={refreshView}
          disabled={!activeTaskId}
          className="text-muted-foreground hover:text-foreground disabled:opacity-50 p-1 rounded"
          aria-label={t("supervisor.refresh")}
          title={t("supervisor.refresh")}
        >
          <RefreshCw size={14} />
        </button>
      }
    >
      <Tabs defaultValue="overview">
        <TabsList className="w-full">
          <TabsTrigger value="overview">{t("supervisor.overview")}</TabsTrigger>
          <TabsTrigger value="goal">{t("supervisor.goal")}</TabsTrigger>
          <TabsTrigger value="tree">{t("supervisor.tree")}</TabsTrigger>
          <TabsTrigger value="review">{t("supervisor.review")}</TabsTrigger>
          <TabsTrigger value="arbitration">{t("supervisor.arbitrationTab")}</TabsTrigger>
          <TabsTrigger value="modify">{t("supervisor.modify")}</TabsTrigger>
          <TabsTrigger value="usage">{t("supervisor.usage")}</TabsTrigger>
        </TabsList>
        <TabsContent value="overview">
          <OverviewTab tasks={longTasks} activeTaskId={activeTaskId} onSelect={setActive} />
        </TabsContent>
        <TabsContent value="goal">
          <GoalTab view={view} />
        </TabsContent>
        <TabsContent value="tree">
          {view ? (
            <LongTaskTree
              nodes={view.plan_tree}
              currentStepId={view.current_step_id}
              onDecompose={(node) => void decomposeNode(node)}
            />
          ) : null}
        </TabsContent>
        <TabsContent value="review">
          <ReviewTab view={view} />
        </TabsContent>
        <TabsContent value="usage">
          {activeTaskId ? <CostPanel taskId={activeTaskId} /> : null}
        </TabsContent>
        <TabsContent value="arbitration">
          <ArbitrationTab />
        </TabsContent>
        <TabsContent value="modify">
          <ModifyTab view={view} loading={loading} onCall={onCall} askHistory={askHistory} setAskHistory={setAskHistory} />
        </TabsContent>
      </Tabs>
      {confirmDialog}
    </DetailDrawer>
  );
}
