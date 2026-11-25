import { useEffect, useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { useApi } from "../hooks/useApi";
import { useAppStore } from "../store/appStore";
import { KnowledgeGraph } from "../components/KnowledgeGraph";
import { useTranslation } from "react-i18next";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Textarea } from "../components/ui/textarea";
import { Badge } from "../components/ui/badge";
import { Card, CardContent } from "../components/ui/card";
import { EmptyState } from "../components/common/EmptyState";
import { Search, MessageSquare, GitBranch } from "lucide-react";
import { useConfirmDialog } from "../hooks/useConfirmDialog";

interface MemoryItem {
  id: string;
  content: string;
  source: string;
  owner: string;
  confidence: number;
  memory_type: string;
  verified: number;
}

interface ConversationItem {
  id: string;
  session_id: string;
  role: string;
  content: string;
  created_at: number;
}

type Tab = "memories" | "conversations" | "graph" | "knowledge";

/**
 * P0-5 (2026-07-04): 安全解析 tags 字段——后端可能返回 JSON 字符串、数组、空字符串、非法 JSON。
 * 旧实现 JSX 中直接 JSON.parse(n.tags) 无 try/catch，脏数据导致整页黑屏。
 * 对标主流前端容错解析（zod safeParse / lodash attempt 模式）。
 */
function parseTags(tags: unknown): string[] {
  if (Array.isArray(tags)) return tags.filter((t): t is string => typeof t === "string");
  if (typeof tags !== "string") return [];
  const trimmed = tags.trim();
  if (!trimmed || trimmed === "[]") return [];
  try {
    const parsed = JSON.parse(trimmed);
    if (Array.isArray(parsed)) {
      return parsed.filter((t): t is string => typeof t === "string");
    }
    return [];
  } catch {
    // 非法 JSON——返回空数组而非抛异常，避免整页崩溃
    return [];
  }
}

/**
 * 标签行——展示层统一走 parseTags 容错。
 *
 * 抽成组件而不是在 nodes.map 里现算：map 回调是括号式 JSX，插不进语句，
 * 内联调用 parseTags 会算两遍（一次判空、一次渲染）。
 */
function NodeTags({ tags }: { tags: unknown }) {
  const parsed = parseTags(tags);
  if (parsed.length === 0) return null;
  return (
    <div className="flex gap-1 flex-wrap">
      {parsed.map((tag) => (
        <Badge key={tag} variant="secondary" className="text-2xs">
          {tag}
        </Badge>
      ))}
    </div>
  );
}

/**
 * Phase 5 MemoryExplorer: 记忆库浏览与搜索
 *
 * 功能:
 * - FTS5 全文搜索语义记忆
 * - 记忆列表（可信度、类型筛选）
 * - 标记可信 / 取消可信
 * - 对话历史查看（按 session_id）
 */
export function MemoryExplorer() {
  const { t } = useTranslation();
  const { api } = useApi();
  const { confirm, confirmDialog } = useConfirmDialog();
  const backendReady = useAppStore((s) => s.backendReady);
  const navigate = useNavigate();
  const [selectedMem, setSelectedMem] = useState<MemoryItem | null>(null);
  const [tab, setTab] = useState<Tab>("memories");

  // 记忆搜索状态
  const [query, setQuery] = useState("");
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [searching, setSearching] = useState(false);
  // Phase 18: 新建/编辑记忆
  const [showAdd, setShowAdd] = useState(false);
  const [newContent, setNewContent] = useState("");
  const [newType, setNewType] = useState("fact");
  const [editingMem, setEditingMem] = useState<MemoryItem | null>(null);

  const addMemory = async () => {
    if (!newContent.trim()) return;
    try {
      await api("/api/v1/memories", {
        method: "POST",
        body: JSON.stringify({ content: newContent.trim(), memory_type: newType, confidence: 0.8 }),
      });
      setShowAdd(false);
      setNewContent("");
      setStatusMsg(t("memories.created"));
      if (query.trim()) doSearch();
    } catch (e: unknown) {
      setStatusMsg(`${t("memories.createFailed")}: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  // 删除记忆
  const deleteMemory = async (memId: string) => {
    const ok = await confirm({
      title: t("common.confirm"),
      description: t("memories.deleteConfirm"),
      variant: "destructive",
    });
    if (!ok) return;
    try {
      await api(`/api/v1/memories/${memId}`, { method: "DELETE" });
      setMemories((prev) => prev.filter((m) => m.id !== memId));
      setStatusMsg(t("memories.deleted"));
    } catch {
      setStatusMsg(t("memories.deleteFailed"));
    }
  };

  const editMemory = async () => {
    if (!editingMem) return;
    try {
      await api(`/api/v1/memories/${editingMem.id}`, {
        method: "PUT",
        body: JSON.stringify({ content: editingMem.content }),
      });
      setMemories((prev) => prev.map((m) => (m.id === editingMem.id ? { ...m, content: editingMem.content } : m)));
      setStatusMsg(t("memories.updated"));
      setEditingMem(null);
    } catch {
      setStatusMsg(t("memories.editFailed"));
    }
  };

  // 对话历史状态
  const [sessionId, setSessionId] = useState("");
  const [conversations, setConversations] = useState<ConversationItem[]>([]);

  const [statusMsg, setStatusMsg] = useState<string | null>(null);

  // Phase 32: MemoryTree 知识浏览
  interface KnowledgeNode {
    id: string;
    title: string;
    content: string;
    parent_id: string | null;
    source: string;
    source_ref: string | null;
    score: number;
    /**
     * 后端以 JSON 字符串存 tags，取出时再 json.loads——一旦某行是脏数据，
     * 该字段可能是字符串而非数组。故类型标 unknown，显示层一律走 parseTags 容错，
     * 不要在这里写 string[]（那是谎报，会让 .map 在字符串上炸掉）。
     */
    tags: unknown;
    created_at: number;
    updated_at: number;
  }
  const [knowledgeNodes, setKnowledgeNodes] = useState<KnowledgeNode[]>([]);
  // Task 7.23: 知识节点编辑状态
  const [editingNode, setEditingNode] = useState<{
    id: string;
    title: string;
    content: string;
  } | null>(null);
  const loadKnowledge = useCallback(async () => {
    try {
      const r = await api<KnowledgeNode[]>("/api/v1/knowledge/browse");
      setKnowledgeNodes(r);
    } catch {
      // no-op
    }
  }, [api]);
  useEffect(() => {
    if (tab === "knowledge") loadKnowledge();
  }, [tab, loadKnowledge]);

  // Task 7.23: 知识节点编辑/删除——对标 memories tab 的 editMemory/deleteMemory 模式
  const deleteKnowledgeNode = async (nodeId: string) => {
    const ok = await confirm({
      title: t("common.confirm"),
      description: t("memories.deleteKnowledgeConfirm"),
      variant: "destructive",
    });
    if (!ok) return;
    try {
      await api(`/api/v1/knowledge/${nodeId}`, { method: "DELETE" });
      setKnowledgeNodes((prev) => prev.filter((n) => n.id !== nodeId));
      setStatusMsg(t("memories.deleted"));
    } catch {
      setStatusMsg(t("memories.deleteFailed"));
    }
  };

  const editKnowledgeNode = async () => {
    if (!editingNode) return;
    try {
      await api(`/api/v1/knowledge/${editingNode.id}`, {
        method: "PUT",
        body: JSON.stringify({ title: editingNode.title, content: editingNode.content }),
      });
      setKnowledgeNodes((prev) =>
        prev.map((n) =>
          n.id === editingNode.id
            ? { ...n, title: editingNode.title, content: editingNode.content }
            : n,
        ),
      );
      setStatusMsg(t("memories.updated"));
      setEditingNode(null);
    } catch {
      setStatusMsg(t("memories.editFailed"));
    }
  };

  // 搜索记忆
  const doSearch = useCallback(async () => {
    setSearching(true);
    try {
      if (!query.trim()) {
        // 无搜索词时加载最近记忆（浏览视图）
        const items = await api<MemoryItem[]>("/api/v1/memories/browse");
        setMemories(items || []);
        setStatusMsg(null);
      } else {
        const result = await api<MemoryItem[]>("/api/v1/memories/search", {
          method: "POST",
          body: JSON.stringify({ query: query.trim(), limit: 50 }),
        });
        setMemories(result);
        setStatusMsg(null);
      }
    } catch (e: unknown) {
      setStatusMsg(`${t("memories.searchFailed")}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSearching(false);
    }
  }, [api, query, t]);

  // 标记可信
  const toggleVerify = async (memId: string) => {
    try {
      await api(`/api/v1/memories/${memId}/verify`, { method: "POST" });
      setMemories((prev) => prev.map((m) => (m.id === memId ? { ...m, verified: 1 } : m)));
      setStatusMsg(t("memories.verifyOk"));
    } catch (e: unknown) {
      setStatusMsg(`${t("memories.verifyFailed")}: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  // 加载对话历史
  const loadConversations = async () => {
    if (!sessionId.trim()) return;
    try {
      const result = await api<ConversationItem[]>(`/api/v1/conversations/${sessionId.trim()}`);
      setConversations(result);
      setStatusMsg(null);
    } catch (e: unknown) {
      setStatusMsg(`${t("common.error")}: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  // Phase 6: Fork 分支
  const handleFork = async (c: ConversationItem) => {
    try {
      const result = await api<{ new_session_id: string }>(
        `/api/v1/conversations/${c.session_id}/fork?from_message_id=${c.id}`,
        { method: "POST" },
      );
      navigate(`/?session_id=${result.new_session_id}`);
    } catch (e: unknown) {
      setStatusMsg(`${t("memories.forkFailed")}: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  // 初始化：空搜索列出最近记忆
  useEffect(() => {
    if (!backendReady) return;
    if (tab === "memories") doSearch();
  }, [backendReady, tab, api, doSearch]);

  const confidenceColor = (c: number) => {
    if (c >= 0.8) return "text-success";
    if (c >= 0.5) return "text-warning";
    return "text-destructive";
  };

  if (!backendReady) return <div className="p-8 text-muted-foreground">{t("common.back")}</div>;

  return (
    <div className="page-shell space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">{t("memories.title")}</h1>
          <p className="text-sm text-muted-foreground mt-1">{t("memories.subtitle")}</p>
        </div>
      </div>

      {/* Tab 切换 */}
      <div className="flex gap-1 border-b pb-0">
        {(["memories", "conversations", "graph", "knowledge"] as Tab[]).map((tabKey) => (
          <button
            key={tabKey}
            onClick={() => setTab(tabKey)}
            className={`px-4 py-2 text-sm rounded-t transition-colors ${
              tab === tabKey
                ? "border-b-2 border-primary font-semibold text-primary"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {t(`memories.tabs.${tabKey}`)}
          </button>
        ))}
      </div>

      {statusMsg && <div className="px-4 py-2 rounded bg-secondary text-sm font-mono">{statusMsg}</div>}

      {/* 语义记忆 */}
      {tab === "memories" && (
        <div className="space-y-4">
          <div className="flex gap-2">
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && doSearch()}
              placeholder={t("memories.searchPlaceholder")}
              className="flex-1"
            />
            <Button onClick={doSearch} disabled={searching}>
              {searching ? t("memories.searching") : t("memories.search")}
            </Button>
            <Button variant="outline" onClick={() => setShowAdd(true)}>
              {t("memories.add")}
            </Button>
          </div>
          {/* Phase 18: 新建记忆弹窗 */}
          {showAdd && (
            <div
              className="fixed inset-0 bg-black/40 flex items-center justify-center z-modal"
              role="dialog"
              aria-modal="true"
              aria-label={t("memories.addTitle")}
            >
              <div className="bg-background rounded-lg shadow-xl p-6 w-[500px] max-w-[95vw] space-y-3">
                <h3 className="font-semibold">{t("memories.addTitle")}</h3>
                <Textarea
                  value={newContent}
                  onChange={(e) => setNewContent(e.target.value)}
                  placeholder={t("memories.addPlaceholder")}
                  rows={4}
                />
                <div className="flex items-center gap-2">
                  <span className="text-xs text-muted-foreground">{t("memories.type")}:</span>
                  <select
                    value={newType}
                    onChange={(e) => setNewType(e.target.value)}
                    className="px-2 py-1 border rounded text-xs"
                  >
                    <option value="fact">{t("memories.types.fact")}</option>
                    <option value="preference">{t("memories.types.preference")}</option>
                    <option value="rule">{t("memories.types.rule")}</option>
                    <option value="note">{t("memories.types.note")}</option>
                  </select>
                </div>
                <div className="flex gap-2 justify-end">
                  <Button variant="outline" size="sm" onClick={() => setShowAdd(false)}>
                    {t("common.cancel")}
                  </Button>
                  <Button size="sm" onClick={addMemory}>
                    {t("common.save")}
                  </Button>
                </div>
              </div>
            </div>
          )}

          {memories.length === 0 ? (
            <EmptyState
              icon={<Search size={22} />}
              title={t("memory.emptyMemories")}
              description={t("memory.emptyMemoriesDesc")}
              compact
            />
          ) : (
            <div className="space-y-2">
              {memories.map((m) => (
                <Card
                  key={m.id}
                  className="cursor-pointer hover:bg-secondary transition-colors"
                  onClick={() => setSelectedMem(m)}
                >
                  <CardContent className="p-3 flex items-start justify-between gap-3">
                    <div className="flex-1 min-w-0">
                      <p className="text-sm">{m.content}</p>
                      <div className="flex gap-2 mt-1.5 flex-wrap">
                        <Badge variant="secondary" className="text-2xs">
                          {m.memory_type}
                        </Badge>
                        {m.source && (
                          <Badge variant="secondary" className="text-2xs">
                            {m.source}
                          </Badge>
                        )}
                        {m.owner && (
                          <Badge variant="secondary" className="text-2xs">
                            {m.owner}
                          </Badge>
                        )}
                        <Badge variant="outline" className={`text-2xs ${confidenceColor(m.confidence)}`}>
                          {Math.round(m.confidence * 100)}%
                        </Badge>
                        {m.verified === 1 && (
                          <Badge variant="default" className="text-2xs">
                            {t("memories.verified")}
                          </Badge>
                        )}
                      </div>
                    </div>
                    <div className="flex gap-1 shrink-0">
                      {m.verified !== 1 && (
                        <Button
                          size="xs"
                          variant="outline"
                          onClick={(e) => {
                            e.stopPropagation();
                            toggleVerify(m.id);
                          }}
                        >
                          {t("memories.verify")}
                        </Button>
                      )}
                      <Button
                        size="xs"
                        variant="outline"
                        className="text-destructive border-destructive hover:bg-destructive/10"
                        onClick={(e) => {
                          e.stopPropagation();
                          deleteMemory(m.id);
                        }}
                      >
                        {t("common.delete")}
                      </Button>
                    </div>
                  </CardContent>
                </Card>
              ))}
            </div>
          )}
        </div>
      )}

      {/* 对话历史 */}
      {tab === "conversations" && (
        <div className="space-y-4">
          <div className="flex gap-2">
            <Input
              value={sessionId}
              onChange={(e) => setSessionId(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && loadConversations()}
              placeholder={t("memories.sessionId")}
              className="flex-1 font-mono"
            />
            <Button onClick={loadConversations}>{t("memories.load")}</Button>
          </div>

          {conversations.length === 0 ? (
            <EmptyState
              icon={<MessageSquare size={20} />}
              title={t("memory.emptyConversations")}
              description={t("memory.emptyConversationsDesc")}
              compact
            />
          ) : (
            <div className="space-y-2">
              {conversations.map((c) => (
                <div key={c.id} className={`border rounded-lg p-3 ${c.role === "user" ? "bg-secondary" : ""}`}>
                  <div className="flex items-center gap-2 mb-1">
                    <Badge variant="secondary" className="text-2xs uppercase">
                      {c.role}
                    </Badge>
                    {c.created_at && (
                      <span className="text-2xs text-muted-foreground">
                        {new Date(c.created_at * 1000).toLocaleString()}
                      </span>
                    )}
                    {/* Phase 6: Fork 按钮 */}
                    <Button
                      size="xs"
                      variant="outline"
                      onClick={() => handleFork(c)}
                      className="ml-auto"
                      title={t("chat.fork")}
                    >
                      {t("memories.fork")}
                    </Button>
                  </div>
                  <p className="text-sm whitespace-pre-wrap">{c.content.slice(0, 500)}</p>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Phase 18: 记忆详情弹窗（含编辑） */}
      {editingMem ? (
        <div
          className="fixed inset-0 bg-black/40 z-modal flex items-center justify-center"
          onClick={() => setEditingMem(null)}
          role="dialog"
          aria-modal="true"
          aria-label={t("memories.editTitle")}
        >
          <div
            className="bg-background rounded-lg shadow-sm p-6 max-w-lg w-full max-h-[80vh] overflow-y-auto space-y-3"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="font-semibold">{t("memories.editMemory")}</h3>
            <Textarea
              value={editingMem.content}
              onChange={(e) => setEditingMem({ ...editingMem, content: e.target.value })}
              rows={6}
              className="font-mono"
            />
            <div className="flex gap-2 justify-end">
              <Button variant="outline" size="sm" onClick={() => setEditingMem(null)}>
                {t("common.cancel")}
              </Button>
              <Button size="sm" onClick={editMemory}>
                {t("common.save")}
              </Button>
            </div>
          </div>
        </div>
      ) : selectedMem ? (
        <div
          className="fixed inset-0 bg-black/40 z-modal flex items-center justify-center"
          onClick={() => setSelectedMem(null)}
        >
          <div
            className="bg-background rounded-lg shadow-sm p-6 max-w-lg w-full max-h-[80vh] overflow-y-auto space-y-3"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between">
              <h3 className="font-semibold">{t("memories.details")}</h3>
              <Button
                variant="ghost"
                size="icon-xs"
                onClick={() => setSelectedMem(null)}
                aria-label={t("common.close")}
              >
                ×
              </Button>
            </div>
            <div className="bg-secondary rounded-lg p-3 text-sm whitespace-pre-wrap">{selectedMem.content}</div>
            <div className="grid grid-cols-2 gap-2 text-xs text-muted-foreground">
              <div>
                <span className="font-semibold">{t("memories.type")}:</span> {selectedMem.memory_type}
              </div>
              <div>
                <span className="font-semibold">{t("memories.source")}:</span> {selectedMem.source || "-"}
              </div>
              <div>
                <span className="font-semibold">{t("memories.confidence")}:</span>{" "}
                {Math.round(selectedMem.confidence * 100)}%
              </div>
              <div>
                <span className="font-semibold">{t("memories.visibility")}:</span>{" "}
                {selectedMem.verified ? t("memories.verified") : t("memories.unverified")}
              </div>
              {selectedMem.owner && (
                <div className="col-span-2">
                  <span className="font-semibold">{t("memories.owner")}:</span> {selectedMem.owner}
                </div>
              )}
            </div>
            <div className="flex gap-2">
              {selectedMem.verified !== 1 && (
                <Button size="sm" onClick={() => toggleVerify(selectedMem.id)}>
                  {t("memories.verify")}
                </Button>
              )}
              <Button variant="outline" size="sm" onClick={() => setEditingMem(selectedMem)}>
                {t("common.edit")}
              </Button>
            </div>
          </div>
        </div>
      ) : null}

      {/* Phase 32: Yuncat 学到的知识——按 source 分组浏览 */}
      {tab === "knowledge" && (
        <div className="space-y-3">
          <p className="text-xs text-muted-foreground">{t("memories.knowledgeHint")}</p>
          {knowledgeNodes.length === 0 ? (
            <EmptyState
              icon={<GitBranch size={20} />}
              title={t("memory.emptyKnowledge")}
              description={t("memory.emptyKnowledgeDesc")}
              compact
            />
          ) : (
            <div className="space-y-4">
              {Object.entries(
                knowledgeNodes.reduce((groups, n) => {
                  const key = n.source || t("memories.unknownSource");
                  groups[key] = groups[key] || [];
                  groups[key].push(n);
                  return groups;
                }, {} as Record<string, KnowledgeNode[]>),
              )
                .sort(([a], [b]) => a.localeCompare(b))
                .map(([source, nodes]) => (
                  <div key={source} className="space-y-2">
                    <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground flex items-center gap-2">
                      <span className="w-1.5 h-1.5 rounded-full bg-primary" />
                      {t("memories.sourceLabel", { source })}
                      <Badge variant="secondary" className="text-2xs">
                        {nodes.length}
                      </Badge>
                    </h3>
                    <div className="space-y-2">
                      {nodes
                        .sort((a, b) => b.score - a.score)
                        .map((n) => (
                          <Card key={n.id}>
                            <CardContent className="p-3 space-y-1">
                              <div className="flex items-center justify-between gap-2">
                                <div className="flex items-center gap-2 min-w-0">
                                  {n.parent_id && (
                                    <span
                                      className="text-2xs text-muted-foreground shrink-0"
                                      title={t("memories.childNode")}
                                    >
                                      └─
                                    </span>
                                  )}
                                  <span className="text-sm font-semibold truncate">{n.title}</span>
                                </div>
                                <div className="flex items-center gap-1 shrink-0">
                                  <span className="text-2xs text-muted-foreground">
                                    {Math.round(n.score * 100)}%
                                  </span>
                                  {/* Task 7.23: 编辑/删除按钮——对标 memories tab 的操作按钮 */}
                                  <Button
                                    size="xs"
                                    variant="outline"
                                    onClick={() =>
                                      setEditingNode({ id: n.id, title: n.title, content: n.content })
                                    }
                                  >
                                    {t("common.edit")}
                                  </Button>
                                  <Button
                                    size="xs"
                                    variant="outline"
                                    className="text-destructive border-destructive hover:bg-destructive/10"
                                    onClick={() => deleteKnowledgeNode(n.id)}
                                  >
                                    {t("common.delete")}
                                  </Button>
                                </div>
                              </div>
                              <p className="text-xs text-muted-foreground line-clamp-3">{n.content}</p>
                              <NodeTags tags={n.tags} />
                            </CardContent>
                          </Card>
                        ))}
                    </div>
                  </div>
                ))}
            </div>
          )}
        </div>
      )}

      {/* Task 7.23: 知识节点编辑弹窗——对标 memories tab 的 editingMem 弹窗 */}
      {editingNode && (
        <div
          className="fixed inset-0 bg-black/40 z-modal flex items-center justify-center"
          onClick={() => setEditingNode(null)}
        >
          <div
            className="bg-background rounded-lg shadow-sm p-6 max-w-lg w-full max-h-[80vh] overflow-y-auto space-y-3"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="font-semibold">{t("memories.editKnowledge")}</h3>
            <div className="space-y-2">
              <Input
                value={editingNode.title}
                onChange={(e) => setEditingNode({ ...editingNode, title: e.target.value })}
                className="font-mono text-sm"
              />
              <Textarea
                value={editingNode.content}
                onChange={(e) => setEditingNode({ ...editingNode, content: e.target.value })}
                rows={6}
                className="font-mono"
              />
            </div>
            <div className="flex gap-2 justify-end">
              <Button variant="outline" size="sm" onClick={() => setEditingNode(null)}>
                {t("common.cancel")}
              </Button>
              <Button size="sm" onClick={editKnowledgeNode} disabled={!editingNode.title.trim()}>
                {t("common.save")}
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* Phase 18: 知识图谱——react-flow 可视化知识树 */}
      {tab === "graph" && (
        <div className="-mx-6 -mb-6" style={{ height: "calc(100vh - 200px)" }}>
          <KnowledgeGraph />
        </div>
      )}
      {confirmDialog}
    </div>
  );
}
