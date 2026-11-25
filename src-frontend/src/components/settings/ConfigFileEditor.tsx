import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useApi } from "../../hooks/useApi";
import { FileText, Save } from "lucide-react";
import { Button } from "../ui/button";

export function ConfigFileEditor() {
  const { t } = useTranslation();
  const { api } = useApi();
  const [files, setFiles] = useState<Array<{ filename: string; provider: string | null; is_index: boolean }>>([]);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const [original, setOriginal] = useState("");
  const [status, setStatus] = useState("");

  useEffect(() => {
    api<typeof files>("/api/v1/models/config/files")
      .then(setFiles)
      .catch((e) => console.warn("SettingsPage: config files load failed:", e));
  }, [api]);

  const openFile = async (filename: string) => {
    try {
      const data = await api<{ filename: string; content: string }>(
        "/api/v1/models/config/files/" + encodeURIComponent(filename),
      );
      setSelectedFile(filename);
      setContent(data.content);
      setOriginal(data.content);
    } catch {
      setStatus(t("settings.readFailed"));
    }
  };
  const saveFile = async () => {
    if (!selectedFile) return;
    try {
      await api("/api/v1/models/config/files/" + encodeURIComponent(selectedFile), {
        method: "PUT",
        body: JSON.stringify({ content }),
      });
      setOriginal(content);
      setStatus(t("common.saved"));
    } catch (e: unknown) {
      setStatus(t("settings.saveFailedPrefix") + (e instanceof Error ? e.message : String(e)));
    }
  };

  return (
    <div className="space-y-3">
      {status && (
        <div
          className={`text-xs px-3 py-1.5 rounded ${status.includes(t("settings.saveFailedPrefix").trim()) ? "bg-destructive/10 text-destructive" : "bg-success/10 text-success"}`}
        >
          {status}
        </div>
      )}
      {!selectedFile ? (
        <div className="space-y-1">
          <p className="text-xs text-muted-foreground mb-2">{t("settings.clickToEdit")}</p>
          {files.map((f) => (
            <button
              key={f.filename}
              onClick={() => openFile(f.filename)}
              className="w-full flex items-center gap-2 px-3 py-2 border rounded-lg hover:bg-secondary transition-colors text-left"
            >
              <FileText size={14} className="text-muted-foreground shrink-0" />
              <code className="text-xs font-mono flex-1 truncate">{f.filename}</code>
              <span className="text-[10px] text-muted-foreground">
                {f.provider || t("settings.indexFile")}
                {f.is_index ? t("settings.activeManage") : ""}
              </span>
            </button>
          ))}
        </div>
      ) : (
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <Button
              variant="link"
              size="sm"
              onClick={() => {
                setSelectedFile(null);
                setContent("");
              }}
              className="text-xs h-auto p-0"
            >
              ← {t("settings.back")}
            </Button>
            <code className="text-xs font-mono text-muted-foreground">{selectedFile}</code>
            <div className="flex-1" />
            {content !== original && (
              <Button size="sm" onClick={saveFile} className="h-7 text-xs gap-1">
                <Save size={12} />
                {t("common.save")}
              </Button>
            )}
          </div>
          <textarea
            value={content}
            onChange={(e) => setContent(e.target.value)}
            className="w-full border rounded-lg p-3 text-xs font-mono bg-background min-h-[300px] resize-y"
            style={{ lineHeight: 1.6 }}
          />
        </div>
      )}
    </div>
  );
}
