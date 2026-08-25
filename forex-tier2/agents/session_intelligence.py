"""
Agent — Session Intelligence (NEW in Tier 2)
Detects which forex trading sessions are active right now (UTC).
Returns a confidence modifier so the main signal pipeline can boost
trades during high-liquidity windows and penalise dead-zone signals.

Sessions (UTC):
  Sydney:   22:00 – 07:00
  Tokyo:    00:00 – 09:00
  London:   08:00 – 17:00
  New York: 13:00 – 22:00

Overlaps (best trading windows):
  London + New York: 13:00 – 17:00 UTC  ← highest liquidity
  Tokyo + London:    08:00 – 09:00 UTC  ← decent
"""
from datetime import datetime, timezone
from typing import NamedTuple


class SessionResult(NamedTuple):
    active_sessions: list[str]   # e.g. ["London", "New York"]
    is_overlap: bool              # True if 2+ major sessions overlap
    overlap_label: str            # e.g. "London+New York"
    confidence_modifier: int      # net adjustment to add to signal confidence
    recommendation: str           # "TRADE" | "CAUTION" | "AVOID"


def _in_session(hour: int, start: int, end: int) -> bool:
    """Check if hour (0-23 UTC) falls inside a session window (handles midnight cross)."""
    if start < end:
        return start <= hour < end
    else:  # crosses midnight (e.g. Sydney 22→07)
        return hour >= start or hour < end


class SessionIntelligenceAgent:
    def __init__(self, settings):
        cfg = settings.get("session", {})
        self.london_cfg   = cfg.get("london",   {"start": 8,  "end": 17})
        self.ny_cfg       = cfg.get("new_york",  {"start": 13, "end": 22})
        self.tokyo_cfg    = cfg.get("tokyo",     {"start": 0,  "end": 9})
        self.sydney_cfg   = cfg.get("sydney",    {"start": 22, "end": 7})
        self.overlap_boost   = cfg.get("overlap_boost", 15)
        self.dead_zone_penalty = cfg.get("dead_zone_penalty", -20)
        self.enabled         = settings.get("session", {}).get("enabled", True)

    def run(self) -> SessionResult:
        if not self.enabled:
            return SessionResult([], False, "", 0, "TRADE")

        now  = datetime.now(timezone.utc)
        hour = now.hour

        london   = _in_session(hour, self.london_cfg["start"],   self.london_cfg["end"])
        new_york = _in_session(hour, self.ny_cfg["start"],       self.ny_cfg["end"])
        tokyo    = _in_session(hour, self.tokyo_cfg["start"],    self.tokyo_cfg["end"])
        sydney   = _in_session(hour, self.sydney_cfg["start"],   self.sydney_cfg["end"])

        active = []
        if london:   active.append("London")
        if new_york: active.append("New York")
        if tokyo:    active.append("Tokyo")
        if sydney:   active.append("Sydney")

        # Detect meaningful overlaps
        lon_ny  = london and new_york     # 13:00–17:00 UTC — best
        tok_lon = tokyo and london        # 08:00–09:00 UTC — decent

        if lon_ny:
            modifier = self.overlap_boost
            overlap_label = "London+New York"
            is_overlap = True
            recommendation = "TRADE"
        elif tok_lon:
            modifier = self.overlap_boost // 2
            overlap_label = "Tokyo+London"
            is_overlap = True
            recommendation = "TRADE"
        elif active:
            modifier = 0
            overlap_label = ""
            is_overlap = False
            recommendation = "TRADE"
        else:
            modifier = self.dead_zone_penalty
            overlap_label = ""
            is_overlap = False
            recommendation = "AVOID"

        if active:
            print(f"[Session] Active: {', '.join(active)} | Overlap: {overlap_label or 'None'} | Modifier: {modifier:+d}")
        else:
            print(f"[Session] ⚠️  Dead zone — no major session active. Modifier: {modifier:+d}")

        return SessionResult(
            active_sessions=active,
            is_overlap=is_overlap,
            overlap_label=overlap_label,
            confidence_modifier=modifier,
            recommendation="AVOID" if not active else "TRADE",
        )
