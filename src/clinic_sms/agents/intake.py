"""The intake agent: three AgentTask steps run by a ResumableTaskGroup."""

from __future__ import annotations

import copy
import logging
from typing import ClassVar

from livekit.agents import Agent, AgentTask, RunContext, function_tool, llm
from livekit.agents.llm import ToolError
from pydantic import BaseModel, ConfigDict

from clinic_sms.agents.common import (  # SMS_STYLE re-exported
    ESCALATION_RULE,
    OTHER_ASSISTANTS,
    SMS_STYLE,
    escalate_to_staff,
)
from clinic_sms.ctx import dump_ctx, load_ctx
from clinic_sms.fmt import mask_phone
from clinic_sms.logsafe import describe_exception
from clinic_sms.state import IntakeState
from clinic_sms.turn import Turn
from clinic_sms.workflow import ReadySignal, ResumableTaskGroup

logger = logging.getLogger(__name__)

# Shared by every step. A later step sees earlier steps' conversation but has none of
# their tools; without this the model disowned earlier saves (Spike 2).
_SCOPE = (
    "Earlier parts of this conversation were handled by other steps, and what they "
    "recorded is already saved. Do not comment on, re-confirm or apologize for earlier "
    "questions or answers. Handle only your own topic."
)


class InsuranceInfo(BaseModel):
    # values are the patient's words: keep them out of error messages and logs
    model_config = ConfigDict(hide_input_in_errors=True)

    carrier: str
    member_id: str


class VisitReason(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    reason: str


class AllergiesMeds(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    allergies: list[str]
    medications: list[str]


# Each step's record_* tool returns None. A tool that returns a value asks for a reply:
# the SDK calls the LLM again with its output (voice/generation.py:1241,
# `reply_required = fnc_out is not None`), and that reply would be one more SMS before
# the next step's question (the two SMS per step change in Spike 2).
class IntakeStep[ResultT](AgentTask[ResultT]):
    OPENING: ClassVar[str]

    def __init__(self, *, instructions: str, resuming: bool, ready: ReadySignal) -> None:
        # Every step can escalate a question it cannot answer (spec §5.4); the rule and the
        # tool are added together here.
        super().__init__(
            instructions=f"{instructions} {ESCALATION_RULE}", tools=[escalate_to_staff]
        )
        self._resuming = resuming
        self._ready = ready

    async def on_enter(self) -> None:
        self._ready.set(self)  # first statement, before any await
        if not self._resuming:
            self.session.generate_reply(instructions=self.OPENING)


class InsuranceTask(IntakeStep[InsuranceInfo]):
    OPENING = "Ask the patient for their insurance carrier and member ID."

    def __init__(self, *, resuming: bool, ready: ReadySignal) -> None:
        super().__init__(
            instructions=(
                f"{SMS_STYLE} {_SCOPE} Your topic: the patient's insurance carrier and "
                "member ID. Call record_insurance only once you have both."
            ),
            resuming=resuming,
            ready=ready,
        )

    @function_tool
    async def record_insurance(self, ctx: RunContext, carrier: str, member_id: str) -> None:
        """Record the patient's insurance carrier and member ID."""
        self.complete(InsuranceInfo(carrier=carrier, member_id=member_id))


class VisitReasonTask(IntakeStep[VisitReason]):
    OPENING = "Ask the patient what brings them in for this visit."

    def __init__(self, *, resuming: bool, ready: ReadySignal) -> None:
        super().__init__(
            instructions=(
                f"{SMS_STYLE} {_SCOPE} Your topic: the reason for the patient's visit. "
                "Call record_visit_reason only once you have the reason."
            ),
            resuming=resuming,
            ready=ready,
        )

    @function_tool
    async def record_visit_reason(self, ctx: RunContext, reason: str) -> None:
        """Record the reason for the patient's visit."""
        self.complete(VisitReason(reason=reason))


class AllergiesMedsTask(IntakeStep[AllergiesMeds]):
    OPENING = "Ask the patient about any allergies and the medications they currently take."

    def __init__(self, *, resuming: bool, ready: ReadySignal) -> None:
        super().__init__(
            instructions=(
                f"{SMS_STYLE} {_SCOPE} Your topic: the patient's allergies and current "
                "medications. Call record_allergies_and_medications only once you have "
                "both (an empty list is fine if the patient has none)."
            ),
            resuming=resuming,
            ready=ready,
        )

    @function_tool
    async def record_allergies_and_medications(
        self, ctx: RunContext, allergies: list[str], medications: list[str]
    ) -> None:
        """Record the patient's allergies and current medications."""
        self.complete(AllergiesMeds(allergies=allergies, medications=medications))


INTAKE_STEPS = [  # (id, description, task class, result type) in order
    (
        "insurance",
        "Collect the patient's insurance carrier and member ID",
        InsuranceTask,
        InsuranceInfo,
    ),
    ("visit_reason", "Collect the reason for the visit", VisitReasonTask, VisitReason),
    (
        "allergies_meds",
        "Collect allergies and current medications",
        AllergiesMedsTask,
        AllergiesMeds,
    ),
]


class IntakeAgent(Agent):
    COMPLETION = (
        "Tell the patient their intake is complete and the clinic will see them at their "
        "appointment."
    )

    def __init__(
        self,
        *,
        state: IntakeState,
        ready: ReadySignal,
        summarize: bool = True,
        turn: Turn | None = None,
    ) -> None:
        """`turn`: the session's userdata, when the runner builds this agent; on_enter then
        makes this agent `turn.active`.

        Each stored context layer is parsed here, so bad stored state raises before the
        session starts.
        """
        super().__init__(
            instructions=(
                f"{SMS_STYLE} The patient's intake for their appointment is handled here. "
                f"{OTHER_ASSISTANTS}"
            ),
            chat_ctx=load_ctx(state.root_ctx) if state.root_ctx is not None else None,
        )
        self.state = state
        self._ready = ready
        self._turn = turn
        self._closing = False
        self._failed = False  # set by on_enter when the group fails
        self.group: ResumableTaskGroup | None = None
        self._group_ctx: llm.ChatContext | None = None
        if state.done:
            return
        if state.group_ctx is not None:
            # not passed as the group's chat_ctx: Agent.__init__ drops function calls of
            # tools the agent does not have (voice/agent.py:92), and a TaskGroup has none,
            # yet its context holds every finished step's calls (TaskGroup.on_enter
            # re-merges them for its summary, task_group.py:114-119). on_enter restores it
            # with exclude_invalid_function_calls=False instead.
            self._group_ctx = load_ctx(state.group_ctx)
        group = ResumableTaskGroup(
            progress=state.progress,
            step_chat_ctx=load_ctx(state.step_ctx) if state.step_ctx is not None else None,
            summarize_chat_ctx=summarize,
        )
        for step_id, description, task_cls, result_type in INTAKE_STEPS:
            group.add_step(
                # bind the loop variable; default args avoid late binding
                lambda resuming, task_cls=task_cls: task_cls(resuming=resuming, ready=ready),
                id=step_id,
                description=description,
                result_type=result_type,
            )
        group.validate_progress()  # bad saved state raises before the session starts
        self.group = group

    def snapshot(self) -> IntakeState:
        """What to store at the end of a message: call after a run, before session.aclose().

        Not done: the group's progress and the three context layers (this agent, the
        group, the current step). Done: the results and this agent's context.
        """
        if self.state.done:
            return IntakeState(
                done=True,
                results=copy.deepcopy(self.state.results),
                root_ctx=dump_ctx(self.chat_ctx),
            )
        if self._failed:
            # Ended without finishing the intake, before the session closed: the group
            # failed (e.g. its summary call), and its progress may list an answered step
            # as pending.
            raise RuntimeError("snapshot() after the intake group failed")
        if self.group is None or self.group.current_task is None:
            raise RuntimeError("snapshot() before the intake's first step has started")
        return IntakeState(
            progress=self.group.progress,
            root_ctx=dump_ctx(self.chat_ctx),
            group_ctx=dump_ctx(self.group.chat_ctx),
            step_ctx=dump_ctx(self.group.current_task.chat_ctx),
        )

    def mark_closing(self) -> None:
        """Call right before closing the session: the end of every message.

        Closing cancels the step that waits for the next answer, and the cancellation
        reaches on_enter as a plain ToolError (SDK-FINDING-4). After this call, on_enter
        treats that as a normal exit instead of a failure.
        """
        self._closing = True

    async def on_enter(self) -> None:
        if self._turn is not None:
            self._turn.active = self  # first statement, before any await
        if self.state.done:
            self._ready.set(self)  # before any await
            return
        assert self.group is not None
        # no reply before or while the group runs: on resume the step must speak first
        # (and only if it was not resuming)
        try:
            if self._group_ctx is not None:
                # the group has not started, so this only replaces its context
                # (voice/agent.py:268-272); every function call is kept
                group_ctx, self._group_ctx = self._group_ctx, None  # used once
                await self.group.update_chat_ctx(group_ctx, exclude_invalid_function_calls=False)
            results = await self.group
        except Exception as e:
            if isinstance(e, ToolError) and self._closing:  # SDK-FINDING-4
                # Normal end of every inbound SMS: closing the session cancels the step
                # that waits for the next answer (AgentTask.cancel completes it with a
                # ToolError, voice/agent.py:945-950), and the group re-raises it. The
                # SDK has no cancellation type, so the owner says it is closing
                # (mark_closing). Known limit: a genuine ToolError raised after that
                # call is also taken for the close. Keep the state as it is; no reply,
                # no error.
                return
            # SDK-FINDING-6: the group failed (e.g. its summary call). Not re-raised: the
            # SDK logs an exception from a root agent's on_enter and leaves it in a task
            # nobody awaits (voice/agent_activity.py:1080-1101), so asyncio logs it again
            # whenever that task is collected. Keep the state as it is; snapshot() raises,
            # so the caller stores nothing for this turn.
            self._failed = True
            # type name and traceback only: the exception's text may hold the patient's
            # words (logsafe.describe_exception)
            if self._turn is not None:
                logger.error(
                    "the intake group failed for %s: %s",
                    mask_phone(self._turn.phone),
                    describe_exception(e),
                )
            else:
                logger.error("the intake group failed: %s", describe_exception(e))
            return
        self.state.done = True
        self.state.results = {
            step_id: result.model_dump(mode="json")
            for step_id, result in results.task_results.items()
        }
        self.state.progress = None
        self.state.group_ctx = None
        self.state.step_ctx = None
        self.session.generate_reply(instructions=self.COMPLETION)
