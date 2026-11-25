import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useApi } from "../../hooks/useApi";
import { useToast } from "../../hooks/use-toast";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Label } from "../ui/label";
import { Input } from "../ui/input";
import { Button } from "../ui/button";

/* ── Workspace ──────────────────────────────────── */
export function WorkspaceSection({ api }: { api: ReturnType<typeof useApi>["api"] }) {
  const { t } = useTranslation();
  const { toast } = useToast();
  const [workspace, setWorkspace] = useState("");
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    api<Record<string, string>>("/api/v1/settings")
      .then((s) => {
        if (s.workspace) setWorkspace(s.workspace);
      })
      .catch((e) => console.warn("SettingsPage: workspace load failed:", e));
  }, [api]);
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t("settings.workspace")}</CardTitle>
        <CardDescription>{t("settings.workspaceDesc")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="space-y-1.5">
          <Label>{t("settings.pathLabel")}</Label>
          <div className="flex gap-2">
            <Input
              value={workspace}
              onChange={(e) => {
                setWorkspace(e.target.value);
                setSaved(false);
              }}
              placeholder={t("settings.workspaceDefault")}
              className="font-mono text-sm flex-1"
            />
            {/* Task 7.22: 目录浏览器——Electron dialog.showOpenDialog，对标 VS Code "Open Folder" */}
            <Button
              variant="outline"
              size="sm"
              className="shrink-0"
              onClick={async () => {
                try {
                  const res = await window.yuncat?.openFileDialog?.({
                    selectFolder: true,
                    title: t("settings.selectWorkspaceDir"),
                  });
                  if (res?.ok && !res.canceled && res.path) {
                    const picked = Array.isArray(res.path) ? res.path[0] : res.path;
                    setWorkspace(picked);
                    setSaved(false);
                  }
                } catch {
                  // Electron 不可用时静默忽略——对标 OnboardingWizard 处理方式
                }
              }}
            >
              {t("settings.browse")}
            </Button>
          </div>
        </div>
        <Button
          onClick={async () => {
            // Task 6.9: 包 try/catch——API 失败时 toast 反馈（saved 保持 false，按钮文案不变）
            try {
              await api("/api/v1/settings", {
                method: "POST",
                body: JSON.stringify({ key: "workspace", value: workspace }),
              });
              setSaved(true);
            } catch {
              toast(t("settings.saveFailed"), "error");
            }
          }}
          disabled={saved || !workspace}
        >
          {saved ? t("common.saved") : t("settings.savePath")}
        </Button>
      </CardContent>
    </Card>
  );
}
