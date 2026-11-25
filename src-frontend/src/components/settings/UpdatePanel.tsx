import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useToast } from "../../hooks/use-toast";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Button } from "../ui/button";
import { Download, RefreshCw, ShieldCheck, AlertTriangle, CheckCircle } from "lucide-react";

/**
 * Phase 94: 更新包校验——纯函数检测 electron-updater 校验失败错误。
 *
 * electron-updater 在 sha512/blockmap 校验失败时抛出含以下关键字的 error：
 * sha512 / checksum / signature / hash mismatch / integrity。
 * main.js 同样用此正则检测并弹原生错误框 + 转发 update-error 事件。
 */
const _VERIFICATION_ERROR_RE = /sha512|checksum|signature|hash mismatch|integrity/i;
export function isVerificationError(message: string): boolean {
  return typeof message === "string" ? _VERIFICATION_ERROR_RE.test(message) : false;
}

type UpdateStatus =
  | "idle"
  | "checking"
  | "available"
  | "no-update"
  | "downloading"
  | "downloaded"
  | "error";

interface UpdateAvailableInfo {
  version?: string;
}

/**
 * Phase 94: UpdatePanel——检查更新 / 下载 / 安装状态机。
 *
 * 数据流：
 * - 用户点"检查更新" → checkForUpdates IPC → 等待 update-available/update-not-available/update-error 事件
 * - 用户点"下载并安装" → downloadUpdate IPC → 等待 download-progress/update-downloaded/update-error 事件
 * - 用户点"立即重启安装" → installUpdate IPC → quitAndInstall
 * - 校验失败（isVerificationError=true）显示专门文案，拒绝安装
 */
export function UpdatePanel() {
  const { t } = useTranslation();
  const { toast } = useToast();
  const [status, setStatus] = useState<UpdateStatus>("idle");
  const [version, setVersion] = useState("");
  const [percent, setPercent] = useState(0);
  const [errorMsg, setErrorMsg] = useState("");
  const [isVerify, setIsVerify] = useState(false);

  // 挂载时注册 5 个事件监听器
  useEffect(() => {
    const unlistenAvailable = window.yuncat?.onUpdateAvailable?.((info) => {
      const v = (info as UpdateAvailableInfo)?.version || "";
      setVersion(v);
      setStatus("available");
    });
    const unlistenNotAvailable = window.yuncat?.onUpdateNotAvailable?.(() => {
      setStatus("no-update");
    });
    const unlistenDownloaded = window.yuncat?.onUpdateDownloaded?.(() => {
      setStatus("downloaded");
    });
    const unlistenError = window.yuncat?.onUpdateError?.((data) => {
      setErrorMsg(data.message);
      setIsVerify(data.isVerificationError);
      setStatus("error");
    });
    const unlistenProgress = window.yuncat?.onDownloadProgress?.((data) => {
      setPercent(data.percent);
    });
    return () => {
      unlistenAvailable?.();
      unlistenNotAvailable?.();
      unlistenDownloaded?.();
      unlistenError?.();
      unlistenProgress?.();
    };
  }, []);

  const handleCheck = async () => {
    setStatus("checking");
    setPercent(0);
    setErrorMsg("");
    setIsVerify(false);
    try {
      const res = await window.yuncat?.checkForUpdates?.();
      if (res && !res.ok) {
        if (res.error === "not packaged") {
          setStatus("idle");
          toast(t("update.notPackaged"), "error");
        } else {
          setErrorMsg(res.error || "unknown");
          setIsVerify(isVerificationError(res.error || ""));
          setStatus("error");
        }
      }
      // ok:true 时等待 update-available / update-not-available 事件
    } catch (e) {
      setErrorMsg(e instanceof Error ? e.message : String(e));
      setIsVerify(false);
      setStatus("error");
    }
  };

  const handleDownload = async () => {
    setStatus("downloading");
    setPercent(0);
    setErrorMsg("");
    setIsVerify(false);
    try {
      const res = await window.yuncat?.downloadUpdate?.();
      if (res && !res.ok) {
        setErrorMsg(res.error || "unknown");
        setIsVerify(isVerificationError(res.error || ""));
        setStatus("error");
      }
      // ok:true 时等待 download-progress / update-downloaded / update-error 事件
    } catch (e) {
      setErrorMsg(e instanceof Error ? e.message : String(e));
      setIsVerify(false);
      setStatus("error");
    }
  };

  const handleInstall = async () => {
    try {
      await window.yuncat?.installUpdate?.();
    } catch {
      /* quitAndInstall 失败忽略——进程即将退出 */
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <ShieldCheck size={16} />
          {t("update.title")}
        </CardTitle>
        <CardDescription>{t("update.desc")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {status === "idle" && (
          <Button size="sm" onClick={handleCheck} className="gap-1">
            <RefreshCw size={14} />
            {t("update.checkNow")}
          </Button>
        )}

        {status === "checking" && (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <RefreshCw size={14} className="animate-spin" />
            {t("update.checking")}
          </div>
        )}

        {status === "no-update" && (
          <div className="flex items-center gap-2 text-sm text-success">
            <CheckCircle size={14} />
            {t("update.noUpdate")}
          </div>
        )}

        {status === "available" && (
          <div className="space-y-2">
            <div className="flex items-center gap-2 text-sm">
              <Download size={14} className="text-primary" />
              <span>{t("update.available")}</span>
              {version && (
                <span className="text-xs font-mono text-muted-foreground">v{version}</span>
              )}
            </div>
            <Button size="sm" onClick={handleDownload} className="gap-1">
              <Download size={14} />
              {t("update.downloadInstall")}
            </Button>
          </div>
        )}

        {status === "downloading" && (
          <div className="space-y-2">
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Download size={14} className="animate-pulse" />
              {t("update.downloading")}
              <span className="text-xs font-mono">{percent}%</span>
            </div>
            <div className="w-full h-1.5 bg-secondary rounded-full overflow-hidden">
              <div
                className="h-full bg-primary transition-all"
                style={{ width: `${Math.min(100, Math.max(0, percent))}%` }}
              />
            </div>
          </div>
        )}

        {status === "downloaded" && (
          <div className="space-y-2">
            <div className="flex items-center gap-2 text-sm text-success">
              <CheckCircle size={14} />
              {t("update.downloaded")}
            </div>
            <Button size="sm" onClick={handleInstall} className="gap-1">
              <RefreshCw size={14} />
              {t("update.restartInstall")}
            </Button>
          </div>
        )}

        {status === "error" && (
          <div className="space-y-2">
            {isVerify ? (
              <div className="space-y-1">
                <div className="flex items-center gap-2 text-sm text-destructive">
                  <AlertTriangle size={14} />
                  {t("update.verifyFailed")}
                </div>
                <p className="text-xs text-muted-foreground">{t("update.verifyFailedDesc")}</p>
              </div>
            ) : (
              <div className="space-y-1">
                <div className="flex items-center gap-2 text-sm text-destructive">
                  <AlertTriangle size={14} />
                  {t("update.error")}
                </div>
                {errorMsg && <p className="text-xs text-muted-foreground font-mono">{errorMsg}</p>}
              </div>
            )}
            <Button size="sm" variant="outline" onClick={handleCheck} className="gap-1">
              <RefreshCw size={14} />
              {t("update.checkNow")}
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
