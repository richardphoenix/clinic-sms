"""The Claude LLM builders, checked offline: no request leaves the process."""

import json

import anthropic
import httpx2 as httpx  # the HTTP client anthropic 1.x is built on
from livekit.agents import llm
from livekit.agents.evals import accuracy_judge

from clinic_sms.llm import MAX_TOKENS, MODEL, make_judge_llm

# One streamed Messages API response: a submit_verdict tool call with verdict "pass".
_EVENTS = [
    {
        "type": "message_start",
        "message": {
            "id": "msg_offline",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    },
    {
        "type": "content_block_start",
        "index": 0,
        "content_block": {
            "type": "tool_use",
            "id": "toolu_offline",
            "name": "submit_verdict",
            "input": {},
        },
    },
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {
            "type": "input_json_delta",
            "partial_json": json.dumps({"verdict": "pass", "reasoning": "canned"}),
        },
    },
    {"type": "content_block_stop", "index": 0},
    {
        "type": "message_delta",
        "delta": {"stop_reason": "tool_use", "stop_sequence": None},
        "usage": {"output_tokens": 1},
    },
    {"type": "message_stop"},
]
CANNED_SSE = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in _EVENTS).encode()


def offline_client(requests: list[httpx.Request]) -> anthropic.AsyncClient:
    """An Anthropic client whose transport records each request and answers CANNED_SSE."""

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=CANNED_SSE
        )

    return anthropic.AsyncClient(
        api_key="offline-test",  # a placeholder: the mock transport never sends it anywhere
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(record)),
    )


def conversation() -> llm.ChatContext:
    ctx = llm.ChatContext()
    ctx.add_message(role="user", content="When is the clinic open on Friday?")
    ctx.add_message(role="assistant", content="We're open 8 AM to 5 PM on Friday.")
    return ctx


async def test_judge_llm_sends_the_judge_request_without_temperature(monkeypatch):  # FINDING-9
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test")  # a placeholder, never sent
    judge = make_judge_llm()
    requests: list[httpx.Request] = []
    # the plugin keeps its client here; swapped so the request goes to the mock transport
    monkeypatch.setattr(judge, "_client", offline_client(requests))

    result = await accuracy_judge(judge).evaluate(chat_ctx=conversation())

    assert result.verdict == "pass"
    (request,) = requests
    body = json.loads(request.content)
    assert "temperature" not in body
    assert (body["model"], body["max_tokens"]) == (MODEL, MAX_TOKENS)
    assert [t["name"] for t in body["tools"]] == ["submit_verdict"]


async def test_judge_llm_keeps_other_extra_kwargs(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test")
    judge = make_judge_llm()
    requests: list[httpx.Request] = []
    monkeypatch.setattr(judge, "_client", offline_client(requests))

    await judge.chat(
        chat_ctx=conversation(),
        extra_kwargs={"temperature": 0.0, "metadata": {"user_id": "eval"}},
    ).collect()

    (request,) = requests
    body = json.loads(request.content)
    assert "temperature" not in body
    assert body["metadata"] == {"user_id": "eval"}
