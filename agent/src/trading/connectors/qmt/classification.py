"""Curated read/write classification for QMT Bridge operations.

Keys are Bridge/client operation names. Order-mutating and fund-moving calls are
pinned WRITE so the live gate never treats them as plain reads; anything
unlisted resolves to UNKNOWN → WRITE (fail-closed).
"""

from __future__ import annotations

from src.live.classification import ToolClass

QMT_TOOL_CLASS: dict[str, ToolClass] = {
    # READ — monitoring surface
    "health_check": ToolClass.READ,
    "get_account_status": ToolClass.READ,
    "query_asset": ToolClass.READ,
    "query_positions": ToolClass.READ,
    "query_orders": ToolClass.READ,
    "query_trades": ToolClass.READ,
    "get_market_snapshot": ToolClass.READ,
    "get_full_tick": ToolClass.READ,
    "get_history": ToolClass.READ,
    # WRITE — not exposed by P0 profiles; catalogued fail-closed
    "place_order": ToolClass.WRITE,
    "cancel_order": ToolClass.WRITE,
    "batch_order": ToolClass.WRITE,
    "batch_cancel": ToolClass.WRITE,
    "credit_order": ToolClass.WRITE,
    "fund_transfer": ToolClass.WRITE,
    "bank_transfer_in": ToolClass.WRITE,
    "bank_transfer_out": ToolClass.WRITE,
    "smt_order": ToolClass.WRITE,
}
