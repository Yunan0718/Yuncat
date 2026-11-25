import { Bot, ChevronDown, Shield, ShieldAlert, ShieldCheck, Unlock } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuLabel,
  DropdownMenuTrigger,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
} from "@/components/ui/dropdown-menu";

import { PRESET_IDS, presetDescKey, presetLabelKey, type PresetId } from "../../lib/securityPreset";

/** 权限档位图标（轻量表达，不另外做一套语义色） */
const PRESET_ICON: Record<PresetId, typeof Shield> = {
  read_only: ShieldAlert,
  workspace_write: ShieldCheck,
  ai_review: Bot,
  full_access: Unlock,
  custom: Shield,
};

const PRESET_TONE: Record<PresetId, string> = {
  read_only: "text-destructive",
  workspace_write: "text-warning",
  ai_review: "text-accent",
  full_access: "text-muted-foreground",
  custom: "text-muted-foreground",
};

interface SecurityModeControlProps {
  preset: PresetId;
  setPreset: (p: PresetId) => void;
  t: (k: string, opts?: Record<string, unknown>) => string;
}

/**
 * 权限档位（权限 + 审批打包）——对标 Codex 的 `/approvals`。
 *
 * 之前是“安全模式 + 自动批准”两个分开的开关，用户要自己组合；现在只选一个预设，
 * 底层两个字段由后端一次写好。预设表在 security/presets.py，增减档位只改那里。
 */
export function SecurityModeControl({ preset, setPreset, t }: SecurityModeControlProps) {
  const Icon = PRESET_ICON[preset] ?? Shield;
  const tone = PRESET_TONE[preset] ?? "text-muted-foreground";

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        className="inline-flex h-7 shrink-0 items-center gap-1 rounded-lg px-2 text-[10px] font-medium text-muted-foreground transition-colors hover:bg-surface-3 hover:text-foreground"
        title={t("settings.securityProfile")}
        aria-label={t("settings.securityProfile")}
      >
        <Icon size={13} className={tone} />
        <span>{t(presetLabelKey(preset))}</span>
        <ChevronDown size={10} className="text-muted-foreground" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" side="top" sideOffset={6} className="w-64">
        <DropdownMenuGroup>
          <DropdownMenuLabel>{t("settings.securityProfile")}</DropdownMenuLabel>
        </DropdownMenuGroup>
        <DropdownMenuRadioGroup value={preset} onValueChange={(v) => setPreset(v as PresetId)}>
          {PRESET_IDS.map((id) => {
            const ItemIcon = PRESET_ICON[id];
            return (
              <DropdownMenuRadioItem key={id} value={id} closeOnClick title={t(presetDescKey(id))}>
                <span className="flex flex-col py-0.5">
                  <span className="flex items-center gap-1.5 text-sm">
                    <ItemIcon size={12} className="text-muted-foreground" />
                    {t(presetLabelKey(id))}
                  </span>
                  <span className="text-[10px] text-muted-foreground">{t(presetDescKey(id))}</span>
                </span>
              </DropdownMenuRadioItem>
            );
          })}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
