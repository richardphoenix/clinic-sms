"""Behavior eval of the demo main line (scenarios/mainline.txt, spec §2.1) with real Claude.

The script runs as tests/test_mainline_scripted.py runs it: line by line through
`Simulator.execute`, with "/restart" done in process (a new Store, runner, Scheduler and
Simulator over the same database), since pytest cannot `execv`. Every agent call goes to
Claude; with the judges, a run makes about 35 calls.

Two kinds of checks. Structure, through `RunResult.expect` and the database: tool calls,
handoffs, message times, the ticket, the saved intake. Wording, through LLM judges. Free
text the model chooses is never compared exactly. Every check runs and prints PASS or
FAIL, every judge's verdict and reasoning is printed, and the test fails at the end if
any check failed.

Run: uv run pytest evals -s     (ANTHROPIC_API_KEY in the environment)
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from livekit.agents import RunResult, llm
from livekit.agents.evals import JudgmentResult, task_completion_judge
from livekit.agents.llm import AgentConfigUpdate
from livekit.agents.voice import run_result

from clinic_sms.agents import InsuranceTask, IntakeAgent, RescheduleAgent, VisitReasonTask
from clinic_sms.channel import RecordingChannel
from clinic_sms.cli import parse_command
from clinic_sms.clinic_db import STAFF_PHONE
from clinic_sms.clock import CLINIC_TZ
from clinic_sms.fmt import format_when
from clinic_sms.jobs import Scheduler
from clinic_sms.llm import make_claude_llm, make_judge_llm
from clinic_sms.runner import FALLBACK_TEXT, ConversationRunner
from clinic_sms.seed import JANE_PHONE, seed_demo
from clinic_sms.sim import Simulator, is_comment, load_clock
from clinic_sms.state import ConversationState
from clinic_sms.store import MessageRow, Store
from clinic_sms.testing import lookup_key

MAINLINE = Path(__file__).resolve().parent.parent / "scenarios" / "mainline.txt"

# lines of scenarios/mainline.txt the checks refer to
REMINDER_DUE = "/advance 5m"
ASK_TO_MOVE = "2, can I move it to next week?"
PICK_FIRST = "The first one works"
INSURANCE_ANSWER = "Aetna, member W123456789"
VISIT_REASON_ANSWER = "Follow-up on my knee"
RESTART = "/restart"
FOLLOWUP_DUE = "/advance 15h"
WRONG_ID = "Actually my member ID is wrong, it's W999"
NEW_ID = "Aetna, W999"
BILLING_QUESTION = "Also, why was I charged $40 last time?"
STAFF_ANSWER = "#1 That was your copay from your 10/2 visit. Nothing else is due."
LAST_ANSWER = "No allergies, and I take ibuprofen daily"
PATIENT_LINES = (
    ASK_TO_MOVE,
    PICK_FIRST,
    INSURANCE_ANSWER,
    VISIT_REASON_ANSWER,
    WRONG_ID,
    NEW_ID,
    BILLING_QUESTION,
    LAST_ANSWER,
)

TUE_10AM = datetime(2026, 11, 17, 10, 0, tzinfo=CLINIC_TZ)
WED_8AM = datetime(2026, 11, 18, 8, 0, tzinfo=CLINIC_TZ)
STAFF_PREFIX = "[#1] Jane D. (555) 010-0001:"

# judge intents (Task 11 brief)
REMINDER_INTENT = (
    "reminds of Thu 11/19 10:00 AM with Dr. Lee and offers 1 to confirm, 2 to reschedule"
)
OFFER_INTENT = "offers specific open times next week, without slot ids"
FOLLOWUP_INTENT = "asks only about allergies and medications"
RELAY_INTENT = (
    "passes on that the $40 was a copay and nothing else is due, then asks about "
    "allergies/medications"
)

_SLOT_LINE = re.compile(r"^slot (\d+): ", re.MULTILINE)


@dataclass
class World:
    """What cli._run builds over one database: one process."""

    store: Store
    sim: Simulator


@dataclass
class LineEffect:
    """What one script line did."""

    calls: list[str]  # key of each agent LLM call, as ScriptedLLM keys it
    jane_out: list[MessageRow]  # the messages Jane was sent
    state: ConversationState | None  # Jane's stored state afterwards


class Runs:
    """The run listener: each inbound turn's RunResult, by the script line that sent it."""

    def __init__(self) -> None:
        self.by_line: dict[str, list[RunResult]] = {}
        self.line = ""

    def __call__(self, phone: str, run: RunResult) -> None:
        self.by_line.setdefault(self.line, []).append(run)


class Checks:
    """Runs every check, prints PASS/FAIL for each, and remembers the failures."""

    def __init__(self) -> None:
        self.failed: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))
        if not ok:
            self.failed.append(name)
        return ok

    def run(self, name: str, fn: Callable[[], object]) -> bool:
        """A check that raises on failure (RunResult.expect, a lookup), not one that returns."""
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - every check runs, failures are reported
            return self.check(name, False, f"{type(e).__name__}: {e}")
        return self.check(name, True)

    async def judge(self, name: str, judgment: Awaitable[JudgmentResult]) -> None:
        try:
            result = await judgment
        except Exception as e:  # noqa: BLE001 - e.g. "LLM did not return verdict arguments"
            self.check(f"judge: {name}", False, f"{type(e).__name__}: {e}")
            return
        print(f"[judge] {name}: verdict={result.verdict}; reasoning: {result.reasoning}")
        self.check(f"judge: {name}", result.passed, f"verdict {result.verdict}")


def call_key(ctx: llm.ChatContext) -> str:
    try:
        return lookup_key(ctx)
    except ValueError:  # e.g. a context ending in an assistant message
        return "<no key>"


def logged_claude(calls: list[str]) -> llm.LLM:
    """make_claude_llm(), noting each call's key in `calls`; the call itself is unchanged."""
    claude = make_claude_llm()
    chat = claude.chat

    def logged_chat(**kwargs: Any) -> llm.LLMStream:
        calls.append(call_key(kwargs["chat_ctx"]))
        return chat(**kwargs)

    claude.chat = logged_chat  # type: ignore[method-assign]
    return claude


def build_world(db: Path, ch: RecordingChannel, calls: list[str], runs: Runs) -> World:
    store = Store(db)
    clock = load_clock(store)
    seed_demo(store, now=clock.now())  # writes nothing once seeded
    runner = ConversationRunner(
        store=store,
        channel=ch,
        clock=clock,
        llm_factory=lambda: logged_claude(calls),
        summarize_intake=True,
        run_listener=runs,
    )
    scheduler = Scheduler(store=store, runner=runner, clock=clock)
    sim = Simulator(
        store=store,
        clock=clock,
        runner=runner,
        scheduler=scheduler,
        phone=JANE_PHONE,
        out=print,
    )
    return World(store, sim)


def script_lines() -> list[str]:
    lines = [ln.strip() for ln in MAINLINE.read_text(encoding="utf-8").splitlines()]
    return [ln for ln in lines if ln and not is_comment(ln)]


async def run_mainline(
    db: Path, ch: RecordingChannel, calls: list[str], runs: Runs
) -> tuple[World, dict[str, LineEffect]]:
    """Run the script; return the last world (its store still open) and each line's effect."""
    world = build_world(db, ch, calls, runs)
    try:
        await world.sim.startup()
        effects: dict[str, LineEffect] = {}
        for line in script_lines():
            first_call, first_message = len(calls), len(world.store.messages(JANE_PHONE))
            runs.line = line
            print(world.sim.prompt() + line)
            if await world.sim.execute(parse_command(line)) == "restart":
                phone = world.sim.phone
                world.store.close()
                world = build_world(db, ch, calls, runs)
                world.sim.phone = phone
                await world.sim.startup()
            loaded = world.store.load(JANE_PHONE)
            sent = world.store.messages(JANE_PHONE)[first_message:]
            effects[line] = LineEffect(
                calls=calls[first_call:],
                jane_out=[m for m in sent if m.direction == "out"],
                state=None if loaded is None else loaded[0],
            )
    except BaseException:
        world.store.close()
        raise
    return world, effects


def mentions(text: str, at: datetime) -> bool:
    """Whether an SMS names this day and hour, e.g. "Mon"/"Monday"/"11/23" and "9"/"9:00"."""
    local = at.astimezone(CLINIC_TZ)
    hour = local.hour % 12 or 12
    day = rf"\b(?:{local:%a}\w*|{local.month}/{local.day})\b"
    clock = rf"(?<![\d/:]){hour}(?::{local:%M})?(?![\d/])"
    return bool(re.search(day, text, re.IGNORECASE) and re.search(clock, text))


def offered_slot_ids(run: RunResult) -> set[int]:
    """The ids of the slots list_open_slots returned in this run."""
    return {
        int(m.group(1))
        for ev in run.events
        if ev.type == "function_call_output" and ev.item.name == "list_open_slots"
        for m in _SLOT_LINE.finditer(ev.item.output)
    }


def offer_index(run: RunResult) -> int:
    """The index of the reschedule reply: the last assistant message after the handoff."""
    handoffs = [
        i
        for i, ev in enumerate(run.events)
        if ev.type == "agent_handoff" and isinstance(ev.new_agent, RescheduleAgent)
    ]
    assert handoffs, "no handoff to RescheduleAgent"
    replies = [
        i
        for i, ev in enumerate(run.events)
        if i > handoffs[0] and ev.type == "message" and ev.item.role == "assistant"
    ]
    assert replies, "no assistant message after the handoff to RescheduleAgent"
    return replies[-1]


def sms_ctx(texts: list[str], intent: str) -> llm.ChatContext:
    """One or more SMS the clinic sent, with the intent as the agent's instructions."""
    ctx = llm.ChatContext()
    ctx.items.append(AgentConfigUpdate(instructions=f"Text the patient a message that {intent}."))
    for text in texts:
        ctx.add_message(role="assistant", content=text)
    return ctx


def conversation_ctx(messages: list[MessageRow]) -> llm.ChatContext:
    ctx = llm.ChatContext()
    for m in messages:
        ctx.add_message(role="user" if m.direction == "in" else "assistant", content=m.text)
    return ctx


@pytest.mark.timeout(1200)
async def test_mainline_six_steps(tmp_path, monkeypatch):
    # the SDK's LIVEKIT_EVALS_VERBOSE switch, read once at import: a passing message judge
    # prints its reason, and each RunResult prints its events when first asserted on
    monkeypatch.setattr(run_result, "lk_evals_verbose", 1)
    lines = script_lines()
    assert len(set(lines)) == len(lines), "each script line is a key below"
    for line in (REMINDER_DUE, RESTART, FOLLOWUP_DUE, STAFF_ANSWER, *PATIENT_LINES):
        assert line in lines, line

    db, ch = tmp_path / "mainline.db", RecordingChannel()
    calls: list[str] = []
    runs = Runs()
    world, effects = await run_mainline(db, ch, calls, runs)
    store, clinic = world.store, world.store.clinic
    try:
        jane = store.messages(JANE_PHONE)
        print(f"\n--- conversation ({len(calls)} agent LLM calls; database {db}) ---")
        for m in jane:
            who = "Jane  " if m.direction == "in" else "clinic"
            print(f"[{format_when(m.at)}] {who}: {m.text}")
        print("--- checks ---")
        c = Checks()
        judge_llm = make_judge_llm()

        def run_of(line: str) -> RunResult:
            (run,) = runs.by_line[line]
            return run

        smoke_ok, smoke = world.sim.smoke_summary()
        c.check("no fallback SMS sent", FALLBACK_TEXT not in [m.text for m in ch.sent])
        c.check("smoke summary OK", smoke_ok, smoke)
        for line in PATIENT_LINES:
            c.check(f"one inbound run for {line!r}", len(runs.by_line.get(line, [])) == 1)

        # 1. the reminder at 10:00 AM
        reminder = effects[REMINDER_DUE].jane_out
        c.check(
            "1 reminder: one SMS, at Tue 10:00 AM",
            [m.at for m in reminder] == [TUE_10AM],
            f"sent at {[format_when(m.at) for m in reminder]}",
        )
        reminder_jobs = [
            j.status
            for j in clinic.jobs(JANE_PHONE)
            if j.kind == "reminder" and j.due_at == TUE_10AM
        ]
        c.check("1 reminder: its job is done", reminder_jobs == ["done"], f"{reminder_jobs}")
        await c.judge(
            "1 reminder",
            task_completion_judge(judge_llm).evaluate(
                chat_ctx=sms_ctx([m.text for m in reminder], REMINDER_INTENT)
            ),
        )

        # 2. handoff to reschedule, then the booking hands off to the intake
        c.run(
            "2 reschedule: transfer_to_reschedule called",
            lambda: run_of(ASK_TO_MOVE).expect.contains_function_call(
                name="transfer_to_reschedule"
            ),
        )
        c.run(
            "2 reschedule: handoff to RescheduleAgent",
            lambda: run_of(ASK_TO_MOVE).expect.contains_agent_handoff(
                new_agent_type=RescheduleAgent
            ),
        )
        c.run(
            "2 reschedule: book_slot called",
            lambda: run_of(PICK_FIRST).expect.contains_function_call(name="book_slot"),
        )
        c.run(
            "2 reschedule: handoff to IntakeAgent",
            lambda: run_of(PICK_FIRST).expect.contains_agent_handoff(new_agent_type=IntakeAgent),
        )
        patient = clinic.patient_by_phone(JANE_PHONE)
        appointment = clinic.next_appointment(patient.id, datetime(2026, 1, 1, tzinfo=CLINIC_TZ))
        booked = clinic.slot(appointment.slot_id)
        print(f"booked: {format_when(booked.start_at)} with {booked.provider} (slot {booked.id})")

        def booked_slot_was_offered() -> None:
            run = run_of(ASK_TO_MOVE)
            offered = offered_slot_ids(run)
            assert booked.id in offered, f"slot {booked.id} not in {sorted(offered)}"
            offer = run.events[offer_index(run)].item.text_content or ""
            assert mentions(offer, booked.start_at), "the offer does not name its day and hour"

        c.run("2 reschedule: the booked slot is one the reply offered", booked_slot_was_offered)

        async def judge_offer() -> None:
            run = run_of(ASK_TO_MOVE)
            await run.expect[offer_index(run)].is_message(role="assistant").judge(
                judge_llm, intent=OFFER_INTENT
            )

        try:
            await judge_offer()
        except Exception as e:  # noqa: BLE001 - e.g. AssertionError("Judgement failed: ...")
            c.check("judge: 2 reschedule offer", False, f"{type(e).__name__}: {e}")
        else:
            c.check("judge: 2 reschedule offer", True)

        # 3. the intake (TaskGroup): two answers, then silence
        after_two = effects[VISIT_REASON_ANSWER].state
        progress = None if after_two is None else after_two.intake.progress
        results = set() if progress is None else set(progress.results)
        c.check(
            "3 intake: insurance and visit_reason saved after two answers",
            {"insurance", "visit_reason"} <= results,
            f"results {sorted(results)}",
        )

        # 4. the follow-up the next morning, after a restart
        followup = effects[FOLLOWUP_DUE].jane_out
        c.check(
            "4 follow-up: one SMS, at Wed 8:00 AM",
            [m.at for m in followup] == [WED_8AM],
            f"sent at {[format_when(m.at) for m in followup]}",
        )
        openings = (InsuranceTask.OPENING, VisitReasonTask.OPENING)
        restart_calls = effects[RESTART].calls + effects[FOLLOWUP_DUE].calls
        c.check(
            "4 follow-up: no insurance or visit-reason opening from the restart to the follow-up",
            not any(k in openings for k in restart_calls),
            f"calls {restart_calls}",
        )
        after_restart = [k for line in lines[lines.index(RESTART) :] for k in effects[line].calls]
        c.check(
            "4 follow-up: the visit reason is never asked again after the restart",
            VisitReasonTask.OPENING not in after_restart,
        )
        await c.judge(
            "4 follow-up",
            task_completion_judge(judge_llm).evaluate(
                chat_ctx=sms_ctx([m.text for m in followup], FOLLOWUP_INTENT)
            ),
        )

        # 5. regression to the insurance step
        c.run(
            "5 regression: out_of_scope called",
            lambda: run_of(WRONG_ID).expect.contains_function_call(name="out_of_scope"),
        )
        after_new_id = effects[NEW_ID].state
        progress = None if after_new_id is None else after_new_id.intake.progress
        c.check(
            "5 regression: back to allergies_meds after the new member ID",
            progress is not None and progress.pending == ["allergies_meds"],
            f"pending {None if progress is None else progress.pending}",
        )

        # 6. the billing question goes to the front desk, whose answer is relayed
        c.run(
            "6 escalation: escalate_to_staff called",
            lambda: run_of(BILLING_QUESTION).expect.contains_function_call(
                name="escalate_to_staff"
            ),
        )
        staff = [m.text for m in store.messages(STAFF_PHONE) if m.direction == "out"]
        c.check(
            "6 escalation: one front-desk SMS, with the ticket and patient prefix",
            len(staff) == 1 and staff[0].startswith(STAFF_PREFIX),
            f"front desk got {staff}",
        )
        ticket = clinic.ticket(1)
        c.check(
            "6 escalation: ticket closed after the relay",
            ticket is not None and ticket.status == "closed",
            f"status {None if ticket is None else ticket.status}",
        )
        relay = effects[STAFF_ANSWER].jane_out
        c.check("6 escalation: the answer was relayed to Jane", len(relay) >= 1)
        await c.judge(
            "6 relay",
            task_completion_judge(judge_llm).evaluate(
                chat_ctx=sms_ctx([m.text for m in relay], RELAY_INTENT)
            ),
        )

        # the end (steps 5 and 6): the intake is done and saved on the appointment
        final = store.load(JANE_PHONE)[0]
        intake = clinic.appointment(appointment.id).intake or {}
        c.check(
            "6 end: intake done, reception active",
            (final.active, final.intake.done) == ("reception", True),
            f"active {final.active}, done {final.intake.done}",
        )
        c.check(
            "6 end: the appointment has the whole intake",
            set(intake) == {"insurance", "visit_reason", "allergies_meds"},
            f"keys {sorted(intake)}",
        )
        c.check(
            "5 end: the saved member_id is W999",
            intake.get("insurance", {}).get("member_id") == "W999",
        )
        medications = intake.get("allergies_meds", {}).get("medications", [])
        c.check(
            "6 end: the saved medications include ibuprofen",
            any("ibuprofen" in m.lower() for m in medications),
            json.dumps(medications),
        )

        # the whole conversation, as user and assistant messages
        await c.judge(
            "whole conversation (task completion)",
            task_completion_judge(judge_llm).evaluate(chat_ctx=conversation_ctx(jane)),
        )

        print(f"--- {len(c.failed)} failed: {c.failed}")
        assert not c.failed, c.failed
    finally:
        store.close()
