# SDK issue log (findings)

This file records the problems this project ran into on livekit-agents, and the conclusions of the spikes.

Short commit hashes of this repo (for example "commit ccb0185") refer to the private development history, which was squashed into a single commit when the repo was published; they no longer resolve here. Hashes and links into livekit/agents are unaffected.

## Entry format

One entry per problem, titled `## FINDING-<n>: <title>`, followed by, in order:

- **Symptom**: what was observed.
- **Minimal repro** (a standalone script that does not depend on this project's code).
- **SDK version**: for example livekit-agents 1.8.11.
- **Root cause**: why it happens.
- **Classification**: SDK bug / uncommon usage on our side / feature gap. A problem that the stock flow cannot reach, and that only triggers when a subclass changes the SDK's internal state, is recorded as a "latent defect".
- **Workaround**: the places in the code marked with `# SDK-FINDING-<n>`.
- **Upstream status**: search existing issues and PRs first; include links and the search date.

## Index

Where each finding stands upstream, as of 2026-11-17. Details, links and search records are in each entry's upstream status. FINDING-10 to 14 come from the text-channel spike ([docs/spikes/text-channel.md](spikes/text-channel.md)); its "Upstream search method" and "Status on main" notes apply to all five.

| # | Problem | Classification | Upstream (livekit/agents unless noted) |
|---|---|---|---|
| 1 | TaskGroup cannot start from saved progress | Feature gap | Issue [#8595](https://github.com/livekit/agents/issues/8595), PR [#8608](https://github.com/livekit/agents/pull/8608) (both open) |
| 2 | TaskGroup discards a step's own `chat_ctx` | Uncommon usage | Covered by #8595 and #8608 |
| 3 | No ready signal after `start()`; an early `run()` raises or is taken by the TaskGroup | Feature gap (contract) | Not filed |
| 4 | A cancelled AgentTask raises only a plain `ToolError` | Feature gap | Issue [#8611](https://github.com/livekit/agents/issues/8611), PR [#8612](https://github.com/livekit/agents/pull/8612) (both open) |
| 5 | Each activation appends an `agent_config_update` | Uncommon usage | Not filed |
| 6 | The root agent's `on_enter` exception is logged twice | SDK bug (logging only) | Not filed. agents-js has the inverse: issue [agents-js#3016](https://github.com/livekit/agents-js/issues/3016), PR [agents-js#3017](https://github.com/livekit/agents-js/pull/3017) (both open) |
| 7 | A resumed TaskGroup offers `out_of_scope` with an empty enum | Latent defect | PR [#8594](https://github.com/livekit/agents/pull/8594) (open) |
| 8 | A failed reply in a handoff turn does not fail `session.run()` | SDK bug | Not filed; open PR #7957 is related. agents-js: PR [agents-js#3013](https://github.com/livekit/agents-js/pull/3013), issue [agents-js#3014](https://github.com/livekit/agents-js/issues/3014), PR [agents-js#3015](https://github.com/livekit/agents-js/pull/3015) (all open) |
| 9 | With the Anthropic plugin as judge, every LLM judge fails on `temperature` | SDK bug | Issue [#8585](https://github.com/livekit/agents/issues/8585) (closed), PR [#8586](https://github.com/livekit/agents/pull/8586) (merged) |
| 10 | A text during an uninterruptible speech is dropped | SDK bug | Issue [#8606](https://github.com/livekit/agents/issues/8606), PR [#8607](https://github.com/livekit/agents/pull/8607) (both open), together with 11 |
| 11 | A text during a handoff is dropped, and the old reply is cut | SDK bug | Issue [#8606](https://github.com/livekit/agents/issues/8606), PR [#8607](https://github.com/livekit/agents/pull/8607) (both open), together with 10 |
| 12 | Texts that arrive together are handled concurrently | SDK bug | Not filed |
| 13 | Text-only: what the agent says before a participant is linked is lost, yet recorded as said | SDK bug | Not filed |
| 14 | A text sent right after joining is dropped | SDK bug | Not filed |

## FINDING-1: TaskGroup cannot start from saved progress

- **Symptom**: on each SMS cold start, the TaskGroup has to be rebuilt from the progress in the database (results of completed steps, visited steps, current step) and continue directly from the current step. The stock `TaskGroup` has no parameter or method that can do this: it always starts from the first registered step, its results start as an empty dict, and its visited steps start as an empty set. Spike 1/2 could only simulate resuming by "registering only the steps not done yet", with these consequences:
  - The results of completed steps are lost. In Spike 2 T3, `root.result` is `TaskGroupResult(task_results={'b': 'Portland'})`, with no `'a'`.
  - `out_of_scope` cannot list the earlier steps: the visited set is empty, so `_build_out_of_scope_tool` returns `None` right away at lines 167–168.
- **Minimal repro**: `repro/finding_1.py`. A standalone script that depends only on livekit-agents, with an inline fake LLM; no API key needed. Run `uv run python repro/finding_1.py`. The exit code is 1 while it still reproduces. Two steps, a and b, each with a `save` tool that completes the step; neither `on_enter` says anything. The control group completes a and then b in one long-lived session; the cold-start group simulates "a is done, restart between the two steps", where the only thing the stock API can do is create a new TaskGroup that registers only the remaining b. The script also prints the parameters of `TaskGroup.__init__`, to show there is no entry point for passing progress. Output (2026-11-13, identical across 3 consecutive runs):

  ```
  livekit-agents 1.8.11
  TaskGroup.__init__ parameters: ['summarize_chat_ctx', 'return_exceptions', 'chat_ctx', 'on_task_completed']
  long-lived, a then b       : step b offered tools ['out_of_scope', 'save'], out_of_scope targets ['a']; result {'a': 'a1', 'b': 'b1'}
  cold start, only b left    : step b offered tools ['save'], out_of_scope targets None; result {'b': 'b1'}
  REPRODUCED: started from 'only the remaining steps', the stock TaskGroup's result lacks the completed step and out_of_scope cannot offer it
  ```

  The script shows that the only workable way to resume with the stock API loses data; "there is no other entry point" is shown by the source lines in the root cause below.
- **SDK version**: livekit-agents 1.8.11 (PyPI), Python 3.13.
- **Root cause**: progress lives in local variables of `on_enter`, the visited set only grows inside `on_enter`, and the step factories take no arguments, so there is no place to hook in. A subclass can only copy the whole `on_enter`, and has to import private names to do it. The links below point to the GitHub tag `livekit-agents@1.8.11`; that file is byte-for-byte identical to the `beta/workflows/task_group.py` installed in `.venv` (checked with `diff` on 2026-11-12).
  - [Lines 47–71](https://github.com/livekit/agents/blob/livekit-agents%401.8.11/livekit-agents/livekit/agents/beta/workflows/task_group.py#L47-L71): the constructor accepts only `summarize_chat_ctx`, `return_exceptions`, `chat_ctx` and `on_task_completed`. Line 69 sets `_visited_tasks` to an empty set.
  - [Lines 93–94](https://github.com/livekit/agents/blob/livekit-agents%401.8.11/livekit-agents/livekit/agents/beta/workflows/task_group.py#L93-L94): `task_stack = list(self._registered_factories.keys())`, `task_results: dict[str, Any] = {}`. Both are local variables, built from scratch on every entry.
  - [Line 100](https://github.com/livekit/agents/blob/livekit-agents%401.8.11/livekit-agents/livekit/agents/beta/workflows/task_group.py#L100): `self._current_task = factory_info.task_factory()`. The factory takes no arguments, so there is no way to tell the first step "this is a resume, don't ask again".
  - [Line 111](https://github.com/livekit/agents/blob/livekit-agents%401.8.11/livekit-agents/livekit/agents/beta/workflows/task_group.py#L111): the visited set grows only here; `_build_out_of_scope_tool` at [lines 166–175](https://github.com/livekit/agents/blob/livekit-agents%401.8.11/livekit-agents/livekit/agents/beta/workflows/task_group.py#L166-L175) only allows going back to steps in this set.
  - [Line 164](https://github.com/livekit/agents/blob/livekit-agents%401.8.11/livekit-agents/livekit/agents/beta/workflows/task_group.py#L164): `self.complete(TaskGroupResult(task_results=task_results))`; the result comes only from the steps completed in this one `on_enter`.
  - The only thing a subclass can replace is the whole `on_enter`. Copying it requires importing the private names [`_FactoryInfo` (lines 22–26)](https://github.com/livekit/agents/blob/livekit-agents%401.8.11/livekit-agents/livekit/agents/beta/workflows/task_group.py#L22-L26) and [`_OutOfScopeError` (lines 41–43)](https://github.com/livekit/agents/blob/livekit-agents%401.8.11/livekit-agents/livekit/agents/beta/workflows/task_group.py#L41-L43).
- **Classification**: feature gap. TaskGroup is designed around "one session runs from start to finish"; the current behavior is not wrong in itself, it just lacks the ability to "start from saved progress".
- **Workaround**: `src/clinic_sms/workflow/resumable_task_group.py` (`# SDK-FINDING-1`). `ResumableTaskGroup` copies the 1.8.11 `on_enter` and changes only the four places listed in the file header: diffs 1, 2 and 4 belong to this finding, diff 3 belongs to FINDING-2. Restoring the visited set lets the inherited `_build_out_of_scope_tool` reach a state the stock flow cannot reach; see FINDING-7. Tests are in `tests/test_resumable_task_group.py`.
- **Upstream status**: searched 2026-11-12; no related issue or PR. The search used read-only `gh search issues` / `gh search prs` (`--repo livekit/agents`); both issues and PRs were searched, except for the terms marked "issues only". Search terms: `TaskGroup` (all 10 issues read; titles of the first 30 PRs read), `TaskGroup resume` (issues only), `resume TaskGroup`, `task group resume`, `TaskGroup progress` (issues only), `TaskGroup restore` (issues only), `TaskGroup state` (issues only), `TaskGroup persist` (issues only), `TaskGroup saved`, `TaskGroup skip completed`, `TaskGroup visited`, `TaskGroup initial`, `TaskGroup serialize`, `restore task results`, `task_results restore`, `resume AgentTask`, `persist AgentTask`, `workflow resume`, `checkpoint`, `restore session`, `serialize session state`. The two closest are both a different problem:
  - [#5697](https://github.com/livekit/agents/issues/5697) Conditional Task Group (open): choosing steps by condition; does not involve resuming.
  - PR [#7556](https://github.com/livekit/agents/pull/7556) (open): keeps the results when summarization fails.

  On 2026-11-17, after the user approved, filed issue [#8595](https://github.com/livekit/agents/issues/8595) "TaskGroup: resume from saved progress in a new session" (feature request template). CONTRIBUTING asks for an issue to discuss scope before a new feature, so this time only an issue was opened, no PR. It covers this finding and FINDING-2: the use case, what is missing (links point to main `0cfc030`, where `task_group.py` is byte-for-byte identical to 1.8.11), a draft API of a progress snapshot plus a constructor parameter that leaves the default behavior unchanged, and the `ResumableTaskGroup` prototype and its tests. PR [#8594](https://github.com/livekit/agents/pull/8594) (FINDING-7) is a prerequisite for this capability. The draft is in `var/upstream/drafts/finding-1-issue.md`.

  The same day, also with the user's approval and before any maintainer had replied (the user's call), opened PR [#8608](https://github.com/livekit/agents/pull/8608) "feat(workflows): let TaskGroup resume from saved progress" (`Closes #8595`), from the fork's branch `feat/task-group-resume`. It implements the API proposed in #8595: `TaskGroupProgress(pending, results, visited)`, a live `TaskGroup.progress` snapshot, `TaskGroup.current_task`, and `TaskGroup(progress=..., current_task=...)`; every new argument defaults to today's behavior. It is stacked on #8594: the first commit (`46ea165`) is #8594's fix cherry-picked, only the second (`4bac0a3`) is new, and it will be rebased once #8594 merges. Its main test runs the same inputs through a stock long-lived group and through a new session per input, resumed from the progress the previous session saved, and checks that the LLM calls match one for one (same last message, same tools, same `out_of_scope` targets). If it is merged and released, the `on_enter` copied into `ResumableTaskGroup` could give way to these arguments (not tried here). As of 2026-11-17 it is open, review required.

  The PR body also notes a separate, pre-existing issue found while writing its tests: when the task a regression returns to does not speak in `on_enter`, `session.run()` raises the private `_OutOfScopeError`, because `AgentTask.complete()` records it as the run's output although the group handles it. It is not recorded here as a finding yet (no repro in this repo).

  agents-js has the same gap (no progress option, and the stack and results are locals of `onEnter`); see "agents-js parity".

## FINDING-2: TaskGroup discards a step's own chat_ctx

- **Symptom**: a step task is constructed with its own `chat_ctx`, which holds its conversation from the previous cold start. When the task is awaited directly, the LLM sees that conversation; inside a stock `TaskGroup`, the LLM sees only the instructions and the new user input, and the earlier conversation is gone. There is no warning and no error at any point. First hit in Spike 1 S2: in `on_enter`, a saw only `['step a']`.
- **Minimal repro**: `repro/finding_2.py`. The script is standalone, depends only on livekit-agents, inlines a fake LLM, and needs no API key. Run `uv run python repro/finding_2.py`. The exit code is 1 while it still reproduces and 0 once it no longer does. Output (2026-11-12, identical across 3 consecutive runs):

  ```
  livekit-agents 1.8.11
  step awaited directly  -> LLM messages: ['step', 'What is your insurance carrier and member ID?', 'Aetna, member ID W123456789', 'next answer']
  step inside TaskGroup  -> LLM messages: ['step', 'next answer']
  REPRODUCED: inside the TaskGroup the LLM never sees the step's own chat_ctx (replaced by the group's context, task_group.py:102-103)
  ```

- **SDK version**: livekit-agents 1.8.11 (PyPI), Python 3.13.
- **Root cause**: before starting each step, `TaskGroup.on_enter` first does `shared_chat_ctx = self.chat_ctx.copy()`, then `await self._current_task.update_chat_ctx(shared_chat_ctx)` (`beta/workflows/task_group.py` lines 102–103). At that point the task has not started yet, so `Agent.update_chat_ctx` directly executes `self._chat_ctx = chat_ctx.copy(...)` (`voice/agent.py` lines 268–272), replacing the whole context the task was constructed with.
- **Classification**: uncommon usage on our side. Reasons:
  - Handing the group's context to each step is an intentional TaskGroup design, so that the steps share the conversation.
  - In normal usage, the factory creates a new task with an empty context each time, so replacing it loses nothing. Data is lost only when a step is constructed with a restored context, as we do.
  - The only thing here that counts as a defect is that it is silent: the constructor argument is accepted and then dropped, and the docs do not mention it. But that is not enough to count as an SDK bug.
- **Workaround**: diff 3 in `src/clinic_sms/workflow/resumable_task_group.py` (`# SDK-FINDING-2`). For the first step when resuming from progress, if `step_chat_ctx` is given, it is used instead of `self.chat_ctx.copy()`. The test is `test_resumed_step_uses_step_chat_ctx`.
- **Upstream status**: searched 2026-11-12; no related issue or PR. Search method same as FINDING-1. Search terms: `TaskGroup chat_ctx` (issues only), `TaskGroup chat context`, `TaskGroup context lost`, `AgentTask chat_ctx`, `chat_ctx ignored`, `chat_ctx overwritten`, `update_chat_ctx TaskGroup`, `shared_chat_ctx`, plus all results for `TaskGroup`. The closest is [#8307](https://github.com/livekit/agents/issues/8307) (open) and its fix PR [#8310](https://github.com/livekit/agents/pull/8310) (open): when an AgentTask is completed by parallel tool calls, the tool call that completed it is dropped from the next task's context. That is about losing a tool call during a handoff, not about a task's own context being overwritten. Reported upstream on 2026-11-17 together with FINDING-1's issue [#8595](https://github.com/livekit/agents/issues/8595); see FINDING-1's upstream status. PR [#8608](https://github.com/livekit/agents/pull/8608) covers it: a `current_task` passed with `progress=` runs the first pending step and keeps the context it was built with (see FINDING-1). agents-js reproduces it with the same output; see "agents-js parity".

## FINDING-3: No documented ready signal; when a plain start() returns the handoff chain has not finished, and an early run() raises or is silently taken by the TaskGroup

- **Symptom**: the setup is a `Root(Agent)` whose `on_enter` awaits a stock TaskGroup with a single step, and the step's `on_enter` says nothing (like a step being resumed). When a plain `await session.start(root)` returns, this chain has not finished and the innermost step has not taken over yet. If `session.run(user_input=...)` is called at this point, the result depends on when the input is fed. The measurement: yield the event loop n times after `start()` (n=0..39), with a new session for each n:
  - In 21 cases `run()` raises `RuntimeError: cannot schedule new speech, the speech scheduling is draining/pausing, the speech will be cancelled`.
    - In 17 of them the LLM is not called, and the SDK also logs `ERROR livekit.agents: Error in _llm_inference_task (RuntimeError: no activity context found, the agent is not running)`.
    - In the other 4, **`run()` raised, yet the LLM was called anyway**: at n=7 the context did not even have the instructions; at n=8 and n=20 the call was made as the TaskGroup (instructions `*empty*`); at n=21 the step itself answered.
  - In 1 case (n=9), `run()` returns normally, but the TaskGroup is the one that answers, with no error at all.
  - n≥22 are all fine. At n=22, the step's `on_enter` is still one tick away from being called.
  - Control A: `run()` after the step's `on_enter` has been called; all 20 runs are fine.
  - Control B: use `start(root, capture_run=True)` instead, await the RunResult it returns, then `run()` immediately; all 20 runs are fine too. In this RunResult's `events`, the last `agent_handoff` points to the step every time (20/20).
- **Minimal repro**: `repro/finding_3.py`. Like FINDING-2: standalone, inline fake LLM, no API key needed. Run `uv run python repro/finding_3.py`. The result depends on a race, so the script loops over n and counts. The exit code is 1 while it still reproduces. On 2026-11-12 the output was identical across 3 consecutive runs; the lines for n=24..39 are omitted below (same as n=23):

  ```
  livekit-agents 1.8.11
  run() after n x asyncio.sleep(0) following start(), n = 0..39:
    n= 0: step entered=False  raised     LLM calls by []  SDK log records=1
    (n=1..6 same as n=0)
    n= 7: step entered=False  raised     LLM calls by ['<no instructions>']  SDK log records=0
    n= 8: step entered=False  raised     LLM calls by ['*empty*']  SDK log records=0
    n= 9: step entered=False  misrouted  LLM calls by ['*empty*']  SDK log records=0
    n=10: step entered=False  raised     LLM calls by []  SDK log records=1
    (n=11..19 same as n=10)
    n=20: step entered=False  raised     LLM calls by ['*empty*']  SDK log records=0
    n=21: step entered=False  raised     LLM calls by ['step']  SDK log records=0
    n=22: step entered=False  ok         LLM calls by ['step']  SDK log records=0
    n=23: step entered=True   ok         LLM calls by ['step']  SDK log records=0
  run() raised x21: RuntimeError: cannot schedule new speech, the speech scheduling is draining/pausing, the speech will be cancelled
  SDK log record x17: ERROR livekit.agents: Error in _llm_inference_task (RuntimeError: no activity context found, the agent is not running)
  early feeds after plain start(): {'raised': 21, 'misrouted': 1, 'ok': 18}
  control A, run() after the step's on_enter, 20 runs: {'ok': 20}
  control B, run() right after awaiting start(capture_run=True), 20 runs: {'ok': 20}; last agent_handoff in its events is to the step in 20/20
  REPRODUCED: 22/40 early feeds after plain start() went wrong (raised=21, misrouted=1); control A 20/20 ok, control B 20/20 ok
  ```

  Matches Spike 1's S3-sweep line for line.

- **SDK version**: livekit-agents 1.8.11 (PyPI), Python 3.13.
- **Root cause** (all line numbers refer to the 1.8.11 installed in `.venv`):
  1. `session.start()` does not wait for `on_enter`. It calls `_update_activity(agent, wait_on_enter=False)` through `_update_activity_task` (`voice/agent_session.py` line 2046). Root's `on_enter` then awaits the TaskGroup, the TaskGroup awaits the step, and every AgentTask handoff is also `wait_on_enter=False` (`voice/agent.py` lines 1070–1077). So after `start()` returns, the handoff chain is still advancing in the background, one tick at a time.
  2. During a handoff, the outer activity is paused (`voice/agent_activity.py` line 1448 sets `_scheduling_paused = True`). When `session.generate_reply` sees that the current activity is paused, it switches to the next activity (`voice/agent_session.py` line 1677), but the next activity has been `_scheduling_paused = True` since construction (`voice/agent_activity.py` line 357) and is only released after it starts (line 1533). In both cases, `_schedule_speech` raises this RuntimeError (lines 2036–2040).
  3. After the TaskGroup's activity is switched in (`voice/agent_session.py` line 1989), and before the next level pauses it, there is a short window in which it is a normal current activity, and the user input is handed to it (n=9).
  4. When `run()` raises, the speech has already been created. In a few ticks it still called the LLM (n=7, 8, 20, 21), so "raised" does not mean "this input was not processed".
  5. There is no **documented** "the innermost agent has taken over" signal:
     - `current_agent` does not work. `_update_activity` sets `self._agent = agent` right at the start (`voice/agent_session.py` line 1914), while the activity is only switched at line 1989 (Spike 1).
     - The RunResult returned by `start(capture_run=True)` actually does this, and by design:
       - It watches the `on_enter` task of every level: `voice/agent_session.py` lines 2048–2051 watch the first level; for nested AgentTasks, `voice/agent.py` lines 1067–1068 and lines 1089–1094 add the handoff task and the new activity's `on_enter` to the watch list. The comment at lines 1091–1092 states that this is so the RunResult does not complete before `on_enter` registers its own speech.
       - Its public attribute `events` (`voice/run_result.py` lines 152–153) records an `AgentHandoffEvent(old_agent, new_agent)` for each handoff, in order (the class is defined at lines 86–91 and produced at lines 217–223), so it also tells you who took over. In control B the last handoff points to the step 20/20; Spike 1 S1 also recorded `agent_handoff ×3` (None→root→task_group→step).
     - What is missing is only the promise: for `capture_run`, the `start()` docstring only says "capture the run result during session start" (`voice/agent_session.py` line 936); it does not say the returned RunResult waits until the innermost `on_enter`.
- **Classification**: feature gap; here this means a contract/documentation gap, not a capability gap. Reasons:
  - The capability exists: the RunResult from `start(capture_run=True)` waits until the innermost `on_enter`, and `events` also shows who the handoff went to (see root cause 5). What is missing is a documented promise: no documentation says "after awaiting this RunResult you can feed input".
  - The behavior in each time window matches the SDK's internal design: scheduling speech is refused during a handoff; when the TaskGroup's activity is the current activity, it answers. So this is not an SDK bug.
  - Not classified as "uncommon usage on our side": our usage is indeed uncommon (a cold start per message, feeding text right after a plain `start()`; in a normal voice call the chain has long finished by the time the user speaks, so this is rarely hit), but whatever the usage, feeding the first input safely needs a ready point with a promise behind it, and the only one that works today is undocumented.
  - The side inconsistency (`run()` raised, but the LLM was already called, and the step even answered) looks like an SDK bug. But it only appears when input is fed too early, so here we only record the behavior and do not classify it separately.
- **Workaround**: `src/clinic_sms/workflow/ready.py` (`# SDK-FINDING-3`). Usage:
  - The agent that is to receive the next input calls `ready.set(self)` on the first line of `on_enter`, before any `await`.
  - The runner first does `await ready.wait(timeout)`, then `run()`.
  - Every cold start in `tests/test_resumable_task_group.py` is a plain `start()` plus `ready.wait()`.

  The exception is the turn that starts intake: `ConversationRunner.start_intake` takes the `inbound is None` branch of `_run_turn` (`src/clinic_sms/runner.py` lines 146–151, marked `# SDK-FINDING-3`), which uses exactly control B: `start(capture_run=True)`, then await the returned RunResult. This turn feeds no user input; what it has to wait for is the first step's opening question to finish generating, and then it collects the outbound SMS. ReadySignal only says that the step has taken over and does not wait for the question to be generated, so this turn relies on exactly the undocumented watch / hold behavior described below. If upstream changes this part so that the RunResult completes early, this turn fails for lack of outbound text (`EmptyReplyError`, only logged, `start_intake` returns `[]`, no state committed), rather than silently storing wrong state.

  The turns that reply to the patient do not switch to control B, for one reason only: control B relies on undocumented watch / hold behavior, and upstream keeps changing this part. The three PRs below are all merged and all included in 1.8.11 (checked with GitHub compare); their titles were checked read-only by the controller with `gh api`:
  - PR [#6315](https://github.com/livekit/agents/pull/6315) (2026-07-06) "fix(voice): race between session.run and an in-flight AgentTask handoff";
  - PR [#6500](https://github.com/livekit/agents/pull/6500) (2026-07-23) "fix(voice): don't hold session.run() open on update_agent handoffs with long-lived on_enter";
  - PR [#7794](https://github.com/livekit/agents/pull/7794) (2026-10-29) "fix(voice): suspend an AgentTask's awaiter in the run instead of unwatching it".

  `ReadySignal`, by contrast, is an explicit contract in our code: when something counts as ready, and which agent is ready, are decided by our own code and tests, and do not follow upstream's internal implementation.
- **Upstream status**: searched 2026-11-12; no issue or PR about "when input can be fed after a cold start". Search method same as FINDING-1. Search terms: `speech scheduling is draining`, `cannot schedule new speech`, `on_enter ready`, `wait for on_enter`, `session.run on_enter`, `run after start`, `start returns before on_enter`. Three groups are related but not the same:
  - [#6313](https://github.com/livekit/agents/issues/6313) (closed) and PR [#6315](https://github.com/livekit/agents/pull/6315) (merged 2026-07-06, included in 1.8.11, checked with GitHub compare): when an AgentTask is awaited in `on_enter`'s reply, `session.run()` returns in the middle of the handoff, and the next `run()` raises the same RuntimeError. The workaround given in the PR description is `start(capture_run=True)`, i.e. control B.
  - [#7632](https://github.com/livekit/agents/issues/7632) (closed) and PR [#7685](https://github.com/livekit/agents/pull/7685) (merged, included in 1.8.11): when an async tool's reply arrives during a handoff, the same error is raised and `no activity context found` is logged.
  - PR [#7956](https://github.com/livekit/agents/pull/7956) (open): after `run()` fails synchronously, the run slot is not cleared, and every later `run()` reports `nested runs are not supported`.
  - agents-js (checked 2026-11-17; see "agents-js parity"): reproduces in a quieter form, with no error at all: a `run()` right after `await start()` is answered by the wrong agent (the outer agent, or the TaskGroup itself) in 5 of 5 runs per shape. The open agents-js PR [#1196](https://github.com/livekit/agents-js/pull/1196) covers it; not filed.

## FINDING-4: A waiting AgentTask that is cancelled raises only a plain ToolError, with no dedicated type; an agent that awaits it in on_enter without catching it logs an ERROR with a traceback on every normal session close

- **Symptom**: each inbound SMS cold-starts a session, which is closed once the message is handled. At close time the current step is still waiting for the next answer; the SDK cancels it, the cancellation is re-raised unchanged through the TaskGroup, and `await self.group` in `IntakeAgent.on_enter` raises `ToolError: AgentTask visit_reason_task is cancelled`. Two points are worth reporting upstream:
  1. Cancellation has no dedicated type. The awaiter gets the same `ToolError` as for a failed tool call, with no public attribute to tell them apart; only the message text differs.
  2. If the agent's `on_enter` does not catch it, then on every normal session close the SDK logs an ERROR with a full traceback in `_traceable_on_enter` (and asyncio logs it once more when the task is collected; see FINDING-6). A normal close is logged as an error, and the logs cannot tell the two apart.

  Can "close" be told apart from "real failure"? Not from the exception itself, but the one closing the session is the application itself (our runner), which can set an application-level flag before `aclose()`; the awaiter reads this flag to tell them apart. An earlier version of this finding said "only the private `AgentSession._is_closing()` can tell them apart", which overstated it; the current workaround uses exactly this application-level flag.
- **Minimal repro**: `repro/finding_4.py`. A standalone script that depends only on livekit-agents, with an inline fake LLM; no API key needed. Run `uv run python repro/finding_4.py`. The exit code is 1 while it still reproduces. The `on_enter` of `Root(Agent)` awaits a stock `TaskGroup` (summarization off) containing one step that keeps waiting for an answer, so what Root gets is the exception re-raised by the TaskGroup (root cause 3). Three cases:
  - case 1: close the session while the step is waiting;
  - case 2: with the session open, the script completes the step with `ToolError("real failure")`. This "real failure" is synthesized by the script and only stands in for a real tool error; it is not produced by the SDK;
  - case 3: same as case 1, but Root's `on_enter` does not catch it.

  Root has a `closing` flag, which the script sets before `aclose()` (the application-level workaround). The script reads no private SDK state. Output (2026-11-13, identical across 3 consecutive runs):

  ```
  livekit-agents 1.8.11
  1 close   -> caught ToolError(AgentTask step is cancelled) public attrs={} owner flag when caught=True ERROR records=[]
  2 failure -> caught ToolError(real failure) public attrs={} owner flag when caught=False ERROR records=[]
  3 uncaught -> ERROR records=['livekit.agents: Error in _traceable_on_enter +traceback', 'asyncio: Task exception was never retrieved +traceback']
  REPRODUCED: a close-cancellation reaches the awaiting agent as a plain ToolError with no public marker (only an owner-set flag tells it from a failure), and an on_enter that does not catch it gets an ERROR with traceback on a normal close
  ```

  The second record in case 3 is FINDING-6's duplicate log: the script calls `gc.collect()` explicitly before it stops collecting logs, so the record appears while the script is running rather than at exit.
- **SDK version**: livekit-agents 1.8.11 (PyPI), Python 3.13.
- **Root cause** (all line numbers refer to the 1.8.11 installed in `.venv`):
  1. Session close goes through `_teardown_activity` (`voice/agent_session.py`, from line 1403): it first sets `_closing = True` (line 1405), then calls `agent_task.cancel()` on each AgentTask of the current activity (line 1428).
  2. `AgentTask.cancel()` calls `self.complete(ToolError(f"AgentTask {self.id} is cancelled"))` (`voice/agent.py` lines 945–950). Cancellation has no dedicated exception type; it uses the same `ToolError` as a failed tool call.
  3. In the TaskGroup, `res = await self._current_task` (`beta/workflows/task_group.py` line 112) raises this ToolError. It is not an `_OutOfScopeError`, so it falls into the generic `except Exception`; `return_exceptions` is False, so `self.complete(e); return` (lines 134–140). The group's future carries the same exception object, and the awaiter's `await group` (`voice/agent.py` line 1126) raises it. Case 1 of the repro takes exactly this path: what Root catches is the step's `AgentTask step is cancelled`.
  4. The exception itself carries no public information to tell the cases apart: same type, no public attributes, only the message text differs, and message text is not a contract. The SDK's internal `AgentSession._is_closing()` (lines 1300–1302) is private.
  5. The ERROR when it is not caught: `on_enter` runs inside `_traceable_on_enter`, which is wrapped by `utils.log_exceptions` (`voice/agent_activity.py` lines 1080–1098); this decorator logs `Error in _traceable_on_enter` with a traceback for any exception (`utils/log.py` lines 15–23), then re-raises it. Cancellation and real errors take the same path.
- **Classification**: feature gap. Reasons:
  - Cancelling waiting tasks on close is an intentional design, and notifying the awaiter with a ToolError makes sense, so this is not an SDK bug.
  - What is missing is a public cancellation type (for example `AgentTaskCancelledError`, a subclass of `ToolError`). With it, the awaiter could catch only cancellation, and `_traceable_on_enter` could stop logging a normal close as an ERROR.
  - Not classified as "uncommon usage on our side": any agent that awaits an AgentTask in `on_enter` hits this whenever the session closes before the task completes; we just hit it on every message.
- **Workaround**: `src/clinic_sms/agents/intake.py`. `IntakeAgent.mark_closing()` sets an application-level flag; the `except` in `IntakeAgent.on_enter` (`# SDK-FINDING-4`) treats the exception as a normal end only when it is a `ToolError` and the flag is set, and records a failure in every other case. The runner calls `mark_closing()` before `session.aclose()` (the `finally` of `_run_turn` in `src/clinic_sms/runner.py`). No private SDK names are used. `StockRoot` in `spikes/compare_ctx_with_stock.py` uses the same application-level flag (`# SDK-FINDING-4`). Tests are in `tests/test_intake_agent.py`: `test_session_close_with_step_waiting_is_a_normal_exit` (flag set: no ERROR) and `test_close_not_marked_by_the_owner_is_logged_as_a_failure` (not set: recorded as a failure).
  - **Known limitation**: a real `ToolError` that happens to occur after `mark_closing()`, during the close, is also treated as a normal exit and not logged as an error. Acceptable: the session is already closing at that point, the state stays as it was, and the next message starts again from the saved progress.
- **Upstream status**: searched read-only with `gh search issues/prs --repo livekit/agents` on 2026-11-13; no related issue. Search terms: `AgentTask is cancelled`, `AgentTask cancel ToolError`, `AgentTask cancelled session close`, `TaskGroup session close`. Related but not the same, among PRs: [#7839](https://github.com/livekit/agents/pull/7839) (merged) "don't cut an AgentTask's speech on shutdown()", which is about a task's speech being cut off at close, not about the exception type.

  On 2026-11-17, with the user's approval, filed feature-request issue [#8611](https://github.com/livekit/agents/issues/8611) "AgentTask: a dedicated exception for cancellation (AgentTaskCancelledError)" and, right after it (the user's call, without waiting for a maintainer), PR [#8612](https://github.com/livekit/agents/pull/8612) "feat(voice): raise AgentTaskCancelledError when an AgentTask is cancelled" (`Closes #8611`), from the fork's branch `feat/agent-task-cancelled-error`. The PR adds `AgentTaskCancelledError`, a public `ToolError` subclass that `AgentTask.cancel()` completes the task with. `except ToolError` still catches it, and a tool that awaits the task still reports the same message to the LLM, but an app can tell a cancel from a real failure without an app-level flag like `mark_closing()`. The duplicate search, rerun right before filing, found nothing. The issue also offers, as the maintainers' call, not logging an uncaught cancellation as an ERROR during a close; that part is left out of the PR. Before filing, the branch was rebased onto main `d5e9e5c` (the cited files are unchanged since `0cfc030`), and the unit gate passed (6556 passed, 4 skipped; ruff and mypy clean). Once it is merged and released, the `mark_closing()` flag in `IntakeAgent` can give way to `except AgentTaskCancelledError`.

  agents-js has the first point (closing the session gives a plain `ToolError("AgentTask step is cancelled")`), but not the ERROR on every close, for the reason in FINDING-6; see "agents-js parity".

## FINDING-5: Every activation appends an agent_config_update to the end of the context, so a context restored on cold start can never match a long-lived session on these items

- **Symptom**: at the end of each SMS, the chat_ctx of all three levels (IntakeAgent, group, current step) is stored, and the next message's cold start rebuilds from them. Compared with a session that is never restarted, the rebuilt message, function_call and function_call_output items are exactly the same (Spike 3), but the count and positions of `agent_config_update` never match, whether these items are stored or not:
  - Store all: each level has one extra at the end, plus one more per cold start, piling up. This is how the first version of the Task 7 test failed: at the step level, `Left contains one more item: ('agent_config_update',)`.
  - Store none: the historical ones are gone, leaving only the one added by this activation, placed after all the restored items. In the long-lived session, the step's own one comes before its opening question.
  - Only the root level matches item for item under "store none", because it says nothing after it is activated.
- **Minimal repro**: `repro/finding_5.py`. A standalone script that depends only on livekit-agents, with an inline fake LLM; no API key needed. Run `uv run python repro/finding_5.py`. The exit code is 1 while it still reproduces. Output (2026-11-13, identical across 3 consecutive runs):

  ```
  livekit-agents 1.8.11
  long-lived session      : ['message/system', 'agent_config_update', 'message/assistant']
  cold start, keep        : ['message/system', 'agent_config_update', 'message/assistant', 'agent_config_update']
  2nd cold start, keep    : ['message/system', 'agent_config_update', 'message/assistant', 'agent_config_update', 'agent_config_update']
  cold start, drop        : ['message/system', 'message/assistant', 'agent_config_update']
  Anthropic request identical for all four: True
  REPRODUCED: after a cold start the restored context never has the long-lived item sequence (a new agent_config_update is appended at activation); keeping the stored ones adds one more per cold start
  ```

- **SDK version**: livekit-agents 1.8.11 (PyPI), Python 3.13.
- **Root cause** (all line numbers refer to the 1.8.11 installed in `.venv`):
  1. Every activation records the current configuration. Lines 1335–1350 of `AgentActivity._start_session_impl` (comment "Record initial agent configuration") create an `AgentConfigUpdate(instructions=..., tools_added=...)` and insert it into the agent's own `_chat_ctx` (line 1349) and the session's history (line 1350). It is recorded whenever the instructions or tools are non-empty; there is no switch. `_start_session` (line 1223) is called by `start()` (line 1072) and `resume()` (line 1557), so both a fresh start and a resume on returning from a handoff add one.
  2. The newly added one goes last. Its `created_at` is the current time at creation (`llm/chat_context.py` line 510), and `ChatContext.insert` finds the position by `created_at` (lines 586–592; `find_insertion_index` is at lines 901–912), so it always lands after all the restored items. The storage side has no way to make it land back in its position in the long-lived session.
  3. A stored `AgentConfigUpdate` cannot be fully restored anyway: the full tool definitions live in the private attribute `_tools`, whose comment says "in-memory only, not serialized" (`chat_context.py` lines 512–513).
  4. The LLM does not see these items. Every provider's formatting goes through `group_tool_calls` (`llm/_provider_format/utils.py`, from line 141), where `_ChatItemGroup.add` (lines 205–213) only accepts message, function_call and function_call_output. Other types only get an empty group, which is dropped by `flatten()` (lines 247–253) or by openai's empty-group check (`openai.py` lines 35–36). In the repro, the Anthropic requests generated from the four contexts are identical. The only code that reads these items is for record-keeping, for example the evals judge (`evals/judge.py` lines 79 and 99–104, which take the instructions of the latest one) and the proto encoding (`livekit/agents/_proto.py` line 110).
- **Classification**: uncommon usage on our side. Reasons:
  - Recording the configuration on every activation is an intentional design. For a long-lived session, what it records is simply a fact: at that moment the agent started working with this configuration.
  - Only when, as we do, the agent is rebuilt from a stored context and re-activated on every message do the "restored context" and the "long-lived context" disagree on these items.
  - The LLM's input is not affected, so this is not an SDK bug. We also do not need a new capability from the SDK; we only need to not store these items and to skip them when comparing, so it is not a feature gap.
- **Workaround**: `src/clinic_sms/ctx.py`, two places, both marked `# SDK-FINDING-5`:
  - `dump_ctx` does not store `agent_config_update`. The SDK records the current configuration again on every activation; stored old entries would only add one more per cold start, and they have already lost `_tools`.
  - `normalize_ctx` skips `agent_config_update` when comparing, narrowing only this one type; other non-message items are still compared by `(item.type,)`.
  - Tests are in `tests/test_snapshot_restore.py`. Spike 3's comparison script prints the count of these items on both sides every turn, so the difference that was narrowed away is still visible.
- **Upstream status**: searched on 2026-11-13 with read-only `gh search issues` / `gh search prs` (`--repo livekit/agents`); no issue or PR about "a restored context gains an extra agent_config_update on re-activation". Search terms (both issues and PRs searched):
  - Single words: `agent_config_update`, `AgentConfigUpdate`, `initial_config`;
  - Exact phrases: `config update chat_ctx`, `restore chat_ctx`, `chat_ctx from_dict`, `chat context serialize restore`, `duplicate agent_config_update`, `persist chat context`, `chat history restore`, `chat_ctx restore session`, `load chat_ctx`, `config update resume`, `AgentConfigUpdate resume`;
  - Keyword combinations (AND): `agent_config_update restore`, `AgentConfigUpdate duplicate`, `config update chat_ctx`, `restore chat_ctx`, `chat_ctx from_dict`, `chat_ctx to_dict`, `rehydrate session`.
  - Another 6 exact phrases (`agent config update`, `config_update`, `rehydrate`, `cold start`, `chat_ctx persist`, `chat_ctx to_dict`) hit the search rate limit (HTTP 403) and returned no results; they do not count as searched.

  Related but not the same:
  - PR [#4547](https://github.com/livekit/agents/pull/4547) (merged) "add `AgentConfigUpdate` & initial judges", which introduced these items;
  - PR [#5601](https://github.com/livekit/agents/pull/5601) (merged) "Emit AgentConfigUpdate in OTLP session logs";
  - PR [#6756](https://github.com/livekit/agents/pull/6756) (merged) "fix(proto): one ChatItem serializer that carries every declared field";
  - [#7324](https://github.com/livekit/agents/issues/7324) (closed): `ChatContext.copy(tools=...)` drops FunctionCallOutput items whose name is empty. That is about filtering by tool, not about configuration records.

## FINDING-6: An exception raised by the root agent's on_enter is logged twice: once by the SDK, and again by asyncio when the task is collected

- **Symptom**: in a Task 8 test, after the last intake step recorded its answer, the TaskGroup's summarization call failed and the group ended with that exception. At the time, `IntakeAgent.on_enter` re-raised the exception from `await self.group` unchanged (the Task 6 approach), and as a result the same exception was logged twice:
  - the SDK immediately logs `ERROR livekit.agents: Error in _traceable_on_enter`, with a full traceback;
  - later, asyncio logs another `Task exception was never retrieved` (task name `AgentTask_on_enter`), at an unpredictable time that depends on when this task is collected. In the tests it appeared after pytest finished, printed straight to stderr (pytest's log capture holds the first record's traceback, the traceback references the activity, and the activity references this task, so the task is only collected after the record is released). If collection happens while some later test is running, this ERROR lands in that test's logs, and tests that assert "no ERROR", such as `test_normal_runs_log_no_errors`, fail intermittently.
- **Minimal repro**: `repro/finding_6.py`. A standalone script that depends only on livekit-agents, with an inline fake LLM; no API key needed. Run `uv run python repro/finding_6.py`. The exit code is 1 while it still reproduces. The control group catches and logs the exception itself in `on_enter` and does not re-raise it. The script calls `gc.collect()` explicitly after `aclose()`, so that the second record appears while the script is running, and checks that the task asyncio reports is exactly the one named `AgentTask_on_enter`. Output (2026-11-13, identical across 3 consecutive runs):

  ```
  livekit-agents 1.8.11
  on_enter raises          -> ERROR records: ['livekit.agents: Error in _traceable_on_enter', 'asyncio: Task exception was never retrieved (task AgentTask_on_enter)']
  on_enter catches and logs -> ERROR records: ['repro: on_enter failed']
  REPRODUCED: the SDK logs an exception from a root agent's on_enter and leaves it unretrieved in its task, so asyncio logs it a second time when the task is collected
  ```

- **SDK version**: livekit-agents 1.8.11 (PyPI), Python 3.13.
- **Root cause** (all line numbers refer to the 1.8.11 installed in `.venv`):
  1. The activity runs `on_enter` wrapped in `_traceable_on_enter` (`voice/agent_activity.py` lines 1080–1094), which is in turn wrapped by `utils.log_exceptions`. This decorator catches the exception, logs `Error in <function name>`, then **re-raises** it (`utils/log.py` lines 15–23).
  2. This coroutine is turned into a task by `_create_speech_task` (lines 1096–1098, `name="AgentTask_on_enter"`). The task has only one done callback attached, which removes it from `_speech_tasks` (lines 1001–1003); the callback does not read the exception. On drain, only the tasks still in `_speech_tasks` are awaited (line 2113), and a task that has already finished is not among them.
  3. The root agent is started by `session.start()` through `_update_activity(agent, wait_on_enter=False)` (`voice/agent_session.py` line 2046), which does not await this task; with a run, `_watch_handle` only attaches a done callback (lines 2049–2051), which does not read the exception either. The only things that await it are the `wait_on_enter=True` case (lines 2035–2037) and an AgentTask's awaiter. The latter does `await asyncio.shield(on_enter_task)` directly when there is no run to guard (`voice/agent.py` lines 1117–1119), and when there is a run it awaits the task in `finally` and logs the exception (lines 1129–1133). So nobody retrieves the exception raised by the root agent's `on_enter`, and when the task is collected asyncio reports "never retrieved".
- **Classification**: SDK bug (minor, affects only logging). Reasons:
  - The SDK already logs this exception with `log_exceptions`, which shows it expects that `on_enter` may raise and intends to handle that itself; re-raising into a task that nobody awaits only produces a duplicate ERROR at an unpredictable time.
  - The other paths (`wait_on_enter=True`, an AgentTask's awaiter) all retrieve this exception; only the path of a root agent started by `start()` misses it.
  - It does not affect functionality: the session keeps running and `run()` returns as usual.
- **Workaround**: `IntakeAgent.on_enter` in `src/clinic_sms/agents/intake.py` (`# SDK-FINDING-6`). When the group fails (other than with the cancellation caused by closing the session after `mark_closing()`; see FINDING-4), it logs once with our own logger via `logger.exception`, then returns without re-raising; the state stays as it was. The runner learns of the failure through `snapshot()`: when `snapshot()` finds that the group has ended but intake is not finished, it raises RuntimeError, and the runner commits no state and sends the patient the fallback SMS. This check in `snapshot()` is not itself a workaround: after the group fails, its `pending` still lists the last step that was already answered, and without this check the runner would store it as a normal result (found in Task 8). The test is `test_group_failure_after_last_step_keeps_state` in `tests/test_runner.py`: it asserts that the state is unchanged, the fallback SMS is sent as the reply, the failure reason is logged by `clinic_sms.agents.intake`, and the SDK did not log `Error in _traceable_on_enter`.
- **Upstream status**: searched on 2026-11-13 with read-only `gh search issues` / `gh search prs` (`--repo livekit/agents`); no issue or PR about "on_enter's exception logged twice / never retrieved". Search terms:
  - issues: `"Task exception was never retrieved"`, `"_traceable_on_enter"`, `"AgentTask_on_enter"`, `"Error in _traceable_on_enter"`, `"on_enter" exception`, `log_exceptions on_enter`;
  - PRs: `"Task exception was never retrieved"`, `"_traceable_on_enter"`, `on_enter exception`, `"never retrieved" on_enter`, `log_exceptions`.

  The results for `"Task exception was never retrieved"` are all other tasks (STT/TTS streams in various plugins, background audio, etc.), not `on_enter`. Related but not the same:
  - [#4302](https://github.com/livekit/agents/issues/4302) (closed, 2025-12-18) "TaskGroup can't handle errors": after `complete(e)` the TaskGroup used `break` instead of `return`, then called `complete` a second time, which reported "already done". The 1.8.11 `task_group.py` already uses `return`; this is unrelated to the duplicate log here.
  - PR [#7556](https://github.com/livekit/agents/pull/7556) (open, checked read-only with `gh api repos/livekit/agents/pulls/7556` on 2026-11-13) "Keep TaskGroup results when chat-context summarization fails": when summarization fails after all steps complete, the TaskGroup no longer ends with the exception but returns a `TaskGroupResult` with `summarize_error`. This finding's trigger (a summarization failure making the group raise) is within its scope, and once it is merged `await group` no longer raises in this scenario; but the double logging of an exception raised by the root agent's `on_enter` is itself outside its scope.

  agents-js (checked 2026-11-17; see "agents-js parity") has the inverse problem: an exception thrown from `onEnter` is not logged at all, at any level, when the agent is activated by `start()`, `updateAgent()` or a tool handoff. Filed with the user's approval as agents-js issue [#3016](https://github.com/livekit/agents-js/issues/3016), with fix PR [#3017](https://github.com/livekit/agents-js/pull/3017) "fix(voice): log an exception thrown from onEnter", which logs it where the activity starts `onEnter`, like Python's `log_exceptions`. Both open as of 2026-11-17. The Python side (logged twice) is not filed.

## FINDING-7: A TaskGroup resumed from progress and stopped at its first visited step offers the LLM an out_of_scope tool with an empty enum, and Anthropic rejects the whole request

- **Symptom**: on 2026-11-13 the author ran the CLI by hand with real Claude (see "Manual check" at the end of this file). `/start-intake` sent the first question normally, and after that every reply from the patient failed: the plugin raised `APIStatusError`, status code 400, with the message `tools.1.custom: Invalid schema: Enum must be a non-empty array`; the patient got the fallback SMS and the state did not change. `tools.1` is `out_of_scope`: on this turn's cold start, `ResumableTaskGroup` resumed from the saved progress, stopped at the first step, insurance, with the visited set restored to `{"insurance"}`, so in the `out_of_scope` tool offered to the LLM, `task_ids.items.enum` was `[]`. ScriptedLLM does not check tool schemas, so the tests stayed green; Spike 3's equivalence comparison did not compare the tools offered to the LLM at the time either, so it did not catch this.
- **Minimal repro**: `repro/finding_7.py`. A standalone script that depends only on livekit-agents, with an inline fake LLM; no API key needed, and Anthropic is not called. Run `uv run python repro/finding_7.py`. The exit code is 1 while it still reproduces. Three TaskGroup groups each run "say something in a, complete a, say something in b", format the tools offered to the LLM on every call the way the Anthropic plugin does (`ToolContext(tools).parse_function_tools("anthropic")`, same as `plugins/anthropic/llm.py` lines 199–202), then look for empty enums:
  - stock: the stock TaskGroup, the control group;
  - visited restored: a subclass sets the private `_visited_tasks` to `{"a"}` before `on_enter`, as resuming from the middle of a requires (1.8.11 has no public way to do it);
  - one-line guard: same as above, plus a one-line guard: if no target is left after discarding the current step, the tool is not built.

  Output (2026-11-13, identical across 3 consecutive runs):

  ```
  livekit-agents 1.8.11
  stock            step a: offered [save]  empty enums: []
  stock            step b: offered [save, out_of_scope(enum=['a'])]  empty enums: []
  visited restored step a: offered [save, out_of_scope(enum=[])]  empty enums: ['tools[1].input_schema.properties.task_ids.items.enum']
  visited restored step b: offered [save, out_of_scope(enum=['a'])]  empty enums: []
  one-line guard   step a: offered [save]  empty enums: []
  one-line guard   step b: offered [save, out_of_scope(enum=['a'])]  empty enums: []
  REPRODUCED: resumed in its first visited step, the TaskGroup offers out_of_scope with an empty enum (Anthropic: 400 Enum must be a non-empty array); the stock flow never does, and the one-line guard removes the tool
  ```

  `tools[1]` matches the `tools.1` in the real 400.
- **SDK version**: livekit-agents 1.8.11 + livekit-plugins-anthropic 1.8.11 (PyPI), Python 3.13.
- **Root cause** (all line numbers refer to the 1.8.11 installed in `.venv`):
  1. `TaskGroup._build_out_of_scope_tool` (`beta/workflows/task_group.py` lines 166–201) first checks `if not self._visited_tasks: return None` (lines 167–168), then copies the visited set and does `discard(active_task_id)` (lines 171–172). The emptiness check comes before the discard, so when the visited set holds only the current step it does not return None, but builds the tool from an empty set: the enum is `list(task_ids)`, i.e. `[]` (line 190), and the task table in the description is `{}`.
  2. The stock flow cannot reach this state. `on_enter` builds the tool first (line 105), then adds the current step to the visited set (line 111): on the first entry into the first step the visited set is empty, so it returns None; on every later build the set holds at least one other step (the same goes for going back, since the step that initiates going back is also in the set).
  3. Once a subclass restores the visited set (FINDING-1's usage: continue from saved progress, with going-back targets that match a long-lived session), a resume point in the middle of "the first visited step" is exactly this state. We hit it on every SMS: after intake starts, the progress is `pending=[insurance, ...]`, `visited=[insurance]`, and every reply from the patient within the first step cold-starts from here.
  4. Anthropic requires `enum` in a JSON schema to be non-empty. The plugin uses strict schemas by default (`_strict_tool_schema=True`) and sends `"enum": []` as is, so the whole request is rejected. Whether other providers accept an empty enum was not checked.
- **Classification**: latent defect, triggered only when a subclass has restored the visited set; not an ordinary SDK bug. Reasons:
  - The stock flow cannot reach it (root cause 2); users who only use the stock TaskGroup are not affected.
  - The trigger is a subclass restoring the private `_visited_tasks` (FINDING-1's use case), which is already outside the public API.
  - Hardening it upstream takes one line: check for emptiness after the discard (`if not task_ids: return None`). The result is unchanged in every state the stock flow can reach. If upstream later supports resuming from progress (FINDING-1), this line becomes required.
- **Workaround**: `src/clinic_sms/workflow/resumable_task_group.py` overrides `_build_out_of_scope_tool` (`# SDK-FINDING-7`): if no target is left after discarding the current step, it returns None; otherwise it defers to the parent class. Relation to the stock code: the result differs only when "the visited set holds only the current step" (it returns None, so the tool is not offered, just as the stock code does at the first step); every other state is left entirely to the parent class. Tests:
  - `test_tools_offered_match_a_long_lived_stock_group` in `tests/test_resumable_task_group.py`: the same sequence of inputs, on one side a long-lived session with the stock TaskGroup, on the other a `ResumableTaskGroup` with a new session per input, resumed from the previous progress; it compares, call by call, the LLM call key, the offered tool names and the `out_of_scope` targets. It covers the first entry, a restart in the middle of the first step, a restart after completing a step, and a restart after going back.
  - The `scripted_llm` fixture in `tests/conftest.py`: in tests with a real session, the tools of every call each ScriptedLLM receives are formatted at teardown the way the Anthropic plugin does it, and any empty enum is reported as an error.
  - The two round-trip tests in `tests/test_snapshot_restore.py` and `spikes/compare_ctx_with_stock.py` now also compare the tool names offered on each LLM call.
  - Running the whole test suite with this override removed: `2 failed, 82 passed, 15 errors`; for example, `test_restart_mid_step` reports at teardown `call 1 ('Aetna'): tool out_of_scope: empty input_schema.properties.task_ids.items.enum`. Running Spike 3 with the override removed: `SUMMARY: 2 difference(s) outside agent_config_update: ['turn 1 LLM call 0 tools', 'turn 2 LLM call 0 tools']`.
- **Upstream status**: on 2026-11-13 the controller searched `livekit/agents` with read-only `gh search`; no issue about the `out_of_scope` enum or "Enum must be a non-empty array". Among PRs, only [#7565](https://github.com/livekit/agents/pull/7565) (merged 2026-10-03, checked read-only with `gh api`) "fix(llm): keep non-ascii text readable in mcp results and task group tool description" touched this tool's description: it changed it to `json.dumps(..., ensure_ascii=False)` (already included in 1.8.11, line 181), and does not involve the enum.

  Rechecked on 2026-11-17, and filed after the user approved:
  - Before filing, searched once more with read-only `gh search issues` / `gh search prs`, with the search terms `out_of_scope`, `empty enum`, `Enum must be a non-empty array`, `TaskGroup enum`, `_build_out_of_scope_tool`, `visited_tasks`, `TaskGroup regress`. Still no issue or PR about the empty enum. The problem is still present on upstream main 03f4e49 (after 1.8.12): `task_group.py` is the same as in 1.8.11, and the line numbers cited in the root cause are unchanged.
  - The 2026-11-13 record missed one related pair: issue [#5044](https://github.com/livekit/agents/issues/5044) and PR [#5073](https://github.com/livekit/agents/pull/5073) (closed and merged, respectively, on 2026-03-18). There the `items` schema of the same `out_of_scope` tool lacked `"type"`, Gemini returned 400, and a one-line fix was merged. It is likewise this tool's schema being rejected by a provider, but not an empty enum.
  - At the time, the only open PR touching `task_group.py` was [#8310](https://github.com/livekit/agents/pull/8310) (the fix for #8307). It only changes line 103 of `on_enter`, so it does not conflict with this.
  - PR [#8594](https://github.com/livekit/agents/pull/8594) (2026-11-17): only a PR was opened, no issue. Reason: CONTRIBUTING only asks for an issue first for new features; the stock flow cannot reach this state, so the user-level repro the bug_report template asks for cannot be written either. The PR was filed from the fork's branch `fix/task-group-out-of-scope-empty-enum` (`5799bc6`). The fix is the one line described under "Classification" above: discard the current step first, and return None if no target is left; the old "visited set is empty" check is subsumed by it, so it was removed. The regression test is `test_out_of_scope_offered_only_with_another_visited_task` in the new file `tests/test_task_group.py`, which fails on main with `assert [] is None`. At filing time the CLA check had passed and the rest of CI was still running.
  - Running `repro/finding_7.py` against the fixed clone prints NOT REPRODUCED: the visited restored group no longer offers `out_of_scope` at step a, and the stock group's output is unchanged. Once #8594 is merged and released, the `# SDK-FINDING-7` override in `ResumableTaskGroup` can be removed, because the parent class will give the same result.
  - Comment on #8594, posted 2026-11-17 with the user's approval ([link](https://github.com/livekit/agents/pull/8594#issuecomment-6942611264)): agents-js's `TaskGroup` already has this guard (`buildOutOfScopeTool` drops the active task first and returns `undefined` when none is left), and a group resumed the way #8595 proposes starts in exactly this state, so the guard becomes required there. PR #8608 carries #8594's commit for that reason.
  - As of 2026-11-17: open, review required.

## FINDING-8: When the old agent's reply fails in a handoff turn, session.run() does not raise, and the failure is hidden by the later speech

- **Symptom**: found in the Task 6 review. `book_slot` returns `(IntakeAgent, "Booked: ...")`: the SDK first has the old rescheduling agent reply to this tool output (a confirmation SMS), then the first intake step asks its question. If the LLM call for the confirmation fails (in the scripted test, a non-retryable `APIError`), `session.run()` returns as usual without raising, and the intake question goes out as usual; the runner only checks "did it raise, is the outbound empty", so it commits the rescheduling (`BookSlot`), the new reminder job and `active=intake`, while the patient never received the rescheduling confirmation. This violates "on any failure, write nothing and reply with the fallback SMS". Conversely, if the failure is in the later speech (the first intake step's question), `run()` raises and the turn fails normally. So for the same kind of failure, whether it raises depends on whether it is the last speech to finish.
- **Minimal repro**: `repro/finding_8.py`. A standalone script that depends only on livekit-agents, with an inline fake LLM that answers based on the last item; no API key needed. Run `uv run python repro/finding_8.py`. The exit code is 1 while it still reproduces, 0 when it does not reproduce, and 2 when a control group does not behave as expected. Three groups:
  - no handoff (control): the tool returns only `"moved"`, and the old agent's reply fails;
  - handoff, reply fails: the tool returns `(New(), "moved")`, the old agent's reply fails, and New's `on_enter` asks its question normally;
  - handoff, all ok (control): same as above, but the old agent's reply succeeds.

  The `Error in _llm_inference_task` (with traceback) that the SDK itself logs for the failed call is expected; the script silences it. Output (2026-11-13, identical across 3 consecutive runs, exit code 1 each time):

  ```
  livekit-agents 1.8.11
  no handoff (control)   run() raised: APIError  sent: []  handle errors: ['APIError']  unrecoverable error events: ['APIError']
  handoff, reply fails   run() raised: None  sent: ['What next?']  handle errors: ['APIError']  unrecoverable error events: ['APIError']
  handoff, all ok        run() raised: None  sent: ['Moving you.', 'What next?']  handle errors: []  unrecoverable error events: []
  REPRODUCED: in a handoff turn, the old agent's failed reply is stored only on its speech handle; run() returns normally and only the new agent's message is sent
  ```

- **SDK version**: livekit-agents 1.8.11 (PyPI), Python 3.13.
- **Root cause** (all line numbers refer to the 1.8.11 installed in `.venv`):
  1. When a tool returns a new agent, the activity first calls `self._session.update_agent(new_agent_task)` and sets `draining = True` (`voice/agent_activity.py` lines 4133–4134), then runs the old agent's reply to the tool output with the **same** speech handle (lines 4173–4199, `speech_handle=speech_handle`). The `generate_reply` in the new agent's `on_enter` is a different speech handle.
  2. When the reply's LLM task fails, the error is recorded only on its speech handle: `_on_llm_task_done` sets `speech_handle._error = exc` (lines 3661–3669).
  3. Whenever a watched handle finishes, `RunResult` records it as "the last speech handle to finish" (`voice/run_result.py` line 268), and once all have finished, `_mark_done` looks only at this one handle's `_error` (lines 281–289). In a handoff turn the last one to finish is the new agent's handle, which has no error, so the run ends normally and nobody looks at the error on the old handle.
  4. The session does emit an `error` event when the LLM fails: `LLMStream` calls `_emit_error(e, recoverable=False)` when the error is not retryable or the retries are used up (`llm/llm.py` lines 359–364), and the activity turns it into the session's `error` event (`voice/agent_activity.py` lines 2246–2248). But `AgentSession._on_error` closes the session only after unrecoverable LLM errors exceed `max_unrecoverable_errors` (default 3) (the default is at `voice/agent_session.py` line 194; lines 2061–2075), so a single failure does not end the run or the session.
- **Classification**: SDK bug. Reason: the comment in `_on_llm_task_done` states that an LLM failure should "propagate through `SpeechHandle.exception()` and RunResult (i.e. `session.run()`)"; without a handoff this is indeed what happens (the control group), but with a handoff the failure is hidden by the later speech. Upstream already has a PR for this part of RunResult's behavior (see upstream status).
- **Workaround**: `src/clinic_sms/runner.py` (`# SDK-FINDING-8`). `SessionErrors` is a context manager: everything the session runs in a turn (`start`, waiting for ready, `run`, and `start_intake`'s `start(capture_run=True)`) is wrapped in it; while active it subscribes to the session's `error` event and keeps only those with `recoverable` False (the attempts during retries are recoverable; when all retries fail, the last one is unrecoverable); it unsubscribes on exit. `raise_if_any()` is called after `run()` returns, before checking whether the outbound is empty and before storing the snapshot / committing: if there are errors it raises `UnrecoverableSessionError`, `from` the first error, with only the count in the message and no exception text. After that the existing failure path runs: reply with the fallback SMS, write nothing; the log has only the type name and call stack given by `describe_exception`. Task 7's proactive messages can reuse it. The test is `test_a_failed_reply_in_a_handoff_turn_writes_nothing` in `tests/test_handoffs.py`:
  - `confirmation_fails`: the confirmation call fails. It asserts that only the fallback SMS is replied, only the fallback SMS was delivered, both replies were attempted, the appointment and the reminder jobs are unchanged, the stored state and version are unchanged, and the log has only the type name. Running `tests/test_handoffs.py tests/test_runner.py` with `errors.raise_if_any()` removed: `1 failed, 43 passed`, and the failure is exactly this case.
  - `next_opening_fails`: the reverse, the first intake step's question fails. This case passes even without the workaround (the failure is in the last speech, so run raises on its own); it is kept as a symmetric regression test.
- **Upstream status**: searched on 2026-11-13 with read-only `gh search issues` / `gh search prs` (`--repo livekit/agents`); no issue about this behavior. Search terms:
  - issues: `"_mark_done" speech handle error`, `run result error handoff`, `RunResult exception swallowed`, `session.run does not raise`, `handoff tool reply error`, `tool_choice none handoff error`, `"last speech handle"`, `RunResult error propagation`, `run_result error`, `speech_handle exception run`, `LLM error after handoff`, `agent handoff error swallowed`, `speech failure hidden`, `earlier speech failure`, `RunResult`, `SpeechHandle exception`;
  - PRs: `RunResult error`, `run_result exception`, `propagate speech handle error`, `speech handle error run`, `handoff tool reply`, `RunResult`, `SpeechHandle exception`.

  Directly related is PR [#7957](https://github.com/livekit/agents/pull/7957) (open, created 2026-10-19, updated 2026-10-29, checked read-only with `gh pr view`) "fix(voice): keep an earlier speech failure from being hidden": `RunResult._mark_done()` reads only the last handle to finish, so an earlier failed handle is hidden by a later clean one; the fix is to remember the failed handle and prefer it. Its examples are `session.say()` in a tool and an AgentTask's speech; it does not mention handoffs, but the root cause is the same (root cause 3), so once it is merged the case here should also make `run()` raise; this was not verified on that branch. The PR has no linked issue. Others that are related but not the same: [#6304](https://github.com/livekit/agents/pull/6304) (merged) "fix: expose speech generation errors via SpeechHandle.exception()", which is the origin of root cause 2; [#8269](https://github.com/livekit/agents/pull/8269) (merged) "fix(voice): don't fail the run with a task error its awaiting tool handled", which is about AgentTask errors and does not involve handoffs.

  Whether #7957 also fixes this case has not been checked yet. If it does, the plan is to comment there rather than open a competing PR.

  agents-js (checked 2026-11-17; see "agents-js parity"): part 1 reproduces as in Python. agents-js also has two problems of its own around LLM failures, both filed with the user's approval: when the stream returned by the llm node errors mid-reply, `run()` never resolves (PR [#3013](https://github.com/livekit/agents-js/pull/3013) "fix(voice): finish the reply when the llm node's stream errors"); and a failed LLM request never fails the reply: `run()` resolves, `SpeechHandle.exception()` is `undefined`, and nothing is logged (issue [#3014](https://github.com/livekit/agents-js/issues/3014), fix PR [#3015](https://github.com/livekit/agents-js/pull/3015) "fix(llm): throw a failed LLM request to the stream's consumer", stacked on #3013). All open as of 2026-11-17; CI on these first-time-contributor PRs waits for a maintainer to approve the workflow runs.

  #3015 update, 2026-11-18 (head [`33802f6`](https://github.com/livekit/agents-js/pull/3015/commits/33802f6a87265bdd9adc87d86cfb410d7324b0a5)): main had moved, and the PR conflicted with it in the imports of `llm.test.ts`, so the branch was rebased onto agents-js main [`91ca16f`](https://github.com/livekit/agents-js/commit/91ca16f6). Two commits were added:
  - Devin Review found that under a `FallbackAdapter` the change skipped `maxRetryPerLLM` for the OpenAI Responses LLM. That LLM re-emits its inner LLM's `error` events. The new listener check passed because the forwarder itself is a listener, and the re-emit, with nobody listening on the outer LLM, threw out of the retry loop. `8066986` re-emits only when someone listens, and adds a test with a fake HTTP client that fails once and then succeeds.
  - Running the whole agents-js suite, not only `agents/src`, found six plugin tests that the change broke: they read a failing stream as if it ended quietly. These are the OpenAI Responses HTTP and WS incomplete-handling tests and two Gemini stream-checkpoint tests. `33802f6` makes them expect iteration to throw the same error the `error` event carries.

  The reply in Devin's thread is [here](https://github.com/livekit/agents-js/pull/3015#discussion_r4622078437), and the PR description now covers both commits. No maintainer review yet.

## FINDING-9: With the Anthropic plugin as the judge, all of the SDK's LLM judges fail: the temperature the judge passes is not a parameter of anthropic 1.x's messages.create(), so a TypeError is raised before the request is sent

- **Symptom**: a suspicion that came up while reading the 1.8.11 source before writing the real-model evals in Task 11, confirmed offline. Every SDK LLM judge passes `extra_kwargs={"temperature": 0.0}` when it calls the LLM, including the built-in judges in `evals/judge.py` (`accuracy_judge`, `task_completion_judge`, etc.) and `ChatMessageAssert.judge` (`result.expect...judge(...)`). With the `LLM` from `livekit-plugins-anthropic` as the judge, `messages.create()` raises `TypeError: AsyncMessages.create() got an unexpected keyword argument 'temperature'`, and not a single request goes out. The plugin wraps it as a retryable `APIConnectionError`, so it is retried, and after 4 attempts (about 4 seconds) the judge raises `APIConnectionError: failed to generate LLM completion after 4 attempts`. It looks like a network failure; the real cause is only at the very bottom of the exception chain. It does not depend on the model: the request never reaches the API, so every Claude model behaves the same.
- **Minimal repro**: `repro/finding_9.py`. A standalone script that depends only on livekit-agents and livekit-plugins-anthropic (plus their dependencies anthropic and httpx2); no API key needed, no network access. It uses the real `anthropic.AsyncClient`, with the transport replaced by httpx2's `MockTransport`, which records each request and returns a fixed streaming response (one `submit_verdict` tool call with verdict pass). The api_key is just the placeholder string `offline-repro`. Both groups run `accuracy_judge(llm).evaluate(chat_ctx=<two messages>)`:
  - stock judge: the plugin's `LLM(model="claude-opus-5-5")` as is;
  - temperature stripped (control): the same LLM, whose `chat()` first removes `temperature` from `extra_kwargs`.

  Run `uv run python repro/finding_9.py`. The exit code is 1 while it still reproduces, 0 once it no longer does, and 2 when the control group does not behave as expected. The judge's retry warnings are silenced; the plugin's own WARNING is printed as is. Output (2026-11-13, identical across 3 consecutive runs, exit code 1 each time):

  ```
  livekit-agents 1.8.11, livekit-plugins-anthropic 1.8.11, anthropic 1.17.0
  stock judge          : raised APIConnectionError <- APIConnectionError <- TypeError: failed to generate LLM completion after 4 attempts (caused by TypeError: AsyncMessages.create() got an unexpected keyword argument 'temperature'); requests sent: 0
  temperature stripped : verdict 'pass'; requests sent: 1; body has temperature: False; tool_choice: {'type': 'auto'}
  plugin log           : WARNING anthropic llm: this model does not support a forced tool_choice; sending 'auto' instead
  REPRODUCED: with the Anthropic plugin, the judge's temperature=0.0 reaches messages.create() as a keyword it does not accept; TypeError before any request is sent, so every LLM judge fails
  ```

  The control group shows two things. First, with `temperature` removed the request goes out normally and the judge gets a verdict. Second, the `tool_choice` in the request is `{"type": "auto"}`: the `"required"` the judge asks for was downgraded by the plugin; see root cause 5.
- **SDK version**: livekit-agents 1.8.11 + livekit-plugins-anthropic 1.8.11 + anthropic 1.17.0 (PyPI), Python 3.13. The plugin's declared dependency is `anthropic<2,>=1.8`, so any environment installed within the plugin's dependency range hits this.
- **Root cause** (all line numbers refer to the versions installed in `.venv`):
  1. The judge always adds `temperature=0.0`, excluding only models whose name contains `gpt-5`: `evals/judge.py` lines 152–156 (`_judge_with_llm`, which all built-in judges go through) and `voice/run_result.py` lines 1057–1061 (`ChatMessageAssert.judge`). This exclusion list comes from PR #3573, added when GPT-5 rejected temperature; Claude is not in it.
  2. The plugin merges `extra_kwargs` as is into the request parameters `extra` (`plugins/anthropic/llm.py` lines 175–176), then calls `self._client.messages.create(..., **extra)` (lines 299–305).
  3. anthropic 1.x's `messages.create()` has no `temperature` parameter (checked on 1.17.0 with `inspect.signature(AsyncMessages.create)` on 2026-11-13). The plugin itself knows this: the `temperature` / `top_k` constructor arguments are sent in `extra_body`, with the comment "SDK 1.x dropped temperature/top_k from messages.create()" (lines 181–191). But parameters that come in through `extra_kwargs` do not go through that code. anthropic's `required_args` wrapper passes the arguments on to `create` unchanged, and Python raises the TypeError there (`anthropic/_utils/_utils.py` line 294).
  4. The plugin wraps every exception other than anthropic's timeout and status-code errors as `APIConnectionError(retryable=True)` (lines 380–381). So `LLMStream._main_task` retries it as a connection error: 4 attempts in total, with intervals of 0.1, 2 and 2 seconds (`llm/llm.py` lines 334–379, the `APIConnectOptions` defaults), and finally raises `APIConnectionError("failed to generate LLM completion after 4 attempts")`.
  5. Related, but not the same problem: the judge also forces `tool_choice="required"` (`evals/judge.py` lines 158–164, `voice/run_result.py` lines 1066–1072). `claude-opus-5-5` is on the plugin's "does not support a forced tool_choice" list (plugin lines 50–55), so `"required"` is replaced with `{"type": "auto"}`, with one WARNING (lines 237–247, from PR #7511). The model is then free not to call `submit_verdict` / `check_intent` and to reply with text directly, in which case the judge reports `LLM did not return verdict arguments` (`evals/judge.py` lines 167–168) or `LLM did not return any arguments for evaluation.` (`voice/run_result.py` lines 1083–1084). The offline repro can only show the downgrade itself (the `tool_choice` in the control group's request), not whether a real model would skip the tool call because of it. Whether to record this as a separate finding will be decided once the real-Claude eval results are in.
- **Classification**: SDK bug. Reasons:
  - The judge and the Anthropic plugin live in the same repo, with the same version number. Installed within the plugin's own declared dependency range, running any LLM judge with the Anthropic plugin always fails, regardless of model or usage.
  - The plugin already handles the anthropic 1.x change for its own `temperature` (root cause 3); the same kind of problem was also fixed for the `user` parameter (PR #7970; see upstream status). The `temperature` the judge passes in through `extra_kwargs` is a path that was missed.
  - The failure shows up as a "connection error" after 4 attempts, which hides the real cause.
- **Workaround**: `src/clinic_sms/llm.py` (`# SDK-FINDING-9`). `JudgeLLM` is a subclass of the plugin's `LLM`; its `chat()` first removes `temperature` from `extra_kwargs` and passes the other arguments to the parent class unchanged; `make_judge_llm()` constructs it with the same model and `max_tokens` as the agent. It is used only for the eval judges; the agent's `make_claude_llm()` is unchanged (it never sets temperature anyway). `temperature` is not moved into `extra_body` the way the plugin does it: PR #6868's description says models from Claude 4.7 on return 400 for `temperature` (this project did not check that against the real API), and the agent does not set temperature anyway, so the judge not setting it makes no difference. Tests are in `tests/test_llm.py`, using the same MockTransport as the repro:
  - `test_judge_llm_sends_the_judge_request_without_temperature`: runs `accuracy_judge` with `make_judge_llm()` and asserts that exactly one request is sent, the request body has no `temperature`, the model and `max_tokens` are correct, and the judge gets `pass`. With `make_judge_llm()` temporarily returning the plugin's `LLM` as is, this case fails: `APIConnectionError: failed to generate LLM completion after 4 attempts (caused by TypeError: AsyncMessages.create() got an unexpected keyword argument 'temperature')`.
  - `test_judge_llm_keeps_other_extra_kwargs`: when `extra_kwargs` carries both `temperature` and `metadata`, only `temperature` is removed.
- **Upstream status**: searched on 2026-11-13 with read-only `gh search issues` / `gh search prs` (`--repo livekit/agents`), with gh logged in and no rate limiting hit. No issue or PR about this problem. Search terms:
  - issues: `temperature anthropic`, `anthropic temperature`, `unexpected keyword argument temperature`, `judge anthropic`, `evals anthropic`, `task_completion_judge`, `judge`, `extra_kwargs anthropic`, `messages.create temperature`, `anthropic extra_kwargs`, `judge claude`, `judge fails`, `judge temperature`, `evals judge`, `RunResult judge`, `excluded_models_temperature`, `temperature 0.0 judge`, `temperature not supported`, `unexpected keyword argument`, `httpx2`, `anthropic 1.0`;
  - PRs: `temperature anthropic`, `anthropic temperature`, `judge temperature`, `temperature judge`, `anthropic sdk 1`, `anthropic extra_body temperature`, `drop temperature`, `sampling params`, `forced tool_choice`, `judge tool_choice`, `extra_kwargs temperature`, `anthropic messages.create`, `judge`, `evals`, `excluded_models_temperature`, `unexpected keyword argument`, `httpx2`, `anthropic 1.x`, `anthropic>=1`, `anthropic sdk 1.x`, `extra_body`, `anthropic` (all titles from 2026-08 on within the first 100 results were read).

  Related but not the same (details checked read-only with `gh issue view` / `gh pr view` / `gh pr diff`):
  - issue [#3556](https://github.com/livekit/agents/issues/3556) (closed 2025-10-13): with the GPT-5 series as the judge, OpenAI rejects `temperature`. The fix PR [#3573](https://github.com/livekit/agents/pull/3573) (merged 2025-10-13) added the `gpt-5` exclusion list from root cause 1. It is likewise the judge always passing temperature, but there the API rejects the request; here the plugin fails before the request is sent, regardless of model.
  - PR [#7970](https://github.com/livekit/agents/pull/7970) (merged 2026-10-26) "fix(anthropic): send user as metadata.user_id": the plugin's `user` parameter ran into the same mechanism (not accepted by `messages.create()`, TypeError before the request, wrapped as a retryable `APIConnectionError`, 4 attempts); only `user` was fixed.
  - issue [#7235](https://github.com/livekit/agents/issues/7235) and PR [#7242](https://github.com/livekit/agents/pull/7242) (closed and merged on 2026-09-11): the PR states that anthropic 1.0 removed `temperature` and `top_k` from `messages.create()`, and at the time it capped the plugin dependency at `<1`. The 1.8.11 plugin has since moved to `anthropic<2,>=1.8` and puts its own temperature into `extra_body` (root cause 3), but the `extra_kwargs` path is not handled.
  - PR [#6868](https://github.com/livekit/agents/pull/6868) (open, last updated 2026-10-11) "fix(anthropic): support the Claude 4.7+ and 5 model generations": drops `temperature` / `top_p` / `top_k` for Claude 4.7 and later models, including those that come in through `extra_kwargs` (its test `test_dropped_when_they_arrive_through_extra_kwargs`). Once merged it should cover the `claude-opus-5-5` here, but it does nothing for models before 4.7. Its diff changes code in the pre-anthropic-1.x style (`extra["temperature"] = ...`); whether it holds on 1.8.11 was not checked; it does not mention the judge either.
  - issue [#7410](https://github.com/livekit/agents/issues/7410) (closed 2026-09-23) and PR [#7511](https://github.com/livekit/agents/pull/7511) (merged 2026-10-17): Opus 5.5 and other models do not support a forced tool_choice, so the Anthropic plugin was changed to downgrade it to auto; this is the origin of root cause 5. #7410 mentions that this breaks the judge, but it is about a 400 on Bedrock.

  Rechecked on 2026-11-16, and filed after the user approved:
  - Before filing, searched once more with read-only `gh search issues` / `gh search prs`, including searches with no date limit and items updated after 2026-11-01; still no duplicate issue or PR. The problem is still present on upstream main 89b0383 (1.8.12), and the line numbers cited in root causes 1–4 are the same as in 1.8.11.
  - issue [#8585](https://github.com/livekit/agents/issues/8585) (2026-11-16): covers the symptom, the offline repro, the root cause (with permalinks on main) and the results of the real-API check.
  - PR [#8586](https://github.com/livekit/agents/pull/8586) (2026-11-16, `Fixes #8585`): filed from the branch `fix/anthropic-extra-kwargs-sampling` of the fork `richardphoenix/agents`. The fix is in the plugin, and the judge is left alone: `temperature` / `top_p` / `top_k` in `extra_kwargs` go into `extra_body` together with the plugin's own sampling parameters, with the per-call values taking precedence. The regression test is `test_sampling_params_in_extra_kwargs_sent_in_the_body` in `tests/test_plugin_anthropic_request.py`. At filing time the CLA was not signed yet; the user signed it afterwards.
  - Merged: on 2026-11-17 at 02:06 UTC, maintainer longcw approved it and squash-merged it into main ([`4a430e3`](https://github.com/livekit/agents/commit/4a430e3cd7e4e60135255fa65cf8b835e9d7c305)). The merged change is the same as what was submitted (plugin +6/-1, tests +18). All CI passed, Devin Review raised no issues, and #8585 was closed with the PR (completed). At merge time the latest release was still 1.8.12, so the fix will only ship with the next release.
  - Not in a release yet as of 2026-11-17: the latest is 1.8.12 (2026-11-13), cut before the merge.
  - Checked `temperature` against the real API (2026-11-16, calling `messages.create(..., extra_body={"temperature": 0.0})` directly, `max_tokens=8`): `claude-sonnet-4-6` returns 200; `claude-opus-5-5` returns 400 "`temperature` is deprecated for this model", and 200 without `temperature`. The Workaround item above says "this project did not check that against the real API"; this result now takes precedence. It follows that after #8586 is merged, the judge still fails on 4.7 and later models, only now with an immediate 400 (4xx is not retried). Dropping these parameters per model is in #6868's scope. This project uses `claude-opus-5-5`, so the `JudgeLLM` workaround is still needed after #8586 ships.
  - Checked #6868's diff: it writes `temperature` and `user` as top-level parameters of `messages.create()` (`extra["temperature"]`, `extra["user"]`), which is the pre-anthropic-1.x style. On models before 4.7, the sampling parameters in `extra_kwargs` still end up among the top-level parameters, so it cannot fix the TypeError here.
- **Scope (per spec §7 R6)**: R6 says that problems already covered by #7726, #6868 and #7755 are only recorded, not filed again. The `claude-opus-5-5` this project uses is one of the 4.7-and-later models, so:
  - On this model, root cause 5's `tool_choice` downgrade overlaps with #6868 (the plugin does not yet officially support Claude 4.7+ and generation-5 models), so it is only recorded, not filed separately, and not given its own FINDING. In the behavior eval (see "Behavior eval (Plan 2)" below) every judge returned a verdict, so the downgrade caused no failure this time.
  - If FINDING-9 is reported upstream later, the scope has only two parts: first, the path where the judge passes `temperature` through `extra_kwargs` (root causes 1–4), which raises a TypeError before the request is sent, regardless of model, and for which #6868's diff is also written against the pre-anthropic-1.x style; second, `tool_choice` on models before 4.7. Going by the 1.8.11 code, the second part is currently empty: the plugin's "does not support a forced tool_choice" list (plugin lines 50–55) contains only generation-5 models, so models before 4.7 still receive the `"required"` the judge asks for and are not downgraded. Only if models before 4.7 are later added to the list will this part have anything to report.

## FINDING-10: A text that arrives while the agent says something with allow_interruptions=False is dropped

- **Symptom**: with RoomIO's default text input, a user text that arrives while the current speech disallows interruptions is lost: it never enters the chat context and is never answered, now or after the speech. The only trace is a WARNING with a traceback (`failed to handle chat text stream`, `RuntimeError: This generation handle does not allow interruptions`). The same text sent after the speech is answered normally. With interruptions disabled for the whole session (`turn_handling=TurnHandlingOptions(interruption={"enabled": False})`), every reply is uninterruptible. Every text sent while the agent replies is then lost (checked on main 0cfc030).

  For comparison, the voice path handles the same moment on purpose: by default it replaces the user's audio with silence while an uninterruptible speech plays (`discard_audio_if_uninterruptible=True`, `voice/turn.py` line 192; `voice/agent_activity.py` lines 1712–1718; the reason is echo), and if a user turn still completes it logs `skipping reply to user input, current speech generation cannot be interrupted` and skips only the reply, keeping the user message in the chat context when the audio was kept (`agent_activity.py` lines 2865–2880). So voice also does not answer such input; the difference is that the text path loses the message through an unhandled exception, whatever the settings, although typed text cannot be echo.
- **Minimal repro**: `repro/finding_10.py`. Standalone, livekit-agents only, inline fake LLM, no key, no network. It calls the stock default callback (`TextInputOptions().text_input_cb`) the way RoomIO's `_read_text` does (same call, same `except`), while a 1 s reply with `allow_interruptions=False` streams; control: the same with `allow_interruptions=True`. Run `uv run python repro/finding_10.py`. Output (2026-11-17, identical across 3 runs):

  ```
  livekit-agents 1.8.11
  locked   (allow_interruptions=False): {'dropped_with': 'RuntimeError: This generation handle does not allow interruptions', 'm1_in_history': False, 'm1_answered': False}
  control  (allow_interruptions=True) : {'dropped_with': None, 'm1_in_history': True, 'm1_answered': True}
  REPRODUCED: the default text input callback raised from session.interrupt(); RoomIO would log a WARNING and drop 'm1' (never in the chat context, never answered)
  ```

  End to end through a real room: spike scenario `locked` (see [the text-channel spike](spikes/text-channel.md)).
- **SDK version**: livekit-agents 1.8.11 (PyPI), Python 3.13; also main 0cfc030 (see "Status on main").
- **Root cause**:
  - The default callback is `async with sess._claim_user_turn(): await sess.interrupt(); sess.generate_reply(user_input=ev.text)` (`voice/room_io/types.py` lines 46–49).
  - `AgentSession.interrupt()` → `AgentActivity.interrupt()` calls `self._current_speech.interrupt(force=force)` without catching (`voice/agent_activity.py` lines 1963–1964); `SpeechHandle.interrupt()` raises `RuntimeError` when the handle disallows interruptions and `force` is False (`voice/speech_handle.py` lines 222–223). Queued speeches get a try/except and a warning (lines 1971–1982); the current one does not.
  - So `generate_reply()` never runs, and the message was never added anywhere.
  - RoomIO runs the callback in a task whose `except Exception` logs a WARNING and returns (`voice/room_io/room_io.py` lines 533–543). Nothing re-delivers the text.
- **Classification**: SDK bug, with a design question attached. `interrupt()` behaves as documented ("Raises: RuntimeError: If the speech currently playing disallows interruptions and force is False"); the default callback built on it does not handle that case, so the message is lost through an exception path and a WARNING with a traceback, not through a decision. What the right behavior is (answer after the speech, or keep the message but skip the reply as voice does with the audio kept) is for upstream to choose; losing the message is not one of the voice path's outcomes for text that is not echo.
- **Workaround**: none in this repo (the demo does not use RoomIO). In an app, use a custom `text_input_cb` that catches the RuntimeError from `interrupt()` and still calls `generate_reply()`. The reply then queues behind the uninterruptible speech. Checked on 1.8.11 and main: with such a callback, `repro/finding_10.py` prints NOT REPRODUCED, and the text is answered after the speech.
- **Upstream status**: searched 2026-11-17 (read-only `gh search issues` / `gh search prs`, `--repo livekit/agents`). Terms (issues and PRs): `text_input_cb`, `failed to handle chat text stream`, `allow_interruptions text input`, `does not allow interruptions`, `text message dropped`, `text input ignored`, plus the broad sweeps (see "Upstream search method" in [the text-channel spike](spikes/text-channel.md)). No issue or PR covers the text input path.
  The closest: [#4443](https://github.com/livekit/agents/issues/4443) (open) has the same RuntimeError text, but from the voice path: `_user_turn_completed_task` interrupting an outdated reply that inherited `allow_interruptions=False`; its fix PRs [#6451](https://github.com/livekit/agents/pull/6451) and [#8419](https://github.com/livekit/agents/pull/8419) (both open) change only `agent_activity.py`'s user-turn path, not the text input callback. [#4780](https://github.com/livekit/agents/issues/4780) (closed; fixed by PR [#4807](https://github.com/livekit/agents/pull/4807), merged) is where the default callback came to `await sess.interrupt()`, the call that raises here. [#8041](https://github.com/livekit/agents/pull/8041) (open) rewrites the callback's last line (`await sess._reply_to_user_text(ev.text)`) but keeps `await sess.interrupt()` before it, so it does not change this. Filed 2026-11-17, with the user's approval, together with FINDING-11 as [#8606](https://github.com/livekit/agents/issues/8606). Fix PR [#8607](https://github.com/livekit/agents/pull/8607): the callback leaves an uninterruptible speech alone, and the reply queues behind it.

## FINDING-11: A text that arrives during an agent handoff is dropped, and the old agent's reply is cut

- **Symptom**: a tool returns a new agent; while the old agent is still answering the tool output (its activity draining) and until the new agent's activity has started, a user text is lost: not in the chat context, never answered. It also interrupts the old agent's reply, so the user sees a cut-off reply and then only the new agent's greeting. Logged: WARNING `failed to handle chat text stream` (`RuntimeError: cannot schedule new speech, the speech scheduling is draining/pausing, the speech will be cancelled`) and ERROR `Error in _llm_inference_task` (`RuntimeError: no activity context found, the agent is not running`). The window lasts as long as the old agent's post-tool reply: in the spike, every text sent 0.1–1.5 s after the user's `transfer` text, during a 2 s post-tool reply, was lost (3/3 at each of 4 delays).

  For comparison, the voice path also drops a user turn that ends during a handoff, but on purpose. `on_end_of_turn` logs `skipping user input, speech scheduling is paused` and returns; it keeps the message only when the session is closing (`voice/agent_activity.py` lines 2727–2746). Next to that return sits `TODO(theomonnom): should we "forward" this new turn to the next agent/activity?` (line 2745). Nothing in the audio-activity interrupt checks for a handoff (lines 2375–2440), so user speech can cut the old agent's reply there too. The text path's outcome therefore matches voice's. The differences are that text gets there through an unhandled exception (a WARNING with a traceback plus a stray ERROR), and that voice's reasons for caution (echo, an uncertain end of turn) do not apply to typed text.
- **Minimal repro**: `repro/finding_11.py`. Standalone, livekit-agents only, inline fake LLM, no key, no network. Old's tool returns `(New(), "moved")` and Old's reply to it streams for 2 s; `m2` is delivered through the stock default callback (RoomIO's `_read_text` minus the room) 0.1, 0.5, 1.0 and 1.5 s after `function_tools_executed`; control: after New's greeting. Run `uv run python repro/finding_11.py`. Output (2026-11-17; abridged: the d=0.5, 1.0 and 1.5 lines are identical to d=0.1):

  ```
  livekit-agents 1.8.11
  in window, d=0.1 : {'dropped_with': 'RuntimeError: cannot schedule new speech, the speech scheduling is draining/pausing, the speech will be cancelled', 'm2_in_history': False, 'm2_answered': False, 'agent_after': 'New'}
  in window, d=0.5 : {... same ...}
  in window, d=1.0 : {... same ...}
  in window, d=1.5 : {... same ...}
  control (after greeting): {'dropped_with': None, 'm2_in_history': True, 'm2_answered': True, 'agent_after': 'New'}
  REPRODUCED: 4/4 messages sent during the handoff were dropped (generate_reply raised; never in the chat context, never answered)
  ```

  End to end through a real room: spike scenarios `handoff` and `handoff_window` (see [the text-channel spike](spikes/text-channel.md)).
- **SDK version**: livekit-agents 1.8.11 (PyPI), Python 3.13; also main 0cfc030.
- **Root cause**: the mechanism is FINDING-3's root cause 2; the trigger is new.
  - The default callback first awaits `sess.interrupt()` (`voice/room_io/types.py` line 48), which cuts the old agent's post-tool reply.
  - It then calls `sess.generate_reply()`. Because the current activity is paused for the handoff, `generate_reply` targets `self._next_activity` (`voice/agent_session.py` line 1677). That activity has had `_scheduling_paused = True` since construction (`voice/agent_activity.py` line 357) and is only released when it starts after the old one has drained; `_schedule_speech` raises (lines 2036–2040). The pipeline task created just before the raise runs without an activity context, hence the ERROR.
  - RoomIO logs the exception and drops the message (`voice/room_io/room_io.py` lines 533–543).
  - Upstream fixed the same error for another source of speech: an async tool reply that lands during a handoff now waits for the paused activity to resume (PR [#7685](https://github.com/livekit/agents/pull/7685), merged 2026-10-23, for [#7632](https://github.com/livekit/agents/issues/7632)). The text input path has no such wait.
- **Classification**: SDK bug in the text path, with a design question that upstream already has open. Voice drops a user turn during a handoff deliberately, with a warning, and a TODO asks whether to forward it instead. The text path reaches the same outcome through an exception, not a decision, and the app is not told. FINDING-3 classified "scheduling is refused during a handoff" as by design for early `run()` calls. Here, though, the caller is the SDK's own default callback. And since #7685, async tool replies wait out the handoff, which is the answer this spike would suggest for text.
- **Workaround**: none in this repo. In an app, use a custom `text_input_cb` that, after `interrupt()`, waits while an activity switch is in flight before calling `generate_reply()`. Every switch, handoff or AgentTask, runs under the session's `_activity_lock`, held across the old activity's drain or pause and the new one's start. Checked on 1.8.11 and main: with such a callback, `repro/finding_11.py` prints NOT REPRODUCED. Waiting on the paused activity's `_scheduling_resumed` instead, as #7685 does for tool replies, stalls when the switch is into an AgentTask. The parent resumes only after the task completes, and the task may be waiting for this very text: in a test with the switch widened to 0.3 s, that version was still waiting after 4 s. Both waits use private attributes; there is no public signal (FINDING-3).
- **Upstream status**: searched 2026-11-17. Terms: `text input handoff`, `lk.chat handoff`, `message lost handoff`, `cannot schedule new speech`, `user message during handoff`, `text input during handoff`, `chat message during agent transfer`, plus the broad sweeps. No issue or PR covers text input during a handoff.
  Related but not the same: [#7632](https://github.com/livekit/agents/issues/7632) (closed) / PR [#7685](https://github.com/livekit/agents/pull/7685) (merged) and PR [#7633](https://github.com/livekit/agents/pull/7633) (open), the async-tool-reply case above; [#6313](https://github.com/livekit/agents/issues/6313) (closed) / PR [#6315](https://github.com/livekit/agents/pull/6315) (merged), `session.run()` during an AgentTask handoff (see FINDING-3). PR [#8041](https://github.com/livekit/agents/pull/8041) (open) replaces the callback's `generate_reply` with `_reply_to_user_text`. From its diff (not run): that also targets `_next_activity` and then, since it is still paused, logs `skipping reply to user input, speech scheduling is paused`, so the text would most likely still be lost, with a warning instead of an exception. Found in the pre-filing re-check, the same evening: PR [#6844](https://github.com/livekit/agents/pull/6844) (open since 2026-08-13, by another contributor) implements the voice TODO, passing a voice turn that completes during a handoff to the next activity. From its diff (not run), it does not touch the text callback, so case 2 is unchanged.
- **Filed**: 2026-11-17, with the user's approval, together with FINDING-10 as [#8606](https://github.com/livekit/agents/issues/8606). Fix PR [#8607](https://github.com/livekit/agents/pull/8607), opened the same day at the user's request. It makes the default callback wait for the session's activity lock while a switch is in flight, and adds tests for a handoff and an AgentTask switch.

## FINDING-12: Texts that arrive together are handled concurrently: out of order in the chat context, and either one reply or one reply per text, each blind to the rest

- **Symptom**: texts that arrive together (a user typing several short messages, an SMS split in parts) are handled concurrently by the default text input, and the outcome varies run to run:
  - their order in the chat context is often not the order they were sent (6 of 8 spike runs; e.g. `m1 m3 m4 m5 m2`), and the replies follow that order, so the conversation the LLM sees later is scrambled;
  - either the last text interrupts everything before it and gets the only reply, or each text after the first gets its own reply, generated from the context before the burst plus that text alone: neither the other texts nor the other replies (spike: 4 replies in 6 of 8 runs, 1 reply in 2).

  If the same texts are spaced further apart than one reply, each one interrupts the previous reply and the last reply sees everything in order.
- **Minimal repro**: `repro/finding_12.py`. Standalone, livekit-agents only, inline fake LLM, no key, no network. Delivers `m1`, `m2`, `m3` through the stock default callback at once (one task each, as RoomIO does); control: one after another, each after the previous reply. In-process the outcome is stable: two replies, each blind to the other, and `m3` committed before `m2`. Run `uv run python repro/finding_12.py`. Output (2026-11-17, identical across 3 runs):

  ```
  livekit-agents 1.8.11
  burst  : {'user_order_in_history': ['m1', 'm3', 'm2'], 'replies': ['got m3', 'got m2'], 'each_reply_saw': {'got m3': ['m1', 'm3'], 'got m2': ['m1', 'm2']}}
  control: {'user_order_in_history': ['m1', 'm2', 'm3'], 'replies': ['got m1', 'got m2', 'got m3'], 'each_reply_saw': {'got m1': ['m1'], 'got m2': ['m1', 'm2'], 'got m3': ['m1', 'm2', 'm3']}}
  REPRODUCED: the burst got 2 separate replies, 2 of them generated without the rest of the burst ['got m3', 'got m2']; user texts in the chat context in send order: False
  ```

  End to end through a real room: spike scenario `rapid` (the 8-run table in [the text-channel spike](spikes/text-channel.md)).

- **SDK version**: livekit-agents 1.8.11 (PyPI), Python 3.13; also main 0cfc030.
- **Root cause**:
  - RoomIO starts one task per `lk.chat` stream (`voice/room_io/room_io.py` lines 533–547), so callbacks for texts that arrive together run concurrently.
  - The default callback's `_claim_user_turn()` is a reentrant counter, not a lock (`voice/agent_session.py` lines 1742–1766), so it does not serialize them.
  - When the texts land within the same few ticks, each callback's `await sess.interrupt()` runs before any of the others' `generate_reply()`: they all interrupt the first text's reply (the only speech that exists yet), wait for it, and then each schedules its own reply. Nothing interrupts those. When they land a little further apart, later callbacks interrupt the earlier replies instead, which gives the one-reply outcome.
  - Each `generate_reply()` builds its request from the agent's chat context at that moment plus its own message (`voice/agent_activity.py` lines 1894–1900, `chat_ctx=chat_ctx or self._agent._chat_ctx`, `new_message=user_message`), and the user messages of the burst are committed only as their own replies run, so the requests miss each other.
  - Nothing orders the callbacks: which one resumes first after the shared interrupt, and so which text is committed first, depends on task scheduling, not on arrival order. Over a real room the stream reads add their own jitter, hence the run-to-run variation.
- **Classification**: SDK bug (a race in the default callback). The callback's evident intent is "the newest text supersedes the reply in flight"; with concurrent callbacks that intent holds only sometimes, and the other outcomes (a scrambled chat context; one reply per text, each blind to its siblings) are not ones an app would choose. On a text channel, where bursts are normal, it is visible to every user who types in short pieces.
- **Workaround**: none in this repo (the demo answers one SMS per cold start and does not batch). In an app: a custom `text_input_cb` that serializes delivery (an `asyncio.Lock`) or debounces and joins texts that arrive within a short window into one user turn.
- **Upstream status**: searched 2026-11-17. Terms: `multiple text messages`, `rapid text messages`, `consecutive messages`, `text input interrupt`, `concurrent text input`, `_claim_user_turn`, `multiple messages separate replies`, `debounce text input`, `batch text messages`, `text messages in a row`, plus the broad sweeps.
  Nothing on bursts of text input. Related history: [#4780](https://github.com/livekit/agents/issues/4780) (closed; the interrupt was not awaited, so the next reply missed the cut message), PR [#5806](https://github.com/livekit/agents/pull/5806) (merged; added `claim_user_turn`, the counter the callback uses), [#8041](https://github.com/livekit/agents/pull/8041) (open; reroutes the callback's reply through `on_user_turn_completed`, no serialization).

## FINDING-13: In a text-only room session, what the agent says before a participant is linked (e.g. the on_enter greeting) is never sent, yet recorded as said

- **Symptom**: with audio output off, an agent that speaks before RoomIO has linked a participant (the standard greet-in-`on_enter` pattern, when the agent reaches the room before the user) produces text that reaches nobody. No warning; the speech completes normally and the history holds the message as an ordinary, not interrupted, assistant message, so the next LLM turn assumes the user has read it. Once a participant is linked, text works. Waiting for the participant first (`ctx.wait_for_participant()` before `session.start()`) avoids it. In voice the same greeting waits for the user to subscribe to the agent's audio.
- **Minimal repro**: `repro/finding_13.py`. Needs `livekit-server --dev`; otherwise livekit-agents only, inline fake LLM, no key. The agent runs in-process (AgentSession + RoomIO, no worker): it greets in `on_enter`; the user joins 1 s later, then sends `m1`. Control: the user is in the room first. Run `uv run python repro/finding_13.py`. Output (2026-11-17, identical across 3 runs):

  ```
  livekit-agents 1.8.11
  late join: {'user_received': ['got m1'], 'history_says_said': [('hello', False), ('got m1', False)], 'linked_participant_at_greeting': [None]}
  control  : {'user_received': ['hello', 'got m1'], 'history_says_said': [('hello', False), ('got m1', False)], 'linked_participant_at_greeting': ['user']}
  REPRODUCED: the greeting was produced before a participant was linked; it never reached the user, yet the history records it as said (not interrupted)
  ```

  End to end with the worker: spike scenario `greet` (`agent_first_default` lost it; `agent_first_wait_for_participant` and all user-first runs received it).
- **SDK version**: livekit-agents 1.8.11 (PyPI), Python 3.13; also main 0cfc030 (`room_io.py` and `_output.py` identical).
- **Root cause**:
  - With no audio output, the agent's transcription output is a bare `_ParticipantTranscriptionOutput` with no `TranscriptSynchronizer` to pace it (`voice/room_io/room_io.py` lines 157–184).
  - Its identity is set only after a participant is linked (`room_io.py` lines 390–392, in `_init_task` after `await self._participant_available_fut`).
  - Until then `capture_text` returns immediately (`voice/room_io/_output.py` lines 789–791): no wait, no log, and the speech pipeline treats the text as delivered.
  - The audio path waits instead: `_ParticipantAudioOutput.capture_frame` awaits the first subscription (`_output.py` lines 143–144), so a voice greeting is held until someone listens; with `sync_transcription` the text follows the audio.
  - The stream is sent to the whole room at that moment, so text produced while the linked user is temporarily away is lost the same way (scenario 7 of [the text-channel spike](spikes/text-channel.md)).
- **Classification**: SDK bug, of the silent kind: text-only mode drops agent output that voice mode would hold, and records it as delivered. Arguably "uncommon usage" when the app could wait for the participant first, but the SDK gives no sign, and the voice-first examples that greet in `on_enter` without waiting work in voice and fail silently in text.
- **Workaround**: `await ctx.wait_for_participant()` (or `session.room_io.wait_for_ready()`) before the first proactive message. The demo is unaffected (no RoomIO).
- **Upstream status**: searched 2026-11-17. Terms: `text only greeting`, `greeting not received text`, `transcription before participant`, `text output participant`, `text-only mode`, `wait_for_participant greeting`, `lk.transcription first message`, `greeting lost`, `on_enter greeting text`, `on_enter greeting`, `greeting text mode`, plus the broad sweeps. No issue or PR about RoomIO dropping agent text before a participant is linked.
  Same symptom on another path: [#6577](https://github.com/livekit/agents/issues/6577) (open) / PR [#6578](https://github.com/livekit/agents/pull/6578) (open): `console --text` never shows agent-initiated messages such as the `on_enter` greeting, because the console renders only `session.run()` events. That fix is in `cli/_legacy.py` and does not touch RoomIO.

## FINDING-14: A text sent right after joining a room where the agent is already waiting is dropped

- **Symptom**: the agent is in the room (no participant linked yet); a user joins and sends a text before the agent has been told about that user. RoomIO drops it with WARNING `participant not found, ignoring text input`; it never enters the chat context. On livekit-server 1.13.11 `--dev` the agent learned of the user ~3 s after the join (2.95–3.0 s in every run), and texts sent at 0.2 s and 1.0 s were lost; a text sent after the link was answered.
- **Minimal repro**: `repro/finding_14.py`. Needs `livekit-server --dev`; otherwise livekit-agents only, inline fake LLM, no key. Output (2026-11-17, identical across 3 runs):

  ```
  livekit-agents 1.8.11
  early, sent 0.2 s after joining: {'agent_linked_user_after_s': 2.96, 'm0_in_history': False, 'm0_answered': False, 'warnings': ['participant not found, ignoring text input']}
  early, sent 1.0 s after joining: {'agent_linked_user_after_s': 2.99, 'm0_in_history': False, 'm0_answered': False, 'warnings': ['participant not found, ignoring text input']}
  control, sent once linked      : {'agent_linked_user_after_s': 3.31, 'm0_in_history': True, 'm0_answered': True, 'warnings': []}
  REPRODUCED: the text sent [0.2, 1.0] s after joining was dropped ('participant not found, ignoring text input'); never in the chat context
  ```

- **SDK version**: livekit-agents 1.8.11 (PyPI), livekit rtc 1.2.0, livekit-server 1.13.11 (`--dev`), Python 3.13; RoomIO unchanged on main 0cfc030.
- **Root cause**:
  - `RoomIO._on_chat_text_stream` accepts any sender while no participant is linked (`voice/room_io/room_io.py` lines 516–519), then looks the sender up in `room.remote_participants` and drops the text if it is not there (lines 521–524).
  - The text stream can arrive before the participant update that adds the sender to `remote_participants`; on the dev server the update came ~3 s after the join, the data at once.
  - `TextInputEvent.participant` is already optional (`voice/room_io/types.py` lines 24–28), so the lookup is not needed to deliver the text.
- **Classification**: SDK bug, with a server-dependent window. How long the window is in production (LiveKit Cloud, self-hosted non-dev) was not measured; on the dev server it is ~3 s, which is enough for a user who joins and types at once. In the worker flow where the user's token dispatches the agent, the user is already in the room when the agent starts, so this does not happen.
- **Workaround**: none in this repo. Waiting on the client side for the agent to be visible does not help, since the user sees the agent at once while the agent does not yet see the user. In an app: turn off RoomIO's text input (`text_input=False`) and register an own `lk.chat` handler that does not require the sender in `remote_participants`; or have the client enable input only after a first message the agent sends once `session.room_io.wait_for_ready()` has returned.
- **Upstream status**: searched 2026-11-17. Terms: `participant not found, ignoring text input`, `participant not found text input`, `participant not found`, `ignoring text input`, `text input before participant`, `first message ignored`, `first text message lost`, plus the broad sweeps. Nothing found.

---

## agents-js parity (FINDING-1 to 9)

On 2026-11-17 each of FINDING-1 to 9 was checked against livekit/agents-js main [`3629c58`](https://github.com/livekit/agents-js/commit/3629c58eaaac5231c456ed8e5654abc2669b7ac7) (just after `@livekit/agents` 1.10.0), with repro scripts that import the locally built package and use an in-process scripted LLM (no key, no network), each run 3 times on a clean main. The scripts and the full notes are kept outside this repo with the other upstream drafts.

| Finding | Applies to agents-js | Result on main | Action |
|---|---|---|---|
| 1 | Yes, same structure | No repro written; by code, there is no progress option, and the stack and results are locals of `onEnter` | None; #8595 / #8608 are the Python proposal |
| 2 | Yes | Reproduces; output identical to the Python repro | None |
| 3 | Yes, in a quieter form | Reproduces: an early `run()` is answered by the wrong agent, with no error (5/5 per shape) | None; agents-js PR [#1196](https://github.com/livekit/agents-js/pull/1196) (open) covers it |
| 4 | Point 1 only | Reproduces: a close and a real failure both give a plain `ToolError`; no ERROR on close | None |
| 5 | Yes (recorded on start only, not on resume) | No repro written | None |
| 6 | Inverse | Reproduces: an `onEnter` exception is never logged | Issue [#3016](https://github.com/livekit/agents-js/issues/3016), PR [#3017](https://github.com/livekit/agents-js/pull/3017) |
| 7 | No, already guarded | Does not reproduce | Cited on #8594 |
| 8 | Part 1, plus two JS-only problems | Part 1 reproduces; an erroring llm node stream hangs `run()`; a provider failure is silent | PR [#3013](https://github.com/livekit/agents-js/pull/3013); issue [#3014](https://github.com/livekit/agents-js/issues/3014) + PR [#3015](https://github.com/livekit/agents-js/pull/3015) |
| 9 | No | By code: the JS plugin passes extras into the request parameters, so the TypeError path does not exist | None |

The agents-js PRs come from the fork `richardphoenix/agents-js`, and its CLA check passed. #3015 is stacked on #3013 and carries a rebased copy of its commit; on 2026-11-18 it was rebased onto main `91ca16f` to resolve a conflict, and it will be rebased again once #3013 merges (see FINDING-8). #3017 is based on main.

## Spike 1 (R1/R2)

- Date: 2026-11-12
- SDK: livekit-agents 1.8.11 (PyPI), Python 3.13
- Script: `spikes/cold_start_spike.py`, run with `uv run python spikes/cold_start_spike.py`
- LLM: `ScriptedLLM` (`default=Reply()`; extra calls the SDK makes on its own are recorded in `llm.calls` but do not fail the run; a call whose last item yields no key is recorded as `<no key: <item type>>` and is also answered by `default`)
- All SDK source line numbers here refer to livekit-agents 1.8.11 as installed under `.venv/lib/python3.13/site-packages/livekit/agents/`.

### Setup

- `Root(Agent)` awaits a stock `TaskGroup(summarize_chat_ctx=False)` in its `on_enter`. In the spike this group never finishes, so summarization is turned off to avoid extra calls that have no script.
- Two steps, `a` and `b`, both `AgentTask`, each with one `save(value)` tool: it calls `complete(value)` and returns `f"saved {id}"`.
- A step's `on_enter` first sets an `asyncio.Event`, and calls `generate_reply(instructions=f"ask {id}")` (not awaited) only when `resuming=False`.
- Script: `"ask a" → "What is A?"`, `"a1" → save(value="a1")`, `"ask b" → "What is B?"`.
- "Resume from a": a new session that registers only the steps not yet done (a, b), with a constructed with `resuming=True`.
- Outbound messages: `ChatMessage` items in `conversation_item_added` with role assistant and text.
- Every `start` / `run` / `aclose` is wrapped in `asyncio.wait_for(..., 5)`.
- Ran 3 times in a row with identical results (including every row of the S3 sweep).

### Results

**S1: PASS.** New session; when `await session.start(root, capture_run=True)` returns, the RunResult is already done.

- Events: `agent_handoff ×3` (None→root→task_group→step), then one message.
- Outbound is only `["What is A?"]`, and `current_agent` is step a. `llm.calls == ["ask a"]`.
- a's `chat_ctx` at the end of S1: `system 'step a'`, `agent_config_update ×2`, `assistant 'What is A?'`.

**S2: PASS.** A new session resumes from a, passing a's `chat_ctx` from the end of S1 to a's constructor. It waits for a's event first, then calls `run(user_input="a1")`.

- When `start()` returns, a's event is not set yet; it takes a little longer.
- `run()` returns in about 0.006 seconds (5–7 ms across runs) and does not hang (R2 holds). Events: `function_call`, `function_call_output`, `agent_handoff ×2` (step→task_group→step), message.
- Outbound is only `["What is B?"]`, and `current_agent` is b.
- `llm.calls == ["a1", "saved a", "ask b"]`. Judging by each call's instructions, the first two came from a and the third from b.
- **But the `chat_ctx` passed to a's constructor is thrown away**: the only message a sees in `on_enter` is `['step a']`, without `'What is A?'`. The cause is that the stock `TaskGroup.on_enter` runs `await self._current_task.update_chat_ctx(self.chat_ctx.copy())` before starting each step (`beta/workflows/task_group.py` lines 102–103 in 1.8.11), replacing the task's own context wholesale with the group's context. So change 3 in the Task 5 plan (the first resumed step uses `step_chat_ctx` instead of `self.chat_ctx.copy()`) is required. This is recorded separately as FINDING-2 (TaskGroup discards a step's own chat_ctx), not folded into FINDING-1 (cannot start from saved progress).
- Control **S2-group: PASS**. Passing the same context to the stock `TaskGroup(chat_ctx=...)` instead, a sees `['step a', 'What is A?']` in `on_enter`; the rest of the results match S2.

**S3 (observation):** Same as S2, but `run()` is called right after `start()` without waiting for the event.

- When `run()` is called, a has not entered yet and `current_agent` is `TaskGroup`.
- `run()` raises `RuntimeError: cannot schedule new speech, the speech scheduling is draining/pausing, the speech will be cancelled` right away (`voice/agent_activity.py` lines 2036–2040 in 1.8.11). Nobody answers, the LLM is not called, and the message is lost.
- a enters 0.5 seconds later; another `run("a1")` on the same session then works (outbound `["What is B?"]`).
- Extra sweep **S3-sweep**: after `start()`, `await asyncio.sleep(0)` n times, then `run("a1")`. Every run gave the same results:

| n | `current_agent` at the time | a entered? | Result |
|---|---|---|---|
| 0–6 | TaskGroup | no | `run()` raises RuntimeError; LLM not called |
| 7 | TaskGroup | no | `run()` raises RuntimeError, **but the LLM is still called**, with key `'a1'` and a context of only `[user 'a1']`, not even instructions |
| 8 | TaskGroup | no | `run()` raises RuntimeError, **but the LLM is still called**, with context `[user 'a1', system '*empty*', agent_config_update]`; the last item yields no key, recorded as `<no key: agent_config_update>` |
| 9 | TaskGroup | no | **`run()` returns normally, but TaskGroup is the one answering** (instructions `*empty*`); it calls `save` and gets `Unknown function: save`. The outer layer takes the message, with no error at all |
| 10–19 | **a** | no | `run()` raises RuntimeError; LLM not called |
| 20 | a | no | `run()` raises RuntimeError, but the LLM is called, with key `'a1'` and TaskGroup's context `[system '*empty*', agent_config_update, user 'a1']` |
| 21 | a | no | `run()` raises RuntimeError, but the LLM is called, with a's context `[system 'step a', agent_config_update, user 'a1', agent_config_update]`, recorded as `<no key: agent_config_update>` |
| 22 | a | no (enters on the next tick) | OK, a answers |
| 23 | a | yes | OK, a answers |

The script prints the contexts for these rows (the `ctx of ...` lines). So even when `run()` raises, the user's message may already have entered some layer's context and been used to call the LLM; the contexts of the n=8 and n=21 calls end with `agent_config_update`. All of this only happens when a message is fed too early.

**S4 (observation):** Old messages restored into the context do **not** fire `conversation_item_added`, so collecting outbound messages from this event does not send old messages again.
- The counts in the spike have to be read separately: the 0 in S2 is not evidence, because the context S2 passed to step a was replaced wholesale by TaskGroup (FINDING-2), so no old messages were restored at all. S2-group and S2-post-tool-text hand the context to TaskGroup; the old messages really are in the context, and the count is also 0.
- The real evidence is `test_restart_mid_step` and `test_restart_between_steps_does_not_reask` in `tests/test_runner.py`: after a cold-start restore, the context of the LLM call contains the earlier user message and assistant question (`test_restart_mid_step` asserts this), while the only SMS sent this turn is the new reply.

Note that this event also emits `AgentHandoff` (5 per resumed run), so the collector must take only `ChatMessage`.

**S5 (observation):** b's `chat_ctx` at the end of S2:

```
message/system: 'step b'
agent_config_update
agent_config_update
message/user: 'a1'
function_call: save({"value": "a1"}) call_id=call_1
function_call_output: 'saved a' call_id=call_1
agent_config_update
agent_config_update
message/assistant: 'What is B?'
```

- Through TaskGroup's merge, b can see the user message and tool call from a's turn.
- In S2-group there is also the `assistant 'What is A?'` that a asked earlier, plus the two `agent_config_update` items it brought in.
- `agent_config_update` items come in pairs, but each activation adds only one. Checked on 2026-11-13 with a small standalone script, by each item's instructions: when a step activates, the SDK adds only one item, to that step's own context (with the step's own instructions; `voice/agent_activity.py` lines 1335–1350, FINDING-5). The other item of the pair has instructions `*empty*`; TaskGroup records it in the group context when the group itself activates or resumes, and it is copied to the step along with the group context (`beta/workflows/task_group.py` lines 102–103). In b's context above, the first pair is the group's activation record plus a's record (merged into the group context after a completed), and the second pair is the group's resume record plus b's own activation record. Task 7 has to account for this when comparing contexts for equivalence.

**Order of LLM calls when a step completes (`llm.calls` in S2):** `['a1', 'saved a', 'ask b']`

1. a receives the user input and returns a `save` tool call;
2. the tool has already `complete()`d a, but the SDK, still acting as a, sends the tool output `'saved a'` to the LLM once more;
3. b enters and asks its question with `ask b`.

Step 2 is not something the SDK always does; the tool's return value decides it. The spike's `save` returns the string `f"saved {id}"`, and 1.8.11 requires a reply for a tool that returns a value (`voice/generation.py` line 1241, `reply_required = fnc_out is not None`), which is why the SDK sends the output to the LLM again. If the tool returns None, this call does not happen. That is why the three intake `record_*` tools return None. `test_completing_a_step_sends_only_the_next_question` in `tests/test_runner.py` has the extra call reply with a line of text: while the tool still returned a string, two SMS went out in the same turn (`['Saved, thanks!', 'Thanks! What brings you in?']`); after it returns None, there are only two LLM calls (the answer and the next step's question) and one SMS.

Extra control **S2-post-tool-text: PASS**. If the call in step 2 returns text ("Got it."), that text becomes a separate outbound message ahead of b's question: outbound is `['Got it.', 'What is B?']`, meaning two SMS in one turn. The Task 5 plan described this as "whether the LLM is called again after the tool output is up to the SDK", which is not accurate: the tool's return value decides it (see above).

**Log noise when closing the session (observation, not a FINDING):** if `aclose()` is called after a message was fed too early, or before the `on_enter` chain has finished, the SDK prints:

- `Error in _llm_inference_task: RuntimeError: no activity context found, the agent is not running` (the cancelled speech);
- an early version of the sweep closed the session right after `run()`, which also produced `RuntimeError: TaskGroup is already done` and `Future exception was never retrieved: RuntimeError('session closed')`.

None of this affects the results, but Task 8's "ready-wait timeout → reply with the fallback SMS" path will run into it.

### Conclusions

**R2: holds.** When a nested AgentTask completes and the next step is waiting for user input, `session.run()` returns normally (about 6 ms), and the RunResult contains the tool call, the handoff and the next step's question. `start(capture_run=True)` also waits until the first step's opening question has been generated before it returns (S1).

**R1: the risk is real (recorded as FINDING-3).** Feeding a message before the innermost step takes over has three possible outcomes, depending on timing:

1. `run()` raises RuntimeError and the message is lost;
2. the outer layer (TaskGroup) silently takes the message, and `run()` returns normally;
3. `run()` raises, but the message still enters some layer's context and the LLM is called anyway.

**Can "the step's `on_enter` was called" serve as the ready signal for feeding messages? Yes.**

- Every time `on_enter` runs, `session._activity.agent is self` holds, and `scheduling_paused=False`.
- Feeding messages after this signal, S2, S2-group and S2-post-tool-text all pass, identically across 3 consecutive runs.
- In the sweep, the earliest point that is handled correctly is only one tick before `on_enter` (n=22), so `on_enter` is the earliest observable public hook, and a reliable one.

What cannot serve as the signal:

- a plain `session.start()` returning (S3: the chain is still starting). See also FINDING-3: `start(capture_run=True)` with its RunResult awaited does wait for the chain to finish, but the docs do not promise this;
- `session.current_agent is step`. `AgentSession._update_activity` sets `self._agent = agent` right at the start (`voice/agent_session.py` line 1914 in 1.8.11), then awaits the old activity's pause/drain, and only actually switches the activity at line 1989 (`self._activity = self._next_activity`). In between, `current_agent` is already a, but `run()` raises (n=10–21).

Preconditions for this signal to hold (ReadySignal and Task 6 must follow them):

- set it on the **first line** of the step's `on_enter`, before any `await`;
- a step that is resuming does not generate speech in `on_enter`;
- on the resume path, the `on_enter` of the outer layers (Root, TaskGroup) neither generates speech nor calls the LLM. That way the ready timing depends only on the activity switch, not on LLM latency.

---

## Spike 2 (R6)

- Date: 2026-11-12
- SDK: livekit-agents 1.8.11 + livekit-plugins-anthropic 1.8.11, Python 3.13
- Model: `claude-opus-5-5`, `max_tokens=8000`, no temperature passed (`src/clinic_sms/llm.py`)
- Script: `spikes/claude_roundtrip.py`; run with `uv run python spikes/claude_roundtrip.py`; log in `var/spikes/claude_roundtrip.log` (git-ignored; the raw log of run 1 is kept locally in `var/spikes/claude_roundtrip.run1.log`)
- Three cold starts: T1 starts (a asks for a color); T2 resumes from a and the user answers "Blue" (`save_color` completes a, b asks for a city); T3 resumes from b and the user answers "Portland" (`save_city` completes, the group finishes). Each start/run is wrapped in `asyncio.wait_for(..., 90)`.

### Run 1 (detector bug)

The last line of the script output is `SUMMARY: FAIL - tools not called: ['save_city', 'save_color']`. **This is a bug in the script's tool-call detector, not Claude failing to call the tools** (see below).

Time per turn (for T2 and T3, the `run()` time, excluding start):

| Turn | Time | Outbound text |
|---|---|---|
| T1 start | 2.76 s | `What is your favorite color?` |
| T2 answers a | 7.85 s | 2 messages: `Thanks, I've saved blue as your favorite color!` and `Sorry, I didn't actually save your favorite color since I can't, but which city do you live in?` |
| T3 answers b | 7.60 s | 1 message: `Thanks, I've noted Portland as your city, so just let me know if anything looks off.` |

- **No 400** (no 400 related to thinking / tool_use), **no exceptions**, **no empty replies**. There are no `[log]` lines in the log at all; both handlers were attached (`livekit` and root), so any record at WARNING or above would have shown up (twice, even), which means there were no warnings at all. **About the forced tool_choice warning: this script does not verify it.** The script never forces tool_choice (neither `llm.py` nor `spikes/claude_roundtrip.py` passes one), and the plugin only warns when tool_choice is forced (`"required"` or a named function) (`plugins/anthropic/llm.py` lines 212–231, 237–247; `claude-opus-5-5` is in `_NO_FORCED_TOOL_CHOICE_PATTERNS`, lines 50–55, so forcing is downgraded to `auto` with a one-time warning). So "this warning is not in the log" proves nothing. Whether our flow can hit it was checked in `.venv/.../livekit/agents/`: the framework itself only passes `"none"` or `"auto"` in the tool loop (`voice/agent_activity.py` lines 1842–1846: calling `generate_reply` inside a tool defaults to `"none"`; lines 4093–4192: `"none"` when sending tool results back, otherwise `_reply_tool_choice(...) or "auto"`); in `beta/workflows/` only `dtmf_inputs.py:194` passes `"none"`, and `AgentTask`, `TaskGroup` and the regression / out_of_scope tools pass no tool_choice. The only places that pass `"required"` are `voice/keyterm_detection.py:448` and `evals/judge.py:161`, which we do not use. In the plugin, `"none"` only clears the tools (lines 229–231) and does not count as forcing. Conclusion: the forced downgrade can only be hit when our own code passes `"required"` / a named function to `generate_reply(tool_choice=...)`, to `AgentSession`/Agent options or to `update_options`; there is no such call today, so it has not actually been verified. Known upstream issues (#7726, #6868, PR #7755): this run hit none of their symptoms, so nothing needs to be flagged.
- The RunResult events of both T2 and T3 include `function_call` and `function_call_output` (T2: `function_call, function_call_output, message, agent_handoff, agent_handoff, message`). In T2, a completes and `current_agent` becomes b; in T3, `root.result` is `TaskGroupResult(task_results={'b': 'Portland'})`. Both of these can only be caused by the tools being called. T1 has no tool call, as expected.
- Thinking was on the whole time, and multi-turn tool use (carrying the chat_ctx passed in from the previous cold start) had no problems. The restored context contains no thinking blocks (the plugin does not keep them), and Claude raised no error.

### The detector bug and the fix

- The original detector read `Probe.items`, and `Probe` is fed only by the session's `conversation_item_added`. That event carries only messages and handoffs, no `function_call` (the `items:` lines in the run 1 log show only `message` and `agent_handoff`; the activity code that emits this event is at `voice/agent_activity.py` line 2336 and passes a `ChatMessage`), so `tool calls=[]`, and the summary came out as "tools not called".
- Fix: read the `FunctionCallEvent` entries in `RunResult.events` instead, recording the name and arguments of each call. Evidence: `.venv/lib/python3.13/site-packages/livekit/agents/voice/run_result.py` lines 234–235, where `RunResult._item_added` creates `FunctionCallEvent(item=item)` when `item.type == "function_call"` (class definition at lines 75–77).
- Logging: there is now a single handler on the root logger (`livekit.*` propagates up to it; the earlier two handlers, `livekit` plus root, printed every record twice). Emitting one WARNING from each of the `livekit.agents` and `livekit.plugins.anthropic` loggers confirmed that both reach the log file.

### Observations and design input

- **T2 sent two SMS (confirms C-3):** after a's tool returned, Claude replied with a confirmation ("I've saved blue..."), and then b's opening asked another question. The cause is not the SDK itself but the tool's return value: the spike's `save_color` / `save_city` return the string `saved <id>` (`spikes/claude_roundtrip.py`), and 1.8.11 requires a reply for a tool that returns a value (`voice/generation.py` line 1241, `reply_required = fnc_out is not None`), so the SDK sends the tool output to Claude, and Claude wrote this confirmation. If the tool returns None, this call does not happen. Handling: the three intake `record_*` tools return None (see Spike 1, "Order of LLM calls when a step completes").
- **b's opening line denied a's save:** `Sorry, I didn't actually save your favorite color since I can't, but which city do you live in?` Step b does not have a's save tool; seeing a's tool call in the history, it concluded "not saved" and apologized on its own. This is design input for Task 6/8: the step prompt has to say clearly "the earlier steps are complete and saved; do not comment on them", and the chat context handed to the next step has to be designed with care (for example, pass only the conversation text, not the previous step's tool call / tool output).
- **T3's `root.result` lost a's result:** `TaskGroupResult(task_results={'b': 'Portland'})`, without `'a'`, because T3 is a cold start and only b was registered. This confirms FINDING-1; no separate entry here.
- Timing: one `run()` with thinking takes about 7.6–7.9 s (including the tool call and the follow-up reply; T2 also includes b's opening), and the single reply in T1 took 2.8 s. If the SMS channel has a timeout, latency of this order has to be budgeted for.

### Run 2 (detector fixed)

Run 2 (detector fixed, rerun by the author, `SUMMARY: PASS`):

- Timing: T1 1.97 s, T2 5.01 s, T3 6.97 s. No 400, exceptions or empty replies; no `[log]` warning lines in the log.
- Tool calls: T2 `save_color({"color": "Blue"})`, T3 `save_city({"city": "Portland"})`.
- T2 again sent two outbound SMS (C-3 confirmed again, same cause as above: the spike's tool returned a string): `Thanks, I've noted blue as your favorite color!` and `Which city do you live in?`.
- The cross-step "apology" appeared in T3 this time: in its confirmation, b apologized about the earlier color question ("it wasn't actually recorded anywhere"). It showed up in both runs, only in different places (run 1 in b's opening in T2, run 2 in the confirmation in T3), so this is reliable design input: the step prompts and the chat context passed in for Task 6/8 must handle it, so that a new step does not comment on or deny an earlier step's save.
- `root.result` again has only b's result: `TaskGroupResult(task_results={'b': 'Portland'})` (see FINDING-1).

---

## Spike 3 (stock vs cold-start context)

- Date: 2026-11-13
- SDK: livekit-agents 1.8.11 (PyPI), Python 3.13
- Script: `spikes/compare_ctx_with_stock.py`, run with `uv run python spikes/compare_ctx_with_stock.py`. A one-off script, not maintained long term (design §5.9).
- LLM: one `ScriptedLLM` per side, with the same replies as `intake_replies` in `tests/conftest.py`; no API key needed. The intake `record_*` tools return None, so there is no LLM call after the tool output (`voice/generation.py` line 1241), and only the summary call falls through to `default` (`Reply(text=SUMMARY)`).
- All SDK source line numbers here refer to 1.8.11 as installed in `.venv`.
- Ran 3 times in a row with byte-identical output and exit code 0 each time (2026-11-13, after the FINDING-7 fix).

### Setup

- **(a) Long-lived**: one AgentSession runs from start to finish. `StockRoot(Agent)` uses IntakeAgent's instructions and awaits a stock `TaskGroup()` in `on_enter` (summarization on by default). All three steps are registered with `factory(False)`, i.e. `task_cls(resuming=False, ...)`. When the group completes, it calls `generate_reply(COMPLETION)`, the same as IntakeAgent. Before closing, it sets the application-level `closing` flag (FINDING-4).
- **(b) Cold start**: a new AgentSession for each message.
  - `IntakeAgent(state=...)` restores from the previous `snapshot()`, going through `model_dump_json` and then `model_validate_json` in between, the same as storing to the database.
  - Plain `start()`, wait for ReadySignal, `run()`, `snapshot()`, `mark_closing()`, then close.
  - Turn 0 is a fresh start and uses `start(capture_run=True)`. Summarization is also on by default.
- **Messages**: `"Aetna"`, `"W123456789"`, `"Follow-up on my knee"`, `"Penicillin; ibuprofen"`. Still three questions and three answers, but the insurance answer is split into two messages so that one cold start lands in the middle of a step.
- **Compared at the end of each turn**:
  - outbound SMS;
  - all LLM calls in the turn, including the key, the context after `normalize_ctx`, and the names of the tools offered to the LLM on each call (added after FINDING-7);
  - `normalize_ctx` of the three layers: current step, group and root.

  `normalize_ctx` does not compare `agent_config_update` (FINDING-5); the count of these items is printed separately.

### Results

```
=== turn 0: start
  outbound (a) ["Hi Jane! What's your insurance carrier and member ID?"]
  outbound (b) ["Hi Jane! What's your insurance carrier and member ID?"]  -> same
  LLM keys (a) ['Ask the patient for their insurance carr']
  LLM keys (b) ['Ask the patient for their insurance carr']  -> same
  LLM call contexts: all 1 equal under normalize_ctx
  LLM call tools (a) [['record_insurance']]
  LLM call tools (b) [['record_insurance']]  -> same
  current step (a) InsuranceTask  (b) InsuranceTask
  step : EQUAL (2 items; agent_config_update (a)=2 (b)=2)
  group: EQUAL (1 items; agent_config_update (a)=1 (b)=1)
  root : EQUAL (1 items; agent_config_update (a)=1 (b)=1)
=== turn 1: user 'Aetna'
  outbound (a) ["Got it. What's your member ID?"]
  outbound (b) ["Got it. What's your member ID?"]  -> same
  LLM keys (a) ['Aetna']
  LLM keys (b) ['Aetna']  -> same
  LLM call contexts: all 1 equal under normalize_ctx
  LLM call tools (a) [['record_insurance']]
  LLM call tools (b) [['record_insurance']]  -> same
  current step (a) InsuranceTask  (b) InsuranceTask
  step : EQUAL (4 items; agent_config_update (a)=2 (b)=1)
  group: EQUAL (1 items; agent_config_update (a)=1 (b)=1)
  root : EQUAL (1 items; agent_config_update (a)=1 (b)=1)
=== turn 2: user 'W123456789'
  outbound (a) ['Thanks! What brings you in?']
  outbound (b) ['Thanks! What brings you in?']  -> same
  LLM keys (a) ['W123456789', 'Ask the patient what brings them in for ']
  LLM keys (b) ['W123456789', 'Ask the patient what brings them in for ']  -> same
  LLM call contexts: all 2 equal under normalize_ctx
  LLM call tools (a) [['record_insurance'], ['record_visit_reason']]
  LLM call tools (b) [['record_insurance'], ['record_visit_reason']]  -> same
  current step (a) VisitReasonTask  (b) VisitReasonTask
  step : EQUAL (6 items; agent_config_update (a)=4 (b)=4)
  group: EQUAL (7 items; agent_config_update (a)=3 (b)=3)
  root : EQUAL (1 items; agent_config_update (a)=1 (b)=1)
=== turn 3: user 'Follow-up on my knee'
  outbound (a) ['Any allergies or current medications?']
  outbound (b) ['Any allergies or current medications?']  -> same
  LLM keys (a) ['Follow-up on my knee', 'Ask the patient about any allergies and ']
  LLM keys (b) ['Follow-up on my knee', 'Ask the patient about any allergies and ']  -> same
  LLM call contexts: all 2 equal under normalize_ctx
  LLM call tools (a) [['out_of_scope', 'record_visit_reason'], ['record_allergies_and_medications']]
  LLM call tools (b) [['out_of_scope', 'record_visit_reason'], ['record_allergies_and_medications']]  -> same
  current step (a) AllergiesMedsTask  (b) AllergiesMedsTask
  step : EQUAL (8 items; agent_config_update (a)=6 (b)=4)
  group: EQUAL (11 items; agent_config_update (a)=5 (b)=3)
  root : EQUAL (1 items; agent_config_update (a)=1 (b)=1)
=== turn 4: user 'Penicillin; ibuprofen'
  outbound (a) ["You're all set. See you Thursday!"]
  outbound (b) ["You're all set. See you Thursday!"]  -> same
  LLM keys (a) ['Penicillin; ibuprofen', 'Conversation to summarize:\n\n<assistant>\n', 'Tell the patient their intake is complet']
  LLM keys (b) ['Penicillin; ibuprofen', 'Conversation to summarize:\n\n<assistant>\n', 'Tell the patient their intake is complet']  -> same
  LLM call contexts: all 3 equal under normalize_ctx
  LLM call tools (a) [['out_of_scope', 'record_allergies_and_medications'], [], []]
  LLM call tools (b) [['out_of_scope', 'record_allergies_and_medications'], [], []]  -> same
  current step (a) None  (b) None
  step : n/a
  group: EQUAL (2 items; agent_config_update (a)=7 (b)=3)
  root : EQUAL (3 items; agent_config_update (a)=9 (b)=5)
=== (b) final snapshot: done=True results={'insurance': {'carrier': 'Aetna', 'member_id': 'W123456789'}, 'visit_reason': {'reason': 'Follow-up on my knee'}, 'allergies_meds': {'allergies': ['penicillin'], 'medications': ['ibuprofen']}}
SUMMARY: no difference outside agent_config_update (SDK-FINDING-5)
```

- **No differences at all outside the narrowed-out category**:
  - the outbound SMS are the same in every turn;
  - the keys, contexts and offered tool names of the LLM calls are the same in every turn. The summary call in turn 4 is also the same; its key is the whole conversation to summarize, which contains every function_call and function_call_output from all three steps, so identical keys mean identical summary input;
  - all three layers of context are the same in every turn.
- **`agent_config_update` (the narrowed-out category)**: the per-turn counts are in the output above. (b) has fewer because `dump_ctx` does not store these items, so each cold start only has the ones added by its own activation (FINDING-5).
  - A separate run without the narrowing: the script was temporarily changed to compare these items too, by `(item.type,)` (rerun with the current script on 2026-11-13, same conclusion). All differences are in the count and position of these items: at the step layer in turns 1–3, at the group layer in turns 2–4, at the root layer in turn 4, and the contexts of each turn's LLM calls also differ only in these items.
  - The order of messages did not change. difflib shows a moved config item as deletions and additions of the messages around it; the narrowed comparison proves the message sequence is the same.
- **A difference found and handled during implementation (not recorded as a FINDING): restoring the group drops tool calls.**
  - If the group's context is passed in as `TaskGroup(chat_ctx=...)`, `Agent.__init__` filters function calls by the agent's own tools (`voice/agent.py` line 92). TaskGroup has no tools, so every function_call and function_call_output from earlier steps is dropped.
  - The long-lived group context does contain these calls: every time TaskGroup completes a step, it merges the step's context back in, tool calls included; the comment says this is for summarization (`beta/workflows/task_group.py` lines 114–119).
  - Verification: with the restore temporarily switched back to the filtering version (`update_chat_ctx` with the default `exclude_invalid_function_calls=True`), rerunning the current script (2026-11-13) gives `SUMMARY: 3 difference(s) outside agent_config_update: ['turn 3 group', 'turn 4 LLM call keys', 'turn 4 LLM call 1 context']`. In turn 3 the group layer is missing the `record_insurance` call and output; in turn 4 the input to the summary call is missing the first two steps' tool calls, so the key differs too (the printed key is truncated to 40 characters, so it looks the same).
  - Current approach: `IntakeAgent.on_enter` restores the group's context with the public `update_chat_ctx(..., exclude_invalid_function_calls=False)` before the group starts. Before the group has started, this only replaces the context (`voice/agent.py` lines 268–272). Only public API is used, and the filtering is intentional SDK behavior, so this is not recorded as a FINDING. The group-layer assertions in `tests/test_snapshot_restore.py` cover it.
- **Differences caused by the stock TaskGroup itself**: none appeared in this script.
  - In (a) every step is newly created (`factory(False)`), and each time the group hands its own context to the new step (`task_group.py` lines 102–103). In (b), steps that are not resuming are handled the same way.
  - Only the first resumed step in (b) uses `step_chat_ctx` instead (diff 3 of FINDING-2). Its content is exactly that step's context in (a) at the same moment, so the two sides match.
  - This script does not test going back (`out_of_scope`). The tools offered before and after going back, and the targets of `out_of_scope`, are compared with a long-lived stock TaskGroup in `test_tools_offered_match_a_long_lived_stock_group` in `tests/test_resumable_task_group.py`.
- **The dimension that was missed: the tools offered to the LLM (FINDING-7).** This script originally compared only keys and contexts, not the tools offered on each call, so it did not catch that a cold start in the middle of the first step offered an extra `out_of_scope` with an empty enum. ScriptedLLM does not check schemas, so the tests missed it too; it was the 400 the author hit when running manually with real Claude. The script now compares tool names call by call. Temporarily removing the `# SDK-FINDING-7` override in `ResumableTaskGroup` and rerunning gives `SUMMARY: 2 difference(s) outside agent_config_update: ['turn 1 LLM call 0 tools', 'turn 2 LLM call 0 tools']`, and both are `(a) ['record_insurance']  (b) ['out_of_scope', 'record_insurance']`.
- **ScriptedLLM takes the key from the last item**: after the (b) restore, the step's context ends with a newly added `agent_config_update`. But before the next LLM call, a user message or instructions are always added first, so no `<no key: ...>` call occurred, and the keys match on both sides.

## Manual check (Plan 1)

### 2026-11-13, the author runs the CLI with real Claude (before the FINDING-7 fix)

- Time: 2026-11-13 (UTC; the CLI shows 11/12 11:36 PM in the clinic's time zone). Model `claude-opus-5-5` (`src/clinic_sms/llm.py`). Phone number (555) 010-0001. Logs are local in `var/manual/run1.log` and `var/manual/run2.log` (git-ignored); database `var/manual/plan1.db`.
- `/start-intake`: OK. Claude sent the first question: "Could you send me the name of your insurance carrier and your member ID? ...". In the database the state version is 1, and progress is `pending=[insurance, visit_reason, allergies_meds]`, `visited=[insurance]`.
- Every reply after that failed: `Aetna, member W123456789` in run1, and `Follow-up on my knee` in run2 (a newly started process, same database). Both times the plugin raised `APIStatusError`, 400, `tools.1.custom: Invalid schema: Enum must be a non-empty array` (FINDING-7); the patient got the fallback SMS "Sorry, something went wrong on our end — please reply again."
- State unchanged: after the two failures, `/state` still shows `done: false`, all three steps in `pending`, `visited: [insurance]`, `results: {}`; in the database the state version is still 1, and the messages table only gained two "inbound + fallback" pairs.
- The log has only the masked number: `ERROR:clinic_sms.runner:inbound message from +1555***0001 failed; state left unchanged`. The stderr records contain neither the full number nor the patient's words (the patient's words appear only in the operator's console lines).
- The R6 spike was rerun the same day (`spikes/claude_roundtrip.py`, with the stricter checks added in commit 485e63e: every turn checks the outbound text, the expected tool calls, the current agent and `root.result`): `SUMMARY: PASS`. T1 1.42 s, T2 7.02 s, T3 7.61 s; T2 again sent two SMS (the spike's tools return strings, see Spike 2 C-3); in T3's confirmation Claude again apologized for the previous step. Log in `var/spikes/claude_roundtrip.log` (git-ignored).

### 2026-11-13, rerun after the FINDING-7 fix (commit ccb0185)

- Code at ccb0185: the `# SDK-FINDING-7` override is in; the three record tools now return `None`. Model and phone number as above. A new database `var/manual/plan1c.db`, three processes run in sequence, logs in `var/manual/run1c.log`, `run2c.log`, `run3c.log` (git-ignored). The CLI shows 11/13 8:43–8:44 AM in the clinic's time zone.
- Process 1: `/start-intake` sent the first question. After the reply `Aetna, member W123456789`, only one SMS came back, asking for the visit reason. Before the fix, the spike sent two at this turn (Spike 2 C-3); this time it did not.
- Process 2 (cold start): after the reply `Follow-up on my knee`, it asked about allergies and medications, without re-asking the first two steps. `/state` shows `pending=[allergies_meds]`; `results` has `insurance` (Aetna / W123456789) and `visit_reason`. `visit_reason` stores Claude's rewording, "Follow-up on knee".
- Process 3 (cold start, testing going back): after the reply `Actually my member ID is W999`, it went back to the insurance step, and Claude asked the patient to resend the insurance carrier and the full member ID. After the reply `Aetna, W999`, it went straight back to allergies and medications without re-asking the visit reason. `/state` shows `member_id` as `W999`, `visit_reason` kept, `pending=[allergies_meds]`.
- No 400 and no fallback SMS. The database has 4 inbound and 5 outbound messages: one opening, then one for each inbound. 0 fallback texts; everything in the outbox was delivered; no ERROR records on stderr.
- **Anthropic accepts empty tool results.** The saved `group_ctx` has three `function_call_output` items, two for `record_insurance` and one for `record_visit_reason`, all with `output` equal to `""`. When processes 2 and 3 called the LLM, all of these were sent along in the context, and every request succeeded. So there is no need to switch to `ToolResult(..., reply_required=False)`.
- Conclusion: the Plan 1 manual check passes. It verified three things: with a cold start per message, steps already answered are not re-asked; going back after a cold start overwrites the result correctly; each turn sends only one SMS.

## Manual check (Plan 2)

### Task 2 and Task 4: scenarios/plan1_intake.txt

- Date: 2026-11-13, commit 966980b, run by the author with real Claude. Command: `uv run clinic-sms --db var/smoke/plan1.db --script scenarios/plan1_intake.txt 2>&1 | tee var/smoke/plan1.log`. Result line: `SMOKE: OK (fallbacks=0)`.
- This is the first real-Claude run of Plan 2, and it also serves as the regression run for both Task 2 Step 7 and Task 4 Step 5 (same scenario; the runner had already been refactored by then).
- After `/restart`, intake continued from the visit reason step without re-asking. "Actually my member ID is W999" went back to the insurance step; after the reply "Aetna, W999" it went straight to allergies and medications without re-asking the visit reason. The final `/state`: insurance member_id is W999, and visit_reason is recorded.
- **Intake did not finish.** The script's last line, "Penicillin; ibuprofen", is ambiguous, and Claude asked a follow-up question: "are you allergic to penicillin and taking ibuprofen, or are you allergic to both?"; `done` is still false. This is reasonable model behavior; the problem is in the script. The smoke script now says "Allergic to penicillin; I take ibuprofen", and the scenario will be rerun at the next checkpoint.
- No full phone numbers in the log.

### Task 5: scenarios/reception.txt

- Date: 2026-11-13, commit 966980b, run by the author with real Claude. Command: `uv run clinic-sms --db var/smoke/t5.db --script scenarios/reception.txt 2>&1 | tee var/smoke/t5.log`. Result line: `SMOKE: OK (fallbacks=0)`.
- The FAQ answer gave the Friday opening hours. After `/restart`, the reply to "When is my appointment again?" said Thu 11/19, 10:00 AM, with Dr. Lee. "Please confirm it" got a confirmation reply, and the status in the appointments table is `confirmed` (`sqlite3 var/smoke/t5.db "select status from appointments"`).
- `/state` still prints the Plan 1 format, which is expected before Task 7.
- No full phone numbers in the log.

### Task 6: scenarios/reschedule.txt

- Date: 2026-11-13, run by the author with real Claude (claude-opus-5-5). The key is in a local, git-ignored `.env`, loaded by `uv run --env-file <that file> clinic-sms --db var/smoke/<name>.db --script scenarios/<file>.txt`; the code still reads `ANTHROPIC_API_KEY` only from the environment. All runs in this section (Task 6–9) were done this way, and the logs contain no full phone numbers (`+1555…`), only the `(555) 010-0001` format.
- **t6 (commit ca28a5c)**: everything was correct up to intake completion. The rescheduling offer listed several of Dr. Lee's slots on Monday 11/23 without exposing slot ids. The appointment confirmation SMS came before the insurance question. The appointment moved to Mon 11/23 9:00 AM, and the reminders followed: the Tue 10:00 reminder is `cancelled`, the Sat 11/21 9:00 reminder is `pending` (checked with `sqlite3` by joining appointments and slots and querying jobs). But the final "Thanks!" got a reply from reception saying it had "made a mistake" and had not changed anything, and it handed off to reschedule again. Root cause: the next agent's context did not contain this booking.
- **t6b (commit 93ed0da, with the FINDING-8 fix in and the booking's assistant message added to the handoff context)**: reception still disowned the earlier turns: "I made up those openings… can't confirm your intake details were saved".
- **Root cause (confirmed offline by the Task 6 implementer)**: reception treats other agents' turns as its own. The SDK drops calls to tools the agent does not have; TaskGroup's chat history summary also says the record tools "returned no output or confirmation"; and reception's instructions say "Answer only from the facts above". So, seeing no tool backing for "its own" replies, it concluded they were made up.
- **t6c (commit 094218c, added the "other clinic assistants" instruction and an intake status line for reception)**: the rescheduling offer, the booking confirmation, the insurance question and the continuation after `/restart` all matched t6, and intake completed. "Thanks!" got a normal reply that mentioned Mon 11/23 9:00 AM and Dr. Lee. As expected; the appointment is `booked`, and the jobs are the same as in t6.
- A failed booking confirmation must not let the turn pass as a success; that problem is fixed separately, see FINDING-8.
- The result line of all three runs is `SMOKE: OK (fallbacks=0)`.

### Task 7: scenarios/reminder.txt

- Date: 2026-11-13, commit a793482. Result line: `SMOKE: OK (fallbacks=0, failed_jobs=0)`.
- The reminder went out at 10:00 AM and included Thu 11/19 10:00 AM, Dr. Lee and reply 1/2. Replying "2…" led to the rescheduling offer, then the booking confirmation, then the insurance question.
- `/state`: `active` is `intake`, the appointment is Mon 11/23 9:00 AM with status `booked`, and the only job is the pending reminder for Sat 11/21 9:00 AM.
- No full phone numbers in the log.

### Task 8: scenarios/followup.txt

- **t8 (commit 743117c; the script's first line was `/advance 7h` at the time)**: the code behaved correctly; what was wrong was the timing design of the scenario in the plan. The simulated clock starts at 9:55 AM, and `/advance 7h` lands at 4:55 PM. The answer was given at 4:55 PM and the follow-up was due at 8:55 PM, not yet in quiet hours, so it was sent right away, asking only about allergies and medications. In the final `/state`, intake is complete and there is no pending job.
- Another observation: the `/start-intake` opening leaked meta text into the SMS: "I need both the carrier and member ID before I can call the tool, so I'll ask for them." Task 9 added the rule "write only the text message itself" to `SMS_STYLE`.
- **t8b (commit 7e8dccb, first line changed to `/advance 7h5m`)**: the answer was given at 5:00 PM, and the follow-up was held until Wed 8:00 AM (after quiet hours), asking about allergies and medications. After the reply, intake completed, and `/state` has no pending job. Result line `SMOKE: OK (fallbacks=0, failed_jobs=0)`.
- Neither log contains full phone numbers.

### Task 9 (mainline): scenarios/mainline.txt

- Date: 2026-11-13, commit 7e8dccb. Result line: `SMOKE: OK (fallbacks=0, failed_jobs=0)`. Following the six steps of spec §2.1:
  1. The reminder went out Tue 10:00 AM.
  2. Replying "2…" at 5:00 PM got the offer; after "The first one works", Mon 11/23 9:00 AM was booked (the slot actually chosen is Mon 11/23 9:00 AM), and the confirmation SMS came before the insurance question.
  3. After the insurance and visit reason answers, silence, then `/restart`.
  4. `/advance 15h`; the follow-up went out Wed 8:00 AM, asking only about allergies and medications.
  5. Going back to insurance: after the replies W999 and "Aetna, W999", it went straight back to allergies and medications without re-asking the visit reason.
  6. The "$40" question was escalated: the patient got a reply saying it was passed to the front desk, followed by the allergy question; the front desk number received "[#1] Jane D. (555) 010-0001: Patient asks why they were charged $40 at their last visit." Staff replied "#1 …" at 11:00 AM, and at 11:00 AM the answer was relayed to the patient along with the allergy question again; after the patient's last answer, intake completed.
- Database state at the end: appointment Mon 11/23 9:00 AM, `booked`, intake saved (`member_id` W999, `medications` ibuprofen (daily)); ticket 1 is `closed`; the `staff_relay` job is `done`; the only pending job is the Sat 11/21 9:00 AM reminder. `intake_followup` has many `cancelled` rows, which is expected: every committed turn cancels and reschedules the follow-up.
- No full phone numbers in the log.


### Task 10: record and replay

- **Recording**: 2026-11-13, commit ddb9ee1, run by the author with real Claude (claude-opus-5-5). The key is in a local, git-ignored file, loaded only into the environment with `uv run --env-file <that file>`. Command: `uv run --env-file <key file> clinic-sms --db var/demo/rec.db --llm record --recording scenarios/mainline.jsonl --script scenarios/mainline.txt`. Result line: `SMOKE: OK (fallbacks=0, failed_jobs=0)`. 22 LLM calls in total, and `scenarios/mainline.jsonl` has 22 lines. The recording contains only fictional data (Jane Doe, Dr. Lee, Dr. Patel, Maple Street Family Clinic), no phone numbers, and no keys or tokens.
- **The first offline replay (commit ddb9ee1) failed**: `REPLAY MISMATCH ... call 20 ... item 1`, and the last turn got the fallback SMS.
- **Root cause**: call 20 is TaskGroup's history summary. The SDK's `ChatContext._summarize` (`livekit/agents/llm/chat_context.py`, around lines 995-996) renders the history as a single user message, with each tool call's `call_id="…"` inlined in the text. The recording has the `toolu_…` ids issued by Claude, while on replay the replayer issues its own ids, so the request fingerprints do not match.
- **Fix (commit 00d3a54)**: `request_fingerprint` strips `call_id="…"` from message text before comparing, on both the recording and the replay side. No re-recording needed; `mainline.jsonl` works as is.
- **Verification**: offline replay on 00d3a54, including a real execv `/restart`, run with `env -u ANTHROPIC_API_KEY`. Result line `SMOKE: OK (fallbacks=0, failed_jobs=0)`, no `REPLAY MISMATCH`. The messages tables of the replay database and the recording database are identical: running `sqlite3 <db> "select phone, direction, text, at from messages order by id"` on both and comparing with `diff` gives empty output (22 rows).
- **Usage**:

  ```
  rm -f var/demo/replay.db && env -u ANTHROPIC_API_KEY uv run clinic-sms --db var/demo/replay.db --llm replay --recording scenarios/mainline.jsonl --script scenarios/mainline.txt
  ```

- Two things to note:
  - Always replay into a new database. Reusing an old one continues from the `llm_cursor` and state saved in it instead of replaying from the start.
  - Call numbers in the mismatch report start at 0, so "call 20" corresponds to the line with `"n": 20` in the recording.
- This SDK rendering is not recorded as a FINDING. It is only something this app's request fingerprint has to handle, not an SDK defect.
- **Re-recording (final review fixes)**: on 2026-11-13 the final review fixes changed the agents' prompts and tools: a reminder that comes due while rescheduling or intake is in progress now uses in-flow text (`REMINDER_IN_FLOW_INSTRUCTIONS`), the rescheduling agent gained `transfer_to_reception`, and the wording of `OTHER_ASSISTANTS` changed. That changed the request fingerprints, so the controller re-recorded the main line with real Claude (claude-opus-5-5): first delete the old `scenarios/mainline.jsonl`, then run record on a new database; 22 LLM calls, result line `SMOKE: OK (fallbacks=0, failed_jobs=0)`. The new recording was committed in the same commit as these changes. Offline replay on a new database (`env -u ANTHROPIC_API_KEY`) is also `SMOKE: OK`, with no `REPLAY MISMATCH`, and the messages table is identical to the recording run. The recording contains only fictional data, no phone numbers, no keys. From now on, `tests/test_recording.py::test_committed_mainline_recording_replays_offline` replays this recording offline in the regular test run and fails when the recording is out of date; the re-recording steps are in "Re-recording the main line" in the README.

### Final review fixes: real-Claude checks

- Background: the final whole-branch review brought three changes in commit 45468a9: a reminder that comes due while rescheduling or intake is in progress now uses in-flow text, `transfer_to_reception` was added so the patient can leave the rescheduling flow, and `OTHER_ASSISTANTS` was rewritten. These changes affect the agent prompts, so the main line has been re-recorded (see `### Task 10`), and the controller ran the checks below with real Claude (claude-opus-5-5) on 2026-11-13 at commit 45468a9. The key is in a local, git-ignored file loaded with `uv run --env-file`; the code reads only `ANTHROPIC_API_KEY`. No full phone numbers in the logs.
- **never_mind** (ad hoc script, not committed): first `I need to move my appointment to next week`, and the agent offered slots next week; then `Actually never mind, I will keep my current time`, and the rescheduling agent confirmed that the appointment stays at Thu 11/19 10:00 AM with Dr. Lee. The next question, `What are your hours on Friday?`, was answered by reception. `/state` shows `active: reception`. Result line `SMOKE: OK (fallbacks=0, failed_jobs=0)`.
- **Reminder during intake** (ad hoc script): `/start-intake`, `/advance 5m`, then the reply `Aetna, member W123456789`. The 10:00 AM reminder went to the intake step with the in-flow wording, mentioning the appointment only briefly, without "reply 1/2", and then asked about insurance again. After the patient answered, the next step (visit reason) was asked. The pending follow-up moved to Tue 2:00 PM. Result line `SMOKE: OK (fallbacks=0, failed_jobs=0)`.
- **Eval rerun**: `uv run pytest evals -s`, `1 passed in 64.36s`, `--- 0 failed: []`, every judge passed. Summary of the whole-conversation judge's reasoning: rescheduling, insurance (including the corrected member ID), visit reason, allergies and medications, and the front desk handoff ($40 is the copay for the 10/2 visit) were all completed; it raised only one minor issue, that "W999" was accepted as is after the agent asked for the full member ID, without further checking, which does not affect the outcome.
- **Known deferred item**: the sentence "asks for something other than scheduling" in the rescheduling instructions overlaps with the escalation rules. If a front desk relay arrives while the patient is rescheduling, the agent may call `transfer_to_reception`; that turn then fails as a proactive handoff and is retried, without writing any partial data. The plan is to narrow the trigger to "no longer wants to reschedule" at the next prompt change, which will need a re-recording.

## Behavior eval (Plan 2)

- Date: 2026-11-13, commit 0ceab3f, run by the author with real Claude (claude-opus-5-5). The key is in a local, git-ignored file, loaded only into the environment with `uv run --env-file <that file>`; the code reads only `ANTHROPIC_API_KEY`. Command: `uv run --env-file <key file> pytest evals -s`. Result: `1 passed in 74.31s`, `--- 0 failed: []`, 22 agent LLM calls.
- All LLM judges go through `make_judge_llm()`, which is the FINDING-9 workaround. Without it, every LLM judge would raise TypeError before sending its request, and this eval could not run at all.
- No full phone numbers in the log.
- After the final review fixes it was rerun once at commit 45468a9 and passed: `1 passed in 64.36s`, every judge passed (details in `### Final review fixes: real-Claude checks` above).

### Check results (by the six main-line steps plus the end state)

| Group | Check | Result |
| --- | --- | --- |
| Global | No fallback SMS; `SMOKE: OK (fallbacks=0, failed_jobs=0)`; each of the 8 user messages triggers exactly one inbound run | PASS |
| 1 reminder | Exactly one SMS, at Tue 10:00 AM; job status `done` | PASS |
| 2 reschedule | `transfer_to_reschedule` called; handoff to RescheduleAgent; `book_slot` called; handoff to IntakeAgent; the booked slot is one offered in the reply | PASS |
| 3 intake | `insurance` and `visit_reason` saved after the two answers | PASS |
| 4 follow-up | Exactly one SMS, at Wed 8:00 AM; after `/restart`, insurance and visit reason are not brought up again; visit reason not re-asked | PASS |
| 5 regression | `out_of_scope` called; back to `allergies_meds` after the new member ID | PASS |
| 6 escalation | `escalate_to_staff` called; the front desk receives one SMS with the ticket and patient prefix (`[#1] Jane D. (555) 010-0001: ...`); after the relay the ticket is `closed`; the answer was relayed to Jane | PASS |
| End state | Intake complete and active is reception; the appointment carries the full intake (three keys); `member_id` is W999; `medications` is `["ibuprofen (daily)"]` | PASS |

Slot actually booked: slot 2, Mon 11/23, 9:00 AM with Dr. Lee.

### Judge verdicts

| Judge | Verdict | Reasoning (summary) |
| --- | --- | --- |
| reminder | pass | The reminder names Dr. Lee and Thu 11/19 10:00 AM and asks the patient to reply 1 to confirm or 2 to reschedule; the instructions ask for nothing more. |
| reschedule offer | pass | The offer gives specific available times next week, without slot IDs. |
| follow-up | pass | Only one short nudge, asking only about allergies and medications, and saying the patient can reply "none" if there are none. |
| relay | pass | One SMS explains that the $40 is a copay and nothing else is owed, then goes on to ask about allergies and medications; the 10/2 visit date mentioned in passing is not in the instructions, which the judge considers minor extra information. |
| whole conversation (task completion) | pass | Rescheduling, insurance (including the corrected member ID), visit reason, allergies and medications, and the front desk handoff were all completed. The judge noted two small flaws: jumping straight from the confirmation to the intake questions is a bit abrupt; "W999" is short for a full member ID, and the agent did not follow up on it. The outcome is not affected. |

### Caveats

- The offer judge sees only that message and the intent, with no date context, so it cannot tell whether the offered dates are right.
- The final whole-conversation judge did not receive a description of the goal.
- Both of these judges passed this time. If they return `maybe` or `fail` in a later run, consider these two points first, before deciding whether it is an agent problem.
- For claude-opus-5-5, the plugin downgrades the judge's forced `tool_choice` to `auto` (see FINDING-9). This time every judge returned a verdict.

---

## Spike 4 (Plan 2 mechanics)

- Date: 2026-11-13
- SDK: livekit-agents 1.8.11 + livekit-plugins-anthropic 1.8.11 (PyPI, unmodified), Python 3.13
- Script: `spikes/plan2_mechanics.py`. A one-off script, not maintained long term.
  - scripted mode: `uv run python spikes/plan2_mechanics.py --llm scripted`, no API key needed.
  - claude mode is run by the author (checkpoint CP1). It was run on 2026-11-13 at commit 966980b (the script is identical to the one committed in 15470de), with the log kept locally in `var/spikes/plan2_mechanics.log` (git-ignored); command: `mkdir -p var/spikes && uv run python spikes/plan2_mechanics.py --llm claude 2>&1 | tee var/spikes/plan2_mechanics.log`. The LLM is built by `clinic_sms.llm.make_claude_llm()`; without `ANTHROPIC_API_KEY` the exit code is 2. scripted mode makes 10 LLM calls in total (including the one for H3e), claude mode 9 (H3e is not run), plus a few more if Claude says more than expected.
  - `--wait-for-idle`: the variant from the Gate, which also does `await session.wait_for_idle()` after `run()` returns (for the H3 series, after the handle completes).
- All SDK source line numbers here refer to 1.8.11 as installed in `.venv`.
- scripted mode ran 3 times in a row with byte-identical output apart from timings, exit code 0 each time; 1 run with `--wait-for-idle` gave the same results.

### Setup

- Each check creates a new AgentSession to simulate a cold start: plain `start()`, wait for `ReadySignal`, then `run()` (H1, H1n, H2) or `generate_reply()` (the H3 series). `start` / `ready.wait` / `run` / `await handle` all have a 90-second timeout.
- Outbound messages are collected the same way as in the runner: `ChatMessage` items in `conversation_item_added` with role assistant and non-empty text. The script also labels the speaking agent by "which agent's `chat_ctx` contains this message's id". claude mode relies on this to check "only B's question"; scripted mode also compares the text word for word.
- `Probe(LLM)` wraps the real LLM and forwards `chat()` unchanged. Each call prints one line: `tool_choice` (`default` when not passed), the tool names the SDK offers, the number of `function_call` items in the context, the number of context items, and the last item. The inner LLM's `error` / `metrics_collected` events are forwarded to the Probe, so the session receives them as usual (AgentActivity subscribes to `session.llm`, `voice/agent_activity.py` lines 957–958).
- Agents:
  - `A`: `on_enter` only does `ready.set(self)`. Tool `go_b()` returns `B(resuming=False)`; `go_b_noted()` returns `(B(resuming=False), "moved to b")`; `go_g()` returns `G()`.
  - `B`: tool `lookup()` returns None and exists only so that the restored `lookup` function_call is not filtered out by `Agent.__init__` (`voice/agent.py` line 92). `on_enter` first does `ready.set(self)`, and calls `generate_reply(instructions=ASK_B)` only when `resuming=False`.
  - `G`: `on_enter` awaits a `ResumableTaskGroup(summarize_chat_ctx=False)` with a single step, `S(AgentTask)`. S's tool `save(value)` returns None, and S's `on_enter` calls `generate_reply(instructions=ASK_S)`. When the session closes, S is cancelled and G catches `ToolError` (FINDING-4).
- `ASK_B`, `ASK_S` and `NUDGE` are the same set of real prompts in both modes (ask for a color, ask for a city, a nudge); in scripted mode they are the script keys. The scripted replies follow the task description: `go b → go_b()`, `ASK_B → "What is B?"`, `go b noted → go_b_noted()`, `moved to b → "Moving you to b."`, `go g → go_g()`, `ASK_S → "What is S?"`, `NUDGE → "Still there? What is B?"`. `default=Reply()`: extra calls the SDK makes on its own are printed by the Probe (there were none this time).
- In claude mode, A's instructions spell out three rules: `go b` calls `go_b`, `go b noted` calls `go_b_noted`, `go g` calls `go_g`; do not reply with text before calling the tool.
- The H3 series resumes from B, with `chat_ctx` in this order: `user "hi"`, the `lookup` function_call and output (output is `""`), `assistant "What is B?"`. The H3/H3n verdict has one more precondition than the task description: "the context of the nudge call contains 1 function_call". Without it, H3n would not test "`tools: []` plus tool_use in the context".

### Results

| Check | scripted | Claude |
|---|---|---|
| H1 | PASS | PASS |
| H1n | PASS | PASS |
| H2 | PASS | PASS |
| H3 | PASS | PASS |
| H3n | PASS | PASS |
| H3e | OBSERVED (see below) | skipped by design (runs only in scripted mode) |

Last line in scripted mode: `SUMMARY: H1=PASS H1n=PASS H2=PASS H3=PASS H3n=PASS H3e=OBSERVED`. Last line in claude mode: `SUMMARY: H1=PASS H1n=PASS H2=PASS H3=PASS H3n=PASS H3e=SKIPPED`; none of the 5 checks had a session `error` event. H1–H3n logged nothing at WARNING or above, either during the checks or while closing the session.

**H1 (tool returns a new agent, no message): scripted PASS.** `run("go b")` returns in about 4–5 ms.

- RunResult events: `function_call go_b`, `function_call_output go_b`, `agent_handoff A->B`, `message/assistant`.
- Outbound is only `[B] 'What is B?'`, and `current_agent` is B.
- 2 LLM calls: A answering `go b` (`default`, tools `go_b, go_b_noted, go_g`); B's opening (`default`, only the `lookup` tool). The tool returns only an agent, so the output is None and `reply_required=False` (`voice/generation.py` line 1241), which is why A makes no call to reply to the tool output.
- The context of B's opening call has only 3 items: B's instructions, `agent_config_update`, `ASK_B`. Neither the user's `go b` nor A's tool call is there. When a tool returns a new agent, the SDK does not carry the old agent's context over; the new agent's `chat_ctx` has only what was passed in at construction (nothing here). In Task 6, the handoff has to decide for itself what context to pass to the new agent.
- Claude mode: PASS. `run()` returns in 4.6 s; outbound is only `[B] "What's your favorite color?"`, `current_agent=B`, `tool_choice` is `default` on both calls, no session errors.

**H1n (tool returns `(agent, message)`): scripted PASS.**

- RunResult events: `function_call go_b_noted`, `function_call_output go_b_noted`, `message/assistant`, `agent_handoff A->B`, `message/assistant`.
- Two outbound messages, in the right order: `[A] 'Moving you to b.'`, then `[B] 'What is B?'`. A's reply is in A's `chat_ctx`, and B's question is in B's `chat_ctx`. `current_agent` is B.
- `tool_choice` of each LLM call (scripted):

  | Call | Who | `tool_choice` | Tools the SDK offers | function_call in context |
  |---|---|---|---|---|
  | 1 | A answering `go b noted` | `default` | `go_b, go_b_noted, go_g` | 0 |
  | 2 | A replying to the tool output `moved to b` | **`none`** | `go_b, go_b_noted, go_g` | **1** |
  | 3 | B's opening `ASK_B` | `default` | `lookup` | 0 |

  Call 2 is the one the task description refers to: on handoff, `draining = True` (`voice/agent_activity.py` lines 4133–4134), and the reply to the tool output uses `"none"` (line 4184). The SDK still passes A's 3 tools to `chat()`; on `"none"`, the Anthropic plugin replaces them with `tools: []` (`plugins/anthropic/llm.py` lines 229–231), while the context holds 1 tool_use. So H1n in claude mode really tests "`tools: []` plus tool_use".
- Claude mode: PASS. `run()` returns in 4.1 s; `tool_choice` per call: 1=default, 2=none, 3=default. Call 2 is A replying to `moved to b`, with tools `go_b, go_b_noted, go_g` offered and 1 function_call in the context:
  - `[llm] call 2: tool_choice=none tools=['go_b', 'go_b_noted', 'go_g'] function_calls_in_ctx=1 ctx_items=5 last=function_call_output:'moved to b'`
  - Two outbound messages, in the right order: `[A] 'Moved to b as noted.'`, `[B] "Hi there, what's your favorite color?"`. The request was not rejected, and there were no session errors.

**H2 (the new agent's `on_enter` in turn awaits a TaskGroup): scripted PASS.** `run("go g")` returns in about 5 ms.

- RunResult events: `function_call go_g`, `function_call_output go_g`, `agent_handoff A->G`, `agent_handoff G->ResumableTaskGroup`, `agent_handoff ResumableTaskGroup->S`, `message/assistant`.
- Outbound is only `[S] 'What is S?'`; after `run()` returns, `current_agent` is S.
- When `run()` returns, G's `on_enter` is still awaiting the group, but the run does not wait for it. The reason: the handoff goes through `_update_activity_task` (`voice/agent_session.py` lines 2040–2051), which switches with `wait_on_enter=False` and then has the run watch the new agent's `on_enter` task; when G awaits an AgentTask in `on_enter`, the SDK registers a `_RunSuspension` for that awaiter (`voice/agent.py` lines 1064–1065, `voice/run_result.py` line 98), which keeps the run from hanging on it while the task is not done (`run_result.py` lines 270–274).
- After `run()` returns, the group's progress is already `pending=['s']`, `results={}`, `visited=['s']`, so Task 6 can save the snapshot at the end of this turn.
- Claude mode: PASS. `run()` returns in 4.8 s; outbound is only `[S] 'Which city do you live in?'`, `current_agent=S`, group progress `{'pending': ['s'], 'results': {}, 'visited': ['s']}`, no session errors.

**H3 (proactive message after a cold start): scripted PASS.** `handle = session.generate_reply(instructions=NUDGE)`, `await asyncio.wait_for(handle, 90)`.

- The restored `lookup` function_call was kept (there is 1 in the context after B is constructed), and the nudge call saw it too.
- Outbound is only `[B] 'Still there? What is B?'`. The restored `assistant "What is B?"` did not fire `conversation_item_added`, consistent with Spike 1 S4.
- `handle.exception()` is None; the last message in B's `chat_ctx` is this nudge (same id). The `generate_reply` instructions did not stay in the context.
- Claude mode: PASS. The handle completed in 4.1 s, and `handle.exception()` is None; the nudge call had `function_calls_in_ctx=1`; outbound is a single nudge message from `[B]`, no session errors.

**H3n (same as H3, with `tool_choice="none"`): scripted PASS.** Same results as H3. The only LLM call: `tool_choice=none`, the SDK offers the `lookup` tool, 1 function_call in the context. So in claude mode the plugin sends `tools: []` with a tool_use in the context.

- Claude mode: PASS. The only call was `tool_choice=none tools=['lookup'] function_calls_in_ctx=1`, i.e. the plugin sends `tools: []` plus the tool_use in the context; Anthropic accepted it, the handle completed in 3.3 s, `exception()` is None, one outbound `[B]` message, no session errors.

**H3e (same as H3, but the reply to NUDGE is `Reply(error=APIError("down", retryable=False))`; runs only in scripted mode and is skipped in claude mode by design): OBSERVED.**

- `handle.exception()`: `APIError: down`. `await handle` itself did not raise (`SpeechHandle.__await__` never raises and keeps the error in `exception()`, `voice/speech_handle.py` lines 183–199, 352–358; when the LLM task fails, `voice/agent_activity.py` line 3669 writes it into the handle).
- Outbound is empty; B's `chat_ctx` is unchanged, and its last message is still the restored `assistant 'What is B?'`.
- The SDK logged 1 ERROR: `livekit.agents: Error in _llm_inference_task`, with exc_info (`APIError: down`). It comes from `utils.log_exceptions` on `_llm_inference_task` (`voice/generation.py` line 189).
- The session emitted 1 `error` event: `APIError: down`, `recoverable=False`. It counts toward the session's `max_unrecoverable_errors` (default 3, `voice/agent_session.py` lines 194, 2071–2074), and only going over that closes the session; with a new session per cold start, this limit is never reached.
- Only a non-retryable error was tested. A retryable error (`APIError` defaults to `retryable=True`) would first be retried by LLMStream; that was not tested here.

### Conclusions

The Gate did not trigger: H1, H2 and H3 passed outright in both scripted and claude modes, without needing `wait_for_idle`. One scripted run with `--wait-for-idle` gave the same results; claude mode was not run with this flag.

1. **Can the runner collect all outbound messages along the handoff chain from a single `run()`? Yes (confirmed in both scripted and claude modes).**
   - H1: the tool returns only a new agent; the new agent's opening question is collected in the same `run()`, and the RunResult has the A→B `agent_handoff`.
   - H1n: both the old agent's reply to the tool message and the new agent's question are collected, the old agent's first.
   - H2: when the new agent's `on_enter` in turn awaits a TaskGroup, the innermost step's question is collected too; after `run()` returns, `current_agent` is that step.
   - So the Task 6 runner can keep the current approach (`run()` plus `conversation_item_added`), with no extra waiting.
   - Claude mode (2026-11-13, 966980b): H1, H1n and H2 all PASS; a single `run()` collects the outbound messages along the whole handoff chain, the same behavior as scripted.
2. **How do we detect a failed proactive message? In scripted mode both checks work; H3e has only been run in scripted mode, so this answer still rests on scripted evidence.**
   - When the LLM call fails, `handle.exception()` returns the original exception (`APIError` in H3e), and outbound is empty; on success, `exception()` is None (H3, H3n).
   - The two checks cover different cases: `handle.exception()` can tell "the call failed" apart from other cases and carries the exception type; "outbound is empty" should, by inference, also catch "the call succeeded but produced no text" (for example, the model replied only with a tool call), a case where `exception()` is None (this is inferred, not tested). Task 7 must do both.
   - The SDK itself logs an ERROR (`Error in _llm_inference_task`) and emits a session `error` event. When Task 7 logs the failure, it should avoid repeating the stack trace the SDK already logged.
   - Claude mode: on the success path of H3 and H3n, `handle.exception()` is None and outbound is non-empty, consistent with scripted. H3e (error detection) was not run in claude mode, so it has not been verified against a real API error.
3. **Does Anthropic accept a request with "`tools: []` plus tool_use in the context"? Yes.**
   - Claude mode (2026-11-13, 966980b): call 2 of H1n and the H3n call both had `tool_choice=none` and 1 function_call in the context; the plugin sent `tools: []`, and both requests succeeded, with no 400 and no session error events.
   - No new FINDING. Task 6 Step 0 is not needed.

## Notes (Plan 2)

Observations that do not count as FINDINGs are recorded here. Whether to report them upstream is for the author to decide later.

### SDK WARNING/ERROR logs carry exception text (Task 4)

- **Symptom**: the places below write exception text into WARNING or ERROR records. If an LLM error or a tool exception echoes the patient's exact words, those words end up in the log.
  - `utils/log.py:22`: the `log_exceptions` decorator calls `logger.exception`, and the exc_info carries the exception message. For example, `_llm_inference_task` at `voice/generation.py:189`: when the LLM call fails, the SDK logs `Error in _llm_inference_task`, and the last line of the traceback is `APIError: <message>`.
  - `llm/llm.py:371`: the retry warning puts `{e}` into the message with an f-string.
  - `voice/agent_session.py:2081`: `AgentSession is closing due to unrecoverable error: {error.error}`.
  - `voice/generation.py:1059`, `:1105`: when a tool raises an exception other than ToolError, `logger.exception` is called.
  - Other tool errors do not go through these paths: argument validation failures (`generation.py:949`) and ToolError (`:1050`) both put the error text in the `lk.pii.error` extra, which the default format does not print.
- **SDK version**: livekit-agents 1.8.11.
- **Handling**: logging exceptions is normal SDK practice. This project's rule that "the patient's words must never appear in logs" is stricter than the SDK's, so redaction follows the application's own logging policy:
  - Implementation: `install_sdk_log_redaction()` in `clinic_sms/logsafe.py` adds a filter to each of the `livekit.agents` and `livekit.plugins.anthropic` loggers; `ConversationRunner.__init__` installs it.
  - Redaction rules: only records at WARNING and above are processed. exc_info is replaced with the result of `describe_exception`, which has only the type name and the call stack; exception text that appears in the message, and whatever the two f-strings above put in, is replaced with `<redacted>`.
  - Records are rewritten, never dropped.
  - Tests: `tests/test_runner.py::test_failure_logs_carry_no_exception_text`, `tests/test_logsafe.py`.
- Not recorded as a FINDING, and there is no `SDK-FINDING` marker in the code.
