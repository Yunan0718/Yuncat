/**
 * 工具视图注册表测试。
 *
 * 借鉴 deepseek-harness 的 keyed toolview 分发：`resolveToolView` 按工具名
 * 返回视图定义（variant + 摘要函数），未注册工具走 GENERIC 兜底。
 * 覆盖重点：变体分类、摘要取值、JSON 字符串 args 兼容、以及兜底永不返回 undefined。
 */
import { describe, it, expect } from "vitest";
import { resolveToolView, toolSummary, toolVariant } from "../toolViews";

describe("toolVariant", () => {
  it("内置工具分类正确", () => {
    expect(toolVariant("read_file")).toBe("read");
    expect(toolVariant("write_file")).toBe("write");
    expect(toolVariant("edit_file")).toBe("edit");
    expect(toolVariant("delete_file")).toBe("delete");
    expect(toolVariant("run_command")).toBe("bash");
    expect(toolVariant("search_files")).toBe("search");
    expect(toolVariant("list_dir")).toBe("dir");
    expect(toolVariant("docs_lookup")).toBe("web");
    expect(toolVariant("sequential_thinking")).toBe("think");
    expect(toolVariant("delegate_to_agent")).toBe("delegate");
    expect(toolVariant("install_skill_from_url")).toBe("install");
  });

  it("未注册工具 / 空名落到 other 兜底", () => {
    expect(toolVariant("some_mcp_tool")).toBe("other");
    expect(toolVariant("")).toBe("other");
    expect(toolVariant(undefined)).toBe("other");
    expect(toolVariant(null)).toBe("other");
  });
});

describe("resolveToolView", () => {
  it("未注册工具仍返回可用定义（GenericToolCard 式兜底）", () => {
    const view = resolveToolView("unknown_tool");
    expect(view.variant).toBe("other");
    expect(typeof view.summary).toBe("function");
  });
});

describe("toolSummary", () => {
  it("read_file 带 offset/limit 时显示行区间", () => {
    expect(toolSummary("read_file", { path: "src/app.tsx" })).toBe("src/app.tsx");
    expect(toolSummary("read_file", { path: "src/app.tsx", offset: 101, limit: 100 })).toBe(
      "src/app.tsx (L101-201)",
    );
    expect(toolSummary("read_file", { path: "src/app.tsx", offset: 101 })).toBe("src/app.tsx (L101+)");
  });

  it("run_command 摘要是命令本身", () => {
    expect(toolSummary("run_command", { command: "npm run build" })).toBe("npm run build");
  });

  it("search_files 摘要为 /pattern/ + 限定目录", () => {
    expect(toolSummary("search_files", { pattern: "useState" })).toBe("/useState/");
    expect(toolSummary("search_files", { pattern: "useState", path: "src" })).toBe("/useState/ in src");
  });

  it("rename_file 摘要为 old → new", () => {
    expect(toolSummary("rename_file", { old_path: "a.ts", new_path: "b.ts" })).toBe("a.ts → b.ts");
  });

  it("docs_lookup / delegate_to_agent 摘要为组合字段", () => {
    expect(toolSummary("docs_lookup", { library: "fastapi", query: "lifespan" })).toBe("fastapi — lifespan");
    expect(toolSummary("delegate_to_agent", { target_agent: "hermes", tool_name: "run_command" })).toBe(
      "hermes · run_command",
    );
  });

  it("缺参数时返回 null（渲染处不显示摘要）", () => {
    expect(toolSummary("read_file", {})).toBeNull();
    expect(toolSummary("run_command", { command: "  " })).toBeNull();
    expect(toolSummary("read_file", undefined)).toBeNull();
    expect(toolSummary("read_file", null)).toBeNull();
  });

  it("args 为 JSON 字符串时同样可解析", () => {
    expect(toolSummary("run_command", '{"command":"git status"}')).toBe("git status");
    expect(toolSummary("run_command", "{ 坏掉的 json")).toBeNull();
    expect(toolSummary("run_command", "not json at all")).toBeNull();
  });

  it("换行被压成单行", () => {
    expect(toolSummary("run_command", { command: "npm\n  run\n  build" })).toBe("npm run build");
  });

  it("未注册工具走兜底：命中常见键", () => {
    expect(toolSummary("mcp_some_tool", { path: "a/b.ts" })).toBe("a/b.ts");
    expect(toolSummary("mcp_some_tool", { query: "hello" })).toBe("hello");
  });

  it("未注册工具走兜底：退化到第一个字符串值", () => {
    expect(toolSummary("mcp_some_tool", { depth: 3, target: "x.ts" })).toBe("x.ts");
  });

  it("未注册工具无可取值时返回 null", () => {
    expect(toolSummary("mcp_some_tool", { depth: 3, flag: true })).toBeNull();
    expect(toolSummary("mcp_some_tool", [])).toBeNull();
  });
});
