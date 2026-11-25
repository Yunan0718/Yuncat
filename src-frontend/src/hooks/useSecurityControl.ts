import { useCallback, useEffect, useState } from "react";

import { normalizePreset, PRESET_ENDPOINT, type PresetId } from "../lib/securityPreset";

/**
 * 权限档位（含审批）的读写——单一来源
 *
 * 对话区与设置页共用这个 hook：两处界面各自存一份状态就会
 * 出现“一处切了另一处还是旧的”。切档先改界面再写后端，失败回滚。
 */
export function useSecurityControl(
  api: <T>(path: string, init?: RequestInit) => Promise<T>,
  onError?: (message: string) => void,
) {
  const [preset, setPreset] = useState<PresetId>("workspace_write");

  const reload = useCallback(() => {
    api<{ preset?: string }>(PRESET_ENDPOINT)
      .then((res) => setPreset(normalizePreset(res?.preset)))
      .catch((error) => {
        console.warn("useSecurityControl: preset load failed:", error);
      });
  }, [api]);

  useEffect(() => {
    reload();
  }, [reload]);

  const change = useCallback(
    (next: PresetId) => {
      const previous = preset;
      setPreset(next);
      api(PRESET_ENDPOINT, { method: "POST", body: JSON.stringify({ preset: next }) }).catch(() => {
        setPreset(previous); // 失败回滚：界面不能显示一个没生效的档位
        onError?.("securityPreset");
      });
    },
    [api, preset, onError],
  );

  return { preset, change, reload };
}
