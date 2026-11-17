"""The signal a cold-started session waits on before it is fed the inbound text."""

from __future__ import annotations

import asyncio

from livekit.agents import Agent


# SDK-FINDING-3 (docs/findings.md): the SDK has no documented "the innermost agent has
# taken over" signal. A plain `session.start()` returns while the handoff chain is still
# running, and `current_agent is step` turns true too early. Feeding
# `session.run(user_input=...)` before the step's `on_enter` has been called can raise
# "speech scheduling is draining" or let the TaskGroup answer instead.
# The alternative is `start(capture_run=True)`: awaiting its RunResult waits for every
# layer's `on_enter`, and its `events` record the handoffs. That behavior is not
# documented and upstream keeps changing it, so readiness is an explicit contract here.
class ReadySignal:
    """Set by the agent that should receive the next user input, once it has taken over.

    That agent calls `ready.set(self)` as the first statement of its `on_enter`,
    before any `await`. By then its activity is the session's current activity and its
    speech scheduling is running. In 1.8.11: voice/agent_session.py:1989 swaps the
    activity and line 2014 starts it; voice/agent_activity.py:1352 resumes scheduling,
    then line 1096 creates the task that calls `on_enter` (line 1088).
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._agent: Agent | None = None

    def set(self, agent: Agent) -> None:
        """Mark `agent` as ready. Only the first call counts; later calls are ignored."""
        if self._agent is None:
            self._agent = agent
            self._event.set()

    async def wait(self, timeout: float) -> Agent:
        """Return the agent from the first `set()`. Raises TimeoutError after `timeout` seconds."""
        await asyncio.wait_for(self._event.wait(), timeout)
        assert self._agent is not None
        return self._agent
