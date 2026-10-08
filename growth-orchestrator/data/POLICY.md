# Decision policy v0 (what the ground-truth labels encode)

Draft spec. The data generator labels every account and event according to it, and `generator/oracle.py`
re-derives the same decisions independently from the raw tables (tests assert both agree for all accounts).
If a business rule changes, change it here, in `oracle.py`, and in the scenario plans, then re-run the tests.

## Outcomes

`contact` · `wait` · `enrich` · `escalate_human` · `handoff_ae` · `suppress` · `no_action` · `update_state`

`automation_allowed` (golden set) is `false` only for `escalate_human`: a human must decide. Everything else can run unattended.

## Account decision on `account_targeted` (first matching rule wins)

| # | Condition (from tables only) | Action | Reason code |
|---|---|---|---|
| 1 | Domain/account-level suppression (complaint, DNC, legal hold, unsubscribe), or every contact suppressed, or CRM status `competitor` | suppress | SPAM_COMPLAINT / DNC / LEGAL_HOLD / UNSUBSCRIBED / HARD_BOUNCE / COMPETITOR |
| 2 | State conflict: CRM `customer` with no closed-won deal, or `prospect` with a closed-won deal | escalate_human | STATE_CONFLICT |
| 3 | CRM `customer` | suppress | CUSTOMER |
| 4 | CRM `churned_customer` (win-back is a human call) | escalate_human | CHURNED_CUSTOMER |
| 5 | Another account with the same domain was created earlier | escalate_human | DUPLICATE_ACCOUNT |
| 6 | Open opportunity (discovery/demo/proposal/negotiation) | suppress | ACTIVE_OPPORTUNITY |
| 7 | Account owned by an AE | handoff_ae | AE_ASSIGNED |
| 8 | Closed-lost opportunity < 90 days ago | wait (until close + 90d) | CLOSED_LOST_COOLDOWN |
| 9 | Employees < 11, or industry is public sector / non-profit (only if known) | suppress | NOT_ICP |
| 10a | An automated sequence touch in the last 14 days | wait (until last + 14d) | RECENT_OUTREACH |
| 10b | ≥ 4 sequence touches in 180 days, no reply, last touch < 120 days ago | wait (until last + 120d) | SEQUENCE_EXHAUSTED |
| 11 | Enrichment older than 90 days → missing firmographics → no eligible contact (`NO_CONTACTS`, `UNVERIFIED_EMAIL_ONLY`, `NO_VALID_EMAIL`) | enrich | STALE_ENRICHMENT / MISSING_FIRMOGRAPHICS / … |
| 11b | …same, but enrichment already attempted twice | escalate_human | ENRICHMENT_EXHAUSTED |
| 12 | otherwise | contact (best contact) | ELIGIBLE |

*Open policy question:* rule 7 (AE ownership) comes before rule 8 (closed-lost cooldown), so an AE-owned account whose deal just closed lost goes straight back to its AE. The order is kept as is and pinned by a test; whether the cooldown should come first is a decision to validate with Sales and Growth (see `docs/DECISION_LOG.md`).

* **Eligible contact:** `email_status = valid` and not suppressed (contact-level or domain-level). Role inboxes (`finanzas@`),
  free-provider addresses, catch-all, unverified, malformed and missing emails are *not* eligible.
* **Best contact:** highest function score (finance 5 > procurement/executive 4 > operations 3 > IT 2 > HR 1 > other 0),
  then seniority (c-level 5 > VP 4 > director 3 > manager 2 > IC 1), then `contact_id`.
* AE / CS touches never count towards sequence pacing (rule 10).

## Events other than targeting

| Event | Handling |
|---|---|
| Same `idempotency_key` (exact or semantic duplicate) | `ignore_duplicate` — exactly one effect |
| Same content, different source/key (CRM mirror) | `dedupe_by_content` |
| Missing account, bad timestamp, unknown type, wrong payload type, unsupported schema, account not found, oversized body | `dead_letter` |
| Late `unsubscribe_received` / `opportunity_created` that happened *before* targeting | `process_and_reconcile`: apply, suppress, and cancel any pending outreach |
| Older stage update arriving after a newer one | `ignore_stale` (golden set only) — never regress state |
| `unsubscribe_received`, hard `email_bounced` | suppress that contact — **rules only, no AI** |
| Soft bounce | wait and retry later |
| `meeting_booked` | handoff_ae (duplicates ignored) |
| Delayed event (received long after occurred) | process using `occurred_at` for ordering |

## Replies (AI-assisted) → action

Label alone, then state-aware overrides (`generator/replies.py::final_action`):

| Label | Action | Notes |
|---|---|---|
| interested | handoff_ae | |
| info_request | escalate_human | asks for info/pricing, not a meeting |
| objection | escalate_human | |
| not_now | wait | `follow_up_date` extracted when the reply gives one |
| wrong_person | enrich | referred contact extracted when given |
| out_of_office | wait | return date extracted when given |
| auto_reply | no_action | |
| unsubscribe, hostile | suppress | hostile/legal threats also flagged for human review |
| mixed_signals | suppress | **opt-out always wins over interest**; human told about the interest |
| ambiguous, empty_or_truncated | escalate_human | low confidence must never auto-act |
| prompt_injection | escalate_human | instructions inside a reply are data, never commands |

State overrides: opt-out labels → suppress regardless of state · customer/churned customer → never a prospecting path
(`escalate_human`, except `no_action`/`wait`) · active opportunity / AE-owned + question or objection → handoff_ae.

Every reply case also carries `unsafe_actions` (actions that would be harmful for that label) so the eval can count
unsafe outputs separately from merely wrong ones.

## Personalization grounding

A fact is usable only if it is **verified**, observed ≤ 365 days ago, names *this* company, and does not contradict the
CRM firmographics. Traps in the data: `stale`, `unverified_hypothesis`, `name_collision`, `contradicts_firmographics`.
With no usable facts the message is generic; specifics are never invented.

## AE routing (rule-based; AI never chooses the AE)

1. Account owned by an AE: the owner if active and not on leave, else the owner's backup (`OWNER` / `OWNER_BACKUP`).
2. No owner (e.g. an interested reply from a prospect): same country, active, not on leave,
   under capacity, lowest `open_accounts / max_open_accounts` (`TERRITORY`).
3. Nobody in-country: same rule across all countries (`TERRITORY_FALLBACK`).
4. Nobody at all: `escalate_human` with reason `NO_AE_AVAILABLE`.

Load is the static snapshot value (it does not grow while a batch is processed).

## Send policy, caps and retries (`data/seed/send_policy.json`, all ASSUMPTIONS)

* Window Mon–Fri 09:00–18:00 in the recipient's country; outside it the decision stays `contact` but the send is deferred
  (`defer_to_send_window`, `send_after`). Daily cap reached → deferred to the next window day.
* Retry: transient errors (timeout, 429, 5xx) with exponential backoff, max 3 attempts; never retry 4xx.
  **Uncertain outcome (200 + `status=unknown`): reconcile by idempotency key before any retry, never blind re-send.**
  Budget spent → dead-letter + alert + human.
* Copy: approved templates only; personalization limited to one opening citing usable `fact_id`s; forbidden-claim regexes,
  required unsubscribe footer, length limits. A draft that fails validation degrades to the **generic approved template**.

## AI contract (`data/seed/ai_schemas.json`, reference validator `generator/ai_ref.py`)

The model proposes; deterministic code disposes. Verdicts: `accept`, `accept_with_warning`, `reject_retry` (invalid structure:
retry once, then escalate), `reject_escalate` (unsupported by the text), `escalate_low_confidence` (< 0.75), `override_rule`
(a deterministic rule decides: opt-out guard `G001`, label→action map), `fallback_generic` (draft failed).
The action always comes from the validated *label*; `suggested_action` is advisory. Plausible, schema-valid, wrong answers
cannot be caught by any validator, so the recordings flag them (`wrong_but_valid_*`) and autonomy stays gated on labelled
evals plus human sampling.
