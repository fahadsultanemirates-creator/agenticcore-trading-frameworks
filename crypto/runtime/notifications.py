"""Safe outbound Telegram reporting for the signal/paper-only crypto worker."""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

from config.settings import CryptoSettings
from domain.models import FrameworkState
from runtime.trade_intelligence import analyze_operator_report
from storage.state import read_json_safe, write_json_atomic

MessageSender = Callable[[CryptoSettings, str], bool]
# Dubai has no daylight-saving changes, so a fixed offset avoids requiring the
# optional IANA tzdata package on standard Windows Python installations.
DUBAI = timezone(timedelta(hours=4), name="Asia/Dubai")
MAX_DEDUP_KEYS = 1000


def _state_path(settings: CryptoSettings) -> str:
    return os.path.join(settings.runtime_dir, "notification_state.json")


def _enabled(settings: CryptoSettings) -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def _value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _number(value: Any, places: int = 4) -> str:
    try:
        return f"{float(value):.{places}f}"
    except (TypeError, ValueError):
        return "—"


def _pnl(value: Any) -> str:
    try:
        return f"{float(value):+.2f} USDT"
    except (TypeError, ValueError):
        return "—"


def send_message(settings: CryptoSettings, text: str) -> bool:
    """Send a plain-text operator message; failed delivery never breaks a cycle."""
    if not _enabled(settings):
        return False
    try:
        payload = urllib.parse.urlencode(
            {"chat_id": settings.telegram_chat_id, "text": text[:4000]}
        ).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(
            request, timeout=min(settings.request_timeout_seconds, 5)
        ) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        return bool(payload.get("ok"))
    except Exception:
        return False


def notify_startup(settings: CryptoSettings, continuous: bool) -> bool:
    """Announce a continuous worker startup, mirroring the Forex operator alert."""
    if not continuous:
        return False
    return send_message(
        settings,
        "🟢 AgenticCore Crypto Tier 1 online\n"
        "MEXC Futures public-data scanner is running.\n"
        "Mode: signal/paper only — no live MEXC orders are sent.",
    )


def _ai_note(reviews: Any) -> str:
    if not isinstance(reviews, list):
        return ""
    live_reviews = [
        f"OpenAI note: {str(review.get('text') or '')}"
        for review in reviews
        if isinstance(review, dict)
        and review.get("provider") == "openai"
        and review.get("status") == "live"
        and review.get("text")
    ]
    return ("\n\n" + "\n".join(live_reviews)[:1200]) if live_reviews else ""


def _trade_outcome(position: Dict[str, Any]) -> str:
    try:
        pnl = float(position.get("net_pnl_usdt"))
    except (TypeError, ValueError):
        return "UNAVAILABLE"
    return "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT"


def _entry_facts(position: Dict[str, Any]) -> str:
    evidence = position.get("entry_evidence")
    evidence_status = (
        str(evidence.get("entry_status") or "unavailable").upper()
        if isinstance(evidence, dict)
        else "UNAVAILABLE"
    )
    return (
        f"Entry: {_number(position.get('entry_price'))} | Qty: {_number(position.get('quantity'), 6)}\n"
        f"Original SL: {_number(position.get('original_stop_price') or position.get('stop_price'))} | "
        f"TP: {_number(position.get('take_profit_price'))}\n"
        f"Confidence: {_number(position.get('confidence'), 0)}% | Evidence: {evidence_status}"
    )


def _format_paper_event(event: Dict[str, Any], daily_pnl: Any) -> Optional[str]:
    position = event.get("position")
    if not isinstance(position, dict):
        return None
    symbol = str(position.get("symbol") or event.get("symbol") or "Unknown")
    side = str(position.get("side") or "unknown").upper()
    event_name = event.get("event")
    explanations = position.get("ai_explanations") if isinstance(position.get("ai_explanations"), dict) else {}
    reviews = explanations.get(str(event.get("id"))) if isinstance(explanations, dict) else []
    ai_note = _ai_note(reviews)

    if event_name == "paper_position_opened":
        return (
            "✅ Paper Trade Opened — AgenticCore Crypto\n"
            f"{side} {symbol} (local paper only)\n"
            + _entry_facts(position)
            + "\n"
            + "No live MEXC order was sent."
            + ai_note
        )
    if event_name == "paper_profit_lock":
        return (
            "🔒 Profit Lock Activated — AgenticCore Crypto\n"
            f"{side} {symbol} (local paper only)\n"
            + _entry_facts(position)
            + "\n"
            f"New SL: {_number(position.get('stop_price'))} | TP unchanged: {_number(position.get('take_profit_price'))}\n"
            + "Lock rule: 65% progress → protect 35% of target distance.\n"
            + "No live MEXC order was sent."
            + ai_note
        )
    if event_name == "paper_position_closed":
        net = position.get("net_pnl_usdt")
        emoji = "🟢" if isinstance(net, (int, float)) and net >= 0 else "🔴"
        return (
            f"{emoji} Paper Trade Closed — AgenticCore Crypto\n"
            f"{side} {symbol} | Outcome: {_trade_outcome(position)} | "
            f"Reason: {str(position.get('close_reason') or 'closed').upper()}\n"
            + _entry_facts(position)
            + "\n"
            f"Exit: {_number(position.get('exit_price'))} | Mark evidence: "
            f"{'recorded' if position.get('close_evidence') else 'unavailable'}\n"
            f"Gross: {_pnl(position.get('gross_pnl_usdt'))} | Fees: {_pnl(-abs(float(position.get('fees_usdt') or 0.0)))}\n"
            f"Net result: {_pnl(net)}\n"
            f"Today realised: {_pnl(daily_pnl)}\n"
            + "Local paper result — no live MEXC order was sent."
            + ai_note
        )
    return None


def _format_signal(candidate: Any) -> str:
    return (
        "📡 Qualified Crypto Signal — AgenticCore Crypto\n"
        f"{str(candidate.planned_side or 'unknown').upper()} {candidate.symbol}\n"
        f"Entry: {_number(candidate.last_price)} | SL: {_number(candidate.planned_stop_price)} | "
        f"TP: {_number(candidate.planned_take_profit_price)}\n"
        f"Confidence: {_number(candidate.confidence, 0)}%\n"
        "Signal only — no live MEXC order was sent."
    )


def _format_setup_status(candidate: Any) -> Optional[str]:
    entry_status = _value(getattr(candidate, "entry_status", "")).lower()
    correlation_status = _value(getattr(candidate, "correlation_status", "")).lower()
    if correlation_status == "blocked":
        title = "⚠️ Paper Setup Blocked"
        status = "Blocked by basket risk or correlation protection"
    elif "expired" in entry_status:
        title = "⌛ Paper Setup Expired"
        status = "The confirmation window closed before a valid paper entry"
    elif "pending" in entry_status:
        title = "⏳ Paper Setup Pending"
        status = "Waiting for a valid confirmed retest before any paper entry"
    else:
        return None
    return (
        f"{title} — AgenticCore Crypto\n"
        f"{str(getattr(candidate, 'planned_side', None) or 'unknown').upper()} "
        f"{getattr(candidate, 'symbol', 'Unknown')}\n"
        f"Status: {status}\n"
        f"Confidence: {_number(getattr(candidate, 'confidence', None), 0)}% | "
        f"Reason: {str(getattr(candidate, 'note', '') or 'unavailable')[:500]}\n"
        "No live MEXC order was sent."
    )


def _closed_for_period(
    positions: Iterable[Dict[str, Any]], start: datetime, end: datetime
) -> List[Dict[str, Any]]:
    """Return closes in the contiguous interval (start, end] in Dubai time."""
    records: List[Dict[str, Any]] = []
    for position in positions:
        if position.get("status") != "closed":
            continue
        raw_closed_at = position.get("closed_at")
        if not isinstance(raw_closed_at, str):
            continue
        try:
            closed_at = datetime.fromisoformat(raw_closed_at.replace("Z", "+00:00")).astimezone(DUBAI)
        except ValueError:
            continue
        if start < closed_at <= end:
            records.append(position)
    return records


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _report_evidence(
    positions: Iterable[Dict[str, Any]],
    start,
    end,
    label: str,
    state: FrameworkState,
    generated_at: datetime,
) -> Dict[str, Any]:
    all_positions = list(positions)
    closed = _closed_for_period(all_positions, start, end)
    open_positions = [
        position for position in all_positions if position.get("status") == "open"
    ]
    pnl_values = [_float(position.get("net_pnl_usdt")) for position in closed]
    return {
        "event": "operator_report",
        "label": label,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "generated_at": generated_at.isoformat(),
        "closed_positions": closed,
        "open_positions": open_positions,
        "trade_count": len(closed),
        "wins": sum(1 for pnl in pnl_values if pnl > 0),
        "losses": sum(1 for pnl in pnl_values if pnl < 0),
        "flats": sum(1 for pnl in pnl_values if pnl == 0),
        "gross_pnl_usdt": round(sum(_float(position.get("gross_pnl_usdt")) for position in closed), 8),
        "fees_usdt": round(sum(abs(_float(position.get("fees_usdt"))) for position in closed), 8),
        "net_pnl_usdt": round(sum(pnl_values), 8),
        "daily_guard_status": _value(state.daily_guard_status),
        "planned_margin_usdt": state.paper_summary.get("planned_margin_usdt"),
    }


def _format_report(evidence: Dict[str, Any], reviews: Any) -> str:
    closed = list(evidence.get("closed_positions") or [])
    open_positions = list(evidence.get("open_positions") or [])
    pnl_values = [_float(position.get("net_pnl_usdt")) for position in closed]
    total = _float(evidence.get("net_pnl_usdt"))
    trade_count = int(evidence.get("trade_count") or 0)
    win_rate = (_float(evidence.get("wins")) / trade_count * 100) if trade_count else 0.0
    label = str(evidence.get("label") or "Report")
    lines = [
        f"{'🟢' if total >= 0 else '🔴'} AgenticCore Crypto — {label}",
        f"Period: ({evidence.get('period_start', 'unavailable')}, "
        f"{evidence.get('period_end', 'unavailable')}] Dubai",
        f"Net P&L: {_pnl(total)} | Gross: {_pnl(evidence.get('gross_pnl_usdt'))} | Fees: {_pnl(-abs(_float(evidence.get('fees_usdt'))))}",
        f"Closed trades: {trade_count} (✅ {evidence.get('wins', 0)} / ❌ {evidence.get('losses', 0)} / ➖ {evidence.get('flats', 0)}) | Win rate: {win_rate:.1f}%",
        (
            f"Best: {_pnl(max(pnl_values))} | Worst: {_pnl(min(pnl_values))}"
            if pnl_values
            else "Best/Worst: unavailable — no closed local paper trades."
        ),
        f"Open exposure: {len(open_positions)} paper position(s) | "
        f"Planned isolated margin: {_pnl(evidence.get('planned_margin_usdt'))}",
        f"Daily guard: {str(evidence.get('daily_guard_status') or 'unknown').upper()}",
        "Closed trade outcomes:",
    ]
    lines.extend(
        (
            f"• {str(position.get('symbol') or 'Unknown')} {str(position.get('side') or 'unknown').upper()} | "
            f"{_trade_outcome(position)} | {str(position.get('close_reason') or 'closed').upper()} | "
            f"{_number(position.get('entry_price'))} → {_number(position.get('exit_price'))} | "
            f"Gross {_pnl(position.get('gross_pnl_usdt'))} | "
            f"Fees {_pnl(-abs(_float(position.get('fees_usdt'))))} | "
            f"Net {_pnl(position.get('net_pnl_usdt'))}"
        )
        for position in closed
    )
    if not closed:
        lines.append("• No closed local paper trades in this period.")
    lines.append("Open paper positions:")
    lines.extend(
        (
            f"• {str(position.get('symbol') or 'Unknown')} {str(position.get('side') or 'unknown').upper()} | "
            f"Entry {_number(position.get('entry_price'))} | "
            f"Last mark {_number(position.get('last_mark_price'))} | "
            f"Marked net {_pnl(position.get('marked_net_pnl_usdt'))}"
        )
        for position in open_positions
    )
    if not open_positions:
        lines.append("• None.")
    lines.append("Local paper results only — no live MEXC order was sent.")
    return "\n".join(lines) + _ai_note(reviews)


def _event_id(event: Dict[str, Any]) -> Optional[str]:
    position = event.get("position")
    position_id = position.get("id") if isinstance(position, dict) else None
    event_name = event.get("event")
    if not event_name or not position_id:
        return None
    return str(event.get("id") or f"{event_name}:{position_id}")


def _merge_outbox(
    settings: CryptoSettings, paper_events: Iterable[Dict[str, Any]]
) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Persist fresh lifecycle events before attempting outbound delivery."""
    paper_path = os.path.join(settings.runtime_dir, "paper_positions.json")
    paper_data = read_json_safe(paper_path)
    outbox = [
        event
        for event in list(paper_data.get("notification_outbox") or [])
        if isinstance(event, dict) and _event_id(event)
    ]
    queued_ids = {_event_id(event) for event in outbox}
    changed = False
    for event in paper_events:
        event_id = _event_id(event)
        if event_id and event_id not in queued_ids:
            queued = dict(event)
            queued["id"] = event_id
            outbox.append(queued)
            queued_ids.add(event_id)
            changed = True
    if changed:
        paper_data["notification_outbox"] = outbox
        write_json_atomic(paper_path, paper_data)
    return paper_data, outbox


def _is_due(now: datetime, configured_time: str) -> bool:
    try:
        hour, minute = (int(part) for part in configured_time.split(":", 1))
        return (now.hour, now.minute) >= (hour, minute)
    except (TypeError, ValueError):
        return False


def _is_last_day_of_month(value) -> bool:
    return (value + timedelta(days=1)).month != value.month


def _guard_halted(value: Any) -> bool:
    return _value(value).lower() in {"loss_limit_reached", "profit_target_reached"}


def emit_cycle_notifications(
    settings: CryptoSettings,
    state: FrameworkState,
    paper_events: Iterable[Dict[str, Any]],
    risk_blocked_symbols: Optional[Iterable[str]] = None,
    now: Optional[datetime] = None,
    sender: Optional[MessageSender] = None,
) -> int:
    """
    Deliver paper lifecycle, guard, signal, and scheduled report messages once.

    Notification bookkeeping is local and contains only event identifiers and
    dates. Delivery failure intentionally leaves an event eligible for retry.
    """
    if not _enabled(settings):
        return 0
    sender = sender or send_message
    persisted = read_json_safe(_state_path(settings))
    sent_keys = list(persisted.get("sent_keys") or [])
    sent_set = set(str(key) for key in sent_keys)
    delivered = 0
    changed = False
    local_now = (now or datetime.now(timezone.utc)).astimezone(DUBAI)

    def persist_state_now() -> None:
        """Flush delivery state before/after an outbound operation."""
        persisted["sent_keys"] = sent_keys[-MAX_DEDUP_KEYS:]
        write_json_atomic(_state_path(settings), persisted)

    def send_once(key: str, text: str) -> bool:
        nonlocal delivered, changed
        if not text:
            return False
        chunks = [text[index : index + 3900] for index in range(0, len(text), 3900)]
        if not chunks:
            return False
        complete = True
        for index, chunk in enumerate(chunks, start=1):
            part_key = key if len(chunks) == 1 else f"{key}:part:{index}:{len(chunks)}"
            if part_key in sent_set:
                continue
            if sender(settings, chunk):
                sent_keys.append(part_key)
                sent_set.add(part_key)
                delivered += 1
                changed = True
                # A restart after Telegram accepts a part must not resend it.
                persist_state_now()
            else:
                complete = False
                break
        return complete

    report_outbox = [
        item
        for item in persisted.get("report_outbox") or []
        if isinstance(item, dict)
        and item.get("id")
        and item.get("persistence_key")
        and item.get("identifier")
        and item.get("text")
    ]

    def send_report_manifest(report: Dict[str, Any]) -> bool:
        """Deliver a report sequentially using acknowledgements in its manifest."""
        nonlocal delivered
        text = str(report["text"])
        chunks = [text[index : index + 3900] for index in range(0, len(text), 3900)]
        delivered_parts = {
            int(part)
            for part in report.get("delivered_parts") or []
            if str(part).isdigit()
        }
        for index, chunk in enumerate(chunks, start=1):
            if index in delivered_parts:
                continue
            if not sender(settings, chunk):
                # Do not expose later parts until this part is acknowledged.
                return False
            delivered_parts.add(index)
            delivered += 1
            report["delivered_parts"] = sorted(delivered_parts)
            persisted["report_outbox"] = report_outbox
            # Per-report acknowledgements are retained independently of the
            # bounded global lifecycle dedup list.
            persist_state_now()
        return True

    def drain_report_outbox() -> None:
        nonlocal changed, report_outbox
        remaining: List[Dict[str, Any]] = []
        for report in report_outbox:
            if send_report_manifest(report):
                persisted[str(report["persistence_key"])] = str(report["identifier"])
                changed = True
            else:
                remaining.append(report)
        if remaining != report_outbox:
            report_outbox = remaining
            persisted["report_outbox"] = report_outbox
            changed = True
            persist_state_now()

    # Retry a failed scheduled report before evaluating the current period. This
    # keeps a 23:59 daily, Sunday, or month-end delivery eligible after midnight.
    drain_report_outbox()

    daily_pnl = state.paper_summary.get(
        "realized_pnl_today_usdt", state.daily_pnl_usdt
    )
    paper_data, outbox = _merge_outbox(settings, paper_events)
    remaining_outbox: List[Dict[str, Any]] = []
    outbox_changed = False
    for event in outbox:
        event_id = _event_id(event)
        if not event_id:
            continue
        message = _format_paper_event(event, daily_pnl)
        if send_once(event_id, message or ""):
            outbox_changed = True
        else:
            remaining_outbox.append(event)
    if outbox_changed:
        paper_data["notification_outbox"] = remaining_outbox
        write_json_atomic(
            os.path.join(settings.runtime_dir, "paper_positions.json"), paper_data
        )

    if state.last_error:
        send_once(
            f"cycle-error:{state.cycle_count}:{state.last_error[:120]}",
            f"⚠️ AgenticCore Crypto cycle alert\n{state.last_error[:500]}",
        )

    guard = _value(state.daily_guard_status)
    previous_guard = persisted.get("daily_guard")
    if previous_guard != guard:
        if _guard_halted(guard):
            delivered_guard_change = send_once(
                f"daily-guard:{guard}:{local_now.date().isoformat()}",
                "🛑 New Crypto Paper Entries Locked\n"
                f"Daily guard: {guard}\n"
                f"Current-day realised P&L: {_pnl(state.daily_pnl_usdt)}\n"
                "Existing paper positions remain monitored.",
            )
        elif _guard_halted(previous_guard):
            delivered_guard_change = send_once(
                f"daily-guard-open:{local_now.date().isoformat()}",
                "✅ New Crypto Paper Entries Open\n"
                "The daily guard is no longer blocking new local paper exposure.",
            )
        else:
            delivered_guard_change = True
    else:
        delivered_guard_change = True
    if persisted.get("daily_guard") != guard and delivered_guard_change:
        persisted["daily_guard"] = guard
        changed = True

    blocked = sorted(set(str(symbol) for symbol in (risk_blocked_symbols or []) if symbol))
    was_blocked = bool(persisted.get("basket_risk_blocked"))
    if blocked and not was_blocked:
        delivered_basket_change = send_once(
            f"basket-risk:{local_now.date().isoformat()}:{','.join(blocked)}",
            "⚠️ Crypto Basket Risk Guard Active\n"
            f"New local paper plans were blocked for: {', '.join(blocked)}\n"
            "The planned-risk or correlation safeguard prevented added exposure.",
        )
    if not blocked and was_blocked:
        delivered_basket_change = send_once(
            f"basket-risk-open:{local_now.date().isoformat()}",
            "✅ Crypto Basket Risk Guard Clear\n"
            "New local paper plans are no longer blocked by the basket safeguard.",
        )
    if bool(blocked) == was_blocked:
        delivered_basket_change = True
    if persisted.get("basket_risk_blocked") != bool(blocked) and delivered_basket_change:
        persisted["basket_risk_blocked"] = bool(blocked)
        changed = True

    if _value(state.mode).lower() in {"signal", "paper"}:
        day_key = local_now.date().isoformat()
        for candidate in state.candidates:
            if candidate.planned_side and candidate.planned_quantity is not None:
                send_once(
                    f"signal:{day_key}:{candidate.symbol}:{candidate.planned_side}",
                    _format_signal(candidate),
                )
            setup_message = _format_setup_status(candidate)
            if setup_message:
                setup_key = ":".join(
                    [
                        "setup-status",
                        day_key,
                        str(candidate.symbol),
                        _value(candidate.entry_status),
                        _value(candidate.correlation_status),
                        str(candidate.entry_structure_id or "none"),
                    ]
                )
                send_once(setup_key, setup_message)

    positions = list(paper_data.get("positions") or [])

    def send_report(
        kind: str,
        persistence_key: str,
        identifier: str,
        start,
        end,
        label: str,
    ) -> None:
        nonlocal changed, report_outbox
        report_key = f"{kind}-report:{identifier}"
        if persisted.get(persistence_key) == identifier or any(
            str(report.get("id")) == report_key for report in report_outbox
        ):
            return
        evidence = _report_evidence(positions, start, end, label, state, local_now)
        reviews = analyze_operator_report(settings, report_key, evidence)
        report_outbox.append(
            {
                "id": report_key,
                "persistence_key": persistence_key,
                "identifier": identifier,
                "text": _format_report(evidence, reviews),
                "delivered_parts": [],
                "created_at": local_now.isoformat(),
            }
        )
        persisted["report_outbox"] = report_outbox
        changed = True
        # Persist the immutable report text before its first Telegram attempt.
        persist_state_now()

    today = local_now.date()
    day_key = today.isoformat()
    if _is_due(local_now, settings.telegram_daily_summary_time):
        daily_end = datetime.combine(
            today,
            datetime.strptime(
                settings.telegram_daily_summary_time, "%H:%M"
            ).time(),
            tzinfo=DUBAI,
        )
        daily_start = daily_end - timedelta(days=1)
        send_report(
            "daily",
            "daily_report_date",
            day_key,
            daily_start,
            daily_end,
            f"Daily (through {daily_end:%d %b %Y %H:%M})",
        )

    if local_now.weekday() == 6 and _is_due(
        local_now, settings.telegram_weekly_summary_time
    ):
        weekly_end = datetime.combine(
            today,
            datetime.strptime(
                settings.telegram_weekly_summary_time, "%H:%M"
            ).time(),
            tzinfo=DUBAI,
        )
        weekly_start = weekly_end - timedelta(days=7)
        week_key = f"{weekly_start.isoformat()}:{weekly_end.isoformat()}"
        send_report(
            "weekly",
            "weekly_report_week",
            week_key,
            weekly_start,
            weekly_end,
            f"Weekly ({weekly_start:%d %b %H:%M}–{weekly_end:%d %b %H:%M})",
        )

    if _is_last_day_of_month(today) and _is_due(
        local_now, settings.telegram_monthly_summary_time
    ):
        monthly_end = datetime.combine(
            today,
            datetime.strptime(
                settings.telegram_monthly_summary_time, "%H:%M"
            ).time(),
            tzinfo=DUBAI,
        )
        previous_month_last_day = today.replace(day=1) - timedelta(days=1)
        monthly_start = datetime.combine(
            previous_month_last_day,
            monthly_end.timetz().replace(tzinfo=None),
            tzinfo=DUBAI,
        )
        month_key = f"{monthly_start.isoformat()}:{monthly_end.isoformat()}"
        send_report(
            "monthly",
            "monthly_report_month",
            month_key,
            monthly_start,
            monthly_end,
            f"Monthly ({monthly_start:%d %b %H:%M}–{monthly_end:%d %b %H:%M})",
        )

    # Deliver reports created for this cycle after they have been durably queued.
    drain_report_outbox()

    if changed:
        persisted["sent_keys"] = sent_keys[-MAX_DEDUP_KEYS:]
        write_json_atomic(_state_path(settings), persisted)
    return delivered


def notify_cycle(
    settings: CryptoSettings,
    state: Optional[FrameworkState],
    error: Optional[str] = None,
) -> bool:
    """Send cycle failures and richer periodic heartbeats."""
    if not _enabled(settings):
        return False
    if error:
        return send_message(settings, f"⚠️ AgenticCore Crypto cycle alert\n{error[:500]}")
    if state and state.cycle_count > 0 and state.cycle_count % settings.heartbeat_every_cycles == 0:
        return send_message(
            settings,
            "💓 AgenticCore Crypto heartbeat\n"
            f"Cycle: {state.cycle_count} | Mode: {_value(state.mode).upper()}\n"
            f"Candidates: {len(state.candidates)} | Paper open: {state.paper_summary.get('open_count', 0)}\n"
            f"Today realised: {_pnl(state.paper_summary.get('realized_pnl_today_usdt', state.daily_pnl_usdt))}",
        )
    return False