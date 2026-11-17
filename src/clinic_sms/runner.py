"""ConversationRunner: one cold-started AgentSession per turn (spec §5.3).

A turn answers an inbound SMS, or sends a message the clinic starts (a proactive turn).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from types import TracebackType

from livekit.agents import (
    Agent,
    AgentSession,
    ConversationItemAddedEvent,
    ErrorEvent,
    FunctionToolsExecutedEvent,
    RunResult,
    llm,
)

from clinic_sms.agents import registry
from clinic_sms.channel import Channel, Outbound
from clinic_sms.clinic_db import STAFF_PHONE, Patient
from clinic_sms.clock import Clock
from clinic_sms.effects import AnswerTicket, ScheduleJob
from clinic_sms.fmt import mask_phone
from clinic_sms.jobs import followup_effects
from clinic_sms.logsafe import describe_exception, install_sdk_log_redaction
from clinic_sms.proactive import NoConversationError, Proactive
from clinic_sms.state import ConversationState, IntakeState
from clinic_sms.store import OutboxRow, Store
from clinic_sms.turn import Turn
from clinic_sms.workflow import ReadySignal

FALLBACK_TEXT = "Sorry, something went wrong on our end — please reply again."
NO_CONVERSATION_TEXT = "Sorry, we don't have an open request for this number."
RUN_TIMEOUT_S = 60.0
CLOSE_TIMEOUT_S = 10.0
STAFF_USAGE_TEXT = (
    "To answer a question, reply with its ticket number and your answer, "
    "e.g. #12 Your copay was $25."
)
STAFF_UNKNOWN_TICKET_TEXT = "There is no open ticket #{ticket_id}."

_STAFF_REPLY = re.compile(r"^\s*#(\d+)\s+(\S.*)$", re.DOTALL)

logger = logging.getLogger(__name__)


def parse_staff_reply(text: str) -> tuple[int, str] | None:
    """A front-desk reply "#<ticket id> <answer>": (ticket id, answer stripped), else None."""
    m = _STAFF_REPLY.match(text)
    if m is None:
        return None
    try:
        ticket_id = int(m.group(1))
    except ValueError:  # more digits than int() converts: not a ticket number
        return None
    return ticket_id, m.group(2).strip()


class EmptyReplyError(RuntimeError):
    """The agent's turn produced no outbound text."""


class FrontDeskIntakeError(ValueError):
    """start_intake for the front desk number (STAFF_PHONE), which is not a patient."""


class ProactiveHandoffError(RuntimeError):
    """A handoff tool ran during a proactive reply: a proactive turn must not change agents."""


class UnrecoverableSessionError(RuntimeError):
    """The session reported an unrecoverable error during the turn; the first is the cause."""


# SDK-FINDING-8 (docs/findings.md): a failed reply does not always fail the run. When a
# tool hands off with `(agent, text)`, the old agent's reply runs on the same speech
# handle as the tool call, and its LLM error is stored only on that handle; RunResult
# raises only the error of the last speech handle to finish (the new agent's), so
# `run()` returns normally while the patient never got that reply. The session's
# `error` event does fire for it, so a turn watches that event instead.
class SessionErrors:
    """Collects the unrecoverable errors one session reports while a turn runs.

    Use around everything the turn runs in the session, then call `raise_if_any()`
    before reading the turn's outcome:

        with SessionErrors(session) as errors:
            await session.run(user_input=text)
        errors.raise_if_any()

    Errors reported as recoverable (an LLM attempt that the SDK retries) are ignored;
    if the retries fail too, the last attempt is reported as unrecoverable.
    """

    def __init__(self, session: AgentSession) -> None:
        self._session = session
        self.errors: list[BaseException] = []

    def __enter__(self) -> SessionErrors:
        self._session.on("error", self._on_error)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._session.off("error", self._on_error)

    def _on_error(self, ev: ErrorEvent) -> None:
        # an error type without the flag counts as unrecoverable
        if getattr(ev.error, "recoverable", False):
            return
        cause = getattr(ev.error, "error", None)
        if not isinstance(cause, BaseException):
            cause = RuntimeError(f"session error from {type(ev.error).__qualname__}")
        self.errors.append(cause)

    def raise_if_any(self) -> None:
        """Raise UnrecoverableSessionError, from the first error, if there was any.

        The message holds only the count: an error's text may hold the patient's words.
        """
        if self.errors:
            raise UnrecoverableSessionError(
                f"{len(self.errors)} unrecoverable session error(s)"
            ) from self.errors[0]


class HandoffWatch:
    """Notes whether a handoff tool ran in a session, from the SDK's own event.

    Use as a context manager around the turn, like SessionErrors.
    """

    def __init__(self, session: AgentSession) -> None:
        self._session = session
        self.happened = False

    def __enter__(self) -> HandoffWatch:
        self._session.on("function_tools_executed", self._on_tools)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._session.off("function_tools_executed", self._on_tools)

    def _on_tools(self, ev: FunctionToolsExecutedEvent) -> None:
        if ev.has_agent_handoff:
            self.happened = True


class ConversationRunner:
    """Runs each turn in a new AgentSession built from the stored state.

    Turns for one phone number run one at a time. The stored active agent is rebuilt
    through the agent registry, with a new `Turn` as the session's userdata. A turn's new
    state, its message log, its outbox rows and the effects its agents queued on the
    Turn are committed in one transaction; the outbox is delivered after the session is
    closed. A failed turn writes nothing: a failed inbound turn is answered with
    FALLBACK_TEXT instead, a failed proactive turn only raises.

    Failures are logged with `describe_exception`: type name and traceback, never the
    exception's text, which may hold the patient's words.

    `run_listener` (for evals and tests) is called with (phone, RunResult) after each
    inbound turn's `session.run()` has succeeded, before the turn commits; never for
    `start_intake` or a proactive turn. An exception from it fails the turn like any other.
    """

    def __init__(
        self,
        *,
        store: Store,
        channel: Channel,
        clock: Clock,
        llm_factory: Callable[[], llm.LLM],
        ready_timeout: float = 10.0,
        summarize_intake: bool = True,
        run_listener: Callable[[str, RunResult], None] | None = None,
    ) -> None:
        self._store = store
        self._channel = channel
        self._clock = clock
        self._llm_factory = llm_factory
        self._ready_timeout = ready_timeout
        self._summarize_intake = summarize_intake
        self._run_listener = run_listener
        self._locks: dict[str, asyncio.Lock] = {}
        install_sdk_log_redaction()

    async def start_intake(self, phone: str) -> list[str]:
        """Ask the intake's first question, unless the intake has already started.

        Returns the texts sent. On failure (including a stored state that no longer
        parses) only logs and returns []: the patient has not written, so there is
        nothing to answer with a fallback. Once the turn has committed it stands: a
        delivery failure is logged, the undelivered rows stay in the outbox for
        `deliver_pending`, and only the texts sent are returned.

        Raises FrontDeskIntakeError for STAFF_PHONE, before any LLM call or write. Any
        other number may start an intake, in the patients table or not.
        """
        if phone == STAFF_PHONE:
            raise FrontDeskIntakeError("the front desk number has no intake")
        async with self._lock(phone):
            try:
                loaded = self._store.load(phone)
                if loaded is not None and loaded[0].intake_started:
                    return []
                state, version = loaded or (ConversationState(phone=phone), None)
                # the intake takes over, starting from the conversation so far
                state = state.model_copy(
                    update={
                        "active": "intake",
                        "intake": IntakeState(root_ctx=state.agent_ctx),
                        "agent_ctx": None,
                        "intake_followups_sent": 0,  # follow-ups are counted per intake
                    }
                )
                rows = await self._run_turn(state, version, inbound=None)
            except Exception as e:
                logger.error(
                    "starting the intake for %s failed: %s",
                    mask_phone(phone),
                    describe_exception(e),
                )
                return []
            sent: list[str] = []
            try:
                await self._deliver(rows, sent)
            except Exception as e:
                logger.error(
                    "delivering the intake's first question for %s failed: %s",
                    mask_phone(phone),
                    describe_exception(e),
                )
            return sent

    async def handle_inbound(self, phone: str, text: str) -> list[str]:
        """Handle one inbound SMS and return the texts sent back. Never raises.

        A failed turn leaves the state as it was and is answered with FALLBACK_TEXT. If
        a committed turn's reply cannot be delivered, the turn stands, the undelivered
        rows stay in the outbox for `deliver_pending`, and only the texts sent are
        returned.

        A message from the front desk (STAFF_PHONE) answers a ticket instead: see
        `_handle_staff`. It runs under the front desk number's own lock.
        """
        if not text.strip():
            return []
        async with self._lock(phone):
            try:
                if phone == STAFF_PHONE:
                    rows = self._handle_staff(text)
                else:
                    rows = await self._answer(phone, text)
            except Exception as e:
                logger.error(
                    "inbound message for %s failed: %s", mask_phone(phone), describe_exception(e)
                )
                return await self._send_fallback(phone, text)
            sent: list[str] = []
            try:
                await self._deliver(rows, sent)
            except Exception as e:
                logger.error(
                    "delivering the reply for %s failed: %s",
                    mask_phone(phone),
                    describe_exception(e),
                )
            return sent

    async def run_proactive(self, phone: str, req: Proactive) -> list[str]:
        """Have the active agent send what `req.instructions` asks; return the texts sent.

        Runs like an inbound turn (the number's lock, a cold-started session), but the
        agent replies to `req.instructions` instead of a patient message, and
        `req.effects` commit with the turn. With no conversation yet, `req.may_start`
        starts one with the reception agent for a known patient; otherwise this raises
        NoConversationError, before any LLM call.

        Raises on any failure, with nothing written and nothing sent; never sends
        FALLBACK_TEXT, since the patient has not written. Once the turn has committed it
        stands: a delivery failure is logged, the undelivered rows stay in the outbox for
        `deliver_pending`, and only the texts sent are returned.
        """
        async with self._lock(phone):
            loaded = self._store.load(phone)
            if loaded is None or loaded[0].active_agent is None:
                patient = self._store.clinic.patient_by_phone(phone)
                if not req.may_start or patient is None:
                    raise NoConversationError("no conversation, and this message may not start one")
                state, version = self._start_reception(phone, loaded, patient)
            else:
                state, version = loaded
            rows = await self._run_turn(state, version, inbound=None, proactive=req)
            sent: list[str] = []
            try:
                await self._deliver(rows, sent)
            except Exception as e:
                logger.error(
                    "delivering the message for %s failed: %s",
                    mask_phone(phone),
                    describe_exception(e),
                )
            return sent

    async def deliver_pending(self) -> int:
        """Send every undelivered outbox row (e.g. after a crash); return how many.

        Meant for startup, before messages are handled.
        """
        rows = self._store.pending_outbox()
        await self._deliver(rows)
        return len(rows)

    def _lock(self, phone: str) -> asyncio.Lock:
        return self._locks.setdefault(phone, asyncio.Lock())

    async def _send_fallback(self, phone: str, inbound: str) -> list[str]:
        """Answer a failed inbound turn with FALLBACK_TEXT; return the texts sent; never raises.

        The fallback is logged and delivered through the outbox. Only if logging it fails
        is it sent directly, without the outbox. A logged fallback that cannot be
        delivered stays in the outbox for `deliver_pending`, like any committed message
        (commit first, deliver after); sending it directly as well would send the patient
        a second apology later.
        """
        try:
            rows = self._store.log_only(
                phone=phone, inbound=inbound, outbound=[FALLBACK_TEXT], at=self._clock.now()
            )
        except Exception as e:
            logger.error(
                "recording the fallback for %s failed: %s",
                mask_phone(phone),
                describe_exception(e),
            )
        else:
            sent: list[str] = []
            try:
                await self._deliver(rows, sent)
            except Exception as e:
                logger.error(
                    "delivering the fallback for %s failed: %s",
                    mask_phone(phone),
                    describe_exception(e),
                )
            return sent
        try:
            await self._channel.send(Outbound(to=phone, text=FALLBACK_TEXT))
        except Exception as e:
            logger.error(
                "sending the fallback for %s failed: %s", mask_phone(phone), describe_exception(e)
            )
            return []
        return [FALLBACK_TEXT]

    async def _answer(self, phone: str, text: str) -> list[OutboxRow]:
        """Run and commit the turn for `text`; raise on any failure.

        A stored state that no longer parses (pydantic hides the stored values in the
        error) fails the turn like any other error. With no active agent, a known
        patient's message starts reception; for any other number only the message and
        the fixed reply are logged.
        """
        loaded = self._store.load(phone)
        if loaded is None or loaded[0].active_agent is None:
            # no conversation yet: a known patient's first message starts reception
            patient = self._store.clinic.patient_by_phone(phone)
            if patient is None:
                return self._store.log_only(
                    phone=phone,
                    inbound=text,
                    outbound=[NO_CONVERSATION_TEXT],
                    at=self._clock.now(),
                )
            state, version = self._start_reception(phone, loaded, patient)
            return await self._run_turn(state, version, inbound=text)
        state, version = loaded
        return await self._run_turn(state, version, inbound=text)

    def _handle_staff(self, text: str) -> list[OutboxRow]:
        """Log a front-desk message and act on it; return the rows to send back.

        "#<ticket id> <answer>" for an open ticket records the answer and schedules a
        staff_relay job, due now, which passes it on to the patient; nothing is sent
        back. Anything else is answered with STAFF_USAGE_TEXT, or
        STAFF_UNKNOWN_TICKET_TEXT for a ticket that does not exist or is not open. No
        LLM call. Nothing here is logged: the text, the question and the answer stay in
        the database.
        """
        now = self._clock.now()
        parsed = parse_staff_reply(text)
        if parsed is None:
            return self._store.log_only(
                phone=STAFF_PHONE, inbound=text, outbound=[STAFF_USAGE_TEXT], at=now
            )
        ticket_id, answer = parsed
        try:
            ticket = self._store.clinic.ticket(ticket_id)
        except OverflowError:  # too large for an SQLite integer: not a ticket id
            ticket = None
        if ticket is None or ticket.status != "open":
            return self._store.log_only(
                phone=STAFF_PHONE,
                inbound=text,
                outbound=[STAFF_UNKNOWN_TICKET_TEXT.format(ticket_id=ticket_id)],
                at=now,
            )
        return self._store.log_only(
            phone=STAFF_PHONE,
            inbound=text,
            outbound=[],
            at=now,
            effects=[
                AnswerTicket(ticket_id, answer),
                ScheduleJob("staff_relay", ticket.phone, now, {"ticket_id": ticket_id}),
            ],
        )

    def _start_reception(
        self,
        phone: str,
        loaded: tuple[ConversationState, int] | None,
        patient: Patient,
    ) -> tuple[ConversationState, int | None]:
        """The state and version for a new conversation, with reception active.

        `loaded` is the stored row, if any, which has no active agent. An empty state row
        (stored by Plan 1) keeps its version, so the commit updates it; with no row, the
        commit inserts one.
        """
        state, version = loaded or (ConversationState(phone=phone), None)
        appointment = self._store.clinic.next_appointment(patient.id, self._clock.now())
        state = state.model_copy(
            update={
                "active": "reception",
                "appointment_id": None if appointment is None else appointment.id,
            }
        )
        return state, version

    async def _run_turn(
        self,
        state: ConversationState,
        version: int | None,
        *,
        inbound: str | None,
        proactive: Proactive | None = None,
    ) -> list[OutboxRow]:
        """Run one cold-started session and commit its turn; raise on any failure.

        `proactive`: the restored agent, once ready, replies to its instructions, and its
        effects commit with the turn. Otherwise `inbound` None: the restored agent speaks
        first (the intake's first step asks its question).
        """
        turn = self._new_turn(state)
        # raises ValueError on stored state the code no longer accepts, before any wait
        agent = registry.restore_agent(state, turn)
        session = AgentSession(llm=self._llm_factory(), userdata=turn)
        outbound: list[str] = []
        inbound_run: RunResult | None = None  # the inbound message's run, for run_listener

        def collect(ev: ConversationItemAddedEvent) -> None:
            # Every assistant message of the turn is its own SMS (spec §5.3 step 5).
            # Restored context items do not fire this event (docs/findings.md Spike 1
            # S4; the restart tests in tests/test_runner.py check it), so only this
            # session's messages are collected. The event also carries AgentHandoff items.
            item = ev.item
            if item.type == "message" and item.role == "assistant":
                text = item.text_content
                if text and text.strip():
                    outbound.append(text)

        session.on("conversation_item_added", collect)
        try:
            # SDK-FINDING-8: a reply that failed fails the turn even when run() returns
            with SessionErrors(session) as errors, HandoffWatch(session) as handoffs:
                if proactive is not None:
                    await self._start_until_ready(session, agent, turn)
                    handle = session.generate_reply(instructions=proactive.instructions)
                    # Spike 4 (H3e): awaiting the handle never raises; a failed reply
                    # shows up only as its exception(), with no outbound text
                    await asyncio.wait_for(handle, RUN_TIMEOUT_S)
                    if (error := handle.exception()) is not None:
                        raise error
                    if handoffs.happened:
                        # Proactive turns must not hand off: the next agent's opening would
                        # be cut off when the session closes.
                        raise ProactiveHandoffError("a handoff ran in a proactive reply")
                    if not outbound:
                        raise EmptyReplyError("the proactive turn produced no outbound text")
                elif inbound is None:
                    # SDK-FINDING-3: relies on this RunResult waiting for the first step's
                    # opening question (undocumented); if it ends early, the turn has no
                    # outbound text and fails below
                    run = await asyncio.wait_for(
                        session.start(agent, capture_run=True), RUN_TIMEOUT_S
                    )
                    await asyncio.wait_for(run, RUN_TIMEOUT_S)
                else:
                    await self._start_until_ready(session, agent, turn)
                    inbound_run = await asyncio.wait_for(
                        session.run(user_input=inbound), RUN_TIMEOUT_S
                    )
            errors.raise_if_any()
            if not outbound:
                raise EmptyReplyError("the turn produced no outbound text")
            if inbound_run is not None and self._run_listener is not None:
                self._run_listener(state.phone, inbound_run)
            # looked up on the module at call time, like restore_agent
            new_state = registry.conversation_snapshot(state, turn)
            if proactive is not None and proactive.followup:
                new_state = new_state.model_copy(
                    update={"intake_followups_sent": new_state.intake_followups_sent + 1}
                )
            effects = [
                *turn.effects,
                *(() if proactive is None else proactive.effects),
                *followup_effects(
                    new_state, now=self._clock.now(), patient_replied=inbound is not None
                ),
            ]
            return self._store.commit_turn(
                phone=state.phone,
                expected_version=version,
                state=new_state,
                inbound=inbound,
                outbound=outbound,
                at=self._clock.now(),
                effects=effects,
            )
        finally:
            # Usually an agent is still waiting for the next answer here (an intake
            # step); mark_closing() makes IntakeAgent treat its cancellation as a normal
            # exit (docs/findings.md FINDING-4). Told to the restored agent and to the one
            # that took over after any handoff. A close error never overrides the turn's
            # outcome: after a commit, the turn stands and its outbox is delivered.
            for closing in (agent, turn.active):
                mark_closing = getattr(closing, "mark_closing", None)
                if mark_closing is not None:
                    mark_closing()
            try:
                await asyncio.wait_for(session.aclose(), CLOSE_TIMEOUT_S)
            except TimeoutError:
                logger.warning("closing the session for %s timed out", mask_phone(state.phone))
            except Exception as e:
                logger.error(
                    "closing the session for %s failed: %s",
                    mask_phone(state.phone),
                    describe_exception(e),
                )

    async def _start_until_ready(self, session: AgentSession, agent: Agent, turn: Turn) -> None:
        """Start `session` with the restored `agent` and wait until an agent is ready.

        Ready: the agent that answers next is waiting (an intake step, once asked).
        """
        await asyncio.wait_for(session.start(agent), self._ready_timeout)
        await turn.ready.wait(self._ready_timeout)

    def _new_turn(self, state: ConversationState) -> Turn:
        """A new Turn, with a new one-shot ReadySignal, for one cold-started session.

        The appointment is the one in the stored state, else the patient's next one
        after now. `intake` is a copy: the stored intake state, whatever the restored
        IntakeAgent then does to its own.
        """
        clinic = self._store.clinic
        patient = clinic.patient_by_phone(state.phone)
        appointment_id = state.appointment_id
        if appointment_id is None and patient is not None:
            appointment = clinic.next_appointment(patient.id, self._clock.now())
            appointment_id = None if appointment is None else appointment.id
        return Turn(
            phone=state.phone,
            clock=self._clock,
            clinic=clinic,
            ready=ReadySignal(),
            intake=state.intake.model_copy(deep=True),
            appointment_id=appointment_id,
            patient_label=None if patient is None else patient.label,
            summarize_intake=self._summarize_intake,
            agent_factory=registry.new_agent,
        )

    async def _deliver(self, rows: list[OutboxRow], sent: list[str] | None = None) -> list[str]:
        """Send `rows` in order, marking each delivered; return their texts.

        Raises on the first failure: that row and the later ones stay in the outbox for
        `deliver_pending`. `sent`, if given, collects each text once it is sent.
        """
        sent = [] if sent is None else sent
        for row in rows:
            await self._channel.send(Outbound(to=row.phone, text=row.text))
            sent.append(row.text)
            self._store.mark_delivered(row.id, self._clock.now())
        return sent
