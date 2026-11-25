import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useApi } from "../../hooks/useApi";
import { useToast } from "../../hooks/use-toast";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Label } from "../ui/label";
import { Input } from "../ui/input";
import { Button } from "../ui/button";

const DEFAULT_MAX_TOOL_ROUNDS = 100;
const MIN_ROUNDS = 10;
const MAX_ROUNDS = 1000;
const STEP = 10;

/* ── Agent Loop ─────────────────────────────────── */
export function AgentLoopSection() {
  const { t } = useTranslation();
  const { api } = useApi();
  const { toast } = useToast();
  const [value, setValue] = useState<number>(DEFAULT_MAX_TOOL_ROUNDS);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    api<Record<string, string>>("/api/v1/settings")
      .then((s) => {
        const raw = s.max_tool_rounds;
        if (raw) {
          const parsed = parseInt(raw, 10);
          if (!Number.isNaN(parsed)) {
            setValue(Math.min(MAX_ROUNDS, Math.max(MIN_ROUNDS, parsed)));
          }
        }
      })
      .catch((e) => console.warn("AgentLoopSection: settings load failed:", e));
  }, [api]);

  const clamp = (v: number) => Math.min(MAX_ROUNDS, Math.max(MIN_ROUNDS, v));

  const handleSave = async () => {
    setLoading(true);
    try {
      const saved = clamp(value);
      setValue(saved);
      await api("/api/v1/settings", {
        method: "POST",
        body: JSON.stringify({ key: "max_tool_rounds", value: String(saved) }),
      });
      toast(t("common.saved"), "success");
    } catch {
      toast(t("common.saveFailed"), "error");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-8">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("agentLoop.title")}</CardTitle>
          <CardDescription>{t("agentLoop.maxRoundsDesc")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <Label>{t("agentLoop.maxRounds")}</Label>
              <span className="text-xs text-muted-foreground">
                {t("agentLoop.defaultValue", { value: DEFAULT_MAX_TOOL_ROUNDS })}
              </span>
            </div>
            <div className="flex items-center gap-4">
              <input
                type="range"
                min={MIN_ROUNDS}
                max={MAX_ROUNDS}
                step={STEP}
                value={value}
                onChange={(e) => setValue(clamp(Number(e.target.value)))}
                className="flex-1 accent-primary"
                aria-label={t("agentLoop.maxRounds")}
              />
              <Input
                type="number"
                min={MIN_ROUNDS}
                max={MAX_ROUNDS}
                step={STEP}
                value={value}
                onChange={(e) => setValue(clamp(Number(e.target.value)))}
                className="w-24"
                aria-label={t("agentLoop.maxRounds")}
              />
            </div>
            <p className="text-[10px] text-muted-foreground">
              {MIN_ROUNDS} – {MAX_ROUNDS} {t("agentLoop.roundsUnit")}
            </p>
          </div>
          <div className="flex justify-end">
            <Button size="sm" onClick={handleSave} disabled={loading}>
              {loading ? t("common.saving") : t("common.save")}
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
