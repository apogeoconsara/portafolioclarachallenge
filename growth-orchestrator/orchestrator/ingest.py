"""Webhook intake: validate the envelope, drop duplicates (3 ways), dead-letter garbage, ignore stale updates."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from .timeutil import valid_iso

KNOWN_TYPES = {"account_targeted", "reply_received", "unsubscribe_received", "email_bounced", "meeting_booked",
               "opportunity_created", "opportunity_stage_changed"}
REQUIRED = ("delivery_id", "event_id", "idempotency_key", "type", "schema_version", "source", "account_id",
            "occurred_at", "received_at", "payload")
SUPPORTED_SCHEMAS = {"1.0"}
MAX_BODY_CHARS = 20_000


@dataclass
class Intake:
    handling: str            # process | ignore_duplicate | dedupe_by_content | dead_letter | ignore_stale
    reason: str = ""
    content_hash: str = ""


def content_hash(env: dict) -> str:
    """Identity of the business fact, independent of who reported it (a CRM mirror and a provider webhook collide)."""
    p = env.get("payload") or {}
    t = env.get("type")
    core = {"account_targeted": p.get("list_id"), "reply_received": p.get("message_id"),
            "unsubscribe_received": env.get("contact_id"), "email_bounced": (p.get("thread_id"), p.get("bounce_type")),
            "meeting_booked": p.get("calendar_event_id"), "opportunity_created": p.get("opportunity_id"),
            "opportunity_stage_changed": (p.get("opportunity_id"), p.get("to_stage"))}.get(t)
    return hashlib.sha256(json.dumps([t, env.get("account_id"), env.get("contact_id"), core]).encode()).hexdigest()[:20]


def invalid_reason(conn: sqlite3.Connection, env) -> str | None:
    if not isinstance(env, dict):
        return "not_an_object"
    missing = [k for k in REQUIRED if k not in env]
    if missing:
        return f"missing_fields:{','.join(missing)}"
    if env["schema_version"] not in SUPPORTED_SCHEMAS:
        return f"unsupported_schema:{env['schema_version']}"
    if env["type"] not in KNOWN_TYPES:
        return f"unknown_type:{env['type']}"
    if not isinstance(env["account_id"], str) or not env["account_id"]:
        return "missing_account_id"
    if not valid_iso(env["occurred_at"]) or not valid_iso(env["received_at"]):
        return "invalid_timestamp"
    if not isinstance(env["payload"], dict):
        return "payload_not_an_object"
    if len(str(env["payload"].get("body_text", ""))) > MAX_BODY_CHARS:
        return "oversized_body"
    if conn.execute("SELECT 1 FROM accounts WHERE account_id=?", (env["account_id"],)).fetchone() is None:
        return "account_not_found"
    return None


def check(conn: sqlite3.Connection, env) -> Intake:
    d = env.get("delivery_id") if isinstance(env, dict) else None
    if d and conn.execute("SELECT 1 FROM inbox WHERE delivery_id=?", (d,)).fetchone():
        return Intake("ignore_duplicate", "delivery_already_seen")
    reason = invalid_reason(conn, env)
    if reason:
        return Intake("dead_letter", reason)
    if conn.execute("SELECT 1 FROM processed WHERE idempotency_key=?", (env["idempotency_key"],)).fetchone():
        return Intake("ignore_duplicate", "idempotency_key_already_processed")
    h = content_hash(env)
    if conn.execute("SELECT 1 FROM processed WHERE content_hash=?", (h,)).fetchone():
        return Intake("dedupe_by_content", "same_fact_reported_by_another_source", h)
    if env["type"] == "opportunity_stage_changed":
        r = conn.execute("SELECT last_event_at FROM opp_versions WHERE opportunity_id=?",
                         (env["payload"].get("opportunity_id"),)).fetchone()
        if r and r[0] > env["occurred_at"]:
            return Intake("ignore_stale", "older_than_applied_update", h)
    return Intake("process", "", h)


def record(conn, env, intake: Intake, status: str):
    e = env if isinstance(env, dict) else {}
    conn.execute("INSERT OR IGNORE INTO inbox (delivery_id, event_id, idempotency_key, content_hash, type, account_id, "
                 "occurred_at, received_at, status, handling, raw) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (e.get("delivery_id") or f"anon_{hashlib.sha256(json.dumps(env, default=str).encode()).hexdigest()[:12]}",
                  e.get("event_id"), e.get("idempotency_key"), intake.content_hash, e.get("type"), e.get("account_id"),
                  e.get("occurred_at"), e.get("received_at"), status, intake.handling,
                  json.dumps(env, default=str)[:50_000]))
    if status == "processed":
        conn.execute("INSERT OR IGNORE INTO processed (idempotency_key, content_hash, delivery_id, handling) VALUES (?,?,?,?)",
                     (e["idempotency_key"], intake.content_hash, e["delivery_id"], intake.handling))
