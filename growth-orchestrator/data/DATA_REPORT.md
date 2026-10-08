# Data report: a complete guide, assuming no prior knowledge

> **Who this is for:** anyone who does not know what a "seed" is, why the percentages are what they are, which parts of the
> data are deliberately wrong, or what kinds of data exist. Everything is explained from scratch.
> **All figures** come from the run `--seed 42 --n 50000` and from the automatic report `data/reports/data_profile.md`.
> If I regenerate with other parameters, the figures change. **Everything is in English**, because the case and the
> presentation are in English. **Where to find the files:** see section 16 and `data/HOW_TO_ANALYZE.md`.

---

## Contents

1. [What this is and what it is for](#1-what-this-is-and-what-it-is-for)
2. [Glossary](#2-glossary)
3. [Why the data is synthetic](#3-why-the-data-is-synthetic)
4. [Map: the kinds of data](#4-map-the-kinds-of-data)
5. [Part A, the "world": the tables](#5-part-a-the-world-the-tables)
6. [Part B, the event stream](#6-part-b-the-event-stream)
7. [Part C, data for the AI](#7-part-c-data-for-the-ai)
8. [Part D, policies and rules](#8-part-d-policies-and-rules)
9. [Part E, business impact (simulated)](#9-part-e-business-impact-simulated)
10. [Part F, the correct answers ("truth")](#10-part-f-the-correct-answers-truth)
11. [Why these percentages](#11-why-these-percentages)
12. [What is wrong ON PURPOSE and what is wrong FOR REAL](#12-what-is-wrong-on-purpose-and-what-is-wrong-for-real)
13. [How I know the data is reliable](#13-how-i-know-the-data-is-reliable)
14. [What is in git and what is not](#14-what-is-in-git-and-what-is-not)
15. [Honest limitations](#15-honest-limitations)
16. [How to regenerate and read it](#16-how-to-regenerate-and-read-it)

---

## 1. What this is and what it is for

### The case
Clara (a fintech selling corporate cards to companies in Latin America) wants to reach about **50,000 companies a month**
without hiring a new person for every batch of accounts. The case asks me to build a small system that, when information about a
company arrives, decides:

> **What is the next best action for this account, and can we safely automate it?**

The possible actions are: **contact**, **wait**, **enrich data**, **escalate to a person**, **hand the account to a sales
executive (AE)**, or **suppress** (never write to them again).

### Why the data is "the most important part"
A decision system can only be **demonstrated** if it has hard situations to be tested on: a customer who must not be
contacted, the same notice arriving twice, an API that fails, an ambiguous prospect reply. If the data were clean and pretty,
the system would look perfect without ever having been tested. So the data was **designed** to contain each of those situations in
sufficient quantity.

### What is in the repository
A program (the **generator**, in `growth-orchestrator/generator/`) that **manufactures** all the data automatically. There is no
real data about anyone. Every time I run it with the same parameters it produces exactly the same files.

---

## 2. Glossary

| Word | What it means in this project |
|---|---|
| **Account** | A company I want to sell to. E.g. "Group Vamar, Peru". |
| **Contact** | A person inside the account (the finance director, the procurement manager…). An account has 1 to 5. |
| **Event** | A notice that reaches the system: "this account was added to this month's list", "this contact replied", "this contact unsubscribed". It arrives as a *webhook* (an automatic message between systems). |
| **Synthetic** | Invented by the program. No company, person, email or domain exists. |
| **Seed** | Two different meanings, see below. |
| **Deterministic** | The same ingredients always give the same result. No "real" randomness. |
| **AE (Account Executive)** | A human sales executive who owns the most valuable accounts. |
| **SDR** | The person who does the first cold outreach. The goal of the system is to need fewer of them. |
| **ICP** | Ideal customer profile: the kind of company Clara wants (here: 11 or more employees, and not government or nonprofit). |
| **Suppression** | A list of people/companies I must **not** write to (they unsubscribed, complained, etc.). |
| **Enrich** | Query an external service to complete or correct an account's data. |
| **Idempotency** | Processing the same event twice has the same effect as processing it once (no two emails). |
| **Webhook / payload** | An automatic message between systems / its content. |
| **Mock** | A simulation of an external system (email, CRM, calendar) that behaves in a controlled way, including failing. |
| **Golden set** | A small set of curated cases with the correct answer, used to test the system. |
| **Eval** | A test that measures how well the AI does against known correct answers. |
| **LLM** | The AI model that reads text (here: to interpret replies and draft emails). |
| **JSONL** | A file format with one record per line. It opens in any text editor. |
| **SQLite** | A database in a single file, with nothing to install. |
| **Truth (ground truth)** | The correct answer, known in advance and stored separately so I can compare. |

### The two meanings of "seed"
1. **The generator seed (`--seed 42`).** A number. The generator uses "random" numbers, but if I give it the same starting
   number it always produces the same sequence. With `42` the same 50,000 companies come out every time. `43` gives a different
   but equally valid world. It makes results **reproducible**: everyone sees the same data.
2. **The "seed" files (`data/seed/`).** The curated files (test cases, templates, example replies). They were drafted with an AI
   assistant at my request, and **I still need to review them** before presenting (see section 15). They are
   called seeds because part of the big data "grows" from them. They are stored in git because they are small and
   the tests need them; the 390 MB of generated data is not.

---

## 3. Why the data is synthetic

The case allows it explicitly ("you may use mock APIs, synthetic data, SQLite…"). Also:

* Using real companies in an **automated mass-email** system could look like I am contacting them.
* With invented data I can **manufacture rare cases** (for example, 250 accounts where an unsubscribe arrives late) in sufficient
  quantity to test. With real data those cases are hard to find.
* **Safety:** every domain ends in `.example`, a suffix that the internet standard (RFC 2606) reserves for examples and that
  **cannot exist for real**. No invented email can reach a real person.

The 13 real companies that an earlier demo in this repository contained were removed; an automatic test now prevents them from
coming back.

---

## 4. Map: the kinds of data

There are **six families**. Each answers a different question:

| Family | Question it answers | Approx. size | In git? |
|---|---|---|---|
| **A. The world** (tables) | What is each company's state before I start? | ~400k rows | No (regenerated); a 500-account sample yes |
| **B. The event stream** | What notices reach the system during the month? | ~56k events | No; sample yes |
| **C. Data for the AI** | What do I give the AI, and which wrong AI answers must the system reject? | ~1,900 texts + 220 recorded outputs | Yes |
| **D. Policies and rules** | Which business and sending rules apply? | 4 files | Yes |
| **E. Business impact** | How would I measure whether the system creates extra sales? | 100,000 simulated rows | No |
| **F. The correct answers** | What was the right answer in each case? | ~240k rows | No; sample yes |

Golden rule: **families A, B, C and D are what the system "sees"; F is the answer key and the system must never read it** (only
tests do). An automatic test verifies that nothing from the answer key leaks into the other tables.

---

## 5. Part A, the "world": the tables

A snapshot of the state **on October 1, 2026, 16:00 UTC** (inside the send window in every country) (the "as-of" date). Everything that happens afterwards arrives as an
event.

### 5.1 `accounts`: the companies (50,000 rows)
Each has: name, legal name, **domain** (e.g. `vamar.pe.example`), country, industry, employee count, size band, revenue band, CRM
status (`prospect`, `customer`, `churned_customer`, `competitor`), owning AE (if any), how fresh its enrichment is, how many
enrichment attempts it has had, which monthly list it belongs to, and dates.

| Country | Accounts | % |
|---|---|---|
| Mexico | 18,054 | 36.1% |
| Brazil | 9,969 | 19.9% |
| Colombia | 9,967 | 19.9% |
| Chile | 4,962 | 9.9% |
| Argentina | 3,996 | 8.0% |
| Peru | 3,052 | 6.1% |

| Size (employees) | Accounts | % |
|---|---|---|
| 51–200 | 17,605 | 35.2% |
| 11–50 | 15,380 | 30.8% |
| 201–500 | 8,750 | 17.5% |
| 501–1000 | 4,352 | 8.7% |
| 1000+ | 2,221 | 4.4% |
| 1–10 | 1,430 | 2.9% |
| unknown (missing on purpose) | 262 | 0.5% |

CRM status: 88.9% prospects, 8.8% customers, 2.0% churned customers, 0.3% competitors. There are 15 industries (retail & commerce
11.6%, manufacturing 11.5%, technology & software 10.5%, logistics 9.5%, construction & real estate 8.5%…). Enrichment state:
83% complete, 16% partial, 1% stale.

### 5.2 `contacts`: the people (118,944 rows)
Name, email, **email status** (how usable it is), title, function (finance, procurement, operations…), seniority, language.
Every contact reads and writes **English**; names are regional (Spanish and Portuguese names) because the companies are in Latin America.

| Contacts per account | Accounts |
|---|---|
| 0 (on purpose, nobody to write to) | 608 |
| 1 | 14,182 |
| 2 | 14,966 |
| 3 | 10,205 |
| 4 | 6,059 |
| 5 | 3,901 |
| 6 (duplicate accounts that share a contact) | 79 |

**Email status** matters because it decides who can be written to:

| Status | Meaning | % |
|---|---|---|
| `valid` | Verified email: **the only one I can write to** | 79.9% |
| `unverified` | Exists but nobody has verified it | 6.9% |
| `role_based` | Generic mailbox (`finance@…`), not a person | 4.4% |
| `catchall` | The domain accepts anything; unknown if the address exists | 2.6% |
| `missing` | No email | 2.2% |
| `invalid_syntax` | Malformed (`john@@company`) | 2.1% |
| `free_provider` | Personal email (gmail-style), not corporate | 2.0% |

Functions: finance 35.2%, operations 15.0%, executive 14.0%, procurement 11.9%, HR 8.0%, other 8.0%, IT 7.9%.

### 5.3 `opportunities`: deals in progress or closed (9,762)
Stage (`discovery`, `demo`, `proposal`, `negotiation`, `closed_won`, `closed_lost`), amount, owner, dates, loss reason.
1,500 accounts have an **open** opportunity.

### 5.4 `outreach_history`: emails already sent before I start (42,030)
What was sent, to whom, when, at which sequence step (1 to 4) and by whom (`sequence` = automated, `ae` = the executive, `cs` =
customer success). 21,484 accounts have at least one send.

### 5.5 `suppression`: who NOT to write to (2,934)
By contact or by whole domain. Reasons: voluntary unsubscribe 69.6%, hard bounce 15.1%, "do not contact" (DNC) 6.7%, spam
complaint 6.3%, legal hold 2.2%.

### 5.6 `company_facts`: facts about each company (81,017)
Sentences like "announced its expansion to Bogota", with a **source**, the date observed, and whether it is verified. They let the AI
personalize an email **without inventing**. Out of every 100 facts, about 68 are usable; the rest are **traps** (see section 12):

| Kind | n | % |
|---|---|---|
| Clean, usable | 54,987 | 67.9% |
| Old (more than a year) | 14,378 | 17.7% |
| Unverified hypothesis | 7,474 | 9.2% |
| About a **different** company with a similar name | 2,114 | 2.6% |
| Contradicts the company's real size | 2,064 | 2.5% |

### 5.7 `aes` and `ae_calendar`: the executives and their calendars
40 AEs: 38 active, 3 on leave at the snapshot date, 2 deactivated, and **13 at their capacity limit**. Each has a country, time
zone, a **backup AE** and a current load. The calendar has 836 rows (working days × AE) listing free 30-minute slots.

### 5.8 `mock_behavior` and `mock_enrichment`: how the simulated APIs behave
`mock_behavior` (50,000) defines, **per account**, how the external systems will react, always the same way (reproducible):

| System | Behavior | % |
|---|---|---|
| CRM | works | 94.2% |
| | transient error (503), then works | 2.3% |
| | rate-limited (429), then works | 1.5% |
| | **uncertain outcome** (write may or may not have applied) | 1.1% |
| | stale-version conflict (409): state changed since it was read | 1.0% |
| Enrichment | works | 87.3% |
| | hangs once, then works | 4.0% |
| | returns unreadable garbage | 2.9% |
| | rate-limited once | 2.8% |
| | permanent 500 error | 2.0% |
| | **uncertain outcome** (accepted, result stays "pending") | 1.0% |
| Email sending | works | 93.1% |
| | transient error, then works | 3.0% |
| | rate-limited | 1.9% |
| | **uncertain outcome** ("200 OK but status unknown") | 1.2% |
| | hard rejection | 0.8% |
| Calendar | works | 91.8% |
| | slot conflict | 4.0% |
| | timeout, then works | 1.5% |
| | **uncertain outcome** (booking may already exist) | 1.1% |
| | rate-limited | 1.1% |
| | permanent 500 error | 0.5% |

How each system answers and what the orchestrator must do about it is documented in `data/seed/mock_api_contracts.json`.

`mock_enrichment` (2,267) is what the enrichment service would return for accounts that need it: 65% good data, 20% no data, 15%
**contradictory** data.

---

## 6. Part B, the event stream

`events`: about **56,000** notices in arrival order. Each is an envelope with: delivery id, event id, **idempotency key**, type,
account, contact, when it **occurred**, when it **arrived**, and the content.

| Event type | n | % | What it is |
|---|---|---|---|
| `account_targeted` | 52,931 | 94.6% | The account entered the month's list: "decide what to do" |
| `reply_received` | 1,647 | 2.9% | A contact replied to an email |
| `opportunity_stage_changed` | 463 | 0.8% | The CRM moved an opportunity to another stage |
| `unsubscribe_received` | 334 | 0.6% | Someone asked not to receive more |
| `opportunity_created` | 267 | 0.5% | A new opportunity was created |
| `email_bounced` | 181 | 0.3% | An email bounced |
| `meeting_booked` | 70 | 0.1% | Someone booked a meeting |
| `lead_scored` | 66 | 0.1% | **Garbage on purpose**: a type the system does not know |

Notices arrive in **bursts**: four or five times a month, thousands of accounts enter within ~20 minutes (each new list). That
pressures queues and rate limits.

### The "mischief" injected into the stream
The generator starts from a clean stream and injects problems **at explicit rates**. This is the reason for each percentage:

| Problem | How many | % of events | What the system must do |
|---|---|---|---|
| Exact duplicate (same notice re-sent) | 1,570 | 2.8% | Ignore it: a single effect |
| "Semantic" duplicate (new id, same key) | 846 | 1.5% | Ignore it |
| Content duplicate (other source, other key, same content) | 263 | 0.5% | Detect it by content |
| Delayed (arrives 10 minutes to 3 days late) | 1,355 | 2.4% | Process using **when it occurred**, not when it arrived |
| Race (something that happened **before** arrives **after**) | 500 | 0.9% | Apply it, suppress, cancel pending sends |
| **Malformed** (deliberately corrupt) | 500 | 0.9% | Send to a dead-letter queue without touching anything |
| Clean, no problem | 50,925 | 91.0% | Process normally |

The 500 malformed ones are of eight kinds: impossible date (79), null payload (74), no account (73), wrong payload type (71),
unknown event type (66), unsupported schema version (64), account that does not exist (63), and giant text (10).
In total, about 2,300 notices (4.2%) arrived more than an hour after they occurred.

**Race example:** the system receives "this account entered the list", sees it as eligible and schedules an email. 20 hours later
a notice arrives saying "that person unsubscribed **yesterday**". The system must apply it, suppress, and cancel the pending
email. There are 250 accounts with a late unsubscribe and 250 with a late opportunity.

---

## 7. Part C, data for the AI

The AI is used for **two** tasks: (1) **interpret prospect replies** and extract information; (2) **draft a personalized email**
based only on verified facts. Everything else (eligibility, unsubscribes, bounces, routing, send windows) is decided by **rules**,
not by the AI.

### 7.1 Prospect replies
**`reply_seeds.jsonl`** (117 seed texts, drafted with an AI assistant; fictional, not real replies). They are labelled base sentences in 13 categories:

| Category | What it is | Correct action | Count in the stream |
|---|---|---|---|
| `interested` | Wants to talk | Hand to an AE | 220 (14.3%) |
| `not_now` | Maybe later (sometimes with a date) | Wait until that date | 204 (13.3%) |
| `objection` | "We already have a provider / no budget" | A person decides | 179 (11.7%) |
| `out_of_office` | Automatic vacation reply | Wait | 149 (9.7%) |
| `wrong_person` | "That's not me; talk to X" | Find the right person | 142 (9.2%) |
| `ambiguous` | "Interesting, let's see" | A person decides | 128 (8.3%) |
| `unsubscribe` | "Don't write to me anymore" | Suppress | 124 (8.1%) |
| `auto_reply` | "We received your email, ticket #…" | Nothing | 123 (8.0%) |
| `info_request` | Asks for pricing/details | A person answers | 81 (5.3%) |
| `mixed_signals` | "I'm interested, but stop emailing me" | **Suppress** (opt-out wins) | 76 (4.9%) |
| `hostile` | Anger or a legal threat | Suppress + a person | 45 (2.9%) |
| `prompt_injection` | Trap: "ignore your instructions and…" | A person; **never obey** | 39 (2.5%) |
| `empty_or_truncated` | Empty or cut off | A person decides | 26 (1.7%) |

The replies in the stream (1,536 labelled) = base text + automatic variations (greetings, signatures, typos, the original email
quoted with the "reply STOP" footer as realistic noise, legal notices). 35% easy, 48% medium, 14% hard.

The seeds include **qualification information** that the AI must extract only if it is stated: team size, current solution (bank,
Excel, ERP…), timeline in months, countries, pain points and a budget signal. There is a **trap**: "we're a group of 12 companies"
does not mean the team has 12 people.

### 7.2 Eval cases and recorded outputs
* **`eval_cases.jsonl`** (18 cases): 10 reply cases (the "core suite" the case asks for), 4 extended ones and 4 personalization cases.
* **`llm_recordings.jsonl`** (220 recordings): for each case, one correct AI answer and several **defective** ones, each with the
  verdict the system should reach. They let me test the system **without calling any real AI**.

Kinds of defect: cut-off JSON, text instead of JSON, empty output, missing or extra field, invalid value, out-of-range confidence
(1.7), invented quote, **invented referral, date or team size**, obeying an injection, a wrong label with high confidence, an
action that contradicts the label and, for emails, forbidden claims, a missing unsubscribe footer, nonexistent or unusable facts.
Verdicts: `accept`, `accept_with_warning`, `reject_retry`, `reject_escalate`, `escalate_low_confidence`, `override_rule`,
`fallback_generic`.

**The principle:** *the AI proposes, deterministic code disposes.* The action always comes from the **validated label**; whatever the
AI "suggests" is ignored if it does not match. And an explicit unsubscribe in the text forces "suppress" even if the AI says
otherwise.

> **Honest point:** two recordings (`wrong_but_valid_*`) contain an AI answer that is **plausible, well-formed and wrong**. No
> validator can detect those. They are flagged because they show why the AI must run with sampled human review and not alone.

### 7.3 Templates
Four approved emails (the four steps of the sequence). If there are no usable facts, the **generic** template is sent; details are never
invented.

---

## 8. Part D, policies and rules

Four files in `data/seed/` and one document (`data/POLICY.md`) that explains the full policy. **Every number here is an assumption
of mine, made so I could build the data**; they are not Clara's real rules. They change in one place.

| File | What it defines |
|---|---|
| `POLICY.md` | The decision tree (12 rules in priority order) and what to do with each event |
| `send_policy.json` | Send window (Mon–Fri 09:00–18:00 recipient local time), daily cap (5,000), 14 days between sends, max 4 steps, API limits, retries (3 attempts, never retry 4xx errors; if the outcome is uncertain, **check before retrying**), forbidden phrases ("guaranteed approval", "0% commission", "save 30%", comparisons…) and **what the AI may and may NOT decide** |
| `ai_schemas.json` | The exact format the AI must return and the catalogue of validation rules (V001…V012, P001…P014, G001) |
| `funnel_assumptions.json` | Sales-funnel assumptions (section 9) |

### The 12 decision rules (the first that applies wins)
1. Suppressed / competitor → **suppress**. 2. Contradictory state → **person**. 3. Customer → **suppress**. 4. Churned customer →
**person**. 5. Duplicate account → **person**. 6. Open opportunity → **suppress** (don't get in the way). 7. Has an AE → **hand to
the AE**. 8. We lost less than 90 days ago → **wait**. 9. Not ICP → **suppress**. 10. A send in the last 14 days, or 4 sends
with no reply → **wait**. 11. Data missing or stale → **enrich** (if already tried twice → **person**). 12. Otherwise → **contact**
the best contact (finance > procurement > executives > operations…, and within that, the most senior).

---

## 9. Part E, business impact (simulated)

The case asks me to explain how I would know whether the system produces **extra sales** and not just more emails. To illustrate the method:

* **`experiment_assignments`** (50,000): each account is randomly assigned to **control** (the current process with SDRs) or
  **treatment** (the system). 25,001 and 24,999. Assignment is **by domain**: two accounts with the same domain always land in the
  same group (so they do not contaminate each other).
* **`experiment_sim_outcomes`** (50,000): **invented** outcomes for one month under the assumptions in
  `funnel_assumptions.json` (SDR coverage 30% vs 97%, reply rates, deal values…). Every row has `simulated: true`.
* **`data/reports/impact_example.md`**: a worked example with sample sizes, an A/A test (comparing two identical groups to check the
  method does not "discover" differences that do not exist) and guardrails (unsubscribes, complaints, bounces).

> ⚠️ **This is NOT evidence that the system works.** It is a demonstration of the method under assumptions. Before concluding
> anything I would replace the assumptions with Clara's real funnel. One honest finding of the example: even assuming a ~2× effect,
> one month of 50,000 accounts is **not enough** to call the primary metric significant (the interval includes 0).

---

## 10. Part F, the correct answers ("truth")

The `truth/` folder holds the answer key so I can measure:

| File | Rows | What it says |
|---|---|---|
| `truth_accounts` | 50,000 | What the system should decide per account, the reason, the best contact, how long to wait, and which AE to route to |
| `truth_events` | ~56k | What mischief each event carries and how it should be handled |
| `truth_replies` | 1,536 | The correct label, the data to extract, and the **unsafe** actions for each reply |
| `truth_facts` | 81,017 | Whether each fact is a trap and why |

**How do I know this truth is right?** It is computed **twice, independently**: once from the scenario design and once by an
"oracle" (`generator/oracle.py`) that looks only at the tables and decides using the rules. If they disagree on **a single
account**, validation fails. Today they agree on all 50,000.

---

## 11. Why these percentages

This is the most important question. **No percentage comes from Clara's reality.** They are design decisions with two purposes:

### 11.1 Scenario quotas (accounts)
Each account gets a "primary scenario" by **exact quota**, not by chance: if I ask for 8% customers, exactly 4,000 come out. With
pure randomness a rare case (0.5%) might appear 190 or 310 times, or almost never, and could not be tested. With quotas, **every
case has enough examples**.

| Scenario | % | Accounts | What it is for |
|---|---|---|---|
| Clean prospect | 38.0 | 19,000 | Happy path: the answer is "contact" |
| Recent outreach | 14.0 | 7,000 | Test "wait" |
| Has an AE | 10.0 | 5,000 | Test "hand to the AE" |
| Customer | 8.0 | 4,000 | **Never** contact a customer |
| Sequence exhausted | 5.0 | 2,500 | 4 sends with no reply |
| Missing data | 5.0 | 2,500 | Test "enrich" (and the 2-attempt limit) |
| Not ICP | 4.0 | 2,000 | Too small, or government/nonprofit |
| Active opportunity | 3.0 | 1,500 | Don't get in the AE's way |
| Suppressed | 2.5 | 1,250 | Unsubscribes, complaints, DNC, legal hold, competitor |
| One contact suppressed, another available | 2.0 | 1,000 | Pick the **other** contact |
| Churned customer | 2.0 | 1,000 | Win-back is a human call |
| Duplicate account | 2.0 | 1,000 | Same domain, different name |
| Recently lost | 2.0 | 1,000 | 90-day cooldown |
| Contradictory state | 1.5 | 750 | CRM says customer but there is no deal, or the reverse |
| Race: late opportunity | 0.5 | 250 | A notice arrives late |
| Race: late unsubscribe | 0.5 | 250 | The riskiest: an unsubscribe arrives late |

The large percentages resemble a typical prospecting funnel (many clean accounts, quite a few on hold, some customers); the small
ones are **deliberately over-represented** (in reality a late unsubscribe would be far rarer than 0.5%) so I can test them.

### 11.2 Problem rates in the events
Duplicates 3% + 1.5% + 0.5%, delays 2.5%, malformed 1%: orders of magnitude that are **plausible** for real webhooks (provider
retries and delays are common, corruption is rare) and high enough to give hundreds of examples of each.

### 11.3 "Natural" distributions
Country (Mexico 36%, Brazil and Colombia 20%…), sizes and titles are calibrated to look like a prospecting market of mid-sized
companies in Latin America. They are **reasonable, not measured**.

### 11.4 The assumptions worth challenging
ICP threshold of 11 employees · 14 days between sends · 4 sends maximum · 120- and 90-day cooldowns · 90 days to call data "stale" ·
AI confidence threshold 0.75 · cap of 5,000 sends a day · every funnel rate in section 9. They are grouped in
`generator/config.py`, `send_policy.json` and `funnel_assumptions.json`.

---

## 12. What is wrong ON PURPOSE and what is wrong FOR REAL

There are two classes of "wrong", and they should not be mixed.

### 12.1 Intentional errors (the data is dirty to test the system)
| Where | What is wrong | How many | Why |
|---|---|---|---|
| `events` | Corrupt notices (eight kinds) | 500 | Test the dead-letter queue |
| `events` | Duplicate notices | ~2,680 | Test idempotency |
| `events` | Delayed or out-of-order notices | ~1,855 | Test ordering by real time |
| `events` | The `lead_scored` type | 66 | Unknown type |
| `contacts` | Misspelled / missing / generic / personal emails | 12,649 (10.6%) | Don't write to unusable emails |
| `accounts` | 262 with no size or industry; 608 with no contacts | 870 | Test "enrich" |
| `accounts` | The same domain on two accounts | 1,000 | Test duplicates |
| `accounts` | Contradictory CRM | 750 | Test escalation |
| `company_facts` | Old, unverified, about another company, or contradicting | ~26,000 (32%) | Test that the AI does not use bad facts |
| `mock_behavior` | APIs that fail or hang | 4–12% depending on system | Test retries |
| replies | Injections, empty, ambiguous, contradictory | 293 ambiguous and 39 injections | Test the AI |
| replies | Typos and noise (quoted thread, legal notices) | varies | Realism |

All of this is **labelled in the truth**: I know exactly what is wrong and what should happen.

### 12.2 Real limitations (not part of the design)
None of the 27 automatic integrity checks fails today. What I do want to be upfront about:

1. **The policy is a set of assumptions.** The oracle and the generator were built from the same policy, so they prove the data is
   **consistent**, not that the **policy is right** for Clara.
2. **The replies are cleaner than real ones.** 117 drafted base sentences plus automatic variations. Real replies are more
   varied and messier.
3. **The event volume is ~56k, not hundreds of thousands.** Delivery and open events are not pre-generated; the simulator will
   produce them at run time.
4. **The impact is simulated.** See section 9.
5. **The effect of one small month:** with 50,000 accounts a month is only enough to detect large improvements (≥ ~100%) in the
   sales metric.
6. **Synthetic names could coincide by chance with a real company.** Very unlikely, and the domains are `.example`, but I cannot
   guarantee it 100%.
7. **AEs at capacity:** 13 of 38 active AEs; deliberate, but the exact load distribution is invented.
8. **Fixed time zones** (no daylight saving), valid for October 2026.

---

## 13. How I know the data is reliable

**27 automatic checks** over the ~390 MB (`data_profile.md`) and **178 tests** (`tests/`). The most important:

| What is verified | Why it matters |
|---|---|
| The same `seed` produces the same files (identical hash) | Reproducibility |
| The quotas are exact | Every case has examples |
| Nothing points to something that does not exist (accounts, contacts, AEs) | Integrity |
| Every domain ends in `.example` | Safety |
| The independent oracle agrees with the truth on all **50,000** accounts | The truth does not contradict itself |
| No truth label leaks into the visible tables | The test is fair |
| The 220 recorded AI outputs receive the expected verdict from a reference validator | The defects are real |
| Every annotation in the 117 seeds is supported by its text | The AI cannot just "guess" |
| Customers, suppressed, duplicate and conflicted accounts are **never** "contact" | Business safety |
| Accounts sharing a domain land in the same experiment arm | No contamination |
| Every event type is actually generated | Nothing silently disappears |
| No name of the earlier real companies appears anywhere in the repo | Synthetic data only |

Whenever a test failed during the build, the fault turned out to be in the data code and was **fixed there** (for example, the first A/A test used a
statistical approximation that is not valid with few events; it now uses an exact test; and after translating everything to
English a test caught that a label comparison was still in the old language and had silently removed the `meeting_booked`
events).

---

## 14. What is in git and what is not

Everything is on the `main` branch (the branch Netlify publishes).
Everything lives in `growth-orchestrator/`; the web page is `public/index.html` (+ `public/data/`) and its function `netlify/functions/orchestrator-llm.mjs`.

```
growth-orchestrator/
├── generator/            ← the program that manufactures everything (Python, nothing to install)
├── orchestrator/         ← the system: engine, rules, AI, executor, mocks, evals, CLI
├── docs/                 ← decision log, AI notes, measurement plan, production thinking
├── evals/results/        ← AI eval results
├── tests/                ← automatic tests (data, rules, engine, AI, web)
├── prompts/              ← the prompt for drafting more replies with an AI
├── data/
│   ├── README.md · POLICY.md · TRACEABILITY.md · DATA_REPORT.md (this file)
│   ├── reports/          ← data_profile.md (figures) · impact_example.md (simulation)
│   ├── seed/             ← IN GIT (~3.7 MB, review pending): seeds, golden set, evals, templates,
│   │   │                    policies, recorded AI outputs, demo, and sample/ (500 full accounts)
│   └── generated/        ← NOT IN GIT (~390 MB, regenerated): the full world of 50,000 accounts
│       ├── *.jsonl and growth.sqlite    (what the system sees)
│       └── truth/                       (the truth: tests only)
```

---

## 15. Honest limitations

* The system (orchestrator, eval runner, architecture diagram, decision log) is built on top of this data; see the
  project README. Its integrations are mocks and it never sends real email.
* The impact figures are not evidence (section 9).
* **Review pending (mine):** the 117 reply seeds, the 89 golden scenarios and the 220 recorded AI outputs were drafted with an AI
  assistant. I have not yet read them line by line. Before the presentation I need to review them, and I should not describe them as
  hand-written or as reviewed until I have.
* Validating the policy needs someone from Clara's business side.
* To measure how realistic the replies are, the ideal would be to test with 10 to 20 anonymized real replies.

---

## 16. Where the files are, and how to regenerate and read them

**Important:** the full 50,000-account data (~390 MB) is **not stored in git**. It lives only on the machine that generated it. To
analyze it I either (a) open the small sample that is in git, or (b) regenerate the full data on my own computer in about a minute.
Step-by-step instructions, ready-made queries and a pandas snippet are in **`data/HOW_TO_ANALYZE.md`**.

| What I want | Where it is | In git? |
|---|---|---|
| A quick look in Excel (500 accounts, every table) | `growth-orchestrator/data/seed/sample/csv/*.csv` | Yes |
| Same sample as JSONL | `growth-orchestrator/data/seed/sample/*.jsonl` | Yes |
| The curated cases, templates, policies, AI recordings | `growth-orchestrator/data/seed/` | Yes |
| The distributions and validation checks | `growth-orchestrator/data/reports/data_profile.md` | Yes |
| **The full data**, JSONL | `growth-orchestrator/data/generated/*.jsonl` after running the generator | No |
| **The full data**, one SQLite file | `growth-orchestrator/data/generated/growth.sqlite` | No |
| **The full data**, CSV for Excel | `growth-orchestrator/data/generated/csv/*.csv` | No |
| The answer key (truth) | `growth-orchestrator/data/generated/truth/` | No |

Repository: `apogeoconsara/portafolioclarachallenge` (branch `main`).

From `growth-orchestrator/`:

```bash
python3 -m generator all --seed 42 --n 50000     # ~1.5 min. Generates everything (incl. CSV) and validates
python3 -m unittest discover -s tests -t .       # runs the 178 tests
```

To **look** at the data without programming: open any `.csv` in Excel, or any `.jsonl` in a text editor (one row per line), or
`growth.sqlite` in any SQLite viewer, and read `data/reports/data_profile.md` for the distributions. To understand a single case,
open `data/seed/golden_scenarios.jsonl` (89 cases with the correct answer) or `data/seed/demo_flows.json` (the demo script).

To change the size: `--n 5000` (fast, for trying things) or `--n 500000` (ten times the volume).
