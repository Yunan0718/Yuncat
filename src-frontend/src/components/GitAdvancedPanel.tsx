import { useEffect, useState, useCallback } from "react";
import { useApi } from "../hooks/useApi";
import { useTranslation } from "react-i18next";
import { GitOperationDialog } from "./GitOperationDialog";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { RotateCcw, Trash2, GitCommit, GitBranch, Tag, Globe } from "lucide-react";

interface RebaseStatus {
  ok: boolean;
  in_progress: {
    rebase: boolean;
    merge: boolean;
    cherry_pick: boolean;
    revert: boolean;
  };
  any: boolean;
}

interface GitTag {
  name: string;
  message: string;
}

interface GitRemote {
  name: string;
  fetch?: string;
  push?: string;
}

interface GitAdvancedPanelProps {
  onRefresh?: () => void;
  onStatusMsg?: (msg: string) => void;
}

/** Phase 103: Git 高级操作面板 */
export function GitAdvancedPanel({ onRefresh, onStatusMsg }: GitAdvancedPanelProps) {
  const { t } = useTranslation();
  const { api } = useApi();
  const [rebaseStatus, setRebaseStatus] = useState<RebaseStatus | null>(null);
  const [tags, setTags] = useState<GitTag[]>([]);
  const [remotes, setRemotes] = useState<GitRemote[]>([]);

  const loadRebaseStatus = useCallback(async () => {
    try {
      const r = await api<RebaseStatus>("/api/v1/git/rebase-status");
      if (r.ok) setRebaseStatus(r);
    } catch {
      /* ignore */
    }
  }, [api]);

  const loadTags = useCallback(async () => {
    try {
      const r = await api<{ ok: boolean; tags: GitTag[] }>("/api/v1/git/tags");
      if (r.ok) setTags(r.tags);
    } catch {
      /* ignore */
    }
  }, [api]);

  const loadRemotes = useCallback(async () => {
    try {
      const r = await api<{ ok: boolean; remotes: GitRemote[] }>("/api/v1/git/remotes");
      if (r.ok) setRemotes(r.remotes);
    } catch {
      /* ignore */
    }
  }, [api]);

  useEffect(() => {
    loadRebaseStatus();
    loadTags();
    loadRemotes();
  }, [loadRebaseStatus, loadTags, loadRemotes]);

  const runOp = async (endpoint: string, body: unknown, successMsg: string) => {
    try {
      const r = await api<{ ok: boolean; error?: string }>(endpoint, {
        method: "POST",
        body: JSON.stringify(body),
      });
      onStatusMsg?.(r.ok ? successMsg : t("gitPanel.advanced.error", { error: r.error || "" }));
      if (r.ok) {
        loadRebaseStatus();
        loadTags();
        loadRemotes();
        onRefresh?.();
      }
    } catch {
      onStatusMsg?.(t("gitPanel.advanced.failed"));
    }
  };

  return (
    <div className="p-2 space-y-3 overflow-y-auto h-full text-[11px]">
      <RebaseStatusSection
        status={rebaseStatus}
        onRefresh={loadRebaseStatus}
        onStatusMsg={onStatusMsg}
        api={api}
      />
      <SquashSection onRun={(body, msg) => runOp("/api/v1/git/rebase/squash", body, msg)} />
      <FixupSection onRun={(body, msg) => runOp("/api/v1/git/rebase/fixup", body, msg)} />
      <OntoSection onRun={(body, msg) => runOp("/api/v1/git/rebase/onto", body, msg)} />
      <CherryPickRevertSection onRun={(endpoint, body, msg) => runOp(endpoint, body, msg)} />
      <TagSection tags={tags} onRefresh={loadTags} onStatusMsg={onStatusMsg} api={api} />
      <RemoteSection remotes={remotes} onRefresh={loadRemotes} onStatusMsg={onStatusMsg} api={api} />
    </div>
  );
}

function RebaseStatusSection({
  status,
  onRefresh,
  onStatusMsg,
  api,
}: {
  status: RebaseStatus | null;
  onRefresh: () => void;
  onStatusMsg?: (msg: string) => void;
  api: ReturnType<typeof useApi>["api"];
}) {
  const { t } = useTranslation();

  const doContinue = async () => {
    try {
      const r = await api<{ ok: boolean; error?: string }>("/api/v1/git/rebase/continue", { method: "POST" });
      onStatusMsg?.(r.ok ? t("gitPanel.advanced.success") : t("gitPanel.advanced.error", { error: r.error || "" }));
      if (r.ok) onRefresh();
    } catch {
      onStatusMsg?.(t("gitPanel.advanced.failed"));
    }
  };

  const doAbort = async () => {
    try {
      const r = await api<{ ok: boolean; error?: string }>("/api/v1/git/rebase/abort", { method: "POST" });
      onStatusMsg?.(r.ok ? t("gitPanel.advanced.success") : t("gitPanel.advanced.error", { error: r.error || "" }));
      if (r.ok) onRefresh();
    } catch {
      onStatusMsg?.(t("gitPanel.advanced.failed"));
    }
  };

  const activeOps = status
    ? Object.entries(status.in_progress)
        .filter(([, v]) => v)
        .map(([k]) => k)
    : [];

  return (
    <div className="border rounded p-2 space-y-1">
      <div className="font-semibold flex items-center gap-1">
        <GitBranch size={11} />
        {t("gitPanel.advanced.rebaseStatus")}
      </div>
      <div className="flex items-center justify-between">
        <span className={activeOps.length ? "text-warning" : "text-muted-foreground"}>
          {activeOps.length ? activeOps.join(", ") : t("gitPanel.advanced.rebaseNone")}
        </span>
        <div className="flex items-center gap-1">
          <Button variant="outline" size="sm" onClick={doContinue} disabled={!status?.any} className="h-6 text-[10px]">
            {t("gitPanel.advanced.rebaseContinue")}
          </Button>
          <Button variant="outline" size="sm" onClick={doAbort} disabled={!status?.any} className="h-6 text-[10px]">
            {t("gitPanel.advanced.rebaseAbort")}
          </Button>
        </div>
      </div>
    </div>
  );
}

function SquashSection({ onRun }: { onRun: (body: object, msg: string) => void }) {
  const { t } = useTranslation();
  const [baseRef, setBaseRef] = useState("");
  const [message, setMessage] = useState("");
  const [dialogOpen, setDialogOpen] = useState(false);

  const handle = () => {
    setDialogOpen(false);
    onRun({ base_ref: baseRef, message }, t("gitPanel.advanced.success"));
  };

  return (
    <div className="border rounded p-2 space-y-1">
      <div className="font-semibold flex items-center gap-1">
        <GitCommit size={11} />
        {t("gitPanel.advanced.squashTitle")}
      </div>
      <Input
        value={baseRef}
        onChange={(e) => setBaseRef(e.target.value)}
        placeholder={t("gitPanel.advanced.squashBase")}
        className="h-7 text-[10px]"
      />
      <Input
        value={message}
        onChange={(e) => setMessage(e.target.value)}
        placeholder={t("gitPanel.advanced.squashMessage")}
        className="h-7 text-[10px]"
      />
      <Button
        size="sm"
        disabled={!baseRef.trim() || !message.trim()}
        onClick={() => setDialogOpen(true)}
        className="h-6 text-[10px]"
      >
        {t("gitPanel.advanced.squashButton")}
      </Button>
      <GitOperationDialog
        open={dialogOpen}
        onCancel={() => setDialogOpen(false)}
        title={t("gitPanel.advanced.squashTitle")}
        message={t("gitPanel.advanced.squashConfirm", { base: baseRef })}
        onConfirm={handle}
      />
    </div>
  );
}

function FixupSection({ onRun }: { onRun: (body: object, msg: string) => void }) {
  const { t } = useTranslation();
  const [target, setTarget] = useState("");
  const [dialogOpen, setDialogOpen] = useState(false);

  const handle = () => {
    setDialogOpen(false);
    onRun({ target_hash: target }, t("gitPanel.advanced.success"));
  };

  return (
    <div className="border rounded p-2 space-y-1">
      <div className="font-semibold">{t("gitPanel.advanced.fixupTitle")}</div>
      <Input
        value={target}
        onChange={(e) => setTarget(e.target.value)}
        placeholder={t("gitPanel.advanced.fixupTarget")}
        className="h-7 text-[10px]"
      />
      <Button size="sm" disabled={!target.trim()} onClick={() => setDialogOpen(true)} className="h-6 text-[10px]">
        {t("gitPanel.advanced.fixupButton")}
      </Button>
      <GitOperationDialog
        open={dialogOpen}
        onCancel={() => setDialogOpen(false)}
        title={t("gitPanel.advanced.fixupTitle")}
        message={t("gitPanel.advanced.fixupConfirm", { hash: target })}
        onConfirm={handle}
      />
    </div>
  );
}

function OntoSection({ onRun }: { onRun: (body: object, msg: string) => void }) {
  const { t } = useTranslation();
  const [upstream, setUpstream] = useState("");
  const [branch, setBranch] = useState("");

  const handle = () => {
    const body: { upstream: string; branch?: string } = { upstream };
    if (branch.trim()) body.branch = branch;
    onRun(body, t("gitPanel.advanced.success"));
  };

  return (
    <div className="border rounded p-2 space-y-1">
      <div className="font-semibold">{t("gitPanel.advanced.ontoTitle")}</div>
      <Input
        value={upstream}
        onChange={(e) => setUpstream(e.target.value)}
        placeholder={t("gitPanel.advanced.ontoUpstream")}
        className="h-7 text-[10px]"
      />
      <Input
        value={branch}
        onChange={(e) => setBranch(e.target.value)}
        placeholder={t("gitPanel.advanced.ontoBranch")}
        className="h-7 text-[10px]"
      />
      <Button size="sm" disabled={!upstream.trim()} onClick={handle} className="h-6 text-[10px]">
        {t("gitPanel.advanced.ontoButton")}
      </Button>
    </div>
  );
}

function CherryPickRevertSection({
  onRun,
}: {
  onRun: (endpoint: string, body: object, msg: string) => void;
}) {
  const { t } = useTranslation();
  const [hash, setHash] = useState("");
  const [noCommit, setNoCommit] = useState(false);

  const run = (endpoint: string) => {
    onRun(endpoint, { hash, no_commit: noCommit }, t("gitPanel.advanced.success"));
  };

  return (
    <div className="border rounded p-2 space-y-1">
      <div className="font-semibold">{t("gitPanel.advanced.cherryPickTitle")} / {t("gitPanel.advanced.revertTitle")}</div>
      <Input
        value={hash}
        onChange={(e) => setHash(e.target.value)}
        placeholder={t("gitPanel.advanced.hash")}
        className="h-7 text-[10px]"
      />
      <label className="flex items-center gap-1 text-[10px] text-muted-foreground">
        <input type="checkbox" checked={noCommit} onChange={(e) => setNoCommit(e.target.checked)} />
        {t("gitPanel.advanced.noCommit")}
      </label>
      <div className="flex gap-1">
        <Button size="sm" disabled={!hash.trim()} onClick={() => run("/api/v1/git/cherry-pick")} className="h-6 text-[10px]">
          {t("gitPanel.advanced.cherryPickTitle")}
        </Button>
        <Button size="sm" disabled={!hash.trim()} onClick={() => run("/api/v1/git/revert")} className="h-6 text-[10px]">
          {t("gitPanel.advanced.revertTitle")}
        </Button>
      </div>
    </div>
  );
}

function TagSection({
  tags,
  onRefresh,
  onStatusMsg,
  api,
}: {
  tags: GitTag[];
  onRefresh: () => void;
  onStatusMsg?: (msg: string) => void;
  api: ReturnType<typeof useApi>["api"];
}) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [target, setTarget] = useState("");
  const [annotate, setAnnotate] = useState(false);
  const [message, setMessage] = useState("");
  const [deleteName, setDeleteName] = useState<string | null>(null);

  const create = async () => {
    try {
      const r = await api<{ ok: boolean; error?: string }>("/api/v1/git/tags", {
        method: "POST",
        body: JSON.stringify({
          name,
          target,
          annotate,
          message: annotate ? message : "",
        }),
      });
      onStatusMsg?.(r.ok ? t("gitPanel.advanced.success") : t("gitPanel.advanced.error", { error: r.error || "" }));
      if (r.ok) {
        setName("");
        setTarget("");
        setMessage("");
        onRefresh();
      }
    } catch {
      onStatusMsg?.(t("gitPanel.advanced.failed"));
    }
  };

  const remove = async () => {
    if (!deleteName) return;
    try {
      const r = await api<{ ok: boolean; error?: string }>(`/api/v1/git/tags/${encodeURIComponent(deleteName)}`, {
        method: "DELETE",
      });
      onStatusMsg?.(r.ok ? t("gitPanel.advanced.success") : t("gitPanel.advanced.error", { error: r.error || "" }));
      setDeleteName(null);
      if (r.ok) onRefresh();
    } catch {
      onStatusMsg?.(t("gitPanel.advanced.failed"));
    }
  };

  return (
    <div className="border rounded p-2 space-y-1">
      <div className="font-semibold flex items-center gap-1">
        <Tag size={11} />
        {t("gitPanel.advanced.tagTitle")}
      </div>
      <div className="max-h-[100px] overflow-y-auto space-y-0.5">
        {tags.map((tag) => (
          <div key={tag.name} className="flex items-center justify-between hover:bg-secondary/50 px-1 rounded">
            <span className="font-mono text-[10px]">{tag.name}</span>
            <button onClick={() => setDeleteName(tag.name)} className="p-0.5 hover:bg-destructive/10 rounded text-destructive">
              <Trash2 size={10} />
            </button>
          </div>
        ))}
      </div>
      <div className="flex gap-1">
        <Input value={name} onChange={(e) => setName(e.target.value)} placeholder={t("gitPanel.advanced.tagName")} className="h-7 text-[10px] flex-1" />
        <Input value={target} onChange={(e) => setTarget(e.target.value)} placeholder={t("gitPanel.advanced.tagTarget")} className="h-7 text-[10px] flex-1" />
      </div>
      <label className="flex items-center gap-1 text-[10px] text-muted-foreground">
        <input type="checkbox" checked={annotate} onChange={(e) => setAnnotate(e.target.checked)} />
        {t("gitPanel.advanced.tagAnnotated")}
      </label>
      {annotate && (
        <Input value={message} onChange={(e) => setMessage(e.target.value)} placeholder={t("gitPanel.advanced.tagMessage")} className="h-7 text-[10px]" />
      )}
      <Button size="sm" disabled={!name.trim()} onClick={create} className="h-6 text-[10px]">
        {t("gitPanel.advanced.tagCreate")}
      </Button>
      <GitOperationDialog
        open={!!deleteName}
        onCancel={() => setDeleteName(null)}
        title={t("gitPanel.advanced.tagTitle")}
        message={t("gitPanel.advanced.tagDeleteConfirm", { name: deleteName || "" })}
        onConfirm={remove}
      />
    </div>
  );
}

function RemoteSection({
  remotes,
  onRefresh,
  onStatusMsg,
  api,
}: {
  remotes: GitRemote[];
  onRefresh: () => void;
  onStatusMsg?: (msg: string) => void;
  api: ReturnType<typeof useApi>["api"];
}) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [editUrl, setEditUrl] = useState("");
  const [deleteName, setDeleteName] = useState<string | null>(null);

  const add = async () => {
    try {
      const r = await api<{ ok: boolean; error?: string }>("/api/v1/git/remotes", {
        method: "POST",
        body: JSON.stringify({ name, url }),
      });
      onStatusMsg?.(r.ok ? t("gitPanel.advanced.success") : t("gitPanel.advanced.error", { error: r.error || "" }));
      if (r.ok) {
        setName("");
        setUrl("");
        onRefresh();
      }
    } catch {
      onStatusMsg?.(t("gitPanel.advanced.failed"));
    }
  };

  const update = async () => {
    if (!editing) return;
    try {
      const r = await api<{ ok: boolean; error?: string }>(`/api/v1/git/remotes/${encodeURIComponent(editing)}`, {
        method: "PUT",
        body: JSON.stringify({ url: editUrl }),
      });
      onStatusMsg?.(r.ok ? t("gitPanel.advanced.success") : t("gitPanel.advanced.error", { error: r.error || "" }));
      setEditing(null);
      if (r.ok) onRefresh();
    } catch {
      onStatusMsg?.(t("gitPanel.advanced.failed"));
    }
  };

  const remove = async () => {
    if (!deleteName) return;
    try {
      const r = await api<{ ok: boolean; error?: string }>(`/api/v1/git/remotes/${encodeURIComponent(deleteName)}`, {
        method: "DELETE",
      });
      onStatusMsg?.(r.ok ? t("gitPanel.advanced.success") : t("gitPanel.advanced.error", { error: r.error || "" }));
      setDeleteName(null);
      if (r.ok) onRefresh();
    } catch {
      onStatusMsg?.(t("gitPanel.advanced.failed"));
    }
  };

  return (
    <div className="border rounded p-2 space-y-1">
      <div className="font-semibold flex items-center gap-1">
        <Globe size={11} />
        {t("gitPanel.advanced.remoteTitle")}
      </div>
      <div className="max-h-[100px] overflow-y-auto space-y-0.5">
        {remotes.map((remote) => (
          <div key={remote.name} className="flex items-center justify-between hover:bg-secondary/50 px-1 rounded">
            <div className="min-w-0 flex-1">
              <span className="font-mono text-[10px]">{remote.name}</span>
              <div className="text-[9px] text-muted-foreground truncate">{remote.fetch || remote.push || "—"}</div>
            </div>
            <div className="flex items-center gap-0.5">
              <button
                onClick={() => {
                  setEditing(remote.name);
                  setEditUrl(remote.fetch || remote.push || "");
                }}
                className="p-0.5 hover:bg-secondary rounded text-muted-foreground"
              >
                <RotateCcw size={10} />
              </button>
              <button onClick={() => setDeleteName(remote.name)} className="p-0.5 hover:bg-destructive/10 rounded text-destructive">
                <Trash2 size={10} />
              </button>
            </div>
          </div>
        ))}
      </div>
      <div className="flex gap-1">
        <Input value={name} onChange={(e) => setName(e.target.value)} placeholder={t("gitPanel.advanced.remoteName")} className="h-7 text-[10px] flex-1" />
        <Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder={t("gitPanel.advanced.remoteUrl")} className="h-7 text-[10px] flex-1" />
      </div>
      <Button size="sm" disabled={!name.trim() || !url.trim()} onClick={add} className="h-6 text-[10px]">
        {t("gitPanel.advanced.remoteAdd")}
      </Button>
      {editing && (
        <div className="flex gap-1 pt-1 border-t">
          <Input value={editUrl} onChange={(e) => setEditUrl(e.target.value)} placeholder={t("gitPanel.advanced.remoteUrl")} className="h-7 text-[10px] flex-1" />
          <Button size="sm" onClick={update} className="h-6 text-[10px]">{t("gitPanel.advanced.remoteUpdate")}</Button>
        </div>
      )}
      <GitOperationDialog
        open={!!deleteName}
        onCancel={() => setDeleteName(null)}
        title={t("gitPanel.advanced.remoteTitle")}
        message={t("gitPanel.advanced.remoteDeleteConfirm", { name: deleteName || "" })}
        onConfirm={remove}
      />
    </div>
  );
}
