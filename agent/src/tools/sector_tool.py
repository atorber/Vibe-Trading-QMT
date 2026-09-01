"""Read-only sector / concept board tool backed by the Eastmoney client.

Eastmoney publishes a free, no-auth board taxonomy that groups A-shares into
industry sectors (行业板块) and thematic concept boards (概念板块). This tool
exposes two read-only views over that taxonomy:

* **Membership** — given a stock ``code``, list the industry / concept boards
  that stock belongs to. Served by the push2 ``slist`` endpoint, addressed by
  the same ``secid`` scheme used for klines.
* **Ranking** — with ``mode="ranking"``, rank the industry boards themselves by
  intraday percent change. Served by the push2 ``clist`` endpoint over the
  industry-board universe (``fs=m:90+t:2``).

Both endpoints route through :mod:`backtest.loaders.eastmoney_client` so every
request goes through the shared per-host throttle (Eastmoney rate-limits by IP
and bans bursting clients). Membership covers A-shares (``.SH`` / ``.SZ`` /
``.BJ``); ranking is the A-share board universe.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from backtest.engines._market_hooks import _detect_market
from backtest.loaders.eastmoney_client import get_json, resolve_secid
from src.agent.tools import BaseTool
from src.tools import akshare_fallbacks

logger = logging.getLogger(__name__)

# Eastmoney push2 board endpoints. ``slist`` returns the boards one stock
# belongs to; ``clist`` enumerates / ranks a board universe.
_MEMBERSHIP_URL = "https://push2.eastmoney.com/api/qt/slist/get"
_RANKING_URL = "https://push2.eastmoney.com/api/qt/clist/get"

# Field selectors. f12 = board/security code, f14 = name, f3 = change percent,
# f2 = latest price, f104/f105 = up/down constituent counts (ranking only).
_MEMBERSHIP_FIELDS = "f12,f13,f14,f3,f2"
_RANKING_FIELDS = "f12,f14,f3,f2,f104,f105,f128,f140"

# Industry-only selector (f13 = market marker distinguishing the stock row
# from its board row in the spt=1 single-industry response).
_INDUSTRY_FIELDS = "f12,f13,f14,f3"

# Board constituent list (clist) field selectors.
_PEER_FIELDS = "f12,f13,f14,f2,f3,f6"
_DEFAULT_PEER_LIMIT = 8
_MAX_PEER_LIMIT = 20

# Industry-board universe selector for the ranking view (m:90 = board market,
# t:2 = industry board sub-type). Sort by f3 (change percent), descending.
_RANKING_FS = "m:90+t:2"

# Defensive caps so a payload can never blow up the LLM context.
_MAX_RANKING = 100
_DEFAULT_RANKING = 30
_VALID_MODES = ("membership", "ranking")


def _error(message: str) -> str:
    """Build the failure envelope as a JSON string.

    Args:
        message: Human-readable error description.

    Returns:
        A ``{"ok": false, "error": ...}`` JSON string.
    """
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


def _as_float(value: Any) -> float | None:
    """Coerce an Eastmoney numeric cell to ``float``, or ``None`` if unusable.

    Eastmoney emits ``"-"`` for missing numerics; those map to ``None``.

    Args:
        value: Raw cell value from a push2 row.

    Returns:
        The float value, or ``None`` when the cell is missing / non-numeric.
    """
    if value is None or value == "-" or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _parse_membership_row(row: Any) -> dict[str, Any] | None:
    """Parse one ``slist`` diff row into a labelled board-membership dict.

    Args:
        row: One element of ``data.diff`` (a dict keyed by ``f12``/``f14``/...).

    Returns:
        A dict ``{board_code, board_name, change_pct, price}``, or ``None`` when
        the row lacks an identifiable board code/name.
    """
    if not isinstance(row, dict):
        return None
    board_code = row.get("f12")
    board_name = row.get("f14")
    if not board_code or not board_name:
        return None
    return {
        "board_code": str(board_code),
        "board_name": str(board_name),
        "change_pct": _as_float(row.get("f3")),
        "price": _as_float(row.get("f2")),
    }


def _parse_ranking_row(row: Any) -> dict[str, Any] | None:
    """Parse one ``clist`` diff row into a labelled board-ranking dict.

    Args:
        row: One element of ``data.diff`` (a dict keyed by ``f12``/``f14``/...).

    Returns:
        A dict ``{board_code, board_name, change_pct, index, leader, up_count,
        down_count}``, or ``None`` when the row lacks a board code/name.
    """
    if not isinstance(row, dict):
        return None
    board_code = row.get("f12")
    board_name = row.get("f14")
    if not board_code or not board_name:
        return None
    leader = row.get("f140")
    return {
        "board_code": str(board_code),
        "board_name": str(board_name),
        "change_pct": _as_float(row.get("f3")),
        "index": _as_float(row.get("f2")),
        "up_count": _as_float(row.get("f104")),
        "down_count": _as_float(row.get("f105")),
        "leader": str(leader) if leader and leader != "-" else None,
    }


def _diff_rows(payload: Any) -> list:
    """Extract the ``data.diff`` row list from a push2 payload, defensively.

    Args:
        payload: Decoded JSON from a push2 board endpoint.

    Returns:
        The list of diff rows, or ``[]`` when the payload carries none.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return []
    diff = data.get("diff")
    if isinstance(diff, dict):
        # Some push2 responses key diff rows by string index instead of a list.
        return list(diff.values())
    if isinstance(diff, list):
        return diff
    return []


def _fetch_membership(code: str) -> str:
    """Fetch the industry / concept boards one stock belongs to.

    Args:
        code: Vibe-Trading A-share symbol (e.g. ``"600519.SH"``).

    Returns:
        A JSON envelope string with the resolved boards, or an error envelope
        when the symbol is unresolvable or the request fails.
    """
    secid = resolve_secid(code)
    if secid is None:
        return _error(f"unresolvable symbol: {code}")

    try:
        payload = get_json(
            _MEMBERSHIP_URL,
            params={
                "secid": secid,
                "spt": "3",
                "pi": "0",
                "pz": "100",
                "fields": _MEMBERSHIP_FIELDS,
                "fltt": "2",
                "po": "1",
            },
        )
    except Exception as exc:  # noqa: BLE001 - surface a clean error envelope
        logger.warning("sector membership fetch failed for %s: %s", code, exc)
        return _error(f"membership request failed: {exc}")

    boards = [
        parsed
        for parsed in (_parse_membership_row(r) for r in _diff_rows(payload))
        if parsed is not None
    ]
    envelope = {
        "ok": True,
        "market": "stock",
        "source": "eastmoney",
        "mode": "membership",
        "data": {"code": code, "secid": secid, "boards": boards},
    }
    return json.dumps(envelope, ensure_ascii=False)


def resolve_industry_board(code: str) -> str | None:
    """Resolve the single Eastmoney industry board (行业板块) name for an A-share.

    Unlike the membership view (``spt=3``), which mixes industry and concept
    boards into one list, the ``slist`` endpoint with ``spt=1`` returns the
    stock itself plus its single industry board. The board row is the one with
    ``f13 == 90`` (the board-market marker); the stock row carries ``f13`` of
    ``1``/``0`` instead.

    Args:
        code: Vibe-Trading symbol (e.g. ``"600519.SH"``).

    Returns:
        The industry board name (e.g. ``"白酒Ⅱ"``), or ``None`` when the symbol
        is not an A-share, is unresolvable, the request fails, or the payload
        carries no board row. Never raises.
    """
    if _detect_market(code) != "a_share":
        return None

    secid = resolve_secid(code)
    if secid is None:
        return None

    try:
        payload = get_json(
            _MEMBERSHIP_URL,
            params={
                "secid": secid,
                "spt": "1",
                "pi": "0",
                "pz": "100",
                "fields": _INDUSTRY_FIELDS,
                "fltt": "2",
                "po": "1",
            },
        )
    except Exception as exc:  # noqa: BLE001 - a failed lookup degrades to None
        logger.warning("industry board fetch failed for %s: %s", code, exc)
        return None

    for row in _diff_rows(payload):
        if not isinstance(row, dict):
            continue
        if row.get("f13") in (90, "90"):
            board_name = row.get("f14")
            if board_name and board_name != "-":
                return str(board_name)
    return None


def _symbol_from_clist_row(row: dict[str, Any]) -> str | None:
    """Map one Eastmoney clist row to a Vibe-Trading A-share symbol."""
    code = row.get("f12")
    if not code:
        return None
    code_str = str(code).strip()
    market = row.get("f13")
    if market in (0, "0"):
        suffix = "SZ"
    elif market in (1, "1"):
        suffix = "SH"
    elif code_str.startswith(("4", "8")):
        suffix = "BJ"
    elif code_str.startswith(("6", "9")):
        suffix = "SH"
    else:
        suffix = "SZ"
    return f"{code_str}.{suffix}"


def _parse_peer_row(row: Any) -> dict[str, Any] | None:
    """Shape one board-constituent clist row into a peer quote record."""
    if not isinstance(row, dict):
        return None
    symbol = _symbol_from_clist_row(row)
    name = row.get("f14")
    if not symbol or not name:
        return None
    return {
        "symbol": symbol,
        "name": str(name),
        "price": _as_float(row.get("f2")),
        "change_pct": _as_float(row.get("f3")),
        "amount": _as_float(row.get("f6")),
    }


def resolve_industry_board_meta(code: str) -> dict[str, Any] | None:
    """Resolve the primary industry board code/name for an A-share symbol.

    Returns:
        ``{board_code, board_name, board_change_pct}`` or ``None``.
    """
    if _detect_market(code) != "a_share":
        return None
    secid = resolve_secid(code)
    if secid is None:
        return None
    try:
        payload = get_json(
            _MEMBERSHIP_URL,
            params={
                "secid": secid,
                "spt": "1",
                "pi": "0",
                "pz": "100",
                "fields": _INDUSTRY_FIELDS,
                "fltt": "2",
                "po": "1",
            },
        )
    except Exception as exc:  # noqa: BLE001 - degrade to None
        logger.warning("industry board meta fetch failed for %s: %s", code, exc)
        return None

    for row in _diff_rows(payload):
        if not isinstance(row, dict):
            continue
        if row.get("f13") in (90, "90"):
            board_code = row.get("f12")
            board_name = row.get("f14")
            if board_code and board_name and board_name != "-":
                return {
                    "board_code": str(board_code),
                    "board_name": str(board_name),
                    "board_change_pct": _as_float(row.get("f3")),
                }
    return None


def fetch_industry_peers(code: str, *, limit: int = _DEFAULT_PEER_LIMIT) -> dict[str, Any] | None:
    """Fetch same-industry peers for an A-share via Eastmoney board constituents.

    Args:
        code: Vibe-Trading symbol (e.g. ``603986.SH``).
        limit: Maximum peer rows (excluding the target symbol).

    Returns:
        A dict with industry metadata, optional target quote, and peer rows sorted
        by turnover amount descending; ``None`` when the industry board cannot be
        resolved or the constituent list is empty.
    """
    code = code.strip().upper()
    if _detect_market(code) != "a_share":
        return None
    limit = max(1, min(int(limit), _MAX_PEER_LIMIT))
    meta = resolve_industry_board_meta(code)
    if meta is None:
        return None

    board_code = meta["board_code"]
    try:
        payload = get_json(
            _RANKING_URL,
            params={
                "fs": f"b:{board_code}",
                "fields": _PEER_FIELDS,
                "pn": "1",
                "pz": str(limit + 12),
                "po": "1",
                "fid": "f6",
                "fltt": "2",
                "invt": "2",
                "np": "1",
            },
        )
    except Exception as exc:  # noqa: BLE001 - degrade to None
        logger.warning("industry peers fetch failed for %s: %s", code, exc)
        return None

    target_bare = _bare_code_from_symbol(code)
    peers: list[dict[str, Any]] = []
    target: dict[str, Any] | None = None
    for row in _diff_rows(payload):
        parsed = _parse_peer_row(row)
        if parsed is None:
            continue
        bare = _bare_code_from_symbol(parsed["symbol"])
        if bare == target_bare:
            target = parsed
            continue
        peers.append(parsed)
        if len(peers) >= limit:
            break

    if not peers and target is None:
        return None

    return {
        "code": code,
        "industry": meta["board_name"],
        "board_code": board_code,
        "board_change_pct": meta.get("board_change_pct"),
        "target": target,
        "peers": peers,
    }


def _bare_code_from_symbol(symbol: str) -> str:
    """Return the bare numeric code from a dotted symbol."""
    return symbol.strip().upper().split(".", 1)[0]


def _fetch_ranking(limit: int) -> str:
    """Fetch the industry-board ranking by intraday percent change.

    Args:
        limit: Number of top boards to keep (already validated and capped).

    Returns:
        A JSON envelope string with the ranked boards, or an error envelope when
        the request fails.
    """
    boards: list[dict[str, Any]] = []
    source = "eastmoney"
    warnings: list[str] = []

    try:
        payload = get_json(
            _RANKING_URL,
            params={
                "fs": _RANKING_FS,
                "fields": _RANKING_FIELDS,
                "pn": "1",
                "pz": str(limit),
                "po": "1",
                "fid": "f3",
                "fltt": "2",
            },
        )
        boards = [
            parsed
            for parsed in (_parse_ranking_row(r) for r in _diff_rows(payload))
            if parsed is not None
        ]
        if len(boards) > limit:
            boards = boards[:limit]
    except Exception as exc:  # noqa: BLE001 - try akshare before surfacing error
        logger.warning("sector ranking fetch failed: %s", exc)
        warnings.append(f"eastmoney ranking failed ({exc})")

    if not boards:
        try:
            boards = akshare_fallbacks.fetch_industry_board_ranking(limit)
            source = "akshare"
            if warnings:
                warnings.append("used akshare fallback")
            else:
                warnings.append("eastmoney returned no boards; used akshare fallback")
        except Exception as exc:  # noqa: BLE001 - surface a clean error envelope
            if warnings:
                return _error(f"{warnings[0]}; akshare fallback failed: {exc}")
            return _error(f"ranking request failed: {exc}")

    envelope: dict[str, Any] = {
        "ok": True,
        "market": "stock",
        "source": source,
        "mode": "ranking",
        "data": {"boards": boards},
    }
    if warnings:
        envelope["warnings"] = warnings
    return json.dumps(envelope, ensure_ascii=False)


class SectorInfoTool(BaseTool):
    """Look up sector / concept board membership for a stock, or rank boards."""

    name = "get_sector_info"
    description = (
        "Look up Chinese A-share sector / concept board info via Eastmoney "
        "(free, no auth). Two modes: (1) membership — given a stock 'code' "
        "(e.g. 600519.SH / 000001.SZ / .BJ), list the industry and concept "
        "boards it belongs to; (2) ranking — set mode='ranking' to rank "
        "industry boards by today's percent change (with up/down constituent "
        "counts and the leading stock). Ranking mode falls back to akshare when "
        "Eastmoney push2 is throttled; call once per run and do not retry on "
        "failure. Use this to map a stock to its sectors or to see which sectors "
        "are hot today. Market: A-share stocks. "
        'Example: {"code": "600519.SH"} or {"mode": "ranking", "limit": 20}.'
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": (
                    "A-share stock symbol with market suffix, e.g. '600519.SH', "
                    "'000001.SZ', '430139.BJ'. Required when mode='membership' "
                    "(the default); ignored when mode='ranking'."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["membership", "ranking"],
                "description": (
                    "'membership' (default) lists the boards a stock belongs to "
                    "and requires 'code'. 'ranking' ranks industry boards by "
                    "today's percent change and ignores 'code'."
                ),
                "default": "membership",
            },
            "limit": {
                "type": "integer",
                "description": (
                    "For mode='ranking', number of top boards to return "
                    f"(1-{_MAX_RANKING}). Ignored for mode='membership'. "
                    f"Default {_DEFAULT_RANKING}."
                ),
                "default": _DEFAULT_RANKING,
            },
        },
        "required": [],
    }

    def execute(self, **kwargs: Any) -> str:
        """Dispatch to the membership or ranking view and return a JSON envelope.

        Args:
            **kwargs: ``mode`` ("membership"|"ranking", default "membership"),
                ``code`` (str, required for membership), ``limit`` (int, default
                30, used by ranking).

        Returns:
            A JSON string ``{"ok": true, "market": "stock", "source":
            "eastmoney", "mode": ..., "data": {...}}`` on success, or
            ``{"ok": false, "error": ...}`` on a validation / request failure.
        """
        mode = kwargs.get("mode", "membership")
        if mode not in _VALID_MODES:
            return _error(f"mode must be one of {list(_VALID_MODES)}")

        if mode == "ranking":
            limit = kwargs.get("limit", _DEFAULT_RANKING)
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                return _error("limit must be a positive integer")
            return _fetch_ranking(min(limit, _MAX_RANKING))

        code = kwargs.get("code")
        if not isinstance(code, str) or not code.strip():
            return _error("code must be a non-empty string for mode='membership'")
        return _fetch_membership(code.strip())
