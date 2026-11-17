"""Clinic tables, read-only queries, and the application of domain effects."""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from clinic_sms.effects import (
    AnswerTicket,
    BookSlot,
    CancelJobs,
    CloseTicket,
    ConfirmAppointment,
    CreateTicket,
    Effect,
    FinishJob,
    JobKind,
    SaveIntakeResults,
    ScheduleJob,
)
from clinic_sms.fmt import format_phone

STAFF_PHONE = "+15550100100"
MAX_JOB_ATTEMPTS = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id INTEGER PRIMARY KEY,
    phone TEXT NOT NULL UNIQUE,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS providers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slots (
    id INTEGER PRIMARY KEY,
    provider_id INTEGER NOT NULL REFERENCES providers(id),
    start_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    slot_id INTEGER NOT NULL UNIQUE REFERENCES slots(id),
    status TEXT NOT NULL CHECK (status IN ('booked','confirmed')),
    intake_json TEXT
);
CREATE TABLE IF NOT EXISTS staff_tickets (
    id INTEGER PRIMARY KEY,
    phone TEXT NOT NULL,
    question TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open','answered','closed')),
    answer TEXT,
    created_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('reminder','intake_followup','staff_relay')),
    phone TEXT NOT NULL,
    due_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','done','cancelled','failed')),
    attempts INTEGER NOT NULL DEFAULT 0
);
"""


class SlotTakenError(Exception):
    """The slot already belongs to an appointment."""


class EffectConflictError(Exception):
    """An effect's precondition does not hold (unknown id, wrong status)."""


@dataclass(frozen=True)
class Patient:
    id: int
    phone: str
    first_name: str
    last_name: str

    @property
    def label(self) -> str:
        return f"{self.first_name} {self.last_name[:1]}."


@dataclass(frozen=True)
class Slot:
    id: int
    provider_id: int
    provider: str
    start_at: datetime
    taken: bool


@dataclass(frozen=True)
class Appointment:
    id: int
    patient_id: int
    phone: str
    slot_id: int
    provider_id: int
    provider: str
    start_at: datetime
    status: Literal["booked", "confirmed"]
    intake: dict | None


@dataclass(frozen=True)
class Ticket:
    id: int
    phone: str
    question: str
    status: Literal["open", "answered", "closed"]
    answer: str | None


@dataclass(frozen=True)
class Job:
    id: int
    kind: JobKind
    phone: str
    due_at: datetime
    payload: dict
    status: Literal["pending", "done", "cancelled", "failed"]
    attempts: int


def _one_line(text: str) -> str:
    """Collapse runs of control characters (incl. CR/LF, U+2028/9) to one space, strip the ends."""
    out: list[str] = []
    for ch in text:
        bad = unicodedata.category(ch) == "Cc" or ch in "\u2028\u2029"
        if bad:
            if not out or out[-1] != " ":
                out.append(" ")
        else:
            out.append(ch)
    return "".join(out).strip()


def to_utc_iso(at: datetime) -> str:
    """Timezone-aware datetime -> UTC ISO 8601 text (sorts correctly as a string)."""
    if at.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return at.astimezone(timezone.utc).isoformat()


_APPT_SQL = (
    "SELECT a.id, a.patient_id, p.phone, a.slot_id, s.provider_id, v.name, s.start_at,"
    " a.status, a.intake_json FROM appointments a"
    " JOIN patients p ON p.id = a.patient_id"
    " JOIN slots s ON s.id = a.slot_id"
    " JOIN providers v ON v.id = s.provider_id"
)
_SLOT_SQL = (
    "SELECT s.id, s.provider_id, v.name, s.start_at,"
    " EXISTS (SELECT 1 FROM appointments a WHERE a.slot_id = s.id)"
    " FROM slots s JOIN providers v ON v.id = s.provider_id"
)
_JOB_SQL = "SELECT id, kind, phone, due_at, payload, status, attempts FROM jobs"


def _appt(r: tuple) -> Appointment:
    return Appointment(
        r[0], r[1], r[2], r[3], r[4], r[5], datetime.fromisoformat(r[6]), r[7],
        None if r[8] is None else json.loads(r[8]),
    )


def _slot(r: tuple) -> Slot:
    return Slot(r[0], r[1], r[2], datetime.fromisoformat(r[3]), bool(r[4]))


def _job(r: tuple) -> Job:
    return Job(r[0], r[1], r[2], datetime.fromisoformat(r[3]), json.loads(r[4]), r[5], r[6])


class ClinicView:
    """Read-only queries on the store's connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _one(self, sql: str, params: tuple = ()) -> tuple | None:
        return self._conn.execute(sql, params).fetchone()

    def patient_by_phone(self, phone: str) -> Patient | None:
        r = self._one("SELECT id, phone, first_name, last_name FROM patients WHERE phone = ?", (phone,))
        return None if r is None else Patient(*r)

    def next_appointment(self, patient_id: int, now: datetime) -> Appointment | None:
        r = self._one(
            _APPT_SQL + " WHERE a.patient_id = ? AND s.start_at >= ? ORDER BY s.start_at LIMIT 1",
            (patient_id, to_utc_iso(now)),
        )
        return None if r is None else _appt(r)

    def appointment(self, appointment_id: int) -> Appointment | None:
        r = self._one(_APPT_SQL + " WHERE a.id = ?", (appointment_id,))
        return None if r is None else _appt(r)

    def slot(self, slot_id: int) -> Slot | None:
        r = self._one(_SLOT_SQL + " WHERE s.id = ?", (slot_id,))
        return None if r is None else _slot(r)

    def open_slots(
        self, *, start: datetime, end: datetime, provider_id: int | None, limit: int
    ) -> list[Slot]:
        sql = (
            _SLOT_SQL + " WHERE s.start_at >= ? AND s.start_at < ?"
            " AND NOT EXISTS (SELECT 1 FROM appointments a WHERE a.slot_id = s.id)"
        )
        params: list[Any] = [to_utc_iso(start), to_utc_iso(end)]
        if provider_id is not None:
            sql += " AND s.provider_id = ?"
            params.append(provider_id)
        sql += " ORDER BY s.start_at, s.provider_id LIMIT ?"
        params.append(limit)
        return [_slot(r) for r in self._conn.execute(sql, params).fetchall()]

    def ticket(self, ticket_id: int) -> Ticket | None:
        r = self._one(
            "SELECT id, phone, question, status, answer FROM staff_tickets WHERE id = ?",
            (ticket_id,),
        )
        return None if r is None else Ticket(*r)

    def tickets(self, phone: str) -> list[Ticket]:
        cur = self._conn.execute(
            "SELECT id, phone, question, status, answer FROM staff_tickets"
            " WHERE phone = ? ORDER BY id",
            (phone,),
        )
        return [Ticket(*r) for r in cur.fetchall()]

    def due_jobs(self, until: datetime) -> list[Job]:
        cur = self._conn.execute(
            _JOB_SQL + " WHERE status = 'pending' AND due_at <= ? ORDER BY due_at, id",
            (to_utc_iso(until),),
        )
        return [_job(r) for r in cur.fetchall()]

    def jobs(self, phone: str) -> list[Job]:
        cur = self._conn.execute(_JOB_SQL + " WHERE phone = ? ORDER BY id", (phone,))
        return [_job(r) for r in cur.fetchall()]

    def job(self, job_id: int) -> Job | None:
        r = self._one(_JOB_SQL + " WHERE id = ?", (job_id,))
        return None if r is None else _job(r)

    def failed_job_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'failed'").fetchone()[0]


def _require_changed(cur: sqlite3.Cursor, what: str) -> None:
    if cur.rowcount != 1:
        raise EffectConflictError(what)


def apply_effect(
    conn: sqlite3.Connection, effect: Effect, *, at: datetime
) -> list[tuple[str, str]]:
    """Apply one effect inside the caller's transaction; return the (phone, text) SMS it adds."""
    ts = to_utc_iso(at)
    match effect:
        case ConfirmAppointment(appointment_id):
            cur = conn.execute(
                "UPDATE appointments SET status = 'confirmed' WHERE id = ?", (appointment_id,)
            )
            _require_changed(cur, "unknown appointment")
        case BookSlot(appointment_id, slot_id):
            if conn.execute("SELECT 1 FROM slots WHERE id = ?", (slot_id,)).fetchone() is None:
                raise EffectConflictError("unknown slot")
            try:
                cur = conn.execute(
                    "UPDATE appointments SET slot_id = ?, status = 'booked' WHERE id = ?",
                    (slot_id, appointment_id),
                )
            except sqlite3.IntegrityError as e:
                raise SlotTakenError("slot already taken") from e
            _require_changed(cur, "unknown appointment")
        case SaveIntakeResults(appointment_id, results):
            cur = conn.execute(
                "UPDATE appointments SET intake_json = ? WHERE id = ?",
                (json.dumps(results), appointment_id),
            )
            _require_changed(cur, "unknown appointment")
        case CreateTicket(phone, question, label):
            cur = conn.execute(
                "INSERT INTO staff_tickets (phone, question, status, created_at)"
                " VALUES (?, ?, 'open', ?)",
                (phone, question, ts),
            )
            # The staff text is one line, so a question cannot fake a second ticket header.
            clean_label = _one_line(label) if label else ""
            who = f"{clean_label} " if clean_label else ""
            text = f"[#{cur.lastrowid}] {who}{format_phone(phone)}: {_one_line(question)}"
            return [(STAFF_PHONE, text)]
        case AnswerTicket(ticket_id, answer):
            cur = conn.execute(
                "UPDATE staff_tickets SET status = 'answered', answer = ?"
                " WHERE id = ? AND status = 'open'",
                (answer, ticket_id),
            )
            _require_changed(cur, "ticket is not open")
        case CloseTicket(ticket_id):
            cur = conn.execute(
                "UPDATE staff_tickets SET status = 'closed', closed_at = ?"
                " WHERE id = ? AND status = 'answered'",
                (ts, ticket_id),
            )
            _require_changed(cur, "ticket is not answered")
        case ScheduleJob(kind, phone, due_at, payload):
            conn.execute(
                "INSERT INTO jobs (kind, phone, due_at, payload, status) VALUES (?, ?, ?, ?, 'pending')",
                (kind, phone, to_utc_iso(due_at), json.dumps(payload)),
            )
        case CancelJobs(phone, kinds):
            if kinds:
                marks = ",".join("?" * len(kinds))
                conn.execute(
                    "UPDATE jobs SET status = 'cancelled'"
                    f" WHERE phone = ? AND status = 'pending' AND kind IN ({marks})",
                    (phone, *kinds),
                )
        case FinishJob(job_id, status):
            cur = conn.execute(
                "UPDATE jobs SET status = ? WHERE id = ? AND status = 'pending'",
                (status, job_id),
            )
            _require_changed(cur, "job is not pending")
        case _:
            raise TypeError(f"unknown effect {type(effect).__name__}")
    return []
