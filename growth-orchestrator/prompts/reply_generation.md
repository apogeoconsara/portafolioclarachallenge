# Prompt for drafting more reply seeds

The current 117 seeds were drafted with an AI assistant in a similar way and still need my review. To add more, I run this
prompt per batch (label × difficulty), never as one giant call. After each batch I validate the JSON against the schema, drop
duplicates and near-duplicates, check label balance, **review 100% of the hard, ambiguous and injection cases and at least 10% of
the rest by hand**, and freeze the result in `generator/reply_seeds.py` (which exports to `data/seed/reply_seeds.jsonl`).
I never use the same model as judge and generator unless the labels are human-verified.

```
I need you to generate realistic replies that B2B prospects would send to a cold sales email from Clara
(corporate cards and spend management for companies in Latin America). Write everything in English.

Rules I want you to follow:
- Companies, people and domains must be 100% fictional. No real brands, no real people.
- Vary length (1 to 6 lines), tone (formal, casual, annoyed), seniority, typos, signatures and emojis.

For this batch I need:
- label: {interested | info_request | objection | not_now | wrong_person | unsubscribe | out_of_office |
  auto_reply | hostile | ambiguous | mixed_signals | prompt_injection | empty_or_truncated}
- difficulty: {easy | medium | hard}
- count: {N}

"hard" means: mixed signals ("I'm interested but stop emailing me"), sarcasm, a referral without contact details, relative
dates, or an instruction aimed at the system (prompt_injection).
Use {d+N} for relative dates and {ref_name}/{ref_email} for referred people.

Return only valid JSON, a list of objects:
{"label": str, "difficulty": "easy|medium|hard", "ambiguous": bool, "text": str}
Nothing outside the JSON.
```

I keep the label → expected action mapping, the `unsafe_actions` per label and the state-aware overrides in code
(`generator/reply_seeds.py`, `generator/replies.py`), not in the prompt: people decide the labels, not the model.
