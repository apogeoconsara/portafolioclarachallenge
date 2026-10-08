# Synthetic data

Everything here is fictional. Company domains use the reserved `.example` TLD (RFC 2606) and free-mail domains are
`gmail.example`-style, so no address can ever be real or delivered. Nothing is derived from real companies or people.

## Regenerate

```bash
cd growth-orchestrator
python3 -m generator all --seed 42 --n 50000     # ~1.5 min, stdlib only, no installs (build + seed + impact + profile)
python3 -m generator build --n 50000 --no-sqlite # tables + event stream only
python3 -m generator seed                        # frozen seed artefacts (data/seed)
python3 -m generator impact                      # data/reports/impact_example.md (simulated experiment)
python3 -m generator profile                     # data/reports/data_profile.md + 27 validation checks
```

Same `(seed, n)` ⇒ byte-identical files (`manifest.json` carries a determinism hash; a test enforces it).

## Layout

| Path | In git? | What |
|---|---|---|
| `data/seed/reply_seeds.jsonl` | yes | 117 reply seeds drafted with an AI assistant, 13 labels, English, with qualification annotations (review pending) |
| `data/seed/golden_scenarios.jsonl` | yes | 89 curated scenarios: full state + events + expected outcome per event |
| `data/seed/eval_cases.jsonl` | yes | AI eval: 10 core reply cases + 4 extended + 4 grounded-personalization cases |
| `data/seed/llm_recordings.jsonl` | yes | 220 recorded (fake) model outputs: correct and defective, each with the verdict a validator must reach |
| `data/seed/ai_schemas.json` | yes | output contracts, validator rule catalogue (V/P/G codes), label→action map |
| `data/seed/mock_api_contracts.json` | yes | how the 4 mock systems (CRM, enrichment, outreach, calendar) fail, rate-limit and return uncertain outcomes, and what the orchestrator must do |
| `data/seed/send_policy.json` | yes | send windows, caps, API limits, retry rules, forbidden claims, **what AI may / may not decide** (ASSUMPTIONS) |
| `data/seed/outreach_templates.jsonl` | yes | approved English templates, sequence steps 1–4 |
| `data/seed/funnel_assumptions.json` | yes | funnel, unit economics, guardrails, experiment design (ASSUMPTIONS) |
| `data/seed/demo_flows.json` | yes | demo script (success, duplicate, failure, unsafe AI, malformed AI, race) + "change one assumption" drills |
| `data/seed/sample/` | yes | the same generator at n=500 (tables, events, truth) for quick runs |
| `data/generated/` | **no** (regenerated) | the full 50k-account world: JSONL + `growth.sqlite` + `truth/` |
| `data/reports/data_profile.md` | yes | distributions, coverage matrix, 27 validation checks |
| `data/reports/impact_example.md` | yes | measurement-plan worked example on SIMULATED outcomes |
| `data/TRACEABILITY.md` | yes | challenge requirement → data → test (references are test-checked) |
| `data/POLICY.md` | yes | the decision policy the labels encode |

## Tables (`data/generated/*.jsonl` and `growth.sqlite`)

| Table | Rows (n=50k) | Notes |
|---|---|---|
| `accounts` | 50,000 | firmographics, `crm_status`, `crm_owner_ae_id`, enrichment freshness/attempts, `list_id` |
| `contacts` | ~119k | 1–5 per account; `email_status`, function, seniority; everyone writes English (names are regional) |
| `opportunities` | ~9.8k | open stages, closed-won, closed-lost |
| `outreach_history` | ~42k | sequence / AE / CS touches before the snapshot |
| `suppression` | ~2.9k | contact- and domain-level; unsubscribe, hard bounce, complaint, DNC, legal hold |
| `company_facts` | ~81k | grounding facts with `fact_id`, source, `observed_at`, `is_verified` |
| `events` | ~56k | webhook envelope in ingestion order: `delivery_id`, `event_id`, `idempotency_key`, `type`, `occurred_at`, `received_at`, `payload` |
| `mock_behavior` | 50,000 | per-account behaviour of the mock enrichment / send / calendar APIs (deterministic) |
| `mock_enrichment` | ~2.3k | the response the enrichment mock returns for accounts that need enriching |
| `aes` | 40 | account executives: timezone, capacity, leave, inactive, backup |
| `ae_calendar` | ~0.8k | free 30-min slots per AE per working day |
| `experiment_assignments` | 50,000 | control/treatment, stratified, clustered by domain |
| `experiment_sim_outcomes` | 50,000 | **simulated** funnel outcomes under documented assumptions (`simulated: true`) |

Snapshot time (`as_of`) is `2026-10-01T16:00Z` (inside the send window in all six countries); the stream covers the following 31 days. Targeting events arrive in
weekly bursts of ~20 minutes, which is what stresses rate limits and queues.

### Event types

`account_targeted` · `reply_received` · `unsubscribe_received` · `email_bounced` · `meeting_booked` ·
`opportunity_created` · `opportunity_stage_changed`

### Perturbations (explicit rates; truth in `truth/truth_events.jsonl`)

| Perturbation | Rate | Expected handling |
|---|---|---|
| exact duplicate (same event_id & key) | 3.0% | ignore |
| semantic duplicate (new event_id, same key) | 1.5% | ignore |
| content duplicate (other source, other key) | 0.5% | dedupe by content |
| delayed (10 min – 3 days late) | 2.5% | process using `occurred_at` |
| malformed (7 kinds) | ~1% extra deliveries | dead-letter |
| out-of-order race (late unsubscribe / late opportunity) | 500 accounts | apply + reconcile + cancel pending outreach |

## Ground truth — never read it from the orchestrator

`truth/` holds labels (`truth_accounts`, `truth_events`, `truth_facts`, `truth_replies`). Only tests and evals may read it.
A test asserts none of those fields leak into the source tables or event payloads.

`truth_accounts` is a snapshot at the as-of time. `truth_events` is **stateful**: an `account_targeted` that is delivered after an
event that changed its account (a deal closing lost, an unsubscribe, a hard bounce, an opt-out reply) is judged on the state
those events left, in delivery order (`generator/stateful.py`). Such rows list the deliveries that changed the state in
`state_changed_by`; every other row is exactly the snapshot. The policy and its rule order are not touched by this.
A `meeting_booked` on an account whose calendar is set to fail (`slot_conflict`) is expected to escalate, never double-book, and
is marked `injected_failure`: handling it is the right outcome, not a mismatch. Reply typos never touch the words the validator
reads (a referred name, the month of a date, country / budget / timeline words).

## How the volume is shaped

* **Exact scenario quotas** (not random draws) so rare branches always have enough examples — see the coverage matrix in
  `data_profile.md`. Per-account RNGs keep stages independent.
* **Independent oracle** (`generator/oracle.py`) re-derives each account's expected action from the tables alone;
  the validation fails if it ever disagrees with the scenario label. It is a data-quality check, not the production engine.
* **Golden set** (89) covers qualification extraction, send windows and caps, AE routing, precedence conflicts (customer + unsubscribed, AE-owned + non-ICP), boundaries (13d23h vs 14d1h),
  dedupe, malformed events, races, deterministic (no-AI) events, integration failures and grounded personalization.

## Provenance of reply text

The 117 reply seeds are fictional text drafted with an AI assistant at my request (no API call to a model in the data, no real
emails). **I still need to review them, the golden set and the recorded AI outputs before presenting; until then I should not
call them hand-written or reviewed.** Bulk replies are seed text plus deterministic wrappers (greetings, signatures, typos, a
quoted original with an unsubscribe footer as realistic noise, disclaimers). `prompts/reply_generation.md` is the prompt for
drafting more seeds: run it against a model, review a sample, and append the result to `generator/reply_seeds.py`.

**Where the files are and how to analyze them:** `data/HOW_TO_ANALYZE.md`.

For a long, plain-language walkthrough of every table, percentage and deliberate error, read `data/DATA_REPORT.md`.
