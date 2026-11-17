"""FINDING-9 repro: the SDK's LLM judges cannot use Claude through livekit-plugins-anthropic.

Every LLM judge call passes `extra_kwargs={"temperature": 0.0}` (livekit-agents 1.8.11,
evals/judge.py:152-156; `ChatMessageAssert.judge` does the same, voice/run_result.py:
1057-1061). The Anthropic plugin merges `extra_kwargs` into the keyword arguments of
`client.messages.create(**extra)` (plugins/anthropic/llm.py:175-176, 299-305), and the
`messages.create` of anthropic 1.17 has no `temperature` parameter. The call raises
TypeError before any request is sent; the plugin wraps it as a retryable
APIConnectionError (llm.py:380-381), so the judge fails after four attempts.

  stock judge           the plugin's LLM as the judge
  temperature stripped  (control) the same LLM, with `temperature` dropped from
                        `extra_kwargs` before the plugin sees it

Both use a real `anthropic.AsyncClient` over an httpx2 MockTransport that records each
request and answers with a canned `submit_verdict` tool call; nothing reaches the
network. The api_key is a placeholder string, not a credential. The judge's retry
warnings are silenced; the plugin's own WARNING records are printed as observed.

Self-contained: livekit-agents and livekit-plugins-anthropic only, no API key.
Run: uv run python repro/finding_9.py
Exits 1 while the finding reproduces, 0 once it no longer does, 2 if the control fails.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import logging
import sys
from typing import Any

import anthropic
import httpx2 as httpx  # the HTTP client anthropic 1.17 is built on
from livekit.agents import llm
from livekit.agents.evals import accuracy_judge
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.plugins.anthropic import LLM

MODEL = "claude-opus-5-5"
PLACEHOLDER_KEY = "offline-repro"  # not a credential: no request leaves the process
TIMEOUT = 30.0

# One streamed Messages API response: a submit_verdict tool call with verdict "pass".
_EVENTS: list[dict[str, Any]] = [
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


class StripTemperature(LLM):
    """The plugin's LLM, with `temperature` dropped from `extra_kwargs`."""

    def chat(self, *, extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN, **kwargs: Any):
        if isinstance(extra_kwargs, dict):
            extra_kwargs = {k: v for k, v in extra_kwargs.items() if k != "temperature"}
        return super().chat(extra_kwargs=extra_kwargs, **kwargs)


def build(cls: type[LLM]) -> tuple[LLM, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=CANNED_SSE
        )

    client = anthropic.AsyncClient(
        api_key=PLACEHOLDER_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(record)),
    )
    return cls(model=MODEL, api_key=PLACEHOLDER_KEY, client=client), requests


def conversation() -> llm.ChatContext:
    ctx = llm.ChatContext()
    ctx.add_message(role="user", content="When is the clinic open on Friday?")
    ctx.add_message(role="assistant", content="We're open 8 AM to 5 PM on Friday.")
    return ctx


def chain(exc: BaseException | None) -> list[BaseException]:
    seen: list[BaseException] = []
    while exc is not None and exc not in seen:
        seen.append(exc)
        exc = exc.__cause__ or exc.__context__
    return seen


async def run(cls: type[LLM]) -> dict[str, Any]:
    judge_llm, requests = build(cls)
    out: dict[str, Any] = {"verdict": None, "chain": [], "requests": requests}
    try:
        result = await asyncio.wait_for(
            accuracy_judge(judge_llm).evaluate(chat_ctx=conversation()), TIMEOUT
        )
        out["verdict"] = result.verdict
    except Exception as e:  # noqa: BLE001 - the outcome is what this script reports
        out["chain"] = chain(e)
    return out


def describe(out: dict[str, Any]) -> str:
    requests: list[httpx.Request] = out["requests"]
    parts = [f"requests sent: {len(requests)}"]
    if out["chain"]:
        names = [type(e).__name__ for e in out["chain"]]
        parts.insert(0, f"raised {' <- '.join(names)}: {out['chain'][0]}")
    else:
        parts.insert(0, f"verdict {out['verdict']!r}")
    for request in requests:
        body = json.loads(request.content)
        parts.append(f"body has temperature: {'temperature' in body}")
        parts.append(f"tool_choice: {body.get('tool_choice')}")
    return "; ".join(parts)


async def main() -> int:
    versions = ", ".join(
        f"{name} {importlib.metadata.version(name)}"
        for name in ("livekit-agents", "livekit-plugins-anthropic", "anthropic")
    )
    print(versions)
    logging.getLogger("livekit.agents").setLevel(logging.CRITICAL)  # retry warnings
    plugin_warnings: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            plugin_warnings.append(f"{record.levelname} {record.getMessage()}")

    plugin_logger = logging.getLogger("livekit.plugins.anthropic")
    plugin_logger.addHandler(Collect(level=logging.WARNING))
    plugin_logger.propagate = False

    stock = await run(LLM)
    stripped = await run(StripTemperature)
    print(f"stock judge          : {describe(stock)}")
    print(f"temperature stripped : {describe(stripped)}")
    for warning in dict.fromkeys(plugin_warnings):
        print(f"plugin log           : {warning}")

    control_ok = (
        stripped["verdict"] == "pass"
        and len(stripped["requests"]) == 1
        and "temperature" not in json.loads(stripped["requests"][0].content)
    )
    if not control_ok:
        print("INCONCLUSIVE: the control did not send one request without temperature")
        return 2
    if stock["requests"]:
        body = json.loads(stock["requests"][0].content)
        print(
            "NOT REPRODUCED: the stock judge sent its request; "
            f"temperature in the body: {'temperature' in body}"
        )
        return 0
    if any(isinstance(e, TypeError) and "temperature" in str(e) for e in stock["chain"]):
        print(
            "REPRODUCED: with the Anthropic plugin, the judge's temperature=0.0 reaches "
            "messages.create() as a keyword it does not accept; TypeError before any "
            "request is sent, so every LLM judge fails"
        )
        return 1
    print("INCONCLUSIVE: the stock judge sent nothing, but not for the expected TypeError")
    return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
