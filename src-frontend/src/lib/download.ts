/** lib/download.ts —— 把 fetch 的二进制响应存成文件

两处备份下载（手动导出 / 历史备份下载）共用：
- 文件名优先取 `Content-Disposition`，没有就用调用方给的兜底名；
- 触发浏览器下载后立即 revoke object URL，避免内存泄漏。
*/

export async function downloadResponseBlob(
  res: Response,
  fallbackName: string,
): Promise<string> {
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  try {
    const cd = res.headers.get("content-disposition") || "";
    const match = cd.match(/filename="?([^"]+)"?/);
    const filename = match?.[1] || fallbackName;
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
    return filename;
  } finally {
    URL.revokeObjectURL(url);
  }
}
