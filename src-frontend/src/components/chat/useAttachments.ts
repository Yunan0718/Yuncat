import { useState } from "react";
import type { ChatAttachment } from "./types";

/**
 * P0-7: 附件管理 hook——从 ChatView.tsx 抽取（候选 13）。
 * 自包含：附件预览状态 + 拖拽/粘贴/手动添加回调。
 */
export interface UseAttachmentsParams {
  setInput: React.Dispatch<React.SetStateAction<string>>;
  setStatusMsg: (s: string) => void;
  t: (key: string, opts?: Record<string, unknown>) => string;
}

export function useAttachments(p: UseAttachmentsParams) {
  const [attachmentPreviews, setAttachmentPreviews] = useState<ChatAttachment[]>([]);

  const addAtt = (f: File, isImg?: boolean) => {
    const r = new FileReader();
    r.onload = () => {
      const b64 = (r.result as string).split(",")[1] || "";
      setAttachmentPreviews((prev) => [...prev, { name: f.name, data: b64, type: f.type }]);
      if (isImg) p.setInput((v) => v + `\n${p.t("chat.imageMarker", { name: f.name })}`);
    };
    r.onerror = () => p.setStatusMsg(p.t("chat.fileReadError", { name: f.name }));
    r.readAsDataURL(f);
  };

  const dropHandler = (e: React.DragEvent) => {
    e.preventDefault();
    Array.from(e.dataTransfer.files).forEach((f) => addAtt(f, f.type.startsWith("image/")));
  };

  const pasteHandler = (e: React.ClipboardEvent) => {
    const files = e.clipboardData?.files;
    if (files && files.length > 0) {
      e.preventDefault();
      Array.from(files).forEach((f) => addAtt(f, f.type.startsWith("image/")));
    }
  };

  return { attachmentPreviews, setAttachmentPreviews, addAtt, dropHandler, pasteHandler };
}
