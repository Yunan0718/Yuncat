import { useTranslation } from "react-i18next";
import { type GitBranch } from "./gitTypes";

export interface GitBranchSelectorProps {
  branches: GitBranch[];
  newBranch: string;
  setNewBranch: (name: string) => void;
  loading: boolean;
  onCreateBranch: () => void;
  onCheckout: (branch: string) => void;
  onPull: () => void;
  onStash: () => void;
  onStashPop: () => void;
}

export function GitBranchSelector({
  branches,
  newBranch,
  setNewBranch,
  loading,
  onCreateBranch,
  onCheckout,
  onPull,
  onStash,
  onStashPop,
}: GitBranchSelectorProps) {
  const { t } = useTranslation();
  return (
    <div>
      <div className="flex items-center gap-1 px-2 py-1 border-b">
        <input
          value={newBranch}
          onChange={(e) => setNewBranch(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && onCreateBranch()}
          placeholder={t("gitPanel.newBranchPlaceholder")}
          className="flex-1 px-1.5 py-0.5 text-[10px] border rounded bg-background"
        />
        <button
          onClick={onCreateBranch}
          disabled={!newBranch.trim() || loading}
          className="text-[10px] px-2 py-0.5 bg-primary text-white rounded font-semibold disabled:opacity-50"
        >
          {t("gitPanel.create")}
        </button>
      </div>
      <div className="flex gap-1 px-2 py-1 border-b">
        <button
          onClick={onPull}
          disabled={loading}
          className="text-[10px] px-2 py-0.5 border rounded hover:bg-secondary"
        >
          {t("gitPanel.pullButton")}
        </button>
        <button
          onClick={onStash}
          disabled={loading}
          className="text-[10px] px-2 py-0.5 border rounded hover:bg-secondary"
        >
          {t("gitPanel.stashButton")}
        </button>
        <button
          onClick={onStashPop}
          disabled={loading}
          className="text-[10px] px-2 py-0.5 border rounded hover:bg-secondary"
        >
          {t("gitPanel.stashPopButton")}
        </button>
      </div>
      {branches.length === 0 ? (
        <p className="text-muted-foreground text-center py-4">{t("gitPanel.loadingBranches")}</p>
      ) : (
        branches.map((b) => (
          <div
            key={b.name}
            className={`px-2 py-1 border-b border-border flex items-center gap-2 ${
              b.current ? "bg-primary-muted" : "hover:bg-secondary"
            }`}
          >
            <span className="text-xs flex-1 font-mono">
              {b.current ? "●" : "○"} {b.name}
            </span>
            {!b.current && (
              <button
                onClick={() => onCheckout(b.name)}
                disabled={loading}
                className="text-[10px] px-2 py-0.5 border rounded hover:bg-secondary"
              >
                {t("gitPanel.checkoutButton")}
              </button>
            )}
            {b.current && (
              <span className="text-[10px] text-success font-semibold">{t("gitPanel.currentBranch")}</span>
            )}
          </div>
        ))
      )}
    </div>
  );
}
