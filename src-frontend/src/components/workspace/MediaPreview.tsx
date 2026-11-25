import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

/** 可内联预览的媒体类型（与 lib/workspaceTabs.previewKind 对应）。 */
export type MediaKind = "image" | "pdf" | "audio" | "video" | "html";

/**
 * 工作区媒体预览（2026-09-15）。
 *
 * 统一走「带鉴权 fetch → blob URL」：`<img>`/`<video>`/`<iframe>` 都无法携带
 * Authorization 头，而把 token 放进 URL 会落进后端访问日志，所以 URL 只带路径。
 * HTML 预览用 `sandbox=""` 的空沙箱 iframe —— 脚本、表单、同源访问全部禁用，
 * 只当渲染视图看（源码编辑仍在 Monaco 里）。
 * 卸载时 revokeObjectURL，避免长期占内存。
 */
export function MediaPreview({
  apiBlob,
  path,
  name,
  kind,
}: {
  apiBlob: (path: string, options?: RequestInit) => Promise<Blob>;
  path: string;
  name: string;
  kind: MediaKind;
}) {
  const { t } = useTranslation();
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let objectUrl: string | null = null;
    let cancelled = false;
    setUrl(null);
    setFailed(false);
    apiBlob(`/api/v1/workspace/files/raw?path=${encodeURIComponent(path)}`)
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [apiBlob, path]);

  if (failed) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-1 text-sm text-muted-foreground">
        <span>{t("mediaPreview.loadFailed", { name })}</span>
        <span className="text-[11px] text-muted-foreground/80">{t("mediaPreview.openExternalHint")}</span>
      </div>
    );
  }
  if (!url) {
    return (
      <div className="flex items-center justify-center h-full text-sm text-muted-foreground">
        {t("mediaPreview.loading")}
      </div>
    );
  }

  if (kind === "image") {
    return (
      <div className="flex items-center justify-center h-full overflow-auto p-4 bg-secondary/20">
        <img src={url} alt={name} className="max-w-full max-h-full object-contain" />
      </div>
    );
  }
  if (kind === "video") {
    return (
      <div className="flex items-center justify-center h-full overflow-auto p-4 bg-black/80">
        <video src={url} controls className="max-w-full max-h-full" />
      </div>
    );
  }
  if (kind === "audio") {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-3">
        <span className="text-sm text-muted-foreground">{name}</span>
        <audio src={url} controls className="w-[min(520px,80%)]" />
      </div>
    );
  }
  // pdf / html：交给浏览器内置查看器；html 用空 sandbox（禁脚本/表单/同源访问）
  return (
    <iframe
      src={url}
      title={name}
      sandbox={kind === "html" ? "" : undefined}
      className="w-full h-full border-0 bg-white"
    />
  );
}
