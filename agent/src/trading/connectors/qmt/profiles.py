"""Built-in QMT Bridge connector profiles.

QMT Bridge runs on Windows beside miniQMT and exposes HTTP. Paper vs live is
operator-declared (the Bridge binds one ``--account-id`` at startup and does not
expose a runtime simulator/real discriminator), so only read-only profiles ship
in this layer — same safety posture as Longbridge / Trading 212.
"""

from __future__ import annotations

from src.trading.types import READ_CAPABILITIES, TradingProfile

QMT_PROFILES: tuple[TradingProfile, ...] = (
    TradingProfile(
        id="qmt-paper-sdk",
        connector="qmt",
        label="QMT Paper · Bridge Read-Only",
        environment="paper",
        transport="broker_sdk",
        capabilities=READ_CAPABILITIES,
        readonly=True,
        config={"profile": "paper"},
        notes=(
            "Reads a QMT Bridge account the operator declares as paper/sim. "
            "Configure QMT_BRIDGE_HOST / QMT_BRIDGE_PORT / QMT_BRIDGE_API_KEY / "
            "QMT_BRIDGE_ACCOUNT_ID in .env. Bridge must be running on Windows with "
            "QMT logged in (独立交易). No runtime paper/live discriminator — order "
            "placement is not exposed."
        ),
    ),
    TradingProfile(
        id="qmt-live-sdk-readonly",
        connector="qmt",
        label="QMT Live · Bridge Read-Only",
        environment="live",
        transport="broker_sdk",
        capabilities=READ_CAPABILITIES,
        readonly=True,
        config={"profile": "live-readonly"},
        notes=(
            "Reads a live QMT Bridge account for trading monitoring only. "
            "Configure QMT_BRIDGE_* in .env. Order placement is not exposed "
            "(no verified runtime paper/live discriminator)."
        ),
    ),
)
