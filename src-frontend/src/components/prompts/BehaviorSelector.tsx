/**
 * Phase 73: BehaviorSelector —— 行为约束多选组件
 *
 * 从 /api/v1/prompt-library/behaviors 加载行为约束列表，
 * 按 language 分组（general/python/golang/typescript），
 * 提供复选框 + 全选/清空 + 已选数量 + 内容预览。
 *
 * 选中的 behavior ids 通过 onChange 回调上抛，供父组件（PromptLibraryList）
 * 后续传递给 PromptAgent（Phase 74+）。
 */
import { useEffect, useMemo, useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { useApi } from "../../hooks/useApi";
import { Badge } from "../ui/badge";
import { LoadingCurve } from "../common/LoadingCurve";
import { cn } from "@/lib/utils";

/** Behavior 条目结构——对齐后端 PromptLibrary.list_behaviors() */
export interface Behavior {
  id: string;
  type: "behavior";
  category: "behaviors";
  name: string;
  description: string;
  content: string;
  language: string; // general / python / golang / typescript
  source: string;
  is_builtin: boolean;
}

interface BehaviorSelectorProps {
  /** 当前选中的 behavior ids */
  selectedIds: string[];
  /** 选中变化回调 */
  onChange: (ids: string[]) => void;
}

/**
 * 按 language 分组 behaviors——纯函数，供单元测试
 *
 * @example
 * groupBehaviorsByLanguage([{id:"a",language:"general"},{id:"b",language:"python"}])
 * // => Map { "general" => [a], "python" => [b] }
 */
export function groupBehaviorsByLanguage(behaviors: Behavior[]): Map<string, Behavior[]> {
  const map = new Map<string, Behavior[]>();
  for (const b of behaviors) {
    const lang = b.language || "general";
    if (!map.has(lang)) map.set(lang, []);
    map.get(lang)!.push(b);
  }
  return map;
}

export function BehaviorSelector({ selectedIds, onChange }: BehaviorSelectorProps) {
  const { t } = useTranslation();
  const { api } = useApi();
  const [behaviors, setBehaviors] = useState<Behavior[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [previewId, setPreviewId] = useState<string | null>(null);

  // 加载 behaviors
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api<{ results: Behavior[]; count: number }>("/api/v1/prompt-library/behaviors")
      .then((data) => {
        if (cancelled) return;
        setBehaviors(data.results || []);
        setError(null);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [api]);

  const grouped = useMemo(() => groupBehaviorsByLanguage(behaviors), [behaviors]);

  const selectedSet = useMemo(() => new Set(selectedIds), [selectedIds]);

  const toggle = useCallback(
    (id: string) => {
      if (selectedSet.has(id)) {
        onChange(selectedIds.filter((x) => x !== id));
      } else {
        onChange([...selectedIds, id]);
      }
    },
    [selectedIds, selectedSet, onChange],
  );

  const selectAllInGroup = useCallback(
    (lang: string) => {
      const group = grouped.get(lang) || [];
      const groupIds = group.map((b) => b.id);
      const newSet = new Set(selectedIds);
      for (const id of groupIds) newSet.add(id);
      onChange(Array.from(newSet));
    },
    [grouped, selectedIds, onChange],
  );

  const clearGroup = useCallback(
    (lang: string) => {
      const group = grouped.get(lang) || [];
      const groupIds = new Set(group.map((b) => b.id));
      onChange(selectedIds.filter((id) => !groupIds.has(id)));
    },
    [grouped, selectedIds, onChange],
  );

  const previewBehavior = useMemo(
    () => behaviors.find((b) => b.id === previewId) || null,
    [behaviors, previewId],
  );

  // Task 7.19: 统一加载态——LoadingCurve + i18n（修复硬编码 "Loading..." bug）
  if (loading) {
    return (
      <div className="flex items-center gap-2 p-4 text-sm text-muted-foreground">
        <LoadingCurve variant="lissajous" size={16} particleCount={6} />
        <span>{t("common.loading")}</span>
      </div>
    );
  }

  if (error) {
    return <div className="text-sm text-destructive p-4">Error: {error}</div>;
  }

  if (behaviors.length === 0) {
    return <div className="text-sm text-muted-foreground p-4">{t("promptLibrary.noResults")}</div>;
  }

  return (
    <div className="space-y-4">
      {/* 已选数量 */}
      <div className="flex items-center gap-2">
        <Badge variant="secondary">
          {t("promptLibrary.behaviors.selected", { count: selectedIds.length })}
        </Badge>
      </div>

      {/* 按 language 分组 */}
      {Array.from(grouped.entries()).map(([lang, items]) => {
        const groupSelectedCount = items.filter((b) => selectedSet.has(b.id)).length;
        const allSelected = groupSelectedCount === items.length;
        return (
          <div key={lang} className="border rounded-lg">
            {/* 分组头部 */}
            <div className="flex items-center justify-between px-3 py-2 border-b bg-muted/30">
              <div className="flex items-center gap-2">
                <Badge variant="outline" className="text-xs">{lang}</Badge>
                <span className="text-xs text-muted-foreground">{items.length}</span>
              </div>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => selectAllInGroup(lang)}
                  disabled={allSelected}
                  className="text-xs px-2 py-0.5 rounded hover:bg-secondary disabled:opacity-40"
                >
                  {t("promptLibrary.behaviors.selectAll")}
                </button>
                <button
                  type="button"
                  onClick={() => clearGroup(lang)}
                  disabled={groupSelectedCount === 0}
                  className="text-xs px-2 py-0.5 rounded hover:bg-secondary disabled:opacity-40"
                >
                  {t("promptLibrary.behaviors.clear")}
                </button>
              </div>
            </div>
            {/* behavior 列表 */}
            <div className="divide-y">
              {items.map((b) => {
                const checked = selectedSet.has(b.id);
                return (
                  <label
                    key={b.id}
                    className="flex items-start gap-2 px-3 py-2 hover:bg-secondary/30 cursor-pointer"
                    onMouseEnter={() => setPreviewId(b.id)}
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggle(b.id)}
                      className="mt-0.5"
                    />
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-mono">{b.id}</div>
                      {b.description && (
                        <div className="text-xs text-muted-foreground truncate">{b.description}</div>
                      )}
                    </div>
                  </label>
                );
              })}
            </div>
          </div>
        );
      })}

      {/* 内容预览 */}
      {previewBehavior && (
        <div className="border rounded-lg">
          <div className="px-3 py-2 border-b bg-muted/30">
            <span className="text-xs font-semibold">
              {t("promptLibrary.behaviors.contentPreview")} — {previewBehavior.id}
            </span>
          </div>
          <pre
            className={cn(
              "text-xs whitespace-pre-wrap p-3 max-h-64 overflow-auto",
              "bg-secondary/20 rounded-b-lg",
            )}
          >
            {previewBehavior.content}
          </pre>
        </div>
      )}
    </div>
  );
}
