<!-- prd-version: 4 -->
# ohmyboring PRD

The product definition for this repository. `GOALS.md` is the "current-slice execution contract" derived from it.

> For the 39 commits before this document, defect discovery dragged the documentation along. This document exists to reverse that direction.
> Draft → six-seat panel review → two independent advisor rounds; the record of that process is in
> `docs/reports/2026-08-25-panel-review.md` — read the reasoning there, and read
> **only the decisions** here.

## 0. BLUF

- The **primary user = the agent** conclusion stands, but **its grounds are replaced**: not supply (1,472 injections/week) but **uptake** (the #224 ledger: injected fingerprints reappearing in assistant turns). The verdict contract is pre-registered in this document (§2): **if, over a two-week window, per-prompt uptake does not significantly exceed the control (hits that were not injected), the injection channel in its current form is declared not working and the R6 parameters are reopened.** Measured against a floor, not an absolute threshold.
- **Throttle decision** (product decision, §5-R6): the session-throttle concept is dropped. `agents/claude-code/README.md:10` "(throttled to once per session)" is false and is deleted; injection frequency is frozen for the two-week uptake window — changing the channel mid-measurement kills the sample.
- **Metric hierarchy corrected**: M8 uptake (new) is the first-class outcome metric, M1 precision is demoted to diagnostic, and M2 is renamed from "the measurement of R1" to a **regression floor (smoke)** (22/22 on an 18-document fixture corpus).
- **R3 stays unresolved**: the defect where a session's biggest decision drops out of distillation (the #218 loss) cannot be answered by renaming a metric. The derived work and its honest cost (1–2 weeks) are recorded in §5-R3, and the start is pinned to after the measurement window — changing distillation changes the notes that get injected.
- **Adoption decision (§7)**: **commit to `docs/PRD.md` in this slice** — so that it can be the pre-registration site for the §2 contract. Anti-drift is designed in its minimal form (§6).
- **Next slice = "demand verdict for the injection channel"** (§7.2): zero build, two weeks of measurement, falsification conditions stated.

---

## 1. What this product does

At the moment the owner types a prompt into a coding agent, the `UserPromptSubmit` hook looks in the vault for how the same problem was solved before and injects up to three snippets (280 chars, with an injection fence — `agents/shared/recall_core.py:193–198`) into the agent's context **on every prompt** (the "1h per-session throttle" v0.1 described was a false fact — it never existed on the Claude Code path, §5-R6). When the session ends, the `SessionEnd` hook distills the transcript with a local LLM into a markdown note (verifier required, one repair, dead-letter), and **now, at the same moment, also records whether what was injected into that session was actually used** (#224, `agents/shared/distill_core.py:791–819`). The vault is the source of truth and the DB is a derived index rebuilt by `sync`. Everything is local (zero cloud), with no user intervention. The product succeeds at the moment "the agent does not re-solve from scratch a problem the owner already solved" — and that success is now **measured** by the uptake ledger.

## 2. Users and the verdict contract

**Panel ruling — agreed (amendment accepted).** v0.1 used "1,472 injections/week" as the grounds for agent-first. The AI PM's objection is right: 1,472 is **supply** created by a hook nobody opted into, and by that logic ad impressions have users too. Evidence of demand is the moment the consumer pays — an active call (16/week), or **actually using what was injected**.

**Replaced grounds**: the agent-first conclusion now stands on two legs.

1. (Elimination) Direct human consumption (brief, 7/week) is not a volume that can drive any product decision — the AI PM supports the conclusion itself on this ground.
2. (Awaiting verification) The #224 uptake ledger — at SessionEnd it counts whether the fingerprints of injected notes (fingerprints only, never the snippet text — `agents/shared/uptake_core.py:88–96`) reappeared **in assistant turns only** (phrases the user already said are excluded from evidence — `uptake_core.py:166–176`). Two rates come out: per-hit (share of what was pushed that got used) and per-prompt (share of turns where the injection meant something) (`uptake_core.py:180–208`).

**Verdict contract (pre-registered — judged on future data, not on this commit):**

What this contract measures is **treatment against control**, not an absolute rate. An uptake number on its own cannot tell "the memory was used" from "any note on this topic would have shared words with the answer", and skipping that distinction is the mistake that produced `0.514`. So hits that were **not** injected are scored the same way to produce the chance rate (`agents/shared/recall_core.py` `CONTROL_RESULTS`).

The control is free: `drudge/src/retrieve.rs:93` has `pool = (max_results*4).max(20)`, so 3 and 5 draw from the same candidate pool. Live measurement confirmed the top-3 order, distances and **the 280 bytes that get injected are identical** — the channel is invariant by structure, not by promise.

| Item | Value |
|---|---|
| Window | Two weeks. The dates are registered in code — `verdict_core.WINDOW_SINCE`, `verdict_core.WINDOW_UNTIL`, `verdict_core.MIDPOINT` — with the commit history as the timestamp of registration. A reset is a dated decision commit that moves those constants before any row exists under the new instrument (§8 D1, D9); metric, floors and thresholds never move with them |
| Metric | per-prompt uptake (treatment) vs per-prompt control |
| **Sample floor** | sessions ≥20 **AND** injected prompts ≥200. Below it, the **verdict is refused** — no number is produced (same shape as the `MIN_DECIDED` precedent in `agents/shared/label_core.py`) |
| **Works** | treatment ≥ **2**× control AND gap ≥ **3**pp |
| **Not working** | treatment ≤ control + 1pp → the injection channel in its current form is declared not working; R6 parameters (form, timing, on-demand) reopened |
| **Withheld** | anything in between → the window is extended once, no forced action |
| Instrumentation-fault clause | sessions end but zero `injection_uptake` events within 48h → not a verdict but an **instrumentation investigation** |
| Instrument self-check | the same transcript is also scored against **another session's** ledger hits. It must be ≈0; if it is at or above the treatment, the instrument is broken |

**Sample coverage is reported beside the verdict.** The share of sessions scored out of sessions distilled inside the window goes next to the verdict. When that share is low, "treatment x%" is a statement about what we **saw**, not what we **sent**, and those are different sentences. **Below 1/2 coverage it is an instrumentation investigation, not a verdict** — the rate survives but what it is a rate *of* does not (§8 D4 is why this clause exists).

**Detector sensitivity is proven before the verdict.** Treatment and control **both** at zero can be evidence that "this detector sees nothing" before it is evidence that "nobody used it". So before the verdict date, a **fixed synthetic transcript** that deliberately places ledger fingerprints in assistant turns is run through `session_uptake` and `used_prompts ≥ 1` is asserted. A "not working" reached while that assertion fails is an instrumentation investigation, not a verdict. The trigger is a fixed input rather than a live number, so it cannot be tuned after the fact, and it is symmetric across both arms, so neither conclusion is favoured.

**Tests cannot write to the verdict ledger.** The verdict aggregation reads only `injection_uptake` events, and before the verdict the **absence** of test-origin session ids (`codex-abc` and the like) in the event store is asserted. Measured 2026-09-02: the verdict series was clean at 0/104 contaminated, but the adjacent series (`distill_resolution`) was 1,014/9,396 (10.8%) test artifacts and growing daily — **the current cleanliness is luck, not structure.**

**Rates are quoted only with their population.** Every measured rate, percentage or "0 cases" assertion in this document or a PR body carries, in the same paragraph, **the source of the denominator** (a reproduction command or a ledger path). A measurement sentence without it counts as unregistered. And **if the numerator and denominator come from different event populations, the comparison is void — including reading a missing field as 0.**

This rule came from a measured incident (2026-09-02). Of 106 `injection_uptake` events, **only 13** carried the `used_control_prompts` field (events before #242 do not have it); `SUM` ignored the NULLs and manufactured a **comparison that does not hold**: "treatment 11/770 vs control 0/770". The numerator 11 came from the 93 events without the field, and the control numerator exists only in the 13. In the only comparable population it is **treatment 0/130 · control 0/130**, both arms at zero, and that interval carries no information. It was the sixth time in one window that a sample was quoted as a population, and none of the eight CI jobs caught it — gates check code and **nobody checks claims.**

**Negative assertions are recorded only with a sensitivity proof.** "0 cases" and "none" stay true when the subject disappears, so without a proof that the detector catches one planted positive (`doctor (d5d)`) they cannot be cited in a verdict.

**The window's dates are the owner's calendar dates — `Asia/Seoul (UTC+09:00)`.** Every date in this section is. Measured in UTC, the 08:00 KST morning briefing reads as the previous day and the gate fires a day late (measured). A **fixed offset** rather than the machine's local zone, because the same code runs inside a container — a date that moves with the `TZ` setting is not a registered date.

**Window midpoint check.** At the window's midpoint (`verdict_core.MIDPOINT`), if **scored sessions per adapter are under 10**, switch to an **instrumentation investigation** without waiting for the close — a progress gauge for the fact that §2's "zero within 48h" clause looks at a single moment.

**Why per adapter**: because the floor of 20 applies per adapter (each adapter runs a different product — §3 M8). Summed, 8 Claude Code sessions plus 3 from another adapter would **pass a gate neither of them passes**. Closed before the midpoint.

**The midpoint is one-time** — it does not apply to an extended window. An extension changes none of the metric, floors or thresholds, and a daily progress alarm turns the red light into a background that trains itself to be ignored.

If the floor is not met at the close, a refused verdict is recorded and the window is **extended exactly once, by two weeks** (metric, floors, thresholds unchanged). If it is still short after the extension, **the floor is not lowered.** Instead, "the injection channel could not produce a judgeable sample in four weeks" is adopted as a product signal on par with "not working", and the R6 parameters are reopened. Because the exit is fixed as "the shortfall itself is the verdict" rather than "lower the floor", there is no freedom to adjust after seeing partial numbers at the close.

**2×, 3pp and 1pp were chosen without priors.** But they are **superiority margins above a measured floor**, not absolute thresholds, and there is a withheld band with no forced action when neither side is met — that is the difference from 0.514. The contract binds **behaviour**, not truth.

*Honesty footnote*: fingerprint reuse is **mostly an undercount** — an injection can change behaviour without lexical reuse (avoiding a path, confirming a decision). The remaining overcount paths are enumerated and small: an assistant mentioning a note **in order to dismiss it** counts as use. So a "works" verdict is strong evidence, and a "not working" verdict means **lexical reuse did not beat the control**, not a rejection of memory injection as a concept.

The user/JTBD table stays as in v0.1 §2, with the frequency column reinterpreted as "measured supply".

## 3. Success metrics

| ID | Metric | Source | Current value (measured 2026-08-25) | Target / verdict |
|---|---|---|---|---|
| **M8 (new · first-class)** | injection uptake per prompt | `event_log` `recall-uptake`/`injection_uptake` (#224) | **0 events** (live `/events` — waiting for the first session end) | Not a target — **verdict against control** (§2 contract) |
| M1 (diagnostic) | injection precision | `recall_label` (#221, daily auto-collection #223) | LLM judge relevant 6 / irrelevant 18 (live `/recall-label-stats`) — **n=24 < 30, so the reporter refuses to report** (`agents/shared/label_core.py:26,167`) · human cross-check agreed 0 / compared 0 | Unset — only after human `--audit` n≥30. **An LLM judge alone can never set the target** |

**The human judge's role is instrument calibration, not ground truth.** The reason is **not** "the judge shares the embedding lineage" — measured (2026-09-02), the embedder is `bge-m3` and the judge is `gemma4:12b`, different lineages. That sentence was wrong, was copied into three code comments, and this commit fixes them together. The real reasons are two: ① **nobody has ever validated this judge, and a judge cannot establish its own accuracy** ② what M1 measures is the counterfactual "would it have helped if a **person** read it", so a person is the standard **by definition**, not by preference.

**M1 labels cannot be assigned by the in-session agent in any form.** Uptake (M8) already comes from the agent's transcript; if M1 becomes the same party's judgement too, **the distinction between "relevant but unused" (a channel defect) and "irrelevant" (a recall defect) disappears** — separating those two is the only reason M1 exists apart from M8. Using another vendor's LLM as a third judge is not forbidden as such, but it is a separate series extending `judge` and **does not count toward `compared`**: filling `compared` would require recording that label as `human`, which is provenance forgery. And the control needed to confirm a third judge's reliability itself requires ~20 human labels, exactly the cost it was meant to replace — a circle.

**The one thing a sample of 20 decides**: it does not flip the threshold (the enforce path was already deleted in #218). The only thing it opens is **whether the nightly-accumulating gemma instrument is promoted to a reportable metric**, and since recall distance mass sits in the 0.40–0.56 ambiguous band, distance can never decide it, so labels are the only path.
| M2 (renamed) | golden **regression floor** (formerly "the measurement of R1") | eval gate | 22/22, MRR 1.000 — on an **18-document fixture corpus** (`data/eval/fixtures/`, 18 files), random baseline ~17% (inferred: 18 documents, top-3) | keep 22/22 (regression guard on an achieved value) |
| M3 | golden false_pass | eval gate | 4/6 | Unset — same reintroduction bar (two-sided predicate, separate commit, `run_eval.py:184–194`) |
| M4 (renamed) | **pipeline survival** (formerly "lossless ingest") | `event_log`/readiness | pass | dead-letter 0 · readiness 100% |
| M5 (method replaced) | claim currency — **sample audit** | manual: random n≥20 current claims vs current code (continuous instrumentation depends on R2) | at least 2 false in the 08-20 probe sample | Unset |
| M6 (scope widened) | deploy match — engine **+ host binary** | `/health.build_sha` + readiness (failure-conditioned by #222) | engine **matches** (`d6fad0e`==HEAD) · host CLI **unversioned — instrumentation dependency** (§5-R4) | match |
| M7 | active MCP use (recall+ask / week) | `query_log` | 16/week | observation only — input to the "moment of demand" surface design |

**Definition of "session"**: in M8 a session means only a unit that `log_uptake_event` recorded at `SessionEnd`. `distill_resolution` fires on every distillation run (including mid-session compactions), so it is not a session count. Any surface that reports that row count as a session count is itself an instrumentation defect.

**M2 citation rule**: M2 is a **wiring** regression floor on an 18-document fixture. It cannot be cited as evidence of production recall quality — 18 documents have no near competitors, and production's failure mode is exactly near competitors. Only M8/M1 can make quality claims.

Corpus update: 1,381 documents (live `/health`). code_index: **rust 836 · python 11,678 · shell 61 symbols** (live `code_index_status`, #226).

## 4. Non-goals

v0.1's exclusions stand: team sharing, company KB, cloud, code-search productisation, ungated writes. **One amendment**:

- ~~"No separate UI (Obsidian is the UI)"~~ → **"An interactive/graphical UI *product* is a non-goal. But injection observability — 'what was injected into my last N prompts, and why' — is a derived requirement of R1, and that surface includes a read-only local view."**

  **Reason for the amendment (2026-09-02, owner decision).** v2 rejected the form assumption "a screen" and ruled that extending the CLI was the cheapest path. That ruling was **wrong, and measurement showed it**: within a week of the screen (`scripts/peek.py`) existing, three things came out that the CLI had not produced in months — ① sample coverage 11.8% (§8 D4, the finding that changed the verdict) ② the recall distance band distribution ③ separating confirmed scoring from pending. All the data was already there; nobody had queried it. **"The data already exists" does not mean it gets read** — the same shape of illusion this repo learned from six delivery defects.

  The boundary is drawn by **property**, not by form: the observation surface is read-only (no writes, no calls to generating endpoints), binds to loopback only, never sends prose from company-origin notes out of the server, and does not recompute verdict numbers itself (it reads them from `verdict_core`). A screen that does not satisfy those properties is still a non-goal. The moment sharing, distribution, multiple users or authentication attach, it is a different product.

## 5. Requirements

### R1. Injection must be precise — *metric hierarchy corrected*
The first-class measurement is M8 (uptake), the diagnostic is M1 (precision). Derived work: ① complete the two-week uptake window (§7.2) ② human `--audit` n≥30 ③ **README truth correction** — agreed with the non-engineering reviewer that `README.md:10` "recalls the useful parts" is incompatible with the measured labels (LLM judge 6/24 relevant). Replace the wording with a measured-in-progress statement ("captures how you solved things; recall precision is being measured in the open") and restore quality adjectives only after M8/M1 are reported.

### R2. Recalled memory must be current — *scope decision added*
- **Measured certainty vocabulary**: the panel's "everything is certain" is an exaggeration — measured 4,395 certain / 1,150 likely / 24 empty string / 2 assumption (grep across the vault). But the substance holds: **once certain, certain forever** — there is no demotion path, so stale claims come back as `certain` (08-20 probe D1). Derived: confidence is a state that has to be **maintained**, not granted. (+ the 24 empty strings are a schema leak, R7's domain)
- **Accepting non-retroactivity**: the existing 7,243 claims cannot be retroactively anchored because the original raws are gone (agreed — engineer). **R2 is forward-only.** Legacy handling (decay label vs excluding `certain`) is an owner decision (§8 Q5).
- M5 is not circular: the sample audit is possible now without instrumentation, and continuous instrumentation comes after.

### R3. Solved experience does not silently vanish
The gate stays (verifier → one repair → dead-letter), but **the key decision of a session that passed must not drop out of distillation.**

A real violation: a four-PR session was compressed to a fixed four claims and the day's biggest decision (#218, deleting the threshold enforce path) is **in no note at all** (`docs/reports/2026-08-20-rag-usage-probe.md` §4 D2). And this happened while M4 was green — which is why M4 was renamed "pipeline survival". **The rename is an honesty correction of the metric, not a resolution of this requirement.**

**Derived work (scheduled)**: remove the structural fixed claim count per note, or split distillation per decision. Honest cost **1–2 weeks** — the prompt/verifier change itself is 2–4 days but it cuts across dedup, claim upsert, eval fixtures and mutation verification. And it is **forward-only**: the engine refused re-distillation 3/3 and the original raws are gone, so the compression of existing notes cannot be undone.

**Schedule**: this work changes distillation and therefore the content of the notes that get injected — it is **not orthogonal** to the §2 measurement window. Start after the window closes.

**Until it starts, raw transcripts are excluded from retention deletion — but it is not urgent.**

The earlier version of this paragraph (#259) said "originals keep being deleted while we defer, so the cost is permanent loss". **Measured, that was an exaggeration** (2026-09-02): retention **archives** processed sessions at 30 days (gzip, content preserved) and deletes **180 days after archiving**; of 3,023 transcripts the oldest is **75 days** and **0** files have passed 180. All 105 archives are restorable. Nothing is being lost right now.

The exclusion clause stays for two reasons anyway: ① the first deletion is ~105 days out and there is no guarantee R3 starts inside that ② R3 is forward-only because of originals **already lost**, not ones we will lose, so the remaining originals are the only range R3 can ever recover. Retention touches neither distillation nor injection, so it is orthogonal to the window.

### R4. The vault is the source of truth; derived state is consistent and rebuildable — *2 extensions*
- (Existing) Engine deploy drift: #222 made it a **readiness failure condition** and it was confirmed working with RC=1 before redeploy (coordinator measured). Closed.
- **(New) Config↔binary coupling**: a real case where a config value meant for a future binary put the current binary in a crash loop (`unknown variant 'python', expected 'rust'` — coordinator report, consistent with the config.rs enum extension in #226). Property: **a config parse failure must degrade to a diagnosable error state, not a crash loop** (parse-don't-validate means rejecting at the boundary, not repeated process suicide). Derived: report config rejection at startup via `/health` + a readiness check that catches "config newer than binary". Root cause first — no retry-loop mitigations (`GOALS.md:80` operating principle).
- **(New) The host CLI binary is unversioned**: `scripts/schedule-maintenance.sh:45` calls `./drudge/target/release/drudge code-sync`, and that binary is stale until `cargo build --release`; #222 only checks the engine. Widen M6's scope to the host — **dependency: the host binary first needs a version surface (`--version` → build sha).**

- **(New) Merged is not delivered.** Every gate in this repo judges **inside the repo**, but all six observed delivery failures died at **joints outside the repo** (cron install lists, hook registration count, production env, Makefile entry points). When the judged target ≠ the executed target, a gate structurally cannot catch it. Derived: readiness asserts ① hook path uniqueness (exactly once after normalisation) ② production presence of the `BORING_*` gate flags the code reads ③ a registered caller for every new execution surface (one of Makefile, cron, hook). M6's scope widens from "binary match" to **"call path reached"**. No new tooling — this is a doctor/readiness extension.
- **(New) Writes to the measurement ledger are closed on the recorder's side.** The convention ("tests remember to spool") has already failed — `test_codex.py` patches `BORING_EVENT_SINK=spool` in 17 places and a missed path still leaked 2 rows per `guard.sh` run into the production store. Derived: force spool at the single test entry point so an individual test cannot forget. Do not flip the production default — production opt-in would require **delivering** `=db` to every hook and cron environment, which re-imports the delivery-failure class above into the instrumentation itself.

- **(New) Confirm delivery on merge day.** A PR that touches files reaching `drudge/` or `~/.hermes` confirms `/health.build_sha == HEAD` and an installer run on the day it merges, and **until then its claims are not cited as applied live.** Measured 2026-09-02: drift caught and fixed in the morning recurred immediately with an afternoon merge (#266, `store.rs`), leaving the engine at `5b5f0ab` — the **seventh** of the same class. Doctor catching it afterwards and confirming at merge time are different jobs.

### R5. Boundaries closed by default — unchanged, nothing derived
(Confirming the state of the §8 Q7 `forget` traversal is still the owner's.)

### R6. The read door is fast and LLM-free — *throttle decision (this round's product decision)*
Facts (all verified): the Claude Code hook injects on every prompt with no throttle (`agents/claude-code/recall.py:46` — `throttle_session` not passed, default False at `recall_core.py:128`), only Kimi has it on (`agents/kimi/recall.py:30`), and `agents/claude-code/README.md:10` **lies** with "throttled to once per session".

**Decision: drop the throttle concept and make the docs match the facts.** Three grounds:
1. ~~**The cost is not real** — 3×280 chars is ~0.1% of a modern context.~~ **This ground is rejected by the 2026-09-02 measurement.** That calculation measured **one injection**; the channel puts **32–168 per session** into the same context. Ledger measurement:

   | | Injections | Context loaded | Est. irrelevant (precision 0.319) |
   |---|---|---|---|
   | Median session | 32 | ~28,800 tokens | ~19,600 |
   | Top quartile | 75 | ~67,500 | ~45,900 |
   | Max | 168 | ~151,200 | ~102,800 |

   With an average cache read of ~502k tokens per message, the worst session has **about 30% of its context as recalled notes**, two-thirds of it irrelevant. Weekly, about 2.05M tokens are loaded into conversations and, through 64× cache amplification, contribute **~130M tokens** of cache reads per week (0.71% of cache_creation).

   **And 34% of those are notes already given in the same session** — of 3,795 in-session injected hits, 2,521 are unique; one note was re-injected up to **25 times** in one session. That text is already in the agent's context, so this part is not even noise but **pure duplication**.

   Even with ground 1 rejected, **the decision (dropping the throttle concept) stands** — grounds 2 and 3 hold it up independently, and a throttle would not fix duplication anyway (it kills the good injections too). But **the claim that there is no cost can no longer be made**, and in-session dedup is the first candidate after the window closes (frozen under §5-R6, so no change during the window). The real cost is still noise, but **tokens were not free either.**
2. **Measurement integrity** — the two-week uptake sample is defined on the current injection frequency. Changing frequency mid-window voids the §2 contract. Frozen for the window.
3. **The verdict supersedes the decision** — if not working, the channel itself is redesigned and the throttle debate is moot; if working, per-prompt injection has proven its value.
Derived: ① remove the falsehood in `agents/claude-code/README.md:10` (immediately, a doc fix) ② Kimi's `throttle_session=True` is disposed of **after the window** when the channel contract is unified (the no-change-during-window rule applies to Kimi too — though Kimi's channel volume is negligible, inferred). The AI PM's point that "nothing is offered at the moment of demand (being stuck)" is right, and that surface goes to §8 Q8 as a **next-design candidate** with M7 as input.

### R7. The injection surface is a declaration, not code — *new*

The surface is already split along three axes — **trigger, selection, render** — and the code does not acknowledge it:

| Axis | Chunk injection | Claim card |
|---|---|---|
| Trigger | `UserPromptSubmit` | `SessionStart` |
| Selection | `/search` (prompt, 3 + 2 control) | `/context` (project, max_items=5) |
| Render | `- [src] snippet[:280]` + fence | `- [kind\|conf] subject predicate: value` |
| Ledger record | yes | **no** |

Derived: writing surface/policy parameters directly into adapter code is a defect (grounds: the 2026-09-02 kimi non-delivery incident — the dedup fix in #245 was delivered only to `wire_claude_code`). Adapters hold only emit code that renders the declaration. **Side benefit**: claim cards currently sit outside the uptake ledger, so "received and not used" cannot be measured; unifying instruments them for free.

### 5.7. Status of code_index
The engineer's objection is accepted: the consumer of the cheapest R2 implementation (claim → `file:symbol` anchor) is the code index, and Python coverage is exactly what claims of the `recall_core.py` shape require. Deployment measurement supports it: python 11,678 symbols, `code_search("session_uptake")` points precisely at `uptake_core.py` (coordinator confirmed). **Ruling: code_index is derived as conditional infrastructure for R2 — the condition is the owner choosing the anchor design in §8 Q5.** Choosing TTL/decay would make it non-derived again, in which case it is frozen in maintenance mode (daily sync + doctor staleness warning already exist, #226) or gets its own slice definition.

## 6. Derived-document anti-drift

**Two concessions**: ① a version handshake alone is indeed a "two-comment-line checksum" — both can be bumped while the content stays stale. ② v0.1 rule 2 (R-id anchors) implicitly demanded a rewrite of GOALS.md into a parseable grammar and left that cost out of the estimate ("1–2 Rust tests + one comment line") — **a dishonest estimate.**

**One defence**: the handshake's purpose is not to guarantee truth but to **remove silence**. The failure mode v0.1 aimed at was "the PRD moved and the derived doc went stale *quietly*", and a forced touch makes staleness loud. No mechanical rule can guarantee content truth; the remainder belongs to review.

**Redesign (minimal, existing patterns only)** — limited to contains-level checks like `quality_gate_readmes_match_mcp_tool_inventory` (`drudge/src/serve/mcp.rs:2096`):
1. Version handshake: assert PRD `<!-- prd-version: N -->` ↔ GOALS `<!-- derived-from: PRD vN -->` match.
2. R-id existence check (both directions, **no grammar rewrite**): every `R#` token the PRD defines appears at least once in GOALS.md; every `[R#]` token in GOALS.md exists in the PRD. grep level — GOALS only needs an `[R4]`-style tag on gate rows.
3. ~~Number SSOT (v0.1 rule 3)~~ — **out of this slice.** At the current document count, cost > benefit.

**Honest estimate**: 1 Rust test + GOALS.md tagging = 0.5–1 day. **Permanent upkeep**: every R-id change in the PRD forces a simultaneous GOALS edit (5–10 min each) — that is both the cost and the feature. A check that fails untagged gate rows (defect-log blocking) is deferred until the GOALS rewrite cost is settled.

## 7. Adoption path and the next slice

### 7.1 Adoption
**Commit to `docs/PRD.md` in this slice.** The decisive reason: the §2 verdict contract is only meaningful if it is registered **before the data arrives, in a commit different from the one that plants the measurement** (the very rule this repo learned from 0.514 — `run_eval.py:190–194`). A scratchpad cannot be a registration site.

**One document.** "Contract first, the rest next commit" is how the second document dies, and the anti-drift handshake needs a PRD to attach to.

### 7.2 Next slice: **"demand verdict for the injection channel"** (derived from R1×§2, zero build, falsifiable in two weeks)
| Item | Content |
|---|---|
| Derived from | R1 (injection precision) + the §2 verdict contract. Not a defect list — one product question: "is there demand for this channel" |
| Work | ① complete the uptake window (instrumentation deployed, events 0→N) ② accumulate labels: at 24 automatic per day, LLM n≥30 within ~2 days, human `--audit` ≥30 in parallel (§3 M1) ③ two doc changes: remove the README throttle falsehood + commit the PRD (§7.1) |
| Verdict (pre-registered) | exactly the §2 table — treatment vs control, refused below the sample floor |
| Falsification | how this slice itself fails is stated too — zero uptake events within 48h → instrumentation-fault investigation (§2); human–LLM agreement below `MIN_COMPARED 20` (`label_core.py:29`) → M1 stays unset |
| Not doing | changing injection parameters during the window (§5-R6), starting precision work (no intervention before measurement), implementing R2 (waiting on Q5) |

## 8. Owner decisions

### Instrument log — decisions that moved the window or the instrument, not the product

Each of these opened this document once. None of them changed a requirement, a metric, a floor
or a threshold; each moved a date, a boundary, or corrected a number. The full account of each
lives in the PR it names and in the vault notes it produced; the commit history of this file
holds the earlier long-form text. From here on, an instrument repair does not open the PRD —
it moves the constants in `verdict_core` in a dated commit and cites this table.

| Date | What | Where the account lives |
|---|---|---|
| 2026-08-31 | D1 — first window (08-26→09-09) reset: the control was counted per hit, not per prompt (`#242`); the hook was registered twice and doubled the sample (`#245`, `#246`). Reset at sample 0/0 | `#242` `#245` `#246`, this file's history |
| 2026-09-02 | D4 — ledger expiry (3 days) shorter than session lifetime (median 48h, max 177h); 78% of ledger rows lost before scoring. Expiry moved to 14 days; window kept, sample split at the repair commit (`LEDGER_REPAIR_AT`); verdict reads post-repair only | `1f45fec`, `scripts/peek.py`, this file's history |
| 2026-09-07 | D7/D8 — a mid-window baseline was hand-computed, then found double-counted and contaminated by filename matches from sessions inspecting the corpus. Figures void; only `event_log` rows at SessionEnd are the contract's value | this file's history |
| 2026-09-11 | D9 — second window (08-31→09-14) abandoned: 26 of 36 rows were mid-session snapshots from a drain forging SessionEnd (`#312`); the scorer missed `wiki-NNNN` without `.md` (`#313`). New window registered in code at sample 0/0; repair boundary = `#313` commit instant | `#312` `#313` `#314`, vault `wiki-1676` `wiki-1677` |
| 2026-09-11 | D10 — three channel changes landed before the window opened: fence usage protocol (`#316`), in-session dedup (`#317`, D6 built), concept-linked notes in the injection (`#318` `#319`). The window measures this channel. Still owed: consumption as edges, after the window | `#316`–`#320`, vault `wiki-1679` |

**Rule for §8 from here** (per the five tests this repo adopted for what belongs in a PRD): an
entry stays in §8 in full only if its subject is the product and it would still matter after the
defect that prompted it is fixed. Otherwise it is one row above.

### Q8 / D5. The moment-of-demand surface — adopted: **PostToolUse anchor trigger** (2026-09-02)

§5-R6 referred to "raised in §8 Q8", but **Q8 never existed** — one reference, zero definitions. This item pays that reference.

**Decision: the unit of observation for task scope is the file.**

| Signal | Discriminative power | Grounds |
|---|---|---|
| Prompt (current) | **fails** | distance mass 0.40–0.56, precision 0.319 — measured undecidable by distance |
| Error ("stuck") | **unmeasurable** | defined and instrumented in no log. The top recurring signatures are the agent's own heredoc noise (`Exit code N`) |
| **File** | **the only one with a measured ceiling** | of 4,190 code edits in 30 days, 971 (23%) touched a file a previous session had touched, and **81% of those (edit-weighted) are mentioned in a vault note** → ceiling ≈ 19% |

**First-person evidence**: the `SessionStart` claim cards reached the agent in this session too and **none was used** (`transcript.py update-allowlist`, `claude_clamp_default new-value`, etc.). All were "things that happened in this project" and none was "the file I am about to touch". That is not evidence the claims are bad but that **the delivery address was wrong because it was the project**. A one-session anecdote, though, so disposal only after the shadow log confirms it.

**Shadow only during the §2 window; injection after it closes.** PostToolUse is orthogonal as a surface, but **injecting changes the transcript and contaminates the §2 treatment's fingerprint echoes.** During the window: log only, inject 0.

**Falsification conditions (pre-registered)**: in the two weeks after the §2 window closes (dates registered in code when it opens, like §2's), if ① the anchor hit rate (share of revisit edits for which an anchor claim existed) is **under 10%**, or ② uptake of anchor injections is **under 2×** a random same-project-claim control or the gap is **under 3pp**, the anchor trigger is declared not working and the surface is removed. The thresholds reuse the §2 values and do not change during the window.

**Rejected candidates**: billboard + pull — pull demand is already dead (20 of 22 MCP tools at 1–2 uses per week, the last 3 active recalls all self-diagnosis). Stuck trigger — its ceiling cannot be measured, so the design is unfalsifiable. Kept as a second candidate after the anchor is validated.

### D6. In-session re-injection — 34% is not noise but duplication (2026-09-02)

3,795 injected hits fold to 2,521 unique notes. One note is re-injected up to **25 times** in one session. It is text the agent already holds in context, so it is **pure duplication** outside the precision debate.

**Dedup reads the uptake ledger as the source of truth and runs in the shared core.** Per-adapter state files are the structure that reproduced the kimi incident; putting it in the engine gives the server session state (precedent: the local-serve corpus doubling) and contaminates the control measurement. **If the ledger cannot be read, the injection is kept** — re-injection is cheaper than omission, and the ledger can die before the session does (§8 D4). Built 2026-09-11 (`#317`, §8 D10), first in §5-R6's derived list.

### D2. R2 anchor design (resolving Q5 of §5-R2)

**Adopted: code anchors + `supersedes` frontmatter. Canonical-axis registry and TTL/decay rejected.**

- Every measured contamination (`threshold-value 0.54`, eval `6/6`) is a **fact anchorable to code**.
- TTL/decay uses age as a proxy for truth — it demotes correct old claims and preserves wrong new ones, so it dodges the root cause. Rejected for the same reason this repo rejects defensive patterns.
- The canonical-axis registry is the premature taxonomy the PRD warns against. Held until the rule of three fires.
- `supersedes` stays: it is the only path to seal **conversational decisions** that cannot be anchored.
- Therefore **code_index is confirmed as R2's derived infrastructure** (§5.7's condition is met).

**The supply side is this design's weak point.** `superseded_by` used in **0 of 1,342 notes** is already measured evidence — *a field that only works if the author (a local LLM) emits it does not get emitted.* So anchors are attached not by LLM output but by a **deterministic post-distillation resolver** (claim subject → code_index lookup → automatic anchor). If only the consumer side (filters, markers) is solid and the supply side is left to the LLM, the entire first commit becomes dead code at coverage ≈ 0. That is why the first commit includes an **adoption counter**.

**The resolver's input is the note body, not the claim subject (corrected by 2026-09-02 measurement).** Measuring where the raw material is:

| Raw material | All | Recent window (197 notes) |
|---|---|---|
| `path:line` in claim value | **12 / 8,003 (0.15%)** | — |
| claim subject is a code file | 220 (2.7%) | — |
| `path:line` in **note body** | 90 notes (5.7%) | **31 (16%)** |
| `dir/file.ext` in **note body** | 293 notes (18.5%) | **66 (34%)** |

So the raw material is **in the note body and not in the claim rows.** A subject→code_index resolver dies at 2.7% coverage. Claims are already tied to notes by `source_path`, so extract from the body and **inherit**.

**Extraction has two tiers**: T1 `path:line` regex (recent window 16%, rising) → T2 path + backtick symbol promoted to a code_index symbol's line span (34%). An anchor is a `project:path[:span]` string **storable without verification**, and only repos registered in code_index get the existence-check and hash layers on top — because the biggest project in path notes is `oh-my-codereview` (88) and it is **not registered**.

**First-pass staleness is a symbol-span hash, not a line-range hash.** A line-range hash breaks on *moves* too, as D2 admitted. Re-querying the code_index symbol's line span and hashing that body is robust to insertions above/below and to moves; only symbol-unresolved anchors fall back to the line-range hash. If the span **moved with identical content, the anchor is re-pinned and not counted stale.**

**Anchors route around the axis collapse but do not replace it.** They do not fix the 84% singleton problem on `(subject, predicate)`; they add a new join axis — the "this file, now" reverse lookup works without predicate normalisation, and supersede matching goes through `same anchor + same kind` without the predicate. But the ceiling is ~34%, so for the other 66% the query axes are still only `kind` (7-value controlled vocabulary) + embeddings. The consumption query shape is **anchor filter (if any) → kind filter → embedding top-k**, with predicate demoted to a display string.

**What goes into the first commit**: 3 schema fields (`anchor`, `stale_at` + reason, `era`), the note-body T1/T2 resolver, symbol-span hash recomputation on sync → `stale_at` marking (re-pin if moved only), a `stale_at IS NULL` default filter on `current_claims` + `include_stale`, the legacy era migration (marker only), and the anchor/supersedes **adoption counter**.

**What must not go in**: the canonical-axis registry, TTL/decay, automatic stale recovery or re-distillation, rewriting `confidence` values, row deletion, new-language extension of code_index.

**Signal that it is wrong**: a `stale_at` spike right after a large refactor (line hashes break on *moves* too) → agents habitually turning on `include_stale`, which makes the filter meaningless. Measure: stale transitions per refactor commit vs actual invalidations.

### D3. Legacy 4,395 `certain` claims

**Adopted: an `era: pre-anchor` marker + a cap at display time. `confidence` values are not rewritten.**

Rewriting values is data tampering and cannot be audited. Instead an era marker is attached and the `claims` rendering caps it as `certain (legacy, unverifiable)` — the same shape as `#218` leaving a marker with a reason instead of silent deletion; reversible, and done in one migration. The "decay label" is a re-import of the TTL thinking D2 rejected, so it is rejected with it.

**Signal that it is wrong**: if the legacy was in fact mostly true, the briefing hedges on every fact it knows and the agent re-derives what it already knows — visible as a rising re-ask rate in recall responses.

### Schedule

Implementation of D2 and D3 starts **after the window closes**. Changing the distillation or claim surface changes the content of the notes that get injected, and that is not orthogonal to the §2 measurement (same reason as §5-R3).
