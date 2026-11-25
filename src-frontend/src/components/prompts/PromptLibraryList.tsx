/**
 * Phase 73: PromptLibraryList —— 提示词模板库浏览器
 *
 * 三栏布局：
 * - 顶部：搜索框（调用 /api/v1/prompt-library/templates?q=）
 * - 左侧：分类树（roles/behaviors/workflows，含计数）
 * - 右侧：按选中分类展示内容
 *   - roles → 角色卡片列表（id/name/source/prompt_template）
 *   - behaviors → BehaviorSelector 多选组件
 *   - workflows → 工作流模板卡片 + DAG 预览（nodes/edges）
 *
 * 数据源：Phase 72 统一 PromptLibrary API。
 */
import { useEffect, useMemo, useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { useApi, isAbortedError } from "../../hooks/useApi";
import { Badge } from "../ui/badge";
import { Input } from "../ui/input";
import { cn } from "@/lib/utils";
import { BehaviorSelector } from "./BehaviorSelector";

type CategoryId = "roles" | "behaviors" | "workflows";

interface Category {
  id: CategoryId;
  name: string;
  count: number;
}

interface RoleItem {
  id: string;
  name: string;
  category: string;
  prompt_template: string;
  description: string;
  source?: string;
  is_builtin?: boolean;
}

interface WorkflowItem {
  id: string;
  type: "workflow";
  category: "workflows";
  name: string;
  description: string;
  source: string;
  is_builtin: boolean;
}

interface WorkflowDetail {
  name: string;
  display_name: string;
  description: string;
  graph: { nodes: Array<{ id: string; type: string; prompt: string }>; edges: Array<{ from: string; to: string }> };
}

/**
 * 按 query 过滤条目——纯函数，供单元测试
 * 匹配 id/name/description（大小写不敏感）
 */
export function filterByQuery<T extends { id: string; name?: string; description?: string }>(
  items: T[],
  q: string,
): T[] {
  const qLower = q.toLowerCase().trim();
  if (!qLower) return items;
  return items.filter((item) => {
    const text = `${item.id} ${item.name || ""} ${item.description || ""}`.toLowerCase();
    return text.includes(qLower);
  });
}

export function PromptLibraryList() {
  const { t } = useTranslation();
  const { api } = useApi();
  const [categories, setCategories] = useState<Category[]>([]);
  const [activeCategory, setActiveCategory] = useState<CategoryId>("roles");
  const [query, setQuery] = useState("");
  const [roles, setRoles] = useState<RoleItem[]>([]);
  const [workflows, setWorkflows] = useState<WorkflowItem[]>([]);
  const [selectedBehaviorIds, setSelectedBehaviorIds] = useState<string[]>([]);
  const [expandedRoleId, setExpandedRoleId] = useState<string | null>(null);
  const [expandedWorkflowId, setExpandedWorkflowId] = useState<string | null>(null);
  const [workflowDetail, setWorkflowDetail] = useState<WorkflowDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  // 加载类错误统一处理——abort（StrictMode 重挂载/切页）属正常取消，不展示为用户错误
  const handleLoadError = useCallback((e: unknown) => {
    if (isAbortedError(e)) return;
    setError(e instanceof Error ? e.message : String(e));
  }, []);

  // 加载分类目录
  useEffect(() => {
    api<{ categories: Category[] }>("/api/v1/prompt-library/categories")
      .then((data) => setCategories(data.categories || []))
      .catch(handleLoadError);
  }, [api, handleLoadError]);

  // 加载角色
  useEffect(() => {
    api<{ results: RoleItem[] }>("/api/v1/prompt-library/roles")
      .then((data) => setRoles(data.results || []))
      .catch(handleLoadError);
  }, [api, handleLoadError]);

  // 加载工作流模板
  useEffect(() => {
    api<{ results: WorkflowItem[] }>("/api/v1/prompt-library/workflows")
      .then((data) => setWorkflows(data.results || []))
      .catch(handleLoadError);
  }, [api, handleLoadError]);

  const filteredRoles = useMemo(() => filterByQuery(roles, query), [roles, query]);
  const filteredWorkflows = useMemo(() => filterByQuery(workflows, query), [workflows, query]);

  const loadWorkflowDetail = useCallback(
    (name: string) => {
      api<WorkflowDetail>(`/api/v1/prompt-library/workflows/${encodeURIComponent(name)}`)
        .then((data) => {
          setWorkflowDetail(data);
          setExpandedWorkflowId(name);
        })
        .catch(handleLoadError);
    },
    [api, handleLoadError],
  );

  const categoryCount = useCallback(
    (id: CategoryId): number => categories.find((c) => c.id === id)?.count ?? 0,
    [categories],
  );

  if (error) {
    return <div className="text-sm text-destructive p-4">Error: {error}</div>;
  }

  return (
    <div className="space-y-4">
      {/* 搜索框 */}
      <Input
        type="search"
        placeholder={t("promptLibrary.searchPlaceholder")}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        className="max-w-md"
      />

      <div className="grid grid-cols-4 gap-4">
        {/* 左侧分类树 */}
        <div className="col-span-1 space-y-1">
          {(["roles", "behaviors", "workflows"] as CategoryId[]).map((catId) => (
            <button
              key={catId}
              onClick={() => setActiveCategory(catId)}
              className={cn(
                "w-full flex items-center justify-between px-3 py-2 rounded text-sm transition-colors",
                activeCategory === catId
                  ? "bg-secondary font-semibold"
                  : "hover:bg-secondary/50",
              )}
            >
              <span>{t(`promptLibrary.categories.${catId}`)}</span>
              <Badge variant="outline" className="text-xs">{categoryCount(catId)}</Badge>
            </button>
          ))}
        </div>

        {/* 右侧内容区 */}
        <div className="col-span-3">
          {activeCategory === "roles" && (
            <RoleList
              roles={filteredRoles}
              expandedId={expandedRoleId}
              onToggle={setExpandedRoleId}
            />
          )}
          {activeCategory === "behaviors" && (
            <BehaviorSelector
              selectedIds={selectedBehaviorIds}
              onChange={setSelectedBehaviorIds}
            />
          )}
          {activeCategory === "workflows" && (
            <WorkflowList
              workflows={filteredWorkflows}
              expandedId={expandedWorkflowId}
              detail={workflowDetail}
              onToggle={loadWorkflowDetail}
            />
          )}
        </div>
      </div>
    </div>
  );
}

// ── 角色列表子组件 ──

function RoleList({
  roles,
  expandedId,
  onToggle,
}: {
  roles: RoleItem[];
  expandedId: string | null;
  onToggle: (id: string | null) => void;
}) {
  const { t } = useTranslation();
  if (roles.length === 0) {
    return <div className="text-sm text-muted-foreground">{t("promptLibrary.noResults")}</div>;
  }
  return (
    <div className="space-y-2">
      {roles.map((r) => {
        const expanded = expandedId === r.id;
        const sourceLabel = r.source === "yaml" || r.is_builtin
          ? t("promptLibrary.roleSourceYaml")
          : t("promptLibrary.roleSourceDb");
        return (
          <div key={r.id} className="border rounded-lg">
            <button
              onClick={() => onToggle(expanded ? null : r.id)}
              className="w-full flex items-center justify-between px-3 py-2 hover:bg-secondary/30"
            >
              <div className="flex items-center gap-2 min-w-0">
                <span className="text-sm font-mono">{r.id}</span>
                {r.name && r.name !== r.id && (
                  <span className="text-sm text-muted-foreground truncate">{r.name}</span>
                )}
              </div>
              <Badge variant="outline" className="text-xs shrink-0">
                {t("promptLibrary.roleSource")}: {sourceLabel}
              </Badge>
            </button>
            {expanded && (
              <pre className="text-xs whitespace-pre-wrap px-3 py-2 border-t bg-secondary/20 max-h-64 overflow-auto rounded-b-lg">
                {r.prompt_template || r.description || ""}
              </pre>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── 工作流列表子组件 ──

function WorkflowList({
  workflows,
  expandedId,
  detail,
  onToggle,
}: {
  workflows: WorkflowItem[];
  expandedId: string | null;
  detail: WorkflowDetail | null;
  onToggle: (name: string) => void;
}) {
  const { t } = useTranslation();
  if (workflows.length === 0) {
    return <div className="text-sm text-muted-foreground">{t("promptLibrary.noResults")}</div>;
  }
  return (
    <div className="space-y-2">
      {workflows.map((w) => {
        const expanded = expandedId === w.id;
        return (
          <div key={w.id} className="border rounded-lg">
            <button
              onClick={() => onToggle(w.id)}
              className="w-full flex items-center justify-between px-3 py-2 hover:bg-secondary/30"
            >
              <div className="flex items-center gap-2 min-w-0">
                <span className="text-sm font-mono">{w.name}</span>
                {w.description && (
                  <span className="text-xs text-muted-foreground truncate">{w.description}</span>
                )}
              </div>
            </button>
            {expanded && detail && detail.name === w.id && (
              <div className="px-3 py-2 border-t bg-secondary/20 rounded-b-lg space-y-2">
                <div className="flex gap-4 text-xs">
                  <span>
                    {t("promptLibrary.workflow.nodes")}: <strong>{detail.graph.nodes.length}</strong>
                  </span>
                  <span>
                    {t("promptLibrary.workflow.edges")}: <strong>{detail.graph.edges.length}</strong>
                  </span>
                </div>
                <div className="text-xs font-semibold">{t("promptLibrary.workflow.preview")}</div>
                <DagPreview nodes={detail.graph.nodes} edges={detail.graph.edges} />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── DAG 预览（简单文本列表）──

function DagPreview({
  nodes,
  edges,
}: {
  nodes: Array<{ id: string; type: string; prompt: string }>;
  edges: Array<{ from: string; to: string }>;
}) {
  return (
    <div className="space-y-1">
      {nodes.map((n, idx) => {
        const incoming = edges.filter((e) => e.to === n.id).map((e) => e.from);
        return (
          <div key={n.id} className="flex items-start gap-2 text-xs">
            <span className="text-muted-foreground shrink-0">{idx + 1}.</span>
            <div className="flex-1 min-w-0">
              <span className="font-mono">{n.id}</span>
              {incoming.length > 0 && (
                <span className="text-muted-foreground"> ← {incoming.join(", ")}</span>
              )}
              <div className="text-muted-foreground truncate">{n.prompt}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
