import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useToast } from "../../hooks/use-toast";
import { useConfirmDialog } from "../../hooks/useConfirmDialog";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Button } from "../ui/button";
import { Trash2 } from "lucide-react";

export function DataClearSection({ fc }: { fc: (path: string, opts?: RequestInit) => Promise<Response> }) {
  const { t } = useTranslation();
  const { toast } = useToast();
  const { confirm, confirmDialog } = useConfirmDialog();
  const [scopes, setScopes] = useState<string[]>([]);
  const options = [
    { key: "conversations", label: t("settings.clearConversations") },
    { key: "tasks", label: t("settings.clearTasks") },
    { key: "analytics", label: t("settings.clearAnalytics") },
    { key: "memories", label: t("settings.clearMemories") },
    { key: "file_cache", label: t("settings.clearFileCache") },
  ];
  const toggle = (key: string) => {
    setScopes((prev) => (prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]));
  };
  const runClear = async () => {
    if (!scopes.length) return;
    const ok = await confirm({
      title: t("common.confirm"),
      description: t("settings.clearConfirm", { scopes: scopes.map((s) => t(`settings.clear_${s}`)).join(", ") }),
      variant: "destructive",
    });
    if (!ok) return;
    try {
      const r = await fc("/api/v1/data/clear", { method: "POST", body: JSON.stringify({ scopes }) });
      const rd = await r.json();
      if (rd.ok) {
        toast(
          t("settings.clearSuccess", {
            summary: Object.entries(rd.cleared || {})
              .map(([k, v]) => `${k}:${v}`)
              .join(", "),
          }),
          "success",
        );
        setScopes([]);
      } else {
        toast(t("settings.clearFailed", { error: rd.error }), "error");
      }
    } catch {
      toast(t("settings.clearFailed", { error: "network" }), "error");
    }
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2 text-destructive">
          <Trash2 size={16} />
          {t("settings.dataClearTitle")}
        </CardTitle>
        <CardDescription>{t("settings.dataClearDesc")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap gap-2">
          {options.map((o) => (
            <label
              key={o.key}
              className="flex items-center gap-1.5 border rounded-md px-2.5 py-1.5 text-xs cursor-pointer hover:bg-secondary"
            >
              <input
                type="checkbox"
                checked={scopes.includes(o.key)}
                onChange={() => toggle(o.key)}
                className="accent-primary"
              />
              {o.label}
            </label>
          ))}
        </div>
        <Button variant="destructive" size="sm" disabled={!scopes.length} onClick={runClear} className="gap-1">
          <Trash2 size={13} />
          {t("settings.clearButton")}
        </Button>
      </CardContent>
      {confirmDialog}
    </Card>
  );
}
