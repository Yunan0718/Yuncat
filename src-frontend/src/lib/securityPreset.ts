/**
 * lib/securityPreset.ts —— 权限档位预设（权限 + 审批打包成一个选择）
 *
 * 对标 Codex 的 `/approvals`：用户只选一个预设，底下的权限档与审批方式
 * 由后端一次写好（GET/POST /api/v1/security/preset）。预设表在后端
 * security/presets.py，这里只存标题/说明的 i18n key与归一函数。
 */
export type PresetId = "read_only" | "workspace_write" | "ai_review" | "full_access" | "custom";

/** 可选预设（custom 是“底层不是默认组合”的状态，不在选项里） */
export const PRESET_IDS: readonly PresetId[] = [
  "read_only",
  "workspace_write",
  "ai_review",
  "full_access",
];

export const PRESET_ENDPOINT = "/api/v1/security/preset";

const LABEL_KEYS: Record<PresetId, string> = {
  read_only: "settings.securityStrict",
  workspace_write: "settings.securityStandard",
  ai_review: "chat.approvalAi",
  full_access: "settings.securityDeveloper",
  custom: "chat.presetCustom",
};

const DESC_KEYS: Record<PresetId, string> = {
  read_only: "settings.securityStrictDesc",
  workspace_write: "settings.securityStandardDesc",
  ai_review: "chat.approvalAiDesc",
  full_access: "settings.securityDeveloperDesc",
  custom: "chat.presetCustomDesc",
};

export function presetLabelKey(id: PresetId): string {
  return LABEL_KEYS[id];
}

export function presetDescKey(id: PresetId): string {
  return DESC_KEYS[id];
}

/** 后端返回值归一；认不出来就当自定义（不能假装成某个预设） */
export function normalizePreset(raw: unknown): PresetId {
  if (typeof raw === "string" && (PRESET_IDS as readonly string[]).includes(raw)) {
    return raw as PresetId;
  }
  return "custom";
}
