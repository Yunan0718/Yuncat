/**
 * FE5: xterm 终端 ANSI 16 色补全测试。
 *
 * 修复背景：theme 原先只有 background/foreground/cursor，ANSI 0-15 回退到 xterm
 * 内置默认色，与 UI 主题不一致。现补全 VS Code Dark+ / Light+ 的 16 色。
 */
import { describe, it, expect } from "vitest";
import { buildDarkTheme, buildLightTheme } from "../workspace/TerminalPanel";

const ANSI_KEYS = [
  "black",
  "red",
  "green",
  "yellow",
  "blue",
  "magenta",
  "cyan",
  "white",
  "brightBlack",
  "brightRed",
  "brightGreen",
  "brightYellow",
  "brightBlue",
  "brightMagenta",
  "brightCyan",
  "brightWhite",
] as const;

const HEX = /^#[0-9a-fA-F]{6}$/;

describe("FE5: xterm ANSI 16 色", () => {
  it("暗色主题补全 16 个 ANSI 色", () => {
    const theme = buildDarkTheme();
    for (const key of ANSI_KEYS) {
      expect(theme[key], `暗色主题缺少 ${key}`).toBeTruthy();
      expect(theme[key]).toMatch(HEX);
    }
  });

  it("亮色主题补全 16 个 ANSI 色", () => {
    const theme = buildLightTheme();
    for (const key of ANSI_KEYS) {
      expect(theme[key], `亮色主题缺少 ${key}`).toBeTruthy();
      expect(theme[key]).toMatch(HEX);
    }
  });
});
