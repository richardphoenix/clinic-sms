# Spike: the SDK's room text channel (RoomIO text input/output)

Date: 2026-11-17. Scope: a bounded spike, separate from the demo. The demo answers each simulated SMS with a headless, cold-started `AgentSession` that never joins a room, so the SDK's own text channel (RoomIO's `lk.chat` text input and `lk.transcription` text output) was never exercised. This spike puts an agent into a real LiveKit room with text only and probes how the SDK behaves on that channel. The demo's main flow is unchanged.

Versions: livekit-agents 1.8.11 (PyPI), livekit (rtc) 1.2.0, livekit-server 1.13.11 in `--dev` mode (Homebrew, local only), Python 3.13, macOS. Upstream `main` at [0cfc030](https://github.com/livekit/agents/commit/0cfc0306335cc8b3633ec2cdce072a3f340cb14e) (2026-11-17) was checked for each candidate below; see "Status on main".

All messages are synthetic markers (`m1`, `slow`, `transfer`, ...). No phone numbers, no patient text, no API key: every LLM call is a scripted fake.

## Summary

| # | What was tried | Result | Candidate |
|---|---|---|---|
| 1 | Five texts sent back to back | Varies per run: 1 reply or 4 separate replies (each blind to the rest of the burst); texts out of send order in the chat context in 6/8 runs | FINDING-12 |
| 2 | A new text while a reply is streaming | Works as designed: the reply is cut and the new text answered | (note) |
| 3 | A text in the handoff turn | **Dropped** (never in the chat context, never answered) for the whole of the old agent's post-tool reply; the old reply is cut too | **FINDING-11** |
| 4 | AgentTask / TaskGroup inline from a tool | Works; texts at the edges go to whichever agent is active | — |
| 5 | `on_enter` proactive message | Delivered when the participant is linked first; **lost but recorded as said** when the agent speaks before a participant is linked | FINDING-13 |
| 6 | 20 s idle (> `user_away_timeout`) then a text | Works; user state goes `away` → `speaking` → `listening` | — |
| 7 | Participant leaves and rejoins; network reconnect | Works within limits; see notes (agent left in the room without a session after `close_on_disconnect`) | (open question) |
| 8 | Two text participants in one room | Only the linked participant is heard; the other is ignored silently and sees the agent's replies to the first | (note) |
| 9 | Closing the session with a reply pending | `aclose()` cuts it (partial text, closed as final); `shutdown(drain=True)` finishes it | (note) |
| 10 | A text while the agent says something with `allow_interruptions=False` | **Dropped** (never in the chat context, never answered) | **FINDING-10** |
| 11 | A text right after joining a room where the agent waits | **Dropped** ("participant not found") until the agent learns of the user, ~3 s on the dev server | FINDING-14 |

Five candidates. Four are about a message that silently disappears. Three user texts (10, 11, 14) are dropped inside RoomIO, which logs a WARNING and discards the text whenever the text input callback raises or the sender is not yet known; the chat context never sees it, so neither the LLM nor anything downstream (history, transcripts, evals) can tell. One agent message (13) is never sent, yet the history records it as said. The fifth (12) loses nothing but scrambles a burst of texts. The two most worth reporting upstream are FINDING-11 and FINDING-10 (see the end). Both are now filed as one issue, [#8606](https://github.com/livekit/agents/issues/8606).

## Setup

Files (all under `spikes/text_channel/`):

- `common.py`: local server URL and dev credentials, the dispatch metadata `Cfg`, the fake LLM, and the agents.
- `worker.py`: an `AgentServer` (thread executor, explicit dispatch as agent `text-spike`). The entrypoint starts an `AgentSession` with `RoomOptions(audio_input=False, audio_output=False, text_output=TextOutputOptions(sync_transcription=False))`, the default text input, and the stock `text_input_cb`. A `Probe` records the session's conversation items, user/agent state, room participant events and WARNING+ log records, and answers a control topic (`spike.ctl`: dump / close / drain / speak uninterruptibly) with a JSON dump on `spike.dump`.
- `driver.py`: the text participant(s), using the livekit rtc Python SDK. Each scenario joins a fresh room with a token that dispatches the agent, sends on `lk.chat`, reads every `lk.transcription` stream chunk by chunk (text, `lk.transcription_final`, stream errors), and asks the worker for a dump. Results are written to `var/spikes/text-channel/<scenario>.json` (git-ignored).

Agents: `Reception` greets in `on_enter` and has three tools: `transfer` returns `(Billing(), "transferred")` (a handoff; `Billing` greets in `on_enter`), `start_intake` awaits an inline `AgentTask` (`StepTask`, which greets and completes on a `save` tool call), and `start_group` awaits a two-step `TaskGroup`. The fake LLM answers from the last conversation item: `slow` streams 8 chunks over ~2 s, `transfer` / `intake` / `group` / `save X` call the matching tool, anything else gets `[<agent>] got <text>`; a turn with no user text last gets `[<agent>] hello`. It also records, per call, which agent answered and which user/assistant messages it was shown.

Run:

```bash
livekit-server --dev                                  # terminal 1
uv run python spikes/text_channel/worker.py start     # terminal 2
uv run python spikes/text_channel/driver.py           # all scenarios, or name some: rapid handoff ...
```

The local server was shared with another session's tests during the spike; every scenario uses its own random room name, so they did not interact.

A server fact that shapes several results: on this `--dev` server, the participants already in a room learn of a newly joined standard participant about 3 s after it joins (2.95–3.0 s in each of 8 measurements), while its data (text streams) reaches them at once. (The newcomer itself gets the participants already present with its join response.) A leave is likewise announced ~3 s late (2.7 s measured), and a leave followed by a rejoin within that delay reaches the others as one "disconnected (`UNKNOWN_REASON`) + connected" pair. Whether LiveKit Cloud or a non-dev server behaves the same was not checked.

## Results by scenario

### 1. Several texts in a row (`rapid`)

Five texts sent in order by one client, back to back (`m1`..`m5` within 2 ms). All five always reach the chat context, but what happens next varied from run to run. 8 runs:

| Runs | Replies delivered | User texts in the agent's chat context |
|---|---|---|
| 2 | `got m2`, `got m3`, `got m4`, `got m5` | `m1 m2 m3 m4 m5` |
| 1 | `got m3`, `got m4`, `got m2`, `got m5` | `m1 m3 m4 m2 m5` |
| 2 | `got m3`, `got m4`, `got m5`, `got m2` | `m1 m3 m4 m5 m2` |
| 1 | `got m2`, `got m4`, `got m3`, `got m5` | `m1 m2 m4 m3 m5` |
| 1 | `got m5` only | `m1 m3 m4 m2 m5` |
| 1 | `got m5` only | `m2 m1 m3 m4 m5` |

The reply to the first text handled was always cut before it produced text. In the runs with four replies whose LLM calls were recorded (2 of the 6), each call was shown only the conversation before the burst, the first text handled, and its own text, e.g.:

```
reception sees ['a:[reception] hello', 'u:m1', 'u:m2']
reception sees ['a:[reception] hello', 'u:m1', 'u:m3']
reception sees ['a:[reception] hello', 'u:m1', 'u:m4']
reception sees ['a:[reception] hello', 'u:m1', 'u:m5']
```

So a burst gets either one reply that sees all of it, or one reply per text, each blind to the others; the texts are out of send order in the chat context in 6 of 8 runs, and the replies follow that order. When the texts are spaced further apart than one reply (scenario 2), each new text interrupts the previous reply instead, and the reply sees everything in order. See FINDING-12.

### 2. A new text while a reply is streaming (`overlap`)

`slow` (a ~2 s reply), then `m2` 0.7 s later. The participant receives `slow part 0. slow part 1. ` (3 chunks) in a stream closed with `lk.transcription_final: "true"`, then `got m2`. The history keeps the cut reply with `interrupted=True`, and the LLM call for `m2` is shown the cut text. This is the default callback's design (`interrupt()`, then reply).

Note for a text channel: nothing on the wire says the delivered reply was cut. The stream closes exactly like a complete one (`lk.transcription_final: "true"`, no interruption attribute), so a chat UI shows a half sentence as a finished message. Only the agent's history knows. Not filed as a candidate.

### 3. A text in the handoff turn (`handoff`, `handoff_window`)

`transfer` (the tool returns `Billing()`), then `m2` after a delay d. With the fast fake LLM:

| d | What the participant receives | `m2` in history | Logs |
|---|---|---|---|
| 0 – 0.06 s | `got m2` from Reception; no handoff (the text interrupted the turn before the tool ran) | yes | — |
| 0.08 – 0.10 s | Billing's greeting only; Reception's post-tool reply is cut to nothing | **no** | WARNING `failed to handle chat text stream [RuntimeError: cannot schedule new speech, the speech scheduling is draining/pausing ...]`, ERROR `Error in _llm_inference_task [RuntimeError: no activity context found ...]` |
| ≥ 0.12 s | `after transferred`, then `got m2` from Billing (Billing's greeting is cut if still streaming) | yes | — |

With Reception's post-tool reply slowed to ~2 s (`slow_after=True`), 3 runs per d: d = 0.1, 0.5, 1.0, 1.5 s lost `m2` in 3/3 runs each; d = 2.5 s (after the reply) lost 0/3. In every lost run Reception's reply was also cut. So the window is the whole of the old agent's reply after the tool, which with a real LLM is the normal case of "a text arrives while the agent is saying 'let me transfer you'". See FINDING-11.

### 4. AgentTask and TaskGroup (`task`)

Inline `AgentTask` (`intake` → task greeting → `m2` → `got m2` from the task → `save x` → `after intake=x` from Reception) and a two-step `TaskGroup` (`group` → step a → `save 1` → step b → `save 2` → `after group=[('a','1'),('b','2')]`) both work over the text channel, including the greetings of each step. Texts sent 0 s and 0.1 s after the turn that starts a task, completes it, or switches steps were all kept and answered by whichever agent was active at that moment (at 0 s after `intake`, the text interrupts the turn before the tool runs, so the task never starts — same as the handoff row d ≤ 0.06 s). No drops at the points swept; the inline-task paths were not swept as finely as the handoff.

When the user left while a task was still waiting for input, the session closed (`close_on_disconnect`) and the worker logged `ToolError while executing tool` for `start_intake` / `start_group` (4 times in the full run): the tool awaiting the inline task gets a ToolError at close. That is the family of FINDING-4, not new.

The TaskGroup's default `summarize_chat_ctx=True` makes one extra LLM call when the group ends (the fake LLM received the summarization prompt), and the parent agent then sees a `<chat_history_summary>` instead of the steps' messages. Expected, but worth knowing on a per-message-billed channel.

### 5. Proactive message from `on_enter` (`greet`)

- The participant joins with a dispatching token, so the agent starts with the participant already in the room: greeting received in 6/6 runs, both when the entrypoint awaits `ctx.connect()` before `session.start()` and when `session.start()` connects the room itself.
- The agent is dispatched into an empty room and the user joins 1.5 s later: **greeting never received**, yet the history holds `[reception] hello` as an ordinary, not interrupted, assistant message. The channel itself works: the user's next text is answered.
- Same, with `await ctx.wait_for_participant()` before `session.start()`: greeting received.
- After a handoff, the new agent's `on_enter` greeting reaches the participant (scenario 3), as do the greetings of AgentTask steps (scenario 4).

See FINDING-13. Related upstream: [#6577](https://github.com/livekit/agents/issues/6577) / PR [#6578](https://github.com/livekit/agents/pull/6578) (both open), where `console --text` never shows agent-initiated messages such as the `on_enter` greeting. Same symptom, different path: the console renders only `session.run()` events, while here RoomIO's text output drops text that has no linked participant.

### 6. Long idle (`idle`)

After the greeting, 20 s of silence: `user_state` turns `away` at 15.0 s (the default `user_away_timeout`); a text at 20 s is answered normally and the state goes `away` → `speaking` → `listening`. No problem found. Longer idle times (minutes, hours) were not tried.

### 7. Leaving, rejoining, reconnecting (`reconnect`)

- Default options (`close_on_disconnect=True`), the user leaves with `room.disconnect()` and rejoins after 1.5 s: the agent receives one coalesced "disconnected (`UNKNOWN_REASON`) + connected" pair at the rejoin, so the session survives and the next text is answered.
- Same, rejoining after 5 s: the agent sees `CLIENT_INITIATED` ~2.8 s after the leave and closes the session (as documented). But the agent participant stays in the room: the rejoined user sees an agent, and its texts go unanswered, with nothing logged (RoomIO unregistered its `lk.chat` handler when the session closed). When the job itself ends was not checked. Open question, not filed: whether the job is expected to end with the session.
- `close_on_disconnect=False`, the user leaves 0.6 s into a ~2 s reply and rejoins 1.5 s later: the user got two chunks and a `StreamError: Disconnected while receiving`; the rest of the reply went to nobody, yet the history records the full reply as delivered. After the rejoin the next text is answered. (Text streams have no delivery acknowledgement, so this is inherent; it is the same "recorded as said, never seen" gap as FINDING-13.)
- A transport-level full reconnect (`SIMULATE_FULL_RECONNECT`) 0.6 s into a reply: the user got two chunks and `StreamError: ... unexpectedly disconnected in the middle of sending data`; the session survives (coalesced events again) and the next text is answered.

### 8. Two text participants (`multi`)

User A dispatches the agent; user B joins the same room. B's text is ignored: not answered, not in the history, nothing logged (`room_io.py:516-519` returns without a log line). A's text is answered, and B receives that reply too, because the agent's `lk.transcription` streams are sent to the whole room. This matches RoomIO's design (it links one participant; `set_participant()` switches), but the silent drop makes a misrouted participant hard to debug. Not filed.

### 9. Closing with a reply pending (`close`)

- `aclose()` 0.6 s into a ~2 s reply: the user receives the first chunks in a stream closed as final; the history marks the reply `interrupted=True`. The agent participant stays in the room.
- `shutdown(drain=True)` at the same point: the whole reply is delivered (8 chunks), then the session closes.

Both as designed. As in scenario 2, the cut reply looks final on the wire.

### 10. A text during a speech that disallows interruptions (`locked`)

The agent starts a ~2 s reply with `allow_interruptions=False`; the user sends `m1` 0.4 s in, and `m2` after the reply. `m1` is never answered and never enters the history; the worker logs `WARNING livekit.agents: failed to handle chat text stream [RuntimeError: This generation handle does not allow interruptions]`. `m2` is answered normally. See FINDING-10.

### 11. A text right after joining (`repro/finding_14.py`)

Found while building FINDING-13's repro. The agent waits in the room; the user joins and sends a text 0.2 s or 1.0 s later: dropped with WARNING `participant not found, ignoring text input`, in 3/3 runs each. The agent links the user ~3 s after the join (the server delay above); a text sent after that is answered. See FINDING-14.

## FINDING candidates

Numbered after FINDING-9 of `docs/findings.md`, in that file's entry format. They are candidates: kept here, not yet in `docs/findings.md`. Each has a standalone repro in `repro/` with the same contract as the others (prints what it observed; exit 1 while it reproduces, 0 once it no longer does, 2 if a control fails). Line numbers refer to livekit-agents 1.8.11 as installed in `.venv`.

### Upstream search method

All searches 2026-11-17, read-only `gh search issues` and `gh search prs` with `--repo livekit/agents`, issues and PRs for every term. Per-term lists are in each entry. Broad sweeps whose titles were all read: `lk.chat` (14 issues, 6 PRs), `text input` (40 issues, 40 PRs), `RoomIO text`, `chat text stream`, `text stream handler`. GitHub's search recall is uneven (`on_enter greeting` finds [#6577](https://github.com/livekit/agents/issues/6577), `on_enter greeting text` does not), hence the short terms and the sweeps. The first batch hit the search API's rate limit (HTTP 403) on 11 queries, which were rerun. Also read in full: [#6577](https://github.com/livekit/agents/issues/6577) and PR [#6578](https://github.com/livekit/agents/pull/6578) (console text mode), [#4443](https://github.com/livekit/agents/issues/4443) with its comments and fix PRs [#6451](https://github.com/livekit/agents/pull/6451) / [#8419](https://github.com/livekit/agents/pull/8419), [#4780](https://github.com/livekit/agents/issues/4780), PR [#8041](https://github.com/livekit/agents/pull/8041) (its `room_io/types.py` diff), PR [#7685](https://github.com/livekit/agents/pull/7685), PR [#7633](https://github.com/livekit/agents/pull/7633). Looked at and not related: [#2451](https://github.com/livekit/agents/issues/2451) (agent state when a chat text interrupts), [#4535](https://github.com/livekit/agents/issues/4535) (`lk.chat` callback missing after the first message), [#3217](https://github.com/livekit/agents/issues/3217), [#3923](https://github.com/livekit/agents/issues/3923), PR [#7427](https://github.com/livekit/agents/pull/7427).

### Status on main

Upstream `main` 0cfc030 (2026-11-17): `voice/room_io/types.py`, `voice/room_io/room_io.py`, `voice/room_io/_output.py` and `voice/speech_handle.py` are byte-for-byte identical to 1.8.11. `voice/agent_session.py` and `voice/agent_activity.py` differ, but not in the code cited below (only line numbers shift: on main, `generate_reply`'s activity choice is at agent_session.py:1678, the `_scheduling_paused = True` of a new activity at agent_activity.py:365, and the `cannot schedule new speech` raise at 2078–2082). FINDING-10, -11 and -12's repros were also run against the local upstream clone, whose `voice/` files that matter here (`agent_activity.py`, `agent_session.py`, `room_io/`, `speech_handle.py`) are identical to main 0cfc030: all three reproduce.

### FINDING-10: A text that arrives while the agent says something with allow_interruptions=False is dropped

- **Symptom**: with RoomIO's default text input, a user text that arrives while the current speech disallows interruptions is lost: it never enters the chat context and is never answered, now or after the speech. The only trace is a WARNING with a traceback (`failed to handle chat text stream`, `RuntimeError: This generation handle does not allow interruptions`). The same text sent after the speech is answered normally. With interruptions disabled for the whole session (`turn_handling=TurnHandlingOptions(interruption={"enabled": False})`), every reply is uninterruptible. Every text sent while the agent replies is then lost (checked on main 0cfc030).

  For comparison, the voice path handles the same moment on purpose: by default it replaces the user's audio with silence while an uninterruptible speech plays (`discard_audio_if_uninterruptible=True`, `voice/turn.py` line 192; `voice/agent_activity.py` lines 1712–1718; the reason is echo), and if a user turn still completes it logs `skipping reply to user input, current speech generation cannot be interrupted` and skips only the reply, keeping the user message in the chat context when the audio was kept (`agent_activity.py` lines 2865–2880). So voice also does not answer such input; the difference is that the text path loses the message through an unhandled exception, whatever the settings, although typed text cannot be echo.
- **Minimal repro**: `repro/finding_10.py`. Standalone, livekit-agents only, inline fake LLM, no key, no network. It calls the stock default callback (`TextInputOptions().text_input_cb`) the way RoomIO's `_read_text` does (same call, same `except`), while a 1 s reply with `allow_interruptions=False` streams; control: the same with `allow_interruptions=True`. Run `uv run python repro/finding_10.py`. Output (2026-11-17, identical across 3 runs):

  ```
  livekit-agents 1.8.11
  locked   (allow_interruptions=False): {'dropped_with': 'RuntimeError: This generation handle does not allow interruptions', 'm1_in_history': False, 'm1_answered': False}
  control  (allow_interruptions=True) : {'dropped_with': None, 'm1_in_history': True, 'm1_answered': True}
  REPRODUCED: the default text input callback raised from session.interrupt(); RoomIO would log a WARNING and drop 'm1' (never in the chat context, never answered)
  ```

  End to end through a real room: spike scenario `locked` (above).
- **SDK version**: livekit-agents 1.8.11 (PyPI), Python 3.13; also main 0cfc030 (see "Status on main").
- **Root cause**:
  - The default callback is `async with sess._claim_user_turn(): await sess.interrupt(); sess.generate_reply(user_input=ev.text)` (`voice/room_io/types.py` lines 46–49).
  - `AgentSession.interrupt()` → `AgentActivity.interrupt()` calls `self._current_speech.interrupt(force=force)` without catching (`voice/agent_activity.py` lines 1963–1964); `SpeechHandle.interrupt()` raises `RuntimeError` when the handle disallows interruptions and `force` is False (`voice/speech_handle.py` lines 222–223). Queued speeches get a try/except and a warning (lines 1971–1982); the current one does not.
  - So `generate_reply()` never runs, and the message was never added anywhere.
  - RoomIO runs the callback in a task whose `except Exception` logs a WARNING and returns (`voice/room_io/room_io.py` lines 533–543). Nothing re-delivers the text.
- **Classification**: SDK bug, with a design question attached. `interrupt()` behaves as documented ("Raises: RuntimeError: If the speech currently playing disallows interruptions and force is False"); the default callback built on it does not handle that case, so the message is lost through an exception path and a WARNING with a traceback, not through a decision. What the right behavior is (answer after the speech, or keep the message but skip the reply as voice does with the audio kept) is for upstream to choose; losing the message is not one of the voice path's outcomes for text that is not echo.
- **Workaround**: none in this repo (the demo does not use RoomIO). In an app, use a custom `text_input_cb` that catches the RuntimeError from `interrupt()` and still calls `generate_reply()`. The reply then queues behind the uninterruptible speech. Checked on 1.8.11 and main: with such a callback, `repro/finding_10.py` prints NOT REPRODUCED, and the text is answered after the speech.
- **Upstream status**: searched 2026-11-17 (read-only `gh search issues` / `gh search prs`, `--repo livekit/agents`). Terms (issues and PRs): `text_input_cb`, `failed to handle chat text stream`, `allow_interruptions text input`, `does not allow interruptions`, `text message dropped`, `text input ignored`, plus the broad sweeps (see "Upstream search method"). No issue or PR covers the text input path.
  The closest: [#4443](https://github.com/livekit/agents/issues/4443) (open) has the same RuntimeError text, but from the voice path: `_user_turn_completed_task` interrupting an outdated reply that inherited `allow_interruptions=False`; its fix PRs [#6451](https://github.com/livekit/agents/pull/6451) and [#8419](https://github.com/livekit/agents/pull/8419) (both open) change only `agent_activity.py`'s user-turn path, not the text input callback. [#4780](https://github.com/livekit/agents/issues/4780) (closed; fixed by PR [#4807](https://github.com/livekit/agents/pull/4807), merged) is where the default callback came to `await sess.interrupt()`, the call that raises here. [#8041](https://github.com/livekit/agents/pull/8041) (open) rewrites the callback's last line (`await sess._reply_to_user_text(ev.text)`) but keeps `await sess.interrupt()` before it, so it does not change this. Filed 2026-11-17, with the user's approval, together with FINDING-11 as [#8606](https://github.com/livekit/agents/issues/8606).

### FINDING-11: A text that arrives during an agent handoff is dropped, and the old agent's reply is cut

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

  End to end through a real room: spike scenarios `handoff` and `handoff_window` (above).
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
- **Filed**: 2026-11-17, with the user's approval, together with FINDING-10 as [#8606](https://github.com/livekit/agents/issues/8606).

### FINDING-12: Texts that arrive together are handled concurrently: out of order in the chat context, and either one reply or one reply per text, each blind to the rest

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

  End to end through a real room: spike scenario `rapid` (the 8-run table above).

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

### FINDING-13: In a text-only room session, what the agent says before a participant is linked (e.g. the on_enter greeting) is never sent, yet recorded as said

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
  - The stream is sent to the whole room at that moment, so text produced while the linked user is temporarily away is lost the same way (spike scenario 7).
- **Classification**: SDK bug, of the silent kind: text-only mode drops agent output that voice mode would hold, and records it as delivered. Arguably "uncommon usage" when the app could wait for the participant first, but the SDK gives no sign, and the voice-first examples that greet in `on_enter` without waiting work in voice and fail silently in text.
- **Workaround**: `await ctx.wait_for_participant()` (or `session.room_io.wait_for_ready()`) before the first proactive message. The demo is unaffected (no RoomIO).
- **Upstream status**: searched 2026-11-17. Terms: `text only greeting`, `greeting not received text`, `transcription before participant`, `text output participant`, `text-only mode`, `wait_for_participant greeting`, `lk.transcription first message`, `greeting lost`, `on_enter greeting text`, `on_enter greeting`, `greeting text mode`, plus the broad sweeps. No issue or PR about RoomIO dropping agent text before a participant is linked.
  Same symptom on another path: [#6577](https://github.com/livekit/agents/issues/6577) (open) / PR [#6578](https://github.com/livekit/agents/pull/6578) (open): `console --text` never shows agent-initiated messages such as the `on_enter` greeting, because the console renders only `session.run()` events. That fix is in `cli/_legacy.py` and does not touch RoomIO.

### FINDING-14: A text sent right after joining a room where the agent is already waiting is dropped

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

## Not filed

- **Interrupted replies look final on the wire** (scenarios 2, 9): the closing header of a cut `lk.transcription` stream is the same as a complete one. A frontend cannot mark a cut reply. Feature gap; not searched.
- **The agent stays in the room after `close_on_disconnect` closed its session** (scenario 7): a user who comes back finds an agent that never answers. Possibly by design; needs a look at the job lifecycle before calling it anything.
- **A second text participant is ignored without a log line** (scenario 8): by design; a debug log would help.
- **Participant join/leave announcements ~3 s late on the dev server**: a server behavior, not investigated. It makes FINDING-14 easy to hit and coalesces quick leave/rejoin pairs (scenario 7).

## Most worth reporting upstream

1. **FINDING-11** (text during a handoff is dropped). Handoffs are a core feature. The window is the whole of the old agent's post-tool reply, which takes seconds with a real LLM, and the loss is silent. Voice drops such a turn too, but on purpose, next to a TODO that asks whether to forward it. Upstream already fixed the same error for async tool replies (#7685), so there is a precedent: wait for the switch to finish before scheduling the reply. For user input the wait must go through the session's activity lock, not the paused activity (see the entry's workaround).
2. **FINDING-10** (text during an uninterruptible speech is dropped). Deterministic, a short repro with no timing in it, and a small fix in the default callback: don't let `interrupt()`'s RuntimeError abort the reply. With session-wide interruptions off it hits every text sent during a reply. It sits next to the open #4443 family, which fixes the voice path only.

Both are in RoomIO's default text callback and share the drop in `_read_text`, and one change to the callback fixes both. They were filed together, with the user's approval, on 2026-11-17 as [#8606](https://github.com/livekit/agents/issues/8606). The issue's proposed callback was checked on 1.8.11 and main against both repros and against an AgentTask switch. The draft and its offline repro are kept outside this repo with the other upstream drafts. FINDING-12, -13 and -14 are not filed. Any further upstream action needs the user's approval each time.
