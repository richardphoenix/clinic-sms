# `ResumableTaskGroup.on_enter` is copied from `TaskGroup.on_enter` in livekit-agents
# 1.8.11 (livekit/agents/beta/workflows/task_group.py, lines 92-164) and changed in the
# four places listed further down. Upstream's notice for that code:
#
# Copyright 2023 LiveKit, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# SDK-FINDING-1: the stock TaskGroup cannot start from saved progress. The task stack,
# the results and the visited set are set up inside `on_enter` (task_stack and
# task_results are locals, lines 93-94), so a subclass has to copy the whole method.
#
# Differences from upstream `TaskGroup.on_enter` (1.8.11 line numbers), each marked
# `# SDK-FINDING-<n> (diff <k>)` in the code; everything else is upstream's code,
# unchanged:
#
#   1. With `progress`: `task_stack = list(progress.pending)` (upstream: every registered
#      id, line 93); `task_results` restored from `progress.results` with each step's
#      `result_type.model_validate` (upstream: `{}`, line 94);
#      `self._visited_tasks = set(progress.visited)` (upstream: empty from `__init__`,
#      line 69, filled at line 111).
#   2. Only the first task of a resume from `progress` is built with `factory(True)`;
#      every other task is built with `factory(False)` through the registered
#      `_FactoryInfo.task_factory`, as upstream does (line 100).
#   3. That resumed first task gets `step_chat_ctx`, when given, instead of
#      `self.chat_ctx.copy()` (lines 102-103), which would overwrite the task's own
#      context with the group's. SDK-FINDING-2.
#   4. Before each task starts: the current step id is updated and `pending` is set to
#      the current step plus the rest of the stack. After each task completes: its
#      result's JSON (`model_dump(mode="json")`) is written to `results`.
#      Upstream keeps neither.
#
# Private SDK names used:
#   - `_FactoryInfo` (task_group.py:22-26): `add_step` puts one into the inherited
#     `_registered_factories`, so the inherited `_build_out_of_scope_tool`
#     (lines 166-201) builds the tool from it. That method is overridden only to skip
#     the tool when no step is left to go back to (SDK-FINDING-7).
#   - `_OutOfScopeError` (task_group.py:41-43): caught in the copied loop (line 129).
#   - Inherited private attributes the copied body uses as upstream does:
#     `_registered_factories`, `_visited_tasks`, `_current_task`, `_chat_ctx`,
#     `_summarize_chat_ctx`, `_return_exceptions`, `_task_completed_callback`,
#     `_build_out_of_scope_tool`, and `ChatContext._summarize`.
"""A TaskGroup that can start from saved progress (see the header above)."""

from __future__ import annotations

import copy
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Self

from livekit.agents import AgentTask, FunctionTool, llm
from livekit.agents.beta.workflows import TaskCompletedEvent, TaskGroup, TaskGroupResult
from livekit.agents.beta.workflows.task_group import _FactoryInfo, _OutOfScopeError
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from pydantic import BaseModel

from clinic_sms.state import GroupProgress

StepFactory = Callable[[bool], AgentTask]  # argument: resuming


class UnknownStepError(ValueError):
    """Saved progress names a step id that is not registered in the group."""


@dataclass
class _Step:
    factory: StepFactory
    result_type: type[BaseModel]


class ResumableTaskGroup(TaskGroup):
    """`TaskGroup` that can start from a `GroupProgress` and reports its progress live.

    Register steps with `add_step`, not the inherited `add`. Each step's result must be
    an instance of its `result_type`, so it can be stored as JSON and restored.
    `step_chat_ctx` is used only for the first step of a resume from `progress`.
    """

    def __init__(
        self,
        *,
        progress: GroupProgress | None = None,
        step_chat_ctx: llm.ChatContext | None = None,
        summarize_chat_ctx: bool = True,
        chat_ctx: NotGivenOr[llm.ChatContext] = NOT_GIVEN,
        on_task_completed: Callable[[TaskCompletedEvent], Coroutine[None, None, None]]
        | None = None,
    ) -> None:
        super().__init__(
            summarize_chat_ctx=summarize_chat_ctx,
            chat_ctx=chat_ctx,
            on_task_completed=on_task_completed,
        )
        self._resume_from = progress.model_copy(deep=True) if progress is not None else None
        self._step_chat_ctx = step_chat_ctx
        self._steps: dict[str, _Step] = {}
        self._current_task: AgentTask | None = None
        self._current_step_id: str | None = None
        self._pending: list[str] | None = None  # None until on_enter starts a step
        self._results: dict[str, dict] = (
            copy.deepcopy(progress.results) if progress is not None else {}
        )

    def add_step(
        self,
        factory: StepFactory,
        *,
        id: str,
        description: str,
        result_type: type[BaseModel],
    ) -> Self:
        """Register a step. `factory(resuming)` builds its task.

        `description` tells the LLM when to regress to this step (the out_of_scope tool).
        """
        self._steps[id] = _Step(factory=factory, result_type=result_type)
        self._registered_factories[id] = _FactoryInfo(
            task_factory=lambda: factory(False), id=id, description=description
        )
        return self

    def validate_progress(self) -> None:
        """Check that `on_enter` can start from the saved progress; raise ValueError if not.

        Raises:
            UnknownStepError: `pending`, `results` or `visited` names a step id that is
                not registered.
            pydantic.ValidationError: a stored result no longer parses with its step's
                `result_type` (for example after a schema change).
        """
        p = self._resume_from
        if p is None:
            return
        named = dict.fromkeys([*p.pending, *p.results, *p.visited])
        unknown = [step_id for step_id in named if step_id not in self._steps]
        if unknown:
            raise UnknownStepError(f"progress names unregistered steps: {unknown}")
        # the same restore that on_enter does (diff 1), so it fails here, before start
        for step_id, data in p.results.items():
            self._steps[step_id].result_type.model_validate(data)

    @property
    def progress(self) -> GroupProgress:
        """Live snapshot of the progress; `pending` lists the current step first.

        Before the first step starts, this is the progress the group will start from.
        """
        if self._pending is None:
            if self._resume_from is not None:
                return self._resume_from.model_copy(deep=True)
            return GroupProgress(pending=list(self._steps), results={}, visited=[])
        return GroupProgress(
            pending=list(self._pending),
            results=copy.deepcopy(self._results),
            # registration order, so the snapshot does not depend on set ordering
            visited=[step_id for step_id in self._steps if step_id in self._visited_tasks],
        )

    @property
    def current_task(self) -> AgentTask | None:
        return self._current_task

    @property
    def current_step_id(self) -> str | None:
        return self._current_step_id

    # SDK-FINDING-7: upstream returns None only when nothing has been visited
    # (task_group.py:167-168), then discards the active step from the targets (line 172).
    # Stock on_enter builds the tool before it adds the active step to the visited set
    # (lines 105 and 111), so the active step is never the only visited one. A group
    # resumed in its first visited step is in exactly that state, and upstream would
    # offer the tool with `"enum": []`, which Anthropic rejects with a 400. Same result
    # as upstream in every state the stock flow reaches.
    def _build_out_of_scope_tool(self, *, active_task_id: str) -> FunctionTool | None:
        if not self._visited_tasks - {active_task_id}:
            return None
        return super()._build_out_of_scope_tool(active_task_id=active_task_id)

    # SDK-FINDING-1: copy of upstream `TaskGroup.on_enter` (1.8.11, task_group.py:92-164)
    # with the four `(diff <k>)` changes listed in the file header.
    async def on_enter(self) -> None:
        # SDK-FINDING-1 (diff 1): start from the saved progress
        progress = self._resume_from
        if progress is not None:
            task_stack = list(progress.pending)
            task_results: dict[str, Any] = {
                task_id: self._steps[task_id].result_type.model_validate(data)
                for task_id, data in progress.results.items()
            }
            self._visited_tasks = set(progress.visited)
        else:
            task_stack = list(self._registered_factories.keys())
            task_results = {}
        # SDK-FINDING-1 (diff 2): only the first task of a resume is built with resuming=True
        resuming = progress is not None

        while len(task_stack) > 0:
            task_id = task_stack.pop(0)
            factory_info = self._registered_factories[task_id]

            # SDK-FINDING-1 (diff 4): record the step that is starting
            self._current_step_id = task_id
            self._pending = [task_id, *task_stack]

            # SDK-FINDING-1 (diff 2)
            if resuming:
                self._current_task = self._steps[task_id].factory(True)
            else:
                self._current_task = factory_info.task_factory()

            # SDK-FINDING-2 (diff 3): upstream always passes the group's context here,
            # which replaces whatever context the task was built with
            if resuming and self._step_chat_ctx is not None:
                shared_chat_ctx = self._step_chat_ctx
            else:
                shared_chat_ctx = self.chat_ctx.copy()
            resuming = False
            await self._current_task.update_chat_ctx(shared_chat_ctx)

            if out_of_scope_tool := self._build_out_of_scope_tool(active_task_id=task_id):
                current_tools = self._current_task.tools
                current_tools.append(out_of_scope_tool)
                await self._current_task.update_tools(current_tools)

            try:
                self._visited_tasks.add(task_id)
                res = await self._current_task

                # AgentTask handoff merges omit function calls. Re-merge the completed
                # task context so task-group summarization can incorporate tool results.
                self._chat_ctx.merge(
                    self._current_task.chat_ctx.copy(),
                    exclude_instructions=True,
                )

                task_results[task_id] = res
                # SDK-FINDING-1 (diff 4): record the result as JSON
                self._results[task_id] = res.model_dump(mode="json")

                if self._task_completed_callback is not None:
                    await self._task_completed_callback(
                        TaskCompletedEvent(
                            agent_task=self._current_task, task_id=task_id, result=res
                        )
                    )
            except _OutOfScopeError as e:
                task_stack.insert(0, task_id)
                for task_id in reversed(e.target_task_ids):
                    task_stack.insert(0, task_id)
                continue
            except Exception as e:
                if self._return_exceptions:
                    task_results[task_id] = e
                    continue
                else:
                    self.complete(e)
                    return

        if self._summarize_chat_ctx:
            try:
                assert isinstance(self.session.llm, llm.LLM), (
                    "llm must be a LLM instance to summarize the chat_ctx"
                )

                # when a task is done, the chat_ctx is going to be merged with the "caller" chat_ctx
                # enabling summarization will result on only one ChatMessage added.
                # keep every item to allow summarization to be more action-aware.
                summarized_chat_ctx = await self.chat_ctx.copy(
                    exclude_instructions=False,
                    exclude_handoff=False,
                    exclude_config_update=False,
                    exclude_empty_message=False,
                    exclude_function_call=False,
                )._summarize(llm_v=self.session.llm, keep_last_turns=0)

                await self.update_chat_ctx(summarized_chat_ctx)
            except Exception as e:
                self.complete(e)
                return

        self.complete(TaskGroupResult(task_results=task_results))
