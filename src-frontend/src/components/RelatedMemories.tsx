import { useEffect, useRef, useState } from "react";
import { useApi } from "../hooks/useApi";
import { useAppStore } from "../store/appStore";
import { useTaskStore } from "../store/taskStore";
import { useTranslation } from "react-i18next";

interface MemoryItem {
  content: string;
  confidence: number;
  id: string;
  tags: string[];
}

/** Phase 17: 相关记忆卡片——仅在任务完成时搜索一次，避免 stream_chunk 触发过量 API 调用 */
export function RelatedMemories() {
  const { api } = useApi();
  const { t } = useTranslation();
  const backendReady = useAppStore((s) => s.backendReady);
  const currentSessionId = useTaskStore((s) => s.currentSessionId);
  const tasks = useTaskStore((s) => s.tasks);
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [expanded, setExpanded] = useState(false);
  // 防止同一 prompt 重复搜索
  const lastSearchedRef = useRef("");

  useEffect(() => {
    if (!backendReady || !currentSessionId) return;
    const taskList = Object.values(tasks);
    // 只关注最近一条 completed task（不订阅 running 的每次 chunk 更新）
    const lastCompleted = [...taskList].reverse().find((t) => t.status === "completed" && t.prompt);
    if (!lastCompleted?.prompt) return;

    const promptKey = lastCompleted.prompt.slice(0, 80);
    if (promptKey === lastSearchedRef.current) return; // 相同 prompt 不重复搜
    lastSearchedRef.current = promptKey;

    api<Array<{ content: string; confidence: number; id: string; tags: string[] }>>("/api/v1/memories/search", {
      method: "POST",
      body: JSON.stringify({ query: lastCompleted.prompt.slice(0, 200), limit: 5 }),
    })
      .then(setMemories)
      .catch((e) => console.warn("RelatedMemories: load failed:", e));
  }, [tasks, currentSessionId, backendReady, api]);

  if (memories.length === 0) return null;

  return (
    <div className="border-t border-border px-3 py-2 bg-secondary">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-1 text-[10px] text-muted-foreground hover:text-foreground transition-colors"
      >
        <span></span>
        <span>{t("relatedMemories.title", { count: memories.length })}</span>
      </button>
      {expanded && (
        <div className="mt-2 space-y-1.5 max-h-48 overflow-y-auto">
          {memories.map((m) => (
            <div key={m.id} className="text-[10px] text-muted-foreground bg-background rounded px-2 py-1.5">
              <p className="leading-relaxed">{m.content.slice(0, 120)}</p>
              <div className="flex items-center gap-1 mt-0.5">
                {m.tags?.map((t) => (
                  <span key={t} className="text-[8px] bg-primary/10 text-primary px-1 rounded">
                    {t}
                  </span>
                ))}
                <span className="text-[8px] opacity-50 ml-auto">{Math.round(m.confidence * 100)}%</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
