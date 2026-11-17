# Clinic SMS scheduling agent: design document

Date: 2026-11-12
Status: confirmed (2026-11-12)

## 1. Purpose and motivation

The author is applying for the Software Engineer, Agents role at LiveKit (North America remote, Python/TS). The role focuses on three things: text/SMS channels, long-running stateful workflows, and handoffs between agents.

This project has two outputs:

1. **Interview demo**: a clinic SMS scheduling agent built on livekit-agents (Python) that can be demoed live and whose design can be explained clearly.
2. **Upstream contributions**: bugs and feature gaps found while actually using the SDK, turned into minimal repros and then filed as livekit/agents issues or PRs.

So this project **deliberately uses** the SDK's `AgentTask`, `TaskGroup` and agent handoffs instead of working around them. When an SDK problem comes up, first record it and build a minimal repro, then decide whether to work around it in the project or fix it upstream (see §5.10).

## 2. Goals / non-goals / anti-goals

### Goals

- **G1**: the six-step demo main line in §2.1 runs end to end.
- **G2**: every SMS cold-starts. The process can be killed and restarted between any two messages, and the conversation picks up from the database exactly where it left off.
- **G3**: a TaskGroup can resume from the middle, including the results of completed steps, the visited steps, and the ability to go back.
- **G4**: leave behind a record of SDK problems (`docs/findings.md`), each with a repro that runs on its own.

### 2.1 Demo main line (confirmed)

1. **The clinic texts first**: the system proactively sends the patient an appointment reminder, "You have an appointment with Dr. Lee on Thu 11/19 10:00 AM. Reply 1 to confirm, 2 to reschedule."
2. **Agent handoff**: the patient replies "2, can I move it to next week?", the reception agent hands the conversation off to the rescheduling agent, the rescheduling agent offers a few slots, and the patient picks one.
3. **TaskGroup**: once the time is changed, the conversation is handed to the intake agent, which asks in turn about insurance, the reason for the visit, and allergies and current medications. The patient answers two of them and then stops replying.
4. **Restart + time fast-forward**: actually restart the process once, then fast-forward the simulated time to the next morning. The agent proactively sends a follow-up; after the patient replies, it continues with the third item and does not ask the first two again.
5. **Going back**: partway through, the patient says "I entered the wrong insurance number", the TaskGroup goes back to the insurance step, and after the correction it returns to where it was.
6. **Escalation**: the patient asks a billing question the agent cannot handle, so the agent escalates it to a front-desk staff member. In the simulator the staff member is another "phone number" and replies only hours later; the agent then passes the answer on to the patient.

The whole demo takes about 3 minutes.

### Non-goals

- Real SMS / Twilio. The channel interface must leave room for it, but this version does not connect it.
- Voice.
- Integration with a real medical records system (EHR) or scheduling system.
- HIPAA compliance. All data is made up.
- A web UI.
- Multiple clinics / multi-tenancy.
- Identity verification and card payments. The official `examples/healthcare` already does these, so we do not redo them.

### Anti-goals (failures even if they technically work)

- Quietly covering up an SDK problem with a workaround without recording it. That defeats the purpose of "finding bugs".
- Spending most of the time on fancy simulator features instead of the agent itself.
- A demo that depends on luck: an unstable LLM makes the live demo fail. Replayable scripts and deterministic tests are needed as a safety net.
- A copy of the official healthcare example. The difference must show in "spread over several days, dozens of text messages, interrupted along the way".

## 3. Constraints and known facts

The following comes from reading the source of livekit-agents 1.8.11 (released 2026-11-05, currently the latest) and from a GitHub search on 2026-11-12.

- The `room` parameter of `AgentSession.start(agent)` can be omitted, so a text-only session that does not connect to a LiveKit server is feasible. `session.run(user_input=...)` returns a `RunResult`, which gives access to the events produced in that turn. The official tests use it this way: `AgentSession(llm=...)`, with no STT/TTS configured.
- `RunResult` has a suspension mechanism: while an AgentTask is waiting for user input, `run()` should still be able to finish (see PR #7794 and #6500 for related fixes). **This needs to be verified in the first prototype** (see §7 R2).
- An `AgentTask` can only be `await`ed inside an Agent's `on_enter` / `on_exit` or inside a tool function, and it is not reentrant (it can be awaited only once). What happens after the task finishes is written in the coroutine that has not finished running, and that cannot be serialized.
- `TaskGroup` (`livekit/agents/beta/workflows/task_group.py`, about 200 lines):
  - The progress (`task_stack`, `task_results`) lives in local variables inside `on_enter`; it cannot be read from outside and cannot be injected.
  - `_visited_tasks` is an instance attribute. The `out_of_scope` go-back tool lists only steps that have already been visited.
  - There is an `on_task_completed` callback, called once each time a step completes.
  - At the end it can use `session.llm` to compress the whole conversation into a summary (`summarize_chat_ctx=True`, on by default).
  - Upstream has an open PR #7556 ("Keep TaskGroup results when chat-context summarization fails") that also changes this file.
- `ChatContext.to_dict()` / `ChatContext.from_dict()` can serialize and restore a conversation.
- The session has the `conversation_item_added` event, `history`, `current_agent`, `update_agent()` and `generate_reply()`.
- The standard way to hand off between agents: a tool function returns a new `Agent` instance and passes `chat_ctx` along.
- The upstream repo has no SMS code at all.
- Upstream has no issue or PR about "resuming a TaskGroup from the middle / persistence".
- Open, unassigned issues in the same area: #5697 (TaskGroup that skips steps conditionally), #6577 (console text mode does not show messages the agent sends proactively), #5936 (cannot hand off inside an AsyncToolset). Before claiming one, check the comments to see whether someone is already working on it.
- **Do not touch**: #8307, #6291, #7015, #6399. Someone is already working on them.
- `FakeLLM` exists only in `tests/fake_llm.py` in the upstream repo; it is not part of any published package.
- **Current state of the Claude plugin (`livekit-plugins-anthropic`, the version that goes with 1.8.11)**:
  - Depends on `anthropic>=1.8,<2`; the default model is `claude-sonnet-4-6`.
  - The constructor has no `thinking` parameter and no `effort` parameter.
  - `max_tokens` defaults to 1024 when not set.
  - Thinking blocks in the stream are not stored in `ChatContext` and are not sent back to the API.
  - For a model that does not support a forced tool_choice, it automatically switches to `auto` and logs a warning.
- Related upstream issues and PRs, all already being worked on by someone, **do not touch**:
  - #7726: Claude 5-generation models think by default, and the 1024 limit can be used up by thinking, leaving no reply; Opus 5.5 / Sonnet 5.5 do answer, but about 5 seconds slower.
  - #6868: the plugin does not yet officially support Claude 4.7+ and 5-generation models.
  - PR #7755: adds a `thinking` option to the plugin; not merged yet.

## 4. Approaches

**The hard part** (shared by all approaches): the state of an AgentTask that is halfway through waiting cannot be saved, so on resume the call chain has to be rebuilt from the database. Concretely: the outer agent's `on_enter` reads "intake is at step 3", builds a TaskGroup carrying the results of the first two steps, and jumps straight to step 3, and step 3 must not ask its question again, because the SMS that just arrived is the answer to it.

- **A. Cold-start on every SMS (chosen)**: for each incoming SMS, create a new session, restore state from SQLite, `run` once, save it back, and close the session.
  - Pros: there is only one code path, and every message goes through the restore logic, so bugs surface quickly; tests are easy to write; it is close to a real deployment (a conversation may span several days, and no system keeps a session alive in memory for days); it is easy to explain in an interview.
  - Cons: every message has to handle "do not ask again"; this is not LiveKit's most common "one session per phone call" usage, so when a problem comes up we have to tell whether it is an SDK bug or uncommon usage on our side.
- **B. Long-lived session, restored from a snapshot on restart**: in normal operation it is the SDK's standard usage.
  - Cons: there are two code paths; the restore path runs only on restart, so bugs hide easily; the restart in the demo is staged on purpose.
- **C. Our own state machine, with one AgentTask per step (rejected)**: resuming is simplest, but it bypasses TaskGroup entirely, and TaskGroup is exactly the thing we want to use and find bugs in.

The author chose A.

## 5. Decisions

Items marked "(designer's decision)" were decided on the author's behalf while this document was written, and the author can change them; unmarked items have been confirmed by the author.

### 5.1 Language and dependencies

- Python, with dependencies managed by `uv` (designer's decision; LiveKit itself also uses uv).
- `livekit-agents` is pinned to `1.8.11`. The dependency must be switchable in one step to a local livekit/agents clone (a uv path / editable source), so upstream fixes can be verified directly in this project (designer's decision).
- **PyPI releases only** (author's decision): the demo runs on unmodified `livekit-agents==1.8.11` and its matching plugins. SDK problems are first worked around in the project, and the real fixes are proposed upstream separately (see §5.11).
- **The LLM is Anthropic Claude** (author's decision), connected through `livekit-plugins-anthropic`:
  - Default model `claude-opus-5-5` (designer's decision). To save money it can be switched to `claude-sonnet-5-5` or `claude-haiku-5-5`; that is the author's call. Note that per #7726, Haiku 5.5 often gives no reply with default settings.
  - Set `max_tokens` explicitly to a large value (for example 8000) instead of the plugin's default of 1024; otherwise the whole budget may be spent on thinking, with no reply (designer's decision).
  - Do not set temperature. Opus 5.5 does not accept sampling parameters.
  - A reply takes about 5 seconds, which is acceptable for SMS.
  - Credentials are read from the `ANTHROPIC_API_KEY` environment variable.
- No LiveKit Cloud account is needed: no room is joined, and LiveKit Inference is not used.

### 5.2 Overall structure

```
  Simulator CLI (patient phone / front-desk phone / clock / restart)
        │  inbound(from, text)          ▲ outbound(to, text)
        ▼                               │
  Channel interface ────────────────────┘     ← a Twilio adapter plugs in here later
        │
        ▼
  ConversationRunner (per message: lock → load state → build session → run → save → send)
        │                    │
        ▼                    ▼
  Agents / Tasks         SQLite (conversation state, appointments, job queue, message log)
        ▲
        │
  Scheduler (fires reminders, follow-ups and relayed staff replies as the clock advances)  ←  Clock (can fast-forward)
```

- **Channel interface**: inbound `inbound(from_phone, text, received_at)`; outbound `send(to_phone, text)`. The simulator implements this interface, and a future Twilio adapter will implement the same interface.
- **The front-desk staff member is just another phone number in the simulator**, using the same channel.

### 5.3 Per-message processing flow

A message from a patient:

1. Take a lock per phone number, so messages from the same number are processed serially. Several SMS sent in a row are not merged; each is processed on its own (designer's decision).
2. Load this number's conversation state from the database.
3. Create a new `AgentSession` (with only an LLM configured) and construct the currently active agent from the state; if the conversation is in the middle of a TaskGroup, construct it down to the innermost step and mark that step as "resuming".
4. `session.start(agent)`, and feed in the message only **after the innermost step has actually taken over the conversation** (see §7 R1).
5. Collect the assistant messages produced in this run through `conversation_item_added`; each one becomes one outbound SMS.
6. `session.run(user_input=text)`, and wait for it to finish.
7. Write the new state and the messages to send (the outbox) in **the same transaction**, then deliver the outbox (designer's decision).
8. `session.aclose()`.

On error: the transaction rolls back and the state is unchanged; send the patient a fallback message (for example "Sorry, something went wrong on our end — please reply again.") and write a log entry (designer's decision).

**Proactive agent messages** (reminders, follow-ups, relaying staff replies): the same flow, except that step 6 becomes `session.generate_reply(instructions=...)`, followed by waiting for that reply to complete.

### 5.4 Agents and handoffs

- **ReceptionAgent (reception)**: the entry agent. It handles replies to reminders ("1" to confirm, "2" to reschedule, or free text), answers common clinic questions (opening hours, address, from a static FAQ), and does the routing.
  - Tools: `confirm_appointment`; transfer to the rescheduling agent (handoff); `escalate_to_staff`.
- **RescheduleAgent (rescheduling)**:
  - Tools: `list_open_slots` (by date range), `book_slot`.
  - Once rescheduled, if the intake for this appointment is not finished yet, hand off to IntakeAgent; otherwise hand back to ReceptionAgent.
- **IntakeAgent (intake)**: its `on_enter` awaits a `ResumableTaskGroup` (§5.5) made of, in order, `InsuranceTask`, `VisitReasonTask` and `AllergiesMedsTask`. When all are done, it saves the intake results on this appointment, sends a short confirmation, and hands back to ReceptionAgent.
- **Escalation**: `escalate_to_staff(question)` is a tool shared by all agents, and it is also available inside every intake step (designer's decision).
  - Calling it creates a staff ticket and sends a message to the front-desk phone, for example `[#12] Jane D. (555) 010-0001: <question>`; at the same time it tells the patient "I've passed this to the front desk; they will text you back".
  - The staff member replies `#12 <answer>` in the simulator; the system has the patient's currently active agent pass the answer on to the patient as a proactive agent message, then closes the ticket.
  - A relay can also arrive while the patient is in the middle of intake; after the relay, the original step must continue.
- **How handoffs work**: the SDK's standard approach, where a tool function returns a new Agent instance and passes `chat_ctx` along. The conversation state records which agent is currently active.

### 5.5 TaskGroup resume (the core)

- Implement `ResumableTaskGroup` in the project. It behaves like the SDK's `TaskGroup` and adds three constructor parameters:
  - the results of completed steps (`id → result`);
  - the ids of visited steps;
  - whether the current step is "resuming".
- Because the progress lives in local variables of `on_enter`, the only option is to rewrite `on_enter` based on the 1.8.11 source. **Keep the differences from the stock version as small as possible**, and note them in the file header, so it is easy to turn into an upstream proposal later.
- **When to persist**: each time a step completes, save the progress in the `on_task_completed` callback; each time a step starts, also record the current step id.
- **Serializable results**: each step's result is defined as a pydantic model or a dataclass that can be converted to JSON and restored.
- **No re-asking on resume**: the current step's task is constructed in the "resuming" state, and its `on_enter` does not send the opening question. The conversation that already happened in this step must be restored into this task's `chat_ctx`, so the LLM can see what it asked earlier.
- **Going back still works after a restart**: on resume, restore the visited steps so that `out_of_scope` lists the correct targets to go back to.
- The summary at the end of the TaskGroup stays on by default (designer's decision). Note that #7556 is about results being lost when summarization fails.
- **Layered storage of the chat context**: the principle is to store enough information to rebuild each layer's `chat_ctx` (the outer agent, the TaskGroup's merged context, the current step's local context). The exact storage format is up to the implementer, but there must be a test proving that the context the LLM sees is equivalent before and after a restart (ids and timestamps may differ).

### 5.6 Time, reminders and follow-ups

- Every "what time is it now" in the business logic goes through a `Clock` abstraction, which the simulator can fast-forward. The clinic time zone is `America/Los_Angeles` (designer's decision).
- The `jobs` table records `due_at`, `kind`, `phone`, `payload` and `status`. Due jobs run when the clock advances (and when the process starts).
- **Appointment reminder**: sent 48 hours before the appointment starts.
- **Intake follow-up**: when the intake is not finished and the patient has not replied for 4 hours, send one follow-up (designer's decision).
  - Nothing is sent during quiet hours, 9:00 PM – 8:00 AM; it is pushed back to 8:00 AM.
  - At most 2 follow-ups per intake.
  - Any reply from the patient cancels all of their pending follow-ups.
  - For example, if the patient stops at 5 PM, 4 hours later is 9 PM, which falls in quiet hours, so it is pushed back to 8:00 AM the next day. This is exactly "the next morning" in step 4 of the demo.
- **Simulated clock start**: Tue 11/17/2026 9:55 AM. The appointment in the seed data is Thu 11/19/2026 10:00 AM, so the first 5-minute fast-forward triggers the reminder (designer's decision).

### 5.7 Data (SQLite)

Tables (the exact columns are up to the implementer):

- `patients`, `providers`, `slots`
- `appointments` (including the intake results as JSON)
- `conversations` (phone number, current agent, state JSON, version number)
- `jobs`
- `staff_tickets`
- `messages` (a log of every message sent and received, for display and the demo)
- `outbox`

Seed data (all fictional, designer's decision):

- Clinic: Maple Street Family Clinic.
- Doctors: Dr. Lee and Dr. Patel, each with a week of bookable slots.
- Patient: Jane Doe, (555) 010-0001, booked Thu 11/19 10:00 AM with Dr. Lee.
- Front-desk phone: (555) 010-0100.
- Phone numbers come from the 555-0100 to 555-0199 range that the US reserves for fictional use. Display format `(555) 010-0001`, date format `Thu 11/19, 10:00 AM`.

### 5.8 Simulator CLI

An interactive terminal. The prompt shows the current simulated time and the current identity. Commands (designer's decision):

- `/as <phone>`: switch identity (patient or front desk).
- `/advance <duration>` (e.g. `14h`, `30m`), `/time`: fast-forward time, show the current time.
- `/restart`: really restart the process with `os.execv`, to show that the state truly comes from the database.
- `/state <phone>`: print the persisted state of this number (current agent, TaskGroup progress, each step's result); used in the interview to show "what is stored".
- `/replay <file>`: replay a scenario from a script; used for both the demo and regression testing.

Each message shows its timestamp, character count and number of SMS segments. Reply length is constrained by the prompt (the target is 1 to 2 SMS segments); there is no automatic truncation.

### 5.9 Testing

- **Deterministic tests** use a scripted fake LLM that returns preset text and tool calls. Upstream's `tests/fake_llm.py` can serve as a reference; if it is copied directly, its Apache-2.0 copyright notice must be kept. Whether to write our own or copy it is up to the implementer.
- **How to compare context equivalence** (designer's decision): under approach A, "before and after a restart" in §5.5 means the context at the end of run N versus the context rebuilt at the start of run N+1. This is a regular test and must be maintained. In addition, during the first prototype phase, do a one-off comparison: run the same script once with a long-lived session plus the stock TaskGroup, and compare how the context the SDK generates itself differs from the one we rebuild. Record the differences in the findings; this comparison script is not maintained long term.
- **Record and replay** (author's decision: run the real model live, with a recorded version as backup): wrap the LLM in a layer with three modes.
  - **live**: call the real model directly.
  - **record**: call the real model and also save the response to each request, in order.
  - **replay**: no network; play back the recorded responses in order. If a request does not match what was recorded, fail with an explicit error; never continue silently.
  - Before the interview, record one good run of the full main line; during the interview use live by default, and switch to replay if something goes wrong. `/replay <file>` replays the patient's input; only together with replay mode is the run guaranteed to be identical.
- Must cover:
  1. After a restart between any two intake steps, completed results are still there and nothing is asked again;
  2. A restart in the middle of a step (question asked, not yet answered);
  3. Going back to the insurance step after a restart;
  4. The reception → rescheduling → intake handoff chain, with the state correctly persisted at every step;
  5. Follow-ups respect quiet hours and are canceled by a patient reply;
  6. The state is unchanged when an error happens partway through a run;
  7. When a relayed staff reply arrives in the middle of intake, the original step continues after the relay.
- **Behavior eval**: run the six-step main line with a real LLM and LiveKit's testing tools (`RunResult.expect`, judge). Run manually; not part of the fast test suite.

### 5.10 SDK problem record

`docs/findings.md`; each entry contains:

- Symptom;
- **Minimal repro** (a standalone script that does not depend on this project's code);
- SDK version;
- Root cause analysis;
- Classification: SDK bug / uncommon usage on our side / feature gap;
- Workaround: its location marked in the code with `# SDK-FINDING-<n>`;
- Upstream status: before filing, search existing issues and PRs to see whether someone is already working on it, and include the links.

### 5.11 What to do when the SDK has a problem, and the relationship with upstream

- The demo runs only on PyPI releases (§5.1). Fixes are first implemented in the project as workarounds (subclassing, with monkeypatching only as a last resort) and marked with `# SDK-FINDING-<n>`.
- If the prototype shows that approach A does not work on the released version and no workaround can be found: **stop and come back to the author**; do not fall back to approach B on your own (designer's decision).
- The implementation plan for this design **stops at `docs/findings.md` and the minimal repros**. Filing an issue or PR on livekit/agents is a public action taken as the author; each one needs the author's separate approval and is not part of this plan (designer's decision).
- #5697, #6577 and #5936 are only background, not tasks for this round. Turning `ResumableTaskGroup` into an upstream proposal comes after this project is done.

### 5.12 Timeline and priorities

There is no fixed deadline (author's decision) and no scope cuts. Work in the order below, and move on only once the earlier items are solid:

1. Prototype: verify R1, R2 and R6 from §7.
2. `ResumableTaskGroup`, persistence, and the resume tests listed in §5.9.
3. The three agents and the handoffs, getting main-line steps 1 to 5 working.
4. Reminders and follow-ups (§5.6).
5. Escalation (main-line step 6).
6. The full set of simulator commands, record and replay.
7. Behavior evals with the real model.

## 6. Left to the implementer

- Module layout, file names, the library used for the CLI.
- The exact format of the layered `chat_ctx` storage (the equivalence test in §5.5 must pass).
- How to implement the ready signal "the innermost step has taken over the conversation" (§7 R1).
- All prompt wording.
- How exactly to write the `on_enter` chain on cold start.
- The exact columns of each table.

## 7. Risks

- **R1 (highest)**: on cold start, the `on_enter` chain (Agent → TaskGroup → current step) is a series of async tasks. If the message is fed in before the innermost step takes over, the outer agent picks it up. **The first prototype must verify this.**
- **R2**: when `run()` finishes inside a nested AgentTask (suspension) may not match expectations. Several recent PRs (#6500, #7794) have been fixing this area.
- **R3**: after resume, the context the LLM sees differs from before the restart, causing behavior drift. The equivalence test in §5.5 exists for exactly this.
- **R4**: the summary at the end of the TaskGroup compresses away details, so later going back and Q&A may not have the original wording.
- **R5**: an unstable LLM makes the demo fail. Opus 5.5 does not allow setting temperature, so the only defenses are record and replay (§5.9) and pinning the model version.
- **R6**: the Claude plugin does not yet officially support 5-generation models (#6868).
  - Thinking is on by default, but thinking blocks do not go into `ChatContext` and are not sent back. Whether multi-turn conversations with tool calls break or change behavior on Opus 5.5 must be verified in the prototype.
  - effort cannot be set through the plugin's constructor parameters; if it is needed, the only way is to subclass the plugin to add the parameter to the request, and that counts as an SDK-FINDING.
  - If a problem we hit is already covered by #7726, #6868 or #7755, only record it; do not file it again.

The recommended first step of the implementation plan is a prototype: a two-step TaskGroup, cold-starting on every message, run against real Claude, specifically to verify R1, R2 and R6, before rolling out the rest.

## 8. Open questions

None.
