/** A 股行情涨跌色：涨红、跌绿（方向色，与 P&L 盈亏色语义不同） */

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function hslToHex(hsl: string): string {
  if (!hsl) return "";
  const [h, s, l] = hsl.split(/\s+/).map(parseFloat);
  if (isNaN(h)) return "";
  const a = (s / 100) * Math.min(l / 100, 1 - l / 100);
  const f = (n: number) => {
    const k = (n + h / 30) % 12;
    const color = l / 100 - a * Math.max(Math.min(k - 3, 9 - k, 1), -1);
    return Math.round(255 * color).toString(16).padStart(2, "0");
  };
  return `#${f(0)}${f(8)}${f(4)}`;
}

export interface MarketDirectionColors {
  upColor: string;
  downColor: string;
  volumeUp: string;
  volumeDown: string;
}

/** K 线/成交量用：固定涨红跌绿，不依赖界面语言 */
export function getMarketDirectionColors(): MarketDirectionColors {
  const upColor = hslToHex(cssVar("--danger")) || "#ef4444";
  const downColor = hslToHex(cssVar("--success")) || "#22c55e";
  return {
    upColor,
    downColor,
    volumeUp: `${upColor}66`,
    volumeDown: `${downColor}66`,
  };
}

export function marketChangeTextClass(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value) || value === 0) return "text-muted-foreground";
  return value > 0 ? "text-danger" : "text-success";
}

export function marketInsightToneClass(tone: "up" | "down" | "neutral"): string {
  if (tone === "up") return "bg-danger/10 text-danger";
  if (tone === "down") return "bg-success/10 text-success";
  return "bg-primary/10 text-primary";
}
