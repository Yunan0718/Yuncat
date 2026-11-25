import { describe, expect, it } from "vitest";

import { normalizePreset, presetDescKey, presetLabelKey, PRESET_IDS } from "../securityPreset";

describe("securityPreset", () => {
  it("可选预设就这四个（custom 是状态不是选项）", () => {
    expect(PRESET_IDS).toEqual(["read_only", "workspace_write", "ai_review", "full_access"]);
  });

  it("后端返回合法预设时原样返回", () => {
    for (const id of PRESET_IDS) {
      expect(normalizePreset(id)).toBe(id);
    }
  });

  it("后端返回不认识的值时当自定义，不假装成某个预设", () => {
    for (const raw of [undefined, null, "", "yolo", 1, {}]) {
      expect(normalizePreset(raw)).toBe("custom");
    }
  });

  it("每个预设都有标题与说明的文案 key", () => {
    for (const id of [...PRESET_IDS, "custom"] as const) {
      expect(presetLabelKey(id).length).toBeGreaterThan(0);
      expect(presetDescKey(id).length).toBeGreaterThan(0);
    }
  });
});
