import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useToast } from "../../hooks/use-toast";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Label } from "../ui/label";
import { Switch } from "../ui/switch";

/**
 * Phase 96: 托盘行为卡片——控制关闭窗口时是直接退出还是隐藏到系统托盘。
 *
 * 默认关闭（直接退出），避免新安装用户误以为"关闭窗口=退出"但实际仍在托盘运行，
 * 也避免安装器升级时检测到"隐藏的后台实例"而无法关闭。
 */
export function TrayBehaviorCard() {
  const { t } = useTranslation();
  const { toast } = useToast();
  const [minimizeToTray, setMinimizeToTray] = useState(false);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    window.yuncat
      ?.getPreference?.("minimizeToTray")
      .then((res) => {
        if (cancelled) return;
        if (res?.ok) setMinimizeToTray(!!res.value);
      })
      .catch(() => {
        /* 非 Electron 环境或 IPC 失败——保持默认 false */
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    const unlisten = window.yuncat?.onPreferenceChanged?.((evt) => {
      if (evt?.key === "minimizeToTray") setMinimizeToTray(!!evt.value);
    });
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, []);

  const handleToggle = async (v: boolean) => {
    setMinimizeToTray(v);
    try {
      const res = await window.yuncat?.setPreference?.("minimizeToTray", v);
      if (res?.ok) {
        setMinimizeToTray(!!res.value);
        toast(t(v ? "trayBehavior.enabled" : "trayBehavior.disabled"), "success");
      } else {
        setMinimizeToTray(!v);
        toast(t("trayBehavior.failed"), "error");
      }
    } catch {
      setMinimizeToTray(!v);
      toast(t("trayBehavior.failed"), "error");
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t("trayBehavior.title")}</CardTitle>
        <CardDescription>{t("trayBehavior.desc")}</CardDescription>
      </CardHeader>
      <CardContent>
        <div className="flex items-center justify-between">
          <div>
            <Label className="text-sm font-medium">{t("trayBehavior.minimizeToTray")}</Label>
            <p className="text-xs text-muted-foreground mt-0.5">{t("trayBehavior.minimizeToTrayDesc")}</p>
          </div>
          <Switch checked={minimizeToTray} disabled={loading} onCheckedChange={handleToggle} />
        </div>
      </CardContent>
    </Card>
  );
}
