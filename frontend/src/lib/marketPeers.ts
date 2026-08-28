import type { MarketPeersResponse } from "@/lib/api";

export function formatPeerChange(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(2)}%`;
}

export function averagePeerChange(
  peers: MarketPeersResponse["peers"],
  target?: MarketPeersResponse["target"] | null,
): number | null {
  const values = [...peers, ...(target ? [target] : [])]
    .map((row) => row.change_pct)
    .filter((value): value is number => value != null && Number.isFinite(value));
  if (!values.length) return null;
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

export function formatBoardChange(value: number | null | undefined): string {
  return formatPeerChange(value);
}
