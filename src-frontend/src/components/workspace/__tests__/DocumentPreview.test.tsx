import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

import type { SheetView } from "../../../lib/documentPreview";

const mockDocxHtml = vi.fn(async (_blob: Blob) => "<p>docx 正文</p>");
const mockSheetsFromDelimited = vi.fn((_text: string, _name: string): SheetView[] => []);
const mockWorkbookSheets = vi.fn(async (_blob: Blob): Promise<SheetView[]> => []);
const mockCreatePptx = vi.fn(async (_host: HTMLElement, _w: number, _h: number) => ({
  preview: vi.fn(),
}));

vi.mock("../../../lib/documentPreview", () => ({
  loadDocxHtml: (blob: Blob) => mockDocxHtml(blob),
  loadWorkbookSheets: (blob: Blob) => mockWorkbookSheets(blob),
  sheetsFromDelimited: (text: string, name: string) => mockSheetsFromDelimited(text, name),
  createPptxPreviewer: (host: HTMLElement, width: number, height: number) =>
    mockCreatePptx(host, width, height),
}));

import { DocumentPreview } from "../DocumentPreview";

const sheet = {
  name: "Sheet1",
  rows: [["名称", "数量"], ["苹果", "3"]],
  rowCount: 2,
  columnCount: 2,
  truncated: false,
};

const fakeBlob = { arrayBuffer: async () => new ArrayBuffer(8) } as unknown as Blob;

describe("DocumentPreview", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockSheetsFromDelimited.mockReturnValue([]);
    mockWorkbookSheets.mockResolvedValue([]);
  });

  it("docx：转换出的 HTML 渲染到容器里", async () => {
    const { container } = render(
      <DocumentPreview apiBlob={async () => fakeBlob} path="a.docx" name="a.docx" kind="document" />,
    );
    expect(await screen.findByText("docx 正文")).toBeInTheDocument();
    expect(container.querySelector(".docx-body")?.innerHTML).toContain("docx 正文");
  });

  it("xlsx：多工作表时显示标签页，单元格按原值展示", async () => {
    mockWorkbookSheets.mockResolvedValue([sheet, { ...sheet, name: "Sheet2" }]);
    render(<DocumentPreview apiBlob={async () => fakeBlob} path="a.xlsx" name="a.xlsx" kind="spreadsheet" />);
    expect(await screen.findByText("苹果")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sheet2" })).toBeInTheDocument();
  });

  it("CSV 走文本通道：不再请求二进制", async () => {
    mockSheetsFromDelimited.mockReturnValue([sheet]);
    const apiBlob = vi.fn(async () => fakeBlob);
    render(
      <DocumentPreview apiBlob={apiBlob} path="a.csv" name="a.csv" kind="spreadsheet" text="名称,数量" />,
    );
    expect(await screen.findByText("苹果")).toBeInTheDocument();
    expect(apiBlob).not.toHaveBeenCalled();
  });

  it("截断时明确告知还有多少行没显示", async () => {
    mockSheetsFromDelimited.mockReturnValue([{ ...sheet, rowCount: 900, truncated: true }]);
    render(<DocumentPreview path="a.csv" name="a.csv" kind="spreadsheet" text="x" />);
    // i18n 在测试环境里被 mock 成「返回 key 本身」，所以断言的是「截断提示确实渲染出来了」；
    // 插值数字由 lib/__tests__/documentPreview.test.ts 的 trimTable 用例负责。
    expect(await screen.findByText("documentPreview.truncated")).toBeInTheDocument();
  });

  it("pptx：把容器交给预览器，并把二进制传给它", async () => {
    const preview = vi.fn();
    mockCreatePptx.mockResolvedValue({ preview });
    render(<DocumentPreview apiBlob={async () => fakeBlob} path="a.pptx" name="a.pptx" kind="slides" />);
    const host = await screen.findByTestId("pptx-host");
    expect(mockCreatePptx).toHaveBeenCalledWith(host, 960, 540);
    expect(preview).toHaveBeenCalled();
  });

  it("解析失败时说清楚原因，而不是留白屏", async () => {
    mockDocxHtml.mockRejectedValueOnce(new Error("不是有效的 docx"));
    render(<DocumentPreview apiBlob={async () => fakeBlob} path="bad.docx" name="bad.docx" kind="document" />);
    expect(await screen.findByText(/不是有效的 docx/)).toBeInTheDocument();
    expect(screen.getByText(/documentPreview.loadFailed/)).toBeInTheDocument();
  });

  it("缺少文件读取接口时也走失败态", async () => {
    render(<DocumentPreview path="a.xlsx" name="a.xlsx" kind="spreadsheet" />);
    expect(await screen.findByText(/documentPreview.loadFailed/)).toBeInTheDocument();
  });
});
