/**
 * Batch 5.6: 备份调度 + 历史管理 Hook
 *
 * 封装 backup schedule settings / history / trigger / delete 的 API 调用。
 */
import { useCallback } from "react";
import { useApi } from "./useApi";

export interface BackupScheduleSettings {
  enabled: boolean;
  cron_expr: string;
  retention_count: number;
  has_password: boolean;
}

export interface BackupScheduleUpdate {
  enabled?: boolean;
  cron_expr?: string;
  retention_count?: number;
  password?: string | null; // null=不改, ""=清除, 非空=更新
}

export interface BackupHistoryItem {
  id: string;
  created_at: number;
  trigger: string;
  changed_since_last: boolean;
  change_reason?: string;
  files: Array<{
    name: string;
    size: number;
    mtime: number;
    sha256: string;
  }>;
  encrypted_size: number;
}

export interface BackupTriggerResult {
  created: boolean;
  skipped?: boolean;
  reason?: string;
  backup_id?: string;
  manifest?: BackupHistoryItem;
  cleaned?: number;
}

export function useBackupSchedule() {
  const { api } = useApi();

  const getSettings = useCallback(async (): Promise<BackupScheduleSettings> => {
    return api<BackupScheduleSettings>("/api/v1/config/backup/settings");
  }, [api]);

  const updateSettings = useCallback(
    async (update: BackupScheduleUpdate): Promise<{ ok: boolean }> => {
      return api<{ ok: boolean }>("/api/v1/config/backup/settings", {
        method: "PUT",
        body: JSON.stringify(update),
      });
    },
    [api],
  );

  const listBackups = useCallback(
    async (limit = 50): Promise<{ items: BackupHistoryItem[]; count: number }> => {
      return api(`/api/v1/config/backup/list?limit=${limit}`);
    },
    [api],
  );

  const triggerBackup = useCallback(async (): Promise<BackupTriggerResult> => {
    return api<BackupTriggerResult>("/api/v1/config/backup/trigger", {
      method: "POST",
    });
  }, [api]);

  const deleteBackup = useCallback(
    async (backupId: string): Promise<{ ok: boolean; deleted: string }> => {
      return api(`/api/v1/config/backup/${encodeURIComponent(backupId)}`, {
        method: "DELETE",
      });
    },
    [api],
  );

  return {
    getSettings,
    updateSettings,
    listBackups,
    triggerBackup,
    deleteBackup,
  };
}
