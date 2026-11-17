import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from clinic_sms.clinic_db import (
    MAX_JOB_ATTEMPTS,
    SCHEMA as _CLINIC_SCHEMA,
    ClinicView,
    EffectConflictError,
    Job,
    apply_effect,
    to_utc_iso,
)
from clinic_sms.effects import Effect
from clinic_sms.fmt import mask_phone
from clinic_sms.state import ConversationState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    phone TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    phone TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('in','out')),
    text TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    phone TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class OutboxRow:
    id: int
    phone: str
    text: str


@dataclass(frozen=True)
class MessageRow:
    phone: str
    direction: Literal["in", "out"]
    text: str
    at: datetime


class StaleStateError(Exception):
    """The stored conversation version no longer matches the caller's."""


def _iso(at: datetime) -> str:
    if at.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return at.isoformat()


class Store:
    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(_SCHEMA + _CLINIC_SCHEMA)
        self._clinic = ClinicView(self._conn)

    @property
    def clinic(self) -> ClinicView:
        return self._clinic

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def load(self, phone: str) -> tuple[ConversationState, int] | None:
        """The stored state and its version, or None.

        Raises pydantic.ValidationError if the stored JSON no longer parses; the error
        does not carry the stored values (`hide_input_in_errors`).
        """
        row = self._conn.execute(
            "SELECT state_json, version FROM conversations WHERE phone = ?", (phone,)
        ).fetchone()
        if row is None:
            return None
        return ConversationState.model_validate_json(row[0]), row[1]

    def commit_turn(
        self,
        *,
        phone: str,
        expected_version: int | None,
        state: ConversationState,
        inbound: str | None,
        outbound: list[str],
        at: datetime,
        effects: Sequence[Effect] = (),
    ) -> list[OutboxRow]:
        if state.phone != phone:
            raise ValueError("state.phone does not match phone")
        ts = _iso(at)
        state_json = state.model_dump_json()
        # `with conn` commits on success and rolls back on any exception.
        with self._conn:
            if expected_version is None:
                try:
                    self._conn.execute(
                        "INSERT INTO conversations (phone, version, state_json, updated_at)"
                        " VALUES (?, 1, ?, ?)",
                        (phone, state_json, ts),
                    )
                except sqlite3.IntegrityError as e:
                    raise StaleStateError(
                        f"conversation {mask_phone(phone)} already exists"
                    ) from e
            else:
                cur = self._conn.execute(
                    "UPDATE conversations SET version = version + 1, state_json = ?,"
                    " updated_at = ? WHERE phone = ? AND version = ?",
                    (state_json, ts, phone, expected_version),
                )
                if cur.rowcount == 0:
                    raise StaleStateError(
                        f"stale version {expected_version} for {mask_phone(phone)}"
                    )
            return self._log_turn(phone, inbound, outbound, effects, at, ts)

    def log_only(
        self,
        *,
        phone: str,
        inbound: str | None,
        outbound: list[str],
        at: datetime,
        effects: Sequence[Effect] = (),
    ) -> list[OutboxRow]:
        ts = _iso(at)
        with self._conn:
            return self._log_turn(phone, inbound, outbound, effects, at, ts)

    def apply(self, effects: Sequence[Effect], *, at: datetime) -> list[OutboxRow]:
        """Apply effects (and queue their SMS) in one transaction, with no conversation write."""
        ts = _iso(at)
        with self._conn:
            sms = [m for e in effects for m in apply_effect(self._conn, e, at=at)]
            return self._log_sms(sms, ts)

    def record_job_failure(self, job_id: int, *, at: datetime) -> Job:
        """Count one failed attempt; the job becomes 'failed' at MAX_JOB_ATTEMPTS."""
        to_utc_iso(at)
        with self._conn:
            cur = self._conn.execute(
                "UPDATE jobs SET attempts = attempts + 1,"
                " status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE status END"
                " WHERE id = ? AND status = 'pending'",
                (MAX_JOB_ATTEMPTS, job_id),
            )
            if cur.rowcount != 1:
                raise EffectConflictError("job is not pending")
        job = self._clinic.job(job_id)
        assert job is not None
        return job

    def _log_turn(
        self,
        phone: str,
        inbound: str | None,
        outbound: list[str],
        effects: Sequence[Effect],
        at: datetime,
        ts: str,
    ) -> list[OutboxRow]:
        sms = [m for e in effects for m in apply_effect(self._conn, e, at=at)]
        return self._log(phone, inbound, outbound, ts) + self._log_sms(sms, ts)

    def _log_sms(self, sms: list[tuple[str, str]], ts: str) -> list[OutboxRow]:
        rows: list[OutboxRow] = []
        for phone, text in sms:
            rows += self._log(phone, None, [text], ts)
        return rows

    def _log(
        self, phone: str, inbound: str | None, outbound: list[str], ts: str
    ) -> list[OutboxRow]:
        if inbound is not None:
            self._conn.execute(
                "INSERT INTO messages (phone, direction, text, at) VALUES (?, 'in', ?, ?)",
                (phone, inbound, ts),
            )
        rows = []
        for text in outbound:
            self._conn.execute(
                "INSERT INTO messages (phone, direction, text, at) VALUES (?, 'out', ?, ?)",
                (phone, text, ts),
            )
            cur = self._conn.execute(
                "INSERT INTO outbox (phone, text, created_at) VALUES (?, ?, ?)",
                (phone, text, ts),
            )
            rows.append(OutboxRow(id=cur.lastrowid, phone=phone, text=text))
        return rows

    def pending_outbox(self) -> list[OutboxRow]:
        cur = self._conn.execute(
            "SELECT id, phone, text FROM outbox WHERE delivered_at IS NULL ORDER BY id"
        )
        return [OutboxRow(*r) for r in cur.fetchall()]

    def mark_delivered(self, outbox_id: int, at: datetime) -> None:
        ts = _iso(at)
        with self._conn:
            if self._conn.execute(
                "SELECT 1 FROM outbox WHERE id = ?", (outbox_id,)
            ).fetchone() is None:
                raise KeyError(outbox_id)
            self._conn.execute(
                "UPDATE outbox SET delivered_at = ? WHERE id = ? AND delivered_at IS NULL",
                (ts, outbox_id),
            )

    def messages(self, phone: str) -> list[MessageRow]:
        cur = self._conn.execute(
            "SELECT phone, direction, text, at FROM messages WHERE phone = ? ORDER BY id",
            (phone,),
        )
        return [MessageRow(p, d, t, datetime.fromisoformat(a)) for p, d, t, a in cur.fetchall()]

    def get_setting(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return None if row is None else row[0]

    def set_setting(self, key: str, value: str | None) -> None:
        """Store `value` under `key`; None deletes the key."""
        with self._conn:
            if value is None:
                self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                self._conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    def outbound_count(self, text: str) -> int:
        """Number of outbound messages, to any phone, whose text equals `text`."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE direction = 'out' AND text = ?", (text,)
        ).fetchone()
        return row[0]
