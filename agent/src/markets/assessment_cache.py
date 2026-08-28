"""Disk cache for Markets AI assessments."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_DIR = Path.home() / ".vibe-trading" / "markets-assessments"
_DEFAULT_TTL_MINUTES = 45


class AssessmentCache:
    """Persist generated assessments per symbol."""

    def __init__(self, root: Path | None = None, *, ttl_minutes: int = _DEFAULT_TTL_MINUTES):
        self._root = root or _DEFAULT_DIR
        self._ttl = max(5, int(ttl_minutes))

    def _path(self, symbol: str) -> Path:
        safe = symbol.strip().upper().replace("/", "_").replace("\\", "_")
        return self._root / f"{safe}.json"

    def get(self, symbol: str) -> dict[str, Any] | None:
        path = self._path(symbol)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("assessment cache read failed for %s: %s", symbol, exc)
            return None
        if not isinstance(payload, dict):
            return None
        expires_at = payload.get("expires_at")
        if isinstance(expires_at, str):
            try:
                exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > exp:
                    return None
            except ValueError:
                pass
        payload["cached"] = True
        return payload

    def put(self, symbol: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._root.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(minutes=self._ttl)
        stored = {
            **payload,
            "cached": False,
            "generated_at": now.isoformat(),
            "expires_at": expires.isoformat(),
        }
        path = self._path(symbol)
        path.write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")
        return stored
