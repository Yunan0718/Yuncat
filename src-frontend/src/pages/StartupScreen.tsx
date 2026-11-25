import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useAppStore } from "../store/appStore";
import { Card, CardContent } from "../components/ui/card";
import { LoadingCurve } from "../components/common/LoadingCurve";
import { Loader2, AlertTriangle, Database, Cpu, Network, Package } from "lucide-react";

const STAGES = [
  { id: "init", labelKey: "startup.stages.init", icon: Loader2 },
  { id: "core-services", labelKey: "startup.stages.core-services", icon: Database },
  { id: "adapters", labelKey: "startup.stages.adapters", icon: Network },
  { id: "agent-service", labelKey: "startup.stages.agent-service", icon: Cpu },
  { id: "services", labelKey: "startup.stages.services", icon: Package },
  { id: "ready", labelKey: "startup.stages.ready", icon: Loader2 },
];

// Phase 49-H: 后端 YUNCAT_STAGE 细粒度阶段名 → 前端 STAGES 主要阶段归并映射
// 后端输出 11 个细粒度阶段，前端精简到 6 个主要阶段（避免进度条跳跃）
const STAGE_MERGE: Record<string, string> = {
  "init": "init",
  "db-init": "init",
  "core-services": "core-services",
  "adapters": "adapters",
  "agent-service": "agent-service",
  "event-bus": "agent-service",
  "deliberation": "agent-service",
  "loops": "agent-service",
  "scheduler": "agent-service",
  "supervisor": "agent-service",
  "services": "services",
  "ready": "ready",
};

export function StartupScreen() {
  const { t } = useTranslation();
  const backendReady = useAppStore((s) => s.backendReady);
  const backendDead = useAppStore((s) => s.backendDead);
  const port = useAppStore((s) => s.port);
  const token = useAppStore((s) => s.token);
  const [error, setError] = useState<string | null>(null);
  const [stage, setStage] = useState("init");
  const [startupMsg, setStartupMsg] = useState("");
  const [elapsed, setElapsed] = useState(0);
  const [chromadbMissing, setChromadbMissing] = useState(false);

  // Phase 97: 启动阶段计时——记录组件挂载时间 + 各 stage 进入时间（相对挂载 ms）
  const mountTimeRef = useRef(Date.now());
  const stageTimesRef = useRef<Record<string, number>>({ init: 0 });

  // 监听 stage 变化，记录首次进入时间（相对挂载时间）
  useEffect(() => {
    const ms = Date.now() - mountTimeRef.current;
    if (stageTimesRef.current[stage] === undefined) {
      stageTimesRef.current[stage] = ms;
    }
  }, [stage]);

  // Phase 97: backendReady 时上报前端启动耗时（fire-and-forget）
  useEffect(() => {
    if (!backendReady || !port || !token) return;
    const frontendMs = Date.now() - mountTimeRef.current;
    const body = {
      frontend_ms: frontendMs,
      stages: stageTimesRef.current,
      platform: typeof navigator !== "undefined" ? navigator.platform : "unknown",
    };
    // fire-and-forget——不阻塞 UI，不上报失败也不影响用户体验
    fetch(`http://127.0.0.1:${port}/api/v1/analytics/startup`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(body),
    }).catch(() => {
      // 静默失败——analytics 是可观测性数据，不影响功能
    });
  }, [backendReady, port, token]);

  // Electron IPC 启动事件监听，只注册一次

  useEffect(() => {
    const u1 = window.yuncat?.onBackendError?.((msg: string) => setError(msg));
    const u2 = window.yuncat?.onBackendStarting?.((msg: string) => {
      // Phase 49-H: 后端 YUNCAT_STAGE 信号——归并到前端主要阶段
      // msg 可能是细粒度 stage 名（如 "deliberation"）或 heartbeat（"spawning"/"waiting"）
      if (msg !== "spawning" && msg !== "waiting") {
        setStartupMsg(msg);
        const merged = STAGE_MERGE[msg];
        if (merged) setStage(merged);
      }
    });
    return () => {
      u1?.();
      u2?.();
    };
  }, []);

  // 启动耗时计数器，组件挂载后只启动一次

  useEffect(() => {
    const t = setInterval(() => setElapsed((e) => e + 1), 1000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    if (!port || !token) return;
    const poll = async () => {
      try {
        const res = await fetch(`http://127.0.0.1:${port}/api/v1/health`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (res.ok) {
          const data = await res.json();
          const s = data.startup_stage;
          // Phase 49-H: 接受所有后端细粒度 stage，通过 STAGE_MERGE 归并到前端主要阶段
          if (s && STAGE_MERGE[s]) setStage(STAGE_MERGE[s]);
          if (data.vector_storage === false) setChromadbMissing(true);
        }
      } catch {
        // no-op
      }
    };
    const interval = setInterval(poll, 800);
    return () => clearInterval(interval);
  }, [port, token]);

  if (backendReady) return null;

  const stageIndex = STAGES.findIndex((s) => s.id === stage);
  const progressPct = Math.max(5, ((stageIndex + 1) / STAGES.length) * 100);
  const isFirstRun = /(extract|first run|解压|首次)/i.test(startupMsg);

  return (
    <div className="flex flex-col items-center justify-center min-h-screen bg-background select-none">
      <div className="absolute top-0 left-0 right-0 h-8 app-drag" />

      {/* Error State */}
      {(error || backendDead) && (
        <div className="animate-fade-in max-w-md w-full px-6 text-center space-y-6">
          <div className="w-14 h-14 rounded-2xl bg-destructive/10 flex items-center justify-center mx-auto">
            <AlertTriangle size={26} className="text-destructive" />
          </div>
          <div className="space-y-2">
            <h1 className="text-lg font-semibold tracking-tight">{t("startup.failed")}</h1>
            <p className="text-sm text-muted-foreground">
              {backendDead ? t("startup.timeout") : error || t("common.unknownError")}
            </p>
          </div>
          <button
            onClick={() => location.reload()}
            className="inline-flex items-center gap-2 px-5 py-2.5 bg-primary text-primary-foreground rounded-lg text-sm font-medium hover:opacity-90 transition-opacity shadow-sm"
          >
            {t("startup.retry")}
          </button>
          <Card>
            <CardContent className="py-3 text-left space-y-2 text-xs">
              <p className="font-medium text-muted-foreground">{t("startup.troubleshooting")}</p>
              <ol className="text-muted-foreground space-y-1 list-decimal list-inside">
                <li>{t("startup.checkLog")}</li>
                <li>{t("startup.checkPython")}</li>
                <li>{t("startup.firstRunTip")}</li>
              </ol>
            </CardContent>
          </Card>
        </div>
      )}

      {/* Loading State */}
      {!error && !backendDead && (
        <div className="max-w-sm w-full px-8 text-center flex flex-col items-center gap-5 animate-slide-up">
          {/* Brand mark */}
          <div className="relative">
            <div className="w-12 h-12 rounded-xl bg-gradient-to-br from-[var(--brand-from)] to-[var(--brand-to)] flex items-center justify-center shadow-md shadow-[var(--brand-shadow)]">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" className="text-primary-foreground">
                <path
                  d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </div>
            <div className="absolute -top-0.5 -right-0.5 w-2.5 h-2.5 rounded-full bg-success ring-2 ring-background animate-pulse" />
          </div>

          <div className="space-y-0.5">
            <h1 className="text-lg font-semibold tracking-tight">{t("chat.emptyTitle")}</h1>
            <p className="text-xs text-muted-foreground">{t("app.subtitle")}</p>
          </div>

          {/* First-run notice — 仅首次解压时显示，保持紧凑一行 */}
          {isFirstRun && (
            <div className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-warning/10 text-[11px] text-warning">
              <Package size={12} className="text-warning" />
              <span>{t("startup.extractingRuntimes")}</span>
              {elapsed > 20 && <span className="text-warning/70">({t("startup.largeFile")})</span>}
            </div>
          )}

          {/* Status card */}
          <div className="w-full space-y-2">
            {/* Progress bar */}
            <div className="w-full h-1 bg-secondary rounded-full overflow-hidden">
              <div
                className="h-full bg-primary rounded-full transition-[width] duration-300 ease-out"
                style={{ width: `${progressPct}%` }}
              />
            </div>

            {/* Status text */}
            <div className="flex items-center justify-center gap-2">
              <LoadingCurve variant="rose" size={14} particleCount={6} color="var(--primary)" />
              <p className="text-[11px] text-muted-foreground">
                {startupMsg ||
                  t("startup.stageStatus", {
                    stage: t(STAGES[Math.max(0, stageIndex)]?.labelKey || "startup.stages.init"),
                  })}
              </p>
            </div>

            {/* Elapsed */}
            <p className="text-[10px] text-muted-foreground/60 font-mono tabular-nums">
              {t("startup.elapsed", { seconds: elapsed })}
            </p>

            {/* Phase 43 P2.7: chromadb 状态提示 */}
            {chromadbMissing && <p className="text-[10px] text-muted-foreground/60">{t("startup.chromadbMissing")}</p>}
          </div>
        </div>
      )}
    </div>
  );
}
