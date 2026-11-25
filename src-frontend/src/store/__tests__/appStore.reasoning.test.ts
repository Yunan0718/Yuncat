/**
 * 思考强度的“跟随档位 vs 用户显式选”语义（2026-09-17）
 *
 * 对标 Codex：能力档位与思考强度分开。用户没选过档位时，切能力档会带着
 * 思考强度走；一旦用户在模型选择器里显式选过，再改能力档就不能覆盖他的选择。
 */
import { beforeEach, describe, expect, it } from "vitest";

import { capabilityToReasoning, useAppStore } from "../appStore";

const initial = useAppStore.getState();

beforeEach(() => {
  localStorage.clear();
  useAppStore.setState({
    capabilityLevel: initial.capabilityLevel,
    reasoningEffort: capabilityToReasoning(initial.capabilityLevel),
    reasoningEffortSource: "auto",
  });
});

describe("思考强度的来源", () => {
  it("默认跟随能力档位（只有默认 / MAX 两档）", () => {
    useAppStore.getState().setCapabilityLevel(2);
    expect(useAppStore.getState().reasoningEffort).toBe("high");

    useAppStore.getState().setCapabilityLevel(1);
    expect(useAppStore.getState().reasoningEffort).toBe("medium");
  });

  it("旧的“轻量”值（0）归到默认档", () => {
    useAppStore.getState().setCapabilityLevel(0);
    expect(useAppStore.getState().capabilityLevel).toBe(1);
  });

  it("用户选过后切能力档不再覆盖", () => {
    useAppStore.getState().setReasoningEffort("xhigh");
    expect(useAppStore.getState().reasoningEffortSource).toBe("manual");

    useAppStore.getState().setCapabilityLevel(1);

    expect(useAppStore.getState().reasoningEffort).toBe("xhigh");
    expect(useAppStore.getState().capabilityLevel).toBe(1);
  });

  it("可以切回跟随，并重新跟上能力档", () => {
    useAppStore.getState().setReasoningEffort("low");
    useAppStore.getState().setCapabilityLevel(2);
    useAppStore.getState().resetReasoningEffort();

    const state = useAppStore.getState();
    expect(state.reasoningEffortSource).toBe("auto");
    expect(state.reasoningEffort).toBe(capabilityToReasoning(2));
  });

  it("显式选过的档位会记到 localStorage", () => {
    useAppStore.getState().setReasoningEffort("minimal");
    expect(localStorage.getItem("yuncat-reasoning-effort")).toBe("minimal");

    useAppStore.getState().resetReasoningEffort();
    expect(localStorage.getItem("yuncat-reasoning-effort")).toBeNull();
  });
});

describe("MAX \u5f00\u5173\uff08\u80fd\u529b\u6863\u4e24\u6863\uff09", () => {
  it("\u5f00 MAX \u2192 2\uff0c\u5173 MAX \u2192 1", () => {
    useAppStore.getState().setMaxMode(true);
    expect(useAppStore.getState().capabilityLevel).toBe(2);
    expect(useAppStore.getState().reasoningEffort).toBe("high");

    useAppStore.getState().setMaxMode(false);
    expect(useAppStore.getState().capabilityLevel).toBe(1);
    expect(useAppStore.getState().reasoningEffort).toBe("medium");
  });

  it("\u624b\u52a8\u9009\u8fc7\u601d\u8003\u6863\u540e\uff0c\u5f00\u5173 MAX \u4e0d\u8986\u76d6\u5b83", () => {
    useAppStore.getState().setReasoningEffort("xhigh");
    useAppStore.getState().setMaxMode(true);

    expect(useAppStore.getState().reasoningEffort).toBe("xhigh");
    expect(useAppStore.getState().capabilityLevel).toBe(2);
  });
});
