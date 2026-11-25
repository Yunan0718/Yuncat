import { useEffect, useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { useApi } from "../../hooks/useApi";
import { Button } from "../ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../ui/dialog";
import { Input } from "../ui/input";
import { Label } from "../ui/label";
import { Textarea } from "../ui/textarea";
import { Switch } from "../ui/switch";
import { Badge } from "../ui/badge";
import { EmptyState } from "../common/EmptyState";
import { GitPullRequest, MessageSquare, GitMerge, Plus, ChevronDown, ChevronRight, Check, GitPullRequestClosed } from "lucide-react";

interface PRPanelProps {
  open: boolean;
  defaultOwner?: string;
  defaultRepo?: string;
  defaultHead?: string;
  defaultBase?: string;
  defaultProvider?: string;
  onClose: () => void;
  onStatusMsg?: (msg: string) => void;
}

interface PRItem {
  number?: number;
  iid?: number;
  id?: number | string;
  title: string;
  state: string;
  user?: { login?: string; name?: string; username?: string };
  author?: { login?: string; name?: string; username?: string };
  html_url?: string;
  web_url?: string;
}

interface VCSProvider {
  provider: string;
  token_mask: string;
  configured: boolean;
  base_url?: string;
}

export function PRPanel({
  open,
  defaultOwner = "",
  defaultRepo = "",
  defaultHead = "",
  defaultBase = "main",
  defaultProvider = "",
  onClose,
  onStatusMsg,
}: PRPanelProps) {
  const { t } = useTranslation();
  const { api } = useApi();
  const [provider, setProvider] = useState(defaultProvider);
  const [owner, setOwner] = useState(defaultOwner);
  const [repo, setRepo] = useState(defaultRepo);
  const [head, setHead] = useState(defaultHead);
  const [base, setBase] = useState(defaultBase);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [draft, setDraft] = useState(false);
  const [reviewers, setReviewers] = useState("");
  const [prs, setPrs] = useState<PRItem[]>([]);
  const [providers, setProviders] = useState<VCSProvider[]>([]);
  const [loading, setLoading] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [expandedPr, setExpandedPr] = useState<string | number | null>(null);
  const [commentText, setCommentText] = useState("");

  useEffect(() => {
    setProvider(defaultProvider);
    setOwner(defaultOwner);
    setRepo(defaultRepo);
    setHead(defaultHead);
    setBase(defaultBase);
  }, [defaultProvider, defaultOwner, defaultRepo, defaultHead, defaultBase, open]);

  const loadProviders = useCallback(async () => {
    try {
      const r = await api<{ ok: boolean; providers?: VCSProvider[] }>("/api/v1/vcs/providers");
      if (r.ok && r.providers) {
        setProviders(r.providers);
        if (!provider && r.providers.length > 0) {
          setProvider(r.providers[0].provider);
        }
      }
    } catch {
      /* ignore */
    }
  }, [api, provider]);

  const loadPRs = useCallback(async () => {
    if (!provider || !owner || !repo) return;
    setLoading(true);
    try {
      const r = await api<{ ok: boolean; data?: PRItem[]; error?: string }>(
        `/api/v1/vcs/prs?provider=${encodeURIComponent(provider)}&owner=${encodeURIComponent(owner)}&repo=${encodeURIComponent(repo)}`,
      );
      if (r.ok && r.data) {
        setPrs(r.data);
      } else {
        onStatusMsg?.(t("vcs.errors.loadFailed", { error: r.error || "" }));
      }
    } catch {
      onStatusMsg?.(t("vcs.errors.loadFailed", { error: "" }));
    }
    setLoading(false);
  }, [api, provider, owner, repo, t, onStatusMsg]);

  useEffect(() => {
    if (open) {
      loadProviders();
      loadPRs();
    }
  }, [open, loadProviders, loadPRs]);

  const runOp = async (
    endpoint: string,
    method: string,
    payload: Record<string, unknown>,
    successMsg: string,
  ) => {
    try {
      const r = await api<{ ok: boolean; error?: string }>(endpoint, { method, body: JSON.stringify(payload) });
      onStatusMsg?.(r.ok ? successMsg : t("vcs.errors.actionFailed", { error: r.error || "" }));
      if (r.ok) loadPRs();
      return r.ok;
    } catch {
      onStatusMsg?.(t("vcs.errors.actionFailed", { error: "" }));
      return false;
    }
  };

  const handleCreate = async () => {
    if (!title.trim() || !provider || !owner || !repo || !head || !base) return;
    const ok = await runOp(
      "/api/v1/vcs/prs",
      "POST",
      {
        provider,
        owner,
        repo,
        title: title.trim(),
        body,
        head,
        base,
        draft,
        reviewers: reviewers.split(",").map((s) => s.trim()).filter(Boolean),
      },
      t("vcs.success.created"),
    );
    if (ok) {
      setCreateOpen(false);
      setTitle("");
      setBody("");
      setDraft(false);
      setReviewers("");
    }
  };

  const handleComment = async (prId: string | number) => {
    if (!commentText.trim()) return;
    const ok = await runOp(
      `/api/v1/vcs/prs/${prId}/comment`,
      "POST",
      { provider, owner, repo, body: commentText.trim() },
      t("vcs.success.commented"),
    );
    if (ok) {
      setCommentText("");
      setExpandedPr(null);
    }
  };

  const handleMerge = async (prId: string | number, method: string = "merge") => {
    await runOp(
      `/api/v1/vcs/prs/${prId}/merge`,
      "POST",
      { provider, owner, repo, method },
      t("vcs.success.merged"),
    );
  };

  const handleClose = async (prId: string | number) => {
    // Task 7.24: Close with comment — 若评论框有内容则作为关闭说明一并提交
    const comment = commentText.trim();
    const ok = await runOp(
      `/api/v1/vcs/prs/${prId}/close`,
      "POST",
      { provider, owner, repo, body: comment },
      t("vcs.success.closed"),
    );
    if (ok) {
      setCommentText("");
      setExpandedPr(null);
    }
  };

  const handleReview = async (prId: string | number) => {
    // Task 7.24: Approve — 评论框内容作为审查附注（可选）
    const comment = commentText.trim();
    const ok = await runOp(
      `/api/v1/vcs/prs/${prId}/review`,
      "POST",
      { provider, owner, repo, event: "APPROVE", body: comment },
      t("vcs.success.approved"),
    );
    if (ok) {
      setCommentText("");
      setExpandedPr(null);
    }
  };

  const prIdentifier = (pr: PRItem) => pr.number ?? pr.iid ?? pr.id ?? "";

  const statusBadge = (state: string) => {
    const s = state?.toLowerCase() || "open";
    if (s === "merged") return <Badge className="text-[9px] h-4 bg-purple-500/20 text-purple-500">{t("vcs.status.merged")}</Badge>;
    if (s === "closed") return <Badge variant="secondary" className="text-[9px] h-4">{t("vcs.status.closed")}</Badge>;
    return <Badge variant="outline" className="text-[9px] h-4 text-success border-success">{t("vcs.status.open")}</Badge>;
  };

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-lg max-h-[80vh] flex flex-col">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-sm">
            <GitPullRequest size={14} />
            {t("vcs.title")}
          </DialogTitle>
        </DialogHeader>

        <div className="flex items-center gap-2 text-[11px] mb-2">
          <Input
            value={owner}
            onChange={(e) => setOwner(e.target.value)}
            placeholder={t("vcs.form.owner")}
            className="h-7 text-[11px] flex-1"
          />
          <span className="text-muted-foreground">/</span>
          <Input
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
            placeholder={t("vcs.form.repo")}
            className="h-7 text-[11px] flex-1"
          />
          <Button size="sm" variant="outline" onClick={loadPRs} disabled={loading} className="text-[10px] h-7 px-2">
            {t("common.refresh")}
          </Button>
        </div>

        <div className="flex items-center justify-between mb-2">
          <span className="text-[10px] text-muted-foreground">
            {providers.length === 0 ? t("vcs.errors.noProvider") : `${provider} • ${t("vcs.listTitle")}`}
          </span>
          <Button size="sm" onClick={() => setCreateOpen(true)} disabled={!provider} className="text-[10px] h-7">
            <Plus size={12} className="mr-1" />
            {t("vcs.createPR")}
          </Button>
        </div>

        <div className="flex-1 overflow-y-auto border rounded divide-y">
          {prs.length === 0 ? (
            <EmptyState compact title={t("vcs.empty")} />
          ) : (
            prs.map((pr) => {
              const id = prIdentifier(pr);
              const expanded = expandedPr === id;
              return (
                <div key={id} className="text-[11px]">
                  <div
                    className="flex items-center gap-2 px-2 py-1.5 hover:bg-secondary cursor-pointer"
                    onClick={() => setExpandedPr(expanded ? null : id)}
                  >
                    {expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                    {statusBadge(pr.state)}
                    <span className="font-medium truncate flex-1">{pr.title}</span>
                    <span className="text-[10px] text-muted-foreground">#{id}</span>
                  </div>
                  {expanded && (
                    <div className="px-2 pb-2 space-y-2 border-t bg-secondary/30">
                      <div className="text-[10px] text-muted-foreground pt-1">
                        {t("vcs.by", { author: pr.user?.login || pr.user?.name || pr.author?.username || "—" })}
                      </div>
                      <Textarea
                        value={commentText}
                        onChange={(e) => setCommentText(e.target.value)}
                        placeholder={t("vcs.actions.commentPlaceholder")}
                        className="text-[11px] min-h-[60px]"
                      />
                      <div className="flex flex-wrap gap-2">
                        <Button size="sm" variant="outline" onClick={() => handleComment(id)} className="text-[10px] h-7">
                          <MessageSquare size={10} className="mr-1" />
                          {t("vcs.actions.comment")}
                        </Button>
                        <Button size="sm" onClick={() => handleMerge(id, "merge")} className="text-[10px] h-7">
                          <GitMerge size={10} className="mr-1" />
                          {t("vcs.actions.merge")}
                        </Button>
                        <Button size="sm" variant="outline" onClick={() => handleMerge(id, "squash")} className="text-[10px] h-7">
                          <GitMerge size={10} className="mr-1" />
                          {t("vcs.actions.squash")}
                        </Button>
                        <Button size="sm" variant="outline" onClick={() => handleClose(id)} className="text-[10px] h-7">
                          <GitPullRequestClosed size={10} className="mr-1" />
                          {t("vcs.actions.close")}
                        </Button>
                        <Button size="sm" variant="outline" onClick={() => handleReview(id)} className="text-[10px] h-7 text-success border-success">
                          <Check size={10} className="mr-1" />
                          {t("vcs.actions.approve")}
                        </Button>
                      </div>
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>

        <Dialog open={createOpen} onOpenChange={(v) => !v && setCreateOpen(false)}>
          <DialogContent className="max-w-sm">
            <DialogHeader>
              <DialogTitle className="text-sm">{t("vcs.createPR")}</DialogTitle>
            </DialogHeader>
            <div className="space-y-2 text-[11px]">
              <div>
                <Label className="text-[10px]">{t("vcs.form.title")}</Label>
                <Input value={title} onChange={(e) => setTitle(e.target.value)} className="h-8 text-[11px]" />
              </div>
              <div>
                <Label className="text-[10px]">{t("vcs.form.body")}</Label>
                <Textarea value={body} onChange={(e) => setBody(e.target.value)} className="text-[11px] min-h-[60px]" />
              </div>
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <Label className="text-[10px]">{t("vcs.form.head")}</Label>
                  <Input value={head} onChange={(e) => setHead(e.target.value)} className="h-8 text-[11px]" />
                </div>
                <div>
                  <Label className="text-[10px]">{t("vcs.form.base")}</Label>
                  <Input value={base} onChange={(e) => setBase(e.target.value)} className="h-8 text-[11px]" />
                </div>
              </div>
              <div>
                <Label className="text-[10px]">{t("vcs.form.reviewers")}</Label>
                <Input
                  value={reviewers}
                  onChange={(e) => setReviewers(e.target.value)}
                  placeholder={t("vcs.form.reviewersPlaceholder")}
                  className="h-8 text-[11px]"
                />
              </div>
              <div className="flex items-center gap-2 py-1">
                <Switch checked={draft} onCheckedChange={setDraft} id="draft" />
                <Label htmlFor="draft" className="text-[10px]">{t("vcs.form.draft")}</Label>
              </div>
              <div className="flex justify-end gap-2 pt-2">
                <Button size="sm" variant="outline" onClick={() => setCreateOpen(false)} className="text-[10px] h-7">
                  {t("common.cancel")}
                </Button>
                <Button size="sm" onClick={handleCreate} disabled={!title.trim() || !provider} className="text-[10px] h-7">
                  {t("vcs.createPR")}
                </Button>
              </div>
            </div>
          </DialogContent>
        </Dialog>
      </DialogContent>
    </Dialog>
  );
}
