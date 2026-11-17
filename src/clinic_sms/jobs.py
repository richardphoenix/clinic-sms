"""Background jobs: the reminder time rule, and the Scheduler that runs due jobs."""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

from clinic_sms.clinic_db import Job
from clinic_sms.clock import CLINIC_TZ, Clock
from clinic_sms.effects import CancelJobs, CloseTicket, Effect, FinishJob, ScheduleJob
from clinic_sms.fmt import format_when, mask_phone
from clinic_sms.logsafe import describe_exception
from clinic_sms.proactive import Proactive
from clinic_sms.state import ConversationState
from clinic_sms.store import Store

if TYPE_CHECKING:
    from clinic_sms.runner import ConversationRunner

REMINDER_LEAD = timedelta(hours=48)
REMINDER_INSTRUCTIONS = (
    "Send the patient a reminder of their appointment on {when} with {provider}. "
    "Ask them to reply 1 to confirm or 2 to reschedule."
)
# For a reminder that falls due while the patient is in the middle of rescheduling or of
# the intake: only reception can confirm, and to the reschedule agent a "1" could read as
# the first slot offered, so this one offers no numbered choices.
REMINDER_IN_FLOW_INSTRUCTIONS = (
    "Remind the patient in one sentence of their appointment on {when} with {provider}, "
    "without asking them to confirm it or to reply with a number. "
    "Then continue where you left off."
)

FOLLOWUP_AFTER = timedelta(hours=4)
QUIET_START = time(21, 0)  # 9:00 PM, quiet from here (inclusive)
QUIET_END = time(8, 0)  # 8:00 AM, sending allowed from here (inclusive)
MAX_FOLLOWUPS = 2  # per intake
FOLLOWUP_INSTRUCTIONS = (
    "The patient has not replied for a while. Send one short, friendly nudge "
    "that repeats only your current question."
)

RELAY_INSTRUCTIONS = (
    "The front desk answered the patient's earlier question. Pass the answer on in your own "
    "words, then continue where you left off.\nQuestion: {question}\nAnswer: {answer}"
)

logger = logging.getLogger(__name__)


def reminder_effects(
    *, phone: str, appointment_id: int, start_at: datetime, now: datetime
) -> list[Effect]:
    """Replace the phone's pending reminder; schedule a new one if it is still in the future."""
    effects: list[Effect] = [CancelJobs(phone, ("reminder",))]
    due = start_at - REMINDER_LEAD
    if due > now:
        effects.append(ScheduleJob("reminder", phone, due, {"appointment_id": appointment_id}))
    return effects


def shift_out_of_quiet_hours(at: datetime) -> datetime:
    """`at`, or the next 8:00 AM clinic time if it falls in quiet hours (9 PM to 8 AM)."""
    local = at.astimezone(CLINIC_TZ)
    if local.time() >= QUIET_START:
        day = local.date() + timedelta(days=1)
    elif local.time() < QUIET_END:
        day = local.date()
    else:
        return at
    return datetime.combine(day, QUIET_END, tzinfo=CLINIC_TZ)


def followup_due(now: datetime) -> datetime:
    return shift_out_of_quiet_hours(now + FOLLOWUP_AFTER)


def intake_pending(state: ConversationState) -> bool:
    return state.active_agent == "intake" and not state.intake.done


def followup_effects(
    state: ConversationState, *, now: datetime, patient_replied: bool
) -> list[Effect]:
    """The follow-up job changes for a turn that has just committed `state`.

    The 4 hours count from the last committed turn: a patient's reply, or a message the
    clinic sent. Any pending follow-up is replaced, or dropped if none is wanted.
    """
    pending = intake_pending(state)
    effects: list[Effect] = []
    if patient_replied or pending:
        effects.append(CancelJobs(state.phone, ("intake_followup",)))
    if pending and state.intake_followups_sent < MAX_FOLLOWUPS:
        effects.append(ScheduleJob("intake_followup", state.phone, followup_due(now), {}))
    return effects


class Scheduler:
    """Runs the pending jobs that are due: each one a proactive turn, or a cancel.

    Runs only from the simulator's command loop, never concurrently with an inbound
    message, so a job's turn and a patient's turn never interleave. A job that fails is
    logged and its attempt counted (`Store.record_job_failure`): it stays pending, to run
    again, until MAX_JOB_ATTEMPTS attempts have failed, and then it is failed.
    """

    def __init__(self, *, store: Store, runner: ConversationRunner, clock: Clock) -> None:
        self._store = store
        self._runner = runner
        self._clock = clock

    def next_due(self, until: datetime) -> Job | None:
        """The first pending job due by `until`, in (due_at, id) order; None if there is none."""
        due = self._store.clinic.due_jobs(until)
        return due[0] if due else None

    async def run_due(self) -> int:
        """Run every pending job due by now, in (due_at, id) order, once each; return how many.

        A job that an earlier one in this call finished or cancelled (through its turn's
        effects) is skipped and not counted. A job's failure is logged and counted, not
        raised; this raises only if that cannot be recorded, or the jobs cannot be read.
        """
        ran = 0
        for due in self._store.clinic.due_jobs(self._clock.now()):
            job = self._store.clinic.job(due.id)
            if job is None or job.status != "pending":
                continue
            await self._run(job)
            ran += 1
        return ran

    async def _run(self, job: Job) -> None:
        now = self._clock.now()
        try:
            match job.kind:
                case "reminder":
                    await self._remind(job, now)
                case "intake_followup":
                    await self._follow_up(job, now)
                case "staff_relay":
                    await self._relay(job, now)
                # unreachable through the jobs table, whose CHECK allows only the kinds above
                case _:
                    raise NotImplementedError(f"no handler for {job.kind} jobs")
        except Exception as e:
            logger.error(
                "job %d (%s) for %s failed: %s",
                job.id,
                job.kind,
                mask_phone(job.phone),
                describe_exception(e),
            )
            self._store.record_job_failure(job.id, at=now)

    async def _remind(self, job: Job, now: datetime) -> None:
        """The 48-hour reminder; cancelled if its appointment is gone or has started.

        Sent with REMINDER_INSTRUCTIONS (reply 1 or 2) when reception answers next, or when
        the reminder starts the conversation; with REMINDER_IN_FLOW_INSTRUCTIONS while
        rescheduling or the intake is in progress.
        """
        appointment = self._store.clinic.appointment(job.payload["appointment_id"])
        if appointment is None or appointment.start_at <= now:
            self._store.apply([FinishJob(job.id, "cancelled")], at=now)
            return
        loaded = self._store.load(job.phone)
        in_flow = loaded is not None and loaded[0].active_agent not in (None, "reception")
        template = REMINDER_IN_FLOW_INSTRUCTIONS if in_flow else REMINDER_INSTRUCTIONS
        instructions = template.format(
            when=format_when(appointment.start_at), provider=appointment.provider
        )
        await self._runner.run_proactive(
            job.phone,
            Proactive(instructions, effects=(FinishJob(job.id),), may_start=True),
        )

    async def _follow_up(self, job: Job, now: datetime) -> None:
        """A nudge for an intake still waiting; cancelled, with no LLM call, if it is stale."""
        loaded = self._store.load(job.phone)
        if (
            loaded is None
            or not intake_pending(loaded[0])
            or loaded[0].intake_followups_sent >= MAX_FOLLOWUPS
        ):
            self._store.apply([FinishJob(job.id, "cancelled")], at=now)
            return
        await self._runner.run_proactive(
            job.phone,
            Proactive(FOLLOWUP_INSTRUCTIONS, effects=(FinishJob(job.id),), followup=True),
        )

    async def _relay(self, job: Job, now: datetime) -> None:
        """The front desk's answer, passed on by the patient's active agent.

        A proactive turn: in the middle of an intake, the step that waits for an answer
        sends it, then asks its question again. The ticket closes with the turn. Cancelled,
        with no LLM call, unless the ticket exists and is answered.
        """
        ticket = self._store.clinic.ticket(job.payload["ticket_id"])
        if ticket is None or ticket.status != "answered":
            self._store.apply([FinishJob(job.id, "cancelled")], at=now)
            return
        instructions = RELAY_INSTRUCTIONS.format(question=ticket.question, answer=ticket.answer)
        await self._runner.run_proactive(
            ticket.phone,
            Proactive(instructions, effects=(FinishJob(job.id), CloseTicket(ticket.id))),
        )
