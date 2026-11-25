import { describe, expect, it } from "vitest";

import {
  EFFORT_FOLLOW_KEY,
  effortDescKey,
  effortLabelKey,
  EFFORT_ORDER,
  isEffortLevel,
  sortEfforts,
} from "../reasoningLevels";

describe("reasoningLevels", () => {
  it("档位顺序从关到最高", () => {
    expect(EFFORT_ORDER).toEqual(["none", "minimal", "low", "medium", "high", "xhigh"]);
  });

  it("后端给的档位按标准顺序排好", () => {
    expect(sortEfforts(["high", "none", "medium"])).toEqual(["none", "medium", "high"]);
  });

  it("不认识的档位不丢，排在末尾", () => {
    expect(sortEfforts(["weird", "low"])).toEqual(["low", "weird"]);
  });

  it("每个档位都有标题与说明的文案 key", () => {
    for (const level of EFFORT_ORDER) {
      expect(effortLabelKey(level)).toMatch(/^chat\.effort/);
      expect(effortDescKey(level)).toMatch(/^chat\.effort/);
    }
  });

  it("未知档位回退到原值，不会把 key 拼错", () => {
    expect(effortLabelKey("weird")).toBe("weird");
    expect(effortDescKey("weird")).toBe("");
    expect(isEffortLevel("weird")).toBe(false);
  });

  it("跟随档位的文案 key 固定", () => {
    expect(EFFORT_FOLLOW_KEY).toBe("chat.effortFollowCap");
  });
});
