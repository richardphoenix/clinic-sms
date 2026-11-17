import asyncio
import time
from typing import Annotated

import pytest
from livekit.agents import Agent, AgentSession, APIError, RunContext, function_tool
from livekit.agents.llm import AgentConfigUpdate, ChatContext, FunctionCall
from pydantic import Field

from clinic_sms.ctx import assistant_texts, normalize_ctx
from clinic_sms.testing import Reply, ScriptedLLM, anthropic_tool_problems, tool_names


async def test_replies_to_user_text():
    llm = ScriptedLLM({"hi": Reply(text="hello")})
    async with AgentSession(llm=llm) as s:
        await asyncio.wait_for(s.start(Agent(instructions="test")), 5)
        await asyncio.wait_for(s.run(user_input="hi"), 5)
        assert assistant_texts(s.history) == ["hello"]
    assert llm.calls == ["hi"]


async def test_tool_call_is_executed():
    seen: list[int] = []

    class A(Agent):
        def __init__(self):
            super().__init__(instructions="test")

        @function_tool
        async def echo(self, ctx: RunContext, x: int) -> str:
            seen.append(x)
            return "echoed"

    llm = ScriptedLLM({"go": Reply(tools=[("echo", {"x": 7})]), "echoed": Reply(text="done")})
    async with AgentSession(llm=llm) as s:
        await asyncio.wait_for(s.start(A()), 5)
        await asyncio.wait_for(s.run(user_input="go"), 5)
    assert seen == [7]


async def test_unscripted_input_raises():
    async with AgentSession(llm=ScriptedLLM({})) as s:
        await asyncio.wait_for(s.start(Agent(instructions="test")), 5)
        with pytest.raises(APIError, match="unscripted input"):
            await asyncio.wait_for(s.run(user_input="nope"), 5)


def _ctx_ending_in_config_update() -> ChatContext:
    # The SDK does call the LLM with such a context (seen in the cold-start spike).
    ctx = ChatContext.empty()
    ctx.add_message(role="system", content="test")
    ctx.insert(AgentConfigUpdate(instructions="test"))
    return ctx


async def test_no_key_call_is_recorded_and_fails_without_default():
    llm = ScriptedLLM({})
    with pytest.raises(APIError):
        await asyncio.wait_for(llm.chat(chat_ctx=_ctx_ending_in_config_update()).collect(), 5)
    assert llm.calls == ["<no key: agent_config_update>"]
    assert [i.type for i in llm.contexts[0].items] == ["message", "agent_config_update"]


async def test_no_key_call_uses_default():
    llm = ScriptedLLM({}, default=Reply(text="fallback"))
    resp = await asyncio.wait_for(llm.chat(chat_ctx=_ctx_ending_in_config_update()).collect(), 5)
    assert resp.text == "fallback"
    assert llm.calls == ["<no key: agent_config_update>"]
    assert len(llm.contexts) == 1


def _ctx(text: str) -> ChatContext:
    ctx = ChatContext.empty()
    ctx.add_message(role="user", content=text)
    return ctx


async def test_tools_are_recorded_and_an_empty_enum_is_reported():
    @function_tool
    async def pick(choice: Annotated[str, Field(json_schema_extra={"enum": []})]) -> None:
        """Pick one."""

    @function_tool
    async def save(value: Annotated[str, Field(json_schema_extra={"enum": ["a"]})]) -> None:
        """Save it."""

    llm = ScriptedLLM({}, default=Reply())
    await asyncio.wait_for(llm.chat(chat_ctx=_ctx("hi"), tools=[pick, save]).collect(), 5)
    await asyncio.wait_for(llm.chat(chat_ctx=_ctx("again")).collect(), 5)
    assert [tool_names(tools) for tools in llm.tools] == [["pick", "save"], []]
    assert anthropic_tool_problems(llm) == [
        "call 0 ('hi'): tool pick: empty input_schema.properties.choice.enum"
    ]


async def test_reply_error_fails_the_call():
    llm = ScriptedLLM({"boom": Reply(error=APIError("scripted failure", retryable=False))})
    with pytest.raises(APIError, match="scripted failure"):
        await asyncio.wait_for(llm.chat(chat_ctx=_ctx("boom")).collect(), 5)
    assert llm.calls == ["boom"]


async def test_reply_delay_holds_back_the_first_chunk():
    llm = ScriptedLLM({"slow": Reply(text="ok", delay=0.2)})
    started = time.monotonic()
    resp = await asyncio.wait_for(llm.chat(chat_ctx=_ctx("slow")).collect(), 5)
    assert resp.text == "ok"
    assert time.monotonic() - started >= 0.15


async def test_default_answers_a_key_that_is_not_scripted():
    llm = ScriptedLLM({"known": Reply(text="a")}, default=Reply(text="fallback"))
    resp = await asyncio.wait_for(llm.chat(chat_ctx=_ctx("other")).collect(), 5)
    assert resp.text == "fallback"
    assert llm.calls == ["other"]


async def test_tool_call_ids_are_unique_within_an_instance():
    llm = ScriptedLLM({"go": Reply(tools=[("a", {}), ("a", {"x": 1})])})
    first = await asyncio.wait_for(llm.chat(chat_ctx=_ctx("go")).collect(), 5)
    second = await asyncio.wait_for(llm.chat(chat_ctx=_ctx("go")).collect(), 5)
    ids = [c.call_id for c in first.tool_calls + second.tool_calls]
    assert len(ids) == 4 and len(set(ids)) == 4


async def test_contexts_keeps_copies():
    llm = ScriptedLLM({"hi": Reply(text="x")})
    ctx = _ctx("hi")
    await asyncio.wait_for(llm.chat(chat_ctx=ctx).collect(), 5)
    ctx.add_message(role="user", content="later")
    assert llm.contexts[0] is not ctx
    assert [i.text_content for i in llm.contexts[0].items] == ["hi"]


def test_normalize_ctx_keeps_arguments_that_are_not_json():
    ctx = ChatContext.empty()
    ctx.insert(FunctionCall(call_id="c1", name="t", arguments="not json {"))
    ctx.insert(FunctionCall(call_id="c2", name="t", arguments='{"b": 1, "a": 2}'))
    assert normalize_ctx(ctx) == [
        ("function_call", "t", "not json {"),
        ("function_call", "t", '{"a":2,"b":1}'),
    ]
