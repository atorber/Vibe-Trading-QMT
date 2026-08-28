import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { Loader2, Search, Star, X } from "lucide-react";
import { api, type MarketQuote } from "@/lib/api";
import { safeGet, safeRemove, safeSet } from "@/lib/storage";
import { marketChangeTextClass } from "@/lib/market-colors";
import { cn } from "@/lib/utils";

const RECENT_KEY = "vibe.markets.recent";
const RECENT_LIMIT = 8;

type RecentItem = { symbol: string; name?: string };

function formatPrice(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "—";
  return value.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: value >= 100 ? 2 : 3,
  });
}

function formatPct(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(2)}%`;
}

function toneClass(value: number | null | undefined) {
  return marketChangeTextClass(value);
}

function shortSymbol(symbol: string) {
  return symbol.replace(/\.(SH|SZ)$/i, "");
}

function readRecent(): RecentItem[] {
  try {
    const raw = safeGet(RECENT_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed
      .map((item) => {
        if (typeof item === "string") {
          return { symbol: item.trim().toUpperCase() };
        }
        if (item && typeof item === "object" && "symbol" in item) {
          const row = item as RecentItem;
          return {
            symbol: String(row.symbol || "").trim().toUpperCase(),
            name: row.name ? String(row.name) : undefined,
          };
        }
        return null;
      })
      .filter((item): item is RecentItem => Boolean(item?.symbol))
      .slice(0, RECENT_LIMIT);
  } catch {
    return [];
  }
}

export function pushRecentSymbol(symbol: string, name?: string) {
  const code = symbol.trim().toUpperCase();
  if (!code) return;
  const next = [
    { symbol: code, name: name?.trim() || undefined },
    ...readRecent().filter((row) => row.symbol !== code),
  ].slice(0, RECENT_LIMIT);
  safeSet(RECENT_KEY, JSON.stringify(next));
}

export function clearRecentSymbols() {
  safeRemove(RECENT_KEY);
}

export function MarketSearchTrigger({
  onClick,
  className,
}: {
  onClick: () => void;
  className?: string;
}) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-2 rounded-md border bg-background px-3 py-2 text-sm text-muted-foreground transition hover:border-primary/40 hover:text-foreground",
        className,
      )}
    >
      <Search className="h-4 w-4 shrink-0" />
      <span className="truncate">{t("markets.searchDialog.trigger")}</span>
    </button>
  );
}

interface MarketSearchDialogProps {
  open: boolean;
  onClose: () => void;
  onSelect: (symbol: string, name?: string) => void;
  watchlistSymbols?: Set<string>;
  onToggleWatchlist?: (symbol: string, name?: string) => void | Promise<void>;
}

export function MarketSearchDialog({
  open,
  onClose,
  onSelect,
  watchlistSymbols,
  onToggleWatchlist,
}: MarketSearchDialogProps) {
  const { t } = useTranslation();
  const inputRef = useRef<HTMLInputElement>(null);
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [recent, setRecent] = useState<RecentItem[]>([]);
  const [popular, setPopular] = useState<MarketQuote[]>([]);
  const [results, setResults] = useState<MarketQuote[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setDraft("");
    setQuery("");
    setResults([]);
    setError(null);
    setRecent(readRecent());
    const previouslyFocused =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const focusTimer = window.setTimeout(() => inputRef.current?.focus(), 0);
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      window.clearTimeout(focusTimer);
      document.removeEventListener("keydown", onKeyDown);
      if (previouslyFocused?.isConnected) previouslyFocused.focus();
    };
  }, [open, onClose]);

  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(() => setQuery(draft.trim()), 220);
    return () => window.clearTimeout(timer);
  }, [draft, open]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const run = async () => {
      setLoading(true);
      setError(null);
      try {
        const data = await api.getMarkets(query || undefined, 12);
        if (cancelled) return;
        if (query) {
          setResults(data.quotes ?? []);
        } else {
          setPopular(data.quotes ?? []);
          setResults([]);
        }
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : t("markets.errorLoad"));
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [open, query, t]);

  if (!open) return null;

  const list = query ? results : popular;

  const pick = (symbol: string, name?: string) => {
    pushRecentSymbol(symbol, name);
    onSelect(symbol);
    onClose();
  };

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <button
        type="button"
        aria-label={t("markets.searchDialog.close")}
        className="absolute inset-0 bg-black/45"
        onClick={onClose}
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="market-search-title"
        className="relative z-10 flex max-h-[min(560px,85vh)] w-full max-w-[440px] flex-col overflow-hidden rounded-2xl border bg-card shadow-2xl"
      >
        <div className="border-b px-4 pb-3 pt-4">
          <div className="mb-3 flex items-center justify-between gap-3">
            <h2 id="market-search-title" className="text-base font-semibold">
              {t("markets.searchDialog.title")}
            </h2>
            <button
              type="button"
              onClick={onClose}
              className="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
              aria-label={t("markets.searchDialog.close")}
            >
              <X className="h-4 w-4" />
            </button>
          </div>
          <div className="relative">
            <Search className="absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
            <input
              ref={inputRef}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder={t("markets.searchDialog.placeholder")}
              className="w-full rounded-lg border bg-background py-2 pl-9 pr-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-2 py-3">
          {!query && recent.length > 0 ? (
            <section className="mb-4 px-2">
              <div className="mb-2 flex items-center justify-between">
                <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  {t("markets.searchDialog.recent")}
                </h3>
                <button
                  type="button"
                  className="text-xs text-muted-foreground hover:text-foreground"
                  onClick={() => {
                    clearRecentSymbols();
                    setRecent([]);
                  }}
                >
                  {t("markets.searchDialog.clear")}
                </button>
              </div>
              <div className="flex flex-wrap gap-2">
                {recent.map((item) => (
                  <button
                    key={item.symbol}
                    type="button"
                    onClick={() => pick(item.symbol, item.name)}
                    className="rounded-full border bg-background px-3 py-1 text-xs font-medium hover:border-primary/40 hover:bg-muted/40"
                  >
                    {shortSymbol(item.symbol)}
                  </button>
                ))}
              </div>
            </section>
          ) : null}

          <section className="px-1">
            <h3 className="mb-2 px-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
              {query ? t("markets.searchResults") : t("markets.searchDialog.watchlist")}
            </h3>

            {error ? (
              <div className="mx-2 rounded-lg border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger">
                {error}
              </div>
            ) : null}

            {loading && !list.length ? (
              <div className="flex h-32 items-center justify-center text-muted-foreground">
                <Loader2 className="h-5 w-5 animate-spin" />
              </div>
            ) : null}

            {!loading && !list.length ? (
              <div className="px-2 py-8 text-center text-sm text-muted-foreground">
                {t("markets.empty")}
              </div>
            ) : null}

            <ul className="space-y-0.5">
              {list.map((row) => {
                const followed = watchlistSymbols?.has(row.symbol.toUpperCase()) ?? false;
                return (
                <li key={row.symbol} className="flex items-center gap-1">
                  {onToggleWatchlist ? (
                    <button
                      type="button"
                      aria-label={followed ? t("markets.watchlistUnfollow") : t("markets.watchlistFollow")}
                      title={followed ? t("markets.watchlistUnfollow") : t("markets.watchlistFollow")}
                      onClick={(event) => {
                        event.stopPropagation();
                        void onToggleWatchlist(row.symbol, row.name);
                      }}
                      className={cn(
                        "ml-1 rounded-md p-2 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
                        followed && "text-warning",
                      )}
                    >
                      <Star className={cn("h-4 w-4", followed && "fill-current")} />
                    </button>
                  ) : null}
                  <button
                    type="button"
                    onClick={() => pick(row.symbol, row.name)}
                    className="flex min-w-0 flex-1 items-center gap-3 rounded-xl px-2 py-2.5 text-left transition-colors hover:bg-muted/60"
                  >
                    <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-muted text-[10px] font-semibold text-muted-foreground">
                      {shortSymbol(row.symbol).slice(0, 4)}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium">{row.symbol}</div>
                      <div className="truncate text-xs text-muted-foreground">{row.name}</div>
                    </div>
                    <div className="shrink-0 text-right">
                      <div className="text-sm font-medium tabular-nums">{formatPrice(row.last)}</div>
                      <div className={`text-xs tabular-nums ${toneClass(row.change_pct)}`}>
                        {formatPct(row.change_pct)}
                      </div>
                    </div>
                  </button>
                </li>
              );
              })}
            </ul>
          </section>
        </div>
      </div>
    </div>,
    document.body,
  );
}
