import { useEffect, useRef, useState, useMemo, useCallback } from "react";
import i18n from "@/i18n";
import { cn } from "@/lib/utils";
import { ChevronDown } from "lucide-react";
import type { PriceBar, TradeMarker, IndicatorPoint } from "@/lib/api";
import { calcMA, calcBOLL, calcMACD, calcRSI, calcKDJ, calcEMA } from "@/lib/indicators";
import { getChartTheme } from "@/lib/chart-theme";
import { getMarketDirectionColors } from "@/lib/market-colors";
import { abbreviateNum } from "@/lib/formatters";
import { echarts, CHART_GROUP, connectCharts } from "@/lib/echarts";
import { escapeHtml } from "@/lib/escapeHtml";
import { useThemeDark } from "@/lib/theme-store";

type Sub = "vol" | "macd" | "rsi" | "kdj";
type Range = "1M" | "3M" | "6M" | "1Y" | "ALL";
type Overlay = "ma5" | "ma10" | "ma20" | "ma60" | "ema12" | "ema26" | "boll";

const OVERLAY_OPTIONS: { id: Overlay; label: string; group: string }[] = [
  { id: "ma5", label: "MA5", group: "MA" },
  { id: "ma10", label: "MA10", group: "MA" },
  { id: "ma20", label: "MA20", group: "MA" },
  { id: "ma60", label: "MA60", group: "MA" },
  { id: "ema12", label: "EMA12", group: "MA" },
  { id: "ema26", label: "EMA26", group: "MA" },
  { id: "boll", label: "BOLL", group: "Channel" },
];

const RANGE_BARS: Record<Range, number> = { "1M": 22, "3M": 63, "6M": 126, "1Y": 252, ALL: Infinity };
const RANGE_I18N: Record<Range, string> = {
  "1M": "charts.range1M",
  "3M": "charts.range3M",
  "6M": "charts.range6M",
  "1Y": "charts.range1Y",
  ALL: "charts.rangeAll",
};
const OVERLAY_COLORS = ["#f59e0b", "#8b5cf6", "#3b82f6", "#ec4899", "#10b981", "#f97316", "#6366f1"];

function defaultRangeForPeriod(barPeriod?: string): Range {
  switch (barPeriod) {
    case "1d":
      return "6M";
    case "60m":
      return "3M";
    case "15m":
    case "5m":
    case "1m":
      return "1M";
    default:
      return "6M";
  }
}

function isIntradayPeriod(barPeriod?: string): boolean {
  return Boolean(barPeriod && barPeriod !== "1d");
}

function formatMarketAxisLabel(value: string, intraday: boolean): string {
  if (intraday) {
    const timeMatch = value.match(/\b(\d{2}:\d{2})\b/);
    if (timeMatch) return timeMatch[1];
  }
  if (/^\d{4}-\d{2}-\d{2}/.test(value)) return value.slice(5, 10);
  if (value.length >= 10) return value.slice(5, 10);
  return value;
}

function priceAxisDecimals(data: PriceBar[]): number {
  const closes = data.map((d) => d.close).filter(Number.isFinite);
  if (!closes.length) return 2;
  const max = Math.max(...closes);
  return max >= 100 ? 2 : 3;
}

/** Normalize QMT/Bridge bar times (epoch ms, YYYYMMDD, ISO) for chart axis labels. */
export function formatChartBarTime(raw: string | number | null | undefined): string {
  if (raw == null || raw === "") return "";
  if (typeof raw === "number" && Number.isFinite(raw)) {
    const n = Math.trunc(raw);
    if (n >= 1e12) {
      const d = new Date(n);
      if (Number.isNaN(d.getTime())) return String(raw);
      const withTime = !(
        (d.getHours() === 0 && d.getMinutes() === 0) ||
        (d.getHours() === 15 && d.getMinutes() === 0)
      );
      return formatLocalDate(d, withTime);
    }
    if (n >= 1e9) {
      const d = new Date(n * 1000);
      if (Number.isNaN(d.getTime())) return String(raw);
      const withTime = !(
        (d.getHours() === 0 && d.getMinutes() === 0) ||
        (d.getHours() === 15 && d.getMinutes() === 0)
      );
      return formatLocalDate(d, withTime);
    }
    const digits = String(n);
    if (digits.length === 8) return `${digits.slice(0, 4)}-${digits.slice(4, 6)}-${digits.slice(6, 8)}`;
    if (digits.length === 14) {
      return `${digits.slice(0, 4)}-${digits.slice(4, 6)}-${digits.slice(6, 8)} ${digits.slice(8, 10)}:${digits.slice(10, 12)}`;
    }
    return digits;
  }

  const text = String(raw).trim();
  if (/^\d{13}$/.test(text)) return formatChartBarTime(Number(text));
  if (/^\d{10}$/.test(text)) return formatChartBarTime(Number(text));
  if (/^\d{8}$/.test(text)) return `${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6, 8)}`;
  if (/^\d{14}$/.test(text)) {
    return `${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6, 8)} ${text.slice(8, 10)}:${text.slice(10, 12)}`;
  }
  const normalized = text.replace("T", " ").replace(/\//g, "-");
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(normalized)) return normalized.slice(0, 16);
  if (/^\d{4}-\d{2}-\d{2}/.test(normalized)) return normalized.slice(0, 10);
  return text;
}

function formatLocalDate(d: Date, withTime: boolean) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  if (!withTime) return `${y}-${m}-${day}`;
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${y}-${m}-${day} ${hh}:${mm}`;
}

interface Props {
  data: PriceBar[];
  markers?: TradeMarker[];
  indicators?: Record<string, IndicatorPoint[]>;
  height?: number;
  /** A 股涨跌色：涨红跌绿（markets 等 A 股场景固定使用） */
  cnDirection?: boolean;
  /** 行情页模式：精简控件、默认缩放、中文 tooltip */
  marketMode?: boolean;
  /** 与 markets 周期联动：1d | 60m | 15m | 5m | 1m */
  barPeriod?: string;
}

export function CandlestickChart({
  data,
  markers,
  indicators,
  height = 500,
  cnDirection = false,
  marketMode = false,
  barPeriod,
}: Props) {
  const useCnColors = cnDirection || marketMode;
  const intraday = isIntradayPeriod(barPeriod);
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ReturnType<typeof echarts.init> | null>(null);
  const [sub, setSub] = useState<Sub>("vol");
  const [range, setRange] = useState<Range>(() => (marketMode ? defaultRangeForPeriod(barPeriod) : "ALL"));
  const [overlays, setOverlays] = useState<Set<Overlay>>(
    () => new Set(marketMode ? ["ma5", "ma10", "ma20"] : ["ma5", "ma20"]),
  );
  const [showMenu, setShowMenu] = useState(false);
  const dark = useThemeDark();

  useEffect(() => {
    if (marketMode) setRange(defaultRangeForPeriod(barPeriod));
  }, [marketMode, barPeriod]);

  const toggleOverlay = useCallback((id: Overlay) => {
    setOverlays(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }, []);

  // Memoize base data arrays — only recompute when raw data changes
  const baseData = useMemo(() => {
    const dates = data.map(d => formatChartBarTime(d.time));
    const closes = data.map(d => d.close);
    const highs = data.map(d => d.high);
    const lows = data.map(d => d.low);
    const opens = data.map(d => d.open);
    const candle = data.map(d => [d.open, d.close, d.low, d.high]);
    return { dates, closes, highs, lows, opens, candle };
  }, [data]);

  // Memoize indicator calculations — only recompute when data changes (not on overlay toggle)
  const indicatorCache = useMemo(() => ({
    ma5: calcMA(baseData.closes, 5),
    ma10: calcMA(baseData.closes, 10),
    ma20: calcMA(baseData.closes, 20),
    ma60: calcMA(baseData.closes, 60),
    ema12: calcEMA(baseData.closes, 12),
    ema26: calcEMA(baseData.closes, 26),
    boll: calcBOLL(baseData.closes, 20, 2),
    macd: calcMACD(baseData.closes),
    rsi: calcRSI(baseData.closes),
    kdj: calcKDJ(baseData.highs, baseData.lows, baseData.closes),
  }), [baseData]);

  // Memoize backend indicator series with Map lookup (O(1) instead of O(n) find)
  const extraIndicators = useMemo(() => {
    if (!indicators) return [];
    return Object.entries(indicators).map(([name, points]) => {
      const lookup = new Map(points.map(p => [p.time, p.value]));
      return { name: name.toUpperCase(), values: baseData.dates.map(d => lookup.get(d) ?? null) };
    });
  }, [indicators, baseData.dates]);

  // Init chart instance — only on mount/unmount and dark mode change
  useEffect(() => {
    if (!containerRef.current || data.length === 0) return;
    const chart = echarts.init(containerRef.current);
    chart.group = CHART_GROUP;
    connectCharts();
    chartRef.current = chart;

    let resizeFrame: number | null = null;
    const ro = new ResizeObserver(() => {
      if (resizeFrame !== null) cancelAnimationFrame(resizeFrame);
      resizeFrame = requestAnimationFrame(() => {
        resizeFrame = null;
        chart.resize();
      });
    });
    ro.observe(containerRef.current);
    return () => {
      ro.disconnect();
      if (resizeFrame !== null) cancelAnimationFrame(resizeFrame);
      chart.dispose();
      chartRef.current = null;
    };
  }, [data.length === 0, dark]); // only re-init when going empty↔non-empty or theme changes

  // Update chart options — setOption on existing instance, no dispose
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || data.length === 0) return;

    const baseTheme = getChartTheme();
    const direction = useCnColors ? getMarketDirectionColors() : baseTheme;
    const t = useCnColors
      ? {
          ...baseTheme,
          upColor: direction.upColor,
          downColor: direction.downColor,
          volumeUp: direction.volumeUp,
          volumeDown: direction.volumeDown,
        }
      : baseTheme;
    const { dates, closes, opens, candle } = baseData;
    const priceDecimals = marketMode ? priceAxisDecimals(data) : 2;
    const ohlcLabels = marketMode
      ? {
          open: i18n.t("charts.ohlcOpen"),
          high: i18n.t("charts.ohlcHigh"),
          low: i18n.t("charts.ohlcLow"),
          close: i18n.t("charts.ohlcClose"),
          vol: i18n.t("charts.ohlcVol"),
        }
      : { open: "O", high: "H", low: "L", close: "C", vol: "Vol" };

    // Overlay series
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const overlaySeries: any[] = [];
    const legendNames: string[] = ["K"];
    let colorIdx = 0;

    const overlayMap: Record<string, { name: string; data: (number | null)[] }> = {
      ma5: { name: "MA5", data: indicatorCache.ma5 },
      ma10: { name: "MA10", data: indicatorCache.ma10 },
      ma20: { name: "MA20", data: indicatorCache.ma20 },
      ma60: { name: "MA60", data: indicatorCache.ma60 },
      ema12: { name: "EMA12", data: indicatorCache.ema12 },
      ema26: { name: "EMA26", data: indicatorCache.ema26 },
    };

    for (const [key, { name, data: lineData }] of Object.entries(overlayMap)) {
      if (overlays.has(key as Overlay)) {
        overlaySeries.push({ name, type: "line", data: lineData, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { color: OVERLAY_COLORS[colorIdx], width: 1 } });
        legendNames.push(name);
        colorIdx++;
      }
    }

    if (overlays.has("boll")) {
      const boll = indicatorCache.boll;
      overlaySeries.push(
        { name: "BOLL+", type: "line", data: boll.upper, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { color: t.bollColor, width: 0.8, type: "dashed" } },
        { name: "BOLL", type: "line", data: boll.mid, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { color: t.bollColor, width: 1 } },
        { name: "BOLL-", type: "line", data: boll.lower, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { color: t.bollColor, width: 0.8, type: "dashed" } },
      );
      legendNames.push("BOLL");
    }

    // Trade markers (name renders as markPoint tooltip HTML, so every
    // artifact-derived field is escaped before interpolation)
    const marks = (markers || []).map(m => ({
      coord: [m.time, m.price],
      value: m.side === "BUY" ? "B" : "S",
      name: [
        `${escapeHtml(m.side)} @ ${escapeHtml(String(m.price))}`,
        m.qty ? `Qty: ${escapeHtml(String(m.qty))}` : "",
        escapeHtml(m.reason || ""),
      ].filter(Boolean).join("\n"),
      itemStyle: { color: m.side === "BUY" ? t.upColor : t.downColor },
      label: { color: "#fff", fontSize: 10, fontWeight: "bold" as const },
    }));

    // Volume
    const vol = data.map((d, i) => ({
      value: d.volume,
      itemStyle: { color: closes[i] >= opens[i] ? t.volumeUp : t.volumeDown },
    }));

    // Sub-chart
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    let subSeries: any[] = [];
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    let subYAxis: any = { scale: true, gridIndex: 1, splitLine: { lineStyle: { color: t.gridColor } }, axisLabel: { color: t.textColor, fontSize: 10 } };

    if (sub === "vol") {
      subSeries = [{ name: "Vol", type: "bar", data: vol, xAxisIndex: 1, yAxisIndex: 1 }];
      subYAxis = { ...subYAxis, axisLabel: { ...subYAxis.axisLabel, formatter: (v: number) => abbreviateNum(v) } };
      legendNames.push("Vol");
    } else if (sub === "macd") {
      const m = indicatorCache.macd;
      subSeries = [
        { name: "DIF", type: "line", data: m.dif, xAxisIndex: 1, yAxisIndex: 1, symbol: "none", lineStyle: { width: 1, color: t.infoColor } },
        { name: "DEA", type: "line", data: m.signal, xAxisIndex: 1, yAxisIndex: 1, symbol: "none", lineStyle: { width: 1, color: t.warningColor } },
        { name: "MACD", type: "bar", data: m.histogram.map(v => ({ value: v ?? 0, itemStyle: { color: (v ?? 0) >= 0 ? t.upColor : t.downColor } })), xAxisIndex: 1, yAxisIndex: 1 },
      ];
      legendNames.push("DIF", "DEA", "MACD");
    } else if (sub === "rsi") {
      subSeries = [{ name: "RSI", type: "line", data: indicatorCache.rsi, xAxisIndex: 1, yAxisIndex: 1, symbol: "none", lineStyle: { width: 1.5, color: t.infoColor } }];
      subYAxis = { ...subYAxis, min: 0, max: 100 };
      legendNames.push("RSI");
    } else {
      const kdj = indicatorCache.kdj;
      subSeries = [
        { name: "%K", type: "line", data: kdj.k, xAxisIndex: 1, yAxisIndex: 1, symbol: "none", lineStyle: { width: 1, color: t.infoColor } },
        { name: "%D", type: "line", data: kdj.d, xAxisIndex: 1, yAxisIndex: 1, symbol: "none", lineStyle: { width: 1, color: t.warningColor } },
        { name: "%J", type: "line", data: kdj.j, xAxisIndex: 1, yAxisIndex: 1, symbol: "none", lineStyle: { width: 1, color: "#a855f7" } },
      ];
      legendNames.push("%K", "%D", "%J");
    }

    // Backend custom indicators (Map-based O(1) lookup)
    const extraSeries = extraIndicators.map((ind, i) => {
      legendNames.push(ind.name);
      return { name: ind.name, type: "line" as const, data: ind.values, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, color: OVERLAY_COLORS[(colorIdx + i) % OVERLAY_COLORS.length], type: "dashed" as const } };
    });

    const maxBars = RANGE_BARS[range];
    const defaultStart = maxBars >= data.length ? 0 : Math.max(0, 100 - (maxBars / data.length) * 100);

    chart.setOption({
      backgroundColor: "transparent",
      animation: !marketMode,
      axisPointer: {
        type: "cross",
        link: [{ xAxisIndex: [0, 1] }],
        lineStyle: { color: t.axisColor, width: 1, type: "dashed" },
        crossStyle: { color: t.axisColor, width: 1, type: "dashed" },
        label: {
          backgroundColor: t.tooltipBg,
          borderColor: t.tooltipBorder,
          color: t.tooltipText,
          fontSize: 10,
          padding: [2, 4],
        },
      },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        backgroundColor: t.tooltipBg,
        borderColor: t.tooltipBorder,
        textStyle: { color: t.tooltipText, fontSize: 11 },
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        formatter: (params: any) => {
          if (!Array.isArray(params) || !params.length) return "";
          let html = `<b>${params[0].axisValue}</b>`;
          for (const p of params) {
            if (p.seriesName === "K" && Array.isArray(p.value)) {
              const [open, close, low, high] = p.value;
              const chg = close - open;
              const pct = open ? ((chg / open) * 100).toFixed(2) : "0.00";
              const clr = chg >= 0 ? t.upColor : t.downColor;
              html += `<br/>${ohlcLabels.open}: ${open.toFixed(priceDecimals)}&nbsp; ${ohlcLabels.high}: ${high.toFixed(priceDecimals)}`;
              html += `<br/>${ohlcLabels.low}: ${low.toFixed(priceDecimals)}&nbsp; ${ohlcLabels.close}: <span style="color:${clr}"><b>${close.toFixed(priceDecimals)}</b> ${chg >= 0 ? "+" : ""}${chg.toFixed(2)} (${chg >= 0 ? "+" : ""}${pct}%)</span>`;
            } else if (p.seriesName === "Vol") {
              html += `<br/>${ohlcLabels.vol}: ${abbreviateNum(Number(p.value))}`;
            } else if (p.value != null) {
              html += `<br/>${p.marker} ${p.seriesName}: ${Number(p.value).toFixed(priceDecimals)}`;
            }
          }
          return html;
        },
      },
      toolbox: marketMode
        ? undefined
        : {
            feature: { saveAsImage: { title: "Save" }, dataZoom: { title: { zoom: "Zoom", back: "Reset" } }, restore: { title: "Reset" } },
            right: 8,
            top: 0,
            iconStyle: { borderColor: t.textColor },
          },
      legend: {
        data: legendNames,
        textStyle: { color: t.textColor, fontSize: 10 },
        right: marketMode ? 4 : 80,
        top: 2,
        type: "scroll",
        itemWidth: 12,
        itemHeight: 8,
        itemGap: 8,
      },
      grid: marketMode
        ? [
            { left: 4, right: 12, top: 28, height: "62%", containLabel: true },
            { left: 4, right: 12, top: "72%", height: "20%", containLabel: true },
          ]
        : [
            { left: 8, right: 8, top: 36, height: "55%", containLabel: true },
            { left: 8, right: 8, top: "66%", height: "22%", containLabel: true },
          ],
      xAxis: [
        {
          type: "category",
          data: dates,
          gridIndex: 0,
          axisLine: { lineStyle: { color: t.axisColor } },
          axisLabel: {
            color: t.textColor,
            fontSize: 10,
            hideOverlap: true,
            formatter: marketMode ? (value: string) => formatMarketAxisLabel(value, intraday) : undefined,
          },
          axisTick: { show: !marketMode },
          boundaryGap: true,
        },
        {
          type: "category",
          data: dates,
          gridIndex: 1,
          axisLine: { lineStyle: { color: t.axisColor } },
          axisLabel: { show: false },
          boundaryGap: true,
        },
      ],
      yAxis: [
        {
          scale: true,
          gridIndex: 0,
          splitNumber: marketMode ? 4 : 5,
          splitLine: { lineStyle: { color: t.gridColor } },
          axisLabel: {
            color: t.textColor,
            fontSize: 10,
            formatter: (value: number) => value.toFixed(priceDecimals),
          },
        },
        subYAxis,
      ],
      dataZoom: marketMode
        ? [{ type: "inside", xAxisIndex: [0, 1], start: defaultStart, end: 100 }]
        : [
            { type: "inside", xAxisIndex: [0, 1], start: defaultStart, end: 100 },
            { type: "slider", xAxisIndex: [0, 1], bottom: 4, height: 20, labelFormatter: (val: string) => val },
          ],
      series: [
        {
          name: "K",
          type: "candlestick",
          data: candle,
          xAxisIndex: 0,
          yAxisIndex: 0,
          barMaxWidth: marketMode ? 14 : 10,
          barMinWidth: marketMode ? 2 : 1,
          itemStyle: { color: t.upColor, color0: t.downColor, borderColor: t.upColor, borderColor0: t.downColor },
          markPoint: marks.length > 0 ? { data: marks, symbolSize: 28, tooltip: { formatter: (p: { name?: string; value?: string }) => p.name || p.value || "" } } : undefined,
        },
        ...overlaySeries,
        ...extraSeries,
        ...subSeries,
      ],
    }, true);
  }, [data, markers, baseData, indicatorCache, extraIndicators, sub, range, overlays, dark, useCnColors, marketMode, barPeriod, intraday]);

  if (data.length === 0) {
    return <div className="text-muted-foreground text-sm p-4">{i18n.t("charts.noPriceData")}</div>;
  }

  const rangeOptions: Range[] = marketMode ? ["1M", "3M", "6M", "1Y", "ALL"] : ["1M", "3M", "6M", "1Y", "ALL"];
  const subOptions: { id: Sub; label: string }[] = marketMode
    ? [
        { id: "vol", label: i18n.t("charts.subVol") },
        { id: "macd", label: i18n.t("charts.subMacd") },
        { id: "rsi", label: i18n.t("charts.subRsi") },
        { id: "kdj", label: i18n.t("charts.subKdj") },
      ]
    : [
        { id: "vol", label: "vol" },
        { id: "macd", label: "macd" },
        { id: "rsi", label: "rsi" },
        { id: "kdj", label: "kdj" },
      ];

  return (
    <div>
      <div className={cn("mb-1 flex flex-wrap items-center gap-2", marketMode && "rounded-lg border bg-muted/20 px-2 py-1.5")}>
        {/* Time range */}
        <div className="flex gap-0.5">
          {rangeOptions.map((r) => (
            <button
              key={r}
              type="button"
              onClick={() => setRange(r)}
              className={cn(
                "rounded px-2 py-0.5 text-[10px] font-medium tabular-nums transition-colors",
                range === r
                  ? "bg-primary/15 text-primary"
                  : "text-muted-foreground/60 hover:text-foreground",
              )}
            >
              {marketMode ? i18n.t(RANGE_I18N[r]) : r}
            </button>
          ))}
        </div>

        <div className="h-3 w-px bg-border/40" />

        {/* Indicator dropdown */}
        <div className="relative">
          <button
            type="button"
            onClick={() => setShowMenu(!showMenu)}
            className="flex items-center gap-1 rounded px-2 py-0.5 text-[10px] text-muted-foreground transition-colors hover:bg-muted/50 hover:text-foreground"
          >
            {marketMode ? i18n.t("charts.indicators") : "Indicators"} ({overlays.size}) <ChevronDown className="h-3 w-3" />
          </button>
          {showMenu && (
            <div className="absolute left-0 top-full z-50 mt-1 min-w-[160px] rounded-lg border bg-card p-2 shadow-lg" onMouseLeave={() => setShowMenu(false)}>
              {["MA", "Channel"].map(group => (
                <div key={group}>
                  <p className="px-1 pt-1 text-[9px] uppercase tracking-wider text-muted-foreground/50">{group}</p>
                  {OVERLAY_OPTIONS.filter(o => o.group === group).map(o => (
                    <label key={o.id} className="flex cursor-pointer items-center gap-2 rounded px-1 py-0.5 hover:bg-muted/30">
                      <input type="checkbox" checked={overlays.has(o.id)} onChange={() => toggleOverlay(o.id)} className="h-3 w-3 rounded accent-primary" />
                      <span className="text-xs">{o.label}</span>
                    </label>
                  ))}
                </div>
              ))}
              <div className="mt-1 border-t pt-1">
                <button
                  type="button"
                  onClick={() => { setOverlays(new Set()); setShowMenu(false); }}
                  className="w-full rounded px-1 py-0.5 text-left text-[10px] text-muted-foreground hover:bg-muted/30 hover:text-foreground"
                >
                  {marketMode ? i18n.t("charts.bareK") : "Bare K (clear all)"}
                </button>
              </div>
            </div>
          )}
        </div>

        <div className="h-3 w-px bg-border/40" />

        {/* Sub-chart selector */}
        <div className="flex gap-0.5">
          {subOptions.map(({ id, label }) => (
            <button
              key={id}
              type="button"
              onClick={() => setSub(id)}
              className={cn(
                "rounded px-2 py-0.5 text-[10px] font-medium uppercase transition-colors",
                sub === id
                  ? "bg-primary/15 text-primary"
                  : "text-muted-foreground/60 hover:text-foreground",
              )}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
      <div ref={containerRef} style={{ height }} />
    </div>
  );
}
