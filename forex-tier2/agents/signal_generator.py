"""
Agent 4 — Signal Generator (Tier 2)
Combines deterministic TA, volume, MTF, session, and memory evidence.
Gemini may add optional commentary but never changes direction or confidence.
"""
import asyncio
import json
from typing import Optional
from google import genai


def _base_score(bullish: int, bearish: int, rsi: float,
                trend: str, macd_hist: float) -> tuple[str, float]:
    """Rule-based signal scoring — same logic as Tier 1."""
    max_signals = 6
    bull_pct    = bullish / max_signals
    bear_pct    = bearish / max_signals
    trend_bonus = 0.1 if trend in ("UP", "DOWN") else 0.0

    if bull_pct > bear_pct:
        raw        = bull_pct + (trend_bonus if trend == "UP" else 0)
        confidence = min(raw * 100, 95)
        direction  = "BUY"
    elif bear_pct > bull_pct:
        raw        = bear_pct + (trend_bonus if trend == "DOWN" else 0)
        confidence = min(raw * 100, 95)
        direction  = "SELL"
    else:
        return "HOLD", 0.0

    if direction == "BUY"  and rsi > 75:
        confidence *= 0.6
    elif direction == "SELL" and rsi < 25:
        confidence *= 0.6

    return direction, round(confidence, 1)


def _llm_commentary_sync(client: genai.Client, model: str,
                         pair: str, ta: dict, direction: str,
                         confidence: float) -> str:
    prompt = (
        f"You are a professional Forex trading assistant. Evaluate this signal:\n"
        f"Pair: {pair}\nDirection: {direction}\nConfidence: {confidence}%\n"
        f"TA: RSI={ta['rsi']}, MACD_hist={ta['macd_hist']}, Trend={ta['trend']}, "
        f"Bull={ta['bullish_signals']}/6, Bear={ta['bearish_signals']}/6, "
        f"Price={ta['price']}, EMA20={ta['ema20']}, EMA50={ta['ema50']}, EMA200={ta['ema200']}\n"
        f"MTF confluence: {ta.get('confluence_summary', 'N/A')}\n\n"
        "Give at most one concise caveat. Do not choose direction, confidence, "
        "position size, stop loss, or take profit."
    )
    try:
        response = client.models.generate_content(model=model, contents=prompt)
        return response.text.strip()[:240]
    except Exception as e:
        print(f"[Signal] LLM failed for {pair}: {e}")
        return ""


async def _llm_commentary(client, model, pair, ta, direction, confidence):
    return await asyncio.to_thread(
        _llm_commentary_sync, client, model, pair, ta, direction, confidence
    )


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def _is_metal(pair: str) -> bool:
    return "XAU" in pair or "XAG" in pair


def _confidence_breakdown(
    base: float,
    ta: dict,
    session_bonus: float,
    memory_adjustment: float,
    settings,
) -> tuple[float, dict]:
    """Bound independent adjustments and restrict exceptional scores to evidence."""
    cfg = settings.get("confidence", {}) or {}
    volume = ta.get("volume", {}) or {}
    regime = volume.get("regime", "unknown")

    mtf = _clamp(
        float(ta.get("confluence_bonus", 0)),
        0,
        float(cfg.get("max_mtf_adjustment", 20)),
    )
    session = _clamp(
        float(session_bonus),
        -float(cfg.get("max_session_penalty", 8)),
        float(cfg.get("max_session_adjustment", 8)),
    )
    memory = _clamp(
        float(memory_adjustment),
        -float(cfg.get("max_memory_adjustment", 5)),
        float(cfg.get("max_memory_adjustment", 5)),
    )
    volume_points = {
        "high": float(cfg.get("high_volume_bonus", 3)),
        "low": -float(cfg.get("low_volume_penalty", 8)),
    }.get(regime, 0.0)

    score = _clamp(
        base + mtf + session + memory + volume_points,
        0,
        float(cfg.get("max_score", 95)),
    )
    has_full_mtf = str(ta.get("confluence_summary", "")).startswith("FULL")
    has_strong_volume = regime in ("normal", "high")
    if not (has_full_mtf and has_strong_volume):
        score = min(score, float(cfg.get("exceptional_cap_without_full_evidence", 79)))

    return round(score, 1), {
        "base": round(base, 1),
        "mtf": round(mtf, 1),
        "session": round(session, 1),
        "memory": round(memory, 1),
        "volume": round(volume_points, 1),
        "volume_regime": regime,
        "full_mtf": has_full_mtf,
    }


class SignalGeneratorAgent:
    def __init__(self, settings):
        self.settings = settings
        api_key      = settings.gemini_api_key
        self._client = genai.Client(api_key=api_key) if api_key else None
        self._model  = settings.get("gemini", {}).get("model", "gemini-2.0-flash")
        self._use_llm = settings.get("gemini", {}).get("use_llm_for_signals", True)

    async def run(self, ta_data: dict, blocked_pairs: list[str],
                  session_bonus: int = 0,
                  memory_adjustments: dict | None = None) -> dict[str, Optional[dict]]:
        """
        Generate signals with all Tier 2 adjustments applied.
        session_bonus — flat confidence modifier from SessionIntelligenceAgent
        memory_adjustments — per-pair modifier from TradeMemoryAgent
        """
        min_conf  = self.settings.min_signal_confidence
        use_llm   = self._client is not None and self._use_llm
        memory    = memory_adjustments or {}

        results = {}
        for pair, ta in ta_data.items():
            if ta is None:
                continue
            if pair in blocked_pairs:
                print(f"[Signal] {pair} blocked (news event)")
                results[pair] = {"direction": "HOLD", "confidence": 0, "summary": "Blocked (news)"}
                continue

            # ── 1. Base rule-based score ───────────────────────────
            direction, confidence = _base_score(
                ta["bullish_signals"], ta["bearish_signals"],
                ta["rsi"], ta["trend"], ta["macd_hist"]
            )

            if direction == "HOLD":
                results[pair] = {"direction": "HOLD", "confidence": 0, "summary": "No signal"}
                continue

            # ── 2. Completed-candle VolumeSense gate ───────────────
            volume_cfg = self.settings.get("volume_sense", {}) or {}
            volume = ta.get("volume", {}) or {}
            volume_regime = volume.get("regime", "unknown")
            volume_blocks = (
                volume_regime == "low"
                and (
                    (_is_metal(pair) and volume_cfg.get("gate_metals_on_low_volume", False))
                    or (not _is_metal(pair) and volume_cfg.get("gate_forex_on_low_volume", True))
                )
            )
            if volume_blocks:
                reason = volume.get("reason", "Low completed-candle participation")
                results[pair] = {
                    "direction": "HOLD",
                    "confidence": 0,
                    "summary": f"Blocked (low volume): {reason}",
                    "ta": ta,
                    "confidence_breakdown": {"volume_regime": volume_regime},
                }
                print(f"[Signal] {pair} blocked — {reason}")
                continue

            # ── 3. Bound evidence instead of stacking uncapped boosts ─
            # Memory keys may be with or without broker suffix
            clean_pair  = self.settings.strip_suffix(pair) if hasattr(self.settings, "strip_suffix") else pair
            mem_adj     = memory.get(clean_pair, memory.get(pair, 0))
            confidence, breakdown = _confidence_breakdown(
                confidence, ta, session_bonus, mem_adj, self.settings
            )

            # ── 4. Optional LLM commentary is never a trading input ─
            commentary = ""
            if use_llm and 40 <= confidence <= 70:
                commentary = await _llm_commentary(
                    self._client, self._model, pair, ta, direction, confidence
                )

            # ── 5. Apply deterministic threshold ───────────────────
            if direction == "HOLD" or confidence < min_conf:
                results[pair] = {
                    "direction": "HOLD",
                    "confidence": confidence,
                    "summary": "Below threshold",
                    "ta": ta,
                    "confidence_breakdown": breakdown,
                }
                continue

            summary = (
                f"{direction} {pair} | conf={confidence:.0f}% "
                f"(base={breakdown['base']:.0f}, MTF={breakdown['mtf']:+.0f}, "
                f"session={breakdown['session']:+.0f}, mem={breakdown['memory']:+.0f}, "
                f"volume={breakdown['volume']:+.0f}/{breakdown['volume_regime']}) | "
                f"RSI={ta['rsi']} | trend={ta['trend']} | "
                f"bull={ta['bullish_signals']}/6 bear={ta['bearish_signals']}/6 | "
                f"confluence={ta.get('confluence_summary','N/A')}"
            )
            print(f"[Signal] ✅ {summary}")
            results[pair] = {
                "direction":  direction,
                "confidence": confidence,
                "summary":    summary,
                "ta":         ta,
                "confidence_breakdown": breakdown,
                "ai_commentary": commentary,
            }

        return results
