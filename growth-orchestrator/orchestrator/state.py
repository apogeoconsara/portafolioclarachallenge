"""All writes to account/contact state go through here, so every change bumps the account version (optimistic locking)."""
from __future__ import annotations

import json
import sqlite3

from .db import one, rows
from .timeutil import iso


def version(conn: sqlite3.Connection, account_id: str) -> int:
    r = conn.execute("SELECT version FROM account_versions WHERE account_id=?", (account_id,)).fetchone()
    return r[0] if r else 0


def bump(conn: sqlite3.Connection, account_id: str) -> int:
    conn.execute("INSERT INTO account_versions (account_id, version) VALUES (?, 1) "
                 "ON CONFLICT(account_id) DO UPDATE SET version = version + 1", (account_id,))
    return version(conn, account_id)


def add_suppression(conn, account_id, reason, scope="contact", contact_id=None, source="orchestrator", now=None):
    c = one(conn, "SELECT * FROM contacts WHERE contact_id=?", (contact_id,)) if contact_id else None
    acct = one(conn, "SELECT domain FROM accounts WHERE account_id=?", (account_id,))
    sid = f"sup_o_{account_id}_{contact_id or scope}_{reason}"
    conn.execute("INSERT OR IGNORE INTO suppression (suppression_id, scope, reason, account_id, contact_id, email, domain, "
                 "source, added_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (sid, scope, reason, account_id, contact_id, c["email"] if c else None,
                  acct["domain"] if scope == "domain" and acct else None, source, iso(now) if now else None))
    bump(conn, account_id)


def mark_contact_invalid(conn, account_id, contact_id):
    conn.execute("UPDATE contacts SET email_status='invalid_syntax' WHERE contact_id=?", (contact_id,))
    bump(conn, account_id)


def upsert_opportunity(conn, account_id, opp_id, stage, owner_ae_id=None, closed_at=None, created_at=None):
    ex = one(conn, "SELECT * FROM opportunities WHERE opportunity_id=?", (opp_id,))
    if ex:
        conn.execute("UPDATE opportunities SET stage=?, closed_at=COALESCE(?, closed_at) WHERE opportunity_id=?",
                     (stage, closed_at, opp_id))
    else:
        conn.execute("INSERT INTO opportunities (opportunity_id, account_id, amount_usd, owner_ae_id, lost_reason, closed_at, "
                     "stage, created_at) VALUES (?,?,?,?,?,?,?,?)", (opp_id, account_id, 0, owner_ae_id, None, closed_at, stage,
                                                                    created_at))
    bump(conn, account_id)


def record_touch(conn, account_id, contact_id, step, subject, sender_type, sent_at, status="delivered", thread_id=None):
    n = conn.execute("SELECT COUNT(*) FROM outreach_history WHERE account_id=?", (account_id,)).fetchone()[0] + 1
    tid = f"tch_o_{account_id}_{n}"
    conn.execute("INSERT INTO outreach_history (touch_id, account_id, contact_id, thread_id, channel, step, sender_type, "
                 "sent_at, subject, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (tid, account_id, contact_id, thread_id or f"thr_{tid}", "email", step, sender_type, sent_at, subject, status))
    bump(conn, account_id)
    return tid


def apply_enrichment(conn, account_id, response: dict, now):
    """Merge an enrichment response into state. Returns what changed."""
    acct = one(conn, "SELECT * FROM accounts WHERE account_id=?", (account_id,))
    fm = response.get("firmographics") or {}
    changed = {"firmographics": [], "contacts_added": 0}
    sets, vals = [], []
    for k in ("employee_count", "industry", "revenue_band"):
        if fm.get(k) is not None and acct.get(k) is None:
            sets.append(f"{k}=?"); vals.append(fm[k]); changed["firmographics"].append(k)
    sets += ["enriched_at=?", "enrichment_attempts=enrichment_attempts+1", "enrichment_status='complete'"]
    vals.append(iso(now))
    conn.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE account_id=?", (*vals, account_id))
    n = conn.execute("SELECT COUNT(*) FROM contacts WHERE account_id=?", (account_id,)).fetchone()[0]
    for c in response.get("contacts") or []:
        n += 1
        conn.execute("INSERT OR IGNORE INTO contacts (contact_id, account_id, first_name, last_name, email, email_status, title, "
                     "function, seniority, language, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (f"con_e_{account_id}_{n}", account_id, c.get("first_name"), c.get("last_name"), c.get("email"),
                      c.get("email_status", "valid"), c.get("title"), c.get("function"), c.get("seniority"),
                      c.get("language", "en"), iso(now)))
        changed["contacts_added"] += 1
    bump(conn, account_id)
    return changed


def record_enrichment_attempt(conn, account_id):
    conn.execute("UPDATE accounts SET enrichment_attempts=enrichment_attempts+1 WHERE account_id=?", (account_id,))
    bump(conn, account_id)
