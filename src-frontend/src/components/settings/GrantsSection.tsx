import { useEffect, useState } from "react";
import { useApi } from "../../hooks/useApi";
import { useTranslation } from "react-i18next";
import { Trash2, ShieldCheck, Clock } from "lucide-react";
import { LoadingCurve } from "../common/LoadingCurve";
import { EmptyState } from "../common/EmptyState";

interface GrantRow {
  id: string;
  tool_name: string;
  pattern: string;
  rule_type: string;
  agent_id: string;
  project_id: string;
  granted_at: number;
  expires_at: number;
  source: string;
  grant_reason: string;
  is_active: boolean;
}

function formatTime(ts: number): string {
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch {
    return "";
  }
}

/** 授权范围标签。**必须传入 t**——纯函数拿不到 i18n，写死中文会让英文界面中英混排。 */
function scopeLabel(g: GrantRow, t: (k: string) => string): string {
  const parts: string[] = [];
  parts.push(g.agent_id ? `agent=${g.agent_id}` : t("grants.allAgents"));
  parts.push(g.project_id ? `project=${g.project_id}` : t("grants.allProjects"));
  return parts.join(" · ");
}

export function GrantsSection() {
  const { t } = useTranslation();
  const { api } = useApi();
  const [grants, setGrants] = useState<GrantRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<string | null>(null);

  const load = async () => {
    setError(null);
    try {
      const rows = await api<GrantRow[]>("/api/v1/approvals/grants");
      setGrants(rows);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setGrants([]);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleRevoke = async (id: string) => {
    setRevoking(id);
    try {
      await api(`/api/v1/approvals/grants/${id}/revoke`, { method: "POST" });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRevoking(null);
    }
  };

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-xl font-semibold tracking-tight">{t("settings.grants")}</h2>
        <p className="text-sm text-muted-foreground mt-1">{t("settings.grantsDesc")}</p>
      </div>

      {grants === null ? (
        <div className="flex items-center gap-2 py-4 text-xs text-muted-foreground">
          <LoadingCurve variant="lissajous" size={14} particleCount={6} />
          <span>{t("common.loading")}</span>
        </div>
      ) : error ? (
        <p className="text-xs text-destructive text-center py-4">{error}</p>
      ) : grants.length === 0 ? (
        <EmptyState compact title={t("grants.empty")} />
      ) : (
        <div className="flex flex-col gap-2">
          {grants.map((g) => (
            <div key={g.id} className="rounded-lg bg-secondary/40 hover:bg-secondary transition-colors p-3 text-xs">
              <div className="flex items-start justify-between gap-2">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-1.5 font-medium truncate">
                    <ShieldCheck size={13} className="text-success shrink-0" />
                    <span className="truncate">
                      {g.tool_name}
                      {g.pattern ? ` (${g.pattern})` : ""}
                    </span>
                  </div>
                  <div className="text-muted-foreground mt-1">{scopeLabel(g, t)}</div>
                  <div className="text-2xs text-muted-foreground mt-1 flex items-center gap-1">
                    <Clock size={11} className="shrink-0" />
                    {t("grants.expiresAt")}: {formatTime(g.expires_at)}
                  </div>
                  {g.grant_reason && (
                    <div className="text-2xs text-muted-foreground mt-0.5">
                      {t("grants.reason")}: {g.grant_reason}
                    </div>
                  )}
                </div>
                <button
                  type="button"
                  onClick={() => handleRevoke(g.id)}
                  disabled={revoking === g.id}
                  className="shrink-0 inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-2xs text-destructive hover:bg-destructive/10 disabled:opacity-50"
                >
                  <Trash2 size={11} />
                  {t("grants.revoke")}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
