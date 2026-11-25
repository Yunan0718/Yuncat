import { useTranslation } from "react-i18next";
import { MessageSquare, Sparkles, Search, GitPullRequest } from "lucide-react";

export interface GitCommitFormProps {
  commitMsg: string;
  setCommitMsg: (msg: string) => void;
  diffFile: string;
  loading: boolean;
  showCreatePR: boolean;
  onLoadDiff: (staged: boolean) => void;
  onLoadBlame: (filePath: string) => void;
  onSuggestCommitMessage: () => void;
  onRunAiReview: () => void;
  onCommit: () => void;
  onPush: () => void;
  onOpenPR: () => void;
}

export function GitCommitForm({
  commitMsg,
  setCommitMsg,
  diffFile,
  loading,
  showCreatePR,
  onLoadDiff,
  onLoadBlame,
  onSuggestCommitMessage,
  onRunAiReview,
  onCommit,
  onPush,
  onOpenPR,
}: GitCommitFormProps) {
  const { t } = useTranslation();
  return (
    <div className="h-7 flex items-center px-2 gap-1 border-b shrink-0">
      <button onClick={() => onLoadDiff(false)} className="text-[10px] px-1.5 py-0.5 border rounded hover:bg-secondary">
        {t("gitPanel.diff.unstaged")}
      </button>
      <button onClick={() => onLoadDiff(true)} className="text-[10px] px-1.5 py-0.5 border rounded hover:bg-secondary">
        {t("gitPanel.diff.staged")}
      </button>
      {diffFile && diffFile !== "all changes" && (
        <button
          onClick={() => onLoadBlame(diffFile)}
          disabled={loading}
          className="text-[10px] px-1.5 py-0.5 border rounded hover:bg-secondary flex items-center gap-1"
          title={t("gitPanel.blame.title")}
        >
          <Search size={10} />
          {t("gitPanel.blame.button")}
        </button>
      )}
      <div className="flex-1" />
      <input
        value={commitMsg}
        onChange={(e) => setCommitMsg(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && onCommit()}
        placeholder={t("gitPanel.commit.placeholder")}
        className="w-40 px-1.5 py-0.5 text-[10px] border rounded bg-background"
      />
      <button
        onClick={onSuggestCommitMessage}
        disabled={loading}
        className="text-[10px] px-2 py-0.5 border rounded hover:bg-secondary flex items-center gap-1"
        title={t("gitPanel.suggest.title")}
        aria-label={t("gitPanel.suggest.title")}
      >
        <MessageSquare size={10} />
      </button>
      <button
        onClick={onRunAiReview}
        disabled={loading}
        className="text-[10px] px-2 py-0.5 border rounded hover:bg-secondary flex items-center gap-1 text-primary"
        title={t("gitPanel.review.title")}
        aria-label={t("gitPanel.review.title")}
      >
        <Sparkles size={10} />
      </button>
      <button
        onClick={onCommit}
        disabled={!commitMsg.trim() || loading}
        className="text-[10px] px-2 py-0.5 bg-primary text-primary-foreground rounded font-semibold disabled:opacity-50"
      >
        {t("legacy.wsPage_939d")}
      </button>
      <button
        onClick={onPush}
        disabled={loading}
        className="text-[10px] px-2 py-0.5 border rounded hover:bg-secondary"
      >
        {t("gitPanel.pushButton")}
      </button>
      {showCreatePR && (
        <button
          onClick={onOpenPR}
          disabled={loading}
          className="text-[10px] px-2 py-0.5 border rounded hover:bg-secondary flex items-center gap-1 text-primary"
          title={t("gitPanel.createPR")}
        >
          <GitPullRequest size={10} />
          {t("gitPanel.createPR")}
        </button>
      )}
    </div>
  );
}
