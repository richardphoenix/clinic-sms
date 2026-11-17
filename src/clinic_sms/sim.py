"""Simulator core: simulated clock, due jobs, command execution, script replay with restart resume.

cli.py is a thin shell over this module: it parses input lines into Commands, owns the
process-level concerns (input thread, os.execv, logging) and prints what `out` receives.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from clinic_sms.clinic_db import STAFF_PHONE, Appointment
from clinic_sms.clock import CLINIC_TZ, SimClock
from clinic_sms.fmt import format_phone, format_when, normalize_phone
from clinic_sms.jobs import Scheduler
from clinic_sms.logsafe import describe_exception
from clinic_sms.runner import FALLBACK_TEXT, ConversationRunner, FrontDeskIntakeError
from clinic_sms.state import ConversationState
from clinic_sms.store import Store

if TYPE_CHECKING:
    from clinic_sms.cli import Command

logger = logging.getLogger(__name__)

SIM_START = datetime(2026, 11, 17, 9, 55, tzinfo=CLINIC_TZ)
START_INTAKE_NOTE = "note: intake already started, or it failed — see stderr"
FRONT_DESK_INTAKE_TEXT = (
    "error: the front desk has no intake; switch to a patient first, e.g. /as (555) 010-0001"
)
NO_CONVERSATION_LINE = "no conversation for this number"
TOO_FAR_TEXT = "error: that is too far ahead"
HELP_TEXT = f"""\
Type a message to send it from the current number. Commands:
  /as PHONE        switch to another number, e.g. /as (555) 010-0002, or to the front
                   desk with /as {format_phone(STAFF_PHONE)}
  /start-intake    have the clinic start the intake for the current patient
  /state [PHONE]   show the conversation: active agent, appointment, intake, jobs, tickets
  /advance DUR     move the simulated clock forward, e.g. /advance 1h30m (units d, h, m);
                   each job that falls due on the way runs at its due time
  /time            show the simulated time
  /replay FILE     run a script file, one line at a time
  /restart         re-launch this process; stored conversations carry over
  /quit            exit
  /help            show this help
As the front desk, answer a ticket with #<ticket> <answer>, e.g. #1 Your copay was $25.
The patient's active agent passes the answer on."""

_SETTING_CLOCK = "sim_now"
_SETTING_FILE = "replay_file"
_SETTING_NEXT = "replay_next"
_SETTING_EXIT = "replay_exit"

_DURATION_RE = re.compile(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?")

Outcome = Literal["continue", "quit", "restart"]


def parse_duration(text: str) -> timedelta:
    """Parse "30m", "14h", "2d" or a combination like "1h30m". Must be positive.

    Raises ValueError for anything else, OverflowError if it is too long for a timedelta.
    """
    m = _DURATION_RE.fullmatch(text.strip())
    if m is None or not any(m.groups()):
        raise ValueError("not a duration")
    days, hours, minutes = (int(g) if g else 0 for g in m.groups())
    delta = timedelta(days=days, hours=hours, minutes=minutes)
    if delta <= timedelta(0):
        raise ValueError("duration must be positive")
    return delta


def is_comment(line: str) -> bool:
    """A "#" followed by a space or the end of the line. "#12 text" is a message."""
    return line == "#" or line.startswith("# ")


def load_clock(store: Store) -> SimClock:
    saved = store.get_setting(_SETTING_CLOCK)
    if saved is None:
        clock = SimClock(SIM_START)
        save_clock(store, clock)
        return clock
    return SimClock(datetime.fromisoformat(saved).astimezone(CLINIC_TZ))


def save_clock(store: Store, clock: SimClock) -> None:
    store.set_setting(_SETTING_CLOCK, clock.now().astimezone(timezone.utc).isoformat())


class Simulator:
    def __init__(
        self,
        *,
        store: Store,
        clock: SimClock,
        runner: ConversationRunner,
        scheduler: Scheduler,
        phone: str,
        out: Callable[[str], None],
        replay_mismatch: Callable[[], str | None] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._runner = runner
        self._scheduler = scheduler
        self._out = out
        self._replay_mismatch = replay_mismatch
        self.phone = phone
        self._replaying = False

    def prompt(self) -> str:
        return f"[{format_when(self._clock.now())}] {format_phone(self.phone)}> "

    async def startup(self) -> None:
        """Deliver messages left in the outbox, then run the jobs already due.

        Failures are logged, never raised.
        """
        try:
            await self._runner.deliver_pending()
        except Exception as e:  # noqa: BLE001 - the simulator must still start
            logger.error("delivering pending messages at startup failed: %s", type(e).__qualname__)
        await self._run_due()
        self._check_replay()

    def _check_replay(self) -> None:
        """Stop the process, loudly, once an LLM replay has gone off the recording."""
        if self._replay_mismatch is not None and (problem := self._replay_mismatch()):
            self._out(f"REPLAY MISMATCH: {problem}")
            raise SystemExit(2)

    async def advance(self, delta: timedelta) -> None:
        """Move the clock forward by `delta`, running each job that falls due on the way.

        The clock stops at each due time (never moving back) and is saved before the jobs
        due by then run, so each message carries its job's due time. A job that keeps
        failing runs at most MAX_JOB_ATTEMPTS times, then is failed and no longer due.
        Raises OverflowError, before changing anything, if the target is past the last
        date a datetime can hold.
        """
        target = self._clock.now() + delta
        while (job := self._scheduler.next_due(target)) is not None:
            if job.due_at > self._clock.now():
                self._clock.advance(job.due_at - self._clock.now())
                save_clock(self._store, self._clock)
            if not await self._run_due():
                break  # it raised, or ran nothing: never spin on a job that cannot run
        self._clock.advance(target - self._clock.now())
        save_clock(self._store, self._clock)

    def smoke_summary(self) -> tuple[bool, str]:
        fallbacks = self._store.outbound_count(FALLBACK_TEXT)
        failed_jobs = self._store.clinic.failed_job_count()
        ok = fallbacks == 0 and failed_jobs == 0
        verdict = "OK" if ok else "FAIL"
        return ok, f"SMOKE: {verdict} (fallbacks={fallbacks}, failed_jobs={failed_jobs})"

    async def execute(self, cmd: Command) -> Outcome:
        """Run one command, then the jobs due by now (a command may have made one due)."""
        outcome = await self._execute(cmd)
        await self._run_due()
        self._check_replay()
        return outcome

    async def _run_due(self) -> int:
        """Run the due jobs; return how many ran. A failure is logged, never raised: 0."""
        try:
            return await self._scheduler.run_due()
        except Exception as e:  # noqa: BLE001 - the command loop must go on
            logger.error("running due jobs failed: %s", describe_exception(e))
            return 0

    async def _execute(self, cmd: Command) -> Outcome:
        from clinic_sms.cli import format_message_line  # cli imports this module

        name = cmd.name
        if name == "say":
            if cmd.arg:
                self._out(
                    format_message_line(
                        at=self._clock.now(), direction="in", phone=self.phone, text=cmd.arg
                    )
                )
                await self._runner.handle_inbound(self.phone, cmd.arg)
        elif name == "as":
            try:
                self.phone = normalize_phone(cmd.arg)
            except ValueError:
                # The raw input is deliberately not echoed or logged.
                self._out("error: not a US phone number, e.g. /as (555) 010-0002")
            else:
                self._out(f"now texting as {format_phone(self.phone)}")
        elif name == "start-intake":
            try:
                sent = await self._runner.start_intake(self.phone)
            except FrontDeskIntakeError:
                self._out(FRONT_DESK_INTAKE_TEXT)
            else:
                if not sent:
                    self._out(START_INTAKE_NOTE)
        elif name == "state":
            self._print_state(cmd.arg)
        elif name == "advance":
            await self._advance_command(cmd.arg)
        elif name == "time":
            self._out(format_when(self._clock.now()))
        elif name == "replay":
            return await self._start_replay(cmd.arg, exit_after=False)
        elif name == "restart":
            return "restart"
        elif name == "quit":
            return "quit"
        else:
            self._out(HELP_TEXT)
        return "continue"

    async def _advance_command(self, arg: str) -> None:
        try:
            delta = parse_duration(arg)
        except ValueError:
            self._out("error: not a duration, e.g. /advance 30m, 14h, 2d or 1h30m")
            return
        except OverflowError:
            self._out(TOO_FAR_TEXT)
            return
        try:
            await self.advance(delta)
        except OverflowError:  # raised before anything changed
            self._out(TOO_FAR_TEXT)
            return
        self._out(f"time is now {format_when(self._clock.now())}")

    async def run_script(self, path: str) -> Outcome:
        """The --script mode: replay `path`, then print the smoke summary and exit."""
        outcome = await self._start_replay(path, exit_after=True)
        if outcome == "continue":  # the replay could not start
            raise SystemExit(1)
        return outcome

    async def resume_replay(self) -> Outcome:
        """Continue a replay that an earlier process left unfinished, if there is one.

        Says so first: "resuming <file> at line <n>", n the next line to run, from 1.
        """
        file = self._store.get_setting(_SETTING_FILE)
        if file is None:
            return "continue"
        index = int(self._store.get_setting(_SETTING_NEXT) or "0")
        self._out(f"resuming {file} at line {index + 1}")
        return await self._run_replay()

    async def _start_replay(self, path: str, *, exit_after: bool) -> Outcome:
        if self._replaying or self._store.get_setting(_SETTING_FILE) is not None:
            self._out("error: a replay is already running")
            return "continue"
        if not path:
            self._out("error: usage: /replay FILE")
            return "continue"
        try:
            Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            self._out("error: cannot read the replay file")
            return "continue"
        self._store.set_setting(_SETTING_FILE, str(Path(path).resolve()))
        self._store.set_setting(_SETTING_NEXT, "0")
        self._store.set_setting(_SETTING_EXIT, "1" if exit_after else "0")
        return await self._run_replay()

    async def _run_replay(self) -> Outcome:
        from clinic_sms.cli import parse_command

        file = self._store.get_setting(_SETTING_FILE)
        try:
            lines = Path(file or "").read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            self._out("error: cannot read the replay file")
            self._clear_replay()
            return "continue"
        index = int(self._store.get_setting(_SETTING_NEXT) or "0")
        exit_after = self._store.get_setting(_SETTING_EXIT) == "1"
        ended: Outcome = "continue"
        self._replaying = True
        try:
            while index < len(lines):
                line = lines[index].strip()
                index += 1
                # Saved before the line runs, so a /restart resumes at the next line.
                self._store.set_setting(_SETTING_NEXT, str(index))
                if not line or is_comment(line):
                    continue
                self._out(self.prompt() + line)
                outcome = await self.execute(parse_command(line))
                if outcome == "restart":
                    return "restart"
                if outcome == "quit":
                    ended = "quit"
                    break
        finally:
            self._replaying = False
        self._clear_replay()
        if not exit_after:
            return ended
        # a script ends here, at its last line or at a /quit
        ok, text = self.smoke_summary()
        self._out(text)
        if not ok:
            raise SystemExit(1)
        return "quit"

    def _clear_replay(self) -> None:
        for key in (_SETTING_FILE, _SETTING_NEXT, _SETTING_EXIT):
            self._store.set_setting(key, None)

    def _print_state(self, arg: str) -> None:
        phone = self.phone
        if arg:
            try:
                phone = normalize_phone(arg)
            except ValueError:
                self._out("error: not a US phone number, e.g. /state (555) 010-0002")
                return
        try:
            loaded = self._store.load(phone)
        except ValueError:  # pydantic.ValidationError: e.g. stored by an older version
            self._out("error: the stored state for this number does not parse")
            return
        # With no conversation yet, the number's appointment, jobs and tickets still show
        # (e.g. a reminder that has not gone out), followed by a line that says so.
        state = ConversationState(phone=phone) if loaded is None else loaded[0]
        intake = state.intake
        appointment = self._appointment_of(state)
        clinic = self._store.clinic
        view = {
            "active": state.active_agent,
            "appointment": None
            if appointment is None
            else {
                "when": format_when(appointment.start_at),
                "provider": appointment.provider,
                "status": appointment.status,
            },
            "intake": {
                "done": intake.done,
                "progress": intake.progress.model_dump(mode="json") if intake.progress else None,
                "results": intake.results,
            },
            "jobs": [
                {"kind": job.kind, "due": format_when(job.due_at), "status": job.status}
                for job in clinic.jobs(phone)
                if job.status == "pending"
            ],
            "tickets": [{"id": t.id, "status": t.status} for t in clinic.tickets(phone)],
        }
        self._out(json.dumps(view, indent=2, ensure_ascii=False))
        if loaded is None:
            self._out(NO_CONVERSATION_LINE)

    def _appointment_of(self, state: ConversationState) -> Appointment | None:
        """The conversation's appointment, as a turn finds it.

        The one in the stored state, else the patient's next one.
        """
        clinic = self._store.clinic
        if state.appointment_id is not None:
            return clinic.appointment(state.appointment_id)
        patient = clinic.patient_by_phone(state.phone)
        return None if patient is None else clinic.next_appointment(patient.id, self._clock.now())
