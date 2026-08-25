"""
Agent 3 — News Sentiment (same as Tier 1 — Economic Calendar filter)
Checks ForexFactory for high-impact events and blocks pairs during them.
"""
import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    import feedparser
    FEEDPARSER_AVAILABLE = True
except ImportError:
    FEEDPARSER_AVAILABLE = False

FF_FEED = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

CURRENCY_PAIRS = {
    "USD": ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "NZDUSD",
            "USDCAD", "XAUUSD", "XAGUSD", "USOIL"],
    "EUR": ["EURUSD", "EURGBP", "EURJPY"],
    "GBP": ["GBPUSD", "GBPJPY", "EURGBP"],
    "JPY": ["USDJPY", "EURJPY", "GBPJPY"],
    "CHF": ["USDCHF"],
    "AUD": ["AUDUSD"],
    "NZD": ["NZDUSD"],
    "CAD": ["USDCAD"],
}
HIGH_IMPACT_KEYWORDS = [
    "NFP", "Non-Farm", "FOMC", "Fed", "CPI", "GDP", "Rate Decision",
    "Interest Rate", "Employment", "Retail Sales", "PMI",
    "Unemployment", "Inflation", "ECB", "BOE", "BOJ", "RBA",
]


def _is_high_impact(title: str) -> bool:
    return any(kw.lower() in title.lower() for kw in HIGH_IMPACT_KEYWORDS)


def _parse_event_time(entry) -> Optional[datetime]:
    try:
        import time
        t = entry.get("published_parsed") or entry.get("updated_parsed")
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    except Exception:
        pass
    return None


def _extract_currency(title: str) -> Optional[str]:
    for cur in CURRENCY_PAIRS:
        if cur in title.upper():
            return cur
    return None


def _fetch_blocked_pairs(pairs_with_suffix: list[str], block_window_minutes: int = 60) -> list[str]:
    if not FEEDPARSER_AVAILABLE:
        return []
    try:
        feed   = feedparser.parse(FF_FEED)
        now    = datetime.now(timezone.utc)
        window = timedelta(minutes=block_window_minutes)
        blocked_currencies: set[str] = set()

        for entry in feed.entries:
            title      = entry.get("title", "")
            event_time = _parse_event_time(entry)
            if not event_time:
                continue
            if not (now - window <= event_time <= now + window):
                continue
            if not _is_high_impact(title):
                continue
            cur = _extract_currency(title)
            if cur:
                blocked_currencies.add(cur)
                print(f"[News] 🔴 High-impact event: '{title}' @ {event_time.strftime('%H:%M UTC')} — blocking {cur}")

        # Map currencies to pair names (strip suffix for matching, re-add for return)
        base_pairs = set()
        for cur in blocked_currencies:
            for p in CURRENCY_PAIRS.get(cur, []):
                base_pairs.add(p)

        # Find matches in configured pairs (with or without suffix)
        blocked = []
        for pair in pairs_with_suffix:
            clean = pair.rstrip("m").rstrip(".")
            if any(clean.startswith(b) or clean.endswith(b.replace("USD","")) for b in base_pairs
                   ) or clean in base_pairs:
                blocked.append(pair)

        return blocked
    except Exception as e:
        print(f"[News] Feed error: {e}")
        return []


class NewsSentimentAgent:
    def __init__(self, settings):
        self.settings = settings

    async def run(self) -> dict:
        pairs = self.settings.pairs_with_suffix
        blocked = await asyncio.to_thread(_fetch_blocked_pairs, pairs)
        if blocked:
            print(f"[News] Blocking {len(blocked)} pair(s): {', '.join(blocked)}")
        else:
            print("[News] No high-impact events blocking any pairs.")
        return {"blocked_pairs": blocked}
