import { useEffect, useState } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { useApi } from "../../hooks/useApi";
import { useToast } from "../../hooks/use-toast";

export function PendingRolesSection({
  api,
  t,
}: {
  api: ReturnType<typeof useApi>["api"];
  t: (key: string, options?: Record<string, unknown>) => string;
}) {
  const { toast } = useToast();
  const [pending, setPending] = useState<Array<{ node_id: string; role_name: string; score: number }>>([]);
  useEffect(() => {
    api<typeof pending>("/api/v1/roles/pending")
      .then(setPending)
      .catch((e) => console.warn("SettingsPage: pending roles load failed:", e));
  }, [api]);
  if (pending.length === 0) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t("settings.adaptiveRoles")}</CardTitle>
        <CardDescription>{t("settings.adaptiveRolesDesc")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        {pending.map((r) => (
          <div key={r.node_id} className="flex items-center justify-between border rounded-lg p-3">
            <div>
              <span className="text-sm font-medium">{r.role_name}</span>
              <Badge variant="secondary" className="ml-2 text-[10px]">
                {t("settings.scoreLabel")}: {r.score.toFixed(1)}
              </Badge>
            </div>
            <div className="flex gap-2">
              <Button
                size="sm"
                className="h-7 text-[11px] bg-success-solid text-success-foreground hover:opacity-90"
                onClick={async () => {
                  // Task 6.9: 静默 catch 改为 toast 反馈（API 失败时列表不变，无需回滚）
                  try {
                    await api("/api/v1/roles/promote", {
                      method: "POST",
                      body: JSON.stringify({ node_id: r.node_id, role_name: r.role_name }),
                    });
                    setPending((p) => p.filter((x) => x.node_id !== r.node_id));
                  } catch {
                    toast(t("settings.rolePromoteFailed"), "error");
                  }
                }}
              >
                {t("settings.promote")}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                className="h-7 text-[11px]"
                onClick={async () => {
                  // Task 6.9: 静默 catch 改为 toast 反馈（API 失败时列表不变，无需回滚）
                  try {
                    await api("/api/v1/roles/reject", {
                      method: "POST",
                      body: JSON.stringify({ node_id: r.node_id }),
                    });
                    setPending((p) => p.filter((x) => x.node_id !== r.node_id));
                  } catch {
                    toast(t("settings.roleRejectFailed"), "error");
                  }
                }}
              >
                {t("settings.dismiss")}
              </Button>
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
