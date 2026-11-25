import { useEffect, useState } from "react";
import { Check, Circle, ClipboardCheck, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useApi } from "../../hooks/useApi";
import { useTaskStore } from "../../store/taskStore";
import type { TodoItem } from "../../types/task";

/**
 * 会话任务清单（对标 DSH `tool-todo` 的 UI 侧）。
 *
 * 数据源是后端那张 session_todos 表——Agent 用 todo_write 整表替换，
 * 这里通过 WS 事件实时刷新，并在切换会话时拉取一次。
 */
export function TodoChecklist() {
  const { t } = useTranslation();
  const { api } = useApi();
  const todos = useTaskStore((s) => s.sessionTodos);
  const setTodos = useTaskStore((s) => s.setSessionTodos);
  const currentSessionId = useTaskStore((s) => s.currentSessionId);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    if (!currentSessionId) {
      setTodos([]);
      return;
    }
    api<{ todos: TodoItem[] }>(`/api/v1/sessions/${currentSessionId}/todos`)
      .then((r) => {
        if (!cancelled) setTodos(Array.isArray(r?.todos) ? r.todos : []);
      })
      .catch(() => {
        if (!cancelled) setTodos([]);
      });
    return () => { cancelled = true; };
  }, [api, currentSessionId, setTodos]);

  if (todos.length === 0) return null;
  const done = todos.filter((x) => x.status === "completed").length;

  return (
    <div className="relative shrink-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label={t("todo.title")}
        title={t("todo.title")}
        className="h-7 inline-flex items-center gap-1 rounded-lg px-1.5 text-[11px] text-muted-foreground hover:text-foreground hover:bg-secondary transition-colors"
      >
        <ClipboardCheck size={15} strokeWidth={1.7} />
        <span className="tabular-nums">{done}/{todos.length}</span>
      </button>
      {open && (
        <div
          data-testid="todo-panel"
          className="absolute top-full left-0 mt-1 z-dropdown w-64 rounded-lg border border-border bg-card shadow-lg p-2"
        >
          <div className="px-1 pb-1.5 text-[10px] font-semibold text-muted-foreground">
            {t("todo.title")}
          </div>
          <div className="max-h-64 overflow-y-auto space-y-0.5">
            {todos.map((item, index) => (
              <div
                key={`${index}-${item.content}`}
                className="flex items-start gap-1.5 rounded px-1 py-0.5 text-[11px]"
              >
                <span className="mt-0.5 shrink-0">
                  {item.status === "completed" ? (
                    <Check size={12} className="text-success" />
                  ) : item.status === "in_progress" ? (
                    <Loader2 size={12} className="text-primary animate-spin" />
                  ) : (
                    <Circle size={12} className="text-muted-foreground/50" />
                  )}
                </span>
                <span className={
                  item.status === "completed"
                    ? "text-muted-foreground line-through"
                    : item.status === "in_progress"
                      ? "text-foreground font-medium"
                      : "text-muted-foreground"
                }>
                  {item.content}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
