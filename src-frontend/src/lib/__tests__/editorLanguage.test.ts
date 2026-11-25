import { describe, expect, it } from "vitest";

import { detectLanguage } from "../editorLanguage";

/**
 * 2026-09-15：语言识别从 MonacoEditor 抽出后补上这一层测试。
 * 之前 .h / .cxx / .xml / .csv 这类常见文件没有映射，打开后只是纯文本（无高亮）。
 */
describe("detectLanguage", () => {
  it("用户手改的常见文件都要有语言（不是 plaintext）", () => {
    expect(detectLanguage("src/main.c")).toBe("c");
    expect(detectLanguage("include/util.h")).toBe("c");
    expect(detectLanguage("src/engine.cpp")).toBe("cpp");
    expect(detectLanguage("src/engine.cxx")).toBe("cpp");
    expect(detectLanguage("scripts/task.py")).toBe("python");
    expect(detectLanguage("docs/readme.md")).toBe("markdown");
    expect(detectLanguage("notes/plan.txt")).toBe("plaintext");
    expect(detectLanguage("data/config.xml")).toBe("xml");
    expect(detectLanguage("data/table.csv")).toBe("plaintext");
  });

  it("大小写与多级路径都能识别", () => {
    expect(detectLanguage("A/B/C/MAIN.PY")).toBe("python");
    expect(detectLanguage("dir.with.dots/file.tsx")).toBe("typescript");
  });

  it("按文件名识别的特例（没有扩展名）", () => {
    expect(detectLanguage("Dockerfile")).toBe("dockerfile");
    expect(detectLanguage("project/Dockerfile")).toBe("dockerfile");
    expect(detectLanguage(".env")).toBe("ini");
    expect(detectLanguage("Makefile")).toBe("plaintext");
  });

  it("认不出时回落到 plaintext（仍可编辑，不报错）", () => {
    expect(detectLanguage("weird.unknown-ext")).toBe("plaintext");
    expect(detectLanguage("no-extension")).toBe("plaintext");
  });
});
