import { useEffect, useState, useCallback } from "react";
import { useApi } from "../hooks/useApi";
import { GIT_TAB_I18N_KEYS, LOG_OP_I18N_KEYS } from "./git/gitOpI18n";
import { MergeConflictPanel } from "./MergeConflictPanel";
import { GitAdvancedPanel } from "./GitAdvancedPanel";
import type { BlameLine } from "./GitBlameView";
import { GitOperationDialog } from "./GitOperationDialog";
import { PRPanel } from "./vcs/PRPanel";
import { useTranslation } from "react-i18next";
import { useConfirmDialog } from "../hooks/useConfirmDialog";
import { GitCommit as GitCommitIcon, GitBranch as GitBranchIcon, GitGraph, RotateCcw, Sparkles, GitMerge, Wrench, Tag } from "lucide-react";
import type { GitStatus, GitCommit, GitBranch } from "./git/gitTypes";
import { GitFileList } from "./git/GitFileList";
import { GitDiffView } from "./git/GitDiffView";
import { GitCommitForm } from "./git/GitCommitForm";
import { GitBranchSelector } from "./git/GitBranchSelector";

/** Phase 18 / Phase 51: Git 面板——状态、Diff、历史、分支、分支图、文件级 stage/unstage/discard */
export function GitPanel() {
  const { t } = useTranslation();
  const { api } = useApi();
  const { confirm, confirmDialog } = useConfirmDialog();
  const [tab, setTab] = useState<"status" | "diff" | "log" | "branch" | "graph" | "review" | "advanced">("status");
  const [status, setStatus] = useState<GitStatus | null>(null);
  const [diffText, setDiffText] = useState("");
  const [diffFile, setDiffFile] = useState("");
  const [commits, setCommits] = useState<GitCommit[]>([]);
  const [branches, setBranches] = useState<GitBranch[]>([]);
  const [graph, setGraph] = useState<{ commits: GitCommit[]; branches: GitBranch[] }>({ commits: [], branches: [] });
  const [commitMsg, setCommitMsg] = useState("");
  const [statusMsg, setStatusMsg] = useState("");
  const [loading, setLoading] = useState(false);
  const [newBranch, setNewBranch] = useState("");
  const [reviewResult, setReviewResult] = useState("");
  const [conflictPanelOpen, setConflictPanelOpen] = useState(false);
  const [conflictFilePath, setConflictFilePath] = useState("");
  const [blameLines, setBlameLines] = useState<BlameLine[]>([]);
  const [blameOpen, setBlameOpen] = useState(false);
  const [logOp, setLogOp] = useState<{ open: boolean; hash: string; type: "cherry-pick" | "revert" | "squash" | "fixup" | "tag"; branch?: string } | null>(null);
  const [logOpInput, setLogOpInput] = useState("");
  const [prPanelOpen, setPrPanelOpen] = useState(false);
  const [prDefaults, setPrDefaults] = useState({
    provider: "",
    owner: "",
    repo: "",
    head: "",
    base: "main",
  });
  const [showCreatePR, setShowCreatePR] = useState(false);

  const loadStatus = useCallback(async () => {
    setLoading(true);
    try {
      setStatus(await api<GitStatus>("/api/v1/git/status"));
    } catch {
      setStatus(null);
      setStatusMsg(t("gitPanel.loadStatusFailed"));
    }
    setLoading(false);
  }, [api, t]);

  const loadLog = useCallback(async () => {
    try {
      const r = await api<{ ok: boolean; commits: GitCommit[] }>("/api/v1/git/log");
      if (r.ok) setCommits(r.commits);
    } catch {
      /* ignore */
    }
  }, [api]);

  const loadBranches = useCallback(async () => {
    try {
      const r = await api<{ ok: boolean; branches: GitBranch[] }>("/api/v1/git/branches");
      if (r.ok) setBranches(r.branches);
    } catch {
      /* ignore */
    }
  }, [api]);

  const loadGraph = useCallback(async () => {
    try {
      const r = await api<{ ok: boolean; commits: GitCommit[]; branches: GitBranch[] }>("/api/v1/git/graph");
      if (r.ok) setGraph({ commits: r.commits, branches: r.branches });
    } catch {
      /* ignore */
    }
  }, [api]);

  useEffect(() => {
    loadStatus();
    loadLog();
  }, [loadStatus, loadLog]);

  const refreshAll = useCallback(() => {
    loadStatus();
    loadLog();
    if (tab === "branch") loadBranches();
    if (tab === "graph") loadGraph();
  }, [loadStatus, loadLog, loadBranches, loadGraph, tab]);

  const doCheckout = async (branch: string) => {
    setLoading(true);
    setStatusMsg(t("gitPanel.checkout.switching", { branch }));
    try {
      const r = await api<{ ok: boolean; error?: string }>("/api/v1/git/checkout", {
        method: "POST",
        body: JSON.stringify({ branch }),
      });
      setStatusMsg(
        r.ok ? t("gitPanel.checkout.success", { branch }) : t("gitPanel.checkout.error", { error: r.error || "" }),
      );
      if (r.ok) refreshAll();
    } catch {
      setStatusMsg(t("gitPanel.checkout.failed"));
    }
    setLoading(false);
  };

  const doCreateBranch = async () => {
    if (!newBranch.trim()) return;
    setLoading(true);
    setStatusMsg(t("gitPanel.createBranch.creating", { branch: newBranch }));
    try {
      const r = await api<{ ok: boolean; error?: string }>("/api/v1/git/branch", {
        method: "POST",
        body: JSON.stringify({ branch: newBranch }),
      });
      setStatusMsg(
        r.ok
          ? t("gitPanel.createBranch.success", { branch: newBranch })
          : t("gitPanel.createBranch.error", { error: r.error || "" }),
      );
      setNewBranch("");
      if (r.ok) refreshAll();
    } catch {
      setStatusMsg(t("gitPanel.createBranch.failed"));
    }
    setLoading(false);
  };

  const doPull = async () => {
    setLoading(true);
    setStatusMsg(t("gitPanel.pull.pulling"));
    try {
      const r = await api<{ ok: boolean; error?: string }>("/api/v1/git/pull", { method: "POST" });
      setStatusMsg(r.ok ? t("gitPanel.pull.success") : t("gitPanel.pull.error", { error: r.error || "" }));
      if (r.ok) refreshAll();
    } catch {
      setStatusMsg(t("gitPanel.pull.failed"));
    }
    setLoading(false);
  };

  const doStash = async () => {
    setLoading(true);
    setStatusMsg(t("gitPanel.stash.stashing"));
    try {
      const r = await api<{ ok: boolean; error?: string }>("/api/v1/git/stash", {
        method: "POST",
        body: JSON.stringify({ message: "" }),
      });
      setStatusMsg(r.ok ? t("gitPanel.stash.success") : t("gitPanel.stash.error", { error: r.error || "" }));
      if (r.ok) refreshAll();
    } catch {
      setStatusMsg(t("gitPanel.stash.failed"));
    }
    setLoading(false);
  };

  const doStashPop = async () => {
    setLoading(true);
    setStatusMsg(t("gitPanel.stashPop.restoring"));
    try {
      const r = await api<{ ok: boolean; error?: string }>("/api/v1/git/stash-pop", { method: "POST" });
      setStatusMsg(r.ok ? t("gitPanel.stashPop.success") : t("gitPanel.stashPop.error", { error: r.error || "" }));
      if (r.ok) refreshAll();
    } catch {
      setStatusMsg(t("gitPanel.stashPop.failed"));
    }
    setLoading(false);
  };

  const loadDiff = async (staged: boolean, filePath?: string) => {
    try {
      const params = new URLSearchParams();
      if (staged) params.set("staged", "true");
      if (filePath) params.set("path", filePath);
      const r = await api<{ diff: string }>(`/api/v1/git/diff?${params.toString()}`);
      setDiffText(r.diff);
      setDiffFile(filePath || "all changes");
      setTab("diff");
      setBlameOpen(false);
      setBlameLines([]);
    } catch {
      setStatusMsg(t("gitPanel.diff.failed"));
    }
  };

  const loadBlame = async (filePath: string) => {
    try {
      const r = await api<{ ok: boolean; lines?: BlameLine[]; error?: string }>(`/api/v1/git/blame?path=${encodeURIComponent(filePath)}`);
      if (r.ok && r.lines) {
        setBlameLines(r.lines);
        setBlameOpen(true);
      } else {
        setStatusMsg(r.error || t("gitPanel.blame.failed"));
      }
    } catch {
      setStatusMsg(t("gitPanel.blame.failed"));
    }
  };

  const doCommit = async () => {
    if (!commitMsg.trim()) return;
    setLoading(true);
    setStatusMsg(t("gitPanel.commit.committing"));
    try {
      const r = await api<{ ok: boolean; commit_hash?: string; error?: string }>("/api/v1/git/commit", {
        method: "POST",
        body: JSON.stringify({ message: commitMsg }),
      });
      if (r.ok) {
        setStatusMsg(t("gitPanel.commit.success", { hash: r.commit_hash?.slice(0, 7) || "" }));
        setCommitMsg("");
        refreshAll();
      } else {
        setStatusMsg(t("gitPanel.commit.error", { error: r.error || "" }));
      }
    } catch {
      setStatusMsg(t("gitPanel.commit.failed"));
    }
    setLoading(false);
  };

  const doPush = async () => {
    setLoading(true);
    setStatusMsg(t("gitPanel.push.pushing"));
    try {
      const r = await api<{ ok: boolean; error?: string }>("/api/v1/git/push", { method: "POST" });
      setStatusMsg(r.ok ? t("gitPanel.push.success") : t("gitPanel.push.error", { error: r.error || "" }));
      if (r.ok) {
        refreshAll();
        await detectRemoteForPR();
      }
    } catch {
      setStatusMsg(t("gitPanel.push.failed"));
    }
    setLoading(false);
  };

  const detectRemoteForPR = async () => {
    try {
      const r = await api<{ ok: boolean; provider?: string; owner?: string; repo?: string }>("/api/v1/git/remote-url");
      if (r.ok && r.provider && r.provider !== "unknown") {
        setPrDefaults({
          provider: r.provider,
          owner: r.owner || "",
          repo: r.repo || "",
          head: status?.branch || "",
          base: "main",
        });
        setShowCreatePR(true);
      }
    } catch {
      setShowCreatePR(false);
    }
  };

  const stageFile = async (path: string, stage: boolean) => {
    try {
      const r = await api<{ ok: boolean; error?: string }>(stage ? "/api/v1/git/stage" : "/api/v1/git/unstage", {
        method: "POST",
        body: JSON.stringify({ path }),
      });
      if (!r.ok) setStatusMsg(r.error || "");
      else loadStatus();
    } catch {
      setStatusMsg(t("gitPanel.stage.failed"));
    }
  };

  const discardFile = async (path: string) => {
    const ok = await confirm({
      title: t("common.confirm"),
      description: t("gitPanel.discard.confirm", { path }),
      variant: "destructive",
    });
    if (!ok) return;
    try {
      const r = await api<{ ok: boolean; error?: string }>("/api/v1/git/discard", {
        method: "POST",
        body: JSON.stringify({ path }),
      });
      if (!r.ok) setStatusMsg(r.error || "");
      else loadStatus();
    } catch {
      setStatusMsg(t("gitPanel.discard.failed"));
    }
  };

  const suggestCommitMessage = async () => {
    try {
      const r = await api<{ message?: string; error?: string }>("/api/v1/git/suggest-commit-message", {
        method: "POST",
      });
      if (r.message) setCommitMsg(r.message);
      else setStatusMsg(r.error || t("gitPanel.suggest.empty"));
    } catch {
      setStatusMsg(t("gitPanel.suggest.failed"));
    }
  };

  const runAiReview = async () => {
    setLoading(true);
    setStatusMsg(t("gitPanel.review.running"));
    try {
      const r = await api<{ review?: string; error?: string }>("/api/v1/git/ai-review", { method: "POST" });
      if (r.review) {
        setReviewResult(r.review);
        setTab("review");
      } else {
        setStatusMsg(r.error || t("gitPanel.review.empty"));
      }
    } catch {
      setStatusMsg(t("gitPanel.review.failed"));
    }
    setLoading(false);
  };

  const runLogOp = async () => {
    if (!logOp) return;
    const { type, hash } = logOp;
    setLoading(true);
    try {
      let endpoint = "";
      let body: Record<string, unknown> = { hash };
      let successMsg = "";
      if (type === "cherry-pick") {
        endpoint = "/api/v1/git/cherry-pick";
        successMsg = t("gitPanel.advanced.success");
      } else if (type === "revert") {
        endpoint = "/api/v1/git/revert";
        successMsg = t("gitPanel.advanced.success");
      } else if (type === "squash") {
        endpoint = "/api/v1/git/rebase/squash";
        body = { base_ref: hash, message: logOpInput };
        successMsg = t("gitPanel.advanced.success");
      } else if (type === "fixup") {
        endpoint = "/api/v1/git/rebase/fixup";
        body = { target_hash: hash };
        successMsg = t("gitPanel.advanced.success");
      } else if (type === "tag") {
        endpoint = "/api/v1/git/tags";
        body = { name: logOpInput, target: hash };
        successMsg = t("gitPanel.advanced.success");
      }
      const r = await api<{ ok: boolean; error?: string }>(endpoint, { method: "POST", body: JSON.stringify(body) });
      setStatusMsg(r.ok ? successMsg : t("gitPanel.advanced.error", { error: r.error || "" }));
      if (r.ok) {
        setLogOp(null);
        setLogOpInput("");
        refreshAll();
      }
    } catch {
      setStatusMsg(t("gitPanel.advanced.failed"));
    }
    setLoading(false);
  };

  const fileCount = status?.files?.length || 0;

  const openConflictPanel = (path: string) => {
    setConflictFilePath(path);
    setConflictPanelOpen(true);
  };

  const handleTab = (tabName: typeof tab) => {
    setTab(tabName);
    if (tabName === "log") loadLog();
    if (tabName === "branch") loadBranches();
    if (tabName === "graph") loadGraph();
    if (tabName !== "diff") {
      setBlameOpen(false);
      setBlameLines([]);
    }
  };

  if (!status) {
    return (
      <div className="border-t shrink-0 bg-background">
        <div className="h-7 flex items-center bg-secondary px-3 border-b">
          <span className="text-[10px] font-semibold text-muted-foreground">Git</span>
        </div>
        <div className="h-[200px] flex items-center justify-center text-xs text-muted-foreground">
          {loading ? t("gitPanel.loading") : t("gitPanel.connectionFailed")}
        </div>
      </div>
    );
  }

  if (!status.ok) {
    return (
      <div className="border-t shrink-0 bg-background">
        <div className="h-7 flex items-center bg-secondary px-3 border-b">
          <span className="text-[10px] font-semibold text-muted-foreground">Git</span>
        </div>
        <div className="h-[200px] flex flex-col items-center justify-center gap-2 text-xs text-muted-foreground">
          <span>{status.error || t("gitPanel.notGitRepo")}</span>
          <button
            onClick={async () => {
              try {
                await api("/api/v1/git/init", { method: "POST" });
                loadStatus();
              } catch {
                setStatusMsg(t("gitPanel.initFailed"));
              }
            }}
            className="text-[10px] text-primary hover:underline"
          >
            {t("gitPanel.runGitInit")}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="border-t shrink-0 bg-background flex flex-col" style={{ height: 260 }}>
      {/* 标题栏 + Tab */}
      <div className="h-7 flex items-center bg-secondary px-2 border-b gap-1 shrink-0">
        {(["status", "diff", "log", "branch", "graph", "review", "advanced"] as const).map((tabName) => (
          <button
            key={tabName}
            onClick={() => handleTab(tabName)}
            className={`text-[10px] px-2 py-0.5 rounded transition-colors flex items-center gap-1 ${
              tab === tabName ? "bg-background font-semibold" : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {tabName === "status" && <RotateCcw size={10} />}
            {tabName === "diff" && <GitCommitIcon size={10} />}
            {tabName === "log" && <GitCommitIcon size={10} />}
            {tabName === "branch" && <GitBranchIcon size={10} />}
            {tabName === "graph" && <GitGraph size={10} />}
            {tabName === "review" && <Sparkles size={10} />}
            {tabName === "advanced" && <Wrench size={10} />}
            {tabName === "status" ? t(GIT_TAB_I18N_KEYS.status, { count: fileCount }) : t(GIT_TAB_I18N_KEYS[tabName] ?? tabName)}
          </button>
        ))}
        <div className="flex-1" />
        <span className="text-[10px] text-muted-foreground">
          {status.branch}
          {status.ahead > 0 && <span className="text-success ml-1">↑{status.ahead}</span>}
          {status.behind > 0 && <span className="text-warning ml-1">↓{status.behind}</span>}
        </span>
      </div>

      {/* 操作栏 */}
      <GitCommitForm
        commitMsg={commitMsg}
        setCommitMsg={setCommitMsg}
        diffFile={diffFile}
        loading={loading}
        showCreatePR={showCreatePR}
        onLoadDiff={loadDiff}
        onLoadBlame={loadBlame}
        onSuggestCommitMessage={suggestCommitMessage}
        onRunAiReview={runAiReview}
        onCommit={doCommit}
        onPush={doPush}
        onOpenPR={() => setPrPanelOpen(true)}
      />

      {/* 状态消息 */}
      {statusMsg && (
        <div
          className={`px-2 py-0.5 text-[10px] font-mono border-b ${
            statusMsg.startsWith("OK") ? "text-success bg-success/10" : "text-destructive bg-destructive/10"
          }`}
        >
          {statusMsg}
        </div>
      )}

      {/* 内容区 */}
      <div className="flex-1 overflow-y-auto text-[11px]">
        {/* 文件状态 Tab */}
        {tab === "status" && (
          <GitFileList
            files={status.files}
            onOpenConflict={openConflictPanel}
            onLoadDiff={(path) => loadDiff(false, path)}
            onStageToggle={stageFile}
            onDiscard={discardFile}
          />
        )}

        {/* Diff Tab */}
        {tab === "diff" && (
          <GitDiffView
            diffText={diffText}
            diffFile={diffFile}
            blameOpen={blameOpen}
            blameLines={blameLines}
            onCloseBlame={() => setBlameOpen(false)}
          />
        )}

        {/* 历史 Tab */}
        {tab === "log" && (
          <div>
            {commits.length === 0 ? (
              <p className="text-muted-foreground text-center py-4">{t("gitPanel.noCommits")}</p>
            ) : (
              commits.map((c) => (
                <div key={c.hash} className="px-2 py-1 border-b border-border hover:bg-secondary group">
                  <div className="flex items-center gap-2">
                    <code className="text-[10px] text-primary font-mono">{c.hash}</code>
                    <span className="text-xs truncate flex-1">{c.message}</span>
                    <div className="hidden group-hover:flex items-center gap-0.5 shrink-0">
                      <button
                        onClick={() => setLogOp({ open: true, hash: c.hash, type: "cherry-pick" })}
                        title={t("gitPanel.advanced.cherryPickTitle")}
                        aria-label={t("gitPanel.advanced.cherryPickTitle")}
                        className="p-0.5 hover:bg-secondary rounded text-muted-foreground"
                      >
                        <GitCommitIcon size={10} />
                      </button>
                      <button
                        onClick={() => setLogOp({ open: true, hash: c.hash, type: "revert" })}
                        title={t("gitPanel.advanced.revertTitle")}
                        aria-label={t("gitPanel.advanced.revertTitle")}
                        className="p-0.5 hover:bg-secondary rounded text-muted-foreground"
                      >
                        <RotateCcw size={10} />
                      </button>
                      <button
                        onClick={() => setLogOp({ open: true, hash: c.hash, type: "squash" })}
                        title={t("gitPanel.advanced.squashTitle")}
                        aria-label={t("gitPanel.advanced.squashTitle")}
                        className="p-0.5 hover:bg-secondary rounded text-muted-foreground"
                      >
                        <GitMerge size={10} />
                      </button>
                      <button
                        onClick={() => setLogOp({ open: true, hash: c.hash, type: "fixup" })}
                        title={t("gitPanel.advanced.fixupTitle")}
                        aria-label={t("gitPanel.advanced.fixupTitle")}
                        className="p-0.5 hover:bg-secondary rounded text-muted-foreground"
                      >
                        <Wrench size={10} />
                      </button>
                      <button
                        onClick={() => setLogOp({ open: true, hash: c.hash, type: "tag" })}
                        title={t("gitPanel.advanced.tagTitle")}
                        aria-label={t("gitPanel.advanced.tagTitle")}
                        className="p-0.5 hover:bg-secondary rounded text-muted-foreground"
                      >
                        <Tag size={10} />
                      </button>
                    </div>
                    <span className="text-[10px] text-muted-foreground shrink-0 group-hover:hidden">{c.time}</span>
                  </div>
                  <span className="text-[10px] text-muted-foreground">{c.author}</span>
                </div>
              ))
            )}
          </div>
        )}

        {/* 分支管理 Tab */}
        {tab === "branch" && (
          <GitBranchSelector
            branches={branches}
            newBranch={newBranch}
            setNewBranch={setNewBranch}
            loading={loading}
            onCreateBranch={doCreateBranch}
            onCheckout={doCheckout}
            onPull={doPull}
            onStash={doStash}
            onStashPop={doStashPop}
          />
        )}

        {/* 分支图 Tab */}
        {tab === "graph" && (
          <div className="p-2">
            {graph.commits.length === 0 ? (
              <p className="text-muted-foreground text-center py-4">{t("gitPanel.noCommits")}</p>
            ) : (
              <div className="space-y-1">
                {graph.commits.map((c) => {
                  const branchNames = graph.branches.filter((b) => b.hash === c.hash).map((b) => b.name);
                  return (
                    <div
                      key={c.hash}
                      className="flex items-start gap-2 text-[10px] border-l-2 border-primary/30 pl-2 py-0.5 hover:bg-secondary/50"
                    >
                      <code className="text-primary font-mono shrink-0">{c.hash}</code>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-1">
                          <span className="truncate font-medium">{c.message}</span>
                          {branchNames.map((name) => (
                            <span
                              key={name}
                              className="shrink-0 px-1 py-0 bg-primary/10 text-primary rounded text-[9px]"
                            >
                              {name}
                            </span>
                          ))}
                        </div>
                        <div className="text-muted-foreground">
                          {c.author} · {c.time}
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        )}

        {/* AI 审查 Tab */}
        {tab === "review" && (
          <div className="p-2 h-full overflow-y-auto">
            {reviewResult ? (
              <div className="whitespace-pre-wrap text-[11px] leading-relaxed font-mono">{reviewResult}</div>
            ) : (
              <div className="flex flex-col items-center justify-center h-full gap-2 text-muted-foreground">
                <Sparkles size={16} />
                <p className="text-[11px]">{t("gitPanel.review.hint")}</p>
                <button
                  onClick={runAiReview}
                  disabled={loading}
                  className="text-[10px] px-3 py-1 bg-primary text-primary-foreground rounded font-semibold disabled:opacity-50"
                >
                  {t("gitPanel.review.run")}
                </button>
              </div>
            )}
          </div>
        )}

        {/* 高级操作 Tab */}
        {tab === "advanced" && (
          <GitAdvancedPanel onRefresh={refreshAll} onStatusMsg={setStatusMsg} />
        )}
      </div>

      <GitOperationDialog
        open={!!logOp?.open}
        onCancel={() => { setLogOp(null); setLogOpInput(""); }}
        title={logOp ? t(LOG_OP_I18N_KEYS[logOp.type]?.title ?? "gitPanel.advanced.tagTitle") : ""}
        message={
          logOp
            ? t(LOG_OP_I18N_KEYS[logOp.type]?.confirm ?? "gitPanel.advanced.tagConfirm", {
                hash: logOp.hash,
              })
            : ""
        }
        onConfirm={runLogOp}
        input={logOp && (logOp.type === "squash" || logOp.type === "tag") ? {
          value: logOpInput,
          onChange: setLogOpInput,
          placeholder: logOp.type === "squash" ? t("gitPanel.advanced.squashMessage") : t("gitPanel.advanced.tagName"),
        } : undefined}
      />

      <MergeConflictPanel
        open={conflictPanelOpen}
        onClose={() => setConflictPanelOpen(false)}
        filePath={conflictFilePath}
        onResolved={() => {
          setStatusMsg(t("gitPanel.conflictResolved"));
          loadStatus();
        }}
      />

      <PRPanel
        open={prPanelOpen}
        onClose={() => setPrPanelOpen(false)}
        defaultProvider={prDefaults.provider}
        defaultOwner={prDefaults.owner}
        defaultRepo={prDefaults.repo}
        defaultHead={prDefaults.head}
        defaultBase={prDefaults.base}
        onStatusMsg={setStatusMsg}
      />
      {confirmDialog}
    </div>
  );
}
