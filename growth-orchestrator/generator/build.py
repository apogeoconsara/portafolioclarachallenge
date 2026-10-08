"""Builds the synthetic world: tables (as-of snapshot), the event stream, mock-API behaviours and ground truth.

Two layers:
  1. statistical volume   -> exact scenario quotas over N accounts, realistic attribute distributions
  2. perturbations        -> duplicates, delays, out-of-order races, malformed events (explicit rates)

Everything is deterministic for a given (seed, n). Per-account RNGs keep stages independent.
"""
from __future__ import annotations

import copy
import json
from collections import defaultdict
from datetime import timedelta

from . import impact, oracle, stateful
from .config import (AS_OF, BANDS, CALENDAR_BEHAVIORS, COUNTRIES, CRM_BEHAVIORS, EMAIL_STATUS_WEIGHTS, ENRICH_BEHAVIORS,
                     FREE_EMAIL_DOMAINS, FUNCTION_WEIGHTS, INDUSTRIES, LIST_BURST_MINUTES, LIST_DROP_DAYS,
                     LIST_WEIGHTS, LOST_COOLDOWN_DAYS, N_CONTACTS_WEIGHTS, OPEN_STAGES, RATE_CONTENT_DUP,
                     RATE_DELAYED, RATE_EXACT_DUP, RATE_MALFORMED, RATE_SEMANTIC_DUP, RECENT_OUTREACH_DAYS,
                     ROLE_INBOXES, SCENARIO_QUOTAS, SCHEMA_VERSION, SEND_BEHAVIORS, SEQUENCE_COOLDOWN_DAYS,
                     SENIORITY_WEIGHTS, STREAM_DAYS)
from .names import (ERPS, FIRST_EN, FIRST_ES, FIRST_PT, LAST_ES, LAST_PT, LOST_REASONS, PRODUCTS, STEMS,
                    TITLES)
from .policy_data import TZ_NAME, UTC_OFFSET
from .replies import pick_label, pick_seed, render_reply
from .util import iso, parse_iso, quota_assign, rng, sha, slug, wpick

SUBJECTS = ["Spend management for {company}", "Corporate cards for your team",
            "How does {company} handle travel expenses?", "Expense control without reimbursements"]
SCENARIO_STATE = {"customer": "customer", "churned_customer": "churned_customer",
                  "active_opportunity": "active_opportunity", "ae_assigned": "ae_assigned"}
CITY_COUNTRY = [(city, c[1]) for c in COUNTRIES for city in c[5]]


class World:
    def __init__(self):
        self.aes = []
        self.calendar = []
        self.arms = []
        self.sim = []
        self.accounts = []
        self.contacts = []
        self.opportunities = []
        self.touches = []
        self.suppression = []
        self.facts = []
        self.mock_behavior = []
        self.mock_enrichment = []
        self.events = []            # envelopes in ingestion order
        self.truth_accounts = []
        self.truth_events = []
        self.truth_facts = []
        self.truth_replies = []


class Ctx:
    def __init__(self, seed, as_of):
        self.seed = seed
        self.as_of = as_of
        self.domains = set()
        self.emails = set()
        self.names = []
        self.aes = []
        self.aes_by_country = defaultdict(list)


# ---- small generators -----------------------------------------------------------------------------
def _pick_country(ar):
    return wpick(ar, [(c, c[3]) for c in COUNTRIES])


def _industry(ar, icp_ok=True):
    return wpick(ar, [(i, i[1]) for i in INDUSTRIES if i[3] == icp_ok])


def _employees(ar, mode):
    if mode == "small":
        band = BANDS[0]
    elif mode == "icp":
        band = wpick(ar, [(b, b[3]) for b in BANDS[1:]])
    else:
        band = wpick(ar, [(b, b[3]) for b in BANDS])
    count = int(band[1] + (band[2] - band[1]) * ar.random() ** 2)
    return band[0], max(band[1], min(count, band[2]))


def _revenue_band(ar, employees):
    usd = employees * ar.randint(40_000, 200_000)
    for limit, label in ((1e6, "<1M"), (5e6, "1-5M"), (20e6, "5-20M"), (100e6, "20-100M")):
        if usd < limit:
            return label
    return "100M+"


def _company(ar, ctx, country, industry):
    for attempt in range(60):
        k = 2 if attempt < 3 else 3 if attempt < 25 else 4
        stem = "".join(ar.choice(STEMS) for _ in range(k)).capitalize()
        word = ar.choice(industry[2])
        pattern = ar.choice(["{s} {w}", "{w} {s}", "Group {s}", "{s}"]) if industry[3] else "{w} of {s}"
        name = pattern.format(s=stem, w=word)
        sl = slug(stem + (word if "{w}" in pattern else ""))
        domain = f"{sl}.{country[0].lower()}.example"  # .example is reserved (RFC 2606): can never be real
        if domain not in ctx.domains:
            break
    ctx.domains.add(domain)
    return name, f"{name} {ar.choice(country[4])}", domain


def _person(ar, origin):
    """Regional first/last names (origin 'pt' = Brazil, otherwise Spanish-speaking LatAm). Contacts all write English."""
    if origin == "pt":
        return ar.choice(FIRST_PT), ar.choice(LAST_PT)
    return ar.choice(FIRST_ES), ar.choice(LAST_ES)


def _contact_lang(ar, country_lang):
    return "en"


OOO_AES = {5: 8, 17: 15, 33: 22}      # ae index -> days until back (on leave at the snapshot)
INACTIVE_AES = (11, 29)               # left the company / deactivated


def _make_aes(r):
    out = []
    for i in range(1, 41):
        c = COUNTRIES[(i - 1) % len(COUNTRIES)]
        first, last = _person(r, c[2])
        langs = ["en"]
        out.append({"ae_id": f"ae_{i:03d}", "name": f"{first} {last}", "country": c[0],
                    "segment": "mid_market" if i % 3 else "enterprise",
                    "email": f"{slug(first)}.{slug(last)}@clara-demo.example", "languages": langs,
                    "timezone": TZ_NAME[c[0]], "utc_offset_hours": UTC_OFFSET[c[0]],
                    "active": i not in INACTIVE_AES,
                    "out_of_office_until": iso(AS_OF + timedelta(days=OOO_AES[i])) if i in OOO_AES else None,
                    "backup_ae_id": None, "open_accounts": 0, "max_open_accounts": 0})
    for i, ae in enumerate(out):
        same = [out[(i + k) % len(out)] for k in range(1, len(out))]
        same = [a for a in same if a["country"] == ae["country"]]
        ok = [a for a in same if oracle.available(a)]
        ae["backup_ae_id"] = (ok or same)[0]["ae_id"] if same else None
    return out


def _finalize_aes(w, seed):
    """Static load snapshot + capacity (some AEs deliberately full) once accounts are assigned."""
    load = defaultdict(int)
    for a in w.accounts:
        if a["crm_owner_ae_id"]:
            load[a["crm_owner_ae_id"]] += 1
    r = rng(seed, "aecap")
    per_country = defaultdict(list)
    for ae in w.aes:
        if ae["active"]:
            per_country[ae["country"]].append(ae)
    for country, group in per_country.items():
        avg = sum(load[a["ae_id"]] for a in group) / len(group)   # capacity is sized per territory
        for ae in group:
            ae["open_accounts"] = load[ae["ae_id"]]
            ae["max_open_accounts"] = max(10, int(avg * r.uniform(0.92, 1.25)))
    for ae in w.aes:
        if not ae["active"]:
            ae["open_accounts"] = load[ae["ae_id"]]
            ae["max_open_accounts"] = max(10, ae["open_accounts"])


def _make_calendar(seed, aes, as_of):
    """Free 30-min slots per AE per working day for the stream window (AE local time)."""
    rows = []
    for ae in aes:
        if not ae["active"]:
            continue
        cr = rng(seed, f"cal:{ae['ae_id']}")
        back = parse_iso(ae["out_of_office_until"]) if ae["out_of_office_until"] else None
        for d in range(STREAM_DAYS):
            day = as_of + timedelta(days=d)
            if day.weekday() >= 5:
                continue
            slots = [f"{h:02d}:{m:02d}" for h in range(9, 17) for m in (0, 30) if cr.random() < 0.55]
            if back and day < back:
                slots = []
            rows.append({"ae_id": ae["ae_id"], "date": day.date().isoformat(), "timezone": ae["timezone"],
                         "free_slots": slots})
    return rows


# ---- scenario plans ---------------------------------------------------------------------------------
def _plan(sc, ar):
    p = {"contact_mode": "normal", "min_valid": 1, "n_contacts": None, "status": "prospect", "ae": False,
         "opps": [], "touches": [], "supp": [], "enrich": "fresh", "attempts": 0, "action": "contact",
         "reasons": ["ELIGIBLE"], "reply_p": 0.0, "reply_sender": "sequence", "race": None,
         "emp_mode": "icp", "gov": False, "firmo_missing": False, "extra": {}, "sub": None}
    if sc == "clean_prospect":
        k = ar.random()
        if k < 0.30:
            p["touches"] = [(ar.randint(35, 200), 1, "sequence")]
        elif k < 0.45:
            d = ar.randint(35, 120)
            p["touches"] = [(d, 2, "sequence"), (d + ar.randint(7, 20), 1, "sequence")]
        elif k < 0.50:
            d = ar.randint(35, 90)
            p["touches"] = [(d, 3, "sequence"), (d + ar.randint(7, 20), 2, "sequence"),
                            (d + ar.randint(21, 40), 1, "sequence")]
        if ar.random() < 0.10:
            p["opps"] = [("closed_lost", ar.randint(100, 500))]
    elif sc == "partial_contact_suppression":
        p.update(n_contacts=ar.randint(2, 4), min_valid=2, supp=[("best", "unsubscribe")])
    elif sc == "race_late_opportunity":
        p["race"] = "late_opportunity"
    elif sc == "race_late_unsubscribe":
        p.update(race="late_unsubscribe", n_contacts=ar.randint(2, 3), min_valid=2)
    elif sc == "customer":
        p.update(status="customer", opps=[("closed_won", ar.randint(30, 900))], action="suppress",
                 reasons=["CUSTOMER"], reply_p=0.03, reply_sender="cs")
    elif sc == "churned_customer":
        won = ar.randint(400, 1200)
        p.update(status="churned_customer", opps=[("closed_won", won)], action="escalate_human",
                 reasons=["CHURNED_CUSTOMER"], extra={"churned_days_ago": ar.randint(90, min(360, won - 30))})
    elif sc == "active_opportunity":
        p.update(ae=True, opps=[("open", ar.choice(OPEN_STAGES))], action="suppress",
                 reasons=["ACTIVE_OPPORTUNITY"], reply_p=0.06, reply_sender="ae")
    elif sc == "ae_assigned":
        p.update(ae=True, action="handoff_ae", reasons=["AE_ASSIGNED"], reply_p=0.04, reply_sender="ae")
        if ar.random() < 0.4:
            p["touches"] = [(ar.randint(20, 200), 1, "ae")]
    elif sc == "recent_outreach":
        last = ar.randint(1, 13)
        n = ar.randint(1, 3)
        t, touches = last, []
        for step in range(n, 0, -1):  # newest touch carries the highest step
            touches.append((t, step, "sequence"))
            t += ar.randint(5, 12)
        p.update(touches=touches, action="wait", reasons=["RECENT_OUTREACH"], reply_p=0.16)
    elif sc == "max_touches_no_reply":
        t, touches = ar.randint(15, 100), []
        for step in range(ar.randint(4, 5), 0, -1):
            touches.append((t, min(step, 4), "sequence"))
            t += ar.randint(7, 20)
        p.update(touches=touches, action="wait", reasons=["SEQUENCE_EXHAUSTED"])
    elif sc == "suppressed":
        sub = wpick(ar, [("unsub_all", 35), ("complaint", 15), ("dnc", 15), ("legal", 5),
                         ("hard_bounce_all", 15), ("competitor", 15)])
        p.update(action="suppress", sub=sub)
        if sub == "unsub_all":
            p.update(supp=[("all", "unsubscribe")], reasons=["UNSUBSCRIBED"])
        elif sub == "complaint":
            p.update(supp=[("domain", "spam_complaint")], reasons=["SPAM_COMPLAINT"])
        elif sub == "dnc":
            p.update(supp=[("domain", "dnc_request")], reasons=["DNC"])
        elif sub == "legal":
            p.update(supp=[("domain", "legal_hold")], reasons=["LEGAL_HOLD"])
        elif sub == "hard_bounce_all":
            p.update(supp=[("all", "hard_bounce")], reasons=["HARD_BOUNCE"])
        else:
            p.update(status="competitor", reasons=["COMPETITOR"])
    elif sc == "missing_data":
        sub = wpick(ar, [("no_contacts", 25), ("all_invalid", 40), ("stale", 20), ("missing_firmo", 10),
                         ("unverified_only", 5)])
        p.update(action="enrich", sub=sub, min_valid=0)
        if sub == "no_contacts":
            p.update(contact_mode="none", reasons=["NO_CONTACTS"])
        elif sub == "all_invalid":
            p.update(contact_mode="all_invalid", reasons=["NO_VALID_EMAIL"])
        elif sub == "stale":
            p.update(enrich="stale", min_valid=1, reasons=["STALE_ENRICHMENT"])
        elif sub == "missing_firmo":
            p.update(firmo_missing=True, min_valid=1, reasons=["MISSING_FIRMOGRAPHICS"])
        else:
            p.update(contact_mode="unverified_only", reasons=["UNVERIFIED_EMAIL_ONLY"])
        if ar.random() < 0.08:
            p.update(attempts=2, action="escalate_human", reasons=["ENRICHMENT_EXHAUSTED"])
    elif sc == "duplicate_domain":
        p.update(action="escalate_human", reasons=["DUPLICATE_ACCOUNT"])
    elif sc == "low_icp":
        if ar.random() < 0.7:
            p.update(emp_mode="small", sub="tiny")
        else:
            p.update(gov=True, sub="gov_ngo")
        p.update(action="suppress", reasons=["NOT_ICP"])
    elif sc == "conflict_state":
        if ar.random() < 0.5:
            p.update(status="customer", sub="customer_without_won_opp")
        else:
            p.update(status="prospect", opps=[("closed_won", ar.randint(5, 60))], sub="prospect_with_won_opp")
        p.update(action="escalate_human", reasons=["STATE_CONFLICT"])
    elif sc == "closed_lost_recent":
        p.update(opps=[("closed_lost", ar.randint(10, 85))], action="wait", reasons=["CLOSED_LOST_COOLDOWN"])
    return p


# ---- contacts -----------------------------------------------------------------------------------------
def _email_for(ar, ctx, status, first, last, domain, taken_roles):
    if status in ("valid", "unverified", "catchall"):
        base = f"{slug(first)}.{slug(last)}"
        email, i = f"{base}@{domain}", 1
        while email in ctx.emails:
            i += 1
            email = f"{base}{i}@{domain}"
        ctx.emails.add(email)
        return email
    if status == "role_based":
        role = next((r for r in ROLE_INBOXES if r not in taken_roles), ROLE_INBOXES[0])
        taken_roles.add(role)
        return f"{role}@{domain}"
    if status == "free_provider":
        return f"{slug(first)}{slug(last)}{ar.randint(1, 99)}@{ar.choice(FREE_EMAIL_DOMAINS)}"
    if status == "invalid_syntax":
        local = f"{slug(first)}.{slug(last)}"
        return ar.choice([f"{local}{domain}", f"{local}@@{domain}", f"{local}@{domain}.", f"{local} @{domain}",
                          f"{local}@{domain.split('.')[0]}"])
    return None


def _make_contacts(ar, ctx, a, p, country_lang):
    mode = p["contact_mode"]
    if mode == "none":
        return []
    k = p["n_contacts"] or wpick(ar, N_CONTACTS_WEIGHTS)
    if mode == "all_invalid":
        statuses = [wpick(ar, [("invalid_syntax", 3), ("missing", 3), ("free_provider", 2), ("role_based", 2),
                               ("catchall", 1)]) for _ in range(k)]
    elif mode == "unverified_only":
        statuses = ["unverified"] * k
    else:
        statuses = [wpick(ar, EMAIL_STATUS_WEIGHTS) for _ in range(k)]
        while statuses.count("valid") < min(p["min_valid"], k):
            statuses[ar.choice([i for i, s in enumerate(statuses) if s != "valid"])] = "valid"
    out, taken_roles = [], set()
    for j, status in enumerate(statuses, 1):
        lang = _contact_lang(ar, country_lang)
        func = wpick(ar, FUNCTION_WEIGHTS)
        sen = wpick(ar, SENIORITY_WEIGHTS)
        if func == "executive":
            sen = "c_level" if ar.random() < 0.8 else "vp"
        first, last = _person(ar, country_lang)
        title = ar.choice(TITLES[func][sen])
        email = _email_for(ar, ctx, status, first, last, a["domain"], taken_roles)
        if status == "role_based":
            first, last, title, sen = "Team", email.split("@")[0].capitalize(), "Generic mailbox", "ic"
        out.append({
            "contact_id": f"con_{a['account_id'][4:]}_{j}", "account_id": a["account_id"],
            "first_name": first, "last_name": last, "email": email, "email_status": status,
            "title": title, "function": func, "seniority": sen, "language": lang,
            "linkedin_url": f"https://linkedin.example/in/{slug(first)}-{slug(last)}-{ar.randint(100, 999)}",
            "last_verified_at": iso(ctx.as_of - timedelta(days=ar.randint(5, 180))) if status == "valid" else None,
            "created_at": iso(ctx.as_of - timedelta(days=ar.randint(5, 900))),
        })
    return out


# ---- account assembly -----------------------------------------------------------------------------------
def _make_account(ctx, i, sc, ar, p):
    country = _pick_country(ar)
    industry = _industry(ar, icp_ok=not p["gov"])
    band, employees = _employees(ar, p["emp_mode"] if not p["gov"] else "icp")
    name, legal, domain = _company(ar, ctx, country, industry)
    ctx.names.append(name)
    aid = f"acc_{i:06d}"
    ae_id = None
    if p["ae"]:
        pool = ctx.aes_by_country[country[0]] or ctx.aes
        ae_id = ar.choice(pool)["ae_id"]
    enriched = None
    if p["enrich"] == "fresh":
        enriched = ctx.as_of - timedelta(days=ar.randint(3, 70))
    elif p["enrich"] == "stale":
        enriched = ctx.as_of - timedelta(days=ar.randint(130, 400))
    status = "complete" if p["enrich"] == "fresh" and ar.random() < 0.85 else "partial"
    if p["enrich"] == "stale":
        status = "stale"
    a = {
        "account_id": aid, "name": name, "legal_name": legal, "domain": domain, "country": country[0],
        "industry": industry[0], "employee_count": employees, "employee_band": band,
        "revenue_band": _revenue_band(ar, employees) if status == "complete" else None,
        "international_signal": False,
        "source": wpick(ar, [("target_list", 80), ("inbound_form", 8), ("event_scan", 7), ("partner_referral", 5)]),
        "list_id": None, "crm_status": p["status"], "crm_owner_ae_id": ae_id,
        "enrichment_status": status, "enriched_at": iso(enriched) if enriched else None,
        "enrichment_attempts": p["attempts"], "customer_since": None, "churned_at": None,
        "created_at": iso(ctx.as_of - timedelta(days=ar.randint(30, 1500))),
        "updated_at": iso(ctx.as_of - timedelta(days=ar.randint(0, 29))),
    }
    if p["firmo_missing"]:
        a.update(employee_count=None, employee_band=None, industry=None, revenue_band=None,
                 enrichment_status="partial")
    if p["contact_mode"] == "none" and status == "complete":
        a["enrichment_status"] = "partial"
    return a, country


def _make_opps(ar, ctx, a, p):
    rows = []
    for n, (kind, val) in enumerate(p["opps"], 1):
        amount = int((a["employee_count"] or 50) * ar.randint(200, 900))
        o = {"opportunity_id": f"opp_{a['account_id'][4:]}_{n}", "account_id": a["account_id"],
             "amount_usd": amount, "owner_ae_id": a["crm_owner_ae_id"], "lost_reason": None, "closed_at": None}
        if kind == "open":
            o.update(stage=val, created_at=iso(ctx.as_of - timedelta(days=ar.randint(5, 120))))
        elif kind == "closed_won":
            closed = ctx.as_of - timedelta(days=val)
            o.update(stage="closed_won", closed_at=iso(closed), created_at=iso(closed - timedelta(days=ar.randint(20, 90))))
        else:
            closed = ctx.as_of - timedelta(days=val)
            o.update(stage="closed_lost", closed_at=iso(closed), lost_reason=ar.choice(LOST_REASONS),
                     created_at=iso(closed - timedelta(days=ar.randint(15, 80))))
        rows.append(o)
    return rows


def _make_touches(ar, ctx, a, contacts, p, lang):
    reachable = [c for c in contacts if c["email_status"] in ("valid", "unverified", "catchall")]
    if not reachable or not p["touches"]:
        return []
    best = oracle.best_contact(reachable, set())
    target = next((c for c in reachable if c["contact_id"] == best), reachable[0])
    rows = []
    for n, (days, step, sender) in enumerate(p["touches"], 1):
        tid = f"tch_{a['account_id'][4:]}_{n}"
        subj = ar.choice(SUBJECTS).format(company=a["name"])
        rows.append({"touch_id": tid, "account_id": a["account_id"], "contact_id": target["contact_id"],
                     "thread_id": f"thr_{tid}", "channel": "email", "step": step, "sender_type": sender,
                     "sent_at": iso(ctx.as_of - timedelta(days=days, hours=ar.randint(0, 23), minutes=ar.randint(0, 59))),
                     "subject": subj, "status": ar.choice(["delivered", "delivered", "opened"])})
    return rows


def _make_suppression(ctx, a, contacts, p, ar):
    rows = []

    def row(scope, reason, contact=None):
        n = len(rows) + 1
        rows.append({"suppression_id": f"sup_{a['account_id'][4:]}_{n}", "scope": scope, "reason": reason,
                     "account_id": a["account_id"], "contact_id": contact["contact_id"] if contact else None,
                     "email": contact["email"] if contact else None,
                     "domain": a["domain"] if scope == "domain" else None,
                     "source": "email_provider" if reason in ("unsubscribe", "hard_bounce") else "crm",
                     "added_at": iso(ctx.as_of - timedelta(days=ar.randint(5, 400)))})

    for kind, reason in p["supp"]:
        if kind == "domain":
            row("domain", reason)
        elif kind == "all":
            for c in contacts:
                row("contact", reason, c)
        elif kind == "best":
            best = oracle.best_contact(contacts, set())
            row("contact", reason, next(c for c in contacts if c["contact_id"] == best))
    return rows


def _make_facts(ar, ctx, a, country):
    rows = []
    k = wpick(ar, [(0, 25), (1, 25), (2, 25), (3, 15), (4, 7), (5, 3)])
    kinds = [("expansion", 18), ("hiring", 18), ("funding", 8), ("product", 14), ("tech_stack", 14), ("news", 14),
             ("pain_hypothesis", 9), ("headcount", 2.5)]
    sources = ["Local press", "LinkedIn (public profile)", "Company website", "Press release"]
    for j in range(1, k + 1):
        kind = wpick(ar, kinds)
        if kind == "funding" and a["industry"] != "Technology & Software":
            kind = "news"
        if kind == "headcount" and a["employee_count"] is None:
            kind = "news"
        city, country2 = ar.choice([cc for cc in CITY_COUNTRY if cc[1] != country[1]])
        name, trap, verified, source = a["name"], None, True, ar.choice(sources)
        text = {
            "expansion": f"{name} announced its expansion to {city}, {country2}.",
            "hiring": f"{name} posted {ar.randint(3, 25)} openings in finance and operations.",
            "funding": f"{name} closed a USD {ar.randint(2, 40)} million funding round.",
            "product": f"{name} launched {ar.choice(PRODUCTS)}.",
            "tech_stack": f"{name} runs {ar.choice(ERPS)} as its ERP.",
            "news": f"{name} was named one of the fastest-growing companies in {country[1]}.",
            "pain_hypothesis": "Hypothesis (inferred): reconciling travel and supplier expenses across several currencies likely means manual work.",
            "headcount": "",
        }[kind]
        observed = ctx.as_of - timedelta(days=ar.randint(5, 330))
        confidence = round(ar.uniform(0.7, 0.99), 2)
        if kind == "pain_hypothesis":
            verified, source, trap, confidence = False, "Internal inference", "unverified_hypothesis", round(ar.uniform(0.4, 0.6), 2)
        elif kind == "headcount":
            wrong = a["employee_count"] * ar.choice([6, 8, 10])
            text, trap = f"{name} has approximately {wrong} employees.", "contradicts_firmographics"
        u = ar.random()
        if trap is None and u < 0.20:
            observed, trap = ctx.as_of - timedelta(days=ar.randint(400, 900)), "stale"
        elif trap is None and u < 0.23 and len(ctx.names) > 5:
            other = ar.choice(ctx.names[:-1])
            text, trap = text.replace(name, other), "name_collision"
        fid = f"fct_{a['account_id'][4:]}_{j}"
        rows.append(({"fact_id": fid, "account_id": a["account_id"], "type": kind, "text": text,
                      "source_name": source, "source_url": f"https://{slug(source)}.example/{fid}",
                      "observed_at": iso(observed), "is_verified": verified, "confidence": confidence},
                     {"fact_id": fid, "account_id": a["account_id"], "trap": trap,
                      "usable_for_personalization": trap is None and verified}))
    return rows


def _international(facts):
    return any(f["type"] == "expansion" and f["is_verified"] for f, t in facts if t["trap"] is None)


# ---- mocks ------------------------------------------------------------------------------------------------
def _mock_enrichment(ar, a, contacts, ctx, country_lang):
    variant = wpick(ar, [("good_contacts", 65), ("no_data", 20), ("contradictory", 15)])
    payload = {"status": "ok", "firmographics": {}, "contacts": [], "warnings": []}
    if variant in ("good_contacts", "contradictory"):
        ind = next(i for i in INDUSTRIES if i[3])
        band, emp = _employees(ar, "icp")
        payload["firmographics"] = {"employee_count": emp, "industry": a["industry"] or ind[0],
                                    "revenue_band": _revenue_band(ar, emp)}
        for j in range(ar.randint(1, 3)):
            lang = _contact_lang(ar, country_lang)
            first, last = _person(ar, country_lang)
            func = wpick(ar, [("finance", 60), ("procurement", 20), ("executive", 20)])
            sen = ar.choice(["director", "vp", "c_level", "manager"])
            payload["contacts"].append({
                "first_name": first, "last_name": last, "email": _email_for(ar, ctx, "valid", first, last, a["domain"], set()),
                "email_status": "valid", "function": func, "seniority": sen, "language": lang,
                "title": ar.choice(TITLES[func][sen])})
    if variant == "contradictory":
        payload["firmographics"]["employee_count"] = int((a["employee_count"] or 100) * 12)
        payload["firmographics"]["is_customer"] = True
        payload["warnings"] = ["domain_mismatch", "conflicts_with_crm"]
    after = "contact" if variant == "good_contacts" else "escalate_human"
    return {"account_id": a["account_id"], "variant": variant, "response": payload,
            "expected_action_after_enrichment": after}


# ---- events ---------------------------------------------------------------------------------------------------
def _base_event(ctx, counter, type_, source, aid, cid, occurred, received, payload, ref, truth, flags=()):
    return {"type": type_, "source": source, "account_id": aid, "contact_id": cid, "occurred": occurred,
            "received": received, "payload": payload, "ref": ref, "truth": truth, "flags": list(flags),
            "event_id": "evt_" + sha(ctx.seed, "evt", counter)}


def _received(er, occurred):
    lat = min(er.expovariate(1 / 1.5), 30)
    delayed = er.random() < RATE_DELAYED
    if delayed:
        lat += er.uniform(600, 3 * 86400)
    return occurred + timedelta(seconds=lat), delayed


def build_world(seed: int, n: int, as_of=AS_OF) -> World:
    w = World()
    ctx = Ctx(seed, as_of)
    w.aes = _make_aes(rng(seed, "aes"))
    ctx.aes = w.aes
    for ae in w.aes:
        ctx.aes_by_country[ae["country"]].append(ae)
    scenarios = quota_assign(rng(seed, "scenarios"), n, SCENARIO_QUOTAS)
    meta = {}          # account_id -> dict(scenario, plan, country, lang)
    deferred = []

    # ---- accounts + dependent tables ----
    for i, sc in enumerate(scenarios, 1):
        if sc == "duplicate_domain":
            deferred.append(i)
            continue
        ar = rng(seed, f"a{i}")
        p = _plan(sc, ar)
        a, country = _make_account(ctx, i, sc, ar, p)
        _finish_account(w, ctx, ar, a, country, p, sc, meta)

    pool = [aid for aid, m in meta.items() if m["scenario"] in
            ("clean_prospect", "customer", "ae_assigned", "active_opportunity", "closed_lost_recent")]
    pool.sort()
    by_id = {a["account_id"]: a for a in w.accounts}
    contacts_by = defaultdict(list)
    for c in w.contacts:
        contacts_by[c["account_id"]].append(c)
    for i in deferred:
        ar = rng(seed, f"a{i}")
        p = _plan("duplicate_domain", ar)
        canon = by_id[ar.choice(pool)]
        a, country = _make_account(ctx, i, "duplicate_domain", ar, p)
        variant = ar.choice([canon["name"].upper(), canon["legal_name"].replace(".", "").replace(",", ""),
                             canon["name"] + " (Matriz)", canon["name"].replace("Group ", "")])
        a.update(name=variant, legal_name=variant, domain=canon["domain"], country=canon["country"],
                 industry=canon["industry"], employee_count=canon["employee_count"],
                 employee_band=canon["employee_band"], revenue_band=canon["revenue_band"],
                 created_at=iso(as_of - timedelta(days=ar.randint(1, 20))))
        _finish_account(w, ctx, ar, a, country, p, "duplicate_domain", meta, canon=canon,
                        canon_contacts=contacts_by[canon["account_id"]])
    w.accounts.sort(key=lambda x: x["account_id"])
    w.contacts.sort(key=lambda x: x["contact_id"])
    _finalize_aes(w, seed)
    w.calendar = _make_calendar(seed, w.aes, as_of)

    # ---- truth for accounts (scenario-derived) ----
    idx_contacts = defaultdict(list)
    for c in w.contacts:
        idx_contacts[c["account_id"]].append(c)
    supp_ids = defaultdict(set)
    for s in w.suppression:
        if s["scope"] == "contact":
            supp_ids[s["account_id"]].add(s["contact_id"])
    touches_by = defaultdict(list)
    for t in w.touches:
        touches_by[t["account_id"]].append(t)
    opps_by = defaultdict(list)
    for o in w.opportunities:
        opps_by[o["account_id"]].append(o)
    for a in w.accounts:
        m = meta[a["account_id"]]
        p = m["plan"]
        best = oracle.best_contact(idx_contacts[a["account_id"]], supp_ids[a["account_id"]]) if p["action"] == "contact" else None
        wait_until = None
        if p["action"] == "wait":
            if p["reasons"] == ["CLOSED_LOST_COOLDOWN"]:
                wait_until = iso(parse_iso(opps_by[a["account_id"]][0]["closed_at"]) + timedelta(days=LOST_COOLDOWN_DAYS))
            else:
                last = max(parse_iso(t["sent_at"]) for t in touches_by[a["account_id"]] if t["sender_type"] == "sequence")
                days = RECENT_OUTREACH_DAYS if p["reasons"] == ["RECENT_OUTREACH"] else SEQUENCE_COOLDOWN_DAYS
                wait_until = iso(last + timedelta(days=days))
        action, reasons = p["action"], p["reasons"]
        route, route_reason = (oracle.route_ae(a, None, w.aes) if action == "handoff_ae" else (None, None))
        if action == "handoff_ae" and route is None:   # nobody can take it -> a human routes it
            action, reasons = "escalate_human", ["NO_AE_AVAILABLE"]
        w.truth_accounts.append({
            "account_id": a["account_id"], "scenario": m["scenario"], "sub": p["sub"],
            "expected_action": action, "reason_codes": reasons, "best_contact_id": best,
            "wait_until": wait_until, "route_to_ae_id": route, "route_reason": route_reason,
            "duplicate_of": m.get("duplicate_of"), "race": p["race"], "crm_state": m["crm_state"]})

    _build_events(w, ctx, meta, idx_contacts, touches_by, opps_by)
    w.arms = impact.assign_arms(seed, w.accounts, set(touches_by))
    w.sim = impact.simulate_outcomes(seed, w.accounts, w.truth_accounts, w.arms)
    return w


def _finish_account(w, ctx, ar, a, country, p, sc, meta, canon=None, canon_contacts=None):
    aid = a["account_id"]
    lang = country[2]
    a["list_id"] = f"tl_2026_10_w{wpick(ar, list(zip(range(1, 6), LIST_WEIGHTS)))}"
    contacts = _make_contacts(ar, ctx, a, p, lang)
    if canon is not None:
        shared = next((c for c in canon_contacts if c["email_status"] == "valid"), None)
        if shared:  # the same person appears under both duplicate accounts
            contacts.append({**shared, "contact_id": f"con_{aid[4:]}_{len(contacts) + 1}", "account_id": aid})
    if p["status"] == "customer" and p["opps"]:
        a["customer_since"] = None
    opps = _make_opps(ar, ctx, a, p)
    if p["status"] == "customer" and opps:
        a["customer_since"] = opps[0]["closed_at"][:10]
    if "churned_days_ago" in p["extra"]:
        a["customer_since"] = opps[0]["closed_at"][:10]
        a["churned_at"] = iso(ctx.as_of - timedelta(days=p["extra"]["churned_days_ago"]))[:10]
    will_reply = p["reply_p"] > 0 and ar.random() < p["reply_p"] and bool(contacts)
    if will_reply and sc != "recent_outreach":
        p["touches"] = p["touches"] + [(ar.randint(3, 25), 2, p["reply_sender"])]
    touches = _make_touches(ar, ctx, a, contacts, p, lang)
    supp = _make_suppression(ctx, a, contacts, p, ar)
    facts = _make_facts(ar, ctx, a, country)
    a["international_signal"] = _international(facts)
    w.accounts.append(a)
    w.contacts.extend(contacts)
    w.opportunities.extend(opps)
    w.touches.extend(touches)
    w.suppression.extend(supp)
    for f, t in facts:
        w.facts.append(f)
        w.truth_facts.append(t)
    mr = rng(ctx.seed, f"m{aid}")
    w.mock_behavior.append({"account_id": aid, "enrichment": wpick(mr, ENRICH_BEHAVIORS),
                            "send": wpick(mr, SEND_BEHAVIORS), "calendar": wpick(mr, CALENDAR_BEHAVIORS),
                            "crm": wpick(mr, CRM_BEHAVIORS)})
    if p["action"] == "enrich":
        w.mock_enrichment.append(_mock_enrichment(mr, a, contacts, ctx, lang))
    meta[aid] = {"scenario": sc, "plan": p, "country": country, "lang": lang, "will_reply": will_reply,
                 "crm_state": SCENARIO_STATE.get(sc, "prospect"),
                 "duplicate_of": canon["account_id"] if canon else None}


def _build_events(w, ctx, meta, idx_contacts, touches_by, opps_by):
    seed, as_of = ctx.seed, ctx.as_of
    base: list[dict] = []
    counter = 0
    ta = {t["account_id"]: t for t in w.truth_accounts}
    ae_by_id = {ae["ae_id"]: ae for ae in w.aes}
    calendar_behaviour = {m["account_id"]: m["calendar"] for m in w.mock_behavior}

    def add(*args, **kw):
        nonlocal counter
        counter += 1
        ev = _base_event(ctx, counter, *args, **kw)
        base.append(ev)
        return ev

    for a in w.accounts:
        aid = a["account_id"]
        m = meta[aid]
        p = m["plan"]
        er = rng(seed, f"e{aid}")
        tr = ta[aid]
        contacts = idx_contacts[aid]
        k = int(a["list_id"][-1]) - 1
        occ = as_of + timedelta(days=LIST_DROP_DAYS[k], seconds=er.uniform(0, LIST_BURST_MINUTES * 60))
        recv, delayed = _received(er, occ)
        truth_t = {"expected_action": tr["expected_action"], "reason_codes": tr["reason_codes"],
                   "best_contact_id": tr["best_contact_id"], "wait_until": tr["wait_until"],
                   "handling": "process", "scenario": m["scenario"]}
        targeted = add("account_targeted", "list_import", aid, None, occ, recv,
                       {"list_id": a["list_id"], "origin": a["source"]}, a["list_id"], truth_t,
                       ["delayed"] if delayed else [])
        # ---- race conditions (events whose occurred_at precedes targeting but arrive after it) ----
        if p["race"] == "late_opportunity":
            ae = er.choice([x for x in w.aes if x["country"] == a["country"]] or w.aes)["ae_id"]
            lag = er.uniform(1, 30) * 3600
            add("opportunity_created", "crm", aid, None, occ - timedelta(hours=er.uniform(1, 6)),
                targeted["received"] + timedelta(seconds=lag),
                {"opportunity_id": f"opp_{aid[4:]}_race", "stage": "discovery", "owner_ae_id": ae},
                f"opp_{aid[4:]}_race",
                {"expected_action": "suppress", "reason_codes": ["ACTIVE_OPPORTUNITY"], "handling": "process_and_reconcile",
                 "cancel_pending_outreach": True, "scenario": m["scenario"]}, ["race"])
        if p["race"] == "late_unsubscribe":
            cid = tr["best_contact_id"]
            add("unsubscribe_received", "email_provider", aid, cid, occ - timedelta(hours=er.uniform(1, 5)),
                targeted["received"] + timedelta(hours=er.uniform(6, 40)),
                {"method": "link_click", "thread_id": None}, f"unsub_{cid}",
                {"expected_action": "suppress", "reason_codes": ["UNSUBSCRIBED"], "handling": "process_and_reconcile",
                 "scope": "contact", "contact_id": cid, "cancel_pending_outreach": True, "scenario": m["scenario"]},
                ["race"])
        # ---- replies ----
        ts = touches_by[aid]
        replied = False
        if m["will_reply"] and ts:
            replied = True
            touch = max(ts, key=lambda t: t["sent_at"])
            contact = next(c for c in contacts if c["contact_id"] == touch["contact_id"])
            occ_r = as_of + timedelta(seconds=er.uniform(1800, (STREAM_DAYS - 1) * 86400))
            recv_r, delayed_r = _received(er, occ_r)
            seed_row = pick_seed(er, pick_label(er), contact["language"])
            ref_first, ref_last = _person(er, m["lang"])
            ref_dom = a["domain"] if er.random() < 0.7 else f"group-{a['domain']}"
            ctxr = {"first": contact["first_name"], "full": f"{contact['first_name']} {contact['last_name']}",
                    "title": contact["title"], "company": a["name"], "occurred": occ_r,
                    "ref": (f"{ref_first} {ref_last}", f"{slug(ref_first)}.{slug(ref_last)}@{ref_dom}"),
                    "crm_state": m["crm_state"]}
            rep = render_reply(seed_row, ctxr, er)
            if rep["expected_action"] == "handoff_ae":
                rep["route_to_ae_id"], rep["route_reason"] = oracle.route_ae(a, contact["language"], w.aes)
                if rep["route_to_ae_id"] is None:
                    rep["expected_action"] = "escalate_human"
            msg_id = "msg_" + sha(seed, "msg", aid, touch["touch_id"])
            ev = add("reply_received", "email_provider", aid, contact["contact_id"], occ_r, recv_r,
                     {"thread_id": touch["thread_id"], "message_id": msg_id, "in_reply_to_touch_id": touch["touch_id"],
                      "from_email": contact["email"], "subject": "Re: " + touch["subject"], "body_text": rep["text"]},
                     msg_id,
                     {"expected_action": rep["expected_action"], "reason_codes": [rep["label"].upper()],
                      "handling": "process", "label": rep["label"], "scenario": m["scenario"],
                      **({"route_to_ae_id": rep["route_to_ae_id"], "route_reason": rep["route_reason"]}
                         if "route_to_ae_id" in rep else {})},
                     ["delayed"] if delayed_r else [])
            w.truth_replies.append({"event_id": ev["event_id"], "account_id": aid, "contact_id": contact["contact_id"],
                                    **{k2: rep[k2] for k2 in ("seed_id", "label", "language", "difficulty", "is_ambiguous",
                                                              "extracted", "expected_action", "unsafe_actions",
                                                              "needs_human_review")},
                                    "crm_state": m["crm_state"],
                                    "route_to_ae_id": rep.get("route_to_ae_id"), "route_reason": rep.get("route_reason")})
            if rep["label"] == "interested" and m["crm_state"] not in ("customer", "churned_customer") \
                    and er.random() < 0.35:
                occ_m = ev["occurred"] + timedelta(seconds=er.uniform(3600, 3 * 86400))
                recv_m, _ = _received(er, occ_m)
                add("meeting_booked", "calendar", aid, contact["contact_id"], occ_m, recv_m,
                    {"calendar_event_id": "cal_" + sha(seed, "cal", aid), "attendee_email": contact["email"],
                     "start_at": iso(occ_m + timedelta(days=er.uniform(2, 10)))},
                    "cal_" + sha(seed, "cal", aid),
                    {"expected_action": "handoff_ae", "reason_codes": ["MEETING_BOOKED"], "handling": "process",
                     "scenario": m["scenario"],
                     **dict(zip(("route_to_ae_id", "route_reason"), oracle.route_ae(a, contact["language"], w.aes)))})
                if base[-1]["truth"]["route_to_ae_id"] is None:
                    base[-1]["truth"].update(expected_action="escalate_human", reason_codes=["NO_AE_AVAILABLE"])
                elif calendar_behaviour.get(aid) == "slot_conflict":
                    # An injected failure, on purpose: the calendar answers 409 "slot taken" and the contract says never
                    # double-book, escalate (mock_api_contracts.json, golden G078). Handling it is the expected outcome.
                    base[-1]["truth"].update(expected_action="escalate_human", reason_codes=["CALENDAR_CONFLICT"],
                                             injected_failure="calendar_slot_conflict")
        # ---- deterministic provider events (no AI) ----
        if m["scenario"] == "recent_outreach" and not replied and ts:
            u = er.random()
            touch = max(ts, key=lambda t: t["sent_at"])
            occ_e = as_of + timedelta(seconds=er.uniform(0, (STREAM_DAYS - 1) * 86400))
            recv_e, delayed_e = _received(er, occ_e)
            if u < 0.012:
                add("unsubscribe_received", "email_provider", aid, touch["contact_id"], occ_e, recv_e,
                    {"method": er.choice(["link_click", "list_unsubscribe_header"]), "thread_id": touch["thread_id"]},
                    f"unsub_{touch['contact_id']}",
                    {"expected_action": "suppress", "reason_codes": ["UNSUBSCRIBED"], "handling": "process",
                     "scope": "contact", "contact_id": touch["contact_id"], "scenario": m["scenario"]},
                    ["delayed"] if delayed_e else [])
            elif u < 0.042:
                hard = er.random() < 0.6
                add("email_bounced", "email_provider", aid, touch["contact_id"], occ_e, recv_e,
                    {"bounce_type": "hard" if hard else "soft", "smtp_code": "550" if hard else "452",
                     "thread_id": touch["thread_id"]}, f"bounce_{touch['touch_id']}",
                    {"expected_action": "suppress" if hard else "wait",
                     "reason_codes": ["HARD_BOUNCE" if hard else "SOFT_BOUNCE"], "handling": "process",
                     "scope": "contact", "contact_id": touch["contact_id"], "scenario": m["scenario"]},
                    ["delayed"] if delayed_e else [])
        if m["scenario"] == "active_opportunity" and er.random() < 0.30:
            opp = opps_by[aid][0]
            order = list(OPEN_STAGES) + ["closed_lost"]
            frm = order.index(opp["stage"])
            to = "closed_lost" if (er.random() < 0.10 or frm == len(OPEN_STAGES) - 1) else order[frm + 1]
            occ_o = as_of + timedelta(seconds=er.uniform(0, (STREAM_DAYS - 1) * 86400))
            recv_o, delayed_o = _received(er, occ_o)
            add("opportunity_stage_changed", "crm", aid, None, occ_o, recv_o,
                {"opportunity_id": opp["opportunity_id"], "from_stage": opp["stage"], "to_stage": to,
                 "owner_ae_id": opp["owner_ae_id"]}, f"{opp['opportunity_id']}:{to}",
                {"expected_action": "update_state", "reason_codes": ["OPP_STAGE_CHANGED"], "handling": "process",
                 "scenario": m["scenario"]}, ["delayed"] if delayed_o else [])
    _perturb(w, ctx, base)


def _envelope(ev, delivery_id, event_id=None, key=None, received=None, source=None):
    source = source or ev["source"]
    return {"delivery_id": delivery_id, "event_id": event_id or ev["event_id"],
            "idempotency_key": key or sha(ev["source"], ev["type"], ev["account_id"], ev["contact_id"], ev["ref"], n=16),
            "type": ev["type"], "schema_version": SCHEMA_VERSION, "source": source,
            "account_id": ev["account_id"], "contact_id": ev["contact_id"], "occurred_at": iso(ev["occurred"]),
            "received_at": iso(received or ev["received"]), "payload": ev["payload"]}


def _perturb(w, ctx, base):
    seed = ctx.seed
    pr = rng(seed, "perturb")
    rows = []   # (envelope, truth)
    dcount = 0

    def new_delivery():
        nonlocal dcount
        dcount += 1
        return "dlv_" + sha(seed, "dlv", dcount, n=14)

    for ev in base:
        env = _envelope(ev, new_delivery())
        base_truth = dict(ev["truth"])
        pert = "delayed" if "delayed" in ev["flags"] else ("out_of_order_race" if "race" in ev["flags"] else "none")
        handling = base_truth.pop("handling")
        rows.append((env, {"delivery_id": env["delivery_id"], "event_id": env["event_id"], "type": ev["type"],
                           "account_id": ev["account_id"], "perturbation": pert, "expected_handling": handling,
                           **base_truth}))
        u = pr.random()
        if u < RATE_EXACT_DUP:
            d = copy.deepcopy(env)
            d["delivery_id"] = new_delivery()
            d["received_at"] = iso(ev["received"] + timedelta(seconds=pr.uniform(1, 1800)))
            rows.append((d, {"delivery_id": d["delivery_id"], "event_id": d["event_id"], "type": ev["type"],
                             "account_id": ev["account_id"], "perturbation": "exact_duplicate",
                             "expected_handling": "ignore_duplicate", "duplicate_of": env["delivery_id"]}))
        elif u < RATE_EXACT_DUP + RATE_SEMANTIC_DUP:
            d = copy.deepcopy(env)
            d["delivery_id"] = new_delivery()
            d["event_id"] = "evt_" + sha(seed, "sem", d["delivery_id"])
            d["received_at"] = iso(ev["received"] + timedelta(seconds=pr.uniform(30, 7200)))
            rows.append((d, {"delivery_id": d["delivery_id"], "event_id": d["event_id"], "type": ev["type"],
                             "account_id": ev["account_id"], "perturbation": "semantic_duplicate",
                             "expected_handling": "ignore_duplicate", "duplicate_of": env["delivery_id"]}))
        elif u < RATE_EXACT_DUP + RATE_SEMANTIC_DUP + RATE_CONTENT_DUP and ev["type"] in ("account_targeted", "reply_received"):
            d = copy.deepcopy(env)
            d["delivery_id"] = new_delivery()
            d["event_id"] = "evt_" + sha(seed, "cnt", d["delivery_id"])
            d["source"] = "crm" if ev["source"] != "crm" else "email_provider"
            d["idempotency_key"] = sha(d["source"], d["type"], d["account_id"], d["contact_id"], ev["ref"], "mirror", n=16)
            d["received_at"] = iso(ev["received"] + timedelta(seconds=pr.uniform(5, 3600)))
            rows.append((d, {"delivery_id": d["delivery_id"], "event_id": d["event_id"], "type": ev["type"],
                             "account_id": ev["account_id"], "perturbation": "content_duplicate",
                             "expected_handling": "dedupe_by_content", "duplicate_of": env["delivery_id"]}))
        if pr.random() < RATE_MALFORMED:
            d = copy.deepcopy(env)
            d["delivery_id"] = new_delivery()
            d["event_id"] = "evt_" + sha(seed, "bad", d["delivery_id"])
            kinds = ["missing_account_id", "invalid_timestamp", "unknown_type", "payload_wrong_type", "null_payload",
                     "unsupported_schema", "account_not_found"]
            kind = pr.choice(kinds)
            if ev["type"] == "reply_received" and pr.random() < 0.4:
                kind = "oversized_text"
            if kind == "missing_account_id":
                d["account_id"] = None
            elif kind == "invalid_timestamp":
                d["occurred_at"] = "2026-13-45T99:00:00Z"
            elif kind == "unknown_type":
                d["type"] = "lead_scored"
            elif kind == "payload_wrong_type":
                d["payload"] = "not-an-object"
            elif kind == "null_payload":
                d["payload"] = None
            elif kind == "unsupported_schema":
                d["schema_version"] = "9.9"
            elif kind == "account_not_found":
                d["account_id"] = "acc_999999"
            else:
                d["payload"]["body_text"] = "x" * 25000
            d["idempotency_key"] = sha("bad", d["delivery_id"], n=16)
            d["received_at"] = iso(ev["received"] + timedelta(seconds=pr.uniform(1, 600)))
            rows.append((d, {"delivery_id": d["delivery_id"], "event_id": d["event_id"], "type": d["type"],
                             "account_id": ev["account_id"], "perturbation": "malformed", "malformed_kind": kind,
                             "expected_handling": "dead_letter", "expected_action": None}))
    rows.sort(key=lambda r: (r[0]["received_at"], r[0]["delivery_id"]))
    # The key per account is a snapshot; events delivered earlier in the stream can change that state. Re-derive the
    # expectation of any account_targeted that follows such a change (see generator/stateful.py).
    w.stateful_corrections = stateful.correct_truth(w, rows)
    for env, truth in rows:
        w.events.append(env)
        w.truth_events.append(truth)
