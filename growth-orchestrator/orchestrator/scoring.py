"""Account priority score and the audience check. Deterministic, no AI.

The score never decides eligibility: `rules.decide` does. Among eligible accounts it picks the track: tiers A and B get
a personal outreach email (first or follow-up), tier C goes to nurture (no email, no model call).
The audience check lists every eligibility condition as pass / fail / unknown. Unknown blocks (fail-closed).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from .db import one, rows
from .policy import SEED_DIR, Policy
from .rules import REASON, suppressed_contact_ids, best_contact
from .timeutil import parse


def load_config(version: str | None = None) -> dict:
    """scoring_policy.json plus the weights of a version (default: the active one) as `weights`, `tier_a`, `tier_b`, `version`."""
    cfg = json.loads((SEED_DIR / "scoring_policy.json").read_text(encoding="utf-8"))
    v = next(x for x in cfg["versions"] if x["id"] == (version or cfg["active_version"]))
    w = dict(v["weights"])
    return {**cfg, "version": v["id"], "tier_a": w.pop("tier_a"), "tier_b": w.pop("tier_b"), "weights": w}


def features(account: dict, facts: list[dict], cfg: dict) -> dict:
    """Raw inputs of the score (what the page needs to recompute it with other weights)."""
    n, (lo, hi) = account["employee_count"], cfg["size_sweet_spot"]
    # From `lo` employees up there is no upper limit (hi is null): a large company is not a weaker fit for this score.
    size = "unknown" if n is None else "ideal" if n >= lo and (hi is None or n <= hi) else "edge" if n >= 11 else "below"
    signals = [f["text"] for f in facts if f["is_verified"] and f["type"] in cfg["signal_fact_types"]]
    if account["international_signal"]:
        signals.append("International operations signal")
    pain = [f["text"] for f in facts if f["type"] == "pain_hypothesis"]
    return {"size": size, "pain": bool(pain), "pain_text": pain[0] if pain else None, "signals": signals}


def score(feat: dict, cfg: dict, weights: dict | None = None) -> dict:
    w = weights or cfg["weights"]
    size_pts = {"ideal": w["size"], "edge": int(w["size"] * cfg["size_edge_fraction"] + 0.5), "below": 0, "unknown": 0}[feat["size"]]
    pain_pts = w["pain"] if feat["pain"] else 0
    sig_pts = min(len(feat["signals"]), cfg["signal_cap"]) * w["signal_each"]
    total = size_pts + pain_pts + sig_pts
    tier = "A" if total >= cfg["tier_a"] else "B" if total >= cfg["tier_b"] else "C"
    return {"score": total, "tier": tier, "parts": {"size": size_pts, "pain": pain_pts, "signals": sig_pts}}


def audience_check(conn, account_id: str, now: datetime, policy: Policy) -> list[dict]:
    """One row per eligibility condition, in the order the rules apply them. status: pass | fail | unknown."""
    a = one(conn, "SELECT * FROM accounts WHERE account_id=?", (account_id,))
    contacts = rows(conn, "SELECT * FROM contacts WHERE account_id=?", (account_id,))
    opps = rows(conn, "SELECT * FROM opportunities WHERE account_id=?", (account_id,))
    touches = rows(conn, "SELECT * FROM outreach_history WHERE account_id=?", (account_id,))
    sup = rows(conn, "SELECT * FROM suppression WHERE account_id=? OR (scope='domain' AND domain=?)", (account_id, a["domain"]))
    blocked = suppressed_contact_ids(conn, a, contacts)
    out = []

    def add(cid, label, status, code=None):
        out.append({"id": cid, "label": label, "status": status, "code": code})

    wide = [s for s in sup if s["scope"] in ("domain", "account")]
    all_blocked = bool(contacts) and all(c["contact_id"] in blocked for c in contacts)
    why = None
    if wide:
        why = REASON[next(s for s in sup if s["scope"] in ("domain", "account"))["reason"]]
    elif all_blocked:
        by = {s["contact_id"]: s["reason"] for s in sup if s["scope"] == "contact"}
        why = REASON[by[contacts[0]["contact_id"]]]
    add("suppression", "Not on a suppression list", "fail" if why else "pass", why)
    add("competitor", "Not a competitor", "fail" if a["crm_status"] == "competitor" else "pass", "COMPETITOR" if a["crm_status"] == "competitor" else None)
    won = any(o["stage"] == "closed_won" for o in opps)
    conflict = (a["crm_status"] == "customer" and not won) or (a["crm_status"] == "prospect" and won)
    add("crm_consistent", "CRM state is consistent", "fail" if conflict else "pass", "STATE_CONFLICT" if conflict else None)
    cust = a["crm_status"] in ("customer", "churned_customer")
    add("not_customer", "Not a current or former customer", "fail" if cust else "pass",
        ("CUSTOMER" if a["crm_status"] == "customer" else "CHURNED_CUSTOMER") if cust else None)
    twin = one(conn, "SELECT account_id FROM accounts WHERE domain=? AND account_id<>? AND created_at < ?",
               (a["domain"], account_id, a["created_at"]))
    add("not_duplicate", "Not a duplicate of an older account", "fail" if twin else "pass", "DUPLICATE_ACCOUNT" if twin else None)
    openopp = any(o["stage"] in policy.open_stages for o in opps)
    add("no_open_opp", "No deal in progress", "fail" if openopp else "pass", "ACTIVE_OPPORTUNITY" if openopp else None)
    add("no_ae", "No sales exec already assigned", "fail" if a["crm_owner_ae_id"] else "pass", "AE_ASSIGNED" if a["crm_owner_ae_id"] else None)
    lost = [parse(o["closed_at"]) for o in opps if o["stage"] == "closed_lost" and o["closed_at"]]
    cool = bool(lost) and now - max(lost) < timedelta(days=policy.lost_cooldown_days)
    add("lost_cooldown", f"Not lost in the last {policy.lost_cooldown_days} days", "fail" if cool else "pass", "CLOSED_LOST_COOLDOWN" if cool else None)
    if a["employee_count"] is None or a["industry"] is None:
        icp = ("unknown", "MISSING_FIRMOGRAPHICS")
    elif a["employee_count"] < policy.icp_min_employees or a["industry"] in policy.non_icp_industries:
        icp = ("fail", "NOT_ICP")
    else:
        icp = ("pass", None)
    add("icp", "Big enough and in a target industry", *icp)
    seq = sorted(parse(t["sent_at"]) for t in touches if t["sender_type"] == "sequence")
    paced, code = True, None
    if seq:
        if now - seq[-1] < timedelta(days=policy.recent_outreach_days):
            paced, code = False, "RECENT_OUTREACH"
        else:
            inwin = [s for s in seq if now - s <= timedelta(days=policy.sequence_window_days)]
            if (len(inwin) >= policy.sequence_max_touches and not any(t["status"] == "replied" for t in touches)
                    and now - seq[-1] < timedelta(days=policy.sequence_cooldown_days)):
                paced, code = False, "SEQUENCE_EXHAUSTED"
    add("pacing", "Not contacted too recently or too often", "pass" if paced else "fail", code)
    stale = not a["enriched_at"] or now - parse(a["enriched_at"]) > timedelta(days=policy.stale_enrichment_days)
    missing = a["employee_count"] is None or a["industry"] is None
    add("data_fresh", "Company data is current and complete", "unknown" if stale or missing else "pass",
        "STALE_ENRICHMENT" if stale else "MISSING_FIRMOGRAPHICS" if missing else None)
    live = [c for c in contacts if c["contact_id"] not in blocked]
    if best_contact(contacts, blocked, policy):
        add("contact", "A valid, unsuppressed contact email", "pass")
    elif not contacts:
        add("contact", "A valid, unsuppressed contact email", "unknown", "NO_CONTACTS")
    elif any(c["email_status"] == "unverified" for c in live) and not any(c["email_status"] == "valid" for c in live):
        add("contact", "A valid, unsuppressed contact email", "unknown", "UNVERIFIED_EMAIL_ONLY")
    else:
        add("contact", "A valid, unsuppressed contact email", "unknown", "NO_VALID_EMAIL")
    return out


def verdict(checks: list[dict]) -> str:
    """Fail-closed: anything that is not an explicit pass blocks the send."""
    return "eligible" if all(c["status"] == "pass" for c in checks) else "blocked"
