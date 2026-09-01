"""Optional AKShare fallback adapters for Eastmoney-backed screeners.

When push2 endpoints are throttled or return empty payloads, these helpers
recover the same research workflow from AKShare's free interfaces.
"""

from __future__ import annotations

from typing import Any

_INDUSTRY_SORT_COL = "涨跌幅"
_SPOT_SORT_COLUMNS = {
    "change_pct": "涨跌幅",
    "volume": "成交量",
    "amount": "成交额",
    "turnover": "换手率",
}


def _coerce_float(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _require_dataframe(frame: Any, *, label: str) -> Any:
    if frame is None or bool(getattr(frame, "empty", True)):
        raise RuntimeError(f"akshare {label} returned no rows")
    return frame


def fetch_industry_board_ranking(limit: int) -> list[dict[str, Any]]:
    """Fetch industry-board ranking rows compatible with ``get_sector_info``."""
    import akshare as ak

    df = _require_dataframe(ak.stock_board_industry_name_em(), label="industry boards")
    if _INDUSTRY_SORT_COL not in df.columns:
        raise RuntimeError(f"akshare missing column {_INDUSTRY_SORT_COL!r}")

    sorted_df = df.sort_values(by=_INDUSTRY_SORT_COL, ascending=False)
    boards: list[dict[str, Any]] = []
    for _, row in sorted_df.head(limit).iterrows():
        board_code = row.get("板块代码")
        board_name = row.get("板块名称")
        if not board_code or not board_name:
            continue
        leader = row.get("领涨股票")
        boards.append(
            {
                "board_code": str(board_code),
                "board_name": str(board_name),
                "change_pct": _coerce_float(row.get("涨跌幅")),
                "index": _coerce_float(row.get("最新价")),
                "up_count": _coerce_float(row.get("上涨家数")),
                "down_count": _coerce_float(row.get("下跌家数")),
                "leader": str(leader) if leader not in (None, "", "-") else None,
            }
        )

    if not boards:
        raise RuntimeError("akshare industry board rows were empty after parsing")
    return boards


def screen_a_share_market(*, sort_by: str, top_n: int) -> list[dict[str, Any]]:
    """Screen A-share spot rows compatible with ``screen_market``."""
    import akshare as ak

    sort_col = _SPOT_SORT_COLUMNS.get(sort_by)
    if sort_col is None:
        raise ValueError(f"unsupported sort_by for akshare fallback: {sort_by!r}")

    df = _require_dataframe(ak.stock_zh_a_spot_em(), label="A-share spot")
    if sort_col not in df.columns:
        raise RuntimeError(f"akshare missing column {sort_col!r}")

    sorted_df = df.sort_values(by=sort_col, ascending=False)
    rows: list[dict[str, Any]] = []
    for _, row in sorted_df.head(top_n).iterrows():
        code_raw = str(row.get("代码", "")).strip()
        if not code_raw:
            continue
        code = code_raw.zfill(6)
        name = row.get("名称")
        if not name:
            continue
        rows.append(
            {
                "code": code,
                "name": str(name),
                "price": _coerce_float(row.get("最新价")),
                "change_pct": _coerce_float(row.get("涨跌幅")),
                "change": _coerce_float(row.get("涨跌额")),
                "volume": _coerce_float(row.get("成交量")),
                "amount": _coerce_float(row.get("成交额")),
                "turnover_rate": _coerce_float(row.get("换手率")),
            }
        )

    if not rows:
        raise RuntimeError("akshare A-share spot rows were empty after parsing")
    return rows
