# Reliability repair tracker

## Current: v0.9.2, 2026-10-23

The user authorized a daily-overwritten proactive diagnostic file and GitHub
v0.9.2 publication. This version includes the prior local-only repair batch;
0.10.0 was a local candidate label and was never published.
`proactive_today.log` now records actual evaluation stages, blocking reasons,
probability/cache decisions, model planning and platform/local-commit outcomes.
Same-day reload appends; local-date rollover atomically replaces the same file.
An independent 60-second watcher also works when the agent loop is disabled.
No added model/draw/send is used for diagnosis. See PROACTIVE_LOG.md.

Local unpacked-package validation: **336 tests passed in 18.786s**, 195 Python
sources compiled, 132 effective schema defaults checked and 230 source files
verified. The 13 new log tests cover rollover/reload, concurrency, scoped
cleanup, unchanged probability/model calls, platform refusal, cancellation and
disk errors. No production reload or real-model/QQ test was performed.

Publication will be verified using the GitHub tag/Release and Actions evidence;
it does not imply production reload or real-model/QQ/Genie acceptance.
The records below retain the historical local-only validation state.

## Previous local-only delivery (historical)

Status: **local repair delivery complete on 2026-10-22**.
The prior 294-test batch and continued R10/R11 work remain uncommitted.
R10 data governance/retention and reactive/proactive recovery are implemented;
R11 extraction and CI/release gates are written. Current candidate is 0.10.0.
Final unpacked-package validation passed: 323 tests, 193 Python sources and
132 schema fields. The user explicitly deferred real testing on 2026-10-22:
“先暂时无需真实测试，模型调用无需上限”. Model/platform acceptance is deferred,
not passed, and is not required for this local delivery. No production
deployment or publication is implied.

Scope authorized on 2026-10-21: repair the preceding audit items in order,
excluding the relationship and character-performance item. Relationship rules,
persona, novel dialogue triggers, and novel knowledge content are out of scope.

Recovery point: clean Git tree at `417fc1b` (v0.9.1). Production configuration,
databases, services, and QQ accounts must not be used as test fixtures. Existing
data must migrate without being discarded. Model and platform acceptance require
their own evidence and cannot be inferred from local tests.

| ID | Required result | Evidence / status |
|---|---|---|
| R01 | Generic topics cannot make unrelated memory pass relevance gates. | Fixed; targeted regression passes, including fallback reranker. |
| R02 | Indexed Chinese retrieval covers old records across the complete scoped database; migration/update stay consistent. | Fixed; old exact match beyond 300 and scope isolation pass. Legacy backfill/update checks added. |
| R03 | Concurrent TTS cannot exchange references or delete another pending output. | Fixed; interleaved MockTransport renders keep their own references and both pending files. |
| R04 | Voice-only and interrupted replies persist only actual spoken/sent source text. | Fixed; actual 80-character TTS source is committed instead of 151 generated characters. |
| R05 | Whole-turn deadlines, stage latency, bounded media wait and text-first delivery; cancellation remains effective. | Implemented and included in final 294-test passing regression; stage timing, shared budget, text-first, timeout, cancellation and partial-generator-close tests. |
| R06 | Provider attempts reserve bounded fallback time without extending the shared total deadline; record redacted attempt timing. | Implemented and included in final passing regression; hung-primary/working-backup, shared total deadline, cancellation and redacted timings tested. |
| R07 | Long-term factual extraction, provenance, correction/versioning, duplicate handling and retention. | Conservative explicit self-statements, provenance/versioning, deduplication and late-arrival protection implemented. R10 now adds opt-in memory/journal/inactive-fact retention with pinned-record protection. |
| R08 | Read-only proactive diagnostics expose candidate/blocking reason and next evaluable conditions without rerolling or sending. | Implemented: self-scoped `/alice-status` plus programmatic inspection; 4 tests pass including repeated no-write/no-model/no-send inspection and overnight windows. |
| R09 | Per-task Jev modes/thresholds, explicit affect-intervention control, grouped metrics and Chinese evaluation tooling. | Implemented and locally verified: 5 new control/metrics/evaluation tests, CLI smoke and full regression. Real-model Chinese calibration remains unverified. |
| R10 | Scoped data inspect/correct/delete, retention and recoverable multi-store delivery writes; failure/restart tests. | Implemented and included in the final 323-test unpacked regression: self-scoped management, atomic deletion + recoverable cleanup, independent retention, snapshot/derived-context invalidation, stale-batch protection and bounded command output. Six reactive/three proactive failure positions plus abrupt subprocess exit tested without duplicate counts/history/cooldowns. See DATA_GOVERNANCE.md for durability/platform boundaries. |
| R11 | Extract message/delivery coordination from composition root; gate releases on tests/schema/boundary checks. | Reactive/delivery/send/config coordination moved to adapters; main reduced from 1588 to about 750 lines. CI tests unpacked artifacts on Windows/Linux using fixed AstrBot 4.28.1 source; release depends on checks. Local extraction regression passes. Remote CI execution remains unverified until pushed/run. |
| R12 | Update version/docs, reconcile evidence by implementation/local/model/platform status; full regression and package checks. | Local candidate 0.10.0 complete: current docs/legacy archives, upgrade/data/release notes, 323 tests from a fresh unpacked ZIP in 17.331s, 193 source compiles, 132 schema fields/effective defaults, archive manifest checks and parsed workflow gates. Real-model/QQ/Genie tests are explicitly deferred by the user; remote CI/publication/deployment have not occurred. These are not reported as passed. |

Execution notes and exact test evidence will be added as each item is verified.
Real paid-model and QQ acceptance remain separate from mocked provider/transport
and temporary-SQLite tests. No source publication or deployment is implied.

2026-10-21: All four new defect reproductions failed against the original tree.
After fixes, 12 memory tests and 18 media/delivery tests passed (overlapping
regression modules). Full-suite results are recorded separately below.

Stage 1 full suite: **274 tests passed in 13.020s**. The first full run exposed
an unclosed SQLite connection in the new migration test fixture; it was closed
explicitly and the complete suite rerun. No production database was involved.

Stage 2 targeted: **29 tests passed**, then **5 new budget tests passed**.
Defaults now bound a reactive generation to 60 seconds, retain the existing
30-second per-LLM-chain total, reserve time inside that total for fallback, and
bound media rendering to 12 seconds. Text-first delivery is on when both text
and audio are requested; voice-only retains its explicit mode. Released audio
files have a five-minute upload grace period; fresh files are not deleted by
another renderer's startup (stale unleased files expire after 24 hours).

Stage 2 full run found an old adapter test expecting the former default
combined audio/text ordering. It now verifies the intended text-first default;
the segment suite explicitly tests the compatibility setting. The targeted
fact/memory/adapter rerun passed **24 tests**. Full regression is still pending
after the fact changes.

R07 facts are limited to unambiguous current-user assertions (name, residence,
occupation, favorite color, preferred language, per-object allergy/preference).
Questions, quotes, third-party statements, uncertainty, history, future plans
and roleplay abstain. No added model call or relationship/persona changes.
Existing raw memories are retained as historical observations; they are not
silently reinterpreted as reviewed profile facts during migration.

Stage 3 full suite: **284 tests passed in 13.334s**. Jev per-task views and
policies, explicit affect-intervention control, and date/model/mode/task metrics
with bounded histogram p95 estimates are implemented. Existing 38 decision,
affect and runtime-hardening tests pass; targeted new tests/evaluation tooling
and full regression are still pending for R09.

Important default change: production `jev_affect_intervention_enabled=false`
keeps shadow numeric observation separate from response emotion. Set true to
restore the previous requested Jev/local-VAD mixture. Relationship reducers,
persona, novel knowledge, and novel retrieval triggers have not been changed.

## Final evidence for the paused batch

- Full suite: **294 tests passed in 13.629s**, using AstrBot's bundled Python and
  `PYTHONPATH=E:\AstrBot\backend\app`; all 25 new tests included.
- Compiled **178 Python files** in memory without creating bytecode artifacts.
- Parsed configuration schema and checked effective new defaults through the
  actual composition helpers.
- Executed the evaluation CLI on temporary files: default selection skips all
  10 unreviewed proposed labels and makes no model-quality claim.
- Legacy v1 backfill test verifies a readable pre-v3 SQLite backup and preservation
  of original content/journal; source edits, fact rollback/restart, scope isolation,
  media interleaving and generator closure are covered.
- `git diff --check` passes. Git confirms no changes under relationship, persona,
  novel knowledge or prompts. Current source recovery point remains `417fc1b`.
- No production reload, instance configuration edit, paid-model/network acceptance,
  QQ delivery, Git commit/push, or release publication was performed.

The evidence above records the previous paused batch. The continuation below
supersedes its former R10/R11 pending status.

## 2026-10-22 continuation evidence

- Local candidate version: **0.10.0**. Framework minimum declaration now matches
  the tested 4.28.1 baseline. No installation or production configuration changed.
- Final complete regression from a fresh unpacked source ZIP: **323 tests passed
  in 17.331s**. Compared with the prior paused batch, 29 additional tests cover
  governance, recovery, packaging and outer generator closure.
- Verifier compiles **193 Python sources**, checks **132 schema fields** and the
  actual composition defaults, checks Core dependency boundaries and validates
  the ZIP file set and each content hash. Static novel data is included; runtime
  databases/configuration/media/caches are excluded by package selection rules.
- Recovery includes six reactive failure positions, three proactive failure
  positions, transient metadata failure, cancellation and a real child process
  terminating with `os._exit(73)` before history/receipt completion, without
  normal connection closure. Reopening committed WAL data does not duplicate
  state/relationship/access/cooldown/history effects. This is not a power-loss
  or platform-acknowledgement test.
- Parsed the Windows/Linux reusable-check workflow and release `needs` gate;
  local package checks pass. Remote Actions have not run for this worktree.
- Git confirms no change to relationship/State reducers, persona, novel data or
  prompts. History prompt projection only omits the new internal delivery ID.
- Final documentation-only evidence updates are followed by rebuilding and
  checking the ZIP/manifest, without repeating already-passing code tests.
- No commit/push, Release creation, production reload, paid-model request or
  real QQ send was performed. Current source baseline is still `417fc1b`.

## User-directed delivery boundary and completion audit

2026-10-22: The user explicitly said “先暂时无需真实测试，模型调用无需上限”.
No real model/QQ test is to be run now. The future model request count has no
user-specified upper limit; this does not trigger any call in the current run.

All currently requested local repair deliverables have direct evidence:
R01/R02 indexed retrieval and scope; R03/R04 concurrent media and actual-history
prefix; R05/R06 shared budgets and fallback; R07 provenance/versioning/retention;
R08 read-only self diagnostics; R09 task controls/metrics/offline evaluation;
R10 scoped management/retention/reactive and proactive recovery; R11 extracted
coordination and checked release-gate definitions; R12 version/docs/package and
full unpacked regression. These are covered by the passing 323-test suite and
the independent source/default/archive checks described above.

Real-model quality/Chinese calibration, real QQ/Genie outcomes and cross-day
acceptance are explicitly deferred, not proven. `ACCEPTANCE.md` preserves the
steps for a later instruction. Remote CI/publication/deployment also remain
unexecuted and are not claimed by this local delivery. No relationship or
character-performance strategy was changed. The current local repair goal can
close on this user-approved delivery boundary without claiming deferred tests
succeeded.
