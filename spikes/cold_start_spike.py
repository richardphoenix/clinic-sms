"""Spike 1: cold start readiness (R1) and run() returning across nested tasks (R2).

Throwaway script, not maintained. Every "inbound SMS" gets a brand-new
AgentSession; the agent -> TaskGroup -> step chain is rebuilt each time.
Resuming "from a" is simulated with the stock TaskGroup by registering only the
steps that are not done yet, the first of them constructed with resuming=True.

Run: uv run python spikes/cold_start_spike.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable

from livekit.agents import (
    Agent,
    AgentSession,
    AgentTask,
    ConversationItemAddedEvent,
    RunContext,
    function_tool,
    llm,
)
from livekit.agents.beta.workflows import TaskGroup
from livekit.agents.llm import ToolError
from livekit.agents.types import NOT_GIVEN, NotGivenOr

from clinic_sms.testing import Reply, ScriptedLLM

TIMEOUT = 5.0
STEP_IDS = ["a", "b"]

results: dict[str, bool] = {}


def script() -> dict[str, Reply]:
    return {
        "ask a": Reply(text="What is A?"),
        "a1": Reply(tools=[("save", {"value": "a1"})]),
        "ask b": Reply(text="What is B?"),
    }


def make_llm(extra: dict[str, Reply] | None = None) -> ScriptedLLM:
    # default=Reply() so calls the SDK makes on its own (e.g. after a tool output)
    # are recorded in llm.calls instead of failing the run.
    return ScriptedLLM(script() | (extra or {}), default=Reply())


def check(name: str, ok: bool, observations: list[str]) -> None:
    results[name] = ok
    print(f"{name}: {'PASS' if ok else 'FAIL'}")
    for line in observations:
        print(f"  | {line}")


def describe(item: llm.ChatItem) -> str:
    if item.type == "message":
        return f"message/{item.role}: {item.text_content!r}"
    if item.type == "function_call":
        return f"function_call: {item.name}({item.arguments}) call_id={item.call_id}"
    if item.type == "function_call_output":
        return f"function_call_output: {item.output!r} call_id={item.call_id}"
    if item.type == "agent_handoff":
        return f"agent_handoff: {item.old_agent_id} -> {item.new_agent_id}"
    return f"{item.type}"


def call_log(scripted: ScriptedLLM) -> list[str]:
    """Every chat() call as `key <- instructions of the agent that made it`."""
    out = []
    for key, ctx in zip(scripted.calls, scripted.contexts, strict=True):
        first = ctx.items[0] if ctx.items else None
        who = (
            first.text_content
            if first is not None and first.type == "message" and first.role == "system"
            else "<no instructions message>"
        )
        out.append(f"{key!r} <- {who!r}")
    return out


def replayed_items(probe: Probe, restored: llm.ChatContext) -> list[str]:
    """Restored (old) items that came back through conversation_item_added."""
    old_ids = {i.id for i in restored.items}
    old_texts = {i.text_content for i in restored.items if i.type == "message"}
    return [
        describe(i)
        for i in probe.items
        if i.id in old_ids or (i.type == "message" and i.text_content in old_texts)
    ]


class Probe:
    """Collects conversation_item_added events of one session."""

    def __init__(self, session: AgentSession) -> None:
        self.items: list[llm.ChatItem] = []
        session.on("conversation_item_added", self._on_item)

    def _on_item(self, ev: ConversationItemAddedEvent) -> None:
        self.items.append(ev.item)

    def outbound(self) -> list[str]:
        return [
            i.text_content
            for i in self.items
            if i.type == "message" and i.role == "assistant" and i.text_content
        ]


class Step(AgentTask[str]):
    def __init__(
        self,
        step_id: str,
        *,
        resuming: bool,
        entered: asyncio.Event,
        enter_log: list[str],
        chat_ctx: NotGivenOr[llm.ChatContext] = NOT_GIVEN,
    ) -> None:
        super().__init__(instructions=f"step {step_id}", chat_ctx=chat_ctx)
        self.step_id = step_id
        self.resuming = resuming
        self._entered = entered
        self._enter_log = enter_log

    async def on_enter(self) -> None:
        # Spike-only peek at private state: is this step's activity the session's
        # current activity at the moment on_enter runs?
        session = self.session
        texts = [i.text_content for i in self.chat_ctx.items if i.type == "message"]
        self._enter_log.append(
            f"{self.step_id}.on_enter(resuming={self.resuming}): "
            f"current_agent is self={session.current_agent is self}, "
            f"session._activity.agent is self={session._activity.agent is self}, "
            f"scheduling_paused={session._activity.scheduling_paused}, "
            f"message texts in own chat_ctx={texts}"
        )
        self._entered.set()
        if not self.resuming:
            self.session.generate_reply(instructions=f"ask {self.step_id}")

    @function_tool
    async def save(self, ctx: RunContext, value: str) -> str:
        """Save the user's answer for this step."""
        self.complete(value)
        return f"saved {self.step_id}"


class Root(Agent):
    def __init__(self, group: TaskGroup) -> None:
        super().__init__(instructions="root")
        self._group = group
        self.result: object = None

    async def on_enter(self) -> None:
        try:
            self.result = await self._group
        except ToolError as e:
            # session.aclose() cancels the waiting step; keep that out of the output
            self.result = e


class ColdStart:
    """One simulated cold start: fresh LLM, session, events and agent chain."""

    def __init__(
        self,
        *,
        resume_from: str | None = None,
        step_ctx: llm.ChatContext | None = None,
        group_ctx: llm.ChatContext | None = None,
        extra_replies: dict[str, Reply] | None = None,
    ) -> None:
        self.llm = make_llm(extra_replies)
        self.session = AgentSession(llm=self.llm)
        self.probe = Probe(self.session)
        self.entered = {sid: asyncio.Event() for sid in STEP_IDS}
        self.enter_log: list[str] = []

        pending = STEP_IDS if resume_from is None else STEP_IDS[STEP_IDS.index(resume_from) :]
        self.group = TaskGroup(
            summarize_chat_ctx=False,
            chat_ctx=group_ctx if group_ctx is not None else NOT_GIVEN,
        )
        for i, sid in enumerate(pending):
            resuming = resume_from is not None and i == 0
            self.group.add(
                self._factory(sid, resuming, step_ctx if resuming else None),
                id=sid,
                description=f"step {sid}",
            )
        self.root = Root(self.group)

    def _factory(
        self, sid: str, resuming: bool, ctx: llm.ChatContext | None
    ) -> Callable[[], AgentTask]:
        def make() -> AgentTask:
            return Step(
                sid,
                resuming=resuming,
                entered=self.entered[sid],
                enter_log=self.enter_log,
                chat_ctx=ctx if ctx is not None else NOT_GIVEN,
            )

        return make

    async def close(self) -> None:
        try:
            await asyncio.wait_for(self.session.aclose(), TIMEOUT)
        except TimeoutError:
            print("  | (session.aclose() timed out)")


def step_id_of(agent: Agent) -> str:
    return agent.step_id if isinstance(agent, Step) else type(agent).__name__


async def s1() -> llm.ChatContext | None:
    print("== S1: fresh session, start(root, capture_run=True)")
    cs = ColdStart()
    obs: list[str] = []
    a_ctx: llm.ChatContext | None = None
    try:
        result = await asyncio.wait_for(cs.session.start(cs.root, capture_run=True), TIMEOUT)
        await asyncio.wait_for(result, TIMEOUT)
        outbound = cs.probe.outbound()
        current = cs.session.current_agent
        obs.append(f"RunResult done={result.done()}, events={[e.type for e in result.events]}")
        obs.append(f"outbound={outbound}")
        obs.append(f"current_agent={step_id_of(current)}")
        obs.append(f"llm.calls={cs.llm.calls}")
        obs.extend(f"call {c}" for c in call_log(cs.llm))
        obs.extend(cs.enter_log)
        ok = outbound == ["What is A?"] and step_id_of(current) == "a"
        a_ctx = current.chat_ctx.copy()
        obs.append("a.chat_ctx at end of S1:")
        obs.extend(f"  {describe(i)}" for i in a_ctx.items)
    except Exception as e:
        ok = False
        obs.append(f"raised {type(e).__name__}: {e}")
    finally:
        await cs.close()
    check("S1", ok, obs)
    return a_ctx


async def s2(
    a_ctx: llm.ChatContext,
    *,
    via: str,
    name: str = "S2",
    extra_replies: dict[str, Reply] | None = None,
) -> None:
    """Resume from a, wait for a.on_enter, then answer a.

    via="step": a_ctx goes to step a's constructor (the literal reading of the plan).
    via="group": a_ctx goes to the stock TaskGroup(chat_ctx=...), which TaskGroup
    copies into each step it starts.
    """
    print(f"== {name}: resume from a (a_ctx passed via {via}), wait for a.on_enter, run('a1')")
    if extra_replies:
        print(f"  | extra replies: {extra_replies}")
    cs = ColdStart(
        resume_from="a",
        step_ctx=a_ctx if via == "step" else None,
        group_ctx=a_ctx if via == "group" else None,
        extra_replies=extra_replies,
    )
    obs: list[str] = []
    s4_obs: list[str] = []
    s5_obs: list[str] = []
    try:
        await asyncio.wait_for(cs.session.start(cs.root), TIMEOUT)
        obs.append(f"after start(): a entered={cs.entered['a'].is_set()}")
        await asyncio.wait_for(cs.entered["a"].wait(), TIMEOUT)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        result = await asyncio.wait_for(cs.session.run(user_input="a1"), TIMEOUT)
        elapsed = loop.time() - t0
        outbound = cs.probe.outbound()
        current = cs.session.current_agent
        obs.append(f"run() returned in {elapsed:.3f}s")
        obs.append(f"RunResult events={[e.type for e in result.events]}")
        obs.append(f"outbound={outbound}")
        obs.append(f"current_agent={step_id_of(current)}")
        obs.append(f"llm.calls={cs.llm.calls}")
        obs.extend(f"call {c}" for c in call_log(cs.llm))
        obs.extend(cs.enter_log)
        expected = ["What is B?"]
        if extra_replies and (post_tool := extra_replies.get("saved a")) and post_tool.text:
            # the post-tool reply of a is expected to be sent too, before b's question
            expected = [post_tool.text, "What is B?"]
        ok = elapsed < TIMEOUT and outbound == expected and step_id_of(current) == "b"

        replayed = replayed_items(cs.probe, a_ctx)
        s4_obs = [
            f"conversation_item_added events={len(cs.probe.items)}: "
            f"{[describe(i) for i in cs.probe.items]}",
            f"restored items replayed={len(replayed)} {replayed}",
        ]
        s5_obs = [describe(i) for i in current.chat_ctx.items]
    except Exception as e:
        ok = False
        obs.append(f"raised {type(e).__name__}: {e}")
        obs.append(f"llm.calls={cs.llm.calls}")
        obs.extend(cs.enter_log)
    finally:
        await cs.close()

    check(name, ok, obs)
    print(f"S4 ({name}): restored items vs conversation_item_added")
    for line in s4_obs:
        print(f"  | {line}")
    print(f"S5 ({name}): b.chat_ctx at end of run")
    for line in s5_obs:
        print(f"  | {line}")


async def s3(a_ctx: llm.ChatContext) -> None:
    """Like S2, but run() immediately after start() without waiting for a.on_enter."""
    print("== S3: resume from a, run('a1') right after start() without waiting")
    cs = ColdStart(resume_from="a", step_ctx=a_ctx)
    obs: list[str] = []
    try:
        await asyncio.wait_for(cs.session.start(cs.root), TIMEOUT)
        obs.append(
            f"at run(): a entered={cs.entered['a'].is_set()}, "
            f"current_agent={step_id_of(cs.session.current_agent)}"
        )
        try:
            result = await asyncio.wait_for(cs.session.run(user_input="a1"), TIMEOUT)
            obs.append(f"run() returned, events={[e.type for e in result.events]}")
        except Exception as e:
            obs.append(f"run() raised {type(e).__name__}: {e}")
        # give the on_enter chain time to settle before looking at the end state
        await asyncio.sleep(0.5)
        obs.append(f"outbound={cs.probe.outbound()}")
        obs.append(f"current_agent after settle={step_id_of(cs.session.current_agent)}")
        obs.append(f"llm.calls={cs.llm.calls}")
        obs.extend(f"call {c}" for c in call_log(cs.llm))
        obs.extend(cs.enter_log)
        # a second message to the same session once a has taken over
        try:
            await asyncio.wait_for(cs.session.run(user_input="a1"), TIMEOUT)
            obs.append(f"second run('a1') after settle: ok, outbound={cs.probe.outbound()}")
        except Exception as e:
            obs.append(f"second run('a1') after settle raised {type(e).__name__}: {e}")
    finally:
        await cs.close()
    print("S3: (observation)")
    for line in obs:
        print(f"  | {line}")


async def s3_sweep(a_ctx: llm.ChatContext, max_yields: int = 40) -> None:
    """Who gets run('a1') when it is fed after n event-loop yields following start()."""
    print("== S3-sweep: run('a1') after n x asyncio.sleep(0) following start()")
    lines: list[str] = []
    for n in range(max_yields + 1):
        cs = ColdStart(resume_from="a", step_ctx=a_ctx)
        try:
            await asyncio.wait_for(cs.session.start(cs.root), TIMEOUT)
            for _ in range(n):
                await asyncio.sleep(0)
            entered = cs.entered["a"].is_set()
            agent = step_id_of(cs.session.current_agent)
            try:
                await asyncio.wait_for(cs.session.run(user_input="a1"), TIMEOUT)
                outcome = "ok"
            except Exception as e:
                outcome = f"raised {type(e).__name__}"
            # let the chain settle so closing does not race the activity transitions
            try:
                await asyncio.wait_for(cs.entered["a"].wait(), TIMEOUT)
            except TimeoutError:
                outcome += " (a never entered)"
            lines.append(
                f"n={n:2d}: a entered={entered!s:5} current_agent={agent:9} run={outcome}; "
                f"calls={call_log(cs.llm)}"
            )
            if outcome != "ok":
                # an LLM call made although run() failed: show what it was given
                for key, ctx in zip(cs.llm.calls, cs.llm.contexts, strict=True):
                    lines.append(f"      ctx of {key!r}: {[describe(i) for i in ctx.items]}")
            if entered and outcome == "ok":
                break
        finally:
            await cs.close()
    for line in lines:
        print(f"  | {line}")


async def main() -> int:
    a_ctx = await s1()
    if a_ctx is None:
        print("S1 produced no chat_ctx for a; cannot run S2-S5")
        return 1
    await s2(a_ctx, via="step", name="S2")
    await s2(a_ctx, via="group", name="S2-group")
    # same as S2-group, but the LLM call that follows a's tool output returns text
    post_tool = {"saved a": Reply(text="Got it.")}
    await s2(a_ctx, via="group", name="S2-post-tool-text", extra_replies=post_tool)
    await s3(a_ctx)
    await s3_sweep(a_ctx)
    print(f"== summary: {results}")
    return 0 if results and all(results.values()) else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)  # keep prints and SDK logs in order
    logging.basicConfig(level=logging.WARNING, format="  [log] %(levelname)s %(name)s: %(message)s")
    sys.exit(asyncio.run(main()))
