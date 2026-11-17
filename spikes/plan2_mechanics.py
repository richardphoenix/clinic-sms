"""Spike 4: SDK mechanics that Plan 2's handoff chain and proactive messages rely on.

Throwaway script, not maintained. Every check builds a brand-new AgentSession, as a
cold start does: plain `start()`, wait for the ReadySignal, then `run()` (H1, H1n, H2)
or `generate_reply()` (H3*). Outbound texts are collected as the runner does: assistant
ChatMessages with non-empty text from `conversation_item_added`.

  H1   a tool returns a new agent B (no message): is B's opening question collected in
       the same run()?
  H1n  a tool returns (B, "moved to b"): A's reply to the message, then B's question.
       The old agent's reply is generated with tool_choice="none" while the context
       holds the tool call; the probe prints every call's tool_choice.
  H2   the new agent G awaits a ResumableTaskGroup in on_enter: is the innermost
       step's question collected in the same run()?
  H3   proactive message on a resumed agent: generate_reply(instructions=NUDGE)
  H3n  as H3, with tool_choice="none" (context holds a restored tool call)
  H3e  (scripted only) as H3, but the LLM call fails

--llm scripted  ScriptedLLM, no network (default).
--llm claude    Claude via clinic_sms.llm.make_claude_llm(); needs ANTHROPIC_API_KEY
                (exit 2 without it); about 9-12 calls (scripted mode makes 9).
--wait-for-idle the Gate variant: also `await session.wait_for_idle()` after run()
                returns / after the handle is done.

Run: uv run python spikes/plan2_mechanics.py --llm scripted
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

from livekit.agents import (
    Agent,
    AgentSession,
    AgentTask,
    APIError,
    ConversationItemAddedEvent,
    ErrorEvent,
    function_tool,
    llm,
)
from livekit.agents.llm import LLM, LLMStream, Tool, ToolChoice, ToolError
from livekit.agents.types import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectOptions,
    NotGivenOr,
)
from livekit.agents.utils import is_given
from pydantic import BaseModel

from clinic_sms.llm import make_claude_llm
from clinic_sms.state import GroupProgress
from clinic_sms.testing import Reply, ScriptedLLM, tool_names
from clinic_sms.workflow import ReadySignal, ResumableTaskGroup

TIMEOUT = 90.0
CLOSE_TIMEOUT = 30.0

# Real prompts in both modes: in scripted mode they are the script keys.
ASK_B = "Ask the user what their favorite color is."
ASK_S = "Ask the user which city they live in."
NUDGE = (
    "The user has not replied for a while. "
    "Send one short, friendly nudge that repeats your last question."
)

SMS_STYLE = (
    "You are chatting with a user by SMS. "
    "Keep every message to one short sentence of plain text."
)
A_INSTRUCTIONS = (
    f"{SMS_STYLE} You are agent A, a router in a test. Rules: "
    "when the user writes `go b`, call go_b; "
    "when the user writes `go b noted`, call go_b_noted; "
    "when the user writes `go g`, call go_g. "
    "Do not reply with any text before calling the tool."
)
B_INSTRUCTIONS = f"{SMS_STYLE} You are agent B. Your job is to learn the user's favorite color."
G_INSTRUCTIONS = f"{SMS_STYLE} You are agent G."
S_INSTRUCTIONS = (
    f"{SMS_STYLE} You are step S. Your job is to learn which city the user lives in; "
    "when they answer, call save with it."
)


def scripted_replies() -> dict[str, Reply]:
    return {
        "go b": Reply(tools=[("go_b", {})]),
        ASK_B: Reply(text="What is B?"),
        "go b noted": Reply(tools=[("go_b_noted", {})]),
        "moved to b": Reply(text="Moving you to b."),
        "go g": Reply(tools=[("go_g", {})]),
        ASK_S: Reply(text="What is S?"),
        NUDGE: Reply(text="Still there? What is B?"),
    }


# ---------------------------------------------------------------- output and logs


def say(line: str = "") -> None:
    print(line, flush=True)


class LogTap(logging.Handler):
    """Prints WARNING+ records as they happen and keeps them for the running check."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(record)
            say(f"  [log] {format_record(record)}")
        except Exception:  # never let logging break the spike
            pass

    def take(self) -> list[logging.LogRecord]:
        out, self.records = self.records, []
        return out


def format_record(record: logging.LogRecord) -> str:
    msg = f"{record.levelname} {record.name}: {record.getMessage()}"
    if record.exc_info and record.exc_info[1] is not None:
        msg += f" [exc_info {describe_exc(record.exc_info[1])}]"
    return msg


LOG_TAP = LogTap()


def describe_exc(e: BaseException) -> str:
    out = f"{type(e).__name__}: {e}"
    status = getattr(e, "status_code", None)
    if status is not None:
        out += f" | status_code={status}"
    body = getattr(e, "body", None)
    if body is not None:
        out += f" | body={body!r}"
    return out


# ---------------------------------------------------------------- the LLM probe


@dataclass
class CallRecord:
    tool_choice: str
    tools: list[str]
    function_calls: int
    items: int
    last: str

    def __str__(self) -> str:
        return (
            f"tool_choice={self.tool_choice} tools={self.tools} "
            f"function_calls_in_ctx={self.function_calls} ctx_items={self.items} last={self.last}"
        )


def last_item_label(chat_ctx: llm.ChatContext) -> str:
    if not chat_ctx.items:
        return "<empty>"
    item = chat_ctx.items[-1]
    if item.type == "message":
        return f"{item.role}:{(item.text_content or '')[:40]!r}"
    if item.type == "function_call_output":
        return f"function_call_output:{item.output[:40]!r}"
    return item.type


class Probe(LLM):
    """Wraps the real LLM and forwards chat() unchanged; prints one line per call."""

    def __init__(self, inner: LLM) -> None:
        super().__init__()
        self._inner = inner
        self.calls: list[CallRecord] = []
        # LLMStream emits error / metrics events on the LLM that created the stream
        # (the inner one). Re-emit them so the session sees them as it would without
        # the probe (AgentActivity subscribes to session.llm, voice/agent_activity.py:957-958).
        inner.on("error", lambda ev: self.emit("error", ev))
        inner.on("metrics_collected", lambda ev: self.emit("metrics_collected", ev))

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def provider(self) -> str:
        return self._inner.provider

    def prewarm(self, *, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._inner.prewarm(loop=loop)

    async def aclose(self) -> None:
        await self._inner.aclose()

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN,
        tool_choice: NotGivenOr[ToolChoice] = NOT_GIVEN,
        extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN,
    ) -> LLMStream:
        record = CallRecord(
            tool_choice=str(tool_choice) if is_given(tool_choice) else "default",
            tools=tool_names(list(tools or [])),
            function_calls=sum(1 for i in chat_ctx.items if i.type == "function_call"),
            items=len(chat_ctx.items),
            last=last_item_label(chat_ctx),
        )
        self.calls.append(record)
        say(f"  [llm] call {len(self.calls)}: {record}")
        return self._inner.chat(
            chat_ctx=chat_ctx,
            tools=tools,
            conn_options=conn_options,
            parallel_tool_calls=parallel_tool_calls,
            tool_choice=tool_choice,
            extra_kwargs=extra_kwargs,
        )


# ---------------------------------------------------------------- agents


class SResult(BaseModel):
    value: str


class A(Agent):
    def __init__(self, h: Harness) -> None:
        super().__init__(instructions=A_INSTRUCTIONS)
        self._h = h
        h.register(self)

    async def on_enter(self) -> None:
        self._h.ready.set(self)

    @function_tool
    async def go_b(self) -> Agent:
        """Move the user to agent B."""
        return B(self._h, resuming=False)

    @function_tool
    async def go_b_noted(self) -> tuple[Agent, str]:
        """Move the user to agent B and leave a note."""
        return B(self._h, resuming=False), "moved to b"

    @function_tool
    async def go_g(self) -> Agent:
        """Move the user to agent G."""
        return G(self._h)


class B(Agent):
    def __init__(
        self,
        h: Harness,
        *,
        resuming: bool,
        chat_ctx: NotGivenOr[llm.ChatContext] = NOT_GIVEN,
    ) -> None:
        super().__init__(instructions=B_INSTRUCTIONS, chat_ctx=chat_ctx)
        self._h = h
        self.resuming = resuming
        h.register(self)

    async def on_enter(self) -> None:
        self._h.ready.set(self)
        if not self.resuming:
            self.session.generate_reply(instructions=ASK_B)

    @function_tool
    async def lookup(self) -> None:
        """Reserved for an internal test. Do not call it."""
        # Exists so a restored `lookup` function_call survives Agent.__init__, which keeps
        # only calls to the agent's own tools (voice/agent.py:92).
        return None


class S(AgentTask[SResult]):
    def __init__(self, h: Harness) -> None:
        super().__init__(instructions=S_INSTRUCTIONS)
        h.register(self)

    async def on_enter(self) -> None:
        self.session.generate_reply(instructions=ASK_S)

    @function_tool
    async def save(self, value: str) -> None:
        """Save the city the user lives in."""
        self.complete(SResult(value=value))


class G(Agent):
    def __init__(self, h: Harness) -> None:
        super().__init__(instructions=G_INSTRUCTIONS)
        h.register(self)
        self.group = ResumableTaskGroup(summarize_chat_ctx=False)
        self.group.add_step(
            lambda resuming: S(h),
            id="s",
            description="Ask which city the user lives in",
            result_type=SResult,
        )
        self.result: object = None

    async def on_enter(self) -> None:
        try:
            self.result = await self.group
        except ToolError as e:
            # session.aclose() cancels the waiting step (FINDING-4); keep it out of the output
            self.result = e


def name_of(agent: Agent | None) -> str:
    if agent is None:
        return "None"
    return type(agent).__name__


# ---------------------------------------------------------------- harness


@dataclass
class Options:
    mode: str  # "scripted" | "claude"
    wait_for_idle: bool

    @property
    def scripted(self) -> bool:
        return self.mode == "scripted"


class Harness:
    """One simulated cold start: fresh LLM, session, ReadySignal and event taps."""

    def __init__(self, opts: Options, *, extra_replies: dict[str, Reply] | None = None) -> None:
        self.opts = opts
        self.ready = ReadySignal()
        inner: LLM
        if opts.scripted:
            # default=Reply(): a call the SDK makes on its own is printed by the probe
            # and answered with nothing, instead of failing the check
            inner = ScriptedLLM(scripted_replies() | (extra_replies or {}), default=Reply())
        else:
            inner = make_claude_llm()
        self.llm = Probe(inner)
        self.session = AgentSession(llm=self.llm)
        self.agents: list[Agent] = []  # every agent built in this check, for attribution
        self.outbound: list[llm.ChatMessage] = []
        self.errors: list[str] = []
        self.session.on("conversation_item_added", self._on_item)
        self.session.on("error", self._on_error)

    def register(self, agent: Agent) -> None:
        self.agents.append(agent)

    def _on_item(self, ev: ConversationItemAddedEvent) -> None:
        item = ev.item
        if item.type == "message" and item.role == "assistant":
            text = item.text_content
            if text and text.strip():
                self.outbound.append(item)

    def _on_error(self, ev: ErrorEvent) -> None:
        err = ev.error
        inner = getattr(err, "error", err)
        recoverable = getattr(err, "recoverable", None)
        self.errors.append(f"{describe_exc(inner)} (recoverable={recoverable})")

    def speaker_of(self, msg: llm.ChatMessage) -> str:
        """Names of the agents whose chat_ctx holds this message ('?' if none)."""
        names = [
            name_of(agent)
            for agent in self.agents
            if any(i.id == msg.id for i in agent.chat_ctx.items)
        ]
        return "+".join(names) or "?"

    def outbound_speakers(self) -> list[str]:
        return [self.speaker_of(m) for m in self.outbound]

    def outbound_texts(self) -> list[str]:
        return [m.text_content or "" for m in self.outbound]

    async def start(self, agent: Agent) -> None:
        await asyncio.wait_for(self.session.start(agent), TIMEOUT)
        await self.ready.wait(TIMEOUT)

    async def maybe_wait_idle(self) -> None:
        if self.opts.wait_for_idle:
            await asyncio.wait_for(self.session.wait_for_idle(), TIMEOUT)

    async def run(self, user_input: str) -> Any:
        result = await asyncio.wait_for(self.session.run(user_input=user_input), TIMEOUT)
        await self.maybe_wait_idle()
        return result

    def common_observations(self) -> list[str]:
        obs = [f"outbound ({len(self.outbound)}):"]
        obs.extend(
            f"  [{self.speaker_of(m)}] {m.text_content!r}" for m in self.outbound
        )
        try:
            obs.append(f"current_agent={name_of(self.session.current_agent)}")
        except RuntimeError as e:
            obs.append(f"current_agent unavailable: {e}")
        obs.append(f"llm calls ({len(self.llm.calls)}):")
        obs.extend(f"  {i + 1}: {c}" for i, c in enumerate(self.llm.calls))
        obs.append(f"session error events: {self.errors or 'none'}")
        return obs

    async def close(self) -> None:
        try:
            await asyncio.wait_for(self.session.aclose(), CLOSE_TIMEOUT)
        except Exception as e:
            say(f"  | session.aclose() failed: {describe_exc(e)}")


# ---------------------------------------------------------------- checks

results: dict[str, str] = {}


def report(name: str, verdict: str, obs: list[str], logs: list[logging.LogRecord]) -> None:
    results[name] = verdict
    say(f"{name}: {verdict}")
    for line in obs:
        say(f"  | {line}")
    say(f"  | log records (WARNING+) during the check and close: {len(logs)}")
    for r in logs:
        say(f"  |   {format_record(r)}")


def describe_event(e: Any) -> str:
    if e.type == "agent_handoff":
        return f"agent_handoff {name_of(e.old_agent)}->{name_of(e.new_agent)}"
    if e.type == "message":
        return f"message/{e.item.role}"
    return f"{e.type} {getattr(e.item, 'name', '')}".rstrip()


def handoffs(result: Any) -> list[tuple[str, str]]:
    return [
        (name_of(e.old_agent), name_of(e.new_agent))
        for e in result.events
        if e.type == "agent_handoff"
    ]


async def check_handoff(
    opts: Options,
    name: str,
    *,
    user_input: str,
    expect_speakers: list[str],
    expect_texts: list[str],
    expect_agent: type[Agent],
    expect_handoff: tuple[str, str],
) -> None:
    """H1, H1n, H2: start from A, run(user_input), check what one run() collected."""
    say(f"== {name}: start from A, run({user_input!r})")
    LOG_TAP.take()
    h = Harness(opts)
    obs: list[str] = []
    ok = False
    try:
        a = A(h)
        await h.start(a)
        t0 = time.monotonic()
        result = await h.run(user_input)
        obs.append(f"run() returned in {time.monotonic() - t0:.3f}s")
        obs.append(f"RunResult events: {[describe_event(e) for e in result.events]}")
        obs.extend(h.common_observations())
        current = h.session.current_agent
        speakers_ok = h.outbound_speakers() == expect_speakers
        texts_ok = not opts.scripted or h.outbound_texts() == expect_texts
        agent_ok = isinstance(current, expect_agent)
        handoff_ok = expect_handoff in handoffs(result)
        obs.append(
            f"criteria: speakers=={expect_speakers}: {speakers_ok}; "
            + (f"texts=={expect_texts}: {texts_ok}; " if opts.scripted else "")
            + f"current_agent is {expect_agent.__name__}: {agent_ok}; "
            f"handoff {expect_handoff[0]}->{expect_handoff[1]} in events: {handoff_ok}"
        )
        if name == "H1n":
            obs.append(
                "tool_choice per call: "
                + ", ".join(f"{i + 1}={c.tool_choice}" for i, c in enumerate(h.llm.calls))
            )
        if isinstance(current, S):
            g = next(x for x in h.agents if isinstance(x, G))
            progress: GroupProgress = g.group.progress
            obs.append(f"group progress after run(): {progress.model_dump()}")
        ok = speakers_ok and texts_ok and agent_ok and handoff_ok
    except Exception as e:
        obs.append(f"raised {describe_exc(e)}")
        obs.extend(h.common_observations())
    finally:
        await h.close()
    report(name, "PASS" if ok else "FAIL", obs, LOG_TAP.take())


def restored_b_ctx() -> llm.ChatContext:
    """B's context as a cold start restores it: hi, lookup call + output, B's question."""
    ctx = llm.ChatContext.empty()
    ctx.add_message(role="user", content="hi")
    ctx.items.append(llm.FunctionCall(call_id="call_lookup_1", name="lookup", arguments="{}"))
    ctx.items.append(
        llm.FunctionCallOutput(call_id="call_lookup_1", name="lookup", output="", is_error=False)
    )
    ctx.add_message(role="assistant", content="What is B?")
    return ctx


async def check_nudge(
    opts: Options,
    name: str,
    *,
    tool_choice: NotGivenOr[ToolChoice] = NOT_GIVEN,
    fail: bool = False,
) -> None:
    """H3, H3n, H3e: resume B, then send a proactive message with generate_reply."""
    tc = f", tool_choice={tool_choice!r}" if is_given(tool_choice) else ""
    suffix = " [LLM fails]" if fail else ""
    say(f"== {name}: resume B, generate_reply(instructions=NUDGE{tc}){suffix}")
    LOG_TAP.take()
    extra = {NUDGE: Reply(error=APIError("down", retryable=False))} if fail else None
    h = Harness(opts, extra_replies=extra)
    obs: list[str] = []
    verdict = "FAIL"
    try:
        b = B(h, resuming=True, chat_ctx=restored_b_ctx())
        restored_calls = sum(1 for i in b.chat_ctx.items if i.type == "function_call")
        obs.append(f"function_call items in B's ctx after __init__: {restored_calls}")
        await h.start(b)
        t0 = time.monotonic()
        handle = h.session.generate_reply(instructions=NUDGE, tool_choice=tool_choice)
        await asyncio.wait_for(handle, TIMEOUT)
        await h.maybe_wait_idle()
        obs.append(f"handle done in {time.monotonic() - t0:.3f}s, done()={handle.done()}")
        exc = handle.exception()
        obs.append(f"handle.exception(): {describe_exc(exc) if exc is not None else None}")
        obs.extend(h.common_observations())
        messages = [i for i in b.chat_ctx.items if i.type == "message"]
        last = messages[-1] if messages else None
        obs.append(
            "B's last chat_ctx message: "
            + (f"{last.role} {last.text_content!r}" if last is not None else "none")
        )
        if fail:
            errors_logged = [r for r in LOG_TAP.records if r.levelno >= logging.ERROR]
            obs.append(
                f"H3e observations: exception type="
                f"{type(exc).__name__ if exc is not None else None}; "
                f"outbound empty={not h.outbound}; "
                f"SDK logged ERROR={bool(errors_logged)} ({len(errors_logged)} record(s) "
                "before close; full list below)"
            )
            verdict = "OBSERVED"
        else:
            speakers_ok = h.outbound_speakers() == ["B"]
            texts_ok = not opts.scripted or h.outbound_texts() == ["Still there? What is B?"]
            exc_ok = exc is None
            last_ok = (
                last is not None
                and len(h.outbound) == 1
                and last.id == h.outbound[0].id
                and last.role == "assistant"
            )
            # without the restored call, H3n would not test "no tools + a tool call in context"
            ctx_ok = bool(h.llm.calls) and h.llm.calls[-1].function_calls == 1
            obs.append(
                f"criteria: speakers==['B']: {speakers_ok}; "
                + (f"texts==['Still there? What is B?']: {texts_ok}; " if opts.scripted else "")
                + f"exception is None: {exc_ok}; last message is the nudge: {last_ok}; "
                f"nudge call saw the restored function_call: {ctx_ok}"
            )
            verdict = (
                "PASS" if speakers_ok and texts_ok and exc_ok and last_ok and ctx_ok else "FAIL"
            )
    except Exception as e:
        obs.append(f"raised {describe_exc(e)}")
        obs.extend(h.common_observations())
    finally:
        await h.close()
    report(name, verdict, obs, LOG_TAP.take())


async def main(opts: Options) -> int:
    say(f"mode={opts.mode} wait_for_idle={opts.wait_for_idle} timeout={TIMEOUT}s")
    await check_handoff(
        opts,
        "H1",
        user_input="go b",
        expect_speakers=["B"],
        expect_texts=["What is B?"],
        expect_agent=B,
        expect_handoff=("A", "B"),
    )
    await check_handoff(
        opts,
        "H1n",
        user_input="go b noted",
        expect_speakers=["A", "B"],
        expect_texts=["Moving you to b.", "What is B?"],
        expect_agent=B,
        expect_handoff=("A", "B"),
    )
    await check_handoff(
        opts,
        "H2",
        user_input="go g",
        expect_speakers=["S"],
        expect_texts=["What is S?"],
        expect_agent=S,
        expect_handoff=("A", "G"),
    )
    await check_nudge(opts, "H3")
    await check_nudge(opts, "H3n", tool_choice="none")
    if opts.scripted:
        await check_nudge(opts, "H3e", fail=True)
    else:
        results["H3e"] = "SKIPPED"
        say("H3e: SKIPPED (scripted mode only)")
    say("SUMMARY: " + " ".join(f"{k}={v}" for k, v in results.items()))
    return 1 if "FAIL" in results.values() else 0


def parse_args(argv: list[str]) -> Options:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--llm", choices=["scripted", "claude"], default="scripted")
    p.add_argument(
        "--wait-for-idle",
        action="store_true",
        help="Gate variant: also await session.wait_for_idle() after run() / the handle",
    )
    a = p.parse_args(argv)
    return Options(mode=a.llm, wait_for_idle=a.wait_for_idle)


def main_sync(argv: list[str]) -> int:
    opts = parse_args(argv)
    if not opts.scripted and not os.environ.get("ANTHROPIC_API_KEY"):
        say("ANTHROPIC_API_KEY is not set; export it in your shell and rerun (exit 2).")
        return 2
    # one handler on the root logger: livekit.* loggers propagate to it
    root = logging.getLogger()
    root.addHandler(LOG_TAP)
    root.setLevel(logging.WARNING)
    return asyncio.run(main(opts))


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)  # keep prints and SDK logs in order
    sys.exit(main_sync(sys.argv[1:]))

