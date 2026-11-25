import { useTranslation } from "react-i18next";
import { useAppStore } from "../../store/appStore";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Label } from "../ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../ui/select";
import { Link } from "lucide-react";

/* ── Appearance ─────────────────────────────────── */
export function AppearanceSection() {
  const { t } = useTranslation();
  const uiDensity = useAppStore((s) => s.uiDensity);
  const uiFontSize = useAppStore((s) => s.uiFontSize);
  const accentHue = useAppStore((s) => s.accentHue);
  const linkOpenBehavior = useAppStore((s) => s.linkOpenBehavior);
  const setUiDensity = useAppStore((s) => s.setUiDensity);
  const setUiFontSize = useAppStore((s) => s.setUiFontSize);
  const setAccentHue = useAppStore((s) => s.setAccentHue);
  const setLinkOpenBehavior = useAppStore((s) => s.setLinkOpenBehavior);

  const accentOptions: { hue: 210 | 140 | 270 | 25; label: string; cls: string }[] = [
    { hue: 210, label: t("settings.accentBlue"), cls: "bg-[#5B63D3]" },
    { hue: 140, label: t("settings.accentGreen"), cls: "bg-[#1EAA5B]" },
    { hue: 270, label: t("settings.accentPurple"), cls: "bg-[#9F7AEA]" },
    { hue: 25, label: t("settings.accentOrange"), cls: "bg-[#ED8936]" },
  ];

  return (
    <div className="space-y-8">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("settings.appearance")}</CardTitle>
          <CardDescription>{t("settings.appearanceDesc")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="space-y-1.5">
            <Label>{t("settings.accentColor")}</Label>
            <div className="flex flex-wrap gap-3">
              {accentOptions.map((opt) => (
                <button
                  key={opt.hue}
                  onClick={() => setAccentHue(opt.hue)}
                  className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-sm transition-all ${
                    accentHue === opt.hue
                      ? "border-primary bg-primary/5 text-primary"
                      : "border-border hover:bg-secondary"
                  }`}
                  aria-pressed={accentHue === opt.hue}
                >
                  <span className={`w-4 h-4 rounded-full ${opt.cls}`} />
                  {opt.label}
                </button>
              ))}
            </div>
          </div>
          <div className="space-y-1.5">
            <Label>{t("settings.uiDensity")}</Label>
            <Select value={uiDensity} onValueChange={(v) => setUiDensity(v as "compact" | "comfortable")}>
              <SelectTrigger className="w-[180px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="compact">{t("settings.densityCompact")}</SelectItem>
                <SelectItem value="comfortable">{t("settings.densityComfortable")}</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <Label>{t("settings.fontSize")}</Label>
              <span className="text-xs text-muted-foreground font-mono">{uiFontSize}px</span>
            </div>
            <input
              type="range"
              min={12}
              max={20}
              step={1}
              value={uiFontSize}
              onChange={(e) => setUiFontSize(Number(e.target.value))}
              className="w-full max-w-xs accent-primary"
              aria-label={t("settings.fontSize")}
            />
          </div>
          <div className="space-y-1.5">
            <Label className="flex items-center gap-1.5">
              <Link size={13} />
              {t("settings.linkOpenBehavior")}
            </Label>
            <Select value={linkOpenBehavior} onValueChange={(v) => setLinkOpenBehavior(v as "copy" | "browser")}>
              <SelectTrigger className="w-[220px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="copy">{t("settings.linkOpenCopy")}</SelectItem>
                <SelectItem value="browser">{t("settings.linkOpenBrowser")}</SelectItem>
              </SelectContent>
            </Select>
            <p className="text-[10px] text-muted-foreground">{t("settings.linkOpenBehaviorDesc")}</p>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
