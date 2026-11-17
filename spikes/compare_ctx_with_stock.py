"""Spike 3: stock long-lived TaskGroup vs cold start per message (snapshot + restore).

Throwaway script, not maintained (design §5.9). Same intake script, run twice:

(a) one long-lived AgentSession. `StockRoot` (IntakeAgent's instructions and completion
    reply) awaits a stock `TaskGroup` whose steps are registered with `factory(False)`.
(b) a new AgentSession per message: `IntakeAgent(state=...)` from the previous
    `snapshot()` (JSON round trip, as the store keeps it), start, wait for the
    ReadySignal, run, `snapshot()`, close.

Both sides use ScriptedLLM with the tests' replies; no API key. The insurance answer
is split over two messages so that one cold start lands mid-step. Summaries are on
(the default on both sides); the summary call gets `default`, every other call is
scripted.

At the end of every turn the script prints, for (a) vs (b): the outbound texts, the
LLM calls (key, normalized context and the names of the tools offered), and the
normalize_ctx diff of the current step, the group and the root. normalize_ctx leaves
agent_config_update out (SDK-FINDING-5); their counts are printed separately.

Run: uv run python spikes/compare_ctx_with_stock.py
"""

from __future__ import annotations

import asyncio
import difflib
import sys

from livekit.agents import Agent, AgentSession, ConversationItemAddedEvent, llm
from livekit.agents.beta.workflows import TaskGroup
from livekit.agents.llm import ToolError

from clinic_sms.agents import (
    INTAKE_STEPS,
    AllergiesMedsTask,
    InsuranceTask,
    IntakeAgent,
    IntakeStep,
    VisitReasonTask,
)
from clinic_sms.ctx import normalize_ctx
from clinic_sms.state import IntakeState
from clinic_sms.testing import Reply, ScriptedLLM, tool_names
from clinic_sms.workflow import ReadySignal

T = 5.0
MESSAGES = ["Aetna", "W123456789", "Follow-up on my knee", "Penicillin; ibuprofen"]
SUMMARY = "Patient: Aetna W123456789; knee follow-up; allergic to penicillin; takes ibuprofen."


def make_llm() -> ScriptedLLM:
    replies = {
        # same as tests/conftest.py intake_replies (the parts this script uses)
        InsuranceTask.OPENING: Reply(text="Hi Jane! What's your insurance carrier and member ID?"),
        "Aetna": Reply(text="Got it. What's your member ID?"),
        "W123456789": Reply(
            tools=[("record_insurance", {"carrier": "Aetna", "member_id": "W123456789"})]
        ),
        VisitReasonTask.OPENING: Reply(text="Thanks! What brings you in?"),
        "Follow-up on my knee": Reply(
            tools=[("record_visit_reason", {"reason": "Follow-up on my knee"})]
        ),
        AllergiesMedsTask.OPENING: Reply(text="Any allergies or current medications?"),
        "Penicillin; ibuprofen": Reply(
            tools=[
                (
                    "record_allergies_and_medications",
                    {"allergies": ["penicillin"], "medications": ["ibuprofen"]},
                )
            ]
        ),
        IntakeAgent.COMPLETION: Reply(text="You're all set. See you Thursday!"),
    }
    # only the summary call is unscripted: the record_* tools return None, so there is
    # no LLM call after their output (voice/generation.py:1241)
    return ScriptedLLM(replies, default=Reply(text=SUMMARY))


class StockRoot(Agent):
    """IntakeAgent's role on the stock TaskGroup, for a session that never restarts."""

    def __init__(self, *, instructions: str, ready: ReadySignal) -> None:
        super().__init__(instructions=instructions)
        self.group = TaskGroup()
        for step_id, description, task_cls, _ in INTAKE_STEPS:
            self.group.add(
                lambda task_cls=task_cls: task_cls(resuming=False, ready=ready),
                id=step_id,
                description=description,
            )
        self.done = False
        self.closing = False  # set by the owner right before aclose(), as in intake.py

    async def on_enter(self) -> None:
        try:
            await self.group
        except ToolError:
            if self.closing:  # SDK-FINDING-4: the cancellation is a plain ToolError
                return
            raise
        self.done = True
        self.session.generate_reply(instructions=IntakeAgent.COMPLETION)


class Outbound:
    def __init__(self, session: AgentSession) -> None:
        self.texts: list[str] = []
        session.on("conversation_item_added", self._on_item)

    def _on_item(self, ev: ConversationItemAddedEvent) -> None:
        item = ev.item
        if item.type == "message" and item.role == "assistant" and item.text_content:
            self.texts.append(item.text_content)


class Turn:
    """What one side looked like at the end of one turn."""

    def __init__(
        self,
        *,
        outbound: list[str],
        calls: list[tuple[str, list[tuple], list[str]]],
        step: Agent | None,
        group: Agent,
        root: Agent,
    ) -> None:
        self.outbound = outbound
        self.calls = calls
        self.step_name = type(step).__name__ if step is not None else None
        self.layers = {
            "step": step.chat_ctx.copy() if step is not None else None,
            "group": group.chat_ctx.copy(),
            "root": root.chat_ctx.copy(),
        }


def calls_since(scripted: ScriptedLLM, start: int) -> list[tuple[str, list[tuple], list[str]]]:
    """(key, normalized context, names of the tools offered) of each call from `start` on."""
    return [
        (key, normalize_ctx(ctx), tool_names(tools))
        for key, ctx, tools in zip(
            scripted.calls[start:], scripted.contexts[start:], scripted.tools[start:], strict=True
        )
    ]


async def run_stock() -> list[Turn]:
    scripted = make_llm()
    ready = ReadySignal()
    # a done IntakeAgent builds no group; it is only read for its instructions
    root_instructions = IntakeAgent(state=IntakeState(done=True), ready=ReadySignal()).instructions
    root = StockRoot(instructions=root_instructions, ready=ready)
    session = AgentSession(llm=scripted)
    out = Outbound(session)
    turns: list[Turn] = []

    def snap(n_out: int, n_calls: int) -> Turn:
        current = session.current_agent
        step = current if isinstance(current, IntakeStep) else None
        return Turn(
            outbound=out.texts[n_out:],
            calls=calls_since(scripted, n_calls),
            step=step,
            group=root.group,
            root=root,
        )

    try:
        await asyncio.wait_for(session.start(root, capture_run=True), T)
        await ready.wait(T)
        turns.append(snap(0, 0))
        for text in MESSAGES:
            n_out, n_calls = len(out.texts), len(scripted.calls)
            await asyncio.wait_for(session.run(user_input=text), T)
            turns.append(snap(n_out, n_calls))
        assert root.done, "stock intake did not finish"
    finally:
        root.closing = True
        await asyncio.wait_for(session.aclose(), T)
    return turns


async def run_cold() -> tuple[list[Turn], IntakeState]:
    scripted = make_llm()
    state = IntakeState()
    turns: list[Turn] = []
    for text in [None, *MESSAGES]:
        ready = ReadySignal()
        agent = IntakeAgent(state=state, ready=ready)
        session = AgentSession(llm=scripted)
        out = Outbound(session)
        n_calls = len(scripted.calls)
        try:
            if text is None:
                await asyncio.wait_for(session.start(agent, capture_run=True), T)
                await ready.wait(T)
            else:
                await asyncio.wait_for(session.start(agent), T)
                await ready.wait(T)
                await asyncio.wait_for(session.run(user_input=text), T)
            assert agent.group is not None
            turns.append(
                Turn(
                    outbound=list(out.texts),
                    calls=calls_since(scripted, n_calls),
                    step=None if agent.state.done else agent.group.current_task,
                    group=agent.group,
                    root=agent,
                )
            )
            state = IntakeState.model_validate_json(agent.snapshot().model_dump_json())
        finally:
            agent.mark_closing()
            await asyncio.wait_for(session.aclose(), T)
    return turns, state


def diff_lines(a: list[tuple], b: list[tuple]) -> list[str]:
    return list(
        difflib.unified_diff(
            [repr(x) for x in a], [repr(x) for x in b], "(a) stock", "(b) cold", lineterm="", n=1
        )
    )[2:]


def config_updates(ctx: llm.ChatContext | None) -> int:
    return 0 if ctx is None else sum(1 for i in ctx.items if i.type == "agent_config_update")


async def main() -> int:
    stock = await run_stock()
    cold, final_state = await run_cold()
    problems: list[str] = []

    for i, (a, b) in enumerate(zip(stock, cold, strict=True)):
        label = "start" if i == 0 else f"user {MESSAGES[i - 1]!r}"
        print(f"=== turn {i}: {label}")

        same = a.outbound == b.outbound
        print(f"  outbound (a) {a.outbound}")
        print(f"  outbound (b) {b.outbound}  -> {'same' if same else 'DIFF'}")
        if not same:
            problems.append(f"turn {i} outbound")

        keys_a, keys_b = [k for k, _, _ in a.calls], [k for k, _, _ in b.calls]
        same = keys_a == keys_b
        print(f"  LLM keys (a) {[k[:40] for k in keys_a]}")
        print(f"  LLM keys (b) {[k[:40] for k in keys_b]}  -> {'same' if same else 'DIFF'}")
        if not same:
            problems.append(f"turn {i} LLM call keys")
        for n, ((_, ca, ta), (_, cb, tb)) in enumerate(zip(a.calls, b.calls, strict=False)):
            if ca != cb:
                problems.append(f"turn {i} LLM call {n} context")
                print(f"  LLM call {n} context DIFF:")
                for line in diff_lines(ca, cb):
                    print(f"    {line}")
            if ta != tb:
                problems.append(f"turn {i} LLM call {n} tools")
                print(f"  LLM call {n} tools DIFF: (a) {ta}  (b) {tb}")
        if a.calls and len(a.calls) == len(b.calls):
            print(f"  LLM call contexts: all {len(a.calls)} equal under normalize_ctx"
                  if all(ca == cb for (_, ca, _), (_, cb, _) in zip(a.calls, b.calls, strict=True))
                  else "  LLM call contexts: see DIFF above")
            print(f"  LLM call tools (a) {[t for _, _, t in a.calls]}")
            print(f"  LLM call tools (b) {[t for _, _, t in b.calls]}  -> "
                  f"{'same' if [t for *_, t in a.calls] == [t for *_, t in b.calls] else 'DIFF'}")

        if a.step_name != b.step_name:
            problems.append(f"turn {i} current step")
        print(f"  current step (a) {a.step_name}  (b) {b.step_name}")
        for layer in ("step", "group", "root"):
            ca, cb = a.layers[layer], b.layers[layer]
            if ca is None or cb is None:
                print(f"  {layer:5}: n/a")
                continue
            na, nb = normalize_ctx(ca), normalize_ctx(cb)
            counts = f"agent_config_update (a)={config_updates(ca)} (b)={config_updates(cb)}"
            if na == nb:
                print(f"  {layer:5}: EQUAL ({len(na)} items; {counts})")
            else:
                problems.append(f"turn {i} {layer}")
                print(f"  {layer:5}: DIFF ({counts})")
                for line in diff_lines(na, nb):
                    print(f"    {line}")

    print(f"=== (b) final snapshot: done={final_state.done} results={final_state.results}")
    if problems:
        print(f"SUMMARY: {len(problems)} difference(s) outside agent_config_update: {problems}")
        return 1
    print("SUMMARY: no difference outside agent_config_update (SDK-FINDING-5)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
