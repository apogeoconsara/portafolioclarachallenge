# Decision log

The four questions the challenge asks, how the system adapts if an assumption changes, then the smaller calls that
shaped the build.

## Something I deliberately did not build

**Real sending, and any real integration.** Outreach goes to a mock ledger only: the engine's executor has no email
client, the live Netlify function has no email code, and a test fails if any module imports an SMTP/HTTP client other
than the LLM client. Real CRM / enrichment / calendar connectors are mocks with every failure mode the contracts describe
(`data/seed/mock_api_contracts.json`). Sending real email to synthetic people would prove nothing and risk a lot;
the interesting parts (eligibility, idempotency, reconciliation, AI validation) are fully exercised against the mocks.

Also not built: a queue/worker split (events are processed synchronously, one transaction each), multi-channel
sequences (email only), and a production approval queue (reviewer sign-in, roles, SLAs, a durable inbox for reviewers). The engine does hold every outreach email as `pending_approval` and `approve()` / `reject()` record a named reviewer, but the site's Approval Queue page cannot call the engine: its decisions live only in the reviewer's browser.

## Where I deliberately did not use AI

- **Eligibility, suppression, routing, timing, caps.** These are policy and compliance; they must be explainable, testable
  and identical every time. `orchestrator/rules.py` reproduces the independent truth on 500/500 sample accounts.
- **Explicit opt-outs.** Unsubscribe events, hard bounces and an opt-out regex on replies are handled deterministically;
  when the model labels an opt-out as anything else, guard G001 overrides it to `suppress`. Opt-out is honoured even when
  the model is unavailable.
- **Which facts may be used for personalization.** `usable_facts` is a rule (verified, ≤ 1 year old, names this company,
  consistent with CRM headcount). The model only rephrases one of them into the approved template.
- **Choosing the action.** The model returns a label and evidence; `LABEL_ACTION` plus state-aware overrides choose the
  action. The model's `suggested_action` is recorded and ignored (V010 when it disagrees).

## The most important tradeoff

**Safety over automation rate.** Anything uncertain goes to a human: low confidence (< 0.75), output that fails
validation twice, a quote not found in the reply, an injection attempt, contradictory enrichment, an uncertain external
outcome that cannot be reconciled, a late fact that arrives after an email went out. This lowers the share of replies
handled automatically and adds review load, but the costly errors in outbound (emailing someone who opted out, a customer
or an active deal; double-sending; promising things legal has not approved) become structurally hard to make.
The same tradeoff shows in personalization: no usable fact → the generic approved template, never invented specifics.

## The biggest production risk

**A confident, well-formed, wrong interpretation.** Validation catches malformed, unsupported, invented and injected
outputs (all 220 recorded outputs are judged as expected), but a reply that is genuinely ambiguous can still get a
plausible label with a real quote and high confidence (`EV-R-AMB-03:wrong_but_valid_overconfident` passes validation and would hand an
unclear lead to an AE). Mitigations: label-level human review at launch (100% of AI-driven handoffs), the live eval
suite as a release gate on every prompt/model change, per-label precision monitoring from reviewer corrections, and
an autonomy rollback switch. See [AI.md](AI.md#before-giving-the-ai-more-autonomy).

Second risk: **deliverability and compliance drift** — caps, windows and footers are assumptions in
`send_policy.json` and must be replaced with Clara's real rules before anything is sent.

## A policy decision I left open: AE ownership before the 90-day cooldown

Today the rules check "an AE owns the account" (rule 7, `AE_ASSIGNED`) before "a deal closed lost less than 90 days ago"
(rule 8, `CLOSED_LOST_COOLDOWN`), so an AE-owned account whose deal just closed lost goes straight back to that AE. In the
sample stream, `acc_000015` closes its negotiation lost on 8 October and is handed to the same AE on 15 October. This is a
business decision, not a data error: the engine and the independent oracle agree on it, and a test pins the rule order so that
changing it is a deliberate edit.

I did not change it. In production I would validate that priority with Sales and Growth. Personally, I would review whether the
cooldown should come first, to avoid contacting an account again too soon after a loss. Moving it is a one-line reorder in
`orchestrator/rules.py` and `generator/oracle.py`; its effect can be measured on the same 50k stream before adopting it.

## If an assumption changes

Policy numbers are configuration, not code, so most changes are a config edit plus a re-run of the tests and of
`compare-scoring` / `export-overview` to see who changes group. The engine reads `data/seed/decision_policy.json` and
`send_policy.json`; the data generator and its independent oracle read `generator/config.py`, so both move together.

| if this changes | what I would touch | how I would check it |
|---|---|---|
| cool-down after outreach goes from 14 to 30 days | `recent_outreach_days` in `decision_policy.json` and `RECENT_OUTREACH_DAYS` in `generator/config.py` | `G010` (last touch 14 days + 1 hour → eligible) flips to waiting, so the boundary goldens `G009`/`G010` get new dates; oracle agreement and the 89 goldens re-run |
| volume grows 10× (500k accounts a month) | queue + workers with per-account ordering, Postgres instead of SQLite (see [PRODUCTION.md](PRODUCTION.md#scale)); the logic does not change | `generator build --n 500000`, then queue depth and `api_limits` in `send_policy.json` |
| sending is allowed for real | an ESP behind the existing executor (idempotency key per send), the approval step built for real, Clara's caps and footers in `send_policy.json` | shadow mode first, then the experiment with a small treatment share |
| a second channel (e.g. WhatsApp) | `channel`, templates and opt-out rules per channel | an opt-out on one channel must suppress the other: a new golden |
| the AI may no longer act on its own, or may do more | `ai_autonomy` in `send_policy.json` (human sampling, confidence threshold) | the opt-out guard is already deterministic; the live eval as a release gate |
| the CRM or enrichment provider becomes unreliable | retry budget, circuit breaker per provider, dead-letter + alert | failure goldens `G070`–`G078`, `G110`–`G117` |
| SDR capacity doubles | nothing in the system; the control arm reaches more accounts | the incremental pipeline shrinks in the worked example; the measurement design stays the same |
| a "not now" reply names a horizon instead of a date ("next quarter", "in two weeks") and 7 days is too soon | the reply schema and prompt (a `follow_up_horizon` the model copies with its quote, never a date), a validator check that the quote is in the reply (like V006/V008), a fixed conversion table in the rules (weeks × 7, months × 30, "next quarter" = first day of the next calendar quarter, with a minimum and a one-year maximum), the JS port in the Netlify function; vague wording keeps the 7-day default or goes to a person | the prompt changes, so the 220 recordings, the parity test and the live eval run again; new reply seeds and goldens with horizons; the model reads and the code computes the date |

## Smaller decisions

| decision | why | cost |
|---|---|---|
| Python stdlib + SQLite, no framework | runs anywhere in seconds, reviewers read every line | no async, no migrations tool |
| Three-way dedupe (delivery id, idempotency key, content hash) | webhook retries, replays under new ids and CRM mirrors are different failures | content hash must be defined per event type |
| Idempotency key per business effect (`send:{account}:{contact}:{step}`) | exactly-once survives retries, replays and uncertain outcomes | a legitimately repeated email needs a new step |
| Look up by key before any retry after a timeout / unknown status | a blind retry is how double-sends happen | one extra call on the failure path |
| Re-decide before sending a scheduled email | facts can change between decision and send window | one rules evaluation per send |
| Lateness = a fact older than something already acted on | out-of-order arrival is normal; earlier decisions need reconciling | flags some benign cases to humans |
| Forced tool call + strict schema, one retry on invalid structure only | structure errors are cheap to retry; semantic errors are not | up to 2 model calls per reply |
| Offline fixture for recorded runs, real model for live panel/evals | recorded demos must be deterministic; live results must be real | two modes, always labelled |
| JS port of validators for the web, parity-tested | the deployed page must validate exactly like the engine | two implementations to keep in sync (tests enforce it) |
| Account priority score (size, payment-pain hypothesis, signals) next to the rules, not inside them | the rules decide eligibility; among eligible accounts the score picks the track (tier A is shown on the page as Top priority, B as Standard, C as Nurture): tiers A and B get a personal outreach email (a first email, or a follow-up if the company already got earlier ones), tier C goes to nurture (no email, no model call, audited as `nurture_enrolled` with the score version) | nurture only records the enrolment: no nurture content exists yet; weights are assumptions pending review |
| Scoring versions (`versions` + `active_version` in `scoring_policy.json`) and a compare view on the page | iterating on the weights must stay traceable: every logged score carries its version, and any two versions can be compared on the same data to see who changes group | versions edited in the browser are only saved locally until someone commits them |
| Audience check as a pass / fail / unknown checklist, fail-closed | missing data must read as "enrich first", never "assume fine"; reviewers see why an account is blocked | a second description of the rules, kept honest by a test against the engine (590 accounts) |
| Hours saved shown as an estimate from editable per-task minutes (`data/seed/time_assumptions.json`), counts from the engine's decisions on all 50,000 accounts | the value of automating is time, but the minutes are not measured; showing the assumptions keeps the number honest | a headline figure that moves when someone changes a minute |
| Page leads with plain-language views (Command Center, Live Demo, Decisions, Approval Queue, AI & Safety, Operations): seven tabs, each answering one question, with related views as sections inside a tab; the system diagram, flows and scenarios sit under "System" (Architecture) | the audience is people who run growth operations, not people who read audit logs | two descriptions of the same rules (everyday text in `orchestrator/plain.py`, tested to cover every reason code) |
| "Live Demo" has two views of the same engine: **Leads** (twelve synthetic companies, four per group, each run through the engine as a new target, with a page that shows the score and why, the routing decision, the CRM record, the outreach angle and which facts the rules allow) and **Scenarios** (sixteen recorded runs: a normal day, careful AI, the same event twice, and failing systems), each drawn as a journey that plays by itself. Every lead and scenario has a live Claude button | a reviewer should see in one minute which company, what the CRM said, the score and tier, which step decided, where the AI added value and which outside systems were touched, and be able to ask the real model about the same input | the engine is not run in the browser: leads and scenarios are recorded engine runs (model answer: an offline fixture that restates the first usable fact), and only the Claude buttons are live. The CRM is a mock with a HubSpot-style contract, labelled simulated |
| Operations page from the full 50k event stream through the engine (offline fixture model) | operating metrics (automation, review, failure, duplicates, dead letters, cost) must come from a run, not from a slide | AI failure and guard counts reflect the safety layer on fixture answers, not a live model; thresholds and cost per call are assumptions |
| The page separates automatic decision and preparation from sending: outreach emails (first emails and follow-ups) are "prepared automatically, pending human approval before sending" | the system automates the judgement and the drafting; an action that reaches a customer stays a human-approved step | the engine enforces it (next row), but the page's own queue is a demonstration: decisions stay in the browser, and there is no reviewer sign-in or production queue (see PRODUCTION.md) |
| Approval Queue page: a batch of 200 drafts held by the real engine as `pending_approval`, drawn from the 5,897 outreach emails prepared (tiers A and B; 3,161 first emails and 2,736 follow-ups), with approve / reject (reason), bulk approve, an override-rate metric and a decision log | makes the human-approval step visible and gives the reviewer override rate a place to come from | decisions live only in the reviewer's browser and nothing is sent: it shows the reviewer's side, it is not the production queue |
| The answer key for `account_targeted` is stateful: it is judged on the account's state after the events delivered before it (a deal closed lost, an unsubscribe, a hard bounce, an opt-out reply), replayed in delivery order by the generator's own oracle (`generator/stateful.py`, which does not import the engine) | the key used to describe the snapshot while the stream kept changing the state, so the engine looked wrong when it was right: 98 differences on the 50k stream, 90 of them this cause (46 closed-lost deals sent back to the AE, 41 suppressed after a bounce or unsubscribe, 3 more); the sample agrees 561/561 and, after two generator fixes, the 50k stream 55,959/55,959 on actions. The other 8 were not state: 7 replies whose text carried a typo on the exact words the validator reads (a date's month, a referred name), which broke the generator's own rule that a typo must not alter what the model has to extract: typos now skip those words (12 reply texts changed, nothing else moved); and 1 meeting booked on an account whose calendar is set to fail, an intended failure-handling case that the key now expects (escalate, never double-book) and marks `injected_failure` | the key is no longer a plain snapshot, so it needs the replay and the failure marks to stay in step with the engine, which `tests/test_answer_key.py` checks on a freshly generated world. A last field difference (862 `enrich` decisions where the engine overwrote `best_contact_id` with the contact enrichment found, against goldens that declare none) was an engine inconsistency and is fixed: the decision keeps its own contact and the found one is the email's recipient, so action, reason codes, contact and wait date agree on all 50,000 targeted events. Whether a closed-lost account may go straight back to its AE is a separate policy question, documented below and not changed |
| Approval gate inside the engine: Draft → Pending approval → Approved → Mock send. The engine only drafts and holds; `approve(key, reviewer)` re-decides eligibility, then releases (immediately or when the send window opens); `reject` closes it; the executor refuses any send without a recorded reviewer, and `run_due` only picks up approved emails. Applies to every outreach email, first or follow-up | "no outreach can execute without a person" is a property of the code, checked by tests (zero sends on the whole 561-delivery sample stream until someone approves; double approval sends once; a suppression or open deal that arrives while a draft waits blocks it), not a promise in the page text | the engine now stops at the draft: send failures and retries happen after approval, so the 50k operations metrics no longer include them; scenario tests use an explicit test approver; there is no reviewer sign-in, so the reviewer name is whatever the caller supplies |
| A held reply (`not_now`, `out_of_office`) waits until the date the prospect gave, otherwise 7 days after the reply (`engine.py`, action `wait`); the Live Demo shows this "next check" date | a wait must always end in a date the system acts on, and a stated date is the only one the model may extract (an exact calendar date, checked against the reply) | 7 days is an assumption: a horizon such as "next quarter" is lost and the account comes back too soon. The planned improvement is in "If an assumption changes"; not built, because it changes the prompt and needs the 220 recordings and the live eval run again |
| Scenario companies are lent from the committed sample world (their own facts and contact) while each scenario keeps its golden's mock behaviour, and the low-priority track is on as in production | the goldens are all alike (120 employees, no facts, a plain 30 points), so a demo built on them looks empty and cannot show a score, a tier or why a fact is blocked | the scenario runs are no longer the goldens themselves, so a golden id is not a company; the model answer for a draft is the offline fixture, which restates the first usable fact |
| Live model calls use `temperature: 0` (Python client and Netlify function) and the live eval is read as a range over several runs, not as one number | the calls left the temperature at the API default of 1.0, so two runs of the same 18 cases scored differently (14 of 14 and 13 of 14 on labels; 116 and 115 of 126 on fields); reading a reply is classification, so it should not vary on purpose | temperature 0 narrows the variation but does not make runs identical; the misses that remain are mostly judgement calls (interest level) or an added or omitted detail, and some may be the answer key's (drafted with AI, review pending); the stored run (2026-10-07) predates this setting and must be run again with the real key |
| Scoring changes are versions, compared before adoption: `compare-scoring` runs the whole 50k event stream through the real engine once per version and reports who changes group, outreach emails prepared, nurture enrolments, AI calls and estimated cost; the page shows it with a change log | a change to the weights must be explainable in operational terms, and decisions already taken must stay traceable to the version that produced them (the audit log records it) | the second version in `scoring_policy.json` is an illustrative proposal, not adopted; effects are on synthetic data and say nothing about real pipeline, which only the experiment can show |
| The AI has to earn its place in the experiment: a second step splits the system group into B1 (rules, approved template, replies read by a person) and B2 (the AI reads replies and writes the opening line), and the AI stays only if B2 beats B1 on qualified pipeline and cost per qualified lead without breaking a guardrail | most of the system's judgment is rules, and the simulation's lift is mostly coverage: it gives AI-written emails no advantage, so nothing yet shows the AI adds pipeline | smaller groups take longer to detect an effect, and B2 changes two jobs at once (reading and writing), so a later split would separate them |
