"""QMT Bridge loader: A-share OHLCV via ``GET /api/market/market_data_ex``.

Uses the same ``QMT_BRIDGE_*`` settings as the trading connector. Bridge down
or empty bars → ``fetch`` returns ``{}`` so ``get_market_data`` can fall through
to tencent / mootdx / eastmoney.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import pandas as pd

from backtest.loaders.base import validate_date_range
from backtest.loaders.registry import register

logger = logging.getLogger(__name__)

_OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]

_INTERVAL_MAP = {
    "1d": "1d",
    "d": "1d",
    "day": "1d",
    "daily": "1d",
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "60m": "60m",
    "1h": "60m",
}


def _period(interval: str) -> str | None:
    token = str(interval or "1D").strip().lower()
    return _INTERVAL_MAP.get(token)


def _ymd(value: str) -> str:
    return pd.Timestamp(value).strftime("%Y%m%d")


@register
class DataLoader:
    """A-share OHLCV from a running QMT Bridge."""

    name = "qmt"
    markets = {"a_share"}
    volume_units = {"a_share": "shares"}
    requires_auth = False

    def is_available(self) -> bool:
        return True

    def fetch(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
        *,
        interval: str = "1D",
        fields: Optional[List[str]] = None,
    ) -> Dict[str, pd.DataFrame]:
        del fields
        validate_date_range(start_date, end_date)
        period = _period(interval)
        if period is None:
            logger.warning("qmt does not support interval=%s", interval)
            return {}

        from src.trading.connectors.qmt import sdk as qmt

        a_share = [
            code.strip().upper()
            for code in codes
            if code.strip().upper().endswith((".SZ", ".SH", ".BJ"))
        ]
        if not a_share:
            return {}

        cfg = qmt.load_config()
        payload = qmt._get(
            cfg,
            "/api/market/market_data_ex",
            params={
                "stocks": ",".join(a_share),
                "period": period,
                "start_time": _ymd(start_date),
                "end_time": _ymd(end_date),
                "count": -1,
                "dividend_type": "front",
                "fill_data": "true",
            },
            require_api_key=False,
        )

        result: Dict[str, pd.DataFrame] = {}
        for code in a_share:
            rows = [qmt._bar_to_dict(row) for row in qmt._bars_from_payload(payload, symbol=code)]
            df = _rows_to_frame(rows)
            if df is not None and not df.empty:
                result[code] = df
        return result


def _rows_to_frame(rows: list[dict]) -> Optional[pd.DataFrame]:
    records = []
    for row in rows:
        ts = row.get("time")
        if ts is None:
            continue
        records.append(
            {
                "trade_date": pd.to_datetime(ts),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "volume": row.get("volume"),
            }
        )
    if not records:
        return None
    df = pd.DataFrame(records).set_index("trade_date").sort_index()
    return df[_OHLCV_COLUMNS].dropna(subset=["open", "high", "low", "close"])
