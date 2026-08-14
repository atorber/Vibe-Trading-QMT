"""QMT Bridge trading connector (read-only monitoring).

Talks to a user-owned QMT Bridge HTTP service (typically on a Windows host
running miniQMT). Connection settings come from ``QMT_BRIDGE_*`` environment
variables. Paper vs live is operator-declared; this package exposes no order
placement.
"""

from src.trading.connectors.qmt.sdk import (
    QmtConfig,
    QmtConfigError,
    QmtAPIError,
    build_config,
    check_status,
    get_account_snapshot,
    get_historical_bars,
    get_open_orders,
    get_positions,
    get_quote,
    load_config,
)

__all__ = [
    "QmtConfig",
    "QmtConfigError",
    "QmtAPIError",
    "build_config",
    "check_status",
    "get_account_snapshot",
    "get_historical_bars",
    "get_open_orders",
    "get_positions",
    "get_quote",
    "load_config",
]
