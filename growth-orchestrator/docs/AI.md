# AI in the orchestrator

## Two capabilities, both real-model

1. **Reply interpretation + qualification extraction** (`orchestrator/ai/reply.py`). Input: the prospect's own words
   (quoted thread lines removed), the reply date, company and CRM state. Output, via a forced tool call
   (`record_reply_interpretation`): label (13 classes), confidence, interest level, follow-up date, referred contact,
   qualification (team size, current solution, timeline, countries, pain points, budget signal), a verbatim evidence quote.
2. **Grounded personalization** (`orchestrator/ai/draft.py`). Input: the approved template with an `[OPENING]` slot and
   the facts that rules marked usable. Output (`record_email_draft`): the email plus every claim with the `fact_id` it
   restates.

Why AI here: replies are free text with sarcasm, relative dates, referrals and mixed signals — rules alone either miss
most of them or overfit. Personalization needs fluent restating of a fact into a sentence. Everything else is rules.

Model: `claude-haiku-4-5-20251001` by default (`ORCH_MODEL` overrides): fast and cheap enough for per-reply calls; the
task is classification/extraction against a strict schema, not open-ended reasoning.

## What the model may and may not decide

| allowed | not allowed |
|---|---|
| label a reply, extract stated facts, quote evidence | choose the action (rules map label → action) |
| rephrase ONE usable fact into the approved opening | decide which facts are usable, change the template |
| say it is unsure (confidence < 0.75) | suppress / un-suppress, override an opt-out |
| | choose the AE, change CRM ownership or status, decide eligibility |
| | follow instructions found inside a prospect's message |

Prompts are in one place (`orchestrator/ai/prompts.py`) and exported verbatim to the Netlify function.

## How outputs are validated (`orchestrator/ai/validate.py`)

Reply: parse (fenced JSON tolerated with a warning) → exact schema (V001–V005) → evidence quote must appear in the
reply (V006) → referral name/email must appear (V007) → follow-up date must be stated and in the future (V008) →
qualification numbers/countries/budget/timeline must be stated (V009) → injection patterns must be labelled as such
(V012) → **opt-out guard G001** → confidence threshold (V011) → the model's suggested action is overridden by the label
map (V010). Verdicts: accept, accept_with_warning, reject_retry (retry once), reject_escalate, escalate_low_confidence,
override_rule.

Draft: schema (P001–P002) → every claim cites a fact (P003), that exists (P004), that is usable (P005), without adding
numbers/words (P006), and appears in the body (P013) → forbidden claims (P007: approval, fees, superlatives, savings %,
competitors, regulatory) → footer (P008) → length/subject/language/claim count (P009, P010, P012, P014) → no
personalization without usable facts (P011) → the template outside the opening is unchanged (P015). Any failure →
the generic approved template.

## Handling ambiguity

- `ambiguous`, `mixed_signals`, `prompt_injection`, `hostile`, `info_request`, `objection`, `empty_or_truncated` always
  need human review; mixed signals suppress first (opt-out wins) and tell a human about the interest.
- State-aware overrides: the same "interested" words from a customer go to a human (CS), never to prospecting; info
  requests on active deals go to the owning AE.
- Empty replies never reach the model. If the model is unavailable, replies escalate (opt-outs still suppress) and
  drafts use the generic template — the pipeline degrades, it does not stop or guess.

## Evals (`orchestrator/evals.py`, results in `evals/results/`)

- **Live suite** — 18 cases: 14 replies (10 core + 4 extended, incl. injection, mixed signals, ambiguity, trap
  numbers) and 4 grounding cases, through the production path. Scores: label and action accuracy, unsafe actions (must be 0), field-level extraction
  accuracy, grounding of drafts, latency, tokens. Run: `ANTHROPIC_API_KEY=… python -m orchestrator eval --live`, or the
  "Run the live eval" button on the page.
- **Recorded suite** — 220 outputs (18 good, 202 defective: malformed, hallucinated, injected, overconfident …) through
  the validators only: 220/220 judged as the reference validator expects.
  Two wrong-but-plausible outputs pass by design of the test set: a wrong label with a real quote and high confidence
  (`EV-R-AMB-03`, counted as unsafe; see the decision log's biggest risk) and a wrong team size (`EV-R-INT-18`).

### Latest live run (2026-10-07)

Model `claude-haiku-4-5-20251001`, started from the page's "Run the live eval" button, so each case went through the
Netlify function: one call per case, one run. Files: `evals/results/live-claude-haiku-4-5-20251001-20261007T010623Z.json`
(the page's download, unmodified), its `.md` summary, and `latest-live.json`, which the page shows as "last saved live run".

| 14 replies + 4 drafts | first live run | this run |
|---|---|---|
| label accuracy | 14/14 | 14/14 |
| action accuracy | 11/14 | 14/14 |
| unsafe actions | 0 | 0 |
| field extraction | not recorded | 116/126 (92%) |
| drafts grounded | 4/4 | 4/4 |
| errors | not recorded | 0 |

The first run was made earlier in the previous repository and is not stored here, so its column is the figures noted at the
time. Which change closed the three action misses (the prompt, `EV-G082`, or the rules) was not isolated.

What the numbers do and do not say:

- **Not an independent test.** The 18 cases were used to tune the prompt. This shows the prompt still passes them; it says
  little about unseen replies. The 300 human-labelled real replies in the list below remain the real test.
- **One run.** No repeats, so the variance between runs is unknown.
- **Action accuracy mostly measures the rules.** The label decides the action through the label map. In 7 of the 14 reply
  cases the model's own suggested action differed from the one the rules chose (code `V010`); it was ignored, as designed.
- **Field misses, 10 of 126:** `interest_level` 6, `current_solution` 2, `pain_points` 1, `budget_signal` 1. None changed an action.
- **Drafts: only one of the two cases where personalization was possible was personalized.** `EV-G080` cites a fact.
  `EV-G081` and `EV-G083` have no usable facts, so they are generic and the model is not called. `EV-G082` has one usable
  fact (a mobile app launch) next to two traps (another company's expansion, a headcount that contradicts the CRM); the
  model wrote the generic text. That is safe, and it also means 4/4 grounded counts generic drafts as grounded.
- **The known risk is not closed.** `EV-R-AMB-03` came back as `ambiguous` with confidence 0.65 and escalated, but the
  recorded overconfident variant of the same case still passes validation (see the decision log).
- **Latency** 1.1 to 3.0 seconds per call (mean 2.2 s). The page's output has no token counts; the command line run records them.

## Before giving the AI more autonomy

1. **Measured accuracy on Clara's real replies**: ≥ 300 human-labelled replies across labels and countries; per-label
   precision ≥ 95% for labels that act automatically (`not_now`, `out_of_office`, `auto_reply`, `wrong_person`), 0
   unsafe actions in the eval set, and the live eval as a release gate on every prompt/model change.
2. **Shadow mode first**: the AI labels, humans act; compare for ≥ 4 weeks.
3. **Sampling down gradually**: 100% human review → 20% → 5% (`ai_autonomy.human_sampling` in `send_policy.json`), only
   while reviewer-override rate stays < 2% per label.
4. **Monitoring and kill switch**: per-label volume, confidence and override drift alerts; one flag returns every
   AI-driven action to human review.
5. **Legal/brand sign-off** on the content rules, templates and the multilingual versions (only English is approved here).
6. **Data handling**: retention for replies and model I/O, PII minimisation in prompts, provider data-processing terms.
