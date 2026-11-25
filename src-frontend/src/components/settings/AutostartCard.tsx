import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useToast } from "../../hooks/use-toast";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Label } from "../ui/label";
import { Switch } from "../ui/switch";

/**
 * Phase 95: 开机自启卡片——读取/写入系统登录项，与托盘菜单勾选状态双向同步。
 *
 * 数据流：
 * - 启动时调 getAutostart IPC 读取系统登录项状态
 * - 用户切换 Switch → setAutostart IPC 写入系统登录项 → 乐观更新 + 失败回滚
 * - 用户在托盘切换 → onAutostartChanged 事件 → 同步 Switch 状态
 */
export function AutostartCard() {
  const { t } = useTranslation();
  const { toast } = useToast();
  const [autostart, setAutostart] = useState(false);
  const [loading, setLoading] = useState(true);

  // 启动时查询当前系统登录项状态 + 监听托盘勾选变化
  useEffect(() => {
    let cancelled = false;
    window.yuncat
      ?.getAutostart?.()
      .then((res) => {
        if (cancelled) return;
        if (res?.ok) setAutostart(!!res.enabled);
      })
      .catch(() => {
        /* 非 Electron 环境或 IPC 失败——保持默认 false */
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    // 用户在托盘切换开机自启时同步 UI
    const unlisten = window.yuncat?.onAutostartChanged?.((enabled) => {
      setAutostart(!!enabled);
    });
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, []);

  const handleToggle = async (v: boolean) => {
    setAutostart(v); // 乐观更新
    try {
      const res = await window.yuncat?.setAutostart?.(v);
      if (res?.ok) {
        setAutostart(!!res.enabled); // 以系统实际状态为准
        toast(t(v ? "autostart.enabled" : "autostart.disabled"), "success");
      } else {
        setAutostart(!v); // 回滚
        toast(t("autostart.failed"), "error");
      }
    } catch {
      setAutostart(!v); // 回滚
      toast(t("autostart.failed"), "error");
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t("autostart.title")}</CardTitle>
        <CardDescription>{t("autostart.desc")}</CardDescription>
      </CardHeader>
      <CardContent>
        <div className="flex items-center justify-between">
          <div>
            <Label className="text-sm font-medium">{t("autostart.openAtLogin")}</Label>
            <p className="text-xs text-muted-foreground mt-0.5">{t("autostart.openAtLoginDesc")}</p>
          </div>
          <Switch checked={autostart} disabled={loading} onCheckedChange={handleToggle} />
        </div>
      </CardContent>
    </Card>
  );
}
