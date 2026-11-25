import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useToast } from "../../hooks/use-toast";
import { useAppStore } from "../../store/appStore";
import { Button } from "../ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Input } from "../ui/input";
import { cn } from "../../lib/utils";

/**
 * Phase 96-5: 只读日志查看器——读取 yuncat.log 最后 N 行，支持等级 + 关键字过滤。
 *
 * 功能：
 * - 启动时自动加载最后 500 行
 * - 等级过滤按钮（ALL / ERROR / WARN / INFO）——单选
 * - 关键字输入框——大小写不敏感子串匹配（后端过滤）
 * - 刷新按钮——手动重新加载
 * - 导出按钮——下载完整 yuncat.log
 * - 截断提示——当日志总行数 > 返回行数时显示 truncated 标记
 *
 * 设计：日志文件 ≤ 10MB（RotatingFileHandler），后端全量读入后截断过滤，前端只渲染过滤结果。
 */
const LEVELS = ["", "ERROR", "WARNING", "INFO"] as const;
type Level = (typeof LEVELS)[number];

export function LogViewer() {
  const { t } = useTranslation();
  const { toast } = useToast();
  const port = useAppStore((s) => s.port);
  const token = useAppStore((s) => s.token);

  const [lines, setLines] = useState<string[]>([]);
  const [total, setTotal] = useState(0);
  const [filtered, setFiltered] = useState(0);
  const [truncated, setTruncated] = useState(false);
  const [loading, setLoading] = useState(false);

  const [level, setLevel] = useState<Level>("");
  const [keyword, setKeyword] = useState("");

  const containerRef = useRef<HTMLDivElement>(null);

  const fc = useCallback(
    (path: string, opts?: RequestInit) =>
      fetch(`http://127.0.0.1:${port}${path}`, {
        ...opts,
        headers: { Authorization: `Bearer ${token}`, ...opts?.headers },
      }),
    [port, token],
  );

  const loadLogs = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({ lines: "500" });
      if (level) params.set("level", level);
      if (keyword) params.set("q", keyword);
      const r = await fc(`/api/v1/logs/tail?${params.toString()}`);
      const data = await r.json().catch(() => ({}));
      if (data.error) {
        toast(t("logviewer.loadFailed") + `: ${data.error}`, "error");
        return;
      }
      setLines(data.lines || []);
      setTotal(data.total || 0);
      setFiltered(data.filtered || 0);
      setTruncated(!!data.truncated);
    } catch {
      toast(t("logviewer.loadFailed"), "error");
    } finally {
      setLoading(false);
    }
  }, [fc, level, keyword, t, toast]);

  // 启动时 + 过滤条件变化时自动加载
  useEffect(() => {
    loadLogs();
  }, [loadLogs]);

  const handleExport = async () => {
    try {
      const r = await fc("/api/v1/logs/download");
      if (!r.ok) {
        toast(t("logviewer.exportFailed"), "error");
        return;
      }
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `yuncat-${Date.now()}.log`;
      a.click();
      URL.revokeObjectURL(url);
      toast(t("logviewer.exportSuccess"), "success");
    } catch {
      toast(t("logviewer.exportFailed"), "error");
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t("logviewer.title")}</CardTitle>
        <CardDescription>{t("logviewer.desc")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {/* 工具栏 */}
        <div className="flex gap-2 flex-wrap items-center">
          {/* 等级过滤按钮组 */}
          <div className="flex gap-1" role="group" aria-label={t("logviewer.levelFilter")}>
            {LEVELS.map((lv) => (
              <Button
                key={lv || "ALL"}
                size="xs"
                variant={level === lv ? "default" : "outline"}
                onClick={() => setLevel(lv)}
                aria-pressed={level === lv}
              >
                {lv || t("logviewer.all")}
              </Button>
            ))}
          </div>
          {/* 关键字搜索 */}
          <Input
            type="text"
            placeholder={t("logviewer.searchPlaceholder")}
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            className="flex-1 min-w-[120px] h-7"
            aria-label={t("logviewer.searchPlaceholder")}
          />
          {/* 刷新 + 导出 */}
          <Button size="sm" variant="outline" onClick={loadLogs} disabled={loading}>
            {loading ? t("logviewer.loading") : t("logviewer.refresh")}
          </Button>
          <Button size="sm" variant="outline" onClick={handleExport}>
            {t("logviewer.export")}
          </Button>
        </div>

        {/* 统计信息 */}
        <div className="text-xs text-muted-foreground flex gap-3 flex-wrap">
          <span>{t("logviewer.total")}: {total}</span>
          <span>{t("logviewer.shown")}: {filtered}</span>
          {truncated && (
            <span className="text-yellow-600 dark:text-yellow-500">
              {t("logviewer.truncated")}
            </span>
          )}
        </div>

        {/* 日志内容 */}
        <div
          ref={containerRef}
          className="h-64 overflow-auto rounded-md bg-muted/50 p-2 font-mono text-xs leading-relaxed"
          role="log"
          aria-live="polite"
        >
          {lines.length === 0 ? (
            <div className="text-muted-foreground text-center py-4">
              {loading ? t("logviewer.loading") : t("logviewer.empty")}
            </div>
          ) : (
            lines.map((line, i) => (
              <div
                key={i}
                className={cn(
                  "whitespace-pre-wrap break-all",
                  line.includes("[ERROR]") && "text-destructive",
                  line.includes("[WARNING]") && "text-yellow-600 dark:text-yellow-500",
                )}
              >
                {line}
              </div>
            ))
          )}
        </div>
      </CardContent>
    </Card>
  );
}
