"""One-shot LLM market assessment from aggregated evidence."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

from src.markets.assessment_cache import AssessmentCache
from src.markets.evidence import collect_market_evidence

logger = logging.getLogger(__name__)

_RATE_LIMIT_SECONDS = 300
_last_generated: dict[str, float] = {}
_JSON_BLOCK = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)

_SYSTEM_PROMPT = """你是 A 股市场研究助手。根据提供的结构化证据，输出 JSON 综合评估。
规则：
1. 不得编造证据包中不存在的价格、EPS、日期或新闻标题。
2. 引用新闻/异动时用概括性表述，不要捏造细节。
3. 输出必须是合法 JSON，不要 markdown 包裹以外的文字。
4. stance 只能是 bullish / neutral / bearish / mixed 之一。
5. confidence 只能是 high / medium / low 之一。"""

_USER_TEMPLATE = """请综合以下证据，评估 {name}（{symbol}）：

【行情】
{quote_json}

【量价叙事】
{narratives_json}

【异动】
{anomalies_json}

【新闻标题】
{news_json}

【财报/研报】
{earnings_json}

【同行】
{peers_json}

返回 JSON：
{{
  "stance": "bullish|neutral|bearish|mixed",
  "headline": "不超过30字的一句话结论",
  "summary": "2-4句综合评估",
  "drivers": ["支撑逻辑，3-5条"],
  "risks": ["风险点，2-4条"],
  "catalysts": ["后续关注点，1-3条"],
  "confidence": "high|medium|low"
}}"""


def _parse_json_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    match = _JSON_BLOCK.search(stripped)
    if match:
        stripped = match.group(1).strip()
    payload = json.loads(stripped)
    if not isinstance(payload, dict):
        raise ValueError("assessment payload is not an object")
    return payload


def _normalize_assessment(raw: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
    stance = str(raw.get("stance") or "neutral").strip().lower()
    if stance not in {"bullish", "neutral", "bearish", "mixed"}:
        stance = "neutral"
    confidence = str(raw.get("confidence") or "medium").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "medium"

    def _strings(key: str, limit: int) -> list[str]:
        value = raw.get(key)
        if not isinstance(value, list):
            return []
        out: list[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                out.append(text)
            if len(out) >= limit:
                break
        return out

    quote = bundle.get("quote") if isinstance(bundle.get("quote"), dict) else {}
    peers = bundle.get("peers") if isinstance(bundle.get("peers"), dict) else {}
    insights = bundle.get("insights") if isinstance(bundle.get("insights"), dict) else {}

    return {
        "status": "ok",
        "symbol": bundle.get("symbol"),
        "name": bundle.get("name"),
        "source": "llm",
        "fallback": False,
        "stance": stance,
        "headline": str(raw.get("headline") or "").strip()[:120],
        "summary": str(raw.get("summary") or "").strip(),
        "drivers": _strings("drivers", 5),
        "risks": _strings("risks", 4),
        "catalysts": _strings("catalysts", 3),
        "confidence": confidence,
        "evidence": {
            "quote_change_pct": quote.get("change_pct"),
            "peer_avg_change_pct": bundle.get("peer_avg_change_pct"),
            "board_change_pct": peers.get("board_change_pct") if peers else None,
            "industry": peers.get("industry") if peers else None,
            "narrative_count": len(insights.get("narratives") or []),
            "anomaly_count": len(insights.get("anomalies") or []),
            "news_count": len(bundle.get("news") or []),
            "report_count": len((bundle.get("earnings") or {}).get("reports") or []),
        },
    }


def _rule_fallback(bundle: dict[str, Any], *, reason: str = "") -> dict[str, Any]:
    quote = bundle.get("quote") if isinstance(bundle.get("quote"), dict) else {}
    change = quote.get("change_pct")
    peer_avg = bundle.get("peer_avg_change_pct")
    name = str(bundle.get("name") or bundle.get("symbol") or "")
    insights = bundle.get("insights") if isinstance(bundle.get("insights"), dict) else {}
    narratives = insights.get("narratives") or []
    anomalies = insights.get("anomalies") or []

    stance = "neutral"
    if isinstance(change, (int, float)):
        if change > 1.5:
            stance = "bullish"
        elif change < -1.5:
            stance = "bearish"
    if isinstance(peer_avg, (int, float)) and isinstance(change, (int, float)):
        if change > peer_avg + 2:
            stance = "bullish"
        elif change < peer_avg - 2:
            stance = "bearish"

    headline = narratives[0]["title"] if narratives else f"{name} 行情摘要"
    summary_parts = []
    if isinstance(change, (int, float)):
        summary_parts.append(f"{name} 最新涨跌幅 {change:+.2f}%。")
    if isinstance(peer_avg, (int, float)):
        summary_parts.append(f"同业可比组均值约 {peer_avg:+.2f}%。")
    if anomalies:
        summary_parts.append(f"近期触发 {len(anomalies)} 条量价异动规则。")
    news = bundle.get("news") or []
    if news:
        summary_parts.append(f"可见 {len(news)} 条相关新闻标题供参考。")
    summary_parts.append("（规则摘要，LLM 暂不可用）")

    return {
        "status": "ok",
        "symbol": bundle.get("symbol"),
        "name": name,
        "source": "rules",
        "fallback": True,
        "fallback_reason": reason[:200] if reason else None,
        "stance": stance,
        "headline": str(headline)[:120],
        "summary": " ".join(summary_parts),
        "drivers": [str(n.get("title") or "") for n in narratives[:3] if n.get("title")],
        "risks": [str(a.get("title") or "") for a in anomalies[:3] if a.get("title")],
        "catalysts": [],
        "confidence": "low",
        "evidence": {
            "quote_change_pct": change,
            "peer_avg_change_pct": peer_avg,
            "narrative_count": len(narratives),
            "anomaly_count": len(anomalies),
            "news_count": len(news),
        },
    }


def _llm_assess(bundle: dict[str, Any]) -> dict[str, Any]:
    from src.providers.chat import ChatLLM

    user_content = _USER_TEMPLATE.format(
        name=bundle.get("name") or bundle.get("symbol"),
        symbol=bundle.get("symbol"),
        quote_json=json.dumps(bundle.get("quote") or {}, ensure_ascii=False, indent=2),
        narratives_json=json.dumps(
            (bundle.get("insights") or {}).get("narratives") or [],
            ensure_ascii=False,
            indent=2,
        ),
        anomalies_json=json.dumps(
            (bundle.get("insights") or {}).get("anomalies") or [],
            ensure_ascii=False,
            indent=2,
        ),
        news_json=json.dumps(bundle.get("news") or [], ensure_ascii=False, indent=2),
        earnings_json=json.dumps(bundle.get("earnings") or {}, ensure_ascii=False, indent=2),
        peers_json=json.dumps(bundle.get("peers") or {}, ensure_ascii=False, indent=2),
    )
    model = os.environ.get("MARKETS_ASSESSMENT_MODEL", "").strip() or None
    llm = ChatLLM(model_name=model) if model else ChatLLM()
    try:
        response = llm.chat(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            timeout=90,
        )
        text = (response.content or "").strip()
        if not text:
            raise ValueError("empty LLM response")
        parsed = _parse_json_text(text)
        return _normalize_assessment(parsed, bundle)
    finally:
        llm.close()


def generate_market_assessment(
    symbol: str,
    *,
    use_cache: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Generate (or return cached) AI assessment for one symbol."""
    code = symbol.strip().upper()
    if not code:
        raise ValueError("symbol required")

    cache = AssessmentCache()
    if use_cache and not force:
        cached = cache.get(code)
        if cached is not None:
            return cached

    now = time.monotonic()
    last = _last_generated.get(code, 0.0)
    if not use_cache and now - last < _RATE_LIMIT_SECONDS:
        wait = int(_RATE_LIMIT_SECONDS - (now - last))
        raise RuntimeError(f"assessment rate limited; retry in {wait}s")

    bundle = collect_market_evidence(code)
    try:
        result = _llm_assess(bundle)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM assessment failed for %s: %s", code, exc)
        result = _rule_fallback(bundle, reason=str(exc))

    _last_generated[code] = time.monotonic()
    return cache.put(code, result)
