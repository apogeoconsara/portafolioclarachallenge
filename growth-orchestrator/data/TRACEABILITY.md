# Traceability: challenge requirement → data → tests

Every backticked path, golden id (`G040`) and recording id (`EV-…:variant`) below is checked by
`tests/test_alignment.py::test_traceability_references_exist`, so this table cannot drift from the repo.
Paths are relative to `growth-orchestrator/`. The system built on this data is described in the project README.

## What to build

| Requirement | Data that exercises it | Checked by |
|---|---|---|
| 1. Event / webhook trigger | `data/generated/events.jsonl` (also `data/seed/sample/events.jsonl`): 7 event types in a webhook envelope, weekly bursts | `tests/test_data.py` integrity checks |
| 2. Persistent account / contact state | `accounts`, `contacts`, `opportunities`, `outreach_history`, `suppression` in `data/generated/growth.sqlite` | `tests/test_data.py::test_sqlite_matches_jsonl` |
| 3. Eligibility and next-best-action | `data/POLICY.md`, `generator/oracle.py`, goldens `G001`…`G034`, `truth/truth_accounts.jsonl` (50,000 labelled accounts) | oracle agrees with the scenario label for every account |
| 4. At least one meaningful LLM capability | **Both**: reply interpretation + qualification extraction (`data/seed/reply_seeds.jsonl`, `data/seed/eval_cases.jsonl`) and grounded personalization (`data/generated/company_facts.jsonl`, `data/seed/outreach_templates.jsonl`) | `tests/test_alignment.py::test_every_seed_annotation_is_supported_by_its_text` |
| 5. Structured, validated AI output | `data/seed/ai_schemas.json`, `generator/ai_ref.py`, 220 recorded model outputs in `data/seed/llm_recordings.jsonl` (good + defective) | `tests/test_alignment.py::test_recordings_match_the_reference_validator` |
| 6. External action / mock integration | the four mock systems (CRM, enrichment, outreach, calendar): `data/seed/mock_api_contracts.json`, `data/generated/mock_behavior.jsonl`, `data/generated/mock_enrichment.jsonl`, `data/seed/send_policy.json` (api limits), `data/generated/ae_calendar.jsonl` | `tests/test_data.py` |
| 7. Duplicate / idempotency protection | exact, semantic and content duplicates (`truth_events.jsonl`); goldens `G040`, `G041`, `G042`, `G053` | `tests/test_data.py` |
| 8. Realistic failure / retry scenario | goldens `G070`…`G078` and `G110`…`G117` (CRM, calendar, enrichment); `data/seed/send_policy.json` retry policy; mock behaviours (timeout, 429, 5xx, uncertain outcome, malformed body) | `tests/test_data.py` |
| 9. Automated tests | `tests/test_data.py`, `tests/test_alignment.py`, `tests/test_scenario_coverage.py` (one test per Scenario sentence) | `python -m unittest discover -s tests -t .` |
| 10. AI eval, ~6–10 cases | `data/seed/eval_cases.jsonl`: 10 core reply cases + 4 grounding cases (+ 4 extended) | `tests/test_data.py::ReplyCorpus` |

## Demo must include

| Required in the demo | Flow in `data/seed/demo_flows.json` | Golden / recordings |
|---|---|---|
| a successful flow | `D1` | `G080`, `EV-G080:good` |
| a duplicate event | `D2` | `G040`, `G041`, `G042` |
| a failure scenario | `D3` | `G070`, `G072`, `G075` |
| an ambiguous or unsafe AI case | `D4` | `G064`, `G065`, `G067`, `EV-R-MIX-01:overconfident_wrong_label`, `EV-R-INJ-01:obeys_injection`, `EV-R-AMB-03:wrong_but_valid_overconfident` |
| (extra) malformed AI output | `D5` | `EV-R-NOW-01:truncated_json`, `EV-R-NOW-01:date_invented` |
| (extra) out-of-order race | `D6` | `G047`, `G048` |

## Scenario paragraph

| Statement | Data |
|---|---|
| accounts may have **multiple contacts** | 1–5 per account (avg 2.4); ranking rule in `data/POLICY.md`; `G002`, `G015` |
| already **customers** | `customer` 8%, `churned_customer` 2%; `G003`, `G005` |
| **active opportunities** | `active_opportunity` 3%; `G006` |
| **recent outreach** | `recent_outreach` 14%, `max_touches_no_reply` 5%; `G008`, `G009`, `G010`, `G011`, `G012` |
| an **assigned AE** | `ae_assigned` 10%; AE capacity, leave and backups in `data/generated/aes.jsonl`; routing `G100`…`G104` |
| **suppression rules** | contact/domain-level, 6 reasons; `G013`, `G014`, `G015`, `G016`, `G017` |
| events **duplicated, delayed, out of order** | perturbation rates in `data/README.md`; `G047`, `G048`, `G049`, `G054` |
| APIs **fail, rate-limit, uncertain outcome** | all four systems (CRM, enrichment, outreach, calendar): `data/seed/mock_api_contracts.json`, `mock_behavior.jsonl`; `G070`, `G071`, `G072`, `G073`, `G074`, `G075`, `G076`, `G110`, `G111`, `G112`, `G114`, `G115`, `G117` |
| AI decisions **incomplete, malformed, unsupported** | `data/seed/llm_recordings.jsonl`: 106 invalid-structure, 29 unsupported-by-text, 36 draft violations |
| **~50,000 companies / month** | `python3 -m generator build --n 50000` (50,000 accounts, ~56k events) |
| calendar systems | `data/generated/ae_calendar.jsonl`; `G078`, `G114`, `G115`, `G116` |
| single outreach channel | email only; `data/seed/send_policy.json`, `data/seed/outreach_templates.jsonl` (English, steps 1–4) |

## AI section: be prepared to explain

| Question | Where the data answers it |
|---|---|
| why AI for that decision | replies are free text (`data/generated/events.jsonl` `reply_received`); rules cannot parse them |
| what AI may / may not decide | `data/seed/send_policy.json` → `ai_autonomy` |
| how output is validated | `data/seed/ai_schemas.json` (rules `V001`…`V012`, `P001`…`P014`, guard `G001`) and `generator/ai_ref.py` |
| ambiguity / low confidence | recording `EV-R-AMB-03:good` (valid output, confidence 0.58, so it escalates), `G067`; threshold 0.75 |
| autonomy prerequisites | `data/seed/send_policy.json` → `human_sampling`; honest limit shown by `EV-R-AMB-03:wrong_but_valid_overconfident` and `EV-R-INT-18:wrong_but_valid_team_size` |
| where AI is deliberately NOT used | unsubscribe and bounce events `G050`, `G051`, `G052`; eligibility `G001`…`G034`; AE routing `G100`…`G104`; send windows `G090`…`G093` |

## Business impact

| Ask | Data |
|---|---|
| funnel | `data/seed/funnel_assumptions.json` (stages, rates, unit economics: ASSUMPTIONS) |
| experiment vs current process | `data/generated/experiment_assignments.jsonl` (stratified, domain-clustered 50/50), design in `data/seed/funnel_assumptions.json` |
| primary metric + guardrails | `data/seed/funnel_assumptions.json`; worked example on simulated outcomes in `data/reports/impact_example.md` |

## Presentation

| Ask | Data |
|---|---|
| "we may change one assumption" | `assumption_drills` in `data/seed/demo_flows.json`; policy numbers are config in `generator/config.py` and `data/seed/send_policy.json` |

## The system (built)

| Ask | Where |
|---|---|
| event → state → decision → AI/rules → action → audit | `orchestrator/engine.py`, tests in `tests/test_engine.py` |
| AI eval suite and results | `orchestrator/evals.py`, `evals/results/` |
| architecture diagram, README with decisions | `README.md` |
| decision log, AI autonomy, measurement plan, production thinking | `docs/` |
| demo | the Netlify site's home page and `python -m orchestrator demo` |
