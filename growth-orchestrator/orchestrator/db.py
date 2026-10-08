"""SQLite persistence: source-system state (accounts, contacts, ...) + the orchestrator's own operational tables."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

STATE_DDL = """
CREATE TABLE IF NOT EXISTS accounts (account_id TEXT PRIMARY KEY, name TEXT, legal_name TEXT, domain TEXT, country TEXT,
  industry TEXT, employee_count INTEGER, employee_band TEXT, revenue_band TEXT, international_signal INTEGER, source TEXT,
  list_id TEXT, crm_status TEXT, crm_owner_ae_id TEXT, enrichment_status TEXT, enriched_at TEXT, enrichment_attempts INTEGER,
  customer_since TEXT, churned_at TEXT, created_at TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS ix_accounts_domain ON accounts(domain);
CREATE TABLE IF NOT EXISTS contacts (contact_id TEXT PRIMARY KEY, account_id TEXT, first_name TEXT, last_name TEXT, email TEXT,
  email_status TEXT, title TEXT, function TEXT, seniority TEXT, language TEXT, linkedin_url TEXT, last_verified_at TEXT,
  created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_contacts_account ON contacts(account_id);
CREATE TABLE IF NOT EXISTS opportunities (opportunity_id TEXT PRIMARY KEY, account_id TEXT, amount_usd INTEGER, owner_ae_id TEXT,
  lost_reason TEXT, closed_at TEXT, stage TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_opps_account ON opportunities(account_id);
CREATE TABLE IF NOT EXISTS outreach_history (touch_id TEXT PRIMARY KEY, account_id TEXT, contact_id TEXT, thread_id TEXT,
  channel TEXT, step INTEGER, sender_type TEXT, sent_at TEXT, subject TEXT, status TEXT);
CREATE INDEX IF NOT EXISTS ix_touch_account ON outreach_history(account_id);
CREATE TABLE IF NOT EXISTS suppression (suppression_id TEXT PRIMARY KEY, scope TEXT, reason TEXT, account_id TEXT,
  contact_id TEXT, email TEXT, domain TEXT, source TEXT, added_at TEXT);
CREATE INDEX IF NOT EXISTS ix_supp_account ON suppression(account_id);
CREATE INDEX IF NOT EXISTS ix_supp_domain ON suppression(domain);
CREATE TABLE IF NOT EXISTS company_facts (fact_id TEXT PRIMARY KEY, account_id TEXT, type TEXT, text TEXT, source_name TEXT,
  source_url TEXT, observed_at TEXT, is_verified INTEGER, confidence REAL);
CREATE INDEX IF NOT EXISTS ix_facts_account ON company_facts(account_id);
CREATE TABLE IF NOT EXISTS aes (ae_id TEXT PRIMARY KEY, name TEXT, country TEXT, segment TEXT, email TEXT, languages TEXT,
  timezone TEXT, utc_offset_hours INTEGER, active INTEGER, out_of_office_until TEXT, backup_ae_id TEXT,
  open_accounts INTEGER, max_open_accounts INTEGER);
CREATE TABLE IF NOT EXISTS ae_calendar (ae_id TEXT, date TEXT, timezone TEXT, free_slots TEXT);
CREATE TABLE IF NOT EXISTS mock_behavior (account_id TEXT PRIMARY KEY, enrichment TEXT, send TEXT, calendar TEXT, crm TEXT);
CREATE TABLE IF NOT EXISTS mock_enrichment (account_id TEXT PRIMARY KEY, variant TEXT, response TEXT,
  expected_action_after_enrichment TEXT);
"""

OPS_DDL = """
CREATE TABLE IF NOT EXISTS inbox (delivery_id TEXT PRIMARY KEY, event_id TEXT, idempotency_key TEXT, content_hash TEXT,
  type TEXT, account_id TEXT, occurred_at TEXT, received_at TEXT, status TEXT, handling TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS processed (idempotency_key TEXT PRIMARY KEY, content_hash TEXT, delivery_id TEXT, handling TEXT);
CREATE INDEX IF NOT EXISTS ix_processed_content ON processed(content_hash);
CREATE TABLE IF NOT EXISTS decisions (decision_id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, event_id TEXT,
  delivery_id TEXT, trigger_type TEXT, action TEXT, reason_codes TEXT, best_contact_id TEXT, wait_until TEXT,
  route_to_ae_id TEXT, route_reason TEXT, automation_allowed INTEGER, state_version INTEGER, decided_by TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS actions (action_id INTEGER PRIMARY KEY AUTOINCREMENT, idempotency_key TEXT UNIQUE, account_id TEXT,
  contact_id TEXT, decision_id INTEGER, kind TEXT, system TEXT, status TEXT, attempts INTEGER DEFAULT 0, request TEXT,
  response TEXT, send_after TEXT, created_at TEXT, updated_at TEXT, reviewer TEXT, reviewed_at TEXT, review_note TEXT);
CREATE INDEX IF NOT EXISTS ix_actions_account ON actions(account_id);
CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, account_id TEXT, event_id TEXT,
  delivery_id TEXT, kind TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_audit_account ON audit_log(account_id);
CREATE TABLE IF NOT EXISTS dead_letters (id INTEGER PRIMARY KEY AUTOINCREMENT, delivery_id TEXT, reason TEXT, raw TEXT,
  created_at TEXT);
CREATE TABLE IF NOT EXISTS review_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, event_id TEXT,
  reason_codes TEXT, payload TEXT, status TEXT DEFAULT 'open', created_at TEXT);
CREATE TABLE IF NOT EXISTS ai_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, model TEXT, mode TEXT, input_hash TEXT,
  raw_output TEXT, verdict TEXT, violation_codes TEXT, final_action TEXT, latency_ms INTEGER, input_tokens INTEGER,
  output_tokens INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS account_versions (account_id TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS opp_versions (opportunity_id TEXT PRIMARY KEY, last_event_at TEXT);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0);
"""

JSON_COLS = {"languages", "free_slots", "response"}


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(STATE_DDL + OPS_DDL)
    return conn


def insert_rows(conn: sqlite3.Connection, table: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    n = 0
    for r in rows:
        use = [c for c in cols if c in r]
        vals = [json.dumps(r[c]) if (c in JSON_COLS and not isinstance(r[c], str)) else
                (int(r[c]) if isinstance(r[c], bool) else r[c]) for c in use]
        conn.execute(f"INSERT OR REPLACE INTO {table} ({','.join(use)}) VALUES ({','.join('?' * len(use))})", vals)
        n += 1
    return n


def load_snapshot(conn: sqlite3.Connection, tables: dict[str, list[dict]]) -> None:
    for table, rows in tables.items():
        insert_rows(conn, table, rows)
    conn.commit()


def load_world_dir(conn: sqlite3.Connection, d: Path) -> dict:
    """Load a generated world (JSONL files) into the state tables. Never reads truth/."""
    names = {"accounts": "accounts", "contacts": "contacts", "opportunities": "opportunities",
             "outreach_history": "outreach_history", "suppression": "suppression", "company_facts": "company_facts",
             "aes": "aes", "ae_calendar": "ae_calendar", "mock_behavior": "mock_behavior",
             "mock_enrichment": "mock_enrichment"}
    counts = {}
    for f, table in names.items():
        p = d / f"{f}.jsonl"
        if p.exists():
            rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
            counts[table] = insert_rows(conn, table, rows)
    conn.commit()
    return counts


def rows(conn, sql, params=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params)]


def one(conn, sql, params=()):
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None
