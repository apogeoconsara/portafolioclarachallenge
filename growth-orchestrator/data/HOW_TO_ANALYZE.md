# How to find and analyze the data

## 1. Where is it?

The full data (50,000 accounts, ~300 MB) is **not in git**: it is regenerated on demand. What *is* in git, in the repository
`apogeoconsara/portafolioclarachallenge` (branch `main`), is a **500-account sample of every table** (JSONL and CSV)
and all the curated files.

| I want to… | Open this (paths relative to `growth-orchestrator/`) |
|---|---|
| Browse every table in Excel right now | `data/seed/sample/csv/*.csv` (download from GitHub, no setup) |
| See distributions, coverage and the 27 validation checks | `data/reports/data_profile.md` |
| See the simulated experiment results | `data/reports/impact_example.md` |
| Read the curated cases, policies, AI recordings, templates | `data/seed/*.jsonl`, `data/seed/*.json` |
| Understand every table and percentage | `data/DATA_REPORT.md` |
| Analyze the **full** 50,000 accounts | generate it first (step 2), then `data/generated/` |

GitHub folder with the sample CSVs:
`https://github.com/apogeoconsara/portafolioclarachallenge/tree/main/growth-orchestrator/data/seed/sample/csv`

## 2. Generate the full data on my computer (about 1.5 minutes, nothing to install except Python 3.11+)

```bash
git clone https://github.com/apogeoconsara/portafolioclarachallenge.git
cd portafolioclarachallenge
cd growth-orchestrator
python3 -m generator all --seed 42 --n 50000
```

That writes `data/generated/` with:

| File | What it is |
|---|---|
| `growth.sqlite` | **Everything in one database file** (open with DB Browser for SQLite, DBeaver, or `sqlite3`) |
| `csv/*.csv` | One CSV per table, UTF-8 with BOM so Excel opens accents correctly (largest: contacts, 119k rows, fits Excel's limit) |
| `*.jsonl` | The same tables, one JSON record per line |
| `truth/` (and `csv/truth/`) | The answer key: what the system *should* decide. Analysis only, never an input to the system |
| `manifest.json` | Row counts and a hash proving the data is reproducible |

The same seed (`42`) always gives the same data. `--seed 43` gives a different but equally valid world; `--n 5000` is a fast
small version.

## 3. Starter queries (SQLite)

```sql
-- accounts by scenario-relevant status
SELECT crm_status, COUNT(*) FROM accounts GROUP BY 1;

-- what should the system do for each account? (answer key lives in the CSV/JSONL truth files; load them as a table to join)
-- how usable are contact emails?
SELECT email_status, COUNT(*), ROUND(100.0*COUNT(*)/(SELECT COUNT(*) FROM contacts),1) AS pct
FROM contacts GROUP BY 1 ORDER BY 2 DESC;

-- event mix
SELECT type, COUNT(*) FROM events GROUP BY 1 ORDER BY 2 DESC;

-- events that arrived more than 1 hour after they happened
SELECT COUNT(*) FROM events
WHERE (julianday(received_at) - julianday(occurred_at)) * 24 > 1;

-- duplicate deliveries by idempotency key
SELECT idempotency_key, COUNT(*) c FROM events GROUP BY 1 HAVING c > 1 ORDER BY c DESC LIMIT 20;

-- AEs at or over capacity
SELECT ae_id, country, open_accounts, max_open_accounts FROM aes
WHERE open_accounts >= max_open_accounts;
```

## 4. Starter pandas snippet

```python
import pandas as pd
d = "data/generated"
accounts = pd.read_json(f"{d}/accounts.jsonl", lines=True)
truth = pd.read_json(f"{d}/truth/truth_accounts.jsonl", lines=True)
df = accounts.merge(truth, on="account_id")
print(df.groupby(["scenario", "expected_action"]).size().unstack(fill_value=0))
```

The answer key (`truth/…`) lets me check any account: its scenario, the action the system should take and why
(`reason_codes`), the best contact, and the AE to route to.
