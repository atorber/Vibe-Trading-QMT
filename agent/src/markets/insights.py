"""Rule-based market narratives and anomalies from OHLCV bars + quote."""

from __future__ import annotations

from typing import Any, Literal

Tone = Literal["up", "down", "neutral"]


def _pct(from_value: float, to_value: float) -> float | None:
    if not from_value or from_value != from_value or to_value != to_value:
        return None
    return (to_value - from_value) / from_value * 100


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _fmt_pct(value: float | None) -> str:
    if value is None or value != value:
        return "—"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}%"


def build_market_insights(
    name: str,
    quote: dict[str, Any],
    bars: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Derive narrative and anomaly records aligned with the SPA rules."""
    narratives: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    series = [
        bar
        for bar in bars
        if all(
            isinstance(bar.get(key), (int, float))
            for key in ("close", "open", "high", "low")
        )
    ]
    if not series:
        return {"narratives": narratives, "anomalies": anomalies}

    last = series[-1]
    prev = series[-2] if len(series) > 1 else None
    day_pct = quote.get("change_pct")
    if day_pct is None and prev is not None:
        day_pct = _pct(float(prev["close"]), float(last["close"]))
    elif day_pct is None:
        day_pct = _pct(float(last["open"]), float(last["close"]))

    range_pct = _pct(float(last["low"]), float(last["high"]))
    vols = [
        float(bar.get("volume") or 0)
        for bar in series[-21:-1]
        if float(bar.get("volume") or 0) > 0
    ]
    vol_avg = _avg(vols)
    last_vol = float(last.get("volume") or 0)
    vol_ratio = last_vol / vol_avg if vol_avg > 0 else None

    if day_pct is not None:
        tone: Tone = "up" if day_pct > 0.15 else "down" if day_pct < -0.15 else "neutral"
        body_parts = [
            f"最新价 {float(quote.get('last') or last['close']):.2f}，"
            f"开 {float(quote.get('open') or last['open']):.2f} / "
            f"高 {float(quote.get('high') or last['high']):.2f} / "
            f"低 {float(quote.get('low') or last['low']):.2f}。",
        ]
        if range_pct is not None:
            body_parts.append(f"当日振幅 {_fmt_pct(range_pct)}。")
        if vol_ratio is not None:
            body_parts.append(f"成交量约为近 20 日均量的 {vol_ratio:.2f} 倍。")
        narratives.append(
            {
                "id": "session-move",
                "tag": "Session Move",
                "title": (
                    f"{name} 上涨 {_fmt_pct(day_pct)}"
                    if tone == "up"
                    else f"{name} 回落 {_fmt_pct(day_pct)}"
                    if tone == "down"
                    else f"{name} 窄幅震荡 {_fmt_pct(day_pct)}"
                ),
                "body": " ".join(body_parts),
                "time": str(last.get("time") or quote.get("updated_at") or ""),
                "tone": tone,
            }
        )

    if len(series) >= 6:
        d5 = series[-6]
        move5 = _pct(float(d5["close"]), float(last["close"]))
        if move5 is not None and abs(move5) >= 3:
            narratives.append(
                {
                    "id": "trend-5d",
                    "tag": "5D Trend",
                    "title": (
                        f"{name} 近 5 个交易日累计上涨 {_fmt_pct(move5)}"
                        if move5 > 0
                        else f"{name} 近 5 个交易日累计下跌 {_fmt_pct(move5)}"
                    ),
                    "body": (
                        f"自 {d5.get('time')} 收盘 {float(d5['close']):.2f} "
                        f"运行至最新 {float(last['close']):.2f}。"
                    ),
                    "time": str(last.get("time") or ""),
                    "tone": "up" if move5 > 0 else "down",
                }
            )

    if len(series) >= 21:
        d20 = series[-21]
        move20 = _pct(float(d20["close"]), float(last["close"]))
        if move20 is not None and abs(move20) >= 5:
            narratives.append(
                {
                    "id": "trend-20d",
                    "tag": "20D Trend",
                    "title": (
                        f"{name} 近 20 日累计上涨 {_fmt_pct(move20)}"
                        if move20 > 0
                        else f"{name} 近 20 日累计下跌 {_fmt_pct(move20)}"
                    ),
                    "body": (
                        f"中期走势自 {d20.get('time')} 的 {float(d20['close']):.2f} "
                        f"变化至 {float(last['close']):.2f}（{_fmt_pct(move20)}）。"
                    ),
                    "time": str(last.get("time") or ""),
                    "tone": "up" if move20 > 0 else "down",
                }
            )

    seen: set[str] = set()

    def push_anomaly(item: dict[str, Any]) -> None:
        key = f"{item['id']}:{item.get('time') or ''}"
        if key in seen:
            return
        seen.add(key)
        anomalies.append(item)

    scan_from = max(1, len(series) - 60)
    for i in range(len(series) - 1, scan_from - 1, -1):
        bar = series[i]
        before = series[i - 1]
        move = _pct(float(before["close"]), float(bar["close"]))
        bar_vol = float(bar.get("volume") or 0)
        vol_window = [
            float(row.get("volume") or 0)
            for row in series[max(0, i - 20) : i]
            if float(row.get("volume") or 0) > 0
        ]
        bar_vol_avg = _avg(vol_window)
        bar_vol_ratio = bar_vol / bar_vol_avg if bar_vol_avg > 0 else None
        bar_range_pct = _pct(float(bar["low"]), float(bar["high"]))
        gap = _pct(float(before["close"]), float(bar["open"])) if float(bar["open"]) > 0 else None
        time = str(bar.get("time") or "")
        price = float(bar["close"])
        is_latest = i == len(series) - 1

        if move is not None and abs(move) >= 3:
            tone = "up" if move >= 0 else "down"
            push_anomaly(
                {
                    "id": "large-move",
                    "tag": "Unusual Price Movement",
                    "title": (
                        f"{name} 上涨 {_fmt_pct(move)}"
                        if tone == "up"
                        else f"{name} 下跌 {_fmt_pct(abs(move))}"
                    ),
                    "body": f"相对前收变动 {_fmt_pct(move)}（阈值 |涨跌幅| ≥ 3%）。",
                    "time": time,
                    "tone": tone,
                    "price": price,
                    "change_pct": move,
                }
            )

        if bar_vol_ratio is not None and bar_vol_ratio >= 2.5:
            push_anomaly(
                {
                    "id": "volume-spike",
                    "tag": "Volume Spike",
                    "title": f"{name} 成交量放大至均量 {bar_vol_ratio:.2f} 倍",
                    "body": f"当日量 {int(bar_vol)}，近 20 日均量约 {int(bar_vol_avg)}。",
                    "time": time,
                    "tone": "up" if (move or 0) >= 0 else "down",
                    "price": price,
                    "change_pct": move,
                }
            )

        if gap is not None and abs(gap) >= 2:
            push_anomaly(
                {
                    "id": "gap",
                    "tag": "Gap",
                    "title": (
                        f"{name} 跳空高开 {_fmt_pct(gap)}"
                        if gap > 0
                        else f"{name} 跳空低开 {_fmt_pct(gap)}"
                    ),
                    "body": (
                        f"开盘 {float(bar['open']):.2f} 相对前收 "
                        f"{float(before['close']):.2f} 偏离 {_fmt_pct(gap)}。"
                    ),
                    "time": time,
                    "tone": "up" if gap > 0 else "down",
                    "price": float(bar["open"]),
                    "change_pct": gap,
                }
            )

    streak = 0
    for i in range(len(series) - 1, 0, -1):
        cur = series[i]
        prev_bar = series[i - 1]
        up = float(cur["close"]) > float(prev_bar["close"])
        if streak == 0:
            streak = 1 if up else -1
        elif (streak > 0 and up) or (streak < 0 and not up):
            streak += 1 if streak > 0 else -1
        else:
            break
    if abs(streak) >= 4:
        push_anomaly(
            {
                "id": "streak",
                "tag": "Streak",
                "title": (
                    f"{name} 连续 {streak} 日收涨"
                    if streak > 0
                    else f"{name} 连续 {abs(streak)} 日收跌"
                ),
                "body": "连续同向收盘可能进入短期趋势或过热阶段，注意回撤风险。",
                "time": str(last.get("time") or ""),
                "tone": "up" if streak > 0 else "down",
                "price": float(last["close"]),
                "change_pct": day_pct,
            }
        )

    return {"narratives": narratives[:5], "anomalies": anomalies[:8]}
