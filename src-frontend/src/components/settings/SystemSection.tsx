import { useTranslation } from "react-i18next";
import { useAppStore } from "../../store/appStore";
import { useToast } from "../../hooks/use-toast";
import { useConfirmDialog } from "../../hooks/useConfirmDialog";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Button } from "../ui/button";
import { Download, Upload } from "lucide-react";
import { DependenciesPanel } from "../DependenciesPanel";
import { UpdatePanel } from "./UpdatePanel";
import { AutostartCard } from "./AutostartCard";
import { TrayBehaviorCard } from "./TrayBehaviorCard";
import { BackupPanel } from "./BackupPanel";
import { LogViewer } from "./LogViewer";
import { LogLevelControl } from "./LogLevelControl";
import { PendingTriggersCard } from "./PendingTriggersCard";
import { EventLogCard } from "./EventLogCard";
import { ImportVscodeSection } from "./ImportVscodeSection";
import { DataClearSection } from "./DataClearSection";

/* ── System ─────────────────────────────────────── */
export function SystemSection() {
  const { t } = useTranslation();
  const port = useAppStore((s) => s.port);
  const token = useAppStore((s) => s.token);
  const { toast } = useToast();
  const { confirm, confirmDialog } = useConfirmDialog();
  const fc = (path: string, opts?: RequestInit) =>
    fetch(`http://127.0.0.1:${port}${path}`, {
      ...opts,
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json", ...opts?.headers },
    });
  return (
    <div className="space-y-8">
      <UpdatePanel />
      <TrayBehaviorCard />
      <AutostartCard />
      <BackupPanel />
      <LogLevelControl />
      <LogViewer />
      <PendingTriggersCard />
      <EventLogCard />
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("settings.dependencies")}</CardTitle>
        </CardHeader>
        <CardContent>
          <DependenciesPanel />
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("settings.devEntry")}</CardTitle>
          <CardDescription>{t("settings.devEntryDesc")}</CardDescription>
        </CardHeader>
        <CardContent className="flex gap-2 flex-wrap">
          {[
            { to: "#/skills", label: t("nav.skills") },
            { to: "#/dashboard", label: t("nav.dashboard") },
            { to: "#/prompts", label: t("settings.promptEditorTitle") },
            { to: "#/mcp", label: t("settings.mcpTitle") },
          ].map(({ to, label }) => (
            <a
              key={to}
              href={to}
              className="inline-flex items-center justify-center rounded-md border text-xs font-medium h-8 px-3 hover:bg-secondary transition-colors"
            >
              {label}
            </a>
          ))}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("settings.migration")}</CardTitle>
          <CardDescription>{t("settings.migrationDesc")}</CardDescription>
        </CardHeader>
        <CardContent className="flex gap-2 flex-wrap items-center">
          <Button
            size="sm"
            className="gap-1"
            onClick={async () => {
              try {
                const r = await fc("/api/v1/config/export");
                const d = await r.json();
                const blob = new Blob([JSON.stringify(d, null, 2)], { type: "application/json" });
                const url = URL.createObjectURL(blob);
                const a = document.createElement("a");
                a.href = url;
                a.download = `yuncat-full-${new Date().toISOString().slice(0, 10)}.json`;
                a.click();
                URL.revokeObjectURL(url);
                toast(
                  t("settings.exportSuccess", {
                    models: d.models?.length || 0,
                    rules: d.rules?.length || 0,
                    workflows: d.workflows?.length || 0,
                  }),
                  "success",
                );
              } catch {
                toast(t("settings.exportFailed"), "error");
              }
            }}
          >
            <Download size={14} />
            {t("settings.exportAll")}
          </Button>
          <label className="cursor-pointer inline-flex items-center justify-center rounded-md border text-xs font-medium h-8 px-3 hover:bg-secondary transition-colors gap-1">
            <input
              type="file"
              accept=".json"
              className="hidden"
              onChange={async (e) => {
                const f = e.target.files?.[0];
                if (!f) return;
                try {
                  const text = await f.text();
                  const d = JSON.parse(text);
                  if (!d.version || !d.models) {
                    toast(t("settings.invalidExport"), "error");
                    return;
                  }
                  if (
                    !(await confirm({
                      title: t("common.confirm"),
                      description: t("settings.importConfirm", {
                        models: d.models?.length || 0,
                        rules: d.rules?.length || 0,
                        workflows: d.workflows?.length || 0,
                      }),
                      variant: "destructive",
                    }))
                  )
                    return;
                  const r = await fc("/api/v1/config/import", { method: "POST", body: JSON.stringify(d) });
                  const rd = await r.json();
                  if (rd.ok)
                    toast(
                      t("settings.importSuccess", { summary: Object.values(rd.imported || {}).join("+") }),
                      "success",
                    );
                  else toast(t("settings.importFailed", { error: rd.error }), "error");
                } catch {
                  toast(t("settings.invalidFile"), "error");
                }
              }}
            />
            <Upload size={14} />
            {t("settings.importConfig")}
          </label>
        </CardContent>
      </Card>

      <ImportVscodeSection fc={fc} />

      <DataClearSection fc={fc} />
      {confirmDialog}
    </div>
  );
}
