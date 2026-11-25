import { useEffect, useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { useApi } from "../hooks/useApi";

interface DepInfo {
  installed: boolean;
  version: string | null;
  path: string | null;
}

const GIT_MIRRORS = [
  { label: "npmmirror", url: "https://npmmirror.com/mirrors/git-for-windows/" },
  { label: "tencent", url: "https://mirrors.cloud.tencent.com/git-for-windows/" },
  { label: "official", url: "https://git-scm.com/downloads" },
];

/** Phase 18: 依赖与环境检测面板——Python/Node.js 已内置，仅 Git 需用户安装 */
export function DependenciesPanel() {
  const { t } = useTranslation();
  const { api } = useApi();
  const [deps, setDeps] = useState<Record<string, DepInfo>>({});
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setDeps(await api<Record<string, DepInfo>>("/api/v1/system/dependencies"));
      setError("");
    } catch {
      setError(t("phase49.dependencies.checkFailed"));
    }
  }, [api, t]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">{t("phase49.dependencies.intro")}</p>
      {error && <div className="text-xs text-destructive">{error}</div>}

      {/* Python —— 内置 */}
      <div className="border rounded p-3 bg-success/5">
        <div className="flex items-center gap-2 mb-1">
          <span className="text-sm font-semibold">Python</span>
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-success/10 text-success">
            {t("phase49.dependencies.bundled")}
          </span>
          {deps.python?.version && (
            <span className="text-[10px] text-muted-foreground font-mono">{deps.python.version}</span>
          )}
        </div>
        <p className="text-xs text-muted-foreground">{t("phase49.dependencies.pythonDesc")}</p>
      </div>

      {/* Node.js —— 内置 */}
      <div className="border rounded p-3 bg-success/5">
        <div className="flex items-center gap-2 mb-1">
          <span className="text-sm font-semibold">Node.js</span>
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-success/10 text-success">
            {t("phase49.dependencies.bundled")}
          </span>
          {deps.node?.version && (
            <span className="text-[10px] text-muted-foreground font-mono">{deps.node.version}</span>
          )}
        </div>
        <p className="text-xs text-muted-foreground">{t("phase49.dependencies.nodeDesc")}</p>
      </div>

      {/* Git —— 可选 */}
      <div className="border rounded p-3">
        <div className="flex items-center gap-2 mb-1">
          <span className="text-sm font-semibold">{t("phase49.dependencies.git")}</span>
          {deps.git?.installed ? (
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-success/10 text-success">
              {deps.git.version || t("phase49.dependencies.installed")}
            </span>
          ) : (
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-muted text-foreground-secondary">
              {deps.git ? t("phase49.dependencies.notInstalled") : t("phase49.dependencies.detecting")}
            </span>
          )}
        </div>
        <p className="text-xs text-muted-foreground mb-1">{t("phase49.dependencies.gitDesc")}</p>
        {deps.git && !deps.git.installed && (
          <div className="space-y-0.5 mt-1.5">
            <span className="text-[10px] text-muted-foreground">{t("phase49.dependencies.mirrors")}</span>
            <div className="flex flex-wrap gap-1">
              {GIT_MIRRORS.map((m) => (
                <a
                  key={m.url}
                  href={m.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-[10px] px-2 py-0.5 border rounded hover:bg-secondary"
                >
                  {t(`phase49.dependencies.mirror.${m.label}`)} →
                </a>
              ))}
            </div>
          </div>
        )}
      </div>

      <button onClick={load} className="text-xs text-muted-foreground hover:text-foreground border rounded px-2 py-1">
        {t("phase49.dependencies.retest")}
      </button>
    </div>
  );
}
