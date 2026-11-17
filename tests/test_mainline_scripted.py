"""The demo main line, scenarios/mainline.txt, with a scripted LLM (spec §2.1, §5.8).

The script is the demo, and run here, the regression test of the demo.
"""

import json
from datetime import datetime
from pathlib import Path

from clinic_sms.agents import InsuranceTask, VisitReasonTask
from clinic_sms.agents.common import ESCALATION_RULE, SMS_STYLE
from clinic_sms.channel import RecordingChannel
from clinic_sms.cli import parse_command
from clinic_sms.clinic_db import STAFF_PHONE
from clinic_sms.clock import CLINIC_TZ
from clinic_sms.runner import FALLBACK_TEXT
from clinic_sms.seed import JANE_PHONE
from clinic_sms.sim import is_comment
from clinic_sms.testing import tool_names
from conftest import MAINLINE_SMS as SMS
from conftest import load
from test_jobs import World, world  # noqa: F401

MAINLINE = Path(__file__).resolve().parent.parent / "scenarios" / "mainline.txt"
INSTRUCTIONS_ID = "lk.agent_task.instructions"  # the SDK's id for the agent's instructions
# a real-Claude run once texted a patient that it was calling a tool
WRITE_ONLY_THE_MESSAGE = (
    "Write only the text message itself; never mention tools, function calls or your own "
    "reasoning."
)


def at(day: int, hour: int) -> datetime:
    return datetime(2026, 11, day, hour, 0, tzinfo=CLINIC_TZ)


async def run_mainline(db: Path, llm, world) -> tuple[World, int]:
    """Run the script line by line, as `--script` does; return the last world and restarts.

    A "restart" closes the Store and builds a new Store, runner, Scheduler and Simulator
    over the same database and LLM: a process restart. One channel records every SMS.
    """
    ch = RecordingChannel()
    w = world(db, llm, ch=ch)
    await w.sim.startup()
    restarts = 0
    for raw in MAINLINE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or is_comment(line):
            continue
        if await w.sim.execute(parse_command(line)) == "restart":
            restarts += 1
            phone = w.sim.phone
            w.store.close()
            w = world(db, llm, ch=ch)
            w.sim.phone = phone
            await w.sim.startup()
    return w, restarts


async def test_mainline_script_with_scripted_llm(
    seeded_db, scripted_llm, mainline_replies, world, clinic, jane_appointment
):
    llm = scripted_llm(mainline_replies)
    w, restarts = await run_mainline(seeded_db, llm, world)
    assert restarts == 1

    def followup_message_times() -> list[datetime]:
        return [m.at for m in w.store.messages(JANE_PHONE) if m.text == SMS["followup"]]

    def staff_texts() -> list[str]:
        return [m.text for m in w.store.messages(STAFF_PHONE) if m.direction == "out"]

    assert FALLBACK_TEXT not in [m.text for m in w.ch.sent]
    appt = jane_appointment()
    assert (appt.start_at, appt.provider) == (
        datetime(2026, 11, 23, 9, 0, tzinfo=CLINIC_TZ),
        "Dr. Lee",
    )
    assert appt.intake["insurance"]["member_id"] == "W999"
    assert appt.intake["allergies_meds"]["medications"] == ["ibuprofen"]
    assert followup_message_times() == [datetime(2026, 11, 18, 8, 0, tzinfo=CLINIC_TZ)]
    assert clinic.ticket(1).status == "closed"
    assert staff_texts() == ["[#1] Jane D. (555) 010-0001: Why was I charged $40 last time?"]
    state = load(seeded_db)[0]
    assert (state.active, state.intake.done) == ("reception", True)
    assert [(j.kind, j.status) for j in clinic.jobs(JANE_PHONE) if j.status == "pending"] == [
        ("reminder", "pending")
    ]
    assert llm.calls.count(InsuranceTask.OPENING) == 2
    assert llm.calls.count(VisitReasonTask.OPENING) == 1

    # every message Jane got, at the time the demo shows it
    assert [(m.at, m.text) for m in w.store.messages(JANE_PHONE) if m.direction == "out"] == [
        (at(17, 10), SMS["reminder"]),
        (at(17, 17), SMS["offer"]),
        (at(17, 17), SMS["booked"]),
        (at(17, 17), SMS["insurance"]),
        (at(17, 17), SMS["visit_reason"]),
        (at(17, 17), SMS["allergies_meds"]),
        (at(18, 8), SMS["followup"]),
        (at(18, 8), SMS["insurance"]),
        (at(18, 8), SMS["allergies_meds"]),
        (at(18, 8), SMS["escalated"]),
        (at(18, 11), SMS["relayed"]),
        (at(18, 11), SMS["completion"]),
    ]
    view = json.loads(w.out[-1])  # /state, the script's last line
    assert (view["active"], view["intake"]["done"], view["tickets"]) == (
        "reception",
        True,
        [{"id": 1, "status": "closed"}],
    )


async def test_every_agent_is_told_to_write_only_the_text_message(
    seeded_db, scripted_llm, mainline_replies, world
):
    assert WRITE_ONLY_THE_MESSAGE in SMS_STYLE
    llm = scripted_llm(mainline_replies)
    await run_mainline(seeded_db, llm, world)
    seen: list[str] = []
    for key, ctx, tools in zip(llm.calls, llm.contexts, llm.tools, strict=True):
        (instructions,) = [i.text_content for i in ctx.items if i.id == INSTRUCTIONS_ID]
        assert WRITE_ONLY_THE_MESSAGE in instructions, key
        # an agent that has the escalation tool is told when to use it
        if "escalate_to_staff" in tool_names(tools):
            assert ESCALATION_RULE in instructions, key
        seen.append(instructions)
    # the main line reaches every agent: reception, reschedule, the intake agent and its steps
    for marker in (
        "You are the clinic's reception.",
        "You help the patient move their appointment",
        "The patient's intake for their appointment is handled here.",
        "Your topic: the patient's insurance",
        "Your topic: the reason for the patient's visit.",
        "Your topic: the patient's allergies",
    ):
        assert any(marker in s for s in seen), marker
