import { useState, useEffect } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { AlertTriangle, RotateCcw, FolderOpen, X } from "lucide-react";
import { Button } from "./ui/button";
import { FocusTrap } from "./FocusTrap";
import { useToast } from "../hooks/use-toast";

/**
 * Phase 92: 崩溃恢复对话框——启动时检测上次未处理的 crash，弹窗提示用户。
 *
 * 三按钮：
 * - 重启应用（relaunchApp——app.relaunch + app.exit）
 * - 打开 crash 目录（openCrashDir + dismissCrash）
 * - 忽略（dismissCrash——写 .crash-dismissed 标记，下次启动不弹窗）
 *
 * z-modal-top 略高于 OnboardingWizard 的 z-modal——崩溃恢复优先级高于 onboarding 引导。
 */
interface CrashInfo {
  timestamp: number;
  time: string;
  file: string;
  path: string;
  size: number;
}

export function CrashRecoveryDialog() {
  const { t } = useTranslation();
  const { toast } = useToast();
  const [crash, setCrash] = useState<CrashInfo | null>(null);

  // 启动时查询最近未处理的 crash（已 dismissed 返回 null → 不弹窗）
  useEffect(() => {
    if (!window.yuncat?.getLastCrash) return;
    let cancelled = false;
    window.yuncat
      .getLastCrash()
      .then((res) => {
        if (cancelled) return;
        if (res?.ok && res.crash) {
          setCrash(res.crash as CrashInfo);
        }
      })
      .catch(() => {
        /* ignore——查询失败不阻塞应用启动 */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!crash) return null;

  const handleRelaunch = () => {
    // relaunchApp 内部已调 dismissCrash——重启后不重复弹窗
    // P0-10: relaunch 失败需反馈——用户点"重启"无反应会困惑
    window.yuncat?.relaunchApp?.().catch(() => {
      toast(t("common.relaunchFailed"), "error");
    });
  };

  const handleOpenDir = () => {
    window.yuncat?.openCrashDir?.().catch(() => {
      /* ignore */
    });
    // 打开目录后标记已处理——避免每次启动都弹窗
    window.yuncat
      ?.dismissCrash?.()
      .then(() => setCrash(null))
      .catch(() => {
        /* ignore */
      });
  };

  const handleDismiss = () => {
    window.yuncat
      ?.dismissCrash?.()
      .then(() => setCrash(null))
      .catch(() => {
        /* ignore */
      });
  };

  const timeStr = new Date(crash.timestamp).toLocaleString();

  return createPortal(
    <FocusTrap active>
      <div
        className="fixed inset-0 bg-black/60 z-modal-top flex items-center justify-center p-4"
        role="dialog"
        aria-modal="true"
        aria-labelledby="crash-recovery-title"
      >
      <div className="bg-background rounded-2xl shadow-xl w-full max-w-[480px] overflow-hidden flex flex-col">
        <div className="px-6 pt-6 pb-2">
          <div className="flex items-center gap-2 mb-3">
            <AlertTriangle size={20} className="text-destructive shrink-0" />
            <h2 id="crash-recovery-title" className="text-lg font-bold">{t("crashRecovery.title")}</h2>
          </div>
          <p className="text-sm text-muted-foreground mb-2">{t("crashRecovery.description")}</p>
          <p className="text-xs text-muted-foreground">
            {t("crashRecovery.crashTime")}: {timeStr}
          </p>
        </div>
        <div className="px-6 py-4 flex flex-col gap-2">
          <Button onClick={handleRelaunch} className="w-full justify-start" variant="default">
            <RotateCcw size={16} className="mr-2" />
            {t("crashRecovery.relaunch")}
          </Button>
          <Button onClick={handleOpenDir} className="w-full justify-start" variant="outline">
            <FolderOpen size={16} className="mr-2" />
            {t("crashRecovery.openDir")}
          </Button>
          <Button onClick={handleDismiss} className="w-full justify-start" variant="ghost">
            <X size={16} className="mr-2" />
            {t("crashRecovery.dismiss")}
          </Button>
        </div>
      </div>
    </div>
    </FocusTrap>,
    document.body,
  );
}
