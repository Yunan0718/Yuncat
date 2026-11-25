import { useTranslation } from "react-i18next";
import type { PythonLspStatus } from "../../lib/monacoPythonLsp";

/**
 * Python 智能提示状态徽标（2026-09-16）。
 *
 * 为什么要有它：语言服务依赖本机 Node，缺的时候不能装作没事（用户会以为补全坏了），
 * 也不能每次都弹窗（编辑器在多个标签页里反复挂载）。折中就是——常驻一个小徽标，
 * 鼠标悬停给出具体原因，缺 Node 时直接说缺什么。
 */
const REASON_KEYS: Record<string, string> = {
  "node-missing": "pythonLsp.reasonNodeMissing",
  "npx-missing": "pythonLsp.reasonNpxMissing",
  "override-missing": "pythonLsp.reasonOverrideMissing",
  "unsupported-language": "pythonLsp.reasonUnsupported",
};

export function PythonLspBadge({ status }: { status: PythonLspStatus | null }) {
  const { t } = useTranslation();
  if (!status) return null;

  if (status.state === "preparing") {
    return (
      <span className="px-2 py-0.5 text-[10px] rounded bg-secondary text-muted-foreground">
        {t("pythonLsp.preparing")}
      </span>
    );
  }

  if (status.state === "unavailable") {
    const key = status.code ? REASON_KEYS[status.code] : undefined;
    return (
      <span
        className="px-2 py-0.5 text-[10px] rounded border border-input text-muted-foreground cursor-help"
        title={status.reason || t("pythonLsp.unavailable")}
      >
        {key ? t(key) : t("pythonLsp.unavailable")}
      </span>
    );
  }

  return (
    <span
      className="px-2 py-0.5 text-[10px] rounded bg-success/10 text-success"
      title={t("pythonLsp.readyHint")}
    >
      {t("pythonLsp.ready")}
    </span>
  );
}
