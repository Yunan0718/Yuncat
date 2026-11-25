import { useTranslation } from "react-i18next";
import { Quote, Copy, ExternalLink, Edit3 } from "lucide-react";
import type { CtxMenuState } from "./types";

/**
 * P0-7: 消息右键上下文菜单——从 ChatView.tsx 抽取（候选 3）。
 * 纯展示组件 + 4 个简单回调，状态由父组件持有。
 */
export interface MessageContextMenuProps {
  ctxMenu: CtxMenuState;
  onClose: () => void;
  onInsertRef: (seq: number) => void;
  onCopyPrompt: (prompt: string) => void;
  onQuoteWithContext: (seq: number) => void;
  onEditRegenerate: (taskId: string, prompt: string) => void;
}

export function MessageContextMenu({
  ctxMenu,
  onClose,
  onInsertRef,
  onCopyPrompt,
  onQuoteWithContext,
  onEditRegenerate,
}: MessageContextMenuProps) {
  const { t } = useTranslation();
  return (
    <div
      className="ctx-menu"
      style={{ left: ctxMenu.x, top: ctxMenu.y }}
      onClick={(e) => e.stopPropagation()}
    >
      <button
        className="ctx-menu-item"
        onClick={() => {
          if (ctxMenu.seq !== undefined) onInsertRef(ctxMenu.seq);
          onClose();
        }}
      >
        <Quote size={13} />
        {t("ctx.quote")}
        {ctxMenu.seq !== undefined && <span className="shortcut">#{ctxMenu.seq}</span>}
      </button>
      <button
        className="ctx-menu-item"
        onClick={() => {
          if (ctxMenu.prompt) onCopyPrompt(ctxMenu.prompt);
          onClose();
        }}
      >
        <Copy size={13} />
        {t("ctx.copy")}
      </button>
      {ctxMenu.seq && (
        <button
          className="ctx-menu-item"
          onClick={() => {
            onQuoteWithContext(ctxMenu.seq!);
            onClose();
          }}
        >
          <ExternalLink size={13} />
          {t("ctx.quoteWithContext")}
        </button>
      )}
      {ctxMenu.prompt && (
        <button
          className="ctx-menu-item"
          onClick={() => {
            onEditRegenerate(ctxMenu.taskId, ctxMenu.prompt);
            onClose();
          }}
        >
          <Edit3 size={13} />
          {t("phase49.editRetry.editAndRegenerate")}
        </button>
      )}
    </div>
  );
}
