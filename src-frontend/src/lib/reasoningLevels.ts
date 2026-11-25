/**
 * lib/reasoningLevels.ts —— 思考强度档位的展示顺序与文案 key
 *
 * 档位表本身在后端 services/reasoning.py（哪个模型支持哪几档由它决定），
 * 这里只管前端两件事：**显示顺序**与**每档的标题/说明文案 key**。
 * 不要在组件里再写一份档位字面量——后端回什么我们就显示什么。
 */
export const EFFORT_ORDER = ["none", "minimal", "low", "medium", "high", "xhigh"] as const;

export type EffortLevel = (typeof EFFORT_ORDER)[number];

const LABEL_KEYS: Record<EffortLevel, string> = {
  none: "chat.effortNone",
  minimal: "chat.effortMinimal",
  low: "chat.effortLow",
  medium: "chat.effortMedium",
  high: "chat.effortHigh",
  xhigh: "chat.effortXhigh",
};

const DESC_KEYS: Record<EffortLevel, string> = {
  none: "chat.effortNoneDesc",
  minimal: "chat.effortMinimalDesc",
  low: "chat.effortLowDesc",
  medium: "chat.effortMediumDesc",
  high: "chat.effortHighDesc",
  xhigh: "chat.effortXhighDesc",
};

export const EFFORT_FOLLOW_KEY = "chat.effortFollowCap";
export const EFFORT_CUSTOM_KEY = "chat.effortCustom";

export function isEffortLevel(value: unknown): value is EffortLevel {
  return typeof value === "string" && (EFFORT_ORDER as readonly string[]).includes(value);
}

/** 后端给的档位按标准顺序排；不认识的档位排在末尾（不丢，照样能选） */
export function sortEfforts(efforts: readonly string[]): string[] {
  return [...efforts].sort((a, b) => {
    const ai = EFFORT_ORDER.indexOf(a as EffortLevel);
    const bi = EFFORT_ORDER.indexOf(b as EffortLevel);
    return (ai === -1 ? EFFORT_ORDER.length : ai) - (bi === -1 ? EFFORT_ORDER.length : bi);
  });
}

export function effortLabelKey(level: string): string {
  return isEffortLevel(level) ? LABEL_KEYS[level] : level;
}

export function effortDescKey(level: string): string {
  return isEffortLevel(level) ? DESC_KEYS[level] : "";
}
