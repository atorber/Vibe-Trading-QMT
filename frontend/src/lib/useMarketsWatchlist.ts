import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type MarketWatchlistItem } from "@/lib/api";

export function useMarketsWatchlist() {
  const [symbols, setSymbols] = useState<MarketWatchlistItem[]>([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.getMarketsWatchlist();
      setSymbols(data.symbols ?? []);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const symbolSet = useMemo(
    () => new Set(symbols.map((row) => row.symbol.toUpperCase())),
    [symbols],
  );

  const add = useCallback(async (symbol: string, name?: string) => {
    const data = await api.addMarketsWatchlist(symbol, name);
    setSymbols(data.symbols ?? []);
  }, []);

  const remove = useCallback(async (symbol: string) => {
    const data = await api.removeMarketsWatchlist(symbol);
    setSymbols(data.symbols ?? []);
  }, []);

  const toggle = useCallback(
    async (symbol: string, name?: string) => {
      const code = symbol.trim().toUpperCase();
      if (symbolSet.has(code)) {
        await remove(code);
        return false;
      }
      await add(code, name);
      return true;
    },
    [add, remove, symbolSet],
  );

  const has = useCallback((symbol: string) => symbolSet.has(symbol.trim().toUpperCase()), [symbolSet]);

  return { symbols, symbolSet, loading, load, add, remove, toggle, has };
}
