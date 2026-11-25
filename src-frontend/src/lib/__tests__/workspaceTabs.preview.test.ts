import { describe, expect, it } from "vitest";

import {
  isDocumentPreview,
  isLegacyOfficeFile,
  previewKind,
  supportsRenderedPreview,
  supportsTablePreview,
} from "../workspaceTabs";

/**
 * 预览格式分流：文本 / 媒体 / 文档三类各去哪条路。
 * 错一格的后果是「双击文件什么都不显示」——所以这里把边界钉住。
 */
describe("previewKind", () => {
  it("媒体格式仍走原生渲染", () => {
    expect(previewKind("a.PNG")).toBe("image");
    expect(previewKind("doc/a.pdf")).toBe("pdf");
    expect(previewKind("a.mp3")).toBe("audio");
    expect(previewKind("a.mp4")).toBe("video");
  });

  it("Office 格式进入文档预览（大小写不敏感）", () => {
    expect(previewKind("report.docx")).toBe("document");
    expect(previewKind("REPORT.DOCX")).toBe("document");
    expect(previewKind("book.xlsx")).toBe("spreadsheet");
    expect(previewKind("book.xlsm")).toBe("spreadsheet");
    expect(previewKind("deck.pptx")).toBe("slides");
  });

  it("旧版二进制 Office 不假装能预览", () => {
    expect(previewKind("old.doc")).toBeNull();
    expect(previewKind("old.xls")).toBeNull();
    expect(isLegacyOfficeFile("old.doc")).toBe(true);
    expect(isLegacyOfficeFile("old.xls")).toBe(true);
    expect(isLegacyOfficeFile("old.ppt")).toBe(true);
    expect(isLegacyOfficeFile("new.docx")).toBe(false);
  });

  it("纯文本格式仍按文本处理", () => {
    expect(previewKind("a.ts")).toBeNull();
    expect(supportsRenderedPreview("README.md")).toBe(true);
    expect(supportsRenderedPreview("index.html")).toBe(true);
  });
});

describe("isDocumentPreview", () => {
  it("只有文档类返回 true", () => {
    expect(isDocumentPreview("document")).toBe(true);
    expect(isDocumentPreview("spreadsheet")).toBe(true);
    expect(isDocumentPreview("slides")).toBe(true);
    expect(isDocumentPreview("image")).toBe(false);
    expect(isDocumentPreview("pdf")).toBe(false);
  });
});

describe("supportsTablePreview", () => {
  it("CSV / TSV 可以切表格视图，但仍是可编辑文本", () => {
    expect(supportsTablePreview("data.csv")).toBe(true);
    expect(supportsTablePreview("data.tsv")).toBe(true);
    expect(supportsTablePreview("data.tab")).toBe(true);
    expect(supportsTablePreview("data.json")).toBe(false);
  });
});
