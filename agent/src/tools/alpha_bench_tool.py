"""Alpha bench orchestrator: registry → universe panel → IC/IR → HTML report.

W2 scaffold: implements the orchestration shape and HTML rendering. Universe
loaders that need network calls return a clean "not yet implemented" envelope —
the full universe wiring lands in W4. The HTML path is autoescaped via Jinja2,
with manual ``html.escape`` fallback when Jinja2 is absent, plus a strict CSP
``<meta>`` so the report cannot fetch or execute external resources.

Output contract — JSON envelope:
    {"status": "ok"|"error",
     "report_path": str | None,
     "n_alphas_tested": int,
     "n_skipped": int,
     "top": [{"id": ..., "ic_mean": ..., "ir": ..., ...}, ...]}

Cache integrity note: the universe panel cache lives in ``~/.vibe-trading/cache/``
as pickle blobs. Each pickle is paired with a ``<name>.sha256`` sidecar holding a
**keyed HMAC-SHA256 tag** (not a bare digest) computed over the blob. On load we
recompute the tag with the same key and refuse the cache on mismatch before the
``pickle.loads`` call. The key is ``API_AUTH_KEY`` when configured, else a
machine-local random 32-byte secret persisted at ``cache/.hmac_key`` (mode 0600).
Because the tag is keyed, a local attacker who rewrites the pickle cannot forge a
matching sidecar without the secret — so this is an authenticity guard against
local-write RCE via ``pickle.loads``, not merely a corruption check.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backtest.loaders.cn_adjust import apply_qfq as _apply_qfq
from src.agent.tools import BaseTool
from src.config.accessor import get_env_config
from src.factors.factor_analysis_core import _MIN_VALID_PER_DATE as MIN_CROSS_SECTIONAL_INSTRUMENTS

logger = logging.getLogger(__name__)

# Date the SP500 constituent list was sampled from Wikipedia (best-effort label
# for the survivorship-bias warning in the bench summary's ``meta`` block).
_SP500_CONSTITUENT_SOURCE_DATE = "2026-05-17"
# Below this share of named sectors the tag is worse than absent: one
# "unknown" bucket demeans as a single group, which is the global fallback
# the alphas already have, but reported as industry neutralization.
_SP500_MIN_SECTOR_COVERAGE = 0.9

# Concurrent Tushare ``pro.daily`` fetches when building CSI300. Free tier
# allows ~200 calls/min; 4 workers stays well under that with a 300-name list.
_CSI300_FETCH_WORKERS = 4


# ---------------------------------------------------------------------------
# Universe + period parsing
# ---------------------------------------------------------------------------

_PERIOD_YEAR = re.compile(r"^(\d{4})-(\d{4})$")
_PERIOD_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})$")

# Universe → (market_key, universe_meta_tag). Only the listed universes have a
# defined contract; everything else returns "not yet implemented".
_UNIVERSE_TAG = {
    "csi300": "equity_cn",
    "sp500": "equity_us",
    "btc-usdt": "crypto",
}


def _parse_period(period: str) -> tuple[str, str]:
    """Return (start_date, end_date) as YYYY-MM-DD strings."""
    if not isinstance(period, str):
        raise ValueError(f"period must be string, got {type(period).__name__}")
    m = _PERIOD_DATE.match(period)
    if m:
        start, end = m.group(1), m.group(2)
    else:
        m = _PERIOD_YEAR.match(period)
        if m:
            start, end = f"{m.group(1)}-01-01", f"{m.group(2)}-12-31"
        else:
            raise ValueError(
                f"period {period!r} must be YYYY-YYYY or YYYY-MM-DD/YYYY-MM-DD"
            )
    # Match backtest loaders.validate_date_range: reject inverted ranges.
    if pd.Timestamp(start) > pd.Timestamp(end):
        raise ValueError(f"start_date ({start}) > end_date ({end})")
    return start, end


def _validate_panel_cross_section(
    panel: dict[str, pd.DataFrame],
    *,
    universe: str,
    start: str,
    end: str,
) -> None:
    """Reject panels too thin for cross-sectional IC (needs >=5 names per bar)."""
    close_df = panel.get("close")
    if close_df is None or close_df.empty:
        raise RuntimeError(
            f"universe {universe!r} produced empty panel for {start}..{end}; "
            "check network / token / date range"
        )
    n_symbols = int(close_df.shape[1])
    if n_symbols < MIN_CROSS_SECTIONAL_INSTRUMENTS:
        raise RuntimeError(
            f"universe {universe!r} panel has only {n_symbols} instrument(s) for "
            f"{start}..{end}; cross-sectional IC needs at least "
            f"{MIN_CROSS_SECTIONAL_INSTRUMENTS}. Delete "
            f"~/.vibe-trading/cache/{universe}_{start}_{end}.pkl* and retry "
            "(often caused by Tushare rate limits, token permissions, or a "
            "partial fetch that was cached)."
        )


def _load_universe_panel(
    universe: str, period: str, *, use_cache: bool = True
) -> dict[str, pd.DataFrame]:
    """Load OHLCV(+amount, +vwap) wide panel for the requested universe.

    Returns a dict keyed by panel column (open/high/low/close/volume/amount/vwap)
    where each value is a wide ``pd.DataFrame`` indexed by date (DatetimeIndex)
    with one column per instrument.

    Args:
        universe: ``csi300`` | ``sp500`` | ``btc-usdt``.
        period: ``YYYY-YYYY`` or ``YYYY-MM-DD/YYYY-MM-DD``.
        use_cache: When True (default) reuse a pickle in
            ``~/.vibe-trading/cache/`` if the same universe+period was fetched
            before. Set to False to force a re-fetch.

    Raises:
        ValueError: unknown universe or bad period.
        RuntimeError: ``TUSHARE_TOKEN`` unset when csi300 is requested.
    """
    if universe not in _UNIVERSE_TAG:
        raise ValueError(
            f"universe {universe!r} not recognized; expected one of {sorted(_UNIVERSE_TAG)}"
        )
    start, end = _parse_period(period)

    cache_dir = Path.home() / ".vibe-trading" / "cache"
    cache_path = cache_dir / f"{universe}_{start}_{end}.pkl"
    if use_cache and cache_path.is_file():
        cached = _read_pickle_cache(cache_path)
        if cached is not None:
            try:
                _validate_panel_cross_section(
                    cached, universe=universe, start=start, end=end
                )
            except RuntimeError as exc:
                logger.warning(
                    "universe %s: refusing stale cache %s (%s); refetching",
                    universe,
                    cache_path.name,
                    exc,
                )
            else:
                logger.info("universe %s: loaded from cache %s", universe, cache_path)
                return cached

    if universe == "csi300":
        panel = _load_csi300_panel(start, end)
    elif universe == "sp500":
        panel = _load_sp500_panel(start, end)
    elif universe == "btc-usdt":
        panel = _load_btc_panel(start, end)
    else:  # pragma: no cover — guarded above
        raise ValueError(f"unhandled universe {universe!r}")

    _validate_panel_cross_section(panel, universe=universe, start=start, end=end)

    # btc-usdt loader returns a single-column close (one instrument). Cross-
    # sectional IC needs >= 2 instruments — short-circuit with a clean error
    # that propagates to API (400) and CLI.
    close_df = panel["close"]
    if universe == "btc-usdt" and close_df.shape[1] < 2:
        raise ValueError(
            "btc-usdt is single-asset; cross-sectional IC needs >=2 instruments. "
            "Use a multi-symbol crypto basket (e.g. multiple OKX pairs) for "
            "meaningful results."
        )

    if use_cache:
        _write_pickle_cache(cache_dir, cache_path, panel)

    return panel


def _sha256_path(cache_path: Path) -> Path:
    return cache_path.with_suffix(cache_path.suffix + ".sha256")


def _cache_hmac_key(cache_dir: Path) -> bytes:
    """Return the secret backing the cache sidecar HMAC.

    Priority: ``API_AUTH_KEY`` (UTF-8) when configured, else a persisted
    machine-local 32-byte random key. The fallback is required because an empty
    HMAC key would let any local attacker forge a matching sidecar for a
    malicious pickle — defeating the whole point of the tag.
    """
    configured = get_env_config().api.api_auth_key.strip()
    if configured:
        return configured.encode("utf-8")

    key_path = cache_dir / ".hmac_key"
    try:
        return bytes.fromhex(key_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass

    key = secrets.token_bytes(32)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        # O_EXCL + mode 0600 from creation: a concurrent writer can't race us
        # into a world-readable key, and we never widen perms after the fact.
        fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key.hex().encode("utf-8"))
        finally:
            os.close(fd)
    except FileExistsError:
        try:
            return bytes.fromhex(key_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return key
    except OSError as exc:
        logger.warning("hmac key persist failed (%s); using ephemeral key", exc)
    return key


def _cache_mac(key: bytes, blob: bytes) -> str:
    """Keyed HMAC-SHA256 authentication tag (hex) for a cache blob."""
    return hmac.new(key, blob, hashlib.sha256).hexdigest()


def _read_pickle_cache(cache_path: Path) -> dict[str, pd.DataFrame] | None:
    """Load a pickle cache, authenticating its keyed HMAC sidecar. None on failure.

    The HMAC is verified before ``pickle.loads`` so a locally-tampered blob whose
    sidecar was forged without the secret is rejected, never deserialized.
    """
    import pickle

    sidecar = _sha256_path(cache_path)
    try:
        blob = cache_path.read_bytes()
    except OSError as exc:
        logger.warning("cache read failed (%s); refetching", exc)
        return None

    if not sidecar.is_file():
        logger.warning(
            "cache sidecar %s missing; refusing stale cache and refetching",
            sidecar.name,
        )
        return None
    try:
        expected = sidecar.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("cache sidecar read failed (%s); refetching", exc)
        return None
    actual = _cache_mac(_cache_hmac_key(cache_path.parent), blob)
    if not _hashes_equal(expected, actual):
        logger.warning(
            "cache integrity mismatch for %s (expected %s..., got %s...); refetching",
            cache_path.name, expected[:12], actual[:12],
        )
        return None

    try:
        cached = pickle.loads(blob)  # noqa: S301 — local cache, HMAC-authenticated above
    except Exception as exc:  # noqa: BLE001 — degrade to fresh fetch
        logger.warning("cache unpickle failed (%s); refetching", exc)
        return None
    if not isinstance(cached, dict) or "close" not in cached:
        logger.warning("cache %s has unexpected shape; refetching", cache_path.name)
        return None
    return cached


def _write_pickle_cache(
    cache_dir: Path, cache_path: Path, panel: dict[str, Any]
) -> None:
    """Pickle ``panel`` + write its keyed HMAC sidecar. Failures are non-fatal."""
    import pickle

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        blob = pickle.dumps(panel, protocol=pickle.HIGHEST_PROTOCOL)
        cache_path.write_bytes(blob)
        _sha256_path(cache_path).write_text(
            _cache_mac(_cache_hmac_key(cache_dir), blob), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001 — cache miss is non-fatal
        logger.warning("cache write failed: %s", exc)


def _hashes_equal(a: str, b: str) -> bool:
    """Constant-time comparison of two hex authentication tags."""
    return hmac.compare_digest(a.strip().lower(), b.strip().lower())


# ---------------------------------------------------------------------------
# Universe loaders
# ---------------------------------------------------------------------------


_CSI300_FALLBACK_CODES = [
    # Blue-chip A-share representatives — used only when index_weight fails.
    # Hand-picked across sectors so a degraded run still gives diverse signal.
    "600519.SH", "601318.SH", "600036.SH", "000333.SZ", "000858.SZ",
    "601166.SH", "600276.SH", "601398.SH", "601288.SH", "600030.SH",
    "600887.SH", "601012.SH", "601888.SH", "000651.SZ", "600028.SH",
    "601628.SH", "600000.SH", "601088.SH", "601857.SH", "600009.SH",
    "601899.SH", "002594.SZ", "600585.SH", "300750.SZ", "601658.SH",
    "600048.SH", "601138.SH", "601668.SH", "000001.SZ", "000002.SZ",
]


# Hand-picked US large-cap representatives. Used when Wikipedia fetch fails.
_SP500_FALLBACK_CODES = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "BRK-B",
    "JPM", "JNJ", "V", "PG", "UNH", "MA", "HD", "XOM", "LLY", "MRK",
    "PEP", "KO", "ABBV", "AVGO", "CVX", "WMT", "COST", "ADBE", "MCD",
    "CRM", "ACN", "BAC", "TMO", "ORCL", "CSCO", "ABT", "WFC", "DHR",
    "VZ", "PFE", "INTC", "DIS", "CMCSA", "AMD", "TXN", "PM", "QCOM",
    "NEE", "RTX", "HON", "T", "IBM",
]


def _ts_code_from_a_share_digits(digits: str) -> str:
    """Map a 6-digit A-share code to a Tushare-style ``ts_code``."""
    code = str(digits).strip().zfill(6)
    if code.startswith(("5", "6", "9")):
        return f"{code}.SH"
    return f"{code}.SZ"


def _fetch_csi300_constituents_akshare() -> tuple[list[str], str | None]:
    """CSI 300 roster via akshare when Tushare ``index_weight`` is unavailable."""
    try:
        import akshare as ak
    except ImportError:
        return [], None
    try:
        frame = ak.index_stock_cons_csindex(symbol="000300")
    except Exception as exc:  # noqa: BLE001
        logger.warning("csi300 akshare constituents failed: %s", exc)
        return [], None
    if frame is None or frame.empty:
        return [], None

    code_col = None
    for col in frame.columns:
        name = str(col)
        if "成分" in name and "代码" in name:
            code_col = col
            break
    if code_col is None and len(frame.columns) > 4:
        code_col = frame.columns[4]

    if code_col is None:
        return [], None

    raw_codes = {
        str(value).strip().zfill(6)
        for value in frame[code_col].astype(str)
        if str(value).strip().isdigit() and len(str(value).strip()) == 6
    }
    raw_codes -= {"000300", "399300", "399006", "000016", "000905", "000852"}
    codes = sorted(_ts_code_from_a_share_digits(code) for code in raw_codes)
    as_of = str(frame.iloc[0, 0]) if len(frame) else None
    logger.info(
        "csi300: %d names from akshare index_stock_cons_csindex (as of %s)",
        len(codes),
        as_of,
    )
    return codes, as_of


def _normalize_akshare_a_share_daily(df: pd.DataFrame) -> pd.DataFrame | None:
    """Normalize ``stock_zh_a_hist`` output to the bench panel schema."""
    if df is None or df.empty:
        return None
    work = df.copy()
    rename = {
        "日期": "trade_date",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "成交量": "volume",
        "成交额": "amount_raw",
    }
    work = work.rename(columns={key: val for key, val in rename.items() if key in work.columns})
    if "trade_date" not in work.columns:
        return None
    work["trade_date"] = pd.to_datetime(work["trade_date"], errors="coerce")
    work = work.dropna(subset=["trade_date"]).set_index("trade_date").sort_index()
    for col in ("open", "high", "low", "close", "volume"):
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")
    if "amount_raw" in work.columns:
        # Match Tushare ``amount`` units (千元) so VWAP math stays shared.
        work["amount"] = pd.to_numeric(work["amount_raw"], errors="coerce") / 1000.0
    keep = [c for c in ("open", "high", "low", "close", "volume", "amount") if c in work.columns]
    out = work[keep].dropna(subset=["open", "high", "low", "close"])
    return out if not out.empty else None


def _fetch_csi300_symbol_akshare(code: str, start: str, end: str) -> pd.DataFrame | None:
    """Fetch one A-share via akshare forward-adjusted daily bars."""
    try:
        import akshare as ak
    except ImportError:
        return None
    symbol = code.split(".")[0]
    sd = start.replace("-", "")
    ed = end.replace("-", "")
    try:
        raw = ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=sd,
            end_date=ed,
            adjust="qfq",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("csi300 akshare %s failed: %s", code, exc)
        return None
    return _normalize_akshare_a_share_daily(raw)


def _fetch_csi300_prices_akshare(
    codes: list[str], start: str, end: str
) -> dict[str, pd.DataFrame]:
    """Parallel akshare fetch used when Tushare ``adj_factor`` is unavailable."""
    fetched: dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=_CSI300_FETCH_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_csi300_symbol_akshare, code, start, end): code
            for code in codes
        }
        for fut in as_completed(futures):
            code = futures[fut]
            try:
                frame = fut.result()
            except Exception as exc:  # noqa: BLE001
                logger.warning("csi300 akshare worker raised for %s: %s", code, exc)
                continue
            if frame is not None and not frame.empty:
                fetched[code] = frame
    return fetched


# QMT Bridge ``market_data_ex`` accepts comma-separated lists; keep batches modest
# for URL length and xtdata latency.
_CSI300_QMT_BATCH = 50


def _normalize_qmt_daily_for_bench(df: pd.DataFrame) -> pd.DataFrame | None:
    """Map QMT OHLCV (volume in shares) to the Tushare bench panel schema."""
    if df is None or df.empty:
        return None
    work = df.copy()
    required = ("open", "high", "low", "close", "volume")
    if not all(col in work.columns for col in required):
        return None
    vol_shares = pd.to_numeric(work["volume"], errors="coerce")
    close = pd.to_numeric(work["close"], errors="coerce")
    for col in ("open", "high", "low", "close"):
        work[col] = pd.to_numeric(work[col], errors="coerce")
    # Bench VWAP math expects Tushare units: volume in 手, amount in 千元.
    work["volume"] = vol_shares / 100.0
    if "amount" in work.columns:
        work["amount"] = pd.to_numeric(work["amount"], errors="coerce") / 1000.0
    else:
        work["amount"] = vol_shares * close / 1000.0
    keep = ["open", "high", "low", "close", "volume", "amount"]
    out = work[keep].dropna(subset=["open", "high", "low", "close"])
    return out if not out.empty else None


def _fetch_csi300_prices_qmt(
    codes: list[str], start: str, end: str
) -> dict[str, pd.DataFrame]:
    """Batch-fetch A-share qfq daily bars via QMT Bridge ``market_data_ex``."""
    if not codes:
        return {}
    try:
        from backtest.loaders.qmt_loader import DataLoader
    except ImportError:
        return {}

    loader = DataLoader()
    fetched: dict[str, pd.DataFrame] = {}
    for offset in range(0, len(codes), _CSI300_QMT_BATCH):
        batch = codes[offset : offset + _CSI300_QMT_BATCH]
        try:
            chunk = loader.fetch(batch, start, end, interval="1D")
        except Exception as exc:  # noqa: BLE001
            logger.warning("csi300 qmt batch %d failed: %s", offset // _CSI300_QMT_BATCH, exc)
            continue
        for code, frame in chunk.items():
            normalized = _normalize_qmt_daily_for_bench(frame)
            if normalized is not None:
                fetched[code] = normalized
    if fetched:
        logger.info(
            "csi300: %d/%d names from QMT Bridge (qfq via dividend_type=front)",
            len(fetched),
            len(codes),
        )
    return fetched


def _load_csi300_panel(start: str, end: str) -> dict[str, pd.DataFrame]:
    """CSI 300 panel via Tushare. Includes ``amount`` (required by gtja191).

    Constituents are taken from the most recent ``index_weight`` snapshot in
    the requested window; if that call fails we degrade to a 30-name
    blue-chip fallback so the bench still runs.
    """
    token = get_env_config().data.tushare_token.strip()
    has_tushare = bool(token) and token != "your-tushare-token"
    pro = None
    if has_tushare:
        try:
            import tushare as ts
        except ImportError as exc:
            logger.warning("tushare not installed (%s); csi300 will use akshare", exc)
        else:
            pro = ts.pro_api(token)
    else:
        logger.warning("TUSHARE_TOKEN not set; csi300 will use akshare for data")

    sd = start.replace("-", "")
    ed = end.replace("-", "")

    codes: list[str] = []
    constituent_source = "tushare index_weight"
    constituent_source_date: str | None = None
    membership: pd.DataFrame | None = None
    if pro is not None:
        try:
            # Reach back before ``start`` so the snapshot that was in force on the
            # first requested day is included; Tushare publishes month-end rosters.
            lookback = (pd.Timestamp(start) - pd.Timedelta(days=60)).strftime("%Y%m%d")
            weights = pro.index_weight(
                index_code="399300.SZ", start_date=lookback, end_date=ed
            )
            if weights is not None and not weights.empty:
                frame = weights.copy()
                frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
                frame = frame.dropna(subset=["trade_date", "con_code"])
                constituent_source_date = str(weights["trade_date"].max())
                # Every name that was a member at any point in the window, so the
                # panel can carry a name that later left the index.
                codes = sorted(frame["con_code"].astype(str).unique())
                membership = (
                    frame.assign(_member=True)
                    .pivot_table(
                        index="trade_date",
                        columns="con_code",
                        values="_member",
                        aggfunc="first",
                    )
                    .notna()
                    .sort_index()
                )
                logger.info(
                    "csi300: %d names ever a member across %d roster snapshots",
                    len(codes),
                    len(membership),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("csi300 index_weight failed (%s); trying akshare roster", exc)

    if not codes:
        ak_codes, ak_as_of = _fetch_csi300_constituents_akshare()
        if ak_codes:
            codes = ak_codes
            constituent_source = "akshare index_stock_cons_csindex"
            constituent_source_date = ak_as_of
        else:
            codes = list(_CSI300_FALLBACK_CODES)
            constituent_source = "hand-picked fallback"
            constituent_source_date = None
            logger.warning("csi300: using %d-name fallback (degraded run)", len(codes))

    price_adjustment = "tushare qfq via adj_factor"
    fetched: dict[str, pd.DataFrame] = {}

    if pro is not None:
        # Fetch raw daily in parallel — we need ``amount`` which the standard
        # loader drops. Tushare's free tier permits ~200 calls/min so 4 concurrent
        # workers is comfortably under the rate limit even for a full 300-name list.
        def _fetch_one(code: str) -> tuple[str, pd.DataFrame | None]:
            df = _retry(lambda: pro.daily(ts_code=code, start_date=sd, end_date=ed))
            if df is None or df.empty:
                return code, None
            df = df.sort_values("trade_date").copy()
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            df = df.set_index("trade_date")
            df = df.rename(columns={"vol": "volume"})
            for col in ("open", "high", "low", "close", "volume", "amount"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            keep = [
                c for c in ("open", "high", "low", "close", "volume", "amount") if c in df.columns
            ]
            df = df[keep].dropna(subset=["open", "high", "low", "close"])
            factor = _retry(lambda: pro.adj_factor(ts_code=code, start_date=sd, end_date=ed))
            adjusted = _apply_qfq(df, factor)
            if adjusted is not None:
                return code, adjusted
            bar = _retry(
                lambda: ts.pro_bar(ts_code=code, adj="qfq", start_date=sd, end_date=ed),
            )
            if bar is None or bar.empty:
                return code, None
            bar = bar.sort_values("trade_date").copy()
            bar["trade_date"] = pd.to_datetime(bar["trade_date"], errors="coerce")
            bar = bar.set_index("trade_date")
            bar = bar.rename(columns={"vol": "volume"})
            for col in ("open", "high", "low", "close", "volume", "amount"):
                if col in bar.columns:
                    bar[col] = pd.to_numeric(bar[col], errors="coerce")
            keep = [
                c for c in ("open", "high", "low", "close", "volume", "amount") if c in bar.columns
            ]
            bar = bar[keep].dropna(subset=["open", "high", "low", "close"])
            return code, bar if not bar.empty else None

        with ThreadPoolExecutor(max_workers=_CSI300_FETCH_WORKERS) as pool:
            futures = [pool.submit(_fetch_one, code) for code in codes]
            for fut in as_completed(futures):
                try:
                    code, frame = fut.result()
                except Exception as exc:  # noqa: BLE001 — _retry already logged
                    logger.warning("csi300 fetch worker raised: %s", exc)
                    continue
                if frame is not None and not frame.empty:
                    fetched[code] = frame

    if not fetched:
        logger.warning(
            "csi300: tushare daily/adj_factor returned 0 usable symbols; "
            "trying QMT Bridge qfq"
        )
        fetched = _fetch_csi300_prices_qmt(codes, start, end)
        if fetched:
            price_adjustment = "qmt bridge qfq (dividend_type=front)"

    if not fetched:
        logger.warning("csi300: QMT Bridge returned 0 symbols; falling back to akshare qfq")
        fetched = _fetch_csi300_prices_akshare(codes, start, end)
        price_adjustment = "akshare qfq"

    # A name with no usable adjustment factors is dropped rather than benched on
    # raw prices, so the drop has to be visible or it becomes its own silent bias.
    dropped = sorted(set(codes) - set(fetched))
    if not fetched:
        raise RuntimeError(
            "csi300: no symbol survived price fetch — Tushare adj_factor returned "
            "nothing usable (token may lack adj_factor permission), QMT Bridge "
            "was unreachable or returned empty bars, and the akshare qfq fallback "
            "also failed. Ensure QMT Bridge is running (`qmt-server`), install "
            "akshare (`pip install akshare`), or upgrade the Tushare token, then retry."
        )
    if dropped:
        logger.warning(
            "csi300: dropped %d/%d name(s) with no usable adjustment factors: %s",
            len(dropped),
            len(codes),
            ", ".join(dropped[:10]) + ("..." if len(dropped) > 10 else ""),
        )

    panel = _wide_from_fetched(fetched, include_amount=True)
    # CN equity vwap: Tushare ``amount`` is in 千元, ``volume`` in 手. True VWAP
    # = (amount * 1000 CNY) / (volume * 100 shares). Matches
    # ``src.factors.base.vwap(EQUITY_CN)``.
    if "amount" in panel and "volume" in panel:
        from src.factors.base import safe_div

        panel["vwap"] = safe_div(
            panel["amount"] * 1000.0, panel["volume"] * 100.0 + 1.0
        )

    # Restrict each date's cross-section to the names that were index members on
    # that date. Without this the panel carries today's roster back through the
    # whole window, so a name is only present because it survived to the end —
    # every IC is then measured on a set selected with hindsight.
    if membership is not None:
        mask = (
            membership.reindex(columns=panel["close"].columns)
            .reindex(index=panel["close"].index.union(membership.index))
            .ffill()
            .reindex(panel["close"].index)
            .bfill()
            .fillna(False)
            .astype(bool)
        )
        for key, frame in panel.items():
            if isinstance(frame, pd.DataFrame):
                panel[key] = frame.where(mask)

    panel["_meta"] = {
        "universe": "csi300",
        # True only on the degraded path: the hand-picked fallback is a
        # survivor-selected static roster with no point-in-time membership.
        "survivorship_bias": membership is None,
        "pit_membership": membership is not None,
        "degraded": constituent_source != "tushare index_weight",
        "constituent_source": constituent_source,
        "constituent_source_date": constituent_source_date,
        "constituent_count": len(codes),
        "price_adjustment": price_adjustment,
        "dropped_unadjustable": len(dropped),
    }
    return panel


def _load_sp500_panel(start: str, end: str) -> dict[str, pd.DataFrame]:
    """SP500 panel via yfinance. Adds vwap = (O+H+L+C)/4 fallback for alpha101.

    Survivorship-bias warning: ``_fetch_sp500_constituents`` returns Wikipedia's
    *current* member list, not a point-in-time snapshot. Names that dropped out
    of the index during ``start..end`` (delistings, mergers, downgrades) are
    silently excluded — so IC stats are biased upward. The caller (bench
    runner) surfaces this in the bench summary's ``meta`` block via the
    ``_meta`` panel key set below.
    """
    codes, sectors = _fetch_sp500_constituents()
    constituent_source = "wikipedia"
    constituent_source_date: str | None = _SP500_CONSTITUENT_SOURCE_DATE
    if not codes:
        codes = list(_SP500_FALLBACK_CODES)
        sectors = {}
        constituent_source = "hand-picked fallback"
        constituent_source_date = None
        logger.warning("sp500: using %d-name fallback (degraded run)", len(codes))

    logger.warning(
        "SP500 universe uses current constituents (%s @ %s) → survivorship-biased",
        constituent_source,
        constituent_source_date,
    )

    # yfinance loader expects project-style symbols (``AAPL.US``).
    project_codes = [f"{c}.US" for c in codes]
    from backtest.loaders.registry import resolve_loader

    loader = resolve_loader("us_equity")
    fetched = _retry(lambda: loader.fetch(project_codes, start, end)) or {}

    panel = _wide_from_fetched(fetched, include_amount=False)
    # Synthetic vwap for alpha101 alphas that require it on US universe
    if all(k in panel for k in ("open", "high", "low", "close")):
        panel["vwap"] = (panel["open"] + panel["high"] + panel["low"] + panel["close"]) / 4.0

    # 19 alpha101 alphas are industry-neutralized and the registry refuses them
    # outright when the panel carries no ``sector`` tag, so the bench reported
    # n_skipped=19 on every SP500 run. The labels come from the same Wikipedia
    # table the constituents do — no extra request, no per-name lookup.
    sector_coverage = 0.0
    if sectors and "close" in panel and not panel["close"].empty:
        columns = panel["close"].columns
        labels = [sectors.get(str(code).removesuffix(".US"), "") for code in columns]
        sector_coverage = sum(1 for label in labels if label) / len(labels)
        # A mostly-unlabelled panel would demean one big "unknown" bucket, which
        # is the global-demean fallback wearing a sector tag. Say so instead.
        if sector_coverage >= _SP500_MIN_SECTOR_COVERAGE:
            panel["sector"] = pd.DataFrame(
                np.repeat(
                    np.array([label or "UNKNOWN" for label in labels], dtype=object)[None, :],
                    len(panel["close"].index),
                    axis=0,
                ),
                index=panel["close"].index,
                columns=columns,
            )
        else:
            logger.warning(
                "sp500: sector coverage %.1f%% below %.0f%% — leaving the tag off "
                "so industry-neutralized alphas skip rather than demean one bucket",
                sector_coverage * 100,
                _SP500_MIN_SECTOR_COVERAGE * 100,
            )

    # Attach a non-DataFrame metadata blob. Registry.compute() only iterates
    # required column names, so this extra key is ignored by the compute path.
    panel["_meta"] = {
        "universe": "sp500",
        "survivorship_bias": True,
        "degraded": constituent_source == "hand-picked fallback",
        "constituent_source": constituent_source,
        "constituent_source_date": constituent_source_date,
        "constituent_count": len(codes),
        "sector_source": "wikipedia GICS" if "sector" in panel else None,
        "sector_coverage": round(sector_coverage, 4),
    }
    return panel


def _fetch_sp500_constituents() -> tuple[list[str], dict[str, str]]:
    """Pull current S&P 500 tickers and GICS sectors from Wikipedia.

    The sector labels ride along in the table we already request, so the 19
    industry-neutralized alpha101 alphas cost no extra call. Returns
    ``([], {})`` on any failure.
    """
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    try:
        import io

        import requests

        resp = requests.get(
            url,
            headers={
                "User-Agent": (
                    "Vibe-Trading/0.1 (research bench; "
                    "https://github.com/HKUDS/Vibe-Trading)"
                )
            },
            timeout=20,
        )
        resp.raise_for_status()
        tables = pd.read_html(io.StringIO(resp.text))
        for tbl in tables:
            if "Symbol" in tbl.columns:
                # yfinance prefers ``BRK-B`` over ``BRK.B`` — normalise
                symbols = tbl["Symbol"].astype(str).str.strip().str.replace(".", "-", regex=False)
                keep = symbols.ne("") & symbols.ne("nan")
                tickers = symbols[keep].tolist()
                sectors: dict[str, str] = {}
                if "GICS Sector" in tbl.columns:
                    labels = tbl["GICS Sector"].astype(str).str.strip()
                    sectors = {
                        symbol: label
                        for symbol, label in zip(symbols[keep], labels[keep])
                        if label and label != "nan"
                    }
                logger.info(
                    "sp500: %d tickers from Wikipedia (%d with a GICS sector)",
                    len(tickers),
                    len(sectors),
                )
                return tickers, sectors
    except Exception as exc:  # noqa: BLE001
        logger.warning("sp500 Wikipedia fetch failed: %s", exc)
    return [], {}


def _load_btc_panel(start: str, end: str) -> dict[str, pd.DataFrame]:
    """Single-instrument BTC-USDT panel via OKX. Adds vwap = typical price."""
    from backtest.loaders.registry import resolve_loader

    loader = resolve_loader("crypto")
    fetched = _retry(lambda: loader.fetch(["BTC-USDT"], start, end)) or {}
    panel = _wide_from_fetched(fetched, include_amount=False)
    if all(k in panel for k in ("open", "high", "low", "close")):
        panel["vwap"] = (panel["open"] + panel["high"] + panel["low"] + panel["close"]) / 4.0
    return panel


def _wide_from_fetched(
    fetched: dict[str, pd.DataFrame], *, include_amount: bool
) -> dict[str, pd.DataFrame]:
    """Stack per-code OHLCV frames into wide panels keyed by field."""
    if not fetched:
        return {}
    all_dates = sorted(set().union(*(df.index for df in fetched.values())))
    if not all_dates:
        return {}
    all_codes = sorted(fetched.keys())
    date_index = pd.DatetimeIndex(all_dates)
    fields = ["open", "high", "low", "close", "volume"]
    if include_amount:
        fields.append("amount")

    panel: dict[str, pd.DataFrame] = {}
    for field in fields:
        present = {
            code: df[field] for code, df in fetched.items() if field in df.columns
        }
        if not present:
            continue
        # pd.concat over a dict of Series gives a wide frame with codes as
        # columns in one pass — avoids the per-code reindex+DataFrame build.
        wide = pd.concat(present, axis=1)
        wide = wide.reindex(index=date_index, columns=all_codes)
        panel[field] = wide.astype(float)
    return panel


def _retry(fn, *, tries: int = 3, base_delay: float = 1.0):
    """Call ``fn`` up to ``tries`` times with exponential backoff."""
    import time

    last_exc: Exception | None = None
    for attempt in range(tries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — recoverable network errors
            last_exc = exc
            if attempt == tries - 1:
                break
            delay = base_delay * (2 ** attempt)
            logger.debug("retry %d/%d after %.1fs: %s", attempt + 1, tries, delay, exc)
            time.sleep(delay)
    if last_exc is not None:
        logger.warning("retry exhausted: %s", last_exc)
    return None


# ---------------------------------------------------------------------------
# Per-alpha IC bench
# ---------------------------------------------------------------------------


def _compute_forward_returns(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Next-bar forward simple returns from close, aligned to factor timestamp."""
    close = panel.get("close")
    if close is None:
        raise ValueError("panel missing 'close' — cannot derive forward returns")
    # Next-period return aligned to current row (use t+1 close, shift back).
    fwd = close.pct_change(fill_method=None).shift(-1)
    return fwd


def _bench_one_alpha(
    registry: Any,
    alpha_id: str,
    panel: dict[str, pd.DataFrame],
    return_df: pd.DataFrame,
) -> dict[str, Any]:
    """Compute IC stats for one alpha. Returns a dict, may raise SkipAlpha / RegistryError."""
    from src.factors.factor_analysis_core import compute_ic_series  # local import

    factor_df = registry.compute(alpha_id, panel)
    ic_series = compute_ic_series(factor_df, return_df)
    if ic_series.empty:
        raise RuntimeError(
            f"{alpha_id}: IC series empty — insufficient overlap between factor and returns"
        )
    ic_mean = float(ic_series.mean())
    ic_std = float(ic_series.std())
    ir = ic_mean / ic_std if ic_std > 0 else 0.0
    ic_pos = float((ic_series > 0).mean())
    alpha = registry.get(alpha_id)
    meta = alpha.meta or {}
    return {
        "id": alpha_id,
        "zoo": alpha.zoo,
        "theme": meta.get("theme", []),
        "formula_latex": meta.get("formula_latex", ""),
        "ic_mean": round(ic_mean, 6),
        "ic_std": round(ic_std, 6),
        "ir": round(ir, 4),
        "ic_positive_ratio": round(ic_pos, 4),
        "ic_count": int(len(ic_series)),
    }


def _select_alpha_ids(
    registry: Any, *, alpha_id: str | None, zoo: str | None
) -> list[str]:
    if alpha_id and zoo:
        raise ValueError("alpha_id and zoo are mutually exclusive")
    if alpha_id:
        registry.get(alpha_id)  # raises KeyError if unknown
        return [alpha_id]
    if zoo:
        return registry.list(zoo=zoo)
    return registry.list()


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_CSP = (
    "<meta http-equiv=\"Content-Security-Policy\" "
    "content=\"default-src 'none'; style-src 'unsafe-inline'; script-src 'none'\">"
)

_REPORT_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 2em;
       color: #222; background: #fafafa; }
h1, h2 { color: #111; }
table { border-collapse: collapse; width: 100%; background: #fff; }
th, td { padding: .5em .75em; border-bottom: 1px solid #e5e5e5; text-align: left; }
th { background: #f0f0f0; }
.meta { color: #666; font-size: .9em; margin-bottom: 1.5em; }
.formula { font-family: monospace; background: #f4f4f4; padding: .25em .5em; }
.skipped { color: #a33; font-size: .9em; }
.bias-warning { background: #fff4e5; border: 1px solid #e0a800; border-left-width: 4px;
       color: #7a4d00; padding: .75em 1em; margin-bottom: 1.5em; font-size: .95em; }
"""

_JINJA_TEMPLATE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
{{ csp | safe }}
<title>Alpha Bench Report</title>
<style>{{ css }}</style>
</head><body>
<h1>Alpha Bench Report</h1>
<div class="meta">
  Generated {{ generated_at }} &middot; Universe {{ universe }} &middot;
  Period {{ period }} &middot; {{ n_alphas_tested }} tested, {{ n_skipped }} skipped
</div>
{% if meta and meta.survivorship_bias %}
<div class="bias-warning">
  <strong>Survivorship bias:</strong> universe membership is the current constituent
  list{% if meta.constituent_source %} ({{ meta.constituent_source }}{% if meta.constituent_source_date %}, as of {{ meta.constituent_source_date }}{% endif %}){% endif %},
  so delisted and removed names are absent. IC statistics in this report are biased upward.
</div>
{% endif %}

<h2>Top {{ top|length }} by IR</h2>
<table>
<tr><th>#</th><th>Alpha ID</th><th>Zoo</th><th>Theme</th>
    <th>IC mean</th><th>IC std</th><th>IR</th><th>IC+ ratio</th><th>N</th></tr>
{% for row in top %}
<tr>
  <td>{{ loop.index }}</td>
  <td>{{ row.id }}</td>
  <td>{{ row.zoo }}</td>
  <td>{{ row.theme | join(", ") }}</td>
  <td>{{ "%.4f"|format(row.ic_mean) }}</td>
  <td>{{ "%.4f"|format(row.ic_std) }}</td>
  <td>{{ "%.4f"|format(row.ir) }}</td>
  <td>{{ "%.4f"|format(row.ic_positive_ratio) }}</td>
  <td>{{ row.ic_count }}</td>
</tr>
{% endfor %}
</table>

{% if strict %}
<h2>Strict gate</h2>
<div class="meta">
  Alpha t-stats against the same-universe random control. The strict gate
  decides on these, not on IC.
</div>
<table>
<tr><th>Alpha ID</th><th>alpha_t full</th><th>alpha_t train</th>
    <th>alpha_t test</th><th>random IC mean</th><th>Category</th></tr>
{% for row in top %}
<tr>
  <td>{{ row.id }}</td>
  <td>{{ "%.4f"|format(row.get('alpha_t_full')) if row.get('alpha_t_full') is not none else "n/a" }}</td>
  <td>{{ "%.4f"|format(row.get('alpha_t_train')) if row.get('alpha_t_train') is not none else "n/a" }}</td>
  <td>{{ "%.4f"|format(row.get('alpha_t_test')) if row.get('alpha_t_test') is not none else "n/a" }}</td>
  <td>{{ "%.6f"|format(row.get('random_ic_mean')) if row.get('random_ic_mean') is not none else "n/a" }}</td>
  <td>{{ row.category }}</td>
</tr>
{% endfor %}
</table>
{% endif %}

<h2>Formulas</h2>
<table>
<tr><th>Alpha ID</th><th>Formula (LaTeX source)</th></tr>
{% for row in top %}
<tr><td>{{ row.id }}</td><td class="formula">{{ row.formula_latex }}</td></tr>
{% endfor %}
</table>

{% if failures %}
<h2 class="skipped">Skipped / Failed ({{ failures|length }} shown)</h2>
<table>
<tr><th>Alpha ID</th><th>Reason</th></tr>
{% for f in failures %}
<tr><td>{{ f.alpha_id }}</td><td>{{ f.reason }}</td></tr>
{% endfor %}
</table>
{% endif %}
</body></html>
"""


def _render_html(context: dict[str, Any]) -> str:
    """Render with Jinja2 autoescape if available; else manual ``html.escape``."""
    try:
        from jinja2 import Environment, select_autoescape

        env = Environment(autoescape=select_autoescape(["html", "xml"]))
        return env.from_string(_JINJA_TEMPLATE).render(**context)
    except ImportError:
        return _render_html_manual(context)


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _render_html_manual(ctx: dict[str, Any]) -> str:
    """Hand-rolled fallback. Every interpolated value goes through html.escape."""
    parts: list[str] = [
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        _CSP,
        f"<title>Alpha Bench Report</title><style>{_REPORT_CSS}</style></head><body>",
        "<h1>Alpha Bench Report</h1>",
        "<div class=\"meta\">Generated ",
        _esc(ctx["generated_at"]),
        " &middot; Universe ",
        _esc(ctx["universe"]),
        " &middot; Period ",
        _esc(ctx["period"]),
        f" &middot; {int(ctx['n_alphas_tested'])} tested, {int(ctx['n_skipped'])} skipped",
        "</div>",
    ]
    _meta = ctx.get("meta") or {}
    if _meta.get("survivorship_bias"):
        source = _meta.get("constituent_source")
        as_of = _meta.get("constituent_source_date")
        provenance = ""
        if source:
            provenance = f" ({_esc(source)}"
            provenance += f", as of {_esc(as_of)})" if as_of else ")"
        parts.append(
            "<div class=\"bias-warning\"><strong>Survivorship bias:</strong> universe "
            f"membership is the current constituent list{provenance}, so delisted and "
            "removed names are absent. IC statistics in this report are biased upward."
            "</div>"
        )
    parts += [
        f"<h2>Top {len(ctx['top'])} by IR</h2><table>",
        "<tr><th>#</th><th>Alpha ID</th><th>Zoo</th><th>Theme</th>"
        "<th>IC mean</th><th>IC std</th><th>IR</th><th>IC+ ratio</th><th>N</th></tr>",
    ]
    for i, row in enumerate(ctx["top"], start=1):
        ic_mean = _esc(f"{row['ic_mean']:.4f}")
        ic_std = _esc(f"{row['ic_std']:.4f}")
        ir = _esc(f"{row['ir']:.4f}")
        ic_pos = _esc(f"{row['ic_positive_ratio']:.4f}")
        parts.append(
            f"<tr><td>{i}</td>"
            f"<td>{_esc(row['id'])}</td>"
            f"<td>{_esc(row['zoo'])}</td>"
            f"<td>{_esc(', '.join(row['theme']))}</td>"
            f"<td>{ic_mean}</td>"
            f"<td>{ic_std}</td>"
            f"<td>{ir}</td>"
            f"<td>{ic_pos}</td>"
            f"<td>{_esc(row['ic_count'])}</td></tr>"
        )
    parts.append("</table>")
    if ctx.get("strict"):
        parts.append(
            "<h2>Strict gate</h2>"
            "<div class=\"meta\">Alpha t-stats against the same-universe random "
            "control. The strict gate decides on these, not on IC.</div><table>"
            "<tr><th>Alpha ID</th><th>alpha_t full</th><th>alpha_t train</th>"
            "<th>alpha_t test</th><th>random IC mean</th><th>Category</th></tr>"
        )

        def _fmt(value: Any, places: int = 4) -> str:
            return "n/a" if value is None else _esc(f"{value:.{places}f}")

        for row in ctx["top"]:
            parts.append(
                f"<tr><td>{_esc(row['id'])}</td>"
                f"<td>{_fmt(row.get('alpha_t_full'))}</td>"
                f"<td>{_fmt(row.get('alpha_t_train'))}</td>"
                f"<td>{_fmt(row.get('alpha_t_test'))}</td>"
                f"<td>{_fmt(row.get('random_ic_mean'), 6)}</td>"
                f"<td>{_esc(row.get('category'))}</td></tr>"
            )
        parts.append("</table>")
    parts.append("<h2>Formulas</h2><table>")
    parts.append("<tr><th>Alpha ID</th><th>Formula (LaTeX source)</th></tr>")
    for row in ctx["top"]:
        parts.append(
            f"<tr><td>{_esc(row['id'])}</td>"
            f"<td class=\"formula\">{_esc(row['formula_latex'])}</td></tr>"
        )
    parts.append("</table>")
    failures = ctx.get("failures") or []
    if failures:
        parts.append(
            f"<h2 class=\"skipped\">Skipped / Failed ({len(failures)} shown)</h2><table>"
        )
        parts.append("<tr><th>Alpha ID</th><th>Reason</th></tr>")
        for f in failures:
            parts.append(
                f"<tr><td>{_esc(f['alpha_id'])}</td><td>{_esc(f['reason'])}</td></tr>"
            )
        parts.append("</table>")
    parts.append("</body></html>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _default_output_dir() -> Path:
    return Path.home() / ".vibe-trading" / "reports"


def run_alpha_bench(**kwargs: Any) -> dict[str, Any]:
    """Run the bench and return a parsed envelope (dict, not JSON string)."""
    universe = kwargs.get("universe")
    period = kwargs.get("period")
    if not universe or not isinstance(universe, str):
        return {"status": "error", "error": "universe is required (string)"}
    if not period or not isinstance(period, str):
        return {"status": "error", "error": "period is required (string)"}

    try:
        _parse_period(period)
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}

    top_n = int(kwargs.get("top", 20) or 20)
    if top_n <= 0:
        return {"status": "error", "error": "top must be > 0"}

    output_dir_raw = kwargs.get("output_dir") or str(_default_output_dir())
    output_dir = Path(output_dir_raw).expanduser().resolve()

    try:
        from src.factors.registry import (
            RegistryError,
            SkipAlpha,
            get_default_registry,
        )
    except Exception as exc:
        return {"status": "error", "error": f"registry import failed: {exc}"}

    try:
        registry = get_default_registry()
    except Exception as exc:
        logger.exception("Registry construction failed")
        return {"status": "error", "error": f"registry init failed: {exc}"}

    try:
        alpha_ids = _select_alpha_ids(
            registry, alpha_id=kwargs.get("alpha_id"), zoo=kwargs.get("zoo")
        )
    except (KeyError, ValueError) as exc:
        return {"status": "error", "error": str(exc)}

    if not alpha_ids:
        return {
            "status": "error",
            "error": "no alphas matched the selection (registry empty or filters too narrow)",
        }

    # Load panel — W4 universe loader fetches constituents + OHLCV (+ amount, vwap).
    try:
        panel = _load_universe_panel(universe, period)
    except (ValueError, NotImplementedError, RuntimeError) as exc:
        return {
            "status": "error",
            "error": str(exc),
            "n_alphas_tested": 0,
            "n_skipped": 0,
            "selected_alphas": alpha_ids[:50],
            "selected_total": len(alpha_ids),
        }

    try:
        return_df = _compute_forward_returns(panel)
    except Exception as exc:
        return {"status": "error", "error": f"forward returns failed: {exc}"}

    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for aid in alpha_ids:
        try:
            results.append(_bench_one_alpha(registry, aid, panel, return_df))
        except (SkipAlpha, RegistryError, RuntimeError, KeyError, ValueError) as exc:
            failures.append({"alpha_id": aid, "reason": str(exc)})
        except Exception as exc:  # noqa: BLE001 — isolate per-alpha failure
            logger.exception("alpha_bench unexpected failure on %s", aid)
            failures.append({"alpha_id": aid, "reason": f"unexpected: {exc}"})

    results.sort(key=lambda r: r["ir"], reverse=True)
    top = results[:top_n]

    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = output_dir / f"alpha_bench_{ts}_{secrets.token_hex(16)}.html"

    context = {
        "csp": _CSP,
        "css": _REPORT_CSS,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "universe": universe,
        "period": period,
        "n_alphas_tested": len(results),
        "n_skipped": len(failures),
        "top": top,
        "failures": failures[:10],
    }

    try:
        report_html = _render_html(context)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        report_fd = os.open(report_path, flags, 0o666)
        with os.fdopen(report_fd, "w", encoding="utf-8") as report_file:
            report_file.write(report_html)
    except OSError as exc:
        return {"status": "error", "error": f"failed to write report: {exc}"}

    return {
        "status": "ok",
        "report_path": str(report_path),
        "n_alphas_tested": len(results),
        "n_skipped": len(failures),
        "top": top,
    }


class AlphaBenchTool(BaseTool):
    """Bench one alpha or a whole zoo on a universe and emit an HTML IC report."""

    name = "alpha_bench"
    description = (
        "Bench a single alpha (alpha_id) or a whole zoo (zoo) on a universe over "
        "a period; computes IC mean/std/IR/positive-ratio per alpha and writes an "
        "HTML report. Returns aggregate stats only — no per-stock per-date payloads."
    )
    parameters = {
        "type": "object",
        "properties": {
            "alpha_id": {
                "type": "string",
                "description": "Bench a single alpha (mutually exclusive with zoo).",
            },
            "zoo": {
                "type": "string",
                "description": "Bench every alpha in a zoo (mutually exclusive with alpha_id).",
            },
            "universe": {
                "type": "string",
                "description": "csi300 | sp500 | btc-usdt (resolved via existing data tools).",
            },
            "period": {
                "type": "string",
                "description": "YYYY-YYYY or YYYY-MM-DD/YYYY-MM-DD.",
            },
            "top": {
                "type": "integer",
                "default": 20,
                "description": "Report the top-N alphas ranked by IR.",
            },
            "output_dir": {
                "type": "string",
                "description": "Where to write the HTML report; default ~/.vibe-trading/reports/.",
            },
        },
        "required": ["universe", "period"],
    }
    repeatable = True
    is_readonly = False

    def execute(self, **kwargs: Any) -> str:
        envelope = run_alpha_bench(**kwargs)
        return json.dumps(envelope, ensure_ascii=False)
