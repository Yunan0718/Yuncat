import { describe, expect, it, afterEach } from "vitest";

import {
  clearExternalPreviewDefinitions,
  documentFileName,
  isBinaryPreview,
  listPreviewDefinitions,
  matchPreviewDefinition,
  registerPreviewDefinition,
} from "../previewRegistry";
import { isDocumentPreview, previewKind } from "../workspaceTabs";

/**
 * 预览注册表（2026-09-16，借鉴 deepseek-harness 的 document/registry）。
 * 旧实现是 switch(ext)，这几条是它能做到而 switch 做不到的：最长后缀、外部优先、二进制判定。
 */
afterEach(() => clearExternalPreviewDefinitions());

describe("documentFileName", () => {
  it("兼容反斜杠路径并转小写", () => {
    expect(documentFileName("C:\\Work\\Docs\\Report.DOCX")).toBe("report.docx");
    expect(documentFileName("a/b/c.PNG")).toBe("c.png");
  });
});

describe("matchPreviewDefinition", () => {
  it("按后缀命中内置实现", () => {
    expect(matchPreviewDefinition("a.png")?.id).toBe("image");
    expect(matchPreviewDefinition("a.docx")?.id).toBe("document");
    expect(matchPreviewDefinition("a.xlsm")?.id).toBe("spreadsheet");
    expect(matchPreviewDefinition("a.pptx")?.id).toBe("slides");
  });

  it("不认识的后缀返回 null（不猜）", () => {
    expect(matchPreviewDefinition("a.psd")).toBeNull();
    expect(matchPreviewDefinition("noext")).toBeNull();
  });

  it("最长后缀优先：复合后缀不会被短后缀抢走", () => {
    registerPreviewDefinition({ id: "html", extensions: ["tar.gz"], loading: "bytes", priority: "extension" });
    registerPreviewDefinition({ id: "pdf", extensions: ["gz"], loading: "bytes", priority: "extension" });
    expect(matchPreviewDefinition("backup.tar.gz")?.extensions).toContain("tar.gz");
  });

  it("外部实现优先于内置实现", () => {
    registerPreviewDefinition({ id: "image", extensions: ["png"], loading: "bytes", priority: "extension" });
    expect(matchPreviewDefinition("logo.png")?.priority).toBe("extension");
  });

  it("列表里外部在前、内置在后", () => {
    registerPreviewDefinition({ id: "slides", extensions: ["key"], loading: "bytes" });
    const list = listPreviewDefinitions();
    expect(list[0].extensions).toContain("key");
    expect(list.length).toBeGreaterThan(5);
  });
});

describe("isBinaryPreview", () => {
  it("二进制后缀不再走纯文本兜底", () => {
    expect(isBinaryPreview("a.docx")).toBe(true);
    expect(isBinaryPreview("a.xlsx")).toBe(true);
    expect(isBinaryPreview("a.html")).toBe(false);
  });
});

describe("与 workspaceTabs 的公开 API 一致", () => {
  it("previewKind 仍然只返回媒体/文档类，html 走文本通道", () => {
    expect(previewKind("A.PNG")).toBe("image");
    expect(previewKind("doc/a.pdf")).toBe("pdf");
    expect(previewKind("report.docx")).toBe("document");
    expect(previewKind("index.html")).toBeNull();
    expect(previewKind("old.doc")).toBeNull();
  });

  it("isDocumentPreview 的判定不变", () => {
    expect(isDocumentPreview("document")).toBe(true);
    expect(isDocumentPreview("image")).toBe(false);
  });
});
