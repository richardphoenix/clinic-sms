# Spike: the SDK's room text channel (RoomIO text input/output)

Date: 2026-11-17. Scope: a bounded spike, separate from the demo. The demo answers each simulated SMS with a headless, cold-started `AgentSession` that never joins a room, so the SDK's own text channel (RoomIO's `lk.chat` text input and `lk.transcription` text output) was never exercised. This spike puts an agent into a real LiveKit room with text only and probes how the SDK behaves on that channel. The demo's main flow is unchanged.

Versions: livekit-agents 1.8.11 (PyPI), livekit (rtc) 1.2.0, livekit-server 1.13.11 in `--dev` mode (Homebrew, local only), Python 3.13, macOS. Upstream `main` at [0cfc030](https://github.com/livekit/agents/commit/0cfc0306335cc8b3633ec2cdce072a3f340cb14e) (2026-11-17) was checked for each candidate below; see "Status on main".

All messages are synthetic markers (`m1`, `slow`, `transfer`, ...). No phone numbers, no patient text, no API key: every LLM call is a scripted fake.

## Summary

| # | What was tried | Result | Finding |
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

Five findings, now FINDING-10 to FINDING-14 in [docs/findings.md](../findings.md). Four are about a message that silently disappears. Three user texts (10, 11, 14) are dropped inside RoomIO, which logs a WARNING and discards the text whenever the text input callback raises or the sender is not yet known; the chat context never sees it, so neither the LLM nor anything downstream (history, transcripts, evals) can tell. One agent message (13) is never sent, yet the history records it as said. The fifth (12) loses nothing but scrambles a burst of texts. The two most worth reporting upstream are FINDING-11 and FINDING-10 (see the end). Both are now filed as one issue, [#8606](https://github.com/livekit/agents/issues/8606), with fix PR [#8607](https://github.com/livekit/agents/pull/8607).

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

## Findings

The five entries are FINDING-10 to FINDING-14 in [docs/findings.md](../findings.md), moved there on 2026-11-17. The two notes below apply to all five. Each has a standalone repro in `repro/` with the same contract as the others (prints what it observed; exit 1 while it reproduces, 0 once it no longer does, 2 if a control fails). Line numbers refer to livekit-agents 1.8.11 as installed in `.venv`.

### Upstream search method

All searches 2026-11-17, read-only `gh search issues` and `gh search prs` with `--repo livekit/agents`, issues and PRs for every term. Per-term lists are in each entry. Broad sweeps whose titles were all read: `lk.chat` (14 issues, 6 PRs), `text input` (40 issues, 40 PRs), `RoomIO text`, `chat text stream`, `text stream handler`. GitHub's search recall is uneven (`on_enter greeting` finds [#6577](https://github.com/livekit/agents/issues/6577), `on_enter greeting text` does not), hence the short terms and the sweeps. The first batch hit the search API's rate limit (HTTP 403) on 11 queries, which were rerun. Also read in full: [#6577](https://github.com/livekit/agents/issues/6577) and PR [#6578](https://github.com/livekit/agents/pull/6578) (console text mode), [#4443](https://github.com/livekit/agents/issues/4443) with its comments and fix PRs [#6451](https://github.com/livekit/agents/pull/6451) / [#8419](https://github.com/livekit/agents/pull/8419), [#4780](https://github.com/livekit/agents/issues/4780), PR [#8041](https://github.com/livekit/agents/pull/8041) (its `room_io/types.py` diff), PR [#7685](https://github.com/livekit/agents/pull/7685), PR [#7633](https://github.com/livekit/agents/pull/7633). Looked at and not related: [#2451](https://github.com/livekit/agents/issues/2451) (agent state when a chat text interrupts), [#4535](https://github.com/livekit/agents/issues/4535) (`lk.chat` callback missing after the first message), [#3217](https://github.com/livekit/agents/issues/3217), [#3923](https://github.com/livekit/agents/issues/3923), PR [#7427](https://github.com/livekit/agents/pull/7427).

### Status on main

Upstream `main` 0cfc030 (2026-11-17): `voice/room_io/types.py`, `voice/room_io/room_io.py`, `voice/room_io/_output.py` and `voice/speech_handle.py` are byte-for-byte identical to 1.8.11. `voice/agent_session.py` and `voice/agent_activity.py` differ, but not in the code cited in the entries (only line numbers shift: on main, `generate_reply`'s activity choice is at agent_session.py:1678, the `_scheduling_paused = True` of a new activity at agent_activity.py:365, and the `cannot schedule new speech` raise at 2078–2082). FINDING-10, -11 and -12's repros were also run against the local upstream clone, whose `voice/` files that matter here (`agent_activity.py`, `agent_session.py`, `room_io/`, `speech_handle.py`) are identical to main 0cfc030: all three reproduce.

## Not filed

- **Interrupted replies look final on the wire** (scenarios 2, 9): the closing header of a cut `lk.transcription` stream is the same as a complete one. A frontend cannot mark a cut reply. Feature gap; not searched.
- **The agent stays in the room after `close_on_disconnect` closed its session** (scenario 7): a user who comes back finds an agent that never answers. Possibly by design; needs a look at the job lifecycle before calling it anything.
- **A second text participant is ignored without a log line** (scenario 8): by design; a debug log would help.
- **Participant join/leave announcements ~3 s late on the dev server**: a server behavior, not investigated. It makes FINDING-14 easy to hit and coalesces quick leave/rejoin pairs (scenario 7).

## Most worth reporting upstream

1. **FINDING-11** (text during a handoff is dropped). Handoffs are a core feature. The window is the whole of the old agent's post-tool reply, which takes seconds with a real LLM, and the loss is silent. Voice drops such a turn too, but on purpose, next to a TODO that asks whether to forward it. Upstream already fixed the same error for async tool replies (#7685), so there is a precedent: wait for the switch to finish before scheduling the reply. For user input the wait must go through the session's activity lock, not the paused activity (see the entry's workaround).
2. **FINDING-10** (text during an uninterruptible speech is dropped). Deterministic, a short repro with no timing in it, and a small fix in the default callback: don't let `interrupt()`'s RuntimeError abort the reply. With session-wide interruptions off it hits every text sent during a reply. It sits next to the open #4443 family, which fixes the voice path only.

Both are in RoomIO's default text callback and share the drop in `_read_text`, and one change to the callback fixes both. They were filed together, with the user's approval, on 2026-11-17 as [#8606](https://github.com/livekit/agents/issues/8606). The fix is PR [#8607](https://github.com/livekit/agents/pull/8607), opened the same day at the user's request, before any maintainer had replied. The PR's tests cover both cases and an AgentTask switch: they fail on main and pass with the fix. The full unit gate passes with it. The draft and its offline repro are kept outside this repo with the other upstream drafts. FINDING-12, -13 and -14 are not filed. Any further upstream action needs the user's approval each time.
