import { useState } from "react";
import { useTranslation } from "react-i18next";
import { SlidersHorizontal } from "lucide-react";
import { useApi } from "../../hooks/useApi";
import { emitGlobalToast } from "../common/toast-context";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Button } from "../ui/button";

/** 运行时可调的日志级别（与后端 set_log_level 接受的取值一致）。 */
const LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] as const;

interface LogLevelResponse {
  ok: boolean;
  level: string;
  logger: string;
}

/**
 * 运行时日志级别（`POST /admin/log-level` 一直没有前端入口）。
 *
 * 排查问题时最常用的动作就是"临时开 DEBUG → 复现 → 看日志"，
 * 以前只能改代码/重启；这里直接切。留空 logger 表示 root（全局）。
 */
export function LogLevelControl() {
  const { t } = useTranslation();
  const { api } = useApi();
  const [level, setLevel] = useState<string>("INFO");
  const [loggerName, setLoggerName] = useState("");
  const [applied, setApplied] = useState<{ level: string; logger: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function apply() {
    setBusy(true);
    try {
      const res = await api<LogLevelResponse>("/api/v1/admin/log-level", {
        method: "POST",
        body: JSON.stringify({ level, logger: loggerName.trim() }),
      });
      setApplied({ level: res.level, logger: res.logger });
      emitGlobalToast(
        t("diagnostics.logLevelApplied", {
          level: res.level,
          logger: res.logger || t("diagnostics.rootLogger"),
        }),
        "success",
      );
    } catch (e) {
      emitGlobalToast(
        `${t("diagnostics.logLevelFailed")}: ${e instanceof Error ? e.message : String(e)}`,
        "error",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <SlidersHorizontal size={16} />
          {t("diagnostics.logLevelTitle")}
        </CardTitle>
        <CardDescription>{t("diagnostics.logLevelDesc")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <select
            value={level}
            onChange={(e) => setLevel(e.target.value)}
            aria-label={t("diagnostics.logLevel")}
            className="rounded border bg-background px-2 py-1 text-xs"
          >
            {LOG_LEVELS.map((lv) => (
              <option key={lv} value={lv}>{lv}</option>
            ))}
          </select>
          <input
            value={loggerName}
            onChange={(e) => setLoggerName(e.target.value)}
            aria-label={t("diagnostics.loggerName")}
            placeholder={t("diagnostics.loggerPlaceholder")}
            className="min-w-48 flex-1 rounded border bg-background px-2 py-1 text-xs font-mono"
          />
          <Button size="sm" onClick={() => void apply()} disabled={busy}>
            {t("diagnostics.apply")}
          </Button>
        </div>
        {applied && (
          <div className="text-[11px] text-muted-foreground">
            {t("diagnostics.logLevelCurrent", {
              level: applied.level,
              logger: applied.logger || t("diagnostics.rootLogger"),
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
