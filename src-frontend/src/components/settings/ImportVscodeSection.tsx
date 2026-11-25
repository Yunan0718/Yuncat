import { useTranslation } from "react-i18next";
import { useToast } from "../../hooks/use-toast";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { FileJson, Upload } from "lucide-react";

export function ImportVscodeSection({ fc }: { fc: (path: string, opts?: RequestInit) => Promise<Response> }) {
  const { t } = useTranslation();
  const { toast } = useToast();
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <FileJson size={16} />
          {t("settings.importVscodeTitle")}
        </CardTitle>
        <CardDescription>{t("settings.importVscodeDesc")}</CardDescription>
      </CardHeader>
      <CardContent className="flex gap-2 flex-wrap items-center">
        <label className="cursor-pointer inline-flex items-center justify-center rounded-md border text-xs font-medium h-8 px-3 hover:bg-secondary transition-colors gap-1">
          <input
            type="file"
            accept=".json"
            className="hidden"
            onChange={async (e) => {
              const f = e.target.files?.[0];
              if (!f) return;
              try {
                const text = await f.text();
                const d = JSON.parse(text);
                const r = await fc("/api/v1/config/import-vscode", {
                  method: "POST",
                  body: JSON.stringify({ settings: d }),
                });
                const rd = await r.json();
                if (rd.ok) {
                  const keys = Object.keys(rd.imported || {});
                  toast(t("settings.importVscodeSuccess", { count: keys.length }), "success");
                } else {
                  toast(t("settings.importVscodeFailed", { error: rd.error }), "error");
                }
              } catch {
                toast(t("settings.invalidFile"), "error");
              }
            }}
          />
          <Upload size={14} />
          {t("settings.importVscodeButton")}
        </label>
      </CardContent>
    </Card>
  );
}
