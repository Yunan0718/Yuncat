import { useCallback, useEffect, useState } from "react";

import { ApiError } from "./useApi";

interface UploadOptions {
  api: <T>(path: string, init?: RequestInit) => Promise<T>;
  refresh: () => void;
  t: (key: string, opts?: Record<string, unknown>) => string;
  toast: (message: string, type?: "info" | "success" | "error") => void;
  /** 体积格式化（FileTree 已有 fmtSize，复用同一实现避免两套显示口径） */
  formatSize: (bytes: number) => string;
}

/**
 * 工作区文件上传（2026-09-15 抽自 FileTree）。
 *
 * 三个决定：
 *  1. **一个文件一个请求、body 为原始字节**——后端不依赖 python-multipart（见
 *     routes/workspace.py 的说明），前端也不需要拼 FormData；
 *  2. 上限来自后端 `/api/v1/capabilities`，前端只做预检提示，不复制这个数字；
 *  3. 逐文件报错：某个文件失败不影响其余文件，提示里带上文件名，便于用户定位。
 */
export function useWorkspaceUpload({ api, refresh, t, toast, formatSize }: UploadOptions) {
  const [maxUploadBytes, setMaxUploadBytes] = useState<number | null>(null);

  useEffect(() => {
    api<{ limits?: { max_upload_bytes?: number } }>("/api/v1/capabilities")
      .then((c) => setMaxUploadBytes(c?.limits?.max_upload_bytes ?? null))
      .catch((e) => console.warn("upload: limits load failed:", e));
  }, [api]);

  const handleUpload = useCallback(
    async (files: FileList | null, targetDir: string = "shared") => {
      if (!files || files.length === 0) return;
      let uploaded = 0;
      for (const file of Array.from(files)) {
        if (maxUploadBytes !== null && file.size > maxUploadBytes) {
          toast(
            t("fileTree.uploadTooLarge", { name: file.name, limit: formatSize(maxUploadBytes) }),
            "error",
          );
          continue;
        }
        try {
          await api(
            `/api/v1/workspace/files/upload?dir=${encodeURIComponent(targetDir)}` +
              `&filename=${encodeURIComponent(file.name)}`,
            { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file },
          );
          uploaded += 1;
        } catch (e) {
          const msg = e instanceof ApiError ? e.message : t("fileTree.uploadFailed");
          toast(`${file.name}: ${msg}`, "error");
        }
      }
      if (uploaded > 0) refresh();
    },
    [api, maxUploadBytes, refresh, t, toast, formatSize],
  );

  return { handleUpload };
}
