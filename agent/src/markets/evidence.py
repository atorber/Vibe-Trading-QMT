"""Collect quote, bars, insights, news, earnings and peers for one symbol."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from src.markets.insights import build_market_insights
from src.tools.research_reports_tool import ResearchReportsTool
from src.tools.sector_tool import fetch_industry_peers
from src.tools.stock_news_tool import StockNewsTool
from src.trading.connectors.qmt import sdk as qmt

logger = logging.getLogger(__name__)


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        number = float(value)
        return number if number == number else None
    except (TypeError, ValueError):
        return None


def _parse_bars(bars_raw: Any) -> list[dict[str, Any]]:
    bars: list[dict[str, Any]] = []
    for row in bars_raw or []:
        if not isinstance(row, dict):
            continue
        close = _num(row.get("close"))
        open_ = _num(row.get("open"))
        high = _num(row.get("high"))
        low = _num(row.get("low"))
        if close is None or open_ is None or high is None or low is None:
            continue
        bars.append(
            {
                "time": qmt._format_bar_time(
                    row.get("time") or row.get("date") or row.get("datetime") or ""
                ),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": _num(row.get("volume") or row.get("vol")) or 0,
            }
        )
    return bars


def _tick_fields(symbol: str, quote: dict[str, Any], *, name: str) -> dict[str, Any]:
    last = _num(quote.get("last") or quote.get("lastPrice") or quote.get("price"))
    prev = _num(quote.get("lastClose") or quote.get("prev_close") or quote.get("preClose"))
    change = None
    change_pct = None
    if last is not None and prev not in (None, 0):
        change = last - prev
        change_pct = change / prev * 100
    return {
        "symbol": symbol,
        "name": name,
        "last": last,
        "open": _num(quote.get("open") or quote.get("openPrice")),
        "high": _num(quote.get("high") or quote.get("highPrice")),
        "low": _num(quote.get("low") or quote.get("lowPrice")),
        "prev_close": prev,
        "change": change if change is not None else _num(quote.get("change")),
        "change_pct": change_pct
        if change_pct is not None
        else _num(quote.get("change_pct") or quote.get("pctChg")),
        "volume": _num(quote.get("volume") or quote.get("vol")),
        "amount": _num(quote.get("amount") or quote.get("turnover")),
        "updated_at": quote.get("time") or quote.get("timetag") or quote.get("updated_at"),
    }


def _resolve_name(code: str, cfg: qmt.QmtConfig) -> str:
    try:
        names = qmt.get_batch_stock_names([code], config=cfg)
        label = names.get(code.upper())
        if label and label != code:
            return label
    except Exception:
        pass
    try:
        payload = qmt._get(
            cfg,
            "/api/instrument/detail_list",
            params={"stocks": code, "iscomplete": "true"},
            require_api_key=False,
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        instrument: dict[str, Any] = {}
        if isinstance(data, dict):
            instrument = data.get(code) or next(iter(data.values()), {}) or {}
        if isinstance(instrument, dict):
            label = instrument.get("InstrumentName") or instrument.get("instrument_name")
            if label:
                return str(label)
    except Exception:
        pass
    return code


def _load_news(code: str, limit: int = 8) -> list[dict[str, Any]]:
    try:
        raw = StockNewsTool().execute(code=code, scope="stock", limit=limit)
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:  # noqa: BLE001
        logger.warning("news fetch failed for %s: %s", code, exc)
        return []
    if not isinstance(payload, dict) or not payload.get("ok"):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    articles = data.get("articles") if isinstance(data, dict) else []
    out: list[dict[str, Any]] = []
    for item in articles or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        out.append(
            {
                "title": title,
                "published": item.get("published"),
                "source": item.get("source"),
                "snippet": item.get("snippet"),
            }
        )
    return out[:limit]


def _load_earnings(code: str, limit: int = 8) -> dict[str, Any]:
    try:
        raw = ResearchReportsTool().execute(code=code, limit=limit)
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:  # noqa: BLE001
        logger.warning("earnings fetch failed for %s: %s", code, exc)
        return {"reports": [], "consensus_eps": []}
    if not isinstance(payload, dict) or not payload.get("ok"):
        return {"reports": [], "consensus_eps": []}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    reports = data.get("reports") if isinstance(data, dict) else []
    consensus = data.get("consensus_eps") if isinstance(data, dict) else []
    return {
        "reports": [r for r in reports or [] if isinstance(r, dict)][:limit],
        "consensus_eps": [r for r in consensus or [] if isinstance(r, dict)][:6],
    }


def _load_peers(code: str, limit: int = 6) -> dict[str, Any] | None:
    try:
        return fetch_industry_peers(code, limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("peers fetch failed for %s: %s", code, exc)
        return None


def collect_market_evidence(symbol: str) -> dict[str, Any]:
    """Build the evidence bundle used by the assessment LLM."""
    code = symbol.strip().upper()
    cfg = qmt.load_config()

    quote_payload = qmt.get_quote(code, config=cfg)
    quote_raw = quote_payload.get("quote") if isinstance(quote_payload, dict) else {}
    if not isinstance(quote_raw, dict):
        quote_raw = {}

    name = _resolve_name(code, cfg)
    quote = _tick_fields(code, quote_raw, name=name)

    bars: list[dict[str, Any]] = []
    try:
        bars_payload = qmt.get_historical_bars(code, config=cfg, period="1d", limit=180)
        bars_raw = bars_payload.get("bars") if isinstance(bars_payload, dict) else []
        bars = _parse_bars(bars_raw)
        if not bars:
            start = (datetime.now() - timedelta(days=400)).strftime("%Y%m%d")
            qmt.download_history_bars([code], period="1d", start_time=start, config=cfg)
            bars_payload = qmt.get_historical_bars(code, config=cfg, period="1d", limit=180)
            bars_raw = bars_payload.get("bars") if isinstance(bars_payload, dict) else []
            bars = _parse_bars(bars_raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("bars fetch failed for %s: %s", code, exc)

    insights = build_market_insights(name, quote, bars)
    news = _load_news(code)
    earnings = _load_earnings(code)
    peers = _load_peers(code)

    peer_avg_change: float | None = None
    if peers and peers.get("peers"):
        values = [
            float(p["change_pct"])
            for p in peers["peers"]
            if isinstance(p, dict) and isinstance(p.get("change_pct"), (int, float))
        ]
        target = peers.get("target")
        if isinstance(target, dict) and isinstance(target.get("change_pct"), (int, float)):
            values.append(float(target["change_pct"]))
        if values:
            peer_avg_change = sum(values) / len(values)

    return {
        "symbol": code,
        "name": name,
        "quote": quote,
        "bars_count": len(bars),
        "insights": insights,
        "news": news,
        "earnings": earnings,
        "peers": peers,
        "peer_avg_change_pct": peer_avg_change,
        "collected_at": datetime.now().isoformat(timespec="seconds"),
    }
