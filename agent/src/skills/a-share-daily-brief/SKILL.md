---
name: a-share-daily-brief
category: research
description: >-
  SOP for same-day China A-share market briefs (大盘/板块/资金/新闻). Load before
  tasks like「分析今日A股市场行情」. Pins canonical index codes, tool order, source
  routing, and fail-fast rules so the agent does not loop on search_symbol,
  yfinance, or web scraping.
---
# A-Share Daily Market Brief

Use this skill when the user asks for **today's** (or the latest session's) China
A-share market overview: major indices, sector heat, northbound flow, and headline
news.

## Canonical index codes (do not search_symbol)

| Index | Code |
|-------|------|
| 上证指数 | `000001.SH` |
| 深证成指 | `399001.SZ` |
| 创业板指 | `399006.SZ` |
| 沪深300 | `000300.SH` |

These codes are fixed. **Never** call `search_symbol` for them or their Chinese names.

## Shortest path (execute in order)

1. **`get_market_data`** — one batch call:

```text
codes=["000001.SH","399001.SZ","399006.SZ","000300.SH"]
as_of="today"
lookback_days=10
source="auto"
```

Use Beijing calendar for `as_of="today"`. On a non-trading day, the latest bar is
the previous session — state that explicitly in the answer.

2. **`get_sector_info`** — `mode="ranking"` **once** (sector heat / 板块涨跌).

3. **`get_northbound_flow`** — once (北向资金).

4. **`get_stock_news`** — `scope="global"` once for A-share headlines.

Only add more tool calls when a specific gap remains after these four steps.

## Source rules (hard)

| Do | Don't |
|----|-------|
| `source="auto"` or `tencent` / `qmt` / `tushare` for A-share OHLCV | `yfinance` or `yahoo` for `.SH`/`.SZ` indices |
| Retry with another **A-share chain** source after empty data | `search_symbol` on the four canonical indices |
| Report missing data after one Eastmoney tool failure | Call the same Eastmoney tool again in the same run |
| Summarize with partial evidence | Use `web_search` / `read_url` as the primary price source |

## Eastmoney throttle discipline

`get_sector_info`, `get_northbound_flow`, `get_stock_news`, and `screen_market` hit
Eastmoney. After one failure or empty envelope:

- Do **not** repeat the same tool with the same arguments.
- Move to the next SOP step or state that dimension is unavailable.

## Date discipline

- Prefer `as_of="today"` + `lookback_days` on `get_market_data` instead of hand-picking stale dates.
- If the user says「今日」and markets are closed, label the answer with the last
  trading session date from the returned bars.

## Output shape

1. Major indices — close, change%, volume context (from step 1).
2. Sector leaders/laggards (from step 2).
3. Northbound flow snapshot (from step 3).
4. 3–5 headline news bullets with time (from step 4).
5. One-paragraph synthesis; mark any missing dimension.
