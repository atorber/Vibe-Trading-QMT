---
name: broker-trade-review
category: research
description: >-
  SOP for same-day live-broker trade review (今日交易复盘) from QMT/Futu/IBKR
  read-only connectors. Pins broker tool order, forbids wasteful search_symbol /
  daily-brief market fetches unless the user asked for market background, and
  requires a data-availability footer when optional context tools were tried.
---
# Live Broker Trade Review (今日交易复盘)

Use when the user asks to review **today's** trades, fills, positions, or P&L
from a **connected read-only broker** (e.g. QMT Bridge, Futu OpenD), not from
an uploaded CSV journal.

## Primary path (broker evidence only)

Execute in order; symbols come from `trading_positions` — **never** `search_symbol`
for codes you already hold:

1. `trading_connections` → `trading_select_connection` (pick QMT / Futu / etc.)
2. `trading_account` — cash, assets, liabilities
3. `trading_positions` — holdings, market value, cost basis
4. `trading_history_deals` (or `trading_orders` + deals) — today's fills
5. `trading_orders` — open / pending orders

Build the report from these envelopes only. Use position `market_value` and
`volume` for mark-to-market; cite the broker field and as-of timestamp.

## What NOT to call by default

Unless the user **explicitly** asks for 大盘 / 板块 / 北向 / 新闻 / 市场背景:

| Do not call | Why |
|-------------|-----|
| `search_symbol` on position codes | Already qualified (`300285.SZ`, etc.) |
| Four-index `get_market_data` batch | That is the **a-share-daily-brief** SOP, not trade review |
| `get_sector_info` ranking | Optional context only |
| `get_northbound_flow` | Optional context only |
| `get_stock_news` | Optional context only |
| `web_search` | Not primary evidence for broker review |

Do **not** load or follow `a-share-daily-brief` for a trade-review task.

## Optional market context (user asked OR you promised it in the outline)

If the user wants market background, or you told them you will add it:

- Call each dimension **at most once** (`get_market_data` indices, sector ranking,
  northbound, news per symbol or global).
- **Mandatory footer** in the final report:

```markdown
### 数据获取状态
| 维度 | 工具 | 结果 | 对报告的影响 |
|------|------|------|----------------|
| 大盘指数 | get_market_data | 成功/失败 | 已纳入 / 未纳入 |
| ... | ... | ... | ... |
```

- If a tool returned `ok: false` or empty rows, write **failed — not retrieved**;
  do not silently omit the dimension.
- If a tool **succeeded**, you **must** use its data in the report or explain
  why it was excluded.

## Report shape

1. 账户概况（broker `trading_account`）
2. 收盘持仓（`trading_positions`）
3. 今日成交明细（`trading_history_deals` / orders）
4. 已实现 / 浮动盈亏（derived from broker fields; show formula）
5. 当前挂单（`trading_orders`）
6. 时间线 + 行为分析（evidence from fills only)
7. **数据获取状态**（only if optional market/news tools were called)

End with a one-line disclaimer: research-only, not investment advice.
