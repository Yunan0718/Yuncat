import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  parseReferences,
  stripReferences,
  resolveFolderRefs,
  resolveGitRefs,
  resolveWebRefs,
} from "../context-pills";
import type { ContextRef } from "../context-pills";

const { mockApi, mockToast } = vi.hoisted(() => ({
  mockApi: vi.fn(),
  mockToast: vi.fn(),
}));

vi.mock("../../hooks/useApi", () => ({
  useApi: () => ({ api: mockApi }),
}));

vi.mock("../../store/taskStore", () => ({
  useTaskStore: () => ({
    contextUsage: { budgets: { tool_result_limit: 3000 } },
  }),
}));

vi.mock("../common/toast-context", () => ({
  emitGlobalToast: mockToast,
}));

const t = (key: string, opts?: Record<string, unknown>) => {
  if (opts && "value" in opts && "count" in opts) return `${key}:${opts.value}:${opts.count}`;
  if (opts && "value" in opts) return `${key}:${opts.value}`;
  if (opts && "path" in opts) return `${key}:${opts.path}`;
  if (opts && "pct" in opts) return `${key}:${opts.pct}`;
  return key;
};

describe("context-pills", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe("parseReferences", () => {
    it("Batch 4.3: 解析 folder/web/git 引用", () => {
      const refs = parseReferences("请分析 @folder:src 和 @git:diff，再搜 @web:python");
      expect(refs.map((r) => ({ type: r.type, value: r.value }))).toEqual([
        { type: "folder", value: "src" },
        { type: "git", value: "diff" },
        { type: "web", value: "python" },
      ]);
    });

    it("保留所有已知类型并按出现顺序", () => {
      const refs = parseReferences("@agent:hermes @memory:rag @file:test.py @session:s1 @folder:docs @web:ai @git:log");
      expect(refs.map((r) => r.type)).toEqual([
        "agent", "memory", "file", "session", "folder", "web", "git",
      ]);
    });

    it("忽略未知类型", () => {
      const refs = parseReferences("@unknown:value @file:ok.py");
      expect(refs).toHaveLength(1);
      expect(refs[0].type).toBe("file");
    });

    it("value 遇到中文标点终止，避免吞入后续句子", () => {
      const refs = parseReferences("看 @folder:docs，然后 @web:query。");
      expect(refs[0].value).toBe("docs");
      expect(refs[1].value).toBe("query");
    });

    it("记录 raw 与 startIndex", () => {
      const refs = parseReferences("abc @folder:src def");
      expect(refs[0].raw).toBe("@folder:src");
      expect(refs[0].startIndex).toBe(4);
    });
  });

  describe("stripReferences", () => {
    it("去除引用并压缩空白", () => {
      const text = "请分析 @folder:src 和 @git:diff。";
      const refs = parseReferences(text);
      expect(stripReferences(text, refs)).toBe("请分析 和 。");
    });

    it("无引用时返回原文字", () => {
      expect(stripReferences("hello world", [])).toBe("hello world");
    });
  });

  describe("resolveFolderRefs", () => {
    it("调用 workspace/files 并渲染目录树", async () => {
      mockApi.mockResolvedValue([
        { name: "src", path: "/ws/src", is_dir: true, size: 0, modified: 1 },
        { name: "README.md", path: "/ws/README.md", is_dir: false, size: 10, modified: 2 },
      ]);
      const refs: ContextRef[] = [{ type: "folder", value: "docs", raw: "@folder:docs", startIndex: 0 }];
      const result = await resolveFolderRefs(refs, mockApi, t);
      expect(mockApi).toHaveBeenCalledWith("/api/v1/workspace/files?dir=docs");
      expect(result).toContain("📁 src/");
      expect(result).toContain("📄 README.md");
    });

    it("无 folder 引用时返回空串", async () => {
      const result = await resolveFolderRefs([], mockApi, t);
      expect(result).toBe("");
      expect(mockApi).not.toHaveBeenCalled();
    });

    it("API 失败时 toast 并返回空串", async () => {
      mockApi.mockRejectedValue(new Error("network"));
      const refs: ContextRef[] = [{ type: "folder", value: "x", raw: "@folder:x", startIndex: 0 }];
      const result = await resolveFolderRefs(refs, mockApi, t);
      expect(result).toBe("");
      expect(mockToast).toHaveBeenCalled();
    });
  });

  describe("resolveGitRefs", () => {
    it("@git:diff 调用 /api/v1/git/diff", async () => {
      mockApi.mockResolvedValue({ diff: "-a\n+b" });
      const refs: ContextRef[] = [{ type: "git", value: "diff", raw: "@git:diff", startIndex: 0 }];
      const result = await resolveGitRefs(refs, mockApi, t);
      expect(mockApi).toHaveBeenCalledWith("/api/v1/git/diff");
      expect(result).toContain("-a");
      expect(result).toContain("+b");
    });

    it("@git:status 调用 /api/v1/git/status", async () => {
      mockApi.mockResolvedValue({ modified: ["a.py"] });
      const refs: ContextRef[] = [{ type: "git", value: "status", raw: "@git:status", startIndex: 0 }];
      const result = await resolveGitRefs(refs, mockApi, t);
      expect(mockApi).toHaveBeenCalledWith("/api/v1/git/status");
      expect(result).toContain('"modified"');
    });

    it("默认调用 /api/v1/git/log", async () => {
      mockApi.mockResolvedValue([{ hash: "abc" }]);
      const refs: ContextRef[] = [{ type: "git", value: "log", raw: "@git:log", startIndex: 0 }];
      const result = await resolveGitRefs(refs, mockApi, t);
      expect(mockApi).toHaveBeenCalledWith("/api/v1/git/log?count=20");
      expect(result).toContain("abc");
    });

    it("API 失败时 toast 并返回空串", async () => {
      mockApi.mockRejectedValue(new Error("git error"));
      const refs: ContextRef[] = [{ type: "git", value: "diff", raw: "@git:diff", startIndex: 0 }];
      const result = await resolveGitRefs(refs, mockApi, t);
      expect(result).toBe("");
      expect(mockToast).toHaveBeenCalled();
    });
  });

  describe("resolveWebRefs", () => {
    it("OC 成功时渲染搜索结果", async () => {
      mockApi.mockResolvedValue({
        ok: true,
        source: "openclaw",
        results: [{ title: "Python", url: "https://python.org", snippet: "official" }],
      });
      const refs: ContextRef[] = [{ type: "web", value: "python", raw: "@web:python", startIndex: 0 }];
      const result = await resolveWebRefs(refs, mockApi, t);
      expect(mockApi).toHaveBeenCalledWith("/api/v1/web/search?q=python");
      expect(result).toContain("Python");
      expect(result).toContain("https://python.org");
    });

    it("OC 不可用时渲染 URL hint", async () => {
      mockApi.mockResolvedValue({
        ok: true,
        source: "url_hint",
        results: [],
        url_hint: { google: "https://www.google.com/search?q=ai" },
      });
      const refs: ContextRef[] = [{ type: "web", value: "ai", raw: "@web:ai", startIndex: 0 }];
      const result = await resolveWebRefs(refs, mockApi, t);
      expect(result).toContain("google");
      expect(result).toContain("https://www.google.com/search?q=ai");
    });

    it("无结果无 hint 时返回空串", async () => {
      mockApi.mockResolvedValue({ ok: false, results: [] });
      const refs: ContextRef[] = [{ type: "web", value: "x", raw: "@web:x", startIndex: 0 }];
      const result = await resolveWebRefs(refs, mockApi, t);
      expect(result).toBe("");
    });

    it("API 失败时 toast 并返回空串", async () => {
      mockApi.mockRejectedValue(new Error("network"));
      const refs: ContextRef[] = [{ type: "web", value: "x", raw: "@web:x", startIndex: 0 }];
      const result = await resolveWebRefs(refs, mockApi, t);
      expect(result).toBe("");
      expect(mockToast).toHaveBeenCalled();
    });
  });
});
