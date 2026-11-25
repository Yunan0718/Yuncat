import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { downloadResponseBlob } from "../../lib/download";
import { useToast } from "../../hooks/use-toast";
import { useAppStore } from "../../store/appStore";
import { useBackupSchedule, type BackupHistoryItem, type BackupScheduleSettings } from "../../hooks/useBackupSchedule";
import { Button } from "../ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Input } from "../ui/input";
import { Label } from "../ui/label";
import { Switch } from "../ui/switch";
import { Check } from "lucide-react";

/**
 * Phase 96 + Batch 5.6: 配置备份/恢复面板。
 *
 * Phase 96（手动全量）:
 * - 导出：输入密码 → POST /export → 二进制 .ybk 下载
 * - 导入：选文件 → 输入密码 → POST /import → staging → 重启
 *
 * Batch 5.6（增量 + 调度 + 历史）:
 * - 调度设置：启用开关、cron 表达式、保留数量、备份密码
 * - 备份历史：列表展示 + 删除 + 立即触发
 */
export function BackupPanel() {
  const { t } = useTranslation();
  const { toast } = useToast();
  const port = useAppStore((s) => s.port);
  const token = useAppStore((s) => s.token);
  const backendReady = useAppStore((s) => s.backendReady);
  const { getSettings, updateSettings, listBackups, triggerBackup, deleteBackup } = useBackupSchedule();

  // 手动导出/导入状态
  const [exportPw, setExportPw] = useState("");
  const [exporting, setExporting] = useState(false);
  const [importPath, setImportPath] = useState("");
  const [importPw, setImportPw] = useState("");
  const [importing, setImporting] = useState(false);
  const [restartPending, setRestartPending] = useState(false);

  // 调度设置状态
  const [schedule, setSchedule] = useState<BackupScheduleSettings | null>(null);
  const [schedulePw, setSchedulePw] = useState("");
  const [savingSettings, setSavingSettings] = useState(false);
  const [triggering, setTriggering] = useState(false);
  const [backups, setBackups] = useState<BackupHistoryItem[]>([]);
  const [loadingBackups, setLoadingBackups] = useState(false);

  const fc = (path: string, opts?: RequestInit) =>
    fetch(`http://127.0.0.1:${port}${path}`, {
      ...opts,
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json", ...opts?.headers },
    });

  // 加载调度设置 + 备份历史
  const loadSchedule = useCallback(async () => {
    if (!backendReady) return;
    try {
      const s = await getSettings();
      setSchedule(s);
    } catch {
      // 静默失败——面板可能尚未就绪
    }
  }, [backendReady, getSettings]);

  const loadBackups = useCallback(async () => {
    if (!backendReady) return;
    setLoadingBackups(true);
    try {
      const data = await listBackups(50);
      setBackups(data.items || []);
    } catch {
      // 静默
    } finally {
      setLoadingBackups(false);
    }
  }, [backendReady, listBackups]);

  useEffect(() => {
    loadSchedule();
    loadBackups();
  }, [loadSchedule, loadBackups]);

  // ── 手动导出/导入 ──

  const handleExport = async () => {
    if (exportPw.length < 4) {
      toast(t("backup.passwordTooShort"), "error");
      return;
    }
    setExporting(true);
    try {
      const r = await fc("/api/v1/config/backup/export", {
        method: "POST",
        body: JSON.stringify({ password: exportPw }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        toast(t("backup.exportFailed") + (err.error ? `: ${err.error}` : ""), "error");
        return;
      }
      await downloadResponseBlob(r, `yuncat-backup-${Date.now()}.ybk`);
      toast(t("backup.exportSuccess"), "success");
      setExportPw("");
    } catch {
      toast(t("backup.exportFailed"), "error");
    } finally {
      setExporting(false);
    }
  };

  const handlePickFile = async () => {
    const res = await window.yuncat?.openFileDialog?.({
      title: t("backup.selectBackupFile"),
    });
    if (res?.ok && typeof res.path === "string") {
      setImportPath(res.path);
    }
  };

  const handleImport = async () => {
    if (!importPath) {
      toast(t("backup.noFileSelected"), "error");
      return;
    }
    if (importPw.length < 4) {
      toast(t("backup.passwordTooShort"), "error");
      return;
    }
    setImporting(true);
    try {
      const r = await fc("/api/v1/config/backup/import", {
        method: "POST",
        body: JSON.stringify({ path: importPath, password: importPw }),
      });
      const data = await r.json().catch(() => ({}));
      if (data.ok) {
        setRestartPending(true);
        setImportPw("");
        setImportPath("");
      } else {
        toast(t("backup.importFailed") + (data.error ? `: ${data.error}` : ""), "error");
      }
    } catch {
      toast(t("backup.importFailed"), "error");
    } finally {
      setImporting(false);
    }
  };

  const handleRestart = async () => {
    await window.yuncat?.relaunchApp?.();
  };

  // ── 调度设置 ──

  const handleSaveSettings = async () => {
    if (!schedule) return;
    setSavingSettings(true);
    try {
      const update: Record<string, unknown> = {
        enabled: schedule.enabled,
        cron_expr: schedule.cron_expr,
        retention_count: schedule.retention_count,
      };
      if (schedulePw) {
        update.password = schedulePw;
      }
      await updateSettings(update);
      setSchedulePw("");
      toast(t("backup.schedule.saved"), "success");
      await loadSchedule();
    } catch (e) {
      toast(t("backup.schedule.saveFailed") + (e instanceof Error ? `: ${e.message}` : ""), "error");
    } finally {
      setSavingSettings(false);
    }
  };

  const handleTrigger = async () => {
    setTriggering(true);
    try {
      const result = await triggerBackup();
      if (result.created) {
        toast(t("backup.schedule.triggerSuccess"), "success");
        await loadBackups();
      } else if (result.skipped) {
        toast(t("backup.schedule.triggerSkipped"), "info");
      }
    } catch (e) {
      toast(t("backup.schedule.triggerFailed") + (e instanceof Error ? `: ${e.message}` : ""), "error");
    } finally {
      setTriggering(false);
    }
  };

  /** 下载一条历史备份——此前列表里只有"删除"，备份存下来却取不出去。 */
  const handleDownload = async (backupId: string) => {
    try {
      const r = await fc(`/api/v1/config/backup/${backupId}/download`);
      if (!r.ok) {
        toast(t("backup.schedule.downloadFailed"), "error");
        return;
      }
      await downloadResponseBlob(r, `${backupId}.ybk`);
      toast(t("backup.schedule.downloaded"), "success");
    } catch {
      toast(t("backup.schedule.downloadFailed"), "error");
    }
  };

  const handleDelete = async (backupId: string) => {
    try {
      await deleteBackup(backupId);
      toast(t("backup.schedule.deleted"), "success");
      await loadBackups();
    } catch (e) {
      toast(t("backup.schedule.deleteFailed") + (e instanceof Error ? `: ${e.message}` : ""), "error");
    }
  };

  const formatTime = (ts: number) => {
    return new Date(ts * 1000).toLocaleString();
  };

  const formatSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t("backup.title")}</CardTitle>
        <CardDescription>{t("backup.desc")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        {/* ── 调度设置 (Batch 5.6) ── */}
        {schedule && (
          <div className="space-y-3 rounded-md border border-primary/20 bg-primary/5 p-3">
            <div className="flex items-center justify-between">
              <Label className="text-sm font-medium">{t("backup.schedule.title")}</Label>
              <Switch
                checked={schedule.enabled}
                onCheckedChange={(v) => setSchedule({ ...schedule, enabled: v })}
              />
            </div>
            <p className="text-xs text-muted-foreground">{t("backup.schedule.desc")}</p>

            {schedule.enabled && (
              <div className="space-y-3">
                <div className="grid grid-cols-2 gap-2">
                  <div className="space-y-1">
                    <Label className="text-xs">{t("backup.schedule.cronExpr")}</Label>
                    <Input
                      value={schedule.cron_expr}
                      onChange={(e) => setSchedule({ ...schedule, cron_expr: e.target.value })}
                      placeholder="0 3 * * *"
                      className="text-xs"
                    />
                  </div>
                  <div className="space-y-1">
                    <Label className="text-xs">{t("backup.schedule.retention")}</Label>
                    <Input
                      type="number"
                      min={1}
                      max={100}
                      value={schedule.retention_count}
                      onChange={(e) =>
                        setSchedule({ ...schedule, retention_count: parseInt(e.target.value) || 7 })
                      }
                      className="text-xs"
                    />
                  </div>
                </div>

                <div className="space-y-1">
                  <Label className="text-xs">
                    {t("backup.schedule.password")}{" "}
                    {schedule.has_password && (
                      <span className="text-success inline-flex items-center gap-0.5"><Check size={12} /> {t("backup.schedule.passwordSet")}</span>
                    )}
                  </Label>
                  <Input
                    type="password"
                    value={schedulePw}
                    onChange={(e) => setSchedulePw(e.target.value)}
                    placeholder={
                      schedule.has_password
                        ? t("backup.schedule.passwordPlaceholderUpdate")
                        : t("backup.schedule.passwordPlaceholder")
                    }
                    className="text-xs"
                    autoComplete="new-password"
                  />
                </div>

                <div className="flex gap-2">
                  <Button onClick={handleSaveSettings} disabled={savingSettings} size="sm">
                    {savingSettings ? t("backup.schedule.saving") : t("backup.schedule.save")}
                  </Button>
                  <Button
                    onClick={handleTrigger}
                    disabled={triggering || !schedule.has_password}
                    variant="outline"
                    size="sm"
                  >
                    {triggering ? t("backup.schedule.triggering") : t("backup.schedule.triggerNow")}
                  </Button>
                </div>
              </div>
            )}
          </div>
        )}

        {/* ── 备份历史 (Batch 5.6) ── */}
        {schedule?.enabled && (
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label className="text-sm font-medium">{t("backup.schedule.history")}</Label>
              <Button onClick={loadBackups} variant="ghost" size="sm" disabled={loadingBackups}>
                {t("backup.schedule.refresh")}
              </Button>
            </div>
            {backups.length === 0 ? (
              <p className="text-xs text-muted-foreground py-2">{t("backup.schedule.empty")}</p>
            ) : (
              <div className="space-y-1 max-h-48 overflow-y-auto">
                {backups.map((b) => (
                  <div
                    key={b.id}
                    className="flex items-center justify-between gap-2 rounded border px-2 py-1.5 text-xs"
                  >
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="font-mono truncate">{b.id}</span>
                        <span className="text-muted-foreground shrink-0">
                          {b.trigger === "manual" ? t("backup.schedule.manual") : t("backup.schedule.scheduled")}
                        </span>
                      </div>
                      <div className="text-muted-foreground">
                        {formatTime(b.created_at)} · {formatSize(b.encrypted_size)}
                      </div>
                    </div>
                    <Button
                      onClick={() => void handleDownload(b.id)}
                      variant="ghost"
                      size="sm"
                      className="h-6 px-2"
                    >
                      {t("backup.schedule.download")}
                    </Button>
                    <Button
                      onClick={() => handleDelete(b.id)}
                      variant="ghost"
                      size="sm"
                      className="text-destructive hover:text-destructive/80 h-6 px-2"
                    >
                      {t("backup.schedule.delete")}
                    </Button>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* ── 手动导出 ── */}
        <div className="space-y-2">
          <Label className="text-sm font-medium">{t("backup.exportTitle")}</Label>
          <p className="text-xs text-muted-foreground">{t("backup.exportDesc")}</p>
          <div className="flex gap-2">
            <Input
              type="password"
              placeholder={t("backup.passwordPlaceholder")}
              value={exportPw}
              onChange={(e) => setExportPw(e.target.value)}
              className="flex-1"
              autoComplete="new-password"
              aria-label={t("backup.exportTitle")}
            />
            <Button onClick={handleExport} disabled={exporting || exportPw.length < 4} size="sm">
              {exporting ? t("backup.exporting") : t("backup.export")}
            </Button>
          </div>
        </div>

        {/* ── 手动导入 ── */}
        <div className="space-y-2">
          <Label className="text-sm font-medium">{t("backup.importTitle")}</Label>
          <p className="text-xs text-muted-foreground">{t("backup.importDesc")}</p>
          <div className="flex gap-2 items-center">
            <Button onClick={handlePickFile} variant="outline" size="sm">
              {t("backup.selectFile")}
            </Button>
            <span className="text-xs text-muted-foreground truncate flex-1">
              {importPath || t("backup.noFileSelected")}
            </span>
          </div>
          {importPath && (
            <div className="flex gap-2">
              <Input
                type="password"
                placeholder={t("backup.passwordPlaceholder")}
                value={importPw}
                onChange={(e) => setImportPw(e.target.value)}
                className="flex-1"
                autoComplete="new-password"
                aria-label={t("backup.importTitle")}
              />
              <Button
                onClick={handleImport}
                disabled={importing || importPw.length < 4}
                variant="destructive"
                size="sm"
              >
                {importing ? t("backup.importing") : t("backup.restore")}
              </Button>
            </div>
          )}
        </div>

        {/* 重启确认 */}
        {restartPending && (
          <div className="rounded-md border border-warning/40 bg-warning/5 p-3 space-y-2">
            <p className="text-sm font-medium">{t("backup.restartRequired")}</p>
            <p className="text-xs text-muted-foreground">{t("backup.restartRequiredDesc")}</p>
            <div className="flex gap-2">
              <Button onClick={handleRestart} size="sm">
                {t("backup.restartNow")}
              </Button>
              <Button onClick={() => setRestartPending(false)} variant="outline" size="sm">
                {t("backup.later")}
              </Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
