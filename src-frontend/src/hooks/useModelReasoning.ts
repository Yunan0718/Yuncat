import { useCallback, useEffect, useState } from "react";

import { sortEfforts } from "../lib/reasoningLevels";
import { useAppStore } from "../store/appStore";

/**
 * 当前模型真实支持的思考档位（后端 /api/v1/models/reasoning 提供）
 *
 * 为什么要一个共用 hook：模型选择器与对话区的强度控件都要知道“这个模型能选哪几档”，
 * 各自拉一遍就会出现两处不一致（一处置灰另一处还能点）。
 */
export interface ModelReasoningInfo {
  /** 后端推断的模型家族（仅供诊断展示） */
  family: string;
  /** 该模型可选的档位，已按标准顺序排好 */
  efforts: string[];
  /** 后端认为的默认档 */
  defaultEffort: string;
  /** 厂商显式声明不接收该参数时为 false，此时不应让用户选 */
  effortSupported: boolean;
}

const EMPTY: ModelReasoningInfo = {
  family: "",
  efforts: [],
  defaultEffort: "",
  effortSupported: true,
};

export function useModelReasoning(
  api: <T>(path: string, init?: RequestInit) => Promise<T>,
) {
  const backendReady = useAppStore((s) => s.backendReady);
  const [info, setInfo] = useState<ModelReasoningInfo>(EMPTY);
  const [loading, setLoading] = useState(false);

  const reload = useCallback(() => {
    if (!backendReady) return;
    setLoading(true);
    api<{ model?: string }>("/api/v1/models/active")
      .then((active) => {
        const model = active?.model || "";
        if (!model) return undefined;
        return api<{ efforts?: string[]; default_effort?: string; effort_supported?: boolean; family?: string }>(
          `/api/v1/models/reasoning?model=${encodeURIComponent(model)}`,
        ).then((res) => {
          setInfo({
            family: res?.family || "",
            efforts: sortEfforts(Array.isArray(res?.efforts) ? res.efforts : []),
            defaultEffort: res?.default_effort || "",
            effortSupported: res?.effort_supported !== false,
          });
        });
      })
      .catch((error) => {
        // 取不到就不限制（空列表 = 未知），后端仍会按模型自己 clamp
        console.warn("useModelReasoning: load failed:", error);
        setInfo(EMPTY);
      })
      .finally(() => setLoading(false));
  }, [api, backendReady]);

  useEffect(() => {
    reload();
  }, [reload]);

  return { info, loading, reload };
}
