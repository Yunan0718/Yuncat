import { Clock, Search } from "lucide-react";

import { EmptyState } from "../common/EmptyState";

/**
 * 工作区浮层集合（2026-09-15 抽自 WorkspacePage）：快速打开 / 最近文件 / 跨文件搜索。
 *
 * 抽出来的原因很直接：WorkspacePage 已经贴着「文件 <800 行」的硬线，这三个浮层
 * 是页内最独立的一块（只依赖入参与回调），放在独立文件里既能控制体积，也便于单独演进。
 * props 名保持与页内状态同名，迁移过程零改写，降低引入回归的风险。
 */
export interface WorkspaceOverlaysProps {
  showQuickOpen: boolean;
  setShowQuickOpen: (v: boolean) => void;
  quickOpenQuery: string;
  setQuickOpenQuery: (v: string) => void;
  quickOpenResults: Array<{ path: string; name: string }>;
  openFile: (path: string, name: string) => void;
  showRecent: boolean;
  setShowRecent: (v: boolean) => void;
  recentFiles: Array<{ path: string; name: string; ts: number }>;
  showSearch: boolean;
  setShowSearch: (v: boolean) => void;
  searchQuery: string;
  setSearchQuery: (v: string) => void;
  searching: boolean;
  searchResults: Array<{ file: string; line: number; text: string }>;
  doSearch: () => void;
  t: (key: string, opts?: Record<string, unknown>) => string;
}

export function WorkspaceOverlays(props: WorkspaceOverlaysProps) {
  const {
    showQuickOpen, setShowQuickOpen, quickOpenQuery, setQuickOpenQuery, quickOpenResults, openFile,
    showRecent, setShowRecent, recentFiles,
    showSearch, setShowSearch, searchQuery, setSearchQuery, searching, searchResults, doSearch, t,
  } = props;

  return (
    <>
      {/* ---- Ctrl+P 快速打开弹窗 ---- */}
      {showQuickOpen && (
        <div
          className="fixed inset-0 bg-black/30 flex items-start justify-center pt-[20vh] z-modal"
          role="dialog"
          aria-modal="true"
          aria-label={t("workspacePage.quickOpenPlaceholder")}
        >
          <div className="bg-background rounded-lg shadow-xl w-[500px] max-h-[400px] flex flex-col">
            <div className="p-3 border-b flex items-center gap-2">
              <span className="text-xs text-muted-foreground">{t("workspacePage.quickOpenTitle")}</span>
              <div className="flex-1" />
              <button onClick={() => setShowQuickOpen(false)} className="text-muted-foreground hover:text-foreground">
                x
              </button>
            </div>
            <input
              autoFocus
              value={quickOpenQuery}
              onChange={(e) => setQuickOpenQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") setShowQuickOpen(false);
                if (e.key === "Enter" && quickOpenResults.length > 0) {
                  const r = quickOpenResults[0];
                  openFile(r.path, r.name);
                  setShowQuickOpen(false);
                  setQuickOpenQuery("");
                }
              }}
              placeholder={t("workspacePage.quickOpenPlaceholder")}
              className="w-full px-4 py-2 border-b text-sm bg-background"
            />
            <div className="flex-1 overflow-y-auto">
              {quickOpenResults.map((f) => (
                <button
                  key={f.path}
                  onClick={() => {
                    openFile(f.path, f.name);
                    setShowQuickOpen(false);
                    setQuickOpenQuery("");
                  }}
                  className="w-full text-left px-4 py-1.5 text-xs hover:bg-secondary flex items-center gap-2"
                >
                  <span className="text-muted-foreground">{f.name}</span>
                  <span className="text-[10px] text-muted-foreground truncate">
                    {f.path.split(/[/\\]/).slice(-2).join("/")}
                  </span>
                </button>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* ---- WS.3: 最近文件弹窗 ---- */}
      {showRecent && (
        <div
          className="fixed inset-0 bg-black/30 flex items-start justify-center pt-[20vh] z-modal"
          role="dialog"
          aria-modal="true"
          aria-label={t("workspacePage.quickOpenPlaceholder")}
        >
          <div className="bg-background rounded-lg shadow-xl w-[500px] max-h-[400px] flex flex-col">
            <div className="p-3 border-b flex items-center gap-2">
              <Clock size={14} className="text-muted-foreground" />
              <span className="text-xs text-muted-foreground">{t("workspacePage.recentTitle")}</span>
              <div className="flex-1" />
              <button onClick={() => setShowRecent(false)} className="text-muted-foreground hover:text-foreground">
                x
              </button>
            </div>
            <div className="flex-1 overflow-y-auto">
              {recentFiles.length === 0 ? (
                <EmptyState icon={<Clock size={16} />} title={t("workspacePage.recentEmpty")} compact />
              ) : (
                recentFiles.map((f, idx) => (
                  <button
                    // 只用 path 作 key：拼上 idx 会让 React 把"位置"当成身份，
                    // 删除中间某一项后，后续项的组件状态（如 hover/focus）会错位复用。
                    // idx 仍用于下面的序号展示，不是 key。
                    key={f.path}
                    onClick={() => {
                      openFile(f.path, f.name);
                      setShowRecent(false);
                    }}
                    className="w-full text-left px-4 py-1.5 text-xs hover:bg-secondary flex items-center gap-2"
                  >
                    <span className="text-muted-foreground shrink-0 text-[10px]">{idx + 1}.</span>
                    <span className="text-foreground">{f.name}</span>
                    <span className="text-[10px] text-muted-foreground truncate flex-1">
                      {f.path.split(/[/\\]/).slice(-3).join("/")}
                    </span>
                    <span className="text-[9px] text-muted-foreground shrink-0">
                      {new Date(f.ts).toLocaleString()}
                    </span>
                  </button>
                ))
              )}
            </div>
            <div className="px-3 py-1.5 border-t text-[10px] text-muted-foreground">
              {t("workspacePage.recentHint")}
            </div>
          </div>
        </div>
      )}

      {/* ---- 跨文件搜索面板 ---- */}
      {showSearch && (
        <div className="fixed inset-0 bg-black/30 flex items-start justify-center pt-[15vh] z-modal">
          <div className="bg-background rounded-lg shadow-xl w-[700px] max-h-[60vh] flex flex-col">
            <div className="p-3 border-b flex items-center gap-2">
              <span className="text-xs text-muted-foreground">{t("workspacePage.searchTitle")}</span>
              <div className="flex-1" />
              <button onClick={() => setShowSearch(false)} className="text-muted-foreground hover:text-foreground">
                x
              </button>
            </div>
            <div className="flex px-3 py-2 gap-2 border-b">
              <input
                autoFocus
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") setShowSearch(false);
                  if (e.key === "Enter") doSearch();
                }}
                placeholder={t("workspacePage.searchPlaceholder")}
                className="flex-1 px-3 py-1 border rounded text-sm"
              />
              <button
                onClick={doSearch}
                disabled={searching}
                className="px-3 py-1 bg-primary text-primary-foreground rounded text-xs font-semibold"
              >
                {searching ? t("workspacePage.searching") : t("workspacePage.searchButton")}
              </button>
            </div>
            <div className="flex-1 overflow-y-auto">
              {searchResults.length === 0 && !searching && (
                <EmptyState icon={<Search size={16} />} title={t("workspacePage.searchHint")} compact />
              )}
              {searchResults.map((r) => (
                <button
                  key={r.file + ":" + r.line + ":" + r.text.slice(0, 10)}
                  onClick={() => {
                    openFile(r.file, r.file.split(/[/\\]/).pop() || r.file);
                    setShowSearch(false);
                  }}
                  className="w-full text-left px-4 py-1.5 text-xs hover:bg-secondary flex items-start gap-3 border-b border-border"
                >
                  <span className="text-primary shrink-0">
                    {r.file}:{r.line}
                  </span>
                  <span className="text-muted-foreground font-mono truncate">{r.text}</span>
                </button>
              ))}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
