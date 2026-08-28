import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { useTranslation } from "react-i18next";
import {
  ArrowLeft,
  LineChart,
  Loader2,
  Plus,
  RefreshCw,
  Star,
  StarOff,
  TrendingDown,
  TrendingUp,
} from "lucide-react";
import {
  api,
  type MarketDetailResponse,
  type MarketEarningsResponse,
  type MarketNewsArticle,
  type MarketPeersResponse,
  type MarketQuote,
  type PriceBar,
} from "@/lib/api";
import { CandlestickChart, formatChartBarTime } from "@/components/charts/CandlestickChart";
import { MarketSearchDialog, MarketSearchTrigger, pushRecentSymbol } from "@/components/markets/MarketSearchDialog";
import { MarketAssessmentCard } from "@/components/markets/MarketAssessmentCard";
import { buildMarketInsights, type MarketInsight } from "@/lib/marketInsights";
import {
  buildWatchingPoints,
  consensusForYear,
  type EarningsStage,
  filterEarningsNews,
  fiscalYearOptions,
  latestReportDate,
} from "@/lib/marketEarnings";
import {
  averagePeerChange,
  formatBoardChange,
  formatPeerChange,
} from "@/lib/marketPeers";
import { marketChangeTextClass, marketInsightToneClass } from "@/lib/market-colors";
import { useMarketsWatchlist } from "@/lib/useMarketsWatchlist";
import { cn } from "@/lib/utils";

type DetailTab = "overview" | "narratives" | "anomalies" | "news" | "earnings" | "peers";

const DETAIL_TABS: DetailTab[] = ["overview", "narratives", "anomalies", "news", "earnings", "peers"];

function isDetailTab(value: string | null | undefined): value is DetailTab {
  return Boolean(value && DETAIL_TABS.includes(value as DetailTab));
}

const CHART_PERIODS = [
  { id: "1m", label: "1m" },
  { id: "5m", label: "5m" },
  { id: "15m", label: "15m" },
  { id: "60m", label: "1H" },
  { id: "1d", label: "1D" },
] as const;

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

function formatAmount(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  if (abs >= 1e8) return `${(value / 1e8).toFixed(2)}亿`;
  if (abs >= 1e4) return `${(value / 1e4).toFixed(2)}万`;
  return value.toLocaleString(undefined, { maximumFractionDigits: 0 });
}

function formatVolume(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "—";
  return Math.round(value).toLocaleString();
}

function toneClass(value: number | null | undefined) {
  return marketChangeTextClass(value);
}

function formatSessionTime(value: string | null | undefined) {
  if (!value) return null;
  const raw = String(value).trim();
  if (!raw) return null;
  // Bridge often returns "YYYYMMDD HH:MM:SS" or epoch-ish strings — show as-is when unparseable.
  const normalized = raw.includes("T") || raw.includes("-") || raw.includes(" ")
    ? raw.replace("T", " ").slice(0, 19)
    : raw;
  return normalized;
}

function QuoteCard({ quote, onClick }: { quote: MarketQuote; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded-xl border bg-card p-4 text-left transition hover:border-primary/40"
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="font-medium">{quote.name}</div>
          <div className="mt-1 text-xs text-muted-foreground">{quote.symbol}</div>
        </div>
        {quote.change_pct != null && quote.change_pct !== 0 ? (
          quote.change_pct > 0 ? <TrendingUp className="h-4 w-4 text-danger" /> : <TrendingDown className="h-4 w-4 text-success" />
        ) : null}
      </div>
      <div className={`mt-4 text-2xl font-semibold tracking-tight ${toneClass(quote.change_pct)}`}>
        {formatPrice(quote.last)}
      </div>
      <div className={`mt-1 text-sm ${toneClass(quote.change_pct)}`}>
        {quote.change != null ? `${quote.change > 0 ? "+" : ""}${formatPrice(quote.change)}` : "—"}
        {" · "}
        {formatPct(quote.change_pct)}
      </div>
    </button>
  );
}

function formatSignedPct(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(2)}%`;
}

function formatInsightTime(value: string | undefined) {
  if (!value) return "";
  const label = formatChartBarTime(value);
  if (/^\d{4}-\d{2}-\d{2}$/.test(label)) {
    const d = new Date(`${label}T15:00:00`);
    if (!Number.isNaN(d.getTime())) {
      return d.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        timeZoneName: "shortOffset",
      });
    }
  }
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(label)) {
    const d = new Date(label.replace(" ", "T"));
    if (!Number.isNaN(d.getTime())) {
      return d.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        timeZoneName: "shortOffset",
      });
    }
  }
  return label;
}

function formatNewsAge(published: string | null | undefined) {
  if (!published) return "";
  const d = new Date(published);
  if (Number.isNaN(d.getTime())) return published;
  const diffMs = Math.max(0, Date.now() - d.getTime());
  const hours = Math.floor(diffMs / 3_600_000);
  if (hours < 24) return hours < 1 ? "<1h" : `${hours}h`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d`;
  const weeks = Math.floor(days / 7);
  if (weeks < 5) return `${weeks}w`;
  return `${Math.floor(days / 30)}mo`;
}

function sourceLabel(source: string | null | undefined) {
  const text = String(source || "News").trim();
  return text || "News";
}

function NewsArticleRow({ article }: { article: MarketNewsArticle }) {
  const source = sourceLabel(article.source);
  const initial = source.charAt(0).toUpperCase();
  const body = (
    <>
      <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-muted text-xs font-semibold text-muted-foreground">
        {initial}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-muted-foreground">
          <span className="truncate lowercase">{source}</span>
          {article.published ? (
            <>
              <span aria-hidden>·</span>
              <time dateTime={article.published}>{formatNewsAge(article.published)}</time>
            </>
          ) : null}
        </div>
        <h3 className="mt-1 line-clamp-2 text-sm font-medium leading-snug text-foreground">
          {article.title}
        </h3>
        {article.snippet ? (
          <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-muted-foreground">{article.snippet}</p>
        ) : null}
      </div>
    </>
  );

  if (article.url) {
    return (
      <a
        href={article.url}
        target="_blank"
        rel="noopener noreferrer"
        className="flex gap-3 rounded-xl px-2 py-3 transition-colors hover:bg-muted/50"
      >
        {body}
      </a>
    );
  }
  return <article className="flex gap-3 px-2 py-3">{body}</article>;
}

const EARNINGS_STAGES: EarningsStage[] = ["pre", "release", "transcript", "post"];

function EarningsPanel({
  name,
  loading,
  error,
  data,
  releaseNews,
  newsLoading,
  askAgentHref,
}: {
  name: string;
  loading: boolean;
  error: string | null;
  data: MarketEarningsResponse | null;
  releaseNews: MarketNewsArticle[];
  newsLoading: boolean;
  askAgentHref: string;
}) {
  const { t } = useTranslation();
  const [stage, setStage] = useState<EarningsStage>("pre");
  const reports = data?.reports ?? [];
  const consensus = data?.consensus_eps ?? [];
  const years = useMemo(() => fiscalYearOptions(consensus), [consensus]);
  const [period, setPeriod] = useState(years[0] ?? String(new Date().getFullYear()));

  useEffect(() => {
    if (years.length && !years.includes(period)) {
      setPeriod(years[0]);
    }
  }, [years, period]);

  const updatedAt = latestReportDate(reports);
  const eps = consensusForYear(consensus, period);
  const watching = buildWatchingPoints(reports, consensus, period);

  if (loading) {
    return (
      <div className="flex h-48 items-center justify-center text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin" />
      </div>
    );
  }
  if (error) {
    return (
      <div className="rounded-2xl border border-danger/30 bg-danger/5 p-4 text-sm text-danger">
        {error}
      </div>
    );
  }
  if (!reports.length && !consensus.length) {
    return (
      <div className="mx-auto flex w-full max-w-[820px] flex-col items-start gap-3 rounded-2xl border bg-card p-8">
        <p className="text-sm text-muted-foreground">{t("markets.earnings.empty")}</p>
        <Link
          to={askAgentHref}
          className="inline-flex items-center rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground"
        >
          {t("markets.askAgent")}
        </Link>
      </div>
    );
  }

  return (
    <div className="mx-auto flex w-full max-w-[820px] flex-col gap-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <label className="flex items-center gap-2 text-sm text-muted-foreground">
          <span>{t("markets.earnings.period")}</span>
          <select
            value={period}
            onChange={(event) => setPeriod(event.target.value)}
            className="rounded-md border bg-background px-2 py-1 text-sm text-foreground"
          >
            {years.map((year) => (
              <option key={year} value={year}>
                {t("markets.earnings.fiscalYear", { year })}
              </option>
            ))}
          </select>
        </label>
        {updatedAt ? (
          <span className="text-xs text-muted-foreground">
            {t("markets.earnings.updated", { date: updatedAt.slice(0, 10) })}
          </span>
        ) : null}
      </div>

      <div
        role="group"
        className="flex flex-wrap gap-1 rounded-full border bg-muted/30 p-1"
      >
        {EARNINGS_STAGES.map((item) => (
          <button
            key={item}
            type="button"
            onClick={() => setStage(item)}
            className={cn(
              "rounded-full px-3 py-1.5 text-xs font-medium transition-colors",
              stage === item
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {t(`markets.earnings.stages.${item}`)}
          </button>
        ))}
      </div>

      {stage === "pre" ? (
        <section className="rounded-2xl border bg-card p-5">
          <h2 className="text-lg font-semibold">{t("markets.earnings.stages.pre")}</h2>
          {updatedAt ? (
            <p className="mt-1 text-xs text-muted-foreground">
              {t("markets.earnings.updated", { date: updatedAt.slice(0, 10) })}
            </p>
          ) : null}
          <p className="mt-4 text-sm leading-relaxed text-foreground">
            {t("markets.earnings.preSummary", {
              name,
              year: period,
              eps: eps != null ? eps.toFixed(2) : "—",
              reportCount: reports.length,
            })}
          </p>
          {watching.length ? (
            <div className="mt-6">
              <h3 className="text-sm font-semibold">{t("markets.earnings.watchingTitle")}</h3>
              <ul className="mt-3 list-disc space-y-2 pl-5 text-sm leading-relaxed text-muted-foreground">
                {watching.map((point, index) => {
                  if (point.type === "consensus") {
                    return (
                      <li key={`consensus-${index}`}>
                        {t("markets.earnings.watching.consensus", {
                          year: point.year,
                          eps: point.eps.toFixed(2),
                        })}
                      </li>
                    );
                  }
                  if (point.type === "ratings") {
                    return (
                      <li key={`ratings-${index}`}>
                        {t("markets.earnings.watching.ratings", { ratings: point.ratings.join("、") })}
                      </li>
                    );
                  }
                  if (point.type === "sellerEps") {
                    return (
                      <li key={`seller-${index}`}>
                        {t("markets.earnings.watching.sellerEps", {
                          eps: point.avg.toFixed(2),
                          count: point.count,
                        })}
                      </li>
                    );
                  }
                  return (
                    <li key={`coverage-${index}`}>
                      {t("markets.earnings.watching.coverage", { count: point.count })}
                    </li>
                  );
                })}
              </ul>
            </div>
          ) : null}
        </section>
      ) : null}

      {stage === "release" ? (
        <section className="rounded-2xl border bg-card p-1">
          {newsLoading ? (
            <div className="flex h-40 items-center justify-center text-muted-foreground">
              <Loader2 className="h-5 w-5 animate-spin" />
            </div>
          ) : releaseNews.length ? (
            <div className="divide-y">
              {releaseNews.map((article, index) => (
                <NewsArticleRow key={`${article.url || article.title}-${index}`} article={article} />
              ))}
            </div>
          ) : (
            <div className="p-8 text-center text-sm text-muted-foreground">
              {t("markets.earnings.releaseEmpty")}
            </div>
          )}
        </section>
      ) : null}

      {stage === "transcript" ? (
        <section className="flex flex-col items-start gap-3 rounded-2xl border bg-card p-8">
          <p className="text-sm leading-relaxed text-muted-foreground">{t("markets.earnings.transcriptEmpty")}</p>
          <Link
            to={askAgentHref}
            className="inline-flex items-center rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground"
          >
            {t("markets.askAgent")}
          </Link>
        </section>
      ) : null}

      {stage === "post" ? (
        <section className="rounded-2xl border bg-card">
          <div className="border-b px-4 py-3 text-sm font-semibold">{t("markets.earnings.reportList")}</div>
          <div className="divide-y">
            {reports.map((report, index) => (
              <article key={`${report.title}-${index}`} className="px-4 py-4">
                <h3 className="text-sm font-medium leading-snug">{report.title}</h3>
                <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
                  {report.brokerage ? <span>{report.brokerage}</span> : null}
                  {report.rating ? (
                    <span>
                      {t("markets.earnings.rating")}: {report.rating}
                    </span>
                  ) : null}
                  {report.publish_date ? <time dateTime={report.publish_date}>{report.publish_date.slice(0, 10)}</time> : null}
                </div>
                {report.eps_forecast?.this_year != null ? (
                  <p className="mt-2 text-xs text-muted-foreground">
                    EPS {report.eps_forecast.this_year}
                    {report.pe_forecast?.this_year != null ? ` · PE ${report.pe_forecast.this_year}` : ""}
                  </p>
                ) : null}
              </article>
            ))}
          </div>
        </section>
      ) : null}
    </div>
  );
}

function PeersPanel({
  name,
  quote,
  loading,
  error,
  data,
  askAgentHref,
}: {
  name: string;
  quote: MarketQuote;
  loading: boolean;
  error: string | null;
  data: MarketPeersResponse | null;
  askAgentHref: string;
}) {
  const { t } = useTranslation();
  const peers = data?.peers ?? [];
  const targetChange = data?.target?.change_pct ?? quote.change_pct;
  const avgChange = useMemo(() => averagePeerChange(peers, data?.target), [peers, data?.target]);

  if (loading) {
    return (
      <div className="flex h-48 items-center justify-center text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin" />
      </div>
    );
  }
  if (error) {
    return (
      <div className="rounded-2xl border border-danger/30 bg-danger/5 p-4 text-sm text-danger">
        {error}
      </div>
    );
  }
  if (!data || !peers.length) {
    return (
      <div className="mx-auto flex w-full max-w-[820px] flex-col items-start gap-3 rounded-2xl border bg-card p-8">
        <p className="text-sm text-muted-foreground">{t("markets.peers.empty")}</p>
        <Link
          to={askAgentHref}
          className="inline-flex items-center rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground"
        >
          {t("markets.askAgent")}
        </Link>
      </div>
    );
  }

  const comps = peers.slice(0, 5);

  return (
    <div className="mx-auto flex w-full max-w-[820px] flex-col gap-8">
      <section>
        <h2 className="text-sm font-semibold tracking-tight text-foreground">{t("markets.peers.compsTitle")}</h2>
        <ul className="mt-4 space-y-4 text-sm leading-relaxed text-foreground">
          {comps.map((peer) => (
            <li key={peer.symbol}>
              <Link to={`/markets/${encodeURIComponent(peer.symbol)}`} className="hover:text-primary">
                <span className="font-medium">{peer.name}</span>
                <span className="text-muted-foreground">
                  {" "}
                  ({peer.symbol.replace(/\.(SH|SZ|BJ)$/i, "")})
                </span>
              </Link>
              <span className="text-muted-foreground">
                {" "}
                —{" "}
                {t("markets.peers.compsItemSuffix", {
                  change: formatPeerChange(peer.change_pct),
                })}
              </span>
            </li>
          ))}
        </ul>
      </section>

      <section>
        <h2 className="text-sm font-semibold tracking-tight text-foreground">
          {t("markets.peers.differentiatedTitle")}
        </h2>
        <p className="mt-4 text-sm leading-relaxed text-foreground">
          {t("markets.peers.differentiatedBody", {
            name,
            industry: data.industry,
            boardChange: formatBoardChange(data.board_change_pct),
            targetChange: formatPeerChange(targetChange),
            avgChange: formatPeerChange(avgChange),
          })}
        </p>
      </section>

      <section>
        <h2 className="text-sm font-semibold tracking-tight text-foreground">{t("markets.peers.watchTitle")}</h2>
        <p className="mt-4 text-sm leading-relaxed text-foreground">
          {t("markets.peers.watchBody", {
            name,
            industry: data.industry,
          })}
        </p>
      </section>

      <Link
        to={askAgentHref}
        className="inline-flex w-fit items-center rounded-md border px-3 py-2 text-sm font-medium text-foreground hover:bg-muted"
      >
        {t("markets.askAgent")}
      </Link>
    </div>
  );
}

function InsightCard({ item }: { item: MarketInsight }) {
  return (
    <article className="rounded-xl border bg-card p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={cn(
            "rounded-md px-2 py-0.5 text-[11px] font-medium",
            marketInsightToneClass(item.tone),
          )}
        >
          {item.tag}
        </span>
        {item.time ? <span className="text-xs text-muted-foreground">{item.time}</span> : null}
      </div>
      <h3 className="mt-2 text-base font-semibold leading-snug">{item.title}</h3>
      <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{item.body}</p>
    </article>
  );
}

function AnomalyCard({ item }: { item: MarketInsight }) {
  const { t } = useTranslation();
  return (
    <article className="rounded-2xl border bg-card p-5 shadow-sm">
      {item.time ? (
        <time className="text-xs text-muted-foreground">{formatInsightTime(item.time)}</time>
      ) : null}
      {item.price != null && item.changePct != null ? (
        <p className="mt-2 text-sm leading-relaxed">
          <span className="text-muted-foreground">{t("markets.anomaly.priceAt")} </span>
          <span className="font-semibold tabular-nums">{formatPrice(item.price)}</span>
          {" "}
          <span className={cn("font-semibold tabular-nums", marketChangeTextClass(item.changePct))}>
            ({formatSignedPct(item.changePct)}
            {item.sessionLabel ? ` · ${item.sessionLabel}` : ""})
          </span>
        </p>
      ) : null}
      <h3 className="mt-3 text-base font-semibold leading-snug tracking-tight text-foreground">
        {item.title}
      </h3>
      {item.body ? (
        <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{item.body}</p>
      ) : null}
    </article>
  );
}

function MarketsList() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const watchlist = useMarketsWatchlist();
  const [searchOpen, setSearchOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [indices, setIndices] = useState<MarketQuote[]>([]);
  const [quotes, setQuotes] = useState<MarketQuote[]>([]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.getMarkets();
      setIndices(data.indices ?? []);
      setQuotes(data.quotes ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : t("markets.errorLoad"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  const handleToggleWatchlist = useCallback(async (symbol: string, name?: string) => {
    await watchlist.toggle(symbol, name);
    await load();
  }, [watchlist, load]);

  const handleRemoveFromWatchlist = useCallback(async (symbol: string) => {
    await watchlist.remove(symbol);
    await load();
  }, [watchlist, load]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)) {
        return;
      }
      event.preventDefault();
      setSearchOpen(true);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  const openSymbol = useCallback((symbol: string, name?: string) => {
    pushRecentSymbol(symbol, name);
    navigate(`/markets/${encodeURIComponent(symbol)}`);
  }, [navigate]);

  return (
    <div className="min-h-screen p-4 sm:p-6 lg:p-8">
      <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-6">
        <header className="flex flex-col gap-4 border-b pb-6 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <div className="mb-3 inline-flex items-center gap-2 rounded-full border bg-card px-3 py-1 text-xs text-muted-foreground">
              <LineChart className="h-3.5 w-3.5 text-primary" />
              {t("markets.badge")}
            </div>
            <h1 className="text-3xl font-semibold tracking-tight">{t("markets.title")}</h1>
            <p className="mt-2 text-sm text-muted-foreground">{t("markets.subtitle")}</p>
          </div>
          <div className="flex w-full items-center gap-2 lg:w-auto">
            <MarketSearchTrigger onClick={() => setSearchOpen(true)} className="flex-1 lg:min-w-[280px]" />
            <kbd className="hidden rounded border bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground sm:inline">/</kbd>
            <button
              type="button"
              onClick={() => void load()}
              className="inline-flex items-center justify-center rounded-md border bg-card px-3 py-2"
              title={t("markets.refresh")}
            >
              <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            </button>
          </div>
        </header>

        {error ? (
          <div className="rounded-lg border border-danger/30 bg-danger/5 p-4 text-sm text-danger">{error}</div>
        ) : null}

        <section>
          <h2 className="mb-3 font-semibold">{t("markets.indices")}</h2>
          {loading && !indices.length ? (
            <div className="flex h-28 items-center justify-center"><Loader2 className="h-5 w-5 animate-spin text-muted-foreground" /></div>
          ) : (
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              {indices.map((item) => (
                <QuoteCard key={item.symbol} quote={item} onClick={() => openSymbol(item.symbol, item.name)} />
              ))}
            </div>
          )}
        </section>

        <section className="overflow-hidden rounded-xl border bg-card">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b p-4">
            <div>
              <h2 className="font-semibold">{t("markets.watchlist")}</h2>
              <p className="mt-1 text-xs text-muted-foreground">{t("markets.watchlistHint")}</p>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-xs text-muted-foreground">{t("markets.count", { count: quotes.length })}</span>
              <button
                type="button"
                onClick={() => setSearchOpen(true)}
                className="inline-flex items-center gap-1.5 rounded-md border bg-background px-3 py-1.5 text-xs font-medium hover:border-primary/40"
              >
                <Plus className="h-3.5 w-3.5" />
                {t("markets.watchlistAdd")}
              </button>
            </div>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-sm">
              <thead className="bg-muted/40 text-left text-xs text-muted-foreground">
                <tr>
                  <th className="px-4 py-3 font-medium">{t("markets.colSymbol")}</th>
                  <th className="px-4 py-3 font-medium">{t("markets.colLast")}</th>
                  <th className="px-4 py-3 font-medium">{t("markets.colChange")}</th>
                  <th className="px-4 py-3 font-medium">{t("markets.colPct")}</th>
                  <th className="px-4 py-3 font-medium">{t("markets.colVolume")}</th>
                  <th className="w-12 px-2 py-3" aria-hidden />
                </tr>
              </thead>
              <tbody className="divide-y">
                {loading && !quotes.length ? (
                  <tr><td colSpan={6} className="px-4 py-10 text-center text-muted-foreground"><Loader2 className="mx-auto h-5 w-5 animate-spin" /></td></tr>
                ) : null}
                {!loading && !quotes.length ? (
                  <tr>
                    <td colSpan={6} className="px-4 py-10 text-center text-muted-foreground">
                      <p>{t("markets.watchlistEmpty")}</p>
                      <button
                        type="button"
                        onClick={() => setSearchOpen(true)}
                        className="mt-3 inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground"
                      >
                        <Plus className="h-3.5 w-3.5" />
                        {t("markets.watchlistAdd")}
                      </button>
                    </td>
                  </tr>
                ) : null}
                {quotes.map((row) => (
                  <tr
                    key={row.symbol}
                    className="cursor-pointer hover:bg-muted/20"
                    onClick={() => openSymbol(row.symbol, row.name)}
                  >
                    <td className="px-4 py-3">
                      <div className="font-medium">{row.name}</div>
                      <div className="text-xs text-muted-foreground">{row.symbol}</div>
                    </td>
                    <td className={`px-4 py-3 font-medium ${toneClass(row.change_pct)}`}>{formatPrice(row.last)}</td>
                    <td className={`px-4 py-3 ${toneClass(row.change_pct)}`}>
                      {row.change != null ? `${row.change > 0 ? "+" : ""}${formatPrice(row.change)}` : "—"}
                    </td>
                    <td className={`px-4 py-3 ${toneClass(row.change_pct)}`}>{formatPct(row.change_pct)}</td>
                    <td className="px-4 py-3 text-muted-foreground">
                      {row.volume != null ? Math.round(row.volume).toLocaleString() : "—"}
                    </td>
                    <td className="px-2 py-3 text-right">
                      <button
                        type="button"
                        aria-label={t("markets.watchlistRemove")}
                        title={t("markets.watchlistRemove")}
                        onClick={(event) => {
                          event.stopPropagation();
                          void handleRemoveFromWatchlist(row.symbol);
                        }}
                        className="rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground"
                      >
                        <StarOff className="h-4 w-4" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </div>

      <MarketSearchDialog
        open={searchOpen}
        onClose={() => setSearchOpen(false)}
        onSelect={(symbol, name) => openSymbol(symbol, name)}
        watchlistSymbols={watchlist.symbolSet}
        onToggleWatchlist={handleToggleWatchlist}
      />
    </div>
  );
}

function MarketsDetail() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const watchlist = useMarketsWatchlist();
  const { symbol: rawSymbol } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const symbol = decodeURIComponent(rawSymbol || "");
  const [searchOpen, setSearchOpen] = useState(false);
  const tab: DetailTab = isDetailTab(searchParams.get("tab")) ? (searchParams.get("tab") as DetailTab) : "overview";
  const [period, setPeriod] = useState<string>("1d");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<MarketDetailResponse | null>(null);
  const [newsLoading, setNewsLoading] = useState(false);
  const [newsError, setNewsError] = useState<string | null>(null);
  const [newsArticles, setNewsArticles] = useState<MarketNewsArticle[]>([]);
  const [earningsLoading, setEarningsLoading] = useState(false);
  const [earningsError, setEarningsError] = useState<string | null>(null);
  const [earningsData, setEarningsData] = useState<MarketEarningsResponse | null>(null);
  const [peersLoading, setPeersLoading] = useState(false);
  const [peersError, setPeersError] = useState<string | null>(null);
  const [peersData, setPeersData] = useState<MarketPeersResponse | null>(null);

  const setTab = useCallback((next: DetailTab) => {
    setSearchParams(
      (prev) => {
        const params = new URLSearchParams(prev);
        if (next === "overview") params.delete("tab");
        else params.set("tab", next);
        return params;
      },
      { replace: true },
    );
  }, [setSearchParams]);

  const load = useCallback(async (nextPeriod = period) => {
    if (!symbol) return;
    setLoading(true);
    setError(null);
    try {
      setDetail(await api.getMarketDetail(symbol, nextPeriod, nextPeriod === "1d" ? 180 : 240));
    } catch (err) {
      setError(err instanceof Error ? err.message : t("markets.errorLoad"));
    } finally {
      setLoading(false);
    }
  }, [symbol, period, t]);

  useEffect(() => {
    void load(period);
  }, [load, period]);

  useEffect(() => {
    if (tab !== "news" || !symbol) return;
    let cancelled = false;
    setNewsLoading(true);
    setNewsError(null);
    void api
      .getMarketNews(symbol, 30)
      .then((data) => {
        if (!cancelled) setNewsArticles(data.articles ?? []);
      })
      .catch((err) => {
        if (!cancelled) {
          setNewsError(err instanceof Error ? err.message : t("markets.newsError"));
          setNewsArticles([]);
        }
      })
      .finally(() => {
        if (!cancelled) setNewsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [tab, symbol, t]);

  useEffect(() => {
    if (tab !== "earnings" || !symbol) return;
    let cancelled = false;
    setEarningsLoading(true);
    setEarningsError(null);
    setNewsLoading(true);
    setNewsError(null);
    void Promise.all([
      api.getMarketEarnings(symbol, 15),
      api.getMarketNews(symbol, 30),
    ])
      .then(([earnings, news]) => {
        if (cancelled) return;
        setEarningsData(earnings);
        setNewsArticles(news.articles ?? []);
      })
      .catch((err) => {
        if (cancelled) return;
        const message = err instanceof Error ? err.message : t("markets.earnings.error");
        setEarningsError(message);
        setEarningsData(null);
        setNewsError(err instanceof Error ? err.message : t("markets.newsError"));
        setNewsArticles([]);
      })
      .finally(() => {
        if (!cancelled) {
          setEarningsLoading(false);
          setNewsLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [tab, symbol, t]);

  const earningsReleaseNews = useMemo(() => filterEarningsNews(newsArticles), [newsArticles]);

  useEffect(() => {
    if (tab !== "peers" || !symbol) return;
    let cancelled = false;
    setPeersLoading(true);
    setPeersError(null);
    void api
      .getMarketPeers(symbol, 8)
      .then((data) => {
        if (!cancelled) setPeersData(data);
      })
      .catch((err) => {
        if (!cancelled) {
          setPeersError(err instanceof Error ? err.message : t("markets.peers.error"));
          setPeersData(null);
        }
      })
      .finally(() => {
        if (!cancelled) setPeersLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [tab, symbol, t]);

  useEffect(() => {
    if (symbol) pushRecentSymbol(symbol, detail?.name);
  }, [symbol, detail?.name]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)) {
        return;
      }
      event.preventDefault();
      setSearchOpen(true);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  const bars: PriceBar[] = useMemo(() => {
    return (detail?.bars ?? [])
      .filter((bar) => bar.open != null && bar.high != null && bar.low != null && bar.close != null)
      .map((bar) => ({
        time: bar.time,
        open: Number(bar.open),
        high: Number(bar.high),
        low: Number(bar.low),
        close: Number(bar.close),
        volume: Number(bar.volume ?? 0),
      }));
  }, [detail]);

  const insights = useMemo(() => {
    if (!detail?.quote) return { narratives: [] as MarketInsight[], anomalies: [] as MarketInsight[] };
    return buildMarketInsights(detail.name, detail.quote, bars);
  }, [detail, bars]);

  const quote = detail?.quote;
  const sessionTime = formatSessionTime(quote?.updated_at);

  const askAgentHref = useMemo(() => {
    if (!detail) return "/";
    const prompt = `请解读 ${detail.name}（${detail.symbol}）的近期走势、量价与风险点。`;
    return `/?q=${encodeURIComponent(prompt)}`;
  }, [detail]);

  const followed = watchlist.has(symbol);

  const handleToggleWatchlist = useCallback(async () => {
    if (!symbol) return;
    await watchlist.toggle(symbol, detail?.name);
  }, [watchlist, symbol, detail?.name]);

  const tabs: { id: DetailTab; label: string }[] = [
    { id: "overview", label: t("markets.tabs.overview") },
    { id: "narratives", label: t("markets.tabs.narratives") },
    { id: "anomalies", label: t("markets.tabs.anomalies") },
    { id: "news", label: t("markets.tabs.news") },
    { id: "earnings", label: t("markets.tabs.earnings") },
    { id: "peers", label: t("markets.tabs.peers") },
  ];

  return (
    <div className="flex h-full min-h-0 w-full flex-1 flex-col overflow-hidden bg-background">
      <div className="shrink-0 border-b">
        <div className="flex items-center justify-between gap-3 px-3 py-2 sm:px-5">
          <Link
            to="/markets"
            className="inline-flex h-9 w-9 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
            aria-label={t("markets.back")}
          >
            <ArrowLeft className="h-5 w-5" />
          </Link>
          <MarketSearchTrigger onClick={() => setSearchOpen(true)} className="max-w-[220px] sm:max-w-none" />
        </div>

        {loading && !detail ? (
          <div className="flex h-24 items-center justify-center"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div>
        ) : null}
        {error ? <div className="mx-3 mb-3 rounded-lg border border-danger/30 bg-danger/5 p-3 text-sm text-danger sm:mx-5">{error}</div> : null}

        {detail && quote ? (
          <>
            <header className="flex flex-col gap-4 px-4 pb-4 sm:px-6 lg:flex-row lg:items-end lg:justify-between">
              <div className="min-w-0">
                <div className="flex items-start gap-2">
                  <h1 className="truncate text-xl font-semibold tracking-tight sm:text-2xl">{detail.name}</h1>
                  <button
                    type="button"
                    aria-label={followed ? t("markets.watchlistUnfollow") : t("markets.watchlistFollow")}
                    title={followed ? t("markets.watchlistUnfollow") : t("markets.watchlistFollow")}
                    onClick={() => void handleToggleWatchlist()}
                    className={cn(
                      "mt-0.5 rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
                      followed && "text-warning",
                    )}
                  >
                    <Star className={cn("h-5 w-5", followed && "fill-current")} />
                  </button>
                </div>
                <p className="mt-1 text-sm text-muted-foreground">
                  {detail.symbol.replace(/\.(SH|SZ)$/i, "")}
                  {" · "}
                  {detail.market || detail.instrument?.exchange || t("markets.ashare")}
                  {detail.sector ? ` · ${detail.sector}` : ""}
                </p>
              </div>

              <div className="flex flex-wrap items-end gap-8 sm:gap-10">
                <div>
                  <div className={`text-3xl font-semibold tabular-nums leading-none ${toneClass(quote.change_pct)}`}>
                    {formatPrice(quote.last)}
                  </div>
                  <div className={`mt-1 text-sm tabular-nums ${toneClass(quote.change_pct)}`}>
                    {quote.change != null ? `${quote.change > 0 ? "+" : ""}${formatPrice(quote.change)}` : "—"}
                    {" "}
                    ({formatPct(quote.change_pct)})
                  </div>
                  <div className="mt-1 text-xs text-muted-foreground">
                    {t("markets.atClose")}
                    {sessionTime ? ` · ${sessionTime}` : ""}
                  </div>
                </div>

                <div>
                  <div className="text-2xl font-semibold tabular-nums leading-none">
                    {formatAmount(quote.amount)}
                  </div>
                  <div className="mt-1 text-sm tabular-nums text-muted-foreground">
                    {formatVolume(quote.volume)} {t("markets.colVolume")}
                  </div>
                  <div className="mt-1 text-xs text-muted-foreground">{t("markets.turnover")}</div>
                </div>
              </div>
            </header>

            <div role="tablist" className="flex gap-0 overflow-x-auto px-2 sm:px-4">
              {tabs.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  role="tab"
                  aria-selected={tab === item.id}
                  onClick={() => setTab(item.id)}
                  className={cn(
                    "relative shrink-0 px-3 py-2.5 text-sm transition-colors",
                    tab === item.id
                      ? "font-medium text-foreground after:absolute after:inset-x-2 after:bottom-0 after:h-0.5 after:rounded-full after:bg-primary"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  {item.label}
                </button>
              ))}
            </div>
          </>
        ) : null}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-6">
        {detail && quote && tab === "overview" ? (
          <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-4">
            <MarketAssessmentCard symbol={symbol} name={detail.name} />

            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              {[
                [t("markets.open"), quote.open],
                [t("markets.high"), quote.high],
                [t("markets.low"), quote.low],
                [t("markets.prevClose"), quote.prev_close],
              ].map(([label, value]) => (
                <div key={String(label)} className="rounded-lg border bg-card px-3 py-2.5">
                  <div className="text-[11px] text-muted-foreground">{label}</div>
                  <div className="mt-1 text-base font-semibold tabular-nums">{formatPrice(value as number | null)}</div>
                </div>
              ))}
            </div>

            <section className="rounded-xl border bg-card p-3 sm:p-4">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <div role="group" className="flex flex-wrap items-center gap-1 rounded-md border bg-muted/30 p-0.5">
                  {CHART_PERIODS.map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => setPeriod(item.id)}
                      className={cn(
                        "rounded px-2.5 py-1 text-xs font-medium tabular-nums transition-colors",
                        period === item.id
                          ? "bg-background text-foreground shadow-sm"
                          : "text-muted-foreground hover:text-foreground",
                      )}
                    >
                      {item.label}
                    </button>
                  ))}
                </div>
                <button
                  type="button"
                  onClick={() => void load(period)}
                  className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
                >
                  <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
                  {t("markets.refresh")}
                </button>
              </div>
              {loading && !bars.length ? (
                <div className="flex h-64 flex-col items-center justify-center gap-2 text-sm text-muted-foreground">
                  <Loader2 className="h-5 w-5 animate-spin" />
                  <span>{t("markets.downloadingBars")}</span>
                </div>
              ) : bars.length ? (
                <div className={cn("relative", loading && "opacity-60")}>
                  <CandlestickChart
                    key={`${symbol}-${period}`}
                    data={bars}
                    height={520}
                    marketMode
                    barPeriod={period}
                  />
                </div>
              ) : (
                <div className="flex h-64 flex-col items-center justify-center gap-2 px-4 text-center text-sm text-muted-foreground">
                  <span>
                    {detail?.downloaded
                      ? t("markets.downloadStillEmpty")
                      : t("markets.noBars")}
                  </span>
                  {typeof detail?.download_status === "string" && detail.download_status ? (
                    <span className="max-w-lg text-xs text-danger/80">{detail.download_status}</span>
                  ) : null}
                </div>
              )}
            </section>
          </div>
        ) : null}

        {detail && quote && tab === "narratives" ? (
          <div className="mx-auto flex w-full max-w-[820px] flex-col gap-3">
            <p className="text-sm text-muted-foreground">{t("markets.narrativesHint")}</p>
            {insights.narratives.length ? (
              insights.narratives.map((item) => <InsightCard key={item.id} item={item} />)
            ) : (
              <div className="rounded-xl border bg-card p-6 text-sm text-muted-foreground">
                {loading ? t("markets.downloadingBars") : t("markets.narrativesEmpty")}
              </div>
            )}
          </div>
        ) : null}

        {detail && quote && tab === "anomalies" ? (
          <div className="mx-auto flex w-full max-w-[820px] flex-col gap-4">
            {insights.anomalies.length ? (
              insights.anomalies.map((item) => <AnomalyCard key={`${item.id}-${item.time}`} item={item} />)
            ) : (
              <div className="rounded-2xl border bg-card p-8 text-center text-sm text-muted-foreground">
                {loading ? t("markets.downloadingBars") : t("markets.anomaliesEmpty")}
              </div>
            )}
          </div>
        ) : null}

        {detail && quote && tab === "news" ? (
          <div className="mx-auto w-full max-w-[820px]">
            {newsLoading ? (
              <div className="flex h-48 items-center justify-center text-muted-foreground">
                <Loader2 className="h-5 w-5 animate-spin" />
              </div>
            ) : null}
            {newsError ? (
              <div className="rounded-2xl border border-danger/30 bg-danger/5 p-4 text-sm text-danger">
                {newsError}
              </div>
            ) : null}
            {!newsLoading && !newsError && newsArticles.length ? (
              <div className="divide-y rounded-2xl border bg-card">
                {newsArticles.map((article, index) => (
                  <NewsArticleRow key={`${article.url || article.title}-${index}`} article={article} />
                ))}
              </div>
            ) : null}
            {!newsLoading && !newsError && !newsArticles.length ? (
              <div className="rounded-2xl border bg-card p-8 text-center text-sm text-muted-foreground">
                {t("markets.newsEmpty")}
              </div>
            ) : null}
          </div>
        ) : null}

        {detail && quote && tab === "earnings" ? (
          <EarningsPanel
            name={detail.name}
            loading={earningsLoading}
            error={earningsError}
            data={earningsData}
            releaseNews={earningsReleaseNews}
            newsLoading={newsLoading}
            askAgentHref={askAgentHref}
          />
        ) : null}

        {detail && quote && tab === "peers" ? (
          <PeersPanel
            name={detail.name}
            quote={quote}
            loading={peersLoading}
            error={peersError}
            data={peersData}
            askAgentHref={askAgentHref}
          />
        ) : null}
      </div>

      <MarketSearchDialog
        open={searchOpen}
        onClose={() => setSearchOpen(false)}
        onSelect={(next, name) => {
          pushRecentSymbol(next, name);
          navigate(`/markets/${encodeURIComponent(next)}`);
        }}
        watchlistSymbols={watchlist.symbolSet}
        onToggleWatchlist={(sym, name) => watchlist.toggle(sym, name)}
      />
    </div>
  );
}

export function Markets() {
  const { symbol } = useParams();
  if (symbol) return <MarketsDetail />;
  return <MarketsList />;
}
