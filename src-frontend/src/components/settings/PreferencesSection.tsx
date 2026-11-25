import { useEffect, useState } from "react";
import { useApi } from "../../hooks/useApi";
import { useTranslation } from "react-i18next";
import { Trash2, Heart, Clock, Plus } from "lucide-react";
import { LoadingCurve } from "../common/LoadingCurve";
import { EmptyState } from "../common/EmptyState";

interface PrefRow {
  id: string;
  content: string;
  tags: string[];
  created_at: number;
  updated_at: number;
  project_id: string | null;
}

function formatTime(ts: number): string {
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch {
    return "";
  }
}

function categoryOf(tags: string[] | undefined): string | null {
  if (!tags) return null;
  for (const tag of tags) {
    if (tag.startsWith("cat:")) return tag.slice(4);
  }
  return null;
}

export function PreferencesSection() {
  const { t } = useTranslation();
  const { api } = useApi();
  const [prefs, setPrefs] = useState<PrefRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [category, setCategory] = useState("");
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);

  const load = async () => {
    setError(null);
    try {
      const rows = await api<PrefRow[]>("/api/v1/preferences");
      setPrefs(rows);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setPrefs([]);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleAdd = async () => {
    const value = text.trim();
    if (!value || saving) return;
    setSaving(true);
    try {
      await api("/api/v1/preferences", {
        method: "POST",
        body: JSON.stringify({ text: value, category: category.trim() || null }),
      });
      setText("");
      setCategory("");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id: string) => {
    setDeleting(id);
    try {
      await api(`/api/v1/preferences/${id}`, { method: "DELETE" });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setDeleting(null);
    }
  };

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-xl font-semibold tracking-tight">{t("settings.preferences")}</h2>
        <p className="text-sm text-muted-foreground mt-1">{t("settings.preferencesDesc")}</p>
      </div>

      <div className="flex flex-col gap-2 rounded-lg bg-secondary/40 hover:bg-secondary transition-colors p-3">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={t("preferences.placeholder")}
          rows={2}
          className="w-full resize-none rounded-md border border-border bg-background px-2 py-1.5 text-xs outline-none focus:border-primary"
        />
        <div className="flex items-center gap-2">
          <input
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            placeholder={t("preferences.category")}
            className="flex-1 rounded-md border border-border bg-background px-2 py-1.5 text-xs outline-none focus:border-primary"
          />
          <button
            type="button"
            onClick={handleAdd}
            disabled={!text.trim() || saving}
            className="inline-flex items-center gap-1 rounded-md bg-primary px-3 py-1.5 text-2xs font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
          >
            <Plus size={12} />
            {t("preferences.add")}
          </button>
        </div>
      </div>

      {prefs === null ? (
        <div className="flex items-center gap-2 py-4 text-xs text-muted-foreground">
          <LoadingCurve variant="lissajous" size={14} particleCount={6} />
          <span>{t("common.loading")}</span>
        </div>
      ) : error ? (
        <p className="text-xs text-destructive text-center py-4">{error}</p>
      ) : prefs.length === 0 ? (
        <EmptyState compact title={t("preferences.empty")} />
      ) : (
        <div className="flex flex-col gap-2">
          {prefs.map((p) => {
            const cat = categoryOf(p.tags);
            return (
              <div key={p.id} className="rounded-lg bg-secondary/40 hover:bg-secondary transition-colors p-3 text-xs">
                <div className="flex items-start justify-between gap-2">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5 font-medium truncate">
                      <Heart size={13} className="text-pink-500 shrink-0" />
                      <span className="truncate">{p.content}</span>
                    </div>
                    {cat && (
                      <div className="mt-1 inline-block rounded bg-muted px-1.5 py-0.5 text-2xs text-muted-foreground">
                        {t("preferences.categoryLabel")}: {cat}
                      </div>
                    )}
                    <div className="text-2xs text-muted-foreground mt-1 flex items-center gap-1">
                      <Clock size={11} className="shrink-0" />
                      {t("preferences.updatedAt")}: {formatTime(p.updated_at)}
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => handleDelete(p.id)}
                    disabled={deleting === p.id}
                    className="shrink-0 inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-2xs text-destructive hover:bg-destructive/10 disabled:opacity-50"
                  >
                    <Trash2 size={11} />
                    {t("preferences.delete")}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
