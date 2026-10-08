# Measurement plan: does the orchestrator create incremental qualified pipeline?

**Question.** Versus the current SDR process, does the orchestrator increase SQL-accepted pipeline per targeted account
without hurting deliverability, compliance or AE trust? More messages or more replies are not the goal.

## Funnel

```
targeted → eligible → contacted → delivered → replied → positive reply → SQL accepted by an AE → qualified pipeline (USD)
```

Every stage is counted per 1,000 **targeted** accounts, not per contacted account, so a system that contacts more
accounts cannot look better just by changing the denominator. Accounts the rules block (customers, active deals,
suppressed, recently contacted) stay in the denominator of both arms.

## Experiment

Two groups answer the main question, and a second step answers the AI question. Keeping them apart is deliberate: the
system's judgment (who to contact, who to leave alone, how fast, which exec) is mostly rules, and the AI has to prove its own
value on top of that instead of riding on it.

**Step 1: does the system create qualified pipeline? (A against B)**

- **Unit:** targeted account; cluster = email domain (duplicate accounts share a group, no contamination through a shared
  inbox). AEs do not see group labels.
- **Assignment:** stratified by country × employee band × prior touch, deterministic hash within stratum
  (`data/generated/experiment_assignments.jsonl`), 50/50.
- **Group A (control):** today's process. **Group B (treatment):** the orchestrator, with people on the review queue and on the
  approval of outreach emails (first emails and follow-ups).
- **Rollout:** group B does not start at full volume. It starts small (for example 10%, 25%, 50%, then 100% of planned volume)
  and each increase passes a gate that checks the guardrails (see Stop rules). The schedule is a design choice, to be set with
  Clara's real deliverability data.
- **Duration:** 8 weeks of targeting + 60-day attribution window; intention-to-treat on all targeted accounts.

**Step 2: does the AI earn its place? (B1 against B2)**

The AI does two small jobs: it reads replies (label and extraction) and it writes one opening line from verified facts. Nothing
else uses AI. Once step 1 shows that group B is safe, group B is split in two:

- **B1, no AI:** the same rules, tiers and human approval, with the approved template and replies read by a person.
- **B2, with AI:** the AI reads replies and writes the opening line.

B2 changes two things at once (reading and writing), so a win says the AI helps, not yet which job does the work; a later split
would separate them. Splitting B also makes each group smaller, so detection takes longer (see the power table on the page):
a point to settle with Clara's real volumes.

**The rule for the AI.** It stays in a task only if B2 beats B1 on qualified pipeline per 1,000 targeted accounts **and** on
cost per qualified lead, without breaking a guardrail, and with zero unsafe AI actions. Reviewer corrections of its labels are
tracked so that a reviewer's workload does not simply replace the SDR's. If it does not win, it is switched off there and the
rules keep working. The decision log lists the AI as
unproven for personalisation today: only one of the two live-eval cases where personalisation was possible was personalised,
and nothing yet ties it to pipeline.

Optional: a small holdout of eligible accounts that nobody contacts would show how much pipeline arrives with no outreach at
all, which is what makes the lift incremental rather than merely larger.

## Metrics

| type | metric | threshold |
|---|---|---|
| **primary** | qualified pipeline USD (SQL accepted by an AE) created within 60 days, per 1,000 targeted accounts | report the CI, not a point estimate |
| leading (readable in weeks) | positive-reply rate · meetings booked per 1,000 targeted · hours to first touch · share of eligible accounts reached | — |
| quality (so volume cannot fake it) | qualified pipeline per company contacted · per SDR hour · cost per qualified lead · AE SQL acceptance | reported next to the primary metric |
| AI value (B2 against B1) | qualified pipeline and cost per qualified lead · hours to a correct next step on a reply · reviewer corrections of AI labels | B2 must win, see the rule above |
| guardrail | unsubscribe rate (read with its 95% interval) | ≤ 0.6% |
| guardrail | spam complaint rate | ≤ 0.1% |
| guardrail | hard bounce rate | ≤ 3% |
| guardrail | AE SQL acceptance | ≥ 95% of control |
| guardrail | policy violations (contacting a suppressed, customer or active-deal account) | 0 |
| guardrail | unsafe AI actions | 0 |
| operational | review-queue age, dead-letter volume | SLA set with the team |

**Stop rules.** Each week a gate reads every guardrail with its 95% interval, not a single number, because counts are small:

- **GO:** every interval is below its limit, and group B's volume goes up to the next step.
- **HOLD:** an interval touches a limit, so the evidence says neither way. Volume stays where it is and measurement continues.
- **PAUSE:** an interval is entirely above a limit, or the limit is broken two weeks in a row. A policy violation or an unsafe AI
  action pauses group B at once.

The same idea would run inside the system in production: a rolling complaint rate that warns at 80% of the limit, stops
releasing approved emails at the limit and asks a person to look. Thresholds would be set with Clara's real deliverability data.

**Sanity checks.** A/A test on the assignment (exact conditional Poisson test, in the tests), sample-ratio mismatch,
pre-period balance on strata, novelty check (weeks 1–2 vs 5–8).

## What the simulation says (SIMULATED, not evidence)

Only step 1's two groups are simulated, from the assumptions in `data/seed/funnel_assumptions.json`
(`data/reports/impact_example.md`). Group A is today's process and group B is the full system; there is no B1 and no B2 yet.

- **Pipeline per 1,000 targeted accounts:** USD 22,736 (A) against 42,709 (B). The 95% interval for the difference is −667 to
  43,885. Even with an assumed ~2× effect, the interval includes 0 after one month of 50k accounts: pipeline is sparse and
  heavy-tailed. So I would decide on the leading metrics and guardrails at week 4, confirm on pipeline at the end of the
  attribution window, and report the interval.
- **Where the total comes from:** coverage. The share of eligible accounts reached goes from 30% to 97%, so companies contacted per
  1,000 go from 141 to 401, while the reply rate per email is assumed lower for B (3.4% against 4.0%) and so is conversion to a
  qualified lead. Per company contacted, B yields less pipeline (about USD 107 against 161). This is deliberately conservative:
  the assumptions give AI-written emails no advantage.
- **Where the system clearly wins in the simulation:** efficiency and safety. Pipeline per SDR hour is about 8 times higher, the
  median first touch is 2 hours instead of 96, and 0 emails reach ineligible companies against 460.
- **What is not in these numbers:** the AI's own contribution. That is what step 2 measures. If the real lift is only volume,
  the case for the system rests on efficiency and safety, and the data will say so.
- **Guardrails:** the spam-complaint rate reads 0.11% against a 0.10% limit (11 complaints in 10,014 emails), but its 95%
  interval is 0.06% to 0.20%, so the gate says HOLD, not breach. Both groups are assumed to have the same complaint chance, so the
  gap is most likely chance. It does flag something real: the assumed rate (0.08%) already sits at 80% of the limit, so there is
  little room to grow volume, and that is the first thing a real run would have to watch.

**Where a lift would come from.** Decompose it into coverage (more eligible accounts reached), speed (hours to first touch) and
per-touch quality (reply rate and conversion). Coverage and speed are the system's judgment and rules; per-touch quality is where
the AI would show up. The data will show which component moved.

All numbers above are assumptions to be replaced with Clara's measured baseline.
