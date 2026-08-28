"""Authenticated Markets API — QMT Bridge quotes for the SPA.

Proxies index snapshots, search, instrument detail, ticks and daily bars so the
frontend never talks to qmt-bridge directly.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from src.api.security import require_auth
from src.markets.assessment import generate_market_assessment
from src.markets.watchlist import MarketsWatchlistStore
from src.trading.connectors.qmt import sdk as qmt
from src.tools.research_reports_tool import ResearchReportsTool
from src.tools.sector_tool import fetch_industry_peers
from src.tools.stock_news_tool import StockNewsTool

_INDEX_NAMES = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000300.SH": "沪深300",
    "000016.SH": "上证50",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
}


def _bridge_error(exc: Exception) -> HTTPException:
    text = str(exc)
    lower = text.lower()
    if any(
        token in lower
        for token in ("refused", "unreachable", "timed out", "failed to establish")
    ):
        return HTTPException(
            status_code=503,
            detail="QMT Bridge unreachable. Start qmt-server on the Windows host.",
        )
    return HTTPException(status_code=502, detail=text[:300])


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        number = float(value)
        return number if number == number else None
    except (TypeError, ValueError):
        return None


def _tick_fields(
    symbol: str, quote: dict[str, Any], *, name: str | None = None
) -> dict[str, Any]:
    last = _num(quote.get("last") or quote.get("lastPrice") or quote.get("price"))
    prev = _num(
        quote.get("lastClose") or quote.get("prev_close") or quote.get("preClose")
    )
    change = None
    change_pct = None
    if last is not None and prev not in (None, 0):
        change = last - prev
        change_pct = change / prev * 100
    return {
        "symbol": symbol,
        "name": name or quote.get("name") or _INDEX_NAMES.get(symbol) or symbol,
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
        "bid": _num(quote.get("bid") or quote.get("bidPrice") or quote.get("bid1")),
        "ask": _num(quote.get("ask") or quote.get("askPrice") or quote.get("ask1")),
        "updated_at": (
            quote.get("time")
            or quote.get("timetag")
            or quote.get("updated_at")
            or quote.get("datetime")
            or None
        ),
    }


def _quote_map(payload: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            out[str(key).upper()] = value
    return out


def _history_start(period: str, limit: int) -> str:
    """Rough calendar start so Bridge download covers the requested bar window."""
    token = (period or "1d").strip().lower()
    if token in ("1d", "day"):
        days = max(int(limit) * 2, 400)
    elif token in ("60m", "1h"):
        days = max(int(limit) // 4, 45)
    elif token in ("15m", "30m"):
        days = max(int(limit) // 12, 21)
    elif token == "5m":
        days = max(int(limit) // 40, 14)
    else:
        days = max(int(limit) // 200, 7)
    return (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")


def _parse_bars(bars_raw: Any) -> list[dict[str, Any]]:
    bars: list[dict[str, Any]] = []
    for row in bars_raw or []:
        if not isinstance(row, dict):
            continue
        bars.append(
            {
                "time": qmt._format_bar_time(
                    row.get("time") or row.get("date") or row.get("datetime") or ""
                ),
                "open": _num(row.get("open")),
                "high": _num(row.get("high")),
                "low": _num(row.get("low")),
                "close": _num(row.get("close")),
                "volume": _num(row.get("volume") or row.get("vol")) or 0,
            }
        )
    return bars


def _names_for(codes: list[str], cfg: qmt.QmtConfig) -> dict[str, str]:
    if not codes:
        return {}
    try:
        payload = qmt._get(
            cfg,
            "/api/utility/batch_stock_name",
            params={"stocks": ",".join(codes)},
            require_api_key=False,
        )
    except Exception:
        return {code: _INDEX_NAMES.get(code, code) for code in codes}
    names: dict[str, str] = {}
    if isinstance(payload, dict):
        data = payload.get("data") or payload.get("names") or payload
        if isinstance(data, dict):
            for key, value in data.items():
                names[str(key).upper()] = str(value)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    code = str(
                        item.get("stock")
                        or item.get("code")
                        or item.get("symbol")
                        or ""
                    ).upper()
                    label = item.get("name") or item.get("stock_name")
                    if code and label:
                        names[code] = str(label)
    for code in codes:
        names.setdefault(code, _INDEX_NAMES.get(code, code))
    return names


class WatchlistSymbolRequest(BaseModel):
    symbol: str
    name: str | None = None


class WatchlistPutRequest(BaseModel):
    symbols: list[WatchlistSymbolRequest] = Field(default_factory=list)


def register_markets_routes(app: FastAPI) -> None:
    """Mount ``/api/markets`` endpoints used by the Markets SPA pages."""

    @app.get("/api/markets/watchlist", dependencies=[Depends(require_auth)])
    def markets_watchlist_get():
        """Return the persisted watchlist (symbol + optional cached name)."""
        watchlist = MarketsWatchlistStore().load()
        return {
            "status": "ok",
            "symbols": [item.to_dict() for item in watchlist.symbols],
        }

    @app.put("/api/markets/watchlist", dependencies=[Depends(require_auth)])
    def markets_watchlist_put(payload: WatchlistPutRequest):
        """Replace the entire watchlist."""
        try:
            watchlist = MarketsWatchlistStore().save(
                {
                    "symbols": [
                        {"symbol": item.symbol, "name": item.name}
                        for item in payload.symbols
                    ]
                }
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "status": "ok",
            "symbols": [item.to_dict() for item in watchlist.symbols],
        }

    @app.post("/api/markets/watchlist", dependencies=[Depends(require_auth)])
    def markets_watchlist_add(payload: WatchlistSymbolRequest):
        """Add one symbol (moves to front when already followed)."""
        try:
            watchlist = MarketsWatchlistStore().add(
                payload.symbol, name=payload.name
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "status": "ok",
            "symbols": [item.to_dict() for item in watchlist.symbols],
        }

    @app.delete(
        "/api/markets/watchlist/{symbol:path}",
        dependencies=[Depends(require_auth)],
    )
    def markets_watchlist_remove(symbol: str):
        """Remove one symbol from the watchlist."""
        try:
            watchlist = MarketsWatchlistStore().remove(symbol)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "status": "ok",
            "symbols": [item.to_dict() for item in watchlist.symbols],
        }

    @app.get("/api/markets", dependencies=[Depends(require_auth)])
    def markets_overview(
        q: str | None = Query(None, description="Optional search keyword"),
        limit: int = Query(20, ge=1, le=50),
    ):
        """Index strip + searchable A-share quote table via QMT Bridge."""
        cfg = qmt.load_config()
        try:
            indices_payload = qmt._get(
                cfg, "/api/market/indices", require_api_key=False
            )
        except Exception as exc:
            raise _bridge_error(exc) from exc

        index_map = _quote_map(indices_payload)
        index_codes = [
            str(code).upper()
            for code in (
                indices_payload.get("indices")
                if isinstance(indices_payload, dict)
                else []
            )
        ] or list(index_map.keys())
        index_names = _names_for(index_codes, cfg)
        indices = [
            _tick_fields(code, index_map.get(code, {}), name=index_names.get(code))
            for code in index_codes
        ]

        if q and q.strip():
            try:
                search_payload = qmt._get(
                    cfg,
                    "/api/utility/search",
                    params={
                        "keyword": q.strip(),
                        "category": "沪深A股",
                        "limit": limit,
                    },
                    require_api_key=False,
                )
            except Exception as exc:
                raise _bridge_error(exc) from exc
            stocks = (
                search_payload.get("stocks") if isinstance(search_payload, dict) else []
            )
            symbols = [str(code).upper() for code in (stocks or [])][:limit]
        else:
            symbols = MarketsWatchlistStore().symbol_codes()[:limit]

        quotes: list[dict[str, Any]] = []
        if symbols:
            try:
                tick_payload = qmt._get(
                    cfg,
                    "/api/market/full_tick",
                    params={"stocks": ",".join(symbols)},
                    require_api_key=False,
                )
            except Exception as exc:
                raise _bridge_error(exc) from exc
            tick_map = _quote_map(tick_payload)
            for code in symbols:
                raw = tick_map.get(code, {})
                connector = qmt._quote_from_payload({"data": {code: raw}}, code)
                merged = {**(raw if isinstance(raw, dict) else {}), **connector}
                quotes.append(_tick_fields(code, merged))
            names = _names_for(symbols, cfg)
            for row in quotes:
                row["name"] = names.get(row["symbol"], row.get("name") or row["symbol"])

        return {
            "status": "ok",
            "source": "qmt-bridge",
            "query": q,
            "indices": indices,
            "quotes": quotes,
            "watchlist": MarketsWatchlistStore().symbol_codes() if not q else None,
        }

    # Register before ``/{symbol:path}`` so ``.../news`` is not swallowed as a symbol.
    @app.get("/api/markets/{symbol}/news", dependencies=[Depends(require_auth)])
    def market_news(
        symbol: str,
        limit: int = Query(20, ge=1, le=50),
    ):
        """Headlines for one symbol via existing StockNewsTool (Eastmoney / Yahoo)."""
        code = symbol.strip().upper()
        if not code or code in ("markets", "news"):
            raise HTTPException(status_code=400, detail="symbol required")
        try:
            raw = StockNewsTool().execute(code=code, scope="stock", limit=limit)
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:  # noqa: BLE001 - surface upstream failure to SPA
            raise HTTPException(status_code=502, detail=str(exc)[:300]) from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=502, detail="invalid news payload")
        if not payload.get("ok"):
            raise HTTPException(
                status_code=502,
                detail=str(payload.get("error") or "news fetch failed")[:300],
            )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        articles_raw = data.get("articles") if isinstance(data, dict) else []
        articles: list[dict[str, Any]] = []
        for item in articles_raw or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            articles.append(
                {
                    "title": title,
                    "url": item.get("url") or None,
                    "source": item.get("source") or None,
                    "published": item.get("published") or None,
                    "snippet": item.get("snippet") or None,
                }
            )
        return {
            "status": "ok",
            "symbol": code,
            "source": payload.get("source") or "stock_news",
            "market": payload.get("market"),
            "articles": articles,
        }

    @app.get("/api/markets/{symbol}/earnings", dependencies=[Depends(require_auth)])
    def market_earnings(
        symbol: str,
        limit: int = Query(15, ge=1, le=50),
    ):
        """Sell-side reports + consensus EPS for one A-share symbol."""
        code = symbol.strip().upper()
        if not code or code in ("markets", "earnings", "news"):
            raise HTTPException(status_code=400, detail="symbol required")
        try:
            raw = ResearchReportsTool().execute(code=code, limit=limit)
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:  # noqa: BLE001 - surface upstream failure to SPA
            raise HTTPException(status_code=502, detail=str(exc)[:300]) from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=502, detail="invalid earnings payload")
        if not payload.get("ok"):
            raise HTTPException(
                status_code=502,
                detail=str(payload.get("error") or "earnings fetch failed")[:300],
            )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        reports_raw = data.get("reports") if isinstance(data, dict) else []
        consensus_raw = data.get("consensus_eps") if isinstance(data, dict) else []
        reports: list[dict[str, Any]] = []
        for item in reports_raw or []:
            if isinstance(item, dict) and item.get("title"):
                reports.append(item)
        consensus_eps: list[dict[str, Any]] = []
        for item in consensus_raw or []:
            if isinstance(item, dict) and item.get("fiscal_year") is not None:
                consensus_eps.append(item)
        return {
            "status": "ok",
            "symbol": code,
            "source": payload.get("source") or "eastmoney+ths",
            "reports": reports,
            "consensus_eps": consensus_eps,
        }

    @app.get("/api/markets/{symbol}/peers", dependencies=[Depends(require_auth)])
    def market_peers(
        symbol: str,
        limit: int = Query(8, ge=1, le=20),
    ):
        """Same-industry peer quotes for one A-share symbol (Eastmoney board)."""
        code = symbol.strip().upper()
        if not code or code in ("markets", "peers", "news", "earnings"):
            raise HTTPException(status_code=400, detail="symbol required")
        try:
            payload = fetch_industry_peers(code, limit=limit)
        except Exception as exc:  # noqa: BLE001 - surface upstream failure to SPA
            raise HTTPException(status_code=502, detail=str(exc)[:300]) from exc
        if not payload:
            raise HTTPException(
                status_code=502,
                detail=f"no industry peers found for {code}",
            )
        return {
            "status": "ok",
            "source": "eastmoney",
            **payload,
        }

    @app.post("/api/markets/{symbol}/assessment", dependencies=[Depends(require_auth)])
    def market_assessment_generate(symbol: str):
        """Generate AI assessment from quote, insights, news, earnings and peers."""
        code = symbol.strip().upper()
        if not code or code in ("markets", "assessment", "news", "earnings", "peers"):
            raise HTTPException(status_code=400, detail="symbol required")
        try:
            payload = generate_market_assessment(code, use_cache=False, force=True)
        except RuntimeError as exc:
            text = str(exc)
            if "rate limited" in text.lower():
                raise HTTPException(status_code=429, detail=text[:300]) from exc
            raise HTTPException(status_code=502, detail=text[:300]) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(exc)[:300]) from exc
        return payload

    @app.get("/api/markets/{symbol:path}", dependencies=[Depends(require_auth)])
    def market_detail(
        symbol: str,
        period: str = Query("1d"),
        limit: int = Query(180, ge=20, le=1000),
        auto_download: bool = Query(
            True, description="When local bars are empty, download via Bridge then retry"
        ),
    ):
        """One symbol: instrument detail, live tick, and OHLC bars."""
        code = symbol.strip().upper()
        if not code or code == "markets":
            raise HTTPException(status_code=400, detail="symbol required")
        cfg = qmt.load_config()

        try:
            quote_payload = qmt.get_quote(code, config=cfg)
            bars_payload = qmt.get_historical_bars(
                code, config=cfg, period=period, limit=limit
            )
        except Exception as exc:
            raise _bridge_error(exc) from exc

        quote = quote_payload.get("quote") if isinstance(quote_payload, dict) else {}
        if not isinstance(quote, dict):
            quote = {}

        instrument: dict[str, Any] = {}
        try:
            detail_payload = qmt._get(
                cfg,
                "/api/instrument/detail_list",
                params={"stocks": code, "iscomplete": "true"},
                require_api_key=False,
            )
            data = (
                detail_payload.get("data") if isinstance(detail_payload, dict) else None
            )
            if isinstance(data, dict):
                instrument = (
                    data.get(code)
                    or data.get(symbol)
                    or next(iter(data.values()), {})
                    or {}
                )
            elif isinstance(data, list) and data and isinstance(data[0], dict):
                instrument = data[0]
        except Exception:
            instrument = {}

        names = _names_for([code], cfg)
        name = (
            names.get(code)
            or instrument.get("InstrumentName")
            or instrument.get("instrument_name")
            or instrument.get("name")
            or code
        )
        tick = _tick_fields(code, quote, name=str(name))

        bars_raw = bars_payload.get("bars") if isinstance(bars_payload, dict) else []
        bars = _parse_bars(bars_raw)

        downloaded = False
        download_status: Any = None
        if not bars and auto_download:
            try:
                download_status = qmt.download_history_bars(
                    [code],
                    config=cfg,
                    period=period,
                    start_time=_history_start(period, limit),
                    timeout=90.0,
                )
                downloaded = True
                bars_payload = qmt.get_historical_bars(
                    code, config=cfg, period=period, limit=limit
                )
                bars_raw = (
                    bars_payload.get("bars") if isinstance(bars_payload, dict) else []
                )
                bars = _parse_bars(bars_raw)
            except Exception as exc:  # noqa: BLE001 - surface download failure to UI
                download_status = str(exc)[:300]

        return {
            "status": "ok",
            "source": "qmt-bridge",
            "symbol": code,
            "name": tick["name"],
            "market": (
                "SSE"
                if code.endswith(".SH")
                else "SZSE"
                if code.endswith(".SZ")
                else ""
            ),
            "sector": instrument.get("Sector")
            or instrument.get("Industry")
            or instrument.get("sector")
            or "",
            "quote": tick,
            "instrument": {
                "exchange": instrument.get("ExchangeID") or instrument.get("exchange"),
                "product": instrument.get("ProductID") or instrument.get("product"),
                "listed_date": instrument.get("OpenDate")
                or instrument.get("listed_date"),
                "currency": "CNY",
            },
            "bars": bars,
            "period": period,
            "downloaded": downloaded,
            "download_status": download_status,
        }
