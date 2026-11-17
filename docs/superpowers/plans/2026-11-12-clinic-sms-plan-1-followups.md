# Plan 1 deferred minor issues

Source: the Plan 1 execution ledger (SDD workspace, since deleted). The items below are copied verbatim from the ledger: Minor issues recorded during per-task review, issues deferred after the final fix round, and one out-of-scope note.

Some of them were already fixed in the final fix round (283b444..ccb0185), for example the Task 8 Store.load order and the Task 9 /start-intake printing nothing. The rest must each be checked before writing Plan 2, or before preparing the findings for upstream submission. Line numbers refer to the code as it was when each item was written. Commit hashes refer to the private development history, which was squashed into a single commit when the repo was published.

## Minor items deferred during per-task review

- Task 1: minor (parked): pytest-asyncio asyncio_default_fixture_loop_scope warning; thin coverage of format_when noon/midnight & naive dt; lenient normalize_phone; requires-python lacks <3.15 bound.
- Task 2: minor (deferred): no committed tests for Reply.error/delay/default/call id uniqueness/contexts (only in uncommitted check_llm.py)
- Task 2: minor (deferred): test_unscripted_input_raises lacks match="unscripted input"
- Task 2: minor (deferred): findings S4 counts S2(via=step) zero as evidence though that ctx was discarded
- Task 2: minor (deferred): readiness premise silent on on_enter awaiting after set()
- Task 2: minor (deferred): readiness conclusion cites only private-field peeks, not the code evidence (agent_session.py:1989→~2014, agent_activity.py:1096)
- Task 2: minor (deferred): findings.md:53 merges ctx-overwrite into FINDING-1 by default
- Task 3: minor (deferred): claude_roundtrip.py:201 whitespace-only reply counts as outbound (use .strip())
- Task 3: minor (deferred): claude_roundtrip.py:358-360 except BaseException swallows KeyboardInterrupt/SystemExit
- Task 3: minor (deferred): claude_roundtrip.py:203-205 three blank lines (ruff E303); ruff never run
- Task 3: minor (deferred): claude_roundtrip.py:206 `result: object` should be RunResult
- Task 3: minor (deferred): findings.md:53-54 unverifiable claims (two handlers / log probe) stated as fact
- Task 3: minor (deferred): claude_roundtrip.py final PASS message line is long (line-length lint)
- Task 4: minor (deferred): stale tests never exercise a rollback after the state write (failure inside _log after UPDATE) — Task 8 relies on all-or-nothing; point Task 8 / final review here
- Task 4: minor (deferred): tests/test_store.py:282 reads s._conn (could use a second sqlite3 connection)
- Task 4: minor (deferred): tests/test_store.py:333 GroupProgress import inside the test
- Task 4: minor (deferred): store.py:109 default busy timeout/no WAL/check_same_thread — revisit only if Task 8 shares across threads
- Task 5: minor (deferred): after a step completes, pending still lists it until the next step starts (src:237 vs :202); if the group fails after the last step (e.g. summarize error → complete(e)), a snapshot would persist pending=[last] and the next cold start rebuilds an answered step with factory(True) — Task 7/8 + final review must look at this
- Task 5: minor (deferred): validate_progress accepts pending == [] → resumed group completes with no on_enter → ready timeout instead of a clear error (becomes reachable if the item above is fixed by recomputing pending after completion)
- Task 5: minor (deferred): no test that a non-first step does NOT get step_chat_ctx; no test for an unknown id only in visited; "a never constructed" checked via on_enter log only
- Task 5: minor (deferred): src:237 assumes res is result_type instance (wrong model stored, fails only at next validate_progress)
- Task 5: minor (deferred): misuse silently accepted — step_chat_ctx without progress ignored; inherited add() not disabled (KeyError on resume)
- Task 5: minor (deferred): FINDING-1 no-script rationale weak (symptom could be scripted as in Spike 2 T3)
- Task 5: minor (deferred): tests' `async with AgentSession` aclose() has no timeout
- Task 5: minor (deferred): ReadySignal is one-shot — Task 8 must create one per session (say so in docstring)
- Task 5: minor (deferred): findings.md FINDING-3 root cause 5 cites AgentHandoffEvent at run_result.py 85–90; actual 86–91 (off by one)
- Task 6: minor (deferred): close test's "no ERROR" check sits behind a fixed 0.1s sleep (test_intake_agent.py:75-76) — could pass vacuously; wait on a deterministic signal
- Task 6: minor (deferred): no test that a ToolError while the session is open still propagates (intake.py:419)
- Task 6: minor (deferred): stale comment test_intake_agent.py:539-542; resume test could assert no assistant message before first input
- Task 6: minor (deferred): FINDING-4 cites _teardown_activity at 1402; actual 1403
- Task 6: minor (deferred): repro/finding_4.py shows AgentTask.cancel() ToolError via plain Agent, not the group re-raise (root cause 3); case 2 "real failure" is synthetic — say so in docstring
- Task 7: minor (deferred → Task 8 tests): multi-cold-start (≥2 restores) and non-ASCII ctx round trip only covered by the one-off spike
- Task 7: minor (deferred): normalize_ctx crashes on non-JSON function_call arguments (ctx.py:54)
- Task 7: minor (deferred → Task 8 contract): snapshot() RuntimeError branch untested (current_task None, e.g. group ended by close before activity) — runner treats it as a failure: state untouched
- Task 7: minor (deferred): kept timestamps assume monotonic wall clock across processes (created_at insertion order) — note in docstring/deploy notes
- Task 7: minor (deferred): spikes/compare_ctx_with_stock.py:101 uses _is_closing() without # SDK-FINDING-4 marker
- Task 7: minor (deferred): FINDING-5 upstream search incomplete (6 queries hit 403) — re-search before findings are final
- Task 7: minor (deferred): _group_ctx not cleared after apply (intake.py:214-216)
- Task 8: minor (deferred): chained test checks allergies-call ctx, not the final COMPLETION ctx (test_runner.py:276) — passes today per reviewer probe
- Task 8: minor (deferred): C8-1 for mid-chain errors outside TaskGroup's try relies on SDK _handle_task_done side effect; suggest explicit _failed flag; intake.py:224 update_chat_ctx outside try
- Task 8: minor (deferred, plan-mandated order): Store.load parse errors (runner.py:81, :65) happen before the try → no fallback, ValidationError may carry stored PHI; log_only/_deliver can raise
- Task 8: minor (deferred): no runner tests for ready timeout, run timeout, StaleStateError, summarize_intake=True success path
- Task 8: minor (deferred): unknown-step speed bound equals ready_timeout (2s) — tighten to 0.5s
- Task 8: minor (deferred): FINDING-6 repro matches any "Task exception was never retrieved"; root cause omits agent.py:1117-1119
- Task 8: minor (deferred): _locks grows unbounded; deliver_pending concurrent with handle_inbound can double-send (startup-only per docstring)
- Task 8: minor (deferred): most test awaits rely on runner-internal timeouts (~74s/130s); no pytest-timeout
- Task 8: minor (deferred): no test for a close error when the turn fails before commit
- Task 8: minor (deferred): exceptions raised inside session.run (tool args / SDK internals) could carry patient text into logs — outside the stored-state path
- Task 9: minor (deferred): /start-intake prints nothing when start_intake returns [] (already started or failed)
- Task 9: minor (deferred): parse_command strips the line (Command("say", stripped))
- Task 9: minor (deferred): no test for /restart execv argv/close order or bad /as
- Task 9: minor (deferred): deliver_pending at startup not wrapped (store error aborts CLI)
- Task 9: minor (deferred): KeyboardInterrupt test only stubs input; does not assert Store closed nor cover blocked input (pty check is the evidence)
- Ruling (C8-3): Task 9 CLI must not emit livekit DEBUG records (lk.pii extras) to any sink and must not log raw CLI input from ValueErrors; deliver_pending runs only at startup before reading input; failed send after commit stays undelivered in outbox (retried next startup) — deferred to final review / Plan 2 — cost if wrong: duplicate or late SMS in the simulator

## Deferred after the final fix round (wording issues, to be fixed together when the findings are prepared for upstream)

- parked — M1 findings.md:381/:453 + intake.py:54-57 say "a returning tool requires a reply" without the public ToolResult(reply_required=False) exception — Ruling: real, wording-only, fix when findings are prepared for upstream
- parked — M2 findings.md:143 "asyncio logs again on GC" holds only for the root agent (FINDING-6 scope) — Ruling: real, wording-only, same as M1
- parked — M3 README says repros exit 1/0 but finding_1/2/3/7 may exit 2 (INCONCLUSIVE); finding_1's exit code would not flip to 0 if upstream added a resume entry — Ruling: real, docs-only, fix with M1/M2
- out-of-scope note — upstream task_group.py:190 enum is list(set) → order nondeterministic across processes; schema validity unaffected
