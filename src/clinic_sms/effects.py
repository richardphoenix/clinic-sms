"""Domain effects: immutable writes a turn queues; the Store applies them in its transaction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

JobKind = Literal["reminder", "intake_followup", "staff_relay"]


@dataclass(frozen=True)
class ConfirmAppointment:
    appointment_id: int


@dataclass(frozen=True)
class BookSlot:
    appointment_id: int
    slot_id: int


@dataclass(frozen=True)
class SaveIntakeResults:
    appointment_id: int
    results: dict[str, dict]


@dataclass(frozen=True)
class CreateTicket:
    phone: str
    question: str
    patient_label: str | None


@dataclass(frozen=True)
class AnswerTicket:
    ticket_id: int
    answer: str


@dataclass(frozen=True)
class CloseTicket:
    ticket_id: int


@dataclass(frozen=True)
class ScheduleJob:
    kind: JobKind
    phone: str
    due_at: datetime
    payload: dict


@dataclass(frozen=True)
class CancelJobs:
    phone: str
    kinds: tuple[JobKind, ...]


@dataclass(frozen=True)
class FinishJob:
    job_id: int
    status: Literal["done", "cancelled"] = "done"


Effect = (
    ConfirmAppointment
    | BookSlot
    | SaveIntakeResults
    | CreateTicket
    | AnswerTicket
    | CloseTicket
    | ScheduleJob
    | CancelJobs
    | FinishJob
)
