"""Which top-level agent handles a conversation, and what to store after its turn.

The runner restores the stored active agent with `restore_agent` and stores
`conversation_snapshot` after the turn. Agents hand off through `Turn.handoff`, which
calls `new_agent`; the Turn carries it, so agent modules need not import this one.
"""

from __future__ import annotations

from livekit.agents import Agent, llm

from clinic_sms.agents.intake import IntakeAgent
from clinic_sms.agents.reception import ReceptionAgent
from clinic_sms.agents.reschedule import RescheduleAgent
from clinic_sms.ctx import dump_ctx, load_ctx
from clinic_sms.effects import SaveIntakeResults
from clinic_sms.state import AgentName, ConversationState, IntakeState
from clinic_sms.turn import Turn


def new_agent(turn: Turn, name: AgentName, chat_ctx: llm.ChatContext) -> Agent:
    """A handoff target: agent `name`, starting fresh with the conversation so far."""
    if name == "intake":
        return IntakeAgent(
            state=IntakeState(root_ctx=dump_ctx(chat_ctx)),
            ready=turn.ready,
            summarize=turn.summarize_intake,
            turn=turn,
        )
    if name == "reception":
        return ReceptionAgent(turn=turn, chat_ctx=chat_ctx)
    if name == "reschedule":
        return RescheduleAgent(turn=turn, chat_ctx=chat_ctx, resuming=False)
    raise ValueError(f"no agent to hand off to: {name}")


def restore_agent(state: ConversationState, turn: Turn) -> Agent:
    """The stored active agent, rebuilt for this turn.

    Raises ValueError for a state with no active agent, or one the code no longer
    accepts; this happens before the session starts.
    """
    name = state.active_agent
    if name is None:
        raise ValueError("no active agent")
    if name == "intake":
        return IntakeAgent(
            state=state.intake, ready=turn.ready, summarize=turn.summarize_intake, turn=turn
        )
    if name == "reception":
        return ReceptionAgent(
            turn=turn, chat_ctx=load_ctx(state.agent_ctx) if state.agent_ctx else None
        )
    if name == "reschedule":
        return RescheduleAgent(
            turn=turn,
            chat_ctx=load_ctx(state.agent_ctx) if state.agent_ctx else None,
            resuming=True,
        )
    raise ValueError(f"cannot restore agent: {name}")


def conversation_snapshot(state: ConversationState, turn: Turn) -> ConversationState:
    """The state to commit for this turn: `state` as loaded, updated from `turn.active`.

    Call after the run and before the session closes. May add effects to `turn.effects`.
    Raises RuntimeError if no agent took over, or the active agent cannot be stored.
    """
    agent = turn.active
    if agent is None:
        raise RuntimeError("no agent took over this turn")
    if isinstance(agent, (ReceptionAgent, RescheduleAgent)):
        return state.model_copy(
            update={
                "active": agent.NAME,
                "agent_ctx": dump_ctx(agent.chat_ctx),
                "intake": turn.intake,
                "appointment_id": turn.appointment_id,
            }
        )
    if isinstance(agent, IntakeAgent) and agent.state.done:
        # The intake is finished: reception takes over from the next message. Done through
        # the stored state, not an update_agent in on_enter, so the handoff cannot race
        # the end of the turn.
        # turn.intake is the stored state: the agent has mutated its own copy by now
        if turn.appointment_id is not None and not turn.intake.done:
            turn.effects.append(SaveIntakeResults(turn.appointment_id, agent.state.results))
        return state.model_copy(
            update={
                "active": "reception",
                "agent_ctx": dump_ctx(agent.chat_ctx),
                "intake": IntakeState(done=True, results=agent.state.results),
                "appointment_id": turn.appointment_id,
            }
        )
    if isinstance(agent, IntakeAgent):
        return state.model_copy(
            update={
                "active": "intake",
                "agent_ctx": None,
                "intake": agent.snapshot(),
                "appointment_id": turn.appointment_id,
            }
        )
    raise RuntimeError(f"cannot snapshot agent: {type(agent).__qualname__}")
