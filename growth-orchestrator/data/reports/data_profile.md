# Data profile

seed `42` · accounts `50,000` · as-of `2026-10-01T16:00:00+00:00` · python `3.13.16`  
determinism hash: `5c597fa626c066755c88629607893bf1`

## Volumes

| file | rows |
|---|---|
| aes.jsonl | 40 |
| ae_calendar.jsonl | 836 |
| accounts.jsonl | 50,000 |
| contacts.jsonl | 118,944 |
| opportunities.jsonl | 9,762 |
| outreach_history.jsonl | 42,030 |
| suppression.jsonl | 2,934 |
| company_facts.jsonl | 81,017 |
| mock_behavior.jsonl | 50,000 |
| mock_enrichment.jsonl | 2,267 |
| events.jsonl | 55,959 |
| experiment_assignments.jsonl | 50,000 |
| experiment_sim_outcomes.jsonl | 50,000 |
| truth/truth_accounts.jsonl | 50,000 |
| truth/truth_events.jsonl | 55,959 |
| truth/truth_facts.jsonl | 81,017 |
| truth/truth_replies.jsonl | 1,536 |

## Validation checks

|  | check | detail |
|---|---|---|
| PASS | account ids unique |  |
| PASS | contact ids unique |  |
| PASS | delivery ids unique |  |
| PASS | FK contacts -> accounts |  |
| PASS | FK opportunities -> accounts |  |
| PASS | FK touches -> contacts |  |
| PASS | FK suppression -> accounts |  |
| PASS | FK facts -> accounts |  |
| PASS | valid events reference existing accounts |  |
| PASS | events sorted by received_at (ingestion order) |  |
| PASS | truth_events aligned 1:1 with events |  |
| PASS | every company domain is reserved (.example) |  |
| PASS | every email domain is reserved (.example) or malformed on purpose |  |
| PASS | touches sent before the snapshot |  |
| PASS | replies reference a real earlier touch | 0 bad |
| PASS | exact/semantic duplicates reuse the original idempotency key |  |
| PASS | independent oracle agrees with scenario truth for every account | 0 mismatches |
| PASS | AE backups exist, share the country and differ from the AE |  |
| PASS | account owners exist as AEs |  |
| PASS | account handoffs go to an available owner or its backup | 0 bad |
| PASS | reply handoffs respect availability, language and capacity | 0 bad |
| PASS | calendar: weekdays only, nobody inactive, no slots while on leave |  |
| PASS | every account has an arm |  |
| PASS | accounts sharing a domain share an arm (no contamination) |  |
| PASS | arms balanced overall (±1.5 pts) | treatment share 0.500 |
| PASS | simulated outcomes are flagged and funnel-consistent |  |
| PASS | every reply event has a label |  |

## Scenario quotas (exact by construction)

| scenario | target | accounts | actual |
|---|---|---|---|
| clean_prospect | 38.0% | 19000 | 38.00% |
| partial_contact_suppression | 2.0% | 1000 | 2.00% |
| race_late_opportunity | 0.5% | 250 | 0.50% |
| race_late_unsubscribe | 0.5% | 250 | 0.50% |
| customer | 8.0% | 4000 | 8.00% |
| churned_customer | 2.0% | 1000 | 2.00% |
| active_opportunity | 3.0% | 1500 | 3.00% |
| ae_assigned | 10.0% | 5000 | 10.00% |
| recent_outreach | 14.0% | 7000 | 14.00% |
| max_touches_no_reply | 5.0% | 2500 | 5.00% |
| suppressed | 2.5% | 1250 | 2.50% |
| missing_data | 5.0% | 2500 | 5.00% |
| duplicate_domain | 2.0% | 1000 | 2.00% |
| low_icp | 4.0% | 2000 | 4.00% |
| conflict_state | 1.5% | 750 | 1.50% |
| closed_lost_recent | 2.0% | 1000 | 2.00% |

## Coverage matrix: scenario × expected action on `account_targeted`

| scenario | contact | enrich | escalate_human | handoff_ae | suppress | wait |
|---|---|---|---|---|---|---|
| clean_prospect | 19000 |  |  |  |  |  |
| partial_contact_suppression | 1000 |  |  |  |  |  |
| race_late_opportunity | 250 |  |  |  |  |  |
| race_late_unsubscribe | 250 |  |  |  |  |  |
| customer |  |  |  |  | 4000 |  |
| churned_customer |  |  | 1000 |  |  |  |
| active_opportunity |  |  |  |  | 1500 |  |
| ae_assigned |  |  |  | 5000 |  |  |
| recent_outreach |  |  |  |  |  | 7000 |
| max_touches_no_reply |  |  |  |  |  | 2500 |
| suppressed |  |  |  |  | 1250 |  |
| missing_data |  | 2267 | 233 |  |  |  |
| duplicate_domain |  |  | 1000 |  |  |  |
| low_icp |  |  |  |  | 2000 |  |
| conflict_state |  |  | 750 |  |  |  |
| closed_lost_recent |  |  |  |  |  | 1000 |

## Sub-scenarios

| sub-scenario | accounts |
|---|---|
| conflict_state/customer_without_won_opp | 382 |
| conflict_state/prospect_with_won_opp | 368 |
| low_icp/gov_ngo | 570 |
| low_icp/tiny | 1430 |
| missing_data/all_invalid | 999 |
| missing_data/missing_firmo | 262 |
| missing_data/no_contacts | 608 |
| missing_data/stale | 502 |
| missing_data/unverified_only | 129 |
| suppressed/competitor | 169 |
| suppressed/complaint | 186 |
| suppressed/dnc | 198 |
| suppressed/hard_bounce_all | 189 |
| suppressed/legal | 66 |
| suppressed/unsub_all | 442 |

## Reason codes

| reason | accounts | share |
|---|---|---|
| ELIGIBLE | 20500 | 41.0% |
| RECENT_OUTREACH | 7000 | 14.0% |
| AE_ASSIGNED | 5000 | 10.0% |
| CUSTOMER | 4000 | 8.0% |
| SEQUENCE_EXHAUSTED | 2500 | 5.0% |
| NOT_ICP | 2000 | 4.0% |
| ACTIVE_OPPORTUNITY | 1500 | 3.0% |
| CLOSED_LOST_COOLDOWN | 1000 | 2.0% |
| CHURNED_CUSTOMER | 1000 | 2.0% |
| DUPLICATE_ACCOUNT | 1000 | 2.0% |
| NO_VALID_EMAIL | 893 | 1.8% |
| STATE_CONFLICT | 750 | 1.5% |
| NO_CONTACTS | 558 | 1.1% |
| STALE_ENRICHMENT | 459 | 0.9% |
| UNSUBSCRIBED | 442 | 0.9% |
| MISSING_FIRMOGRAPHICS | 241 | 0.5% |
| ENRICHMENT_EXHAUSTED | 233 | 0.5% |
| DNC | 198 | 0.4% |
| HARD_BOUNCE | 189 | 0.4% |
| SPAM_COMPLAINT | 186 | 0.4% |
| COMPETITOR | 169 | 0.3% |
| UNVERIFIED_EMAIL_ONLY | 116 | 0.2% |
| LEGAL_HOLD | 66 | 0.1% |

## Accounts

**Country**

| country | n | share |
|---|---|---|
| MX | 18054 | 36.1% |
| BR | 9969 | 19.9% |
| CO | 9967 | 19.9% |
| CL | 4962 | 9.9% |
| AR | 3996 | 8.0% |
| PE | 3052 | 6.1% |

**Employee band**

| band | n | share |
|---|---|---|
| 51-200 | 17605 | 35.2% |
| 11-50 | 15380 | 30.8% |
| 201-500 | 8750 | 17.5% |
| 501-1000 | 4352 | 8.7% |
| 1000+ | 2221 | 4.4% |
| 1-10 | 1430 | 2.9% |
| None | 262 | 0.5% |

**Industry (top 8)**

| industry | n | share |
|---|---|---|
| Retail & Commerce | 5793 | 11.6% |
| Manufacturing | 5761 | 11.5% |
| Technology & Software | 5239 | 10.5% |
| Logistics & Transportation | 4728 | 9.5% |
| Construction & Real Estate | 4238 | 8.5% |
| Agribusiness | 4144 | 8.3% |
| Professional Services | 4101 | 8.2% |
| Food & Beverage | 3804 | 7.6% |

**CRM status / enrichment status**

| crm_status | n | share |
|---|---|---|
| prospect | 44449 | 88.9% |
| customer | 4382 | 8.8% |
| churned_customer | 1000 | 2.0% |
| competitor | 169 | 0.3% |

| enrichment | n | share |
|---|---|---|
| complete | 41492 | 83.0% |
| partial | 8006 | 16.0% |
| stale | 502 | 1.0% |

## Contacts

118,944 contacts; accounts with 0 contacts: 608

| contacts / account | accounts |
|---|---|
| 1 | 14182 |
| 2 | 14966 |
| 3 | 10205 |
| 4 | 6059 |
| 5 | 3901 |
| 6 | 79 |

| email_status | n | share |
|---|---|---|
| valid | 95039 | 79.9% |
| unverified | 8182 | 6.9% |
| role_based | 5192 | 4.4% |
| catchall | 3074 | 2.6% |
| missing | 2571 | 2.2% |
| invalid_syntax | 2520 | 2.1% |
| free_provider | 2366 | 2.0% |

| function | n | share |
|---|---|---|
| finance | 41839 | 35.2% |
| operations | 17900 | 15.0% |
| executive | 16637 | 14.0% |
| procurement | 14095 | 11.9% |
| hr | 9570 | 8.0% |
| other | 9457 | 8.0% |
| it | 9446 | 7.9% |

| language | n | share |
|---|---|---|
| en | 118944 | 100.0% |

## Relationship / history

| measure | value |
|---|---|
| opportunities | 9762 |
| outreach touches | 42030 |
| suppression entries | 2934 |
| accounts with ≥1 touch | 21484 |
| accounts with an open opportunity | 1500 |
| accounts owned by an AE | 6500 |

| suppression reason | n | share |
|---|---|---|
| unsubscribe | 2042 | 69.6% |
| hard_bounce | 442 | 15.1% |
| dnc_request | 198 | 6.7% |
| spam_complaint | 186 | 6.3% |
| legal_hold | 66 | 2.2% |

## Event stream

55,959 deliveries over 33 days

| type | n | share |
|---|---|---|
| account_targeted | 52931 | 94.6% |
| reply_received | 1647 | 2.9% |
| opportunity_stage_changed | 463 | 0.8% |
| unsubscribe_received | 334 | 0.6% |
| opportunity_created | 267 | 0.5% |
| email_bounced | 181 | 0.3% |
| meeting_booked | 70 | 0.1% |
| lead_scored | 66 | 0.1% |

**Perturbations (ground truth)**

| perturbation | n | share of deliveries |
|---|---|---|
| none | 50925 | 91.0% |
| exact_duplicate | 1570 | 2.8% |
| delayed | 1355 | 2.4% |
| semantic_duplicate | 846 | 1.5% |
| malformed | 500 | 0.9% |
| out_of_order_race | 500 | 0.9% |
| content_duplicate | 263 | 0.5% |

| malformed kind | n | share |
|---|---|---|
| invalid_timestamp | 79 | 15.8% |
| null_payload | 74 | 14.8% |
| missing_account_id | 73 | 14.6% |
| payload_wrong_type | 71 | 14.2% |
| unknown_type | 66 | 13.2% |
| unsupported_schema | 64 | 12.8% |
| account_not_found | 63 | 12.6% |
| oversized_text | 10 | 2.0% |

| expected handling | n | share |
|---|---|---|
| process | 52280 | 93.4% |
| ignore_duplicate | 2416 | 4.3% |
| dead_letter | 500 | 0.9% |
| process_and_reconcile | 500 | 0.9% |
| dedupe_by_content | 263 | 0.5% |

Deliveries received >1h after they occurred: **2,325** (4.2%). Targeting events arrive in weekly bursts of ~20 minutes (rate-limit / queue stress).

## Replies (AI input)

1,536 labelled replies

| label | n | share |
|---|---|---|
| interested | 220 | 14.3% |
| not_now | 204 | 13.3% |
| objection | 179 | 11.7% |
| out_of_office | 149 | 9.7% |
| wrong_person | 142 | 9.2% |
| ambiguous | 128 | 8.3% |
| unsubscribe | 124 | 8.1% |
| auto_reply | 123 | 8.0% |
| info_request | 81 | 5.3% |
| mixed_signals | 76 | 4.9% |
| hostile | 45 | 2.9% |
| prompt_injection | 39 | 2.5% |
| empty_or_truncated | 26 | 1.7% |

| language | n | share |
|---|---|---|
| en | 1536 | 100.0% |

| difficulty | n | share |
|---|---|---|
| medium | 734 | 47.8% |
| easy | 581 | 37.8% |
| hard | 221 | 14.4% |

| expected action (state-aware) | n | share |
|---|---|---|
| escalate_human | 435 | 28.3% |
| wait | 353 | 23.0% |
| handoff_ae | 247 | 16.1% |
| suppress | 245 | 16.0% |
| enrich | 133 | 8.7% |
| no_action | 123 | 8.0% |

Ambiguous: 293 · needs human review: 574

## Company facts (personalization grounding)

81,017 facts · usable for personalization: 54,987 (67.9%)

| trap | n | share |
|---|---|---|
| clean | 54987 | 67.9% |
| stale | 14378 | 17.7% |
| unverified_hypothesis | 7474 | 9.2% |
| name_collision | 2114 | 2.6% |
| contradicts_firmographics | 2064 | 2.5% |

## AEs, routing and calendar

40 AEs · active 38 · on leave at the snapshot 3 · at/over capacity 13 · calendar rows 836

| account handoff route | n | share |
|---|---|---|
| OWNER | 4612 | 92.2% |
| OWNER_BACKUP | 388 | 7.8% |

| reply handoff route | n | share |
|---|---|---|
| TERRITORY | 161 | 65.2% |
| OWNER | 83 | 33.6% |
| OWNER_BACKUP | 3 | 1.2% |

## Experiment arms (simulated outcomes, see impact_example.md)

| arm | accounts | share |
|---|---|---|
| control | 25001 | 50.0% |
| treatment | 24999 | 50.0% |

## Mock API behaviours (deterministic per account)

| enrichment | n | share |
|---|---|---|
| ok | 43641 | 87.3% |
| timeout_once | 1991 | 4.0% |
| malformed_response | 1460 | 2.9% |
| rate_limit_once | 1411 | 2.8% |
| server_error_persistent | 986 | 2.0% |
| uncertain_outcome | 511 | 1.0% |

| send | n | share |
|---|---|---|
| ok | 46565 | 93.1% |
| transient_error_then_ok | 1490 | 3.0% |
| rate_limit_then_ok | 945 | 1.9% |
| uncertain_outcome | 603 | 1.2% |
| hard_reject | 397 | 0.8% |

| calendar | n | share |
|---|---|---|
| ok | 45924 | 91.8% |
| slot_conflict | 1993 | 4.0% |
| timeout_then_ok | 772 | 1.5% |
| uncertain_outcome | 546 | 1.1% |
| rate_limit_then_ok | 537 | 1.1% |
| server_error_persistent | 228 | 0.5% |

| crm | n | share |
|---|---|---|
| ok | 47091 | 94.2% |
| transient_error_then_ok | 1152 | 2.3% |
| rate_limit_then_ok | 745 | 1.5% |
| uncertain_outcome | 526 | 1.1% |
| stale_version_conflict | 486 | 1.0% |

**Mock enrichment payload variants**

| variant | n | share |
|---|---|---|
| good_contacts | 1498 | 66.1% |
| no_data | 444 | 19.6% |
| contradictory | 325 | 14.3% |
