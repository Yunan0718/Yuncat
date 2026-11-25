import { describe, it, expect, beforeEach } from "vitest";
import { matchShortcut, isTypingElement, getShortcutKeys, SHORTCUT_DEFS } from "../shortcuts";

describe("shortcuts", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("SHORTCUT_DEFS 包含全局帮助快捷键", () => {
    const help = SHORTCUT_DEFS.find((s) => s.id === "shortcut_help");
    expect(help).toBeDefined();
    // 默认禁用——用户需手动启用
    expect(help?.defaultKeys).toBe("");
  });

  it("matchShortcut 默认禁用时不匹配任何快捷键", () => {
    const event = new KeyboardEvent("keydown", { key: "?", shiftKey: true });
    expect(matchShortcut(event, "shortcut_help")).toBe(false);
  });

  it("matchShortcut 用户启用后识别 Shift+? 为帮助快捷键", () => {
    localStorage.setItem("yuncat-shortcuts", JSON.stringify({ shortcut_help: "Shift+?" }));
    const event = new KeyboardEvent("keydown", { key: "?", shiftKey: true });
    expect(matchShortcut(event, "shortcut_help")).toBe(true);
  });

  it("getShortcutKeys 对未分配的功能返回空字符串（即未启用）", () => {
    expect(getShortcutKeys("undo")).toBe("");
  });

  it("命令面板默认 Ctrl+Shift+P（与 VS Code/Cursor 一致）", () => {
    expect(getShortcutKeys("command_palette")).toBe("Ctrl+Shift+P");
  });

  it("getShortcutKeys 返回用户覆盖组合", () => {
    localStorage.setItem("yuncat-shortcuts", JSON.stringify({ command_palette: "Ctrl+Shift+P" }));
    expect(getShortcutKeys("command_palette")).toBe("Ctrl+Shift+P");
  });

  it("isTypingElement 识别 input/textarea/contenteditable", () => {
    const input = document.createElement("input");
    const textarea = document.createElement("textarea");
    const div = document.createElement("div");
    div.contentEditable = "true";
    const button = document.createElement("button");

    expect(isTypingElement(input)).toBe(true);
    expect(isTypingElement(textarea)).toBe(true);
    expect(isTypingElement(div)).toBe(true);
    expect(isTypingElement(button)).toBe(false);
    expect(isTypingElement(null)).toBe(false);
  });
});
