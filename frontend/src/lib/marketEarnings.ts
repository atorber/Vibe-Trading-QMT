import type { MarketConsensusEps, MarketEarningsReport, MarketNewsArticle } from "@/lib/api";

export type EarningsStage = "pre" | "release" | "transcript" | "post";

const EARNINGS_NEWS_RE =
  /财报|业绩|季报|年报|中报|半年|一季|三季|披露|净利润|EPS|eps|盈利预告|业绩预告|快报/i;

export function isEarningsNews(article: MarketNewsArticle): boolean {
  const text = `${article.title ?? ""} ${article.snippet ?? ""}`;
  return EARNINGS_NEWS_RE.test(text);
}

export function filterEarningsNews(articles: MarketNewsArticle[]): MarketNewsArticle[] {
  return articles.filter(isEarningsNews);
}

export function fiscalYearOptions(consensus: MarketConsensusEps[]): string[] {
  const years = consensus
    .map((row) => String(row.fiscal_year ?? "").trim())
    .filter(Boolean);
  if (years.length) return years;
  return [String(new Date().getFullYear())];
}

export function latestReportDate(reports: MarketEarningsReport[]): string | null {
  for (const report of reports) {
    const date = report.publish_date?.trim();
    if (date) return date;
  }
  return null;
}

export function consensusForYear(
  consensus: MarketConsensusEps[],
  year: string,
): number | null {
  const row = consensus.find((item) => String(item.fiscal_year) === year);
  const value = row?.consensus_eps;
  return value != null && Number.isFinite(value) ? value : null;
}

export type WatchingPoint =
  | { type: "consensus"; year: string; eps: number }
  | { type: "ratings"; ratings: string[] }
  | { type: "sellerEps"; avg: number; count: number }
  | { type: "coverage"; count: number };

export function buildWatchingPoints(
  reports: MarketEarningsReport[],
  consensus: MarketConsensusEps[],
  year: string,
): WatchingPoint[] {
  const points: WatchingPoint[] = [];
  const eps = consensusForYear(consensus, year);
  if (eps != null) {
    points.push({ type: "consensus", year, eps });
  }
  const rated = reports.filter((r) => r.rating);
  if (rated.length) {
    const ratings = [...new Set(rated.map((r) => r.rating).filter(Boolean))].slice(0, 4) as string[];
    if (ratings.length) points.push({ type: "ratings", ratings });
  }
  const withEps = reports.filter((r) => r.eps_forecast?.this_year != null);
  if (withEps.length) {
    const avg =
      withEps.reduce((sum, r) => sum + Number(r.eps_forecast?.this_year ?? 0), 0) / withEps.length;
    if (Number.isFinite(avg)) {
      points.push({ type: "sellerEps", avg, count: withEps.length });
    }
  }
  if (reports.length) {
    points.push({ type: "coverage", count: reports.length });
  }
  return points.slice(0, 4);
}
