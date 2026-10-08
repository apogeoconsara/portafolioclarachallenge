"""Eligibility and next-best-action. Deterministic, no AI: policy v0 (data/POLICY.md), first matching rule wins."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .db import one, rows
from .policy import Policy
from .routing import route_ae
from .state import version
from .timeutil import iso, parse

REASON = {"unsubscribe": "UNSUBSCRIBED", "hard_bounce": "HARD_BOUNCE", "spam_complaint": "SPAM_COMPLAINT",
          "dnc_request": "DNC", "legal_hold": "LEGAL_HOLD", "competitor": "COMPETITOR"}


@dataclass
class Decision:
    action: str
    reason_codes: list
    best_contact_id: str | None = None
    wait_until: str | None = None
    route_to_ae_id: str | None = None
    route_reason: str | None = None
    state_version: int = 0
    notes: dict = field(default_factory=dict)

    @property
    def automation_allowed(self) -> bool:
        return self.action != "escalate_human"


def suppressed_contact_ids(conn, account: dict, contacts: list[dict]) -> set[str]:
    """Contacts that must not be written to: contact-level rows, plus everyone when the domain/account is suppressed."""
    sup = rows(conn, "SELECT * FROM suppression WHERE account_id=? OR (scope='domain' AND domain=?)",
               (account["account_id"], account["domain"]))
    if any(s["scope"] in ("domain", "account") for s in sup):
        return {c["contact_id"] for c in contacts}
    return {s["contact_id"] for s in sup if s["scope"] == "contact" and s["contact_id"]}


def best_contact(contacts: list[dict], blocked: set[str], policy: Policy) -> str | None:
    ok = [c for c in contacts if c["email_status"] == "valid" and c["contact_id"] not in blocked]
    if not ok:
        return None
    return min(ok, key=lambda c: (-policy.function_score[c["function"]], -policy.seniority_score[c["seniority"]],
                                  c["contact_id"]))["contact_id"]


def decide(conn: sqlite3.Connection, account_id: str, now: datetime, policy: Policy) -> Decision:
    a = one(conn, "SELECT * FROM accounts WHERE account_id=?", (account_id,))
    ver = version(conn, account_id)
    contacts = rows(conn, "SELECT * FROM contacts WHERE account_id=?", (account_id,))
    opps = rows(conn, "SELECT * FROM opportunities WHERE account_id=?", (account_id,))
    touches = rows(conn, "SELECT * FROM outreach_history WHERE account_id=?", (account_id,))
    sup = rows(conn, "SELECT * FROM suppression WHERE account_id=? OR (scope='domain' AND domain=?)", (account_id, a["domain"]))
    D = lambda action, codes, **kw: Decision(action, codes, state_version=ver, **kw)
    blocked = suppressed_contact_ids(conn, a, contacts)

    # 1. suppression
    wide = [s for s in sup if s["scope"] in ("domain", "account")]
    if wide:
        return D("suppress", [REASON[wide[0]["reason"]]])
    if a["crm_status"] == "competitor":
        return D("suppress", ["COMPETITOR"])
    if contacts and all(c["contact_id"] in blocked for c in contacts):
        by = {s["contact_id"]: s["reason"] for s in sup if s["scope"] == "contact"}
        return D("suppress", [REASON[by[contacts[0]["contact_id"]]]])
    # 2. conflicting CRM state
    won = [o for o in opps if o["stage"] == "closed_won"]
    if (a["crm_status"] == "customer" and not won) or (a["crm_status"] == "prospect" and won):
        return D("escalate_human", ["STATE_CONFLICT"])
    # 3-4. customers
    if a["crm_status"] == "customer":
        return D("suppress", ["CUSTOMER"])
    if a["crm_status"] == "churned_customer":
        return D("escalate_human", ["CHURNED_CUSTOMER"])
    # 5. duplicate account
    twin = one(conn, "SELECT account_id FROM accounts WHERE domain=? AND account_id<>? AND created_at < ?",
               (a["domain"], account_id, a["created_at"]))
    if twin:
        return D("escalate_human", ["DUPLICATE_ACCOUNT"], notes={"duplicate_of": twin["account_id"]})
    # 6. active opportunity
    if any(o["stage"] in policy.open_stages for o in opps):
        return D("suppress", ["ACTIVE_OPPORTUNITY"])
    # 7. assigned AE
    if a["crm_owner_ae_id"]:
        aes = rows(conn, "SELECT * FROM aes")
        ae, why = route_ae(a, None, aes, now)
        if ae is None:
            return D("escalate_human", ["NO_AE_AVAILABLE"], route_reason="NO_AE_AVAILABLE")
        return D("handoff_ae", ["AE_ASSIGNED"], route_to_ae_id=ae, route_reason=why)
    # 8. recently lost
    lost = [parse(o["closed_at"]) for o in opps if o["stage"] == "closed_lost" and o["closed_at"]]
    if lost and now - max(lost) < timedelta(days=policy.lost_cooldown_days):
        return D("wait", ["CLOSED_LOST_COOLDOWN"], wait_until=iso(max(lost) + timedelta(days=policy.lost_cooldown_days)))
    # 9. not ICP (only judged on known data)
    if a["employee_count"] is not None and a["employee_count"] < policy.icp_min_employees:
        return D("suppress", ["NOT_ICP"])
    if a["industry"] is not None and a["industry"] in policy.non_icp_industries:
        return D("suppress", ["NOT_ICP"])
    # 10. pacing (automated sequence touches only)
    seq = sorted(parse(t["sent_at"]) for t in touches if t["sender_type"] == "sequence")
    if seq:
        last = seq[-1]
        if now - last < timedelta(days=policy.recent_outreach_days):
            return D("wait", ["RECENT_OUTREACH"], wait_until=iso(last + timedelta(days=policy.recent_outreach_days)))
        in_window = [s for s in seq if now - s <= timedelta(days=policy.sequence_window_days)]
        if (len(in_window) >= policy.sequence_max_touches and not any(t["status"] == "replied" for t in touches)
                and now - last < timedelta(days=policy.sequence_cooldown_days)):
            return D("wait", ["SEQUENCE_EXHAUSTED"], wait_until=iso(last + timedelta(days=policy.sequence_cooldown_days)))
    # 11. data needs enrichment
    best = best_contact(contacts, blocked, policy)
    reason = None
    if not a["enriched_at"] or now - parse(a["enriched_at"]) > timedelta(days=policy.stale_enrichment_days):
        reason = "STALE_ENRICHMENT"
    elif a["employee_count"] is None or a["industry"] is None:
        reason = "MISSING_FIRMOGRAPHICS"
    elif best is None:
        live = [c for c in contacts if c["contact_id"] not in blocked]
        if not contacts:
            reason = "NO_CONTACTS"
        elif any(c["email_status"] == "unverified" for c in live) and not any(c["email_status"] == "valid" for c in live):
            reason = "UNVERIFIED_EMAIL_ONLY"
        else:
            reason = "NO_VALID_EMAIL"
    if reason:
        if a["enrichment_attempts"] >= policy.max_enrich_attempts:
            return D("escalate_human", ["ENRICHMENT_EXHAUSTED"])
        return D("enrich", [reason])
    # 12. eligible
    return D("contact", ["ELIGIBLE"], best_contact_id=best)
