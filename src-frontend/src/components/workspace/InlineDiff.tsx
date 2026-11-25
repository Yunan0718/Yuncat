import { DiffEditor } from "@monaco-editor/react";
import { useAppStore } from "../../store/appStore";
import { useTranslation } from "react-i18next";

/** Phase 15: Monaco DiffEditor 版本——原生支持逐行对比 + 右键还原 */
export function InlineDiff({
  oldContent,
  newContent,
  onAccept,
  onReject,
  title,
  filePath,
}: {
  oldContent: string;
  newContent: string;
  onAccept?: () => void;
  onReject?: () => void;
  title?: string;
  filePath?: string;
}) {
  const { t } = useTranslation();
  const theme = useAppStore((s) => s.theme);
  const resolvedTheme = theme === "dark" ? "vs-dark" : theme === "light" ? "light" : undefined;

  return (
    <div className="border rounded overflow-hidden bg-background">
      <div className="flex items-center justify-between px-3 py-2 border-b bg-secondary">
        <span className="text-xs font-semibold">{title || t("inlineDiff.defaultTitle")}</span>
        {(onAccept || onReject) && (
          <div className="flex items-center gap-2">
            {onAccept && (
              <button
                type="button"
                aria-label={t("inlineDiff.acceptAll")}
                onClick={onAccept}
                className="text-[10px] bg-success-solid text-success-foreground rounded px-2 py-0.5 font-semibold"
              >
                {t("inlineDiff.acceptAll")}
              </button>
            )}
            {onReject && (
              <button
                type="button"
                aria-label={t("tool.reject")}
                onClick={onReject}
                className="text-[10px] border border-destructive/30 text-destructive rounded px-2 py-0.5"
              >
                {t("tool.reject")}
              </button>
            )}
          </div>
        )}
      </div>
      <div className="h-80">
        <DiffEditor
          height="100%"
          language={filePath ? filePath.split(".").pop() || "text" : "text"}
          original={oldContent}
          modified={newContent}
          theme={resolvedTheme}
          options={{
            readOnly: true,
            minimap: { enabled: false },
            scrollBeyondLastLine: false,
            fontSize: 12,
            wordWrap: "on",
            renderSideBySide: true,
            originalEditable: false,
          }}
        />
      </div>
    </div>
  );
}
