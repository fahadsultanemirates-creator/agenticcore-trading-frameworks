"""
Durable per-coin intelligence for Crypto Tier 1.

The latest framework state is intentionally small and replaceable. This store
keeps the longer-lived evidence that lets the worker understand a symbol across
cycles, restarts, listings, setup expiry, and paper-trade outcomes.

Only public market evidence and local paper-trade records are stored here.
There is no private exchange connectivity and no execution state.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from domain.models import Candidate, ContractDetail, Ticker
from storage.state import write_json_atomic


MEMORY_SCHEMA_VERSION = "1"
_MAX_SNAPSHOT_COINS = 2_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def _contract_dict(contract: ContractDetail) -> Dict[str, Any]:
    return {
        "symbol": contract.symbol,
        "display_name": contract.display_name,
        "base_coin": contract.base_coin,
        "quote_coin": contract.quote_coin,
        "contract_size": contract.contract_size,
        "volume_step": contract.volume_step,
        "min_quantity": contract.min_quantity,
        "max_quantity": contract.max_quantity,
        "price_precision": contract.price_precision,
        "quantity_precision": contract.quantity_precision,
        "is_active": contract.is_active,
        "fetched_at": contract.fetched_at,
        "contract_type": contract.contract_type,
        "concept_plates": contract.concept_plates,
        "price_increment": contract.price_increment,
    }


def _ticker_dict(ticker: Optional[Ticker]) -> Dict[str, Any]:
    return asdict(ticker) if ticker is not None else {}


class CoinMemoryStore:
    """SQLite-backed memory store with an atomic dashboard/Manager export."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "CoinMemoryStore":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS coin_profiles (
                symbol TEXT PRIMARY KEY,
                base_coin TEXT,
                display_name TEXT,
                quote_coin TEXT,
                contract_type INTEGER,
                is_active INTEGER NOT NULL DEFAULT 1,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_observed_at TEXT,
                last_data_status TEXT,
                last_price REAL,
                last_turnover_usdt REAL,
                last_change_pct_24h REAL,
                radar_state TEXT NOT NULL DEFAULT 'watch',
                radar_rank INTEGER,
                last_confidence INTEGER,
                last_signal_status TEXT,
                last_entry_status TEXT,
                last_side TEXT,
                last_reason TEXT,
                total_scans INTEGER NOT NULL DEFAULT 0,
                total_eligible_scans INTEGER NOT NULL DEFAULT 0,
                total_probes INTEGER NOT NULL DEFAULT 0,
                total_setup_events INTEGER NOT NULL DEFAULT 0,
                total_trade_count INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                flats INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cycle_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                cycle_count INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                eligible INTEGER NOT NULL DEFAULT 0,
                probed INTEGER NOT NULL DEFAULT 0,
                data_status TEXT,
                evidence_json TEXT NOT NULL,
                UNIQUE(symbol, cycle_count)
            );
            CREATE INDEX IF NOT EXISTS idx_cycle_observations_symbol_time
                ON cycle_observations(symbol, observed_at DESC);
            CREATE TABLE IF NOT EXISTS setup_events (
                event_key TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                cycle_count INTEGER NOT NULL,
                occurred_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                side TEXT,
                structure_id TEXT,
                entry_status TEXT,
                confidence INTEGER,
                reason TEXT,
                evidence_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_setup_events_symbol_time
                ON setup_events(symbol, occurred_at DESC);
            CREATE TABLE IF NOT EXISTS trade_events (
                event_key TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                side TEXT,
                outcome TEXT,
                reason TEXT,
                evidence_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_trade_events_symbol_time
                ON trade_events(symbol, occurred_at DESC);
            CREATE TABLE IF NOT EXISTS heartbeats (
                cycle_count INTEGER PRIMARY KEY,
                occurred_at TEXT NOT NULL,
                status TEXT NOT NULL,
                eligible_count INTEGER NOT NULL DEFAULT 0,
                probed_count INTEGER NOT NULL DEFAULT 0,
                radar_count INTEGER NOT NULL DEFAULT 0,
                open_position_count INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_heartbeats_time
                ON heartbeats(occurred_at DESC);
            INSERT OR REPLACE INTO memory_meta(key, value)
                VALUES ('schema_version', '1');
            """
        )
        self.connection.commit()

    @staticmethod
    def _row_dict(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def record_universe(
        self,
        cycle_count: int,
        contracts: Iterable[ContractDetail],
        tickers: Dict[str, Ticker],
        eligible_symbols: Iterable[str],
        probed_symbols: Iterable[str],
        observed_at: Optional[str] = None,
    ) -> None:
        """Record every discovered symbol and a compact observation for eligible symbols."""
        now = observed_at or _now()
        eligible = set(eligible_symbols)
        probed = set(probed_symbols)
        contracts_list = list(contracts)
        active_symbols = {contract.symbol for contract in contracts_list if contract.is_active}
        with self.connection:
            for contract in contracts_list:
                existing = self.connection.execute(
                    "SELECT first_seen_at FROM coin_profiles WHERE symbol = ?",
                    (contract.symbol,),
                ).fetchone()
                first_seen = existing["first_seen_at"] if existing else now
                ticker = tickers.get(contract.symbol)
                self.connection.execute(
                    """
                    INSERT INTO coin_profiles(
                        symbol, base_coin, display_name, quote_coin, contract_type,
                        is_active, first_seen_at, last_seen_at, last_observed_at,
                        last_data_status, last_price, last_turnover_usdt,
                        last_change_pct_24h, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        base_coin=excluded.base_coin,
                        display_name=excluded.display_name,
                        quote_coin=excluded.quote_coin,
                        contract_type=excluded.contract_type,
                        is_active=excluded.is_active,
                        last_seen_at=excluded.last_seen_at,
                        last_observed_at=COALESCE(excluded.last_observed_at, coin_profiles.last_observed_at),
                        last_data_status=COALESCE(excluded.last_data_status, coin_profiles.last_data_status),
                        last_price=COALESCE(excluded.last_price, coin_profiles.last_price),
                        last_turnover_usdt=COALESCE(excluded.last_turnover_usdt, coin_profiles.last_turnover_usdt),
                        last_change_pct_24h=COALESCE(excluded.last_change_pct_24h, coin_profiles.last_change_pct_24h),
                        total_scans=coin_profiles.total_scans + 1,
                        updated_at=excluded.updated_at
                    """,
                    (
                        contract.symbol,
                        contract.base_coin,
                        contract.display_name,
                        contract.quote_coin,
                        contract.contract_type,
                        int(contract.is_active),
                        first_seen,
                        now,
                        ticker.fetched_at if ticker else None,
                        "fresh" if ticker and ticker.last_price else None,
                        ticker.last_price if ticker else None,
                        ticker.turnover_24h_usdt if ticker else None,
                        ticker.change_pct_24h if ticker else None,
                        now,
                    ),
                )
                if not existing:
                    self.connection.execute(
                        "UPDATE coin_profiles SET total_scans = 1 WHERE symbol = ?",
                        (contract.symbol,),
                    )
                if contract.symbol in eligible:
                    self.connection.execute(
                        "UPDATE coin_profiles SET total_eligible_scans = total_eligible_scans + 1 WHERE symbol = ?",
                        (contract.symbol,),
                    )
                if contract.symbol in probed:
                    self.connection.execute(
                        "UPDATE coin_profiles SET total_probes = total_probes + 1 WHERE symbol = ?",
                        (contract.symbol,),
                    )

            self.connection.execute(
                "UPDATE coin_profiles SET is_active = 0, updated_at = ?",
                (now,),
            )
            symbol_list = list(active_symbols)
            for start in range(0, len(symbol_list), 900):
                batch = symbol_list[start : start + 900]
                if not batch:
                    continue
                self.connection.execute(
                    "UPDATE coin_profiles SET is_active = 1, updated_at = ? WHERE symbol IN ({})".format(
                        ",".join("?" for _ in batch)
                    ),
                    (now, *batch),
                )

            for symbol in eligible:
                ticker = tickers.get(symbol)
                self.connection.execute(
                    """
                    INSERT INTO cycle_observations(
                        symbol, cycle_count, observed_at, eligible, probed,
                        data_status, evidence_json
                    ) VALUES (?, ?, ?, 1, ?, ?, ?)
                    ON CONFLICT(symbol, cycle_count) DO UPDATE SET
                        observed_at=excluded.observed_at,
                        probed=excluded.probed,
                        data_status=excluded.data_status,
                        evidence_json=excluded.evidence_json
                    """,
                    (
                        symbol,
                        cycle_count,
                        now,
                        int(symbol in probed),
                        "fresh" if ticker and ticker.last_price else "stale",
                        _json({"ticker": _ticker_dict(ticker)}),
                    ),
                )

    def record_candidates(
        self,
        cycle_count: int,
        candidates: Iterable[Candidate],
        selected_symbols: Iterable[str],
        observed_at: Optional[str] = None,
    ) -> None:
        """Persist the scored radar and only record setup events on lifecycle changes."""
        now = observed_at or _now()
        selected = set(selected_symbols)
        candidate_list = list(candidates)
        with self.connection:
            for radar_rank, candidate in enumerate(candidate_list, start=1):
                if candidate.symbol in selected:
                    radar_state = "selected"
                elif candidate.correlation_status == "blocked":
                    radar_state = "blocked"
                elif candidate.entry_status == "confirmed":
                    radar_state = "confirmed"
                elif candidate.entry_status == "pending_entry":
                    radar_state = "pending"
                elif candidate.entry_status == "expired":
                    radar_state = "cooldown"
                else:
                    radar_state = "watch"
                self.connection.execute(
                    """
                    UPDATE coin_profiles SET
                        last_observed_at=?,
                        radar_state=?,
                        radar_rank=?,
                        last_confidence=?,
                        last_signal_status=?,
                        last_entry_status=?,
                        last_side=?,
                        last_reason=?,
                        updated_at=?
                    WHERE symbol=?
                    """,
                    (
                        now,
                        radar_state,
                        radar_rank,
                        candidate.confidence,
                        candidate.signal_status,
                        candidate.entry_status,
                        candidate.planned_side,
                        candidate.note,
                        now,
                        candidate.symbol,
                    ),
                )
                event_key = ":".join(
                    [
                        str(cycle_count),
                        candidate.symbol,
                        candidate.entry_status,
                        candidate.correlation_status,
                        candidate.selection_status,
                        candidate.entry_structure_id or "none",
                    ]
                )
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO setup_events(
                        event_key, symbol, cycle_count, occurred_at, event_type,
                        side, structure_id, entry_status, confidence, reason,
                        evidence_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_key,
                        candidate.symbol,
                        cycle_count,
                        now,
                        "candidate_observed",
                        candidate.planned_side,
                        candidate.entry_structure_id,
                        candidate.entry_status,
                        candidate.confidence,
                        candidate.note,
                        _json(asdict(candidate)),
                    ),
                )
            self.connection.execute(
                """
                UPDATE coin_profiles
                SET total_setup_events = (
                    SELECT COUNT(*) FROM setup_events
                    WHERE setup_events.symbol = coin_profiles.symbol
                )
                WHERE symbol IN ({})
                """.format(",".join("?" for _ in {candidate.symbol for candidate in candidate_list}) or "''"),
                tuple({candidate.symbol for candidate in candidate_list}),
            )

    def record_trade_events(self, events: Iterable[Dict[str, Any]]) -> None:
        """Store immutable paper lifecycle evidence and aggregate outcomes."""
        event_list = [event for event in events if isinstance(event, dict)]
        with self.connection:
            for event in event_list:
                position = event.get("position")
                if not isinstance(position, dict) or not position.get("id"):
                    continue
                event_key = str(event.get("id") or f"{event.get('event')}:{position['id']}")
                symbol = str(position.get("symbol") or event.get("symbol") or "")
                event_name = str(event.get("event") or "paper_event")
                net = position.get("net_pnl_usdt")
                outcome = None
                if event_name == "paper_position_closed":
                    try:
                        numeric_net = float(net)
                        outcome = "win" if numeric_net > 0 else "loss" if numeric_net < 0 else "flat"
                    except (TypeError, ValueError):
                        outcome = "unknown"
                inserted = self.connection.execute(
                    """
                    INSERT OR IGNORE INTO trade_events(
                        event_key, symbol, occurred_at, event_type, side,
                        outcome, reason, evidence_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_key,
                        symbol,
                        str(position.get("closed_at") or position.get("opened_at") or _now()),
                        event_name,
                        position.get("side"),
                        outcome,
                        position.get("close_reason") or event_name,
                        _json(position),
                    ),
                )
                if inserted.rowcount == 1 and event_name == "paper_position_closed":
                    self.connection.execute(
                        """
                        UPDATE coin_profiles SET
                            total_trade_count=total_trade_count+1,
                            wins=wins+?,
                            losses=losses+?,
                            flats=flats+?,
                            updated_at=?
                        WHERE symbol=?
                        """,
                        (
                            int(outcome == "win"),
                            int(outcome == "loss"),
                            int(outcome == "flat"),
                            _now(),
                            symbol,
                        ),
                    )
            self.connection.execute(
                """
                UPDATE coin_profiles SET total_trade_count=(
                    SELECT COUNT(*) FROM trade_events
                    WHERE trade_events.symbol=coin_profiles.symbol
                    AND trade_events.event_type='paper_position_closed'
                )
                """
            )

    def record_heartbeat(
        self,
        cycle_count: int,
        status: str,
        eligible_count: int,
        probed_count: int,
        radar_count: int,
        open_position_count: int,
        error: Optional[str] = None,
        occurred_at: Optional[str] = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO heartbeats(
                    cycle_count, occurred_at, status, eligible_count,
                    probed_count, radar_count, open_position_count, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle_count,
                    occurred_at or _now(),
                    status,
                    eligible_count,
                    probed_count,
                    radar_count,
                    open_position_count,
                    error,
                ),
            )

    def _profile_rows(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        if symbol:
            rows = self.connection.execute(
                "SELECT * FROM coin_profiles WHERE symbol = ?", (symbol,)
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT * FROM coin_profiles
                ORDER BY CASE WHEN radar_rank IS NULL THEN 1 ELSE 0 END,
                         radar_rank ASC, last_observed_at DESC
                LIMIT ?
                """,
                (_MAX_SNAPSHOT_COINS,),
            ).fetchall()
        return [self._row_dict(row) for row in rows]

    def _recent_rows(
        self, table: str, symbol: str, limit: int, where: str = ""
    ) -> List[Dict[str, Any]]:
        if table == "cycle_observations":
            query = f"SELECT * FROM {table} WHERE symbol=? {where} ORDER BY observed_at DESC LIMIT ?"
        else:
            query = f"SELECT * FROM {table} WHERE symbol=? {where} ORDER BY occurred_at DESC LIMIT ?"
        rows = self.connection.execute(query, (symbol, limit)).fetchall()
        result: List[Dict[str, Any]] = []
        for row in rows:
            value = self._row_dict(row)
            raw_json = value.pop("evidence_json", None)
            if raw_json:
                try:
                    value["evidence"] = json.loads(raw_json)
                except json.JSONDecodeError:
                    value["evidence"] = None
            result.append(value)
        return result

    def get_coin(self, symbol: str, history_limit: int = 50) -> Optional[Dict[str, Any]]:
        profiles = self._profile_rows(symbol.upper())
        if not profiles:
            return None
        profile = profiles[0]
        return {
            "profile": profile,
            "recent_observations": self._recent_rows("cycle_observations", symbol.upper(), history_limit),
            "setup_events": self._recent_rows("setup_events", symbol.upper(), history_limit),
            "trade_events": self._recent_rows("trade_events", symbol.upper(), history_limit),
        }

    def radar(self, limit: int = 25) -> List[Dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT symbol, base_coin, display_name, is_active, last_observed_at,
                   last_price, last_turnover_usdt, last_change_pct_24h,
                   radar_state, radar_rank, last_confidence, last_signal_status,
                   last_entry_status, last_side, last_reason, total_scans,
                   total_eligible_scans, total_probes, total_setup_events,
                   total_trade_count, wins, losses, flats
            FROM coin_profiles
            ORDER BY CASE WHEN radar_rank IS NULL THEN 1 ELSE 0 END,
                     radar_rank ASC, last_observed_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [self._row_dict(row) for row in rows]

    def summary(self) -> Dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT
                COUNT(*) AS known_coins,
                SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END) AS active_coins,
                SUM(CASE WHEN last_observed_at IS NOT NULL THEN 1 ELSE 0 END) AS observed_coins,
                SUM(CASE WHEN radar_state IN ('confirmed','selected') THEN 1 ELSE 0 END) AS confirmed_radar,
                MAX(updated_at) AS last_update
            FROM coin_profiles
            """
        ).fetchone()
        heartbeat = self.connection.execute(
            "SELECT * FROM heartbeats ORDER BY occurred_at DESC LIMIT 1"
        ).fetchone()
        result = self._row_dict(row) if row else {}
        result["schema_version"] = MEMORY_SCHEMA_VERSION
        result["last_heartbeat"] = self._row_dict(heartbeat) if heartbeat else None
        result["outcome_by_condition"] = self.outcome_by_condition()
        return result

    def outcome_by_condition(self) -> List[Dict[str, Any]]:
        """
        Aggregate recorded paper outcomes by deterministic entry labels.

        This is observational reporting only. Small samples never alter a rule,
        size, signal, or AI prompt.
        """
        rows = self.connection.execute(
            """
            SELECT outcome, evidence_json
            FROM trade_events
            WHERE event_type='paper_position_closed'
            ORDER BY occurred_at DESC
            """
        ).fetchall()
        buckets: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            try:
                evidence = json.loads(row["evidence_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            entry = evidence.get("entry_evidence") if isinstance(evidence, dict) else {}
            tags = evidence.get("condition_tags") if isinstance(evidence, dict) else []
            if not isinstance(entry, dict) or not isinstance(tags, list):
                continue
            try:
                net = float(evidence.get("net_pnl_usdt") or 0.0)
            except (TypeError, ValueError):
                net = 0.0
            for raw_tag in {str(tag) for tag in tags if tag}:
                bucket = buckets.setdefault(
                    raw_tag,
                    {
                        "condition": raw_tag,
                        "sample_count": 0,
                        "wins": 0,
                        "losses": 0,
                        "flats": 0,
                        "net_pnl_usdt": 0.0,
                    },
                )
                bucket["sample_count"] += 1
                bucket["wins"] += int(row["outcome"] == "win")
                bucket["losses"] += int(row["outcome"] == "loss")
                bucket["flats"] += int(row["outcome"] == "flat")
                bucket["net_pnl_usdt"] += net
        return [
            {
                **bucket,
                "net_pnl_usdt": round(float(bucket["net_pnl_usdt"]), 8),
                "average_net_pnl_usdt": round(
                    float(bucket["net_pnl_usdt"]) / int(bucket["sample_count"]), 8
                )
                if bucket["sample_count"]
                else None,
            }
            for bucket in sorted(
                buckets.values(), key=lambda item: (-int(item["sample_count"]), item["condition"])
            )
        ]

    def write_snapshot(self, path: str, radar_limit: int = 25) -> Dict[str, Any]:
        """Export queryable current profiles plus recent evidence for the API/Manager."""
        profiles = self._profile_rows()
        coins: Dict[str, Any] = {}
        for profile in profiles:
            symbol = profile["symbol"]
            coins[symbol] = {
                "profile": profile,
                "recent_observations": self._recent_rows("cycle_observations", symbol, 10),
                "setup_events": self._recent_rows("setup_events", symbol, 25),
                "trade_events": self._recent_rows("trade_events", symbol, 25),
            }
        snapshot = {
            "schema_version": MEMORY_SCHEMA_VERSION,
            "generated_at": _now(),
            "summary": self.summary(),
            "radar": self.radar(radar_limit),
            "coins": coins,
        }
        write_json_atomic(path, snapshot)
        return snapshot