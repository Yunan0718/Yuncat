import { describe, it, expect, vi, beforeEach } from "vitest";
import { downloadResponseBlob } from "../download";

function makeResponse(blob: Blob, filename?: string): Response {
  const headers = new Headers();
  if (filename) headers.set("content-disposition", `attachment; filename="${filename}"`);
  return { blob: async () => blob, headers } as unknown as Response;
}

describe("downloadResponseBlob", () => {
  const createUrl = vi.fn(() => "blob:1");
  const revokeUrl = vi.fn();
  let clickSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    createUrl.mockClear();
    revokeUrl.mockClear();
    Object.defineProperty(URL, "createObjectURL", { writable: true, value: createUrl });
    Object.defineProperty(URL, "revokeObjectURL", { writable: true, value: revokeUrl });
    clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  });

  it("优先用 Content-Disposition 的文件名", async () => {
    const name = await downloadResponseBlob(makeResponse(new Blob(["x"]), "backup-1.ybk"), "fallback.ybk");
    expect(name).toBe("backup-1.ybk");
    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(revokeUrl).toHaveBeenCalledWith("blob:1");
  });

  it("没有 header 时用兜底名", async () => {
    const name = await downloadResponseBlob(makeResponse(new Blob(["x"])), "fallback.ybk");
    expect(name).toBe("fallback.ybk");
  });
});
