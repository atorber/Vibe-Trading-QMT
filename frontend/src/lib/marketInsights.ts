import type { MarketQuote, PriceBar } from "@/lib/api";

export type MarketInsightKind = "narrative" | "anomaly";

export type MarketInsight = {
  id: string;
  kind: MarketInsightKind;
  tag: string;
  title: string;
  body: string;
  time?: string;
  tone: "up" | "down" | "neutral";
  /** 异动卡片展示用 */
  price?: number | null;
  changePct?: number | null;
  sessionLabel?: string;
};

function pct(from: number, to: number) {
  if (!Number.isFinite(from) || !Number.isFinite(to) || from === 0) return null;
  return ((to - from) / from) * 100;
}

function fmtPct(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(2)}%`;
}

function avg(values: number[]) {
  if (!values.length) return 0;
  return values.reduce((a, b) => a + b, 0) / values.length;
}

/** Derive Alva-like narratives / anomalies from local QMT bars + quote. */
export function buildMarketInsights(
  name: string,
  quote: MarketQuote,
  bars: PriceBar[],
): { narratives: MarketInsight[]; anomalies: MarketInsight[] } {
  const narratives: MarketInsight[] = [];
  const anomalies: MarketInsight[] = [];
  const series = bars.filter(
    (bar) =>
      Number.isFinite(bar.close) &&
      Number.isFinite(bar.open) &&
      Number.isFinite(bar.high) &&
      Number.isFinite(bar.low),
  );
  if (!series.length) {
    return { narratives, anomalies };
  }

  const last = series[series.length - 1];
  const prev = series.length > 1 ? series[series.length - 2] : null;
  const dayPct =
    quote.change_pct ??
    (prev ? pct(prev.close, last.close) : pct(last.open, last.close));
  const rangePct = pct(last.low, last.high);
  const vols = series.slice(-21, -1).map((bar) => Number(bar.volume || 0)).filter((v) => v > 0);
  const volAvg = avg(vols);
  const lastVol = Number(last.volume || 0);
  const volRatio = volAvg > 0 ? lastVol / volAvg : null;

  // Session narrative
  if (dayPct != null) {
    const tone = dayPct > 0.15 ? "up" : dayPct < -0.15 ? "down" : "neutral";
    narratives.push({
      id: "session-move",
      kind: "narrative",
      tag: "Session Move",
      title:
        tone === "up"
          ? `${name} 上涨 ${fmtPct(dayPct)}`
          : tone === "down"
            ? `${name} 回落 ${fmtPct(dayPct)}`
            : `${name} 窄幅震荡 ${fmtPct(dayPct)}`,
      body: [
        `最新价 ${Number(quote.last ?? last.close).toFixed(2)}，开 ${Number(quote.open ?? last.open).toFixed(2)} / 高 ${Number(quote.high ?? last.high).toFixed(2)} / 低 ${Number(quote.low ?? last.low).toFixed(2)}。`,
        rangePct != null ? `当日振幅 ${fmtPct(rangePct)}。` : "",
        volRatio != null ? `成交量约为近 20 日均量的 ${volRatio.toFixed(2)} 倍。` : "",
        "依据本机 QMT Bridge K 线与快照生成，不构成投资建议。",
      ]
        .filter(Boolean)
        .join(" "),
      time: String(last.time || quote.updated_at || ""),
      tone,
    });
  }

  // Multi-day trend narrative (5 / 20 sessions)
  if (series.length >= 6) {
    const d5 = series[series.length - 6];
    const move5 = pct(d5.close, last.close);
    if (move5 != null && Math.abs(move5) >= 3) {
      narratives.push({
        id: "trend-5d",
        kind: "narrative",
        tag: "5D Trend",
        title: move5 > 0 ? `${name} 近 5 个交易日累计上涨 ${fmtPct(move5)}` : `${name} 近 5 个交易日累计下跌 ${fmtPct(move5)}`,
        body: `自 ${d5.time} 收盘 ${d5.close.toFixed(2)} 运行至最新 ${last.close.toFixed(2)}。短线动量${move5 > 0 ? "偏强" : "偏弱"}，需结合量能与板块确认。`,
        time: String(last.time || ""),
        tone: move5 > 0 ? "up" : "down",
      });
    }
  }
  if (series.length >= 21) {
    const d20 = series[series.length - 21];
    const move20 = pct(d20.close, last.close);
    if (move20 != null && Math.abs(move20) >= 5) {
      narratives.push({
        id: "trend-20d",
        kind: "narrative",
        tag: "20D Trend",
        title: move20 > 0 ? `${name} 近 20 日累计上涨 ${fmtPct(move20)}` : `${name} 近 20 日累计下跌 ${fmtPct(move20)}`,
        body: `中期走势自 ${d20.time} 的 ${d20.close.toFixed(2)} 变化至 ${last.close.toFixed(2)}（${fmtPct(move20)}）。`,
        time: String(last.time || ""),
        tone: move20 > 0 ? "up" : "down",
      });
    }
  }

  // Anomalies — scan recent bars newest-first (Alva-style timeline)
  const seen = new Set<string>();
  const pushAnomaly = (item: MarketInsight) => {
    const key = `${item.id}:${item.time || ""}`;
    if (seen.has(key)) return;
    seen.add(key);
    anomalies.push(item);
  };

  const scanFrom = Math.max(1, series.length - 60);
  for (let i = series.length - 1; i >= scanFrom; i -= 1) {
    const bar = series[i];
    const before = series[i - 1];
    const move = pct(before.close, bar.close);
    const barVol = Number(bar.volume || 0);
    const volWindow = series
      .slice(Math.max(0, i - 20), i)
      .map((row) => Number(row.volume || 0))
      .filter((v) => v > 0);
    const barVolAvg = avg(volWindow);
    const barVolRatio = barVolAvg > 0 ? barVol / barVolAvg : null;
    const barRangePct = pct(bar.low, bar.high);
    const gap = bar.open > 0 ? pct(before.close, bar.open) : null;
    const time = String(bar.time || "");
    const price = bar.close;
    const isLatest = i === series.length - 1;

    if (move != null && Math.abs(move) >= 3) {
      const tone = move >= 0 ? "up" : "down";
      pushAnomaly({
        id: "large-move",
        kind: "anomaly",
        tag: "Unusual Price Movement",
        title:
          tone === "up"
            ? `${name} 上涨 ${fmtPct(move)}${isLatest ? "，短线波动显著" : ""}`
            : `${name} 下跌 ${fmtPct(Math.abs(move))}${isLatest ? "，短线波动显著" : ""}`,
        body: isLatest
          ? `相对前收偏离 ${fmtPct(move)}（阈值 |涨跌幅| ≥ 3%）。`
          : `收盘 ${price.toFixed(2)}，相对前收 ${before.close.toFixed(2)} 变动 ${fmtPct(move)}。`,
        time,
        tone,
        price,
        changePct: move,
        sessionLabel: isLatest ? "最新" : undefined,
      });
    }

    if (barVolRatio != null && barVolRatio >= 2.5) {
      pushAnomaly({
        id: "volume-spike",
        kind: "anomaly",
        tag: "Volume Spike",
        title: `${name} 成交量放大至均量 ${barVolRatio.toFixed(2)} 倍`,
        body: `当日量 ${Math.round(barVol).toLocaleString()}，近 20 日均量约 ${Math.round(barVolAvg).toLocaleString()}。`,
        time,
        tone: (move ?? 0) >= 0 ? "up" : "down",
        price,
        changePct: move,
        sessionLabel: isLatest ? "最新" : undefined,
      });
    }

    if (gap != null && Math.abs(gap) >= 2) {
      pushAnomaly({
        id: "gap",
        kind: "anomaly",
        tag: "Gap",
        title: gap > 0 ? `${name} 跳空高开 ${fmtPct(gap)}` : `${name} 跳空低开 ${fmtPct(gap)}`,
        body: `开盘 ${bar.open.toFixed(2)} 相对前收 ${before.close.toFixed(2)} 偏离 ${fmtPct(gap)}。`,
        time,
        tone: gap > 0 ? "up" : "down",
        price: bar.open,
        changePct: gap,
      });
    }

    if (barRangePct != null && barRangePct >= 6) {
      pushAnomaly({
        id: "wide-range",
        kind: "anomaly",
        tag: "Wide Range",
        title: `${name} 振幅扩大至 ${fmtPct(barRangePct)}`,
        body: `最高 ${bar.high.toFixed(2)} / 最低 ${bar.low.toFixed(2)}。`,
        time,
        tone: "neutral",
        price,
        changePct: move,
      });
    }
  }

  // streak (latest only)
  let streak = 0;
  for (let i = series.length - 1; i > 0; i -= 1) {
    const cur = series[i];
    const before = series[i - 1];
    const up = cur.close > before.close;
    if (streak === 0) streak = up ? 1 : -1;
    else if ((streak > 0 && up) || (streak < 0 && !up)) streak += streak > 0 ? 1 : -1;
    else break;
  }
  if (Math.abs(streak) >= 4) {
    pushAnomaly({
      id: "streak",
      kind: "anomaly",
      tag: "Streak",
      title: streak > 0 ? `${name} 连续 ${streak} 日收涨` : `${name} 连续 ${Math.abs(streak)} 日收跌`,
      body: "连续同向收盘可能进入短期趋势或过热阶段，注意回撤与反抽风险。",
      time: String(last.time || ""),
      tone: streak > 0 ? "up" : "down",
      price: last.close,
      changePct: dayPct,
      sessionLabel: "最新",
    });
  }

  return { narratives, anomalies };
}
