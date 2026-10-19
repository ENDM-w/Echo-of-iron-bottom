from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models import OrderBatch, Phase, Side
from .models import ControllerType, JudgeOverrideRecord, MatchRecord, MatchStage, SeatRecord, UserRecord


class PlatformRepository:
    """Persistence for platform state, separate from the rules snapshot tables."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS platform_users (
              user_id TEXT PRIMARY KEY,
              username TEXT NOT NULL UNIQUE,
              password_hash TEXT NOT NULL,
              is_referee INTEGER NOT NULL DEFAULT 0,
              is_observer INTEGER NOT NULL DEFAULT 0,
              disabled INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS platform_matches (
              match_id TEXT PRIMARY KEY,
              game_id TEXT NOT NULL UNIQUE,
              scenario_id TEXT NOT NULL,
              judge_user_id TEXT NOT NULL,
              phase TEXT NOT NULL,
              stage TEXT NOT NULL,
              version INTEGER NOT NULL DEFAULT 0,
              required_seat_ids_json TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              FOREIGN KEY (judge_user_id) REFERENCES platform_users(user_id)
            );
            CREATE TABLE IF NOT EXISTS platform_seats (
              seat_id TEXT PRIMARY KEY,
              match_id TEXT NOT NULL,
              user_id TEXT,
              credential_password TEXT,
              side TEXT NOT NULL,
              controlled_ship_ids_json TEXT NOT NULL,
              controller_type TEXT NOT NULL,
              FOREIGN KEY (match_id) REFERENCES platform_matches(match_id),
              FOREIGN KEY (user_id) REFERENCES platform_users(user_id),
              UNIQUE (match_id, user_id),
              UNIQUE (match_id, side, controlled_ship_ids_json)
            );
            CREATE TABLE IF NOT EXISTS platform_observers (
              match_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY (match_id, user_id),
              FOREIGN KEY (match_id) REFERENCES platform_matches(match_id),
              FOREIGN KEY (user_id) REFERENCES platform_users(user_id)
            );
            CREATE TABLE IF NOT EXISTS platform_orders (
              match_id TEXT NOT NULL,
              phase TEXT NOT NULL,
              scope TEXT NOT NULL DEFAULT 'phase',
              seat_id TEXT NOT NULL,
              request_id TEXT NOT NULL,
              batch_json TEXT NOT NULL,
              validation_errors_json TEXT NOT NULL DEFAULT '[]',
              submitted_at TEXT NOT NULL,
              PRIMARY KEY (match_id, phase, seat_id),
              FOREIGN KEY (match_id) REFERENCES platform_matches(match_id),
              FOREIGN KEY (seat_id) REFERENCES platform_seats(seat_id)
            );
            CREATE TABLE IF NOT EXISTS platform_pending_adjudications (
              match_id TEXT PRIMARY KEY,
              data_json TEXT NOT NULL,
              FOREIGN KEY (match_id) REFERENCES platform_matches(match_id)
            );
            CREATE TABLE IF NOT EXISTS platform_order_audit (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              match_id TEXT NOT NULL,
              phase TEXT NOT NULL,
              seat_id TEXT NOT NULL,
              request_id TEXT NOT NULL,
              batch_json TEXT NOT NULL,
              action TEXT NOT NULL,
              note TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS platform_seat_knowledge (
              match_id TEXT NOT NULL,
              seat_id TEXT NOT NULL,
              marker_id TEXT NOT NULL,
              PRIMARY KEY (match_id, seat_id, marker_id),
              FOREIGN KEY (match_id) REFERENCES platform_matches(match_id),
              FOREIGN KEY (seat_id) REFERENCES platform_seats(seat_id)
            );
            CREATE TABLE IF NOT EXISTS platform_seat_contacts (
              match_id TEXT NOT NULL,
              seat_id TEXT NOT NULL,
              ship_id TEXT NOT NULL,
              scale TEXT NOT NULL,
              alias TEXT NOT NULL,
              PRIMARY KEY (match_id, seat_id, ship_id),
              FOREIGN KEY (match_id) REFERENCES platform_matches(match_id),
              FOREIGN KEY (seat_id) REFERENCES platform_seats(seat_id)
            );
            CREATE TABLE IF NOT EXISTS platform_seat_signal_targets (
              match_id TEXT NOT NULL,
              seat_id TEXT NOT NULL,
              ship_id TEXT NOT NULL,
              alias TEXT NOT NULL,
              PRIMARY KEY (match_id, seat_id, ship_id),
              UNIQUE (match_id, seat_id, alias),
              FOREIGN KEY (match_id) REFERENCES platform_matches(match_id),
              FOREIGN KEY (seat_id) REFERENCES platform_seats(seat_id)
            );
            CREATE TABLE IF NOT EXISTS platform_seat_alias_migrations (
              match_id TEXT NOT NULL,
              seat_id TEXT NOT NULL,
              PRIMARY KEY (match_id, seat_id),
              FOREIGN KEY (match_id) REFERENCES platform_matches(match_id),
              FOREIGN KEY (seat_id) REFERENCES platform_seats(seat_id)
            );
            CREATE TABLE IF NOT EXISTS platform_judge_overrides (
              override_id TEXT PRIMARY KEY,
              match_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              target TEXT NOT NULL,
              reason TEXT NOT NULL,
              before_json TEXT NOT NULL,
              after_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY (match_id) REFERENCES platform_matches(match_id)
            );
            CREATE TABLE IF NOT EXISTS platform_telegrams (
              telegram_id TEXT PRIMARY KEY,
              match_id TEXT NOT NULL,
              sender_seat_id TEXT NOT NULL,
              side TEXT NOT NULL,
              sent_turn INTEGER NOT NULL,
              message TEXT NOT NULL,
              submitted_at TEXT NOT NULL,
              resolved_turn INTEGER,
              UNIQUE(match_id, sender_seat_id, sent_turn),
              FOREIGN KEY (match_id) REFERENCES platform_matches(match_id),
              FOREIGN KEY (sender_seat_id) REFERENCES platform_seats(seat_id)
            );
            CREATE TABLE IF NOT EXISTS platform_telegram_deliveries (
              telegram_id TEXT NOT NULL,
              recipient_seat_id TEXT NOT NULL,
              rule_id TEXT NOT NULL,
              received_turn INTEGER NOT NULL,
              char_count INTEGER NOT NULL,
              die_sides INTEGER NOT NULL,
              roll INTEGER NOT NULL,
              hit_count INTEGER NOT NULL DEFAULT 0,
              adjusted_roll INTEGER NOT NULL DEFAULT 0,
              delivered INTEGER NOT NULL,
              PRIMARY KEY (telegram_id, recipient_seat_id),
              FOREIGN KEY (telegram_id) REFERENCES platform_telegrams(telegram_id),
              FOREIGN KEY (recipient_seat_id) REFERENCES platform_seats(seat_id)
            );
            """
        )
        # Older MVP databases predate credentials being authored with a scenario.
        # Keep them readable and add the nullable column in place.
        try:
            self.connection.execute(
                "ALTER TABLE platform_seats ADD COLUMN credential_password TEXT"
            )
        except sqlite3.OperationalError:
            pass
        try:
            self.connection.execute("ALTER TABLE platform_users ADD COLUMN is_observer INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            self.connection.execute(
                "ALTER TABLE platform_telegram_deliveries ADD COLUMN hit_count INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass
        try:
            self.connection.execute(
                "ALTER TABLE platform_telegram_deliveries ADD COLUMN adjusted_roll INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass
        try:
            self.connection.execute(
                "ALTER TABLE platform_telegram_deliveries ADD COLUMN rule_id TEXT NOT NULL DEFAULT 'IBS-R-COMMS-01'"
            )
        except sqlite3.OperationalError:
            pass
        try:
            self.connection.execute("ALTER TABLE platform_orders ADD COLUMN scope TEXT NOT NULL DEFAULT 'phase'")
        except sqlite3.OperationalError:
            pass
        try:
            self.connection.execute("ALTER TABLE platform_orders ADD COLUMN validation_errors_json TEXT NOT NULL DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass

    def create_user(self, user: UserRecord) -> UserRecord:
        with self.connection:
            self.connection.execute(
                "INSERT INTO platform_users(user_id, username, password_hash, is_referee, is_observer, disabled) VALUES (?, ?, ?, ?, ?, ?)",
                (user.user_id, user.username, user.password_hash, int(user.is_referee), int(user.is_observer), int(user.disabled)),
            )
        return user

    def user_by_username(self, username: str) -> UserRecord | None:
        row = self.connection.execute(
            "SELECT user_id, username, password_hash, is_referee, is_observer, disabled FROM platform_users WHERE username = ?",
            (username,),
        ).fetchone()
        return _user(row) if row else None

    def user_by_id(self, user_id: str) -> UserRecord | None:
        row = self.connection.execute(
            "SELECT user_id, username, password_hash, is_referee, is_observer, disabled FROM platform_users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return _user(row) if row else None

    def has_users(self) -> bool:
        return self.connection.execute("SELECT 1 FROM platform_users LIMIT 1").fetchone() is not None

    def add_observer(self, match_id: str, user_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO platform_observers(match_id, user_id) VALUES (?, ?)",
                (match_id, user_id),
            )

    def is_match_observer(self, match_id: str, user_id: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM platform_observers WHERE match_id = ? AND user_id = ?",
            (match_id, user_id),
        ).fetchone() is not None

    def observers(self, match_id: str) -> list[dict[str, str]]:
        rows = self.connection.execute(
            """SELECT u.user_id, u.username FROM platform_observers o
               JOIN platform_users u ON u.user_id = o.user_id
               WHERE o.match_id = ? ORDER BY o.created_at, u.username""",
            (match_id,),
        ).fetchall()
        return [{"user_id": row["user_id"], "username": row["username"]} for row in rows]

    def save_match(self, match: MatchRecord) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO platform_matches
                   (match_id, game_id, scenario_id, judge_user_id, phase, stage, version, required_seat_ids_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(match_id) DO UPDATE SET
                     phase=excluded.phase, stage=excluded.stage, version=excluded.version,
                     required_seat_ids_json=excluded.required_seat_ids_json,
                     updated_at=CURRENT_TIMESTAMP""",
                (
                    match.match_id,
                    match.game_id,
                    match.scenario_id,
                    match.judge_user_id,
                    match.phase.value,
                    match.stage.value,
                    match.version,
                    json.dumps(match.required_seat_ids, ensure_ascii=False),
                ),
            )

    def match(self, match_id: str) -> MatchRecord:
        row = self.connection.execute(
            "SELECT * FROM platform_matches WHERE match_id = ?", (match_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown platform match {match_id}")
        return MatchRecord(
            match_id=row["match_id"],
            game_id=row["game_id"],
            scenario_id=row["scenario_id"],
            judge_user_id=row["judge_user_id"],
            phase=Phase(row["phase"]),
            stage=MatchStage(row["stage"]),
            version=int(row["version"]),
            required_seat_ids=json.loads(row["required_seat_ids_json"]),
        )

    def match_for_game(self, game_id: str) -> MatchRecord | None:
        row = self.connection.execute(
            "SELECT match_id FROM platform_matches WHERE game_id = ?", (game_id,)
        ).fetchone()
        return self.match(row["match_id"]) if row else None

    def pending_adjudication(self, match_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT data_json FROM platform_pending_adjudications WHERE match_id = ?", (match_id,)
        ).fetchone()
        return json.loads(row["data_json"]) if row else None

    def update_telegram_result(self, telegram_id: str, seat_id: str, hits: int, adjusted: int, delivered: bool) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE platform_telegram_deliveries SET hit_count = ?, adjusted_roll = ?, delivered = ? WHERE telegram_id = ? AND recipient_seat_id = ?",
                (hits, adjusted, int(delivered), telegram_id, seat_id),
            )

    def save_adjudication(self, match_id: str, data: dict[str, Any]) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO platform_pending_adjudications(match_id, data_json) VALUES (?, ?)",
                (match_id, json.dumps(data, ensure_ascii=False)),
            )

    def clear_adjudication(self, match_id: str) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM platform_pending_adjudications WHERE match_id = ?", (match_id,))

    def create_seat(self, seat: SeatRecord) -> SeatRecord:
        with self.connection:
            self.connection.execute(
                """INSERT INTO platform_seats
                   (seat_id, match_id, user_id, credential_password, side,
                    controlled_ship_ids_json, controller_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    seat.seat_id,
                    seat.match_id,
                    seat.user_id,
                    seat.credential_password,
                    seat.side.value,
                    json.dumps(seat.controlled_ship_ids, ensure_ascii=False),
                    seat.controller_type.value,
                ),
            )
        return seat

    def seat(self, seat_id: str) -> SeatRecord:
        row = self.connection.execute(
            """SELECT platform_seats.*, platform_users.username
               FROM platform_seats LEFT JOIN platform_users ON platform_users.user_id = platform_seats.user_id
               WHERE platform_seats.seat_id = ?""", (seat_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown platform seat {seat_id}")
        return _seat(row)

    def seats(self, match_id: str) -> list[SeatRecord]:
        rows = self.connection.execute(
            """SELECT platform_seats.*, platform_users.username
               FROM platform_seats LEFT JOIN platform_users ON platform_users.user_id = platform_seats.user_id
               WHERE platform_seats.match_id = ? ORDER BY platform_seats.seat_id""", (match_id,)
        ).fetchall()
        return [_seat(row) for row in rows]

    def save_order(
        self,
        match_id: str,
        phase: Phase,
        seat: SeatRecord,
        request_id: str,
        batch: OrderBatch,
        scope: str = "phase",
        validation_errors: list[str] | None = None,
    ) -> None:
        encoded = batch.model_dump_json()
        validation_errors = validation_errors or []
        now = datetime.now(timezone.utc).isoformat()
        with self.connection:
            self.connection.execute(
                """INSERT INTO platform_order_audit(match_id, phase, seat_id, request_id, batch_json, action, note, created_at)
                   VALUES (?, ?, ?, ?, ?, 'submitted', ?, ?)""",
                (match_id, phase.value, seat.seat_id, request_id, encoded,
                 "忽略校验报错后提交：" + "；".join(validation_errors) if validation_errors else None, now),
            )
            self.connection.execute(
                """INSERT INTO platform_orders(match_id, phase, scope, seat_id, request_id, batch_json, validation_errors_json, submitted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(match_id, phase, seat_id) DO UPDATE SET
                     scope=excluded.scope, request_id=excluded.request_id, batch_json=excluded.batch_json,
                     validation_errors_json=excluded.validation_errors_json,
                     submitted_at=excluded.submitted_at""",
                (match_id, phase.value, scope, seat.seat_id, request_id, encoded,
                 json.dumps(validation_errors, ensure_ascii=False), now),
            )

    def order_scope(self, match_id: str, phase: Phase) -> str | None:
        rows = self.connection.execute(
            "SELECT DISTINCT scope FROM platform_orders WHERE match_id = ? AND phase = ?",
            (match_id, phase.value),
        ).fetchall()
        return rows[0]["scope"] if len(rows) == 1 else None

    def orders(self, match_id: str, phase: Phase) -> dict[str, tuple[str, OrderBatch]]:
        rows = self.connection.execute(
            "SELECT seat_id, request_id, batch_json FROM platform_orders WHERE match_id = ? AND phase = ?",
            (match_id, phase.value),
        ).fetchall()
        return {
            row["seat_id"]: (row["request_id"], OrderBatch.model_validate_json(row["batch_json"]))
            for row in rows
        }

    def order_validation_errors(self, match_id: str, phase: Phase) -> dict[str, list[str]]:
        rows = self.connection.execute(
            "SELECT seat_id, validation_errors_json FROM platform_orders WHERE match_id = ? AND phase = ?",
            (match_id, phase.value),
        ).fetchall()
        return {row["seat_id"]: json.loads(row["validation_errors_json"] or "[]") for row in rows}

    def save_telegram(self, match_id: str, seat: SeatRecord, turn: int, message: str) -> dict[str, Any]:
        current = self.connection.execute(
            "SELECT telegram_id FROM platform_telegrams WHERE match_id = ? AND sender_seat_id = ? AND sent_turn = ?",
            (match_id, seat.seat_id, turn),
        ).fetchone()
        telegram_id = current["telegram_id"] if current else _new_id()
        with self.connection:
            self.connection.execute(
                """INSERT INTO platform_telegrams
                   (telegram_id, match_id, sender_seat_id, side, sent_turn, message, submitted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(match_id, sender_seat_id, sent_turn) DO UPDATE SET
                     message=excluded.message, submitted_at=excluded.submitted_at""",
                (telegram_id, match_id, seat.seat_id, seat.side.value, turn, message,
                 datetime.now(timezone.utc).isoformat()),
            )
        saved = self.connection.execute(
            "SELECT telegram_id FROM platform_telegrams WHERE match_id = ? AND sender_seat_id = ? AND sent_turn = ?",
            (match_id, seat.seat_id, turn),
        ).fetchone()
        return {"telegram_id": saved["telegram_id"], "sent_turn": turn}

    def pending_telegrams(self, match_id: str, before_turn: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT t.*, s.controlled_ship_ids_json, u.username
               FROM platform_telegrams t
               JOIN platform_seats s ON s.seat_id = t.sender_seat_id
               LEFT JOIN platform_users u ON u.user_id = s.user_id
               WHERE t.match_id = ? AND t.sent_turn < ? AND t.resolved_turn IS NULL
               ORDER BY t.sent_turn, t.submitted_at, t.telegram_id""",
            (match_id, before_turn),
        ).fetchall()
        return [dict(row) for row in rows]

    def resolve_telegram(
        self,
        telegram_id: str,
        received_turn: int,
        deliveries: list[dict[str, Any]],
    ) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE platform_telegrams SET resolved_turn = ? WHERE telegram_id = ? AND resolved_turn IS NULL",
                (received_turn, telegram_id),
            )
            if cursor.rowcount == 0:
                return False
            self.connection.executemany(
                """INSERT INTO platform_telegram_deliveries
                   (telegram_id, recipient_seat_id, rule_id, received_turn, char_count, die_sides,
                    roll, hit_count, adjusted_roll, delivered)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(telegram_id, item["recipient_seat_id"], item["rule_id"], received_turn, item["char_count"],
                  item["die_sides"], item["roll"], item["hit_count"], item["adjusted_roll"],
                  int(item["delivered"])) for item in deliveries],
            )
        return True

    def telegram_inbox(self, match_id: str, seat_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT t.telegram_id, t.sent_turn, t.message, t.side, d.rule_id,
                      d.received_turn, d.char_count, d.die_sides, d.roll,
                      d.hit_count, CASE WHEN d.adjusted_roll = 0 THEN d.roll ELSE d.adjusted_roll END AS adjusted_roll,
                      d.delivered
               FROM platform_telegram_deliveries d
               JOIN platform_telegrams t ON t.telegram_id = d.telegram_id
               WHERE t.match_id = ? AND d.recipient_seat_id = ?
               ORDER BY d.received_turn, t.sent_turn, t.submitted_at""",
            (match_id, seat_id),
        ).fetchall()
        return [
            {**dict(row), "adjusted_roll": row["adjusted_roll"] or row["roll"]}
            for row in rows
        ]

    def telegram_sent_this_turn(self, match_id: str, seat_id: str, turn: int) -> str | None:
        row = self.connection.execute(
            "SELECT message FROM platform_telegrams WHERE match_id = ? AND sender_seat_id = ? AND sent_turn = ?",
            (match_id, seat_id, turn),
        ).fetchone()
        return str(row["message"]) if row else None

    def telegram_records(self, match_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT t.*, s.controlled_ship_ids_json, u.username AS sender_username
               FROM platform_telegrams t
               JOIN platform_seats s ON s.seat_id = t.sender_seat_id
               LEFT JOIN platform_users u ON u.user_id = s.user_id
               WHERE t.match_id = ? ORDER BY t.sent_turn, t.submitted_at""",
            (match_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def telegram_deliveries(self, telegram_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT d.*, s.side, s.controlled_ship_ids_json,
                      u.username AS recipient_username
               FROM platform_telegram_deliveries d
               JOIN platform_seats s ON s.seat_id = d.recipient_seat_id
               LEFT JOIN platform_users u ON u.user_id = s.user_id
               WHERE d.telegram_id = ? ORDER BY d.recipient_seat_id""",
            (telegram_id,),
        ).fetchall()
        return [
            {**dict(row), "adjusted_roll": row["adjusted_roll"] or row["roll"]}
            for row in rows
        ]

    def audit(self, match_id: str, phase: Phase, seat_id: str, request_id: str, batch: OrderBatch, action: str, note: str | None = None) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO platform_order_audit(match_id, phase, seat_id, request_id, batch_json, action, note, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    match_id,
                    phase.value,
                    seat_id,
                    request_id,
                    batch.model_dump_json(),
                    action,
                    note,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def clear_orders(self, match_id: str, phase: Phase) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM platform_orders WHERE match_id = ? AND phase = ?",
                (match_id, phase.value),
            )

    def remember_markers(self, match_id: str, seat_id: str, marker_ids: list[str]) -> None:
        if not marker_ids:
            return
        with self.connection:
            self.connection.executemany(
                "INSERT OR IGNORE INTO platform_seat_knowledge(match_id, seat_id, marker_id) VALUES (?, ?, ?)",
                [(match_id, seat_id, marker_id) for marker_id in marker_ids],
            )

    def forget_markers(self, match_id: str, seat_id: str, marker_ids: list[str]) -> None:
        if not marker_ids:
            return
        with self.connection:
            self.connection.executemany(
                "DELETE FROM platform_seat_knowledge WHERE match_id = ? AND seat_id = ? AND marker_id = ?",
                [(match_id, seat_id, marker_id) for marker_id in marker_ids],
            )

    def contact_aliases(self, match_id: str, seat_id: str) -> dict[str, tuple[str, str]]:
        rows = self.connection.execute(
            "SELECT ship_id, scale, alias FROM platform_seat_contacts WHERE match_id = ? AND seat_id = ?",
            (match_id, seat_id),
        ).fetchall()
        return {row["ship_id"]: (row["scale"], row["alias"]) for row in rows}

    def remember_contact_alias(self, match_id: str, seat_id: str, ship_id: str, scale: str, alias: str) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT OR IGNORE INTO platform_seat_contacts(match_id, seat_id, ship_id, scale, alias)
                   VALUES (?, ?, ?, ?, ?)""",
                (match_id, seat_id, ship_id, scale, alias),
            )

    def remember_signal_target(self, match_id: str, seat_id: str, ship_id: str, alias: str) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT OR IGNORE INTO platform_seat_signal_targets(match_id, seat_id, ship_id, alias)
                   VALUES (?, ?, ?, ?)""",
                (match_id, seat_id, ship_id, alias),
            )

    def signal_targets(self, match_id: str, seat_id: str) -> dict[str, str]:
        rows = self.connection.execute(
            "SELECT ship_id, alias FROM platform_seat_signal_targets WHERE match_id = ? AND seat_id = ?",
            (match_id, seat_id),
        ).fetchall()
        return {str(row["ship_id"]): str(row["alias"]) for row in rows}

    def contact_alias_migration_done(self, match_id: str, seat_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM platform_seat_alias_migrations WHERE match_id = ? AND seat_id = ?",
            (match_id, seat_id),
        ).fetchone()
        return row is not None

    def mark_contact_alias_migration_done(self, match_id: str, seat_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO platform_seat_alias_migrations(match_id, seat_id) VALUES (?, ?)",
                (match_id, seat_id),
            )

    def rename_contact_aliases(self, match_id: str, seat_id: str, aliases: dict[str, str]) -> None:
        with self.connection:
            self.connection.executemany(
                "UPDATE platform_seat_contacts SET alias = ? WHERE match_id = ? AND seat_id = ? AND ship_id = ?",
                [(alias, match_id, seat_id, ship_id) for ship_id, alias in aliases.items()],
            )

    def known_markers(self, match_id: str, seat_id: str) -> set[str]:
        rows = self.connection.execute(
            "SELECT marker_id FROM platform_seat_knowledge WHERE match_id = ? AND seat_id = ?",
            (match_id, seat_id),
        ).fetchall()
        return {str(row["marker_id"]) for row in rows}

    def save_override(
        self,
        match_id: str,
        *,
        kind: str,
        target: str,
        reason: str,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> JudgeOverrideRecord:
        record = JudgeOverrideRecord(
            override_id=_new_id(),
            match_id=match_id,
            kind=kind,
            target=target,
            reason=reason,
            before=before,
            after=after,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        with self.connection:
            self.connection.execute(
                """INSERT INTO platform_judge_overrides
                   (override_id, match_id, kind, target, reason, before_json, after_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.override_id,
                    record.match_id,
                    record.kind,
                    record.target,
                    record.reason,
                    json.dumps(record.before, ensure_ascii=False),
                    json.dumps(record.after, ensure_ascii=False),
                    record.created_at,
                ),
            )
        return record

    def overrides(self, match_id: str) -> list[JudgeOverrideRecord]:
        rows = self.connection.execute(
            "SELECT * FROM platform_judge_overrides WHERE match_id = ? ORDER BY created_at, override_id",
            (match_id,),
        ).fetchall()
        return [
            JudgeOverrideRecord(
                override_id=row["override_id"],
                match_id=row["match_id"],
                kind=row["kind"],
                target=row["target"],
                reason=row["reason"],
                before=json.loads(row["before_json"]),
                after=json.loads(row["after_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]


def _user(row: sqlite3.Row) -> UserRecord:
    return UserRecord(
        user_id=row["user_id"],
        username=row["username"],
        password_hash=row["password_hash"],
        is_referee=bool(row["is_referee"]),
        is_observer=bool(row["is_observer"]) if "is_observer" in row.keys() else False,
        disabled=bool(row["disabled"]),
    )


def _seat(row: sqlite3.Row) -> SeatRecord:
    return SeatRecord(
        seat_id=row["seat_id"],
        match_id=row["match_id"],
        user_id=row["user_id"],
        username=row["username"] if "username" in row.keys() else None,
        credential_password=(
            row["credential_password"]
            if "credential_password" in row.keys()
            else None
        ),
        side=Side(row["side"]),
        controlled_ship_ids=json.loads(row["controlled_ship_ids_json"]),
        controller_type=ControllerType(row["controller_type"]),
    )


def _new_id() -> str:
    import uuid

    return str(uuid.uuid4())
