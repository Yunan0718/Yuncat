import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useApi } from "../../hooks/useApi";
import { useToast } from "../../hooks/use-toast";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Label } from "../ui/label";
import { Switch } from "../ui/switch";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../ui/select";
import { Badge } from "../ui/badge";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog";
import { ToolPolicyPanel } from "../ToolPolicyPanel";
import { PendingRolesSection } from "./PendingRolesSection";
import { PRESET_IDS, presetDescKey, presetLabelKey } from "../../lib/securityPreset";
import { useSecurityControl } from "../../hooks/useSecurityControl";

/* ── Security ───────────────────────────────────── */
export function SecuritySection({ api }: { api: ReturnType<typeof useApi>["api"] }) {
  const { t } = useTranslation();
  const { toast } = useToast();
  const [_saving, setSaving] = useState("");
  // 权限档位与对话区共用同一个来源（/api/v1/security/preset）
  const { preset, change: changePreset } = useSecurityControl(api, () => toast(t("chat.approvalSaveFailed"), "error"));
  const [watchFiles, setWatchFiles] = useState(false);
  const [rules, setRules] = useState<
    Array<{ id: string; description: string; trigger_on: string[]; action: string; enabled: boolean }>
  >([]);
  const [editingRule, setEditingRule] = useState<{
    description: string;
    trigger_on: string[];
    action: string;
    enabled: boolean;
  } | null>(null);

  useEffect(() => {
    api<Record<string, string>>("/api/v1/settings")
      .then((s) => {
        if (s.watch_files_enabled === "true") setWatchFiles(true);
      })
      .catch((e) => console.warn("SettingsPage: settings load failed:", e));
  }, [api]);
  useEffect(() => {
    api<typeof rules>("/api/v1/rules")
      .then(setRules)
      .catch((e) => console.warn("SettingsPage: rules load failed:", e));
  }, [api]);

  const saveSetting = async (k: string, v: string) => {
    setSaving(k);
    try {
      await api("/api/v1/settings", { method: "POST", body: JSON.stringify({ key: k, value: v }) });
    } catch {
      toast(t("common.saveFailed"), "error");
    }
    setSaving("");
  };
  const saveRule = async () => {
    if (!editingRule) return;
    try {
      await api("/api/v1/rules", { method: "POST", body: JSON.stringify(editingRule) });
    } catch {
      toast(t("settings.saveFailed"), "error");
    }
    setEditingRule(null);
    api<typeof rules>("/api/v1/rules")
      .then(setRules)
      .catch(() => toast(t("common.ruleRefreshFailed"), "error"));
  };
  // Task 6.9: 包 try/catch——API 失败时 toast 反馈（悲观更新：API 成功才 setRules，失败无需回滚）
  const toggleRule = async (rid: string, en: boolean) => {
    try {
      await api(`/api/v1/rules/${rid}`, { method: "PATCH", body: JSON.stringify({ enabled: en }) });
      setRules((p) => p.map((r) => (r.id === rid ? { ...r, enabled: en } : r)));
    } catch {
      toast(t("settings.ruleToggleFailed"), "error");
    }
  };
  const deleteRule = async (rid: string) => {
    try {
      await api(`/api/v1/rules/${rid}`, { method: "DELETE" });
      setRules((p) => p.filter((r) => r.id !== rid));
    } catch {
      toast(t("settings.ruleDeleteFailed"), "error");
    }
  };

  const actionLabels: Record<string, string> = {
    require_confirmation: t("tool.requireConfirmation"),
    auto_backup: t("tool.autoBackup"),
    block: t("tool.blockExec"),
    inject_context: t("tool.injectContext"),
  };

  return (
    <div className="space-y-8">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("settings.securityLevel")}</CardTitle>
          <CardDescription>{t("settings.securityProfileDesc")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          {PRESET_IDS.map((id) => (
            <label
              key={id}
              className={`flex items-start gap-3 border rounded-lg p-3 cursor-pointer transition-colors ${preset === id ? "border-primary bg-primary/5" : "hover:bg-secondary"}`}
            >
              <input
                type="radio"
                name="security-preset"
                className="mt-0.5"
                checked={preset === id}
                onChange={() => changePreset(id)}
              />
              <div>
                <div className="text-sm font-medium">{t(presetLabelKey(id))}</div>
                <div className="text-xs text-muted-foreground mt-0.5">{t(presetDescKey(id))}</div>
              </div>
            </label>
          ))}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("settings.toolApproval")}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <Label className="text-sm font-medium">{t("settings.watchFiles")}</Label>
              <p className="text-xs text-muted-foreground mt-0.5">{t("settings.watchFilesDesc")}</p>
            </div>
            <Switch
              checked={watchFiles}
              onCheckedChange={(v) => {
                setWatchFiles(v);
                saveSetting("watch_files_enabled", v ? "true" : "false");
              }}
            />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle className="text-base">{t("settings.customRules")}</CardTitle>
            <CardDescription>{t("settings.customRulesDesc")}</CardDescription>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() =>
              setEditingRule({ description: "", trigger_on: [], action: "require_confirmation", enabled: true })
            }
          >
            {t("settings.newRule")}
          </Button>
        </CardHeader>
        <CardContent className="space-y-2">
          {rules.map((r) => (
            <div key={r.id} className="flex items-center justify-between border rounded-lg p-3">
              <div className="flex-1 min-w-0">
                <p className="text-sm">{r.description}</p>
                <div className="flex gap-1 mt-1.5 flex-wrap">
                  {r.trigger_on.map((trigger) => (
                    <Badge key={trigger} variant="secondary" className="text-[10px] font-mono">
                      {trigger}
                    </Badge>
                  ))}
                  <span className="text-[10px] text-muted-foreground ml-1">{actionLabels[r.action] || r.action}</span>
                </div>
              </div>
              <div className="flex gap-1 shrink-0 ml-3">
                <Button
                  variant={r.enabled ? "default" : "ghost"}
                  size="sm"
                  className="h-7 text-[11px]"
                  onClick={() => toggleRule(r.id, !r.enabled)}
                >
                  {r.enabled ? t("common.enabled") : t("common.disabled")}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 text-[11px] text-destructive"
                  onClick={() => deleteRule(r.id)}
                >
                  {t("common.delete")}
                </Button>
              </div>
            </div>
          ))}
        </CardContent>
      </Card>

      <PendingRolesSection api={api} t={t} />

      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("toolPolicy.title")}</CardTitle>
          <CardDescription>{t("toolPolicy.subtitle")}</CardDescription>
        </CardHeader>
        <CardContent>
          <ToolPolicyPanel />
        </CardContent>
      </Card>

      <Dialog open={!!editingRule} onOpenChange={() => setEditingRule(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("settings.newRule")}</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1.5">
              <Label className="text-xs">{t("settings.ruleDesc")}</Label>
              <Input
                value={editingRule?.description || ""}
                onChange={(e) => setEditingRule((r) => (r ? { ...r, description: e.target.value } : r))}
              />
            </div>
            <div className="space-y-1.5">
              <Label className="text-xs">{t("settings.ruleTriggers")}</Label>
              <Input
                value={editingRule?.trigger_on.join(", ") || ""}
                onChange={(e) =>
                  setEditingRule((r) =>
                    r
                      ? {
                          ...r,
                          trigger_on: e.target.value
                            .split(",")
                            .map((s) => s.trim())
                            .filter(Boolean),
                        }
                      : r,
                  )
                }
                placeholder="tool:write_file"
              />
            </div>
            <div className="space-y-1.5">
              <Label className="text-xs">{t("settings.ruleAction")}</Label>
              <Select
                value={editingRule?.action ?? ""}
                onValueChange={(v) => {
                  if (!v || !editingRule) return;
                  setEditingRule({ ...editingRule, action: v });
                }}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="require_confirmation">{t("tool.requireConfirmation")}</SelectItem>
                  <SelectItem value="auto_backup">{t("tool.autoBackup")}</SelectItem>
                  <SelectItem value="block">{t("tool.blockExec")}</SelectItem>
                  <SelectItem value="inject_context">{t("tool.injectContext")}</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditingRule(null)}>
              {t("common.cancel")}
            </Button>
            <Button onClick={saveRule}>{t("common.save")}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
