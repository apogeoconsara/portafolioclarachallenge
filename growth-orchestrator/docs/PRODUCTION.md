# Production thinking

What changes between this slice and something Clara could run.

## Reliability
- **Ingest**: accept the webhook, verify the HMAC signature (`ORCH_WEBHOOK_SECRET`, already in `serve`), write to a
  durable inbox, return 2xx; process from a queue (SQS / Pub/Sub) with per-account ordering (FIFO group = account_id)
  so one account's events never race each other. The three-way dedupe and the `processed` table stay as they are.
- **Exactly-once effects**: keep idempotency keys per business effect and the "look up before retry" rule; use the
  providers' native idempotency headers where they exist. Outbox pattern: decision + pending action in one transaction,
  a dispatcher performs the call.
- **Retries**: the policy in `send_policy.json` (backoff + jitter, Retry-After, never retry 4xx, budget then
  dead-letter + alert + human). Circuit breaker per provider so an outage does not burn every account's retry budget.
- **Scheduled sends**: a scheduler calls `run_due`; every send is re-decided at send time (already implemented).
- **Model API**: timeouts, one retry on 429/5xx, then the safe fallback (escalate / generic template) — never block
  the pipeline on the LLM.

## Security
- Secrets only in the platform's secret store (the Netlify function reads `ANTHROPIC_API_KEY` server-side; nothing is
  committed). Separate keys per environment, rotation, least privilege on CRM scopes.
- Treat replies and enrichment as untrusted input: quoted lines stripped, injection detection, the model cannot trigger
  actions directly, output validated before use, HTML-escaped in any UI.
- PII: minimise what goes into prompts (no emails/phones unless needed), retention policy for replies and model I/O,
  provider DPA, regional data residency where required (LatAm data-protection laws: MX LFPDPPP, BR LGPD, CO, AR, CL, PE).
- Compliance: suppression is global and checked at decision AND send time; unsubscribe footer is mandatory; legal
  sign-off on templates and claims.

## Observability
- The audit log is the trace: one row per step with event, state version, decision, reasons, model output, verdict and
  action. Ship it to a warehouse.
- Metrics: events by handling, decisions by action/reason, AI verdicts and codes by label, confidence distribution,
  reviewer override rate, retries / dead letters per provider, review-queue age, sends vs cap.
- Alerts: any unsafe-action indicator, opt-out guard firing rate jump, dead-letter spike, provider error rate,
  review-queue SLA breach, guardrail metrics from the experiment.

## Scale
- 50k accounts/month is small: ~2k events/day. SQLite → Postgres (row-level locking on account versions), workers
  scale horizontally because ordering is per account.
- LLM cost: ~1 call per reply and ≤ 1 per personalized email; Haiku-class pricing keeps it in cents per thousand
  accounts. Cache by input hash for replays.
- Enrichment is the expensive dependency: batch, cache by domain, respect provider rate limits (already modeled).

## Build vs buy
| piece | choice | reason |
|---|---|---|
| eligibility rules, routing, guards, audit | **build** | this is Clara's policy and its competitive edge; must be testable and owned |
| CRM, sequencer/ESP, calendar | **buy** (HubSpot/Salesforce, an ESP with suppression + deliverability tooling, Google/Outlook calendars) | commodity, deliverability is a specialist problem |
| enrichment | **buy** (one primary + one fallback provider) | data coverage is the product; contradictions handled by our rules |
| LLM | **buy** the model, **build** the prompts, validators and evals | the safety layer and eval set are ours |
| queue, scheduler, warehouse | managed cloud services | no reason to run them ourselves |

## Before turning on real sending
Replace the assumption files (`send_policy.json`, `decision_policy.json`) with Clara's real rules; connect the real
suppression source of truth; run in shadow mode (decide, don't act) against live events for two weeks and compare with
what SDRs did; then start the experiment in the measurement plan with a small treatment share.

## If Clara runs this on n8n
An earlier prototype used an n8n workflow. This slice keeps the logic in code, and the same shape maps onto n8n nodes:
webhook → validate payload (reject malformed, dedupe) → CRM lookup → **score** (code node, deterministic) → gate (IF: below the Tier B threshold, skip the model) → model call (HTTP node, validated output) → **rules** decide the action → human approval for escalations → CRM / sequencer write with the idempotency key. The score and rules nodes stay code, never prompts; the model only labels, extracts and drafts. Whether n8n is part of Clara's stack is an open question.

## Approval before anything leaves the building
The engine decides and prepares; it sends nothing to anyone, and it does not even send to its simulated log until a person approves. Every outreach email, first or follow-up, is drafted and held as `pending_approval`; `Orchestrator.approve(key, reviewer)` re-decides eligibility and releases it (now, or when the send window opens), `reject` closes it, and the executor refuses to send anything without a recorded reviewer. `python -m orchestrator serve` exposes this as `GET /approvals` and `POST /approvals/<key>/approve|reject`. The Approval Queue page of the demo shows the reviewer's side (drafts, reasons, approve / reject, override rate), but a static page cannot call the engine, so its decisions stay in the browser.

What production would add: reviewer sign-in and roles (the reviewer name is whatever the caller supplies today), a durable queue with age and SLA alerts, bulk approval by segment with sampling, and an approval record that lives in the audit warehouse. Start with approval on every email, then relax it per segment once the reviewer override rate and the guardrail metrics justify it.

