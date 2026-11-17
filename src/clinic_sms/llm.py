"""Claude wiring for the clinic SMS agent."""

from __future__ import annotations

from typing import Any

from livekit.agents import llm
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.plugins import anthropic

MODEL = "claude-opus-5-5"
# Thinking is always on for this model, so leave room beyond the plugin's 1024 default.
MAX_TOKENS = 8000


def make_claude_llm() -> anthropic.LLM:
    """Build the Claude LLM. The API key comes from ANTHROPIC_API_KEY; no temperature is set."""
    return anthropic.LLM(model=MODEL, max_tokens=MAX_TOKENS)


# SDK-FINDING-9 (docs/findings.md): every LLM judge of livekit-agents 1.8.11 passes
# `extra_kwargs={"temperature": 0.0}` to `chat()`. The Anthropic plugin merges
# `extra_kwargs` into the keyword arguments of `messages.create()`, which in anthropic 1.x
# has no `temperature`: the call raises TypeError before any request is sent, and the judge
# fails after four attempts. The judge drops it, and runs without a temperature like the
# agents do.
class JudgeLLM(anthropic.LLM):
    """The plugin's LLM, minus the `temperature` the SDK's judges pass in `extra_kwargs`."""

    def chat(  # type: ignore[override]
        self, *, extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN, **kwargs: Any
    ) -> llm.LLMStream:
        if isinstance(extra_kwargs, dict) and "temperature" in extra_kwargs:
            extra_kwargs = {k: v for k, v in extra_kwargs.items() if k != "temperature"}
        return super().chat(extra_kwargs=extra_kwargs, **kwargs)


def make_judge_llm() -> llm.LLM:
    """Build Claude for the SDK's LLM judges (the evals).

    Same model and max_tokens as the agents, and no temperature (SDK-FINDING-9). The API
    key comes from ANTHROPIC_API_KEY.
    """
    return JudgeLLM(model=MODEL, max_tokens=MAX_TOKENS)
