"""Evidence for the web page's "Live Demo" and "Operations" views, produced by the real engine.

flows_payload():      five curated end-to-end runs, each broken into the seven stages
                      event -> state -> rules -> model -> validator -> action -> audit trail
operations_payload(): the whole 50k world's event stream through one engine instance, reduced to operating metrics

Model answers are an offline fixture (a deterministic stand-in), except for the deliberate "model mistake" in the guard
flow, which is a simulated answer. Both are labelled as such in the payload.
"""
from __future__ import annotations

import copy
import json
import math
from collections import Counter
from datetime import timedelta
from pathlib import Path

import hashlib

from . import db, plain, scenario, scoring, state
from .db import rows
from .ai import draft as ai_draft
from .ai.fixture import FixtureLLM, reply_output
from .engine import SENDERS, Orchestrator
from .rules import decide
from .windows import next_send_time
from .policy import SEED_DIR, Policy
from .timeutil import iso, parse

ROOT = Path(__file__).resolve().parent.parent
GENERATED = ROOT / "data" / "generated"
GUARD_TEXT = "I'm interested, but don't email me again."
RECORDED = "Recorded run of the real engine. The model's answer is an offline fixture (a deterministic stand-in), not a live call."
BROKEN = ("Recorded run of the real engine. The model's answer is a SIMULATED defect: we hand the engine a cut-off answer taken from the recorded "
          "suite, because a strong model rarely breaks like this. The live button asks the real model to read the same reply.")
SIMULATED = ("Recorded run of the real engine. The model's answer is a SIMULATED mistake: we hand the engine an \"interested\" answer, "
             "because a strong model usually gets this reply right. Use the live button to see what Claude really says.")

# (flow id, title, one-line story, golden scenario, event index, label, group)
FLOWS = [
    ("F0", "A new target gets an outreach email", "The draft waits as pending approval, a scripted demo reviewer approves it, and only then it lands in the simulated log. Nothing is sent.", "G040", 0, RECORDED),
    ("F1", "A prospect says they are interested", "The happy path: the reply is read, checked, and the account goes to a sales exec.", "G068", 0, RECORDED),
    ("F2", "\"I'm interested, but don't email me again\"", "The model reads the interest. The rules read the opt-out. The rules win.", "G064", 0, SIMULATED),
    ("F3", "The same webhook arrives twice", "The second delivery is recognised and ignored: no second email.", "G040", 1, RECORDED),
    ("F4", "The email API fails once", "A 503 is retried safely: exactly one email is recorded.", "G070", 0, RECORDED),
    ("F5", "A reply too vague to act on", "The model labels it ambiguous, so nothing happens automatically: a person decides.", "G067", 0, RECORDED),
    ("F6", "The CRM fails once (503)", "The write is retried with the same key: exactly one task lands in the CRM.", "G110", 0, RECORDED),
    ("F7", "The CRM answers \"unknown\"", "The system does not guess: it reads back by key before writing again, so nothing is written twice.", "G111", 0, RECORDED),
    ("F8", "The CRM record changed meanwhile (409)", "The decision is re-made on the fresh state before the system writes.", "G112", 0, RECORDED),
    ("F9", "A weaker-fit company is added", "The score sends it to the slow nurture track: no email and no AI call, only the enrolment is recorded.", "G040", 0, RECORDED),
    ("F10", "A strong-fit company is added", "Verified facts exist, so the AI writes the opening line. The validator checks it, and the draft waits for a person.", "G080", 0, RECORDED),
    ("F11", "A current customer is targeted", "The rules stop it before anything else: a customer is never prospected.", "G003", 0, RECORDED),
    ("F12", "The company has an open deal", "A deal in negotiation belongs to its sales exec: no outreach from the SDR side.", "G006", 0, RECORDED),
    ("F13", "The account owner is on leave", "The sales exec who owns the account is away, so the engine routes it to the backup.", "G101", 0, RECORDED),
    ("F14", "An unsubscribe arrives a day late", "The opt-out happened before the email was drafted but arrived after it. The draft waiting for approval is cancelled.", "G047", 1, RECORDED),
    ("F15", "The AI answers with broken output", "The model's answer is cut off, twice. The system does not guess: a person reads the reply.", "G068", 0, BROKEN),
]
NO_SETTLE = {"F14"}                                  # the draft must still be waiting when the late event arrives
# The golden companies are all alike (120 employees, no facts, a plain 30 points), which makes a demo look empty. For the demo each
# scenario keeps its golden's mock behaviour (the CRM that fails, the webhook that repeats) but is lent a company from the committed
# sample world, with its own facts and contact. `WHO` says which kind of company each scenario needs.
SAMPLE = SEED_DIR / "sample"
WHO = {"F10": {"tier": "A", "facts": 2}, "F0": {"tier": "B", "facts": 1}, "F4": {"tier": "B", "facts": 0}, "F8": {"tier": "B", "facts": 1},
       "F6": {"tier": "B", "facts": 1}, "F7": {"tier": "B", "facts": 1}, "F9": {"tier": "C", "facts": 1},
       "F1": {"tier": "A", "emp": (100, 140)}, "F2": {"tier": "B"}, "F5": {"tier": "B"}}
WHO["F3"] = WHO["F0"]
WHO.update({"F11": {"tier": "B"}, "F12": {"tier": "B"}, "F13": {"tier": "B"}, "F14": {"tier": "B", "facts": 1}, "F15": {"tier": "B"}})
BROKEN_TEXT = "Thanks, but this quarter we're closing our budget. Write to me after November 24."
IDENTITY = ("name", "legal_name", "domain", "country", "industry", "employee_count", "employee_band", "revenue_band", "international_signal")
_WORLD: dict = {}


def _world() -> dict:
    if not _WORLD:
        by = lambda f, k: {r[k]: r for r in _jsonl(SAMPLE / f)}
        facts, contacts = {}, {}
        for f in _jsonl(SAMPLE / "company_facts.jsonl"):
            facts.setdefault(f["account_id"], []).append(f)
        for c in _jsonl(SAMPLE / "contacts.jsonl"):
            contacts.setdefault(c["account_id"], []).append(c)
        _WORLD.update(accounts=_jsonl(SAMPLE / "accounts.jsonl"), facts=facts, contacts=contacts)
    return _WORLD


def _lend(fid: str, taken: set) -> tuple[dict, list, dict] | None:
    """A sample-world prospect that fits what scenario `fid` needs: its tier, how many usable facts, an English-speaking contact."""
    w, need, cfg, now, pol = _world(), WHO[fid], scoring.load_config(), parse("2026-10-01T16:00:00Z"), Policy.load()
    for a in sorted(w["accounts"], key=lambda x: x["account_id"]):
        if (a["account_id"] in taken or a["crm_status"] != "prospect" or a["industry"] in pol.non_icp_industries
                or (a["employee_count"] or 0) < pol.icp_min_employees):
            continue
        fs = w["facts"].get(a["account_id"], [])
        c = next((c for c in w["contacts"].get(a["account_id"], []) if c["language"] == "en" and c["email_status"] == "valid"), None)
        lo, hi = need.get("emp", (0, 10**9))
        if (c is None or scoring.score(scoring.features(a, fs, cfg), cfg)["tier"] != need["tier"] or not lo <= (a["employee_count"] or 0) <= hi
                or len(ai_draft.usable_facts(fs, a, now)) < need.get("facts", 0)
                or ("facts" in need and need["facts"] == 0 and ai_draft.usable_facts(fs, a, now))):
            continue
        return a, fs, c
    return None


def _lend_all() -> dict:
    taken, out = set(), {}
    for fid in ["F10", "F0", "F4", "F8", "F6", "F7", "F9", "F1", "F2", "F5", "F11", "F12", "F13", "F14", "F15"]:
        a, fs, c = _lend(fid, taken)
        out[fid] = (a, fs, c)
        taken.add(a["account_id"])
    out["F3"] = out["F0"]
    return out


def _overlay(g: dict, who) -> None:
    a, fs, c = who
    mine = g["state"]["accounts"][0]
    for k in IDENTITY:
        mine[k] = a[k]
    g["state"]["company_facts"] = [{**f, "account_id": mine["account_id"], "fact_id": f"{mine['account_id']}_{f['fact_id']}"} for f in fs]
    k = g["state"]["contacts"][0]
    for f in ("first_name", "last_name", "email", "title", "function", "seniority", "language"):
        k[f] = c[f]


CRM_FLOWS = {"F6", "F7", "F8"}
COORDINATION = [
    {"system": "AI", "reads": "The prospect's own words (quoted thread removed) and verified company facts",
     "writes": "Nothing. It only proposes: a label, extracted facts, an opening line",
     "guard": "Structured output, a validator, and rules that choose the action. It can never pick an action, an AE or who may be emailed"},
    {"system": "CRM", "reads": "Account and contact state, deals, owner, suppression, plus inbound webhooks (deal created, stage changed, meeting booked)",
     "writes": "A decision note, or a task for the sales exec",
     "guard": "One idempotency key per write; retry with the same key; read back before retrying an uncertain result; re-read and re-decide on a version conflict"},
    {"system": "Outreach", "reads": "The decision and the approved template, never the AI's free text",
     "writes": "The outreach email (first or follow-up): drafted, held as pending approval, and recorded in a simulated log only after a person approves it (nothing is sent)",
     "guard": "The draft is held until a named person approves it; eligibility is re-checked at approval and again at send time, plus send window, daily cap and suppression. The executor refuses to send anything that was not approved"},
]
CALL_TEXT = {200: "ok", 503: "temporary error", 429: "rate limited, wait and retry", 409: "conflict: the record changed since it was read"}


def _short(d: dict) -> str:
    return ", ".join(f"{k}: {v}" for k, v in d.items() if v not in (None, "", [], {}))[:220]


def _stage(sid, name, status, headline, lines=()):
    return {"id": sid, "name": name, "status": status, "headline": headline, "lines": list(lines)}


ROUTE_TEXT = {"OWNER": "the account owner", "OWNER_BACKUP": "the owner is away, so the backup", "TERRITORY": "the sales exec for its country with the most room",
              "TERRITORY_FALLBACK": "no sales exec in its country has room, so one from another country"}
REVIEWER = "demo reviewer (scripted stand-in for a person)"


def _settle(orch, ev) -> list[dict]:
    """The recorded runs include the human step: whatever the engine is holding is approved by a scripted reviewer, which is
    labelled as such everywhere it shows. Without it the engine would (correctly) stop at `pending_approval`."""
    return [orch.approve(p["key"], REVIEWER, now=parse(ev["received_at"])) for p in orch.pending_approvals()]


class _CutOffLLM:
    """Answers every reply with the same cut-off output from the recorded suite (what a model that stops mid-sentence would return)."""
    mode, model = "fixture", "fixture"

    def __init__(self):
        rec = next(r for r in _jsonl(SEED_DIR / "llm_recordings.jsonl") if r["recording_id"] == "EV-R-NOW-01:truncated_json")
        self.raw, self.calls = rec["model_output_raw"], []

    def run(self, system, user, tool, max_tokens=700):
        from .ai.llm import LLMResponse
        self.calls.append(tool["name"])
        return LLMResponse(self.raw, self.model, self.mode)


def _card(orch, aid: str) -> dict:
    """The company as the demo shows it: the CRM mock record, and the score that picks its track."""
    a, cfg = orch._account(aid), orch.score_cfg
    facts = rows(orch.conn, "SELECT * FROM company_facts WHERE account_id=?", (aid,))
    feat = scoring.features(a, facts, cfg)
    sc = scoring.score(feat, cfg)
    n = lambda q: orch.conn.execute(q, (aid,)).fetchone()[0]
    w = cfg["weights"]
    people = rows(orch.conn, "SELECT * FROM contacts WHERE account_id=? ORDER BY contact_id", (aid,))
    who = next((c for c in people if c["language"] == "en" and c["email_status"] == "valid"), people[0] if people else None)
    return {"name": a["name"], "contact": {"first": who["first_name"], "name": f'{who["first_name"]} {who["last_name"]}', "title": who["title"]} if who else None, "country": a["country"], "industry": a["industry"], "employees": a["employee_count"],
            "crm": {"status": a["crm_status"], "owner_ae": a["crm_owner_ae_id"], "contacts": n("SELECT COUNT(*) FROM contacts WHERE account_id=?"),
                    "open_deals": n("SELECT COUNT(*) FROM opportunities WHERE account_id=? AND closed_at IS NULL"),
                    "emails_so_far": n("SELECT COUNT(*) FROM outreach_history WHERE account_id=? AND sender_type='automated'"),
                    "suppressed": n("SELECT COUNT(*) FROM suppression WHERE account_id=?") > 0,
                    "drafts_waiting": n("SELECT COUNT(*) FROM actions WHERE account_id=? AND kind='email' AND status='pending_approval'")},
            "score": sc["score"], "tier": sc["tier"], "parts": sc["parts"], "version": cfg["version"],
            "size": feat["size"], "pain": feat["pain_text"], "signals": feat["signals"],
            "tier_a": cfg["tier_a"], "tier_b": cfg["tier_b"], "max": w["size"] + w["pain"] + cfg["signal_cap"] * w["signal_each"],
            "signal_each": w["signal_each"], "signal_cap": cfg["signal_cap"],
            "facts": [{"text": f["text"], "source": f["source_name"], "seen": f["observed_at"][:10], "type": f["type"],
                       "why_not": _why_not(f, a, parse("2026-10-01T16:00:00Z"))} for f in facts]}


def _run(g: dict, idx: int, llm=None, who=None, settle_prior=True):
    aid0 = g["events"][0]["account_id"]
    if who:
        _overlay(g, who)
    orch = scenario.build(g, llm)
    orch.score_cfg = {**orch.score_cfg, "gate_enabled": True}
    aid = aid0
    for ev in g["events"][:idx]:
        orch.process(ev)
        if settle_prior:
            _settle(orch, ev)
    ev, v0, n0 = g["events"][idx], state.version(orch.conn, aid), len(orch.mocks.calls)
    card = _card(orch, aid)
    res = orch.process(ev)
    for out in _settle(orch, ev):
        res.effects.append({"system": "approval", "kind": "approved", "reviewer": REVIEWER})
        if out["status"] == "scheduled":
            res.effects.append({"system": "send", "kind": "email_scheduled", "send_after": out["send_after"]})
        else:
            res.effects.append({"system": "send", "kind": "email", "status": "ok" if out["status"] == "sent_mock" else out["status"],
                                "attempts": out.get("attempts", 1)})
        res.email["status"] = out["status"]
    return orch, ev, res, v0, state.version(orch.conn, aid), orch.mocks.calls[n0:], card


def _compose(fid, title, story, label, orch, ev, res, v0, v1, calls=(), card=None) -> dict:
    r, aid = res.to_dict(), ev["account_id"]
    dup = res.handling in ("ignore_duplicate", "dedupe_by_content")
    trail = [a for a in orch.audit.trail(account_id=aid)
             if (a["delivery_id"] == ev["delivery_id"] if dup else a["event_id"] == ev["event_id"])]
    reply = ev["type"] == "reply_received"
    ai = r.get("ai") or {}
    act = plain.ACTIONS.get(res.final_action or res.action or "", [res.action or "no action", ""])
    final = res.action
    st = []
    text = (ev.get("payload") or {}).get("body_text")
    late = (parse(ev["received_at"]) - parse(ev["occurred_at"])).total_seconds() / 3600
    st.append(_stage("event", "Event comes in", "done", plain.EVENTS.get(ev["type"], ev["type"]),
                     [f'Delivery {ev["delivery_id"]}, source {ev.get("source", "?")}'] + ([f'"{text.strip()}"'] if text else [])
                     + ([f"Out of order: it happened {late:.0f} hours before it arrived, after the email draft was made"] if late > 1 else [])))
    if dup:
        st.append(_stage("state", "State changes", "skipped", f"Already processed: state stays at version {v0}", ["The delivery id was seen before."]))
        st.append(_stage("rules", "Rules decide", "skipped", "Nothing to decide: this is a duplicate"))
        st.append(_stage("model", "Claude interprets", "skipped", "No AI call needed"))
        st.append(_stage("validator", "Validator checks", "skipped", "Nothing to validate"))
        st.append(_stage("action", "Action is recorded", "done", "Ignored as a duplicate: no second effect", ["No second email, no second CRM note."]))
    else:
        changed = {"reply_received": "The reply is stored and the earlier outreach is marked as replied.",
                   "account_targeted": "The company enters the pipeline and its eligibility is evaluated."}.get(ev["type"], "The new fact is applied to the account.")
        st.append(_stage("state", "State changes", "done", f"Account state version {v0} to {v1}", [changed]))
        if reply:
            crm = ai.get("crm_state", "prospect").replace("_", " ")
            st.append(_stage("rules", "Rules decide", "done", f"The account is a {crm}: prospecting rules apply",
                             ["Rules fix which actions are allowed for this account. The model cannot widen them."]))
        else:
            codes = [plain.CODES.get(c, c) for c in res.reason_codes]
            head = "Eligible: passed every check" if res.action == "nurture" else f"Decision: {plain.ACTIONS.get(res.action, [res.action])[0]}"
            st.append(_stage("rules", "Rules decide", "done", head,
                             codes + (["Then the company's score picked the slow track instead of an email."] if res.action == "nurture" else [])
                             + ["Deterministic policy: no AI involved in this decision."]))
        if ai.get("used_ai") or (ai and ai.get("task") == "draft" and ai.get("used_ai")):
            if reply and ai.get("label") is None:
                st.append(_stage("model", "Claude interprets", "done", "Claude's answer came back cut off",
                                 [f'Source of this answer: {"a simulated defect (a cut-off answer from the recorded suite)" if fid == "F15" else "an offline stand-in for the model"}.',
                                  "The system asked once more and got the same defect. It only proposes; it never picks the action."]))
            elif reply:
                conf = f' ({ai["confidence"]})' if ai.get("confidence") is not None else ""
                st.append(_stage("model", "Claude interprets", "done", f'Claude reads the reply as "{ai.get("label")}"{conf}',
                                 [f'Source of this answer: {"a simulated model mistake" if fid == "F2" else "an offline stand-in for the model (not a live call)"}.', "It only proposes: label and extracted facts. It never picks the action."]))
            else:
                st.append(_stage("model", "Claude interprets", "done", "Claude drafts an opening line from verified facts", ["It may restate a verified fact, nothing else."]))
        else:
            why = "No verified fact to mention, so the generic template is used." if ai.get("task") == "draft" else "Nothing to interpret here: the rules decide alone."
            st.append(_stage("model", "Claude interprets", "skipped", "No AI call needed", [why]))
        if ai and ai.get("used_ai"):
            codes = ai.get("codes") or []
            head = ("The rules overrode the model" if "G001" in codes else
                    "Rejected twice: the output never matched the required structure" if ai.get("verdict") == "reject_retry" else
                    "Accepted: every claim cites a verified fact about the company" if ai.get("task") == "draft" and ai.get("verdict") == "accept"
                    else plain.VERDICTS.get(ai.get("verdict"), ai.get("verdict")))
            st.append(_stage("validator", "Validator checks", "done" if ai.get("verdict") in ("accept", "accept_with_warning") else "flag", head,
                             [plain.VALIDATION_CODES.get(c, c) for c in codes] or ["No violations."]))
        else:
            st.append(_stage("validator", "Validator checks", "skipped", "Nothing to validate", []))
        eff = []
        for e in r.get("effects") or []:
            if e.get("system") == "approval":
                eff.append("Draft held as pending approval: it cannot be sent until a person approves it" if e.get("kind") == "held"
                           else f'Approved by {e.get("reviewer")}. In this recorded run the reviewer is scripted; in real use it is a person, named in the audit trail')
            elif e.get("system") == "send":
                n = e.get("attempts", 1)
                eff.append("Email recorded in the simulated log" + (f" after {n} attempts: the first call failed with a temporary error and was retried safely" if n > 1 else "") + ". Nothing is sent.")
            elif e.get("system") == "crm":
                eff.append("CRM note written" if e.get("kind") == "decision_note" else "Task created for the sales exec")
            elif e.get("system") == "nurture":
                eff.append("Enrolled in the slow nurture track")
        if r.get("cancel_pending_outreach"):
            eff.append("Pending outreach to this contact cancelled")
        if res.action == "suppress" and reply:
            eff.append("Contact added to the suppression list")
        if r.get("needs_human_review") or res.action == "escalate_human":
            eff.append("Queued for a person to review")
        if r.get("route_to_ae_id"):
            eff.append(f'Routed to sales exec {r["route_to_ae_id"]}: {ROUTE_TEXT.get(r.get("route_reason"), r.get("route_reason"))}')
        kinds = {a["kind"] for a in trail}
        if "crm_conflict_reread" in kinds:
            eff.insert(0, "The CRM said the record changed (409): the system re-read it and re-decided on the fresh state before writing")
        for a in trail:
            if a["kind"] == "reconcile_lookup":
                eff.insert(0, "The CRM said 200 but the outcome was unknown: the system read back by idempotency key (applied: "
                              + ("yes" if a["detail"].get("applied") else "no") + ") before writing again")
        st.append(_stage("action", "Action is recorded", "done", f'Final action: {act[0]}', eff or [act[1]]))
    st.append(_stage("audit", "Audit trail", "done", f"{len(trail)} steps written", [f'{a["kind"].replace("_", " ")}' for a in trail]))
    log = [{"system": c[0], "op": c[1], "key": str(c[2]), "status": c[3], "text": CALL_TEXT.get(c[3], str(c[3]))} for c in calls]
    for i, c in enumerate(log):
        if c["op"] == "lookup" and i:
            applied = next((a["detail"].get("applied") for a in trail if a["kind"] == "reconcile_lookup"), False)
            log[i - 1]["text"] = "200, but the outcome is unknown"
            c["text"] = "read back by idempotency key: " + ("already applied" if applied else "not applied, safe to write once")
    out = {"id": fid, "title": title, "story": story, "label": label, "stages": st, "group": "crm" if fid in CRM_FLOWS else "core", "calls": log,
           "ledger": {k: list(v) for k, v in orch.mocks.ledger.items() if v},
           "audit": [{"kind": a["kind"], "ts": a["ts"], "detail": _short(a["detail"])} for a in trail],
           "final": {"action": res.final_action or res.action, "codes": list(res.reason_codes), "text": act[0]}, "account": card}
    if fid == "F2":
        out["contradiction"] = {"text": text, "claude_label": ai.get("label"), "claude_confidence": ai.get("confidence"),
                                "guard": "G001" in (ai.get("codes") or []), "final_action": res.action}
    return out


def flows_payload() -> dict:
    golden = {g["id"]: g for g in scenario.load_golden()}
    out = []
    lent = _lend_all()
    for fid, title, story, gid, idx, label in FLOWS:
        g, llm = copy.deepcopy(golden[gid]), None
        if fid == "F2":
            g["events"][0]["payload"]["body_text"] = GUARD_TEXT
            llm = FixtureLLM({GUARD_TEXT: reply_output("interested", GUARD_TEXT, {"interest_level": "high"}, 0.93)})
        if fid == "F15":
            g["events"][0]["payload"]["body_text"] = BROKEN_TEXT
            llm = _CutOffLLM()
        orch, ev, res, v0, v1, calls, card = _run(g, idx, llm, lent[fid], settle_prior=fid not in NO_SETTLE)
        out.append(_compose(fid, title, story, label, orch, ev, res, v0, v1, calls, card))
    return {"label": RECORDED, "flows": out, "guard_text": GUARD_TEXT, "coordination": COORDINATION, "plain": {"actions": plain.ACTIONS}}


# ------------------------------------------------------------------------------------------------------------------
# Leads: twelve companies from the sample world (four per group), each run through the real engine as a new target, so the page
# can show a lead the way a person would read it: the score and why, what the rules decided, the CRM record and the opening line.
LEAD_TIERS = {"A": 4, "B": 4, "C": 4}


def _why_not(f: dict, a: dict, now) -> str | None:
    if not f["is_verified"]:
        return "not verified"
    if now - parse(f["observed_at"]) > timedelta(days=365):
        return "older than a year"
    if a["name"] not in f["text"]:
        return "not about this company"
    return None if ai_draft.usable_facts([f], a, now) else "clashes with the CRM"


def _lead(aid: str, w: dict, sample: dict, golden: dict, now) -> dict:
    a = next(x for x in w["accounts"] if x["account_id"] == aid)
    g = copy.deepcopy(golden["G040"])
    g["state"] = {"accounts": [a], "contacts": w["contacts"].get(aid, []), "company_facts": w["facts"].get(aid, []),
                  **{n: [r for r in rs if r["account_id"] == aid] for n, rs in sample.items()}}
    g["events"] = g["events"][:1]
    g["events"][0]["account_id"] = aid
    orch = scenario.build(g)
    orch.score_cfg = {**orch.score_cfg, "gate_enabled": True}
    card = _card(orch, aid)
    res = orch.process(g["events"][0])
    held = orch.pending_approvals()
    final = res.final_action or res.action
    contact = card["contact"]
    facts = card["facts"]
    act = plain.ACTIONS.get(final, [final, ""])
    return {"id": aid, "account": card, "contact": {"name": contact["name"], "title": contact["title"]},
            "facts": facts, "decision": {"action": final, "text": act[0], "what": act[1], "codes": [plain.CODES.get(c, c) for c in res.reason_codes]},
            "draft": ({"step": held[0]["step"], "subject": held[0]["subject"], "body": held[0]["body"], "ai": held[0]["mode"] != "generic" and bool(held[0]["claims"]),
                       "claims": held[0]["claims"]} if held else None),
            "usable": sum(1 for f in facts if not f["why_not"]), "audit": [x["kind"] for x in orch.audit.trail(account_id=aid)]}


def leads_payload() -> dict:
    """Twelve leads, four per group. Three of each group follow the normal path (A and B: an outreach email is prepared; C: nurture) and
    one shows another outcome the rules can reach (waiting, needs better data, do not contact), so the page shows more than the happy path."""
    w, now, cfg, pol = _world(), parse("2026-10-01T16:00:00Z"), scoring.load_config(), Policy.load()
    golden = {g["id"]: g for g in scenario.load_golden()}
    sample = {n: _jsonl(SAMPLE / f"{n}.jsonl") for n in ("opportunities", "outreach_history", "suppression")}
    skip = {x[0]["account_id"] for x in _lend_all().values()}
    main, other, countries = {t: [] for t in "ABC"}, {t: None for t in "ABC"}, {}
    for a in sorted(w["accounts"], key=lambda x: x["account_id"]):
        aid, fs = a["account_id"], w["facts"].get(a["account_id"], [])
        if (aid in skip or a["crm_status"] != "prospect" or a["industry"] in pol.non_icp_industries or (a["employee_count"] or 0) < pol.icp_min_employees
                or not any(c["language"] == "en" and c["email_status"] == "valid" for c in w["contacts"].get(aid, []))):
            continue
        t = scoring.score(scoring.features(a, fs, cfg), cfg)["tier"]
        if len(main[t]) >= 3 and other[t] is not None:
            if all(len(main[x]) >= 3 and other[x] is not None for x in "ABC"):
                break
            continue
        if t != "C" and not ai_draft.usable_facts(fs, a, now):
            continue
        lead = _lead(aid, w, sample, golden, now)
        normal = lead["decision"]["action"] == ("nurture" if t == "C" else "contact")
        if normal and len(main[t]) < 3 and countries.get((t, a["country"]), 0) < 2:
            main[t].append(lead)
            countries[(t, a["country"])] = countries.get((t, a["country"]), 0) + 1
        elif not normal and other[t] is None:
            other[t] = lead
    return {"label": "Each lead below is a synthetic company from the sample world, run through the real engine as a new target. The model's answer is an offline fixture; nothing is sent.",
            "leads": [l for t in "ABC" for l in main[t] + [other[t]] if l]}


# ------------------------------------------------------------------------------------------------------------------
REPLAY_POINTS = 300
BUCKET_OF = {"wait": "wait", "enrich": "lookup", "handoff_ae": "sales", "escalate_human": "review", "suppress": "blocked"}
HANDLING_GROUP = {"ignore_duplicate": "dup", "dedupe_by_content": "dup", "retry_then_process": "retry", "reconcile_before_retry": "retry",
                  "process_and_reconcile": "retry", "reread_and_reevaluate": "retry", "dead_letter": "dead", "dead_letter_and_alert": "dead"}


def replay_payload(world: Path = GENERATED) -> dict:
    """The month as the engine saw it, in the order the events arrived, for the Command Center's "Play the month".

    Each company is counted once, when its first event arrives, under the decision the engine takes for it (the same decisions the
    Command Center totals show). The event counters (duplicates, retries, dead letters) come from the real run of the whole stream."""
    cfg, policy, now = scoring.load_config(), Policy.load(), parse("2026-10-01T16:00:00Z")
    conn = db.connect()
    db.load_world_dir(conn, world)
    facts = {}
    for f in db.rows(conn, "SELECT * FROM company_facts ORDER BY fact_id"):
        facts.setdefault(f["account_id"], []).append(f)
    outcome = {}
    for a in db.rows(conn, "SELECT * FROM accounts ORDER BY account_id"):
        d = decide(conn, a["account_id"], now, policy)
        if d.action == "contact":
            tier = scoring.score(scoring.features(a, facts.get(a["account_id"], []), cfg), cfg)["tier"]
            outcome[a["account_id"]] = "nurture" if tier == "C" and cfg["gate_enabled"] else "email"
        else:
            outcome[a["account_id"]] = BUCKET_OF.get(d.action, "wait")
    conn.close()
    _, events, _, res = _stream_run(world)
    cols = ["events", "day", "email", "nurture", "wait", "lookup", "sales", "review", "blocked", "dup", "retry", "dead"]
    cnt, seen, rows_, step = Counter(), set(), [], max(1, math.ceil(len(events) / REPLAY_POINTS))
    for i, (e, r) in enumerate(zip(events, res), 1):
        aid = e["account_id"]
        if aid in outcome and aid not in seen:
            seen.add(aid)
            cnt[outcome[aid]] += 1
        g = HANDLING_GROUP.get(r.handling)
        if g:
            cnt[g] += 1
        if i % step == 0 or i == len(events):
            rows_.append([i, e["received_at"][:10]] + [cnt[c] for c in cols[2:]])
    for aid, b in outcome.items():                              # a company with no event of its own still gets counted at the end
        if aid not in seen:
            cnt[b] += 1
    rows_[-1] = [rows_[-1][0], rows_[-1][1]] + [cnt[c] for c in cols[2:]]
    return {"label": "Recorded run of the real engine over the whole month: the 55,959 events of the 50,000 synthetic companies, in the order they "
                     "arrived (model answers: an offline fixture). Nothing is sent.",
            "cols": cols, "rows": rows_, "total_events": len(events), "accounts": len(outcome)}


# ------------------------------------------------------------------------------------------------------------------
def _jsonl(p: Path):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


HUMAN = {"handoff_ae", "escalate_human"}


def _stream_run(world: Path, score_version: str | None = None):
    """The world's whole event stream through one engine instance (offline fixture model), with a given scoring version."""
    conn = db.connect()
    db.load_world_dir(conn, world)
    events = _jsonl(world / "events.jsonl")
    by_id = {e["event_id"]: e for e in events}
    answers = {by_id[r["event_id"]]["payload"]["body_text"]: reply_output(r["label"], by_id[r["event_id"]]["payload"]["body_text"], r.get("extracted"))
               for r in _jsonl(world / "truth" / "truth_replies.jsonl") if r["event_id"] in by_id}
    orch = Orchestrator(conn, llm=FixtureLLM(answers), as_of=parse("2026-10-01T16:00:00Z"), score_version=score_version)
    return conn, events, orch, [orch.process(e) for e in events]


def operations_payload(world: Path = GENERATED) -> dict:
    """The whole event stream of the 50k world through one engine instance, as operating metrics."""
    manifest = json.loads((world / "manifest.json").read_text(encoding="utf-8"))
    conn, events, orch, res = _stream_run(world)
    handling = Counter(r.handling for r in res)
    decided = [r for r in res if r.action and r.handling in ("process", "retry_then_process", "process_and_reconcile", "reread_and_reevaluate", "reconcile_before_retry")]
    final = Counter((r.final_action or r.action) for r in decided)
    human = sum(n for a, n in final.items() if a in HUMAN)
    q = lambda sql: conn.execute(sql).fetchone()[0]
    ai_total = q("SELECT COUNT(*) FROM ai_calls")
    verdicts = Counter(r[0] for r in conn.execute("SELECT verdict FROM ai_calls"))
    failed = sum(n for v, n in verdicts.items() if v in ("reject_retry", "reject_escalate", "llm_unavailable", "escalate_low_confidence"))
    guard = q("SELECT COUNT(*) FROM ai_calls WHERE violation_codes LIKE '%G001%'")
    retried = sum(1 for r in res if r.handling in ("retry_then_process", "reconcile_before_retry"))
    time = json.loads((SEED_DIR / "time_assumptions.json").read_text(encoding="utf-8"))
    return {"label": "The full event stream of the 50,000-account world through the real engine, offline fixture model, nothing sent: outreach emails stop at pending approval.",
            "n_accounts": manifest["n_accounts"], "events": len(events), "decided": len(decided),
            "handling": dict(handling), "final_actions": {str(k): v for k, v in final.items()},
            "automated": len(decided) - human, "human": human,
            "review_queue": q("SELECT COUNT(*) FROM review_queue"), "dead_letters": q("SELECT COUNT(*) FROM dead_letters"),
            "duplicates_prevented": handling["ignore_duplicate"] + handling["dedupe_by_content"],
            "ai": {"calls": ai_total, "by_kind": dict(Counter(r[0] for r in conn.execute("SELECT kind FROM ai_calls"))), "verdicts": dict(verdicts),
                   "failed": failed, "opt_out_guard": guard,
                   "cost_per_call_usd": time["ai_cost_usd_per_call"], "cost_usd": round(ai_total * time["ai_cost_usd_per_call"], 2)},
            "unsafe_prevented": {"ineligible_not_contacted": sum(1 for r in decided if r.action in ("suppress", "wait", "escalate_human") and r.handling != "retry_then_process"),
                                 "late_events_reconciled": handling["process_and_reconcile"],
                                 "opt_out_guard_overrides": guard,
                                 "ai_outputs_rejected": verdicts.get("reject_retry", 0) + verdicts.get("reject_escalate", 0)},
            "retried_then_ok": retried, "emails_held_for_approval": len(orch.pending_approvals()),
            "emails_in_simulated_log": len(orch.mocks.ledger["send"]), "real_emails_sent": 0}


# ------------------------------------------------------------------------------------------------------------------
BATCH = 200


def approvals_payload(world: Path = GENERATED, batch: int = BATCH) -> dict:
    """The approval queue: a batch of the prepared outreach emails, first or follow-up (tiers A and B), drawn deterministically from all 50,000 accounts.

    The totals are exact for the whole world; the page loads `batch` drafts to review. Each draft is held by the real engine
    as `pending_approval` (written with the offline stand-in model, which restates the first verified fact exactly). The
    decisions made on the page stay in the browser; releasing a draft in the engine takes `Orchestrator.approve()`."""
    cfg, policy, now = scoring.load_config(), Policy.load(), parse("2026-10-01T16:00:00Z")
    conn = db.connect()
    db.load_world_dir(conn, world)
    facts = {}
    for f in db.rows(conn, "SELECT * FROM company_facts ORDER BY fact_id"):
        facts.setdefault(f["account_id"], []).append(f)
    queue = {"A": [], "B": []}
    touches = Counter(r["account_id"] for r in db.rows(conn, "SELECT account_id FROM outreach_history WHERE sender_type='sequence'"))
    by_step = Counter()
    for a in db.rows(conn, "SELECT * FROM accounts ORDER BY account_id"):
        d = decide(conn, a["account_id"], now, policy)
        if d.action != "contact":
            continue
        feat = scoring.features(a, facts.get(a["account_id"], []), cfg)
        sc = scoring.score(feat, cfg)
        if sc["tier"] != "C":
            queue[sc["tier"]].append((a, d, feat, sc))
            by_step[1 + touches.get(a["account_id"], 0)] += 1       # the email's step in the sequence (the engine uses the same count)
    total = sum(len(v) for v in queue.values())
    take = {"A": round(batch * len(queue["A"]) / total)}
    take["B"] = batch - take["A"]
    # The batch is what the REAL engine holds: an account_targeted event goes through it for each picked company, and the
    # engine drafts the email and parks it as pending approval (no approver is configured, so nothing can be released).
    orch = Orchestrator(conn, policy, llm=FixtureLLM(), as_of=now)
    picked = []
    for tier in ("A", "B"):
        picked += [(a, d, feat, sc) for a, d, feat, sc in
                   sorted(queue[tier], key=lambda x: hashlib.sha256(x[0]["account_id"].encode()).hexdigest())[: take[tier]]]
    for a, *_ in picked:
        aid = a["account_id"]
        orch.process({"delivery_id": f"dlv_aq_{aid}", "event_id": f"evt_aq_{aid}", "idempotency_key": f"aq:{aid}",
                      "type": "account_targeted", "schema_version": "1.0", "source": "approval_queue_batch",
                      "account_id": aid, "contact_id": None, "occurred_at": iso(now), "received_at": iso(now),
                      "payload": {"list_id": "tl_approval_queue", "origin": "target_list"}})
    held = {p["account_id"]: p for p in orch.pending_approvals()}
    assert len(held) == len(picked) and not orch.mocks.ledger["send"], "the engine must hold every draft and send none"
    items = []
    for a, d, feat, sc in picked:
        contact = db.one(conn, "SELECT * FROM contacts WHERE contact_id=?", (d.best_contact_id,))
        p = held[a["account_id"]]
        sender = SENDERS[int(hashlib.sha256(a["account_id"].encode()).hexdigest(), 16) % len(SENDERS)]
        tz = policy.send["timezones"][a["country"]]
        local = parse(p["send_after"]) + __import__("datetime").timedelta(hours=tz["utc_offset_hours"])
        items.append({"id": a["account_id"], "name": a["name"], "country": a["country"], "industry": a["industry"],
                      "employees": a["employee_count"], "tier": sc["tier"], "score": sc["score"], "parts": sc["parts"],
                      "contact": {"name": f'{contact["first_name"]} {contact["last_name"]}', "title": contact["title"], "email": contact["email"]},
                      "subject": p["subject"], "body": p["body"], "mode": p["mode"], "claims": [c["text"] for c in p["claims"]],
                      "signals": feat["signals"][:3], "sender": sender, "step": p["step"], "engine_key": p["key"], "engine_status": "pending_approval",
                      "window": f'{local.strftime("%a %d %b, %H:%M")} local time ({tz["iana"]})'})
    items.sort(key=lambda x: (-x["score"], x["id"]))
    return {"label": "Computed by the real engine on the 50,000-account world. Each draft below is held by the engine as pending approval; decisions on this page stay in your browser and nothing is ever sent.",
            "as_of": "2026-10-01T16:00:00Z", "total_prepared": total, "by_tier": {t: len(v) for t, v in queue.items()},
            "by_step": {str(k): v for k, v in sorted(by_step.items())}, "follow_ups": total - by_step[1],
            "batch": len(items), "personalized": sum(1 for i in items if i["mode"] == "personalized"), "items": items}


# ------------------------------------------------------------------------------------------------------------------
def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson interval for a rate of k events in n trials."""
    if n == 0:
        return 0.0, 1.0
    p, d = k / n, 1 + z * z / n
    c, a = p + z * z / (2 * n), z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - a) / d, (c + a) / d


def measurement_payload(world: Path = GENERATED, aa_runs: int = 40) -> dict:
    """The measurement plan's worked example as data, from the SIMULATED experiment (assumptions in funnel_assumptions.json)."""
    from generator import impact
    from generator.impact import per_1000
    from generator.util import read_jsonl

    accounts, truth = read_jsonl(world / "accounts.jsonl"), read_jsonl(world / "truth" / "truth_accounts.jsonl")
    arms, sim = read_jsonl(world / "experiment_assignments.jsonl"), read_jsonl(world / "experiment_sim_outcomes.jsonl")
    seed = json.loads((world / "manifest.json").read_text(encoding="utf-8"))["seed"]
    T, C, A = [r for r in sim if r["arm"] == "treatment"], [r for r in sim if r["arm"] == "control"], impact.ASSUMPTIONS
    rate = lambda rows, f, d=None: sum(r[f] for r in [x for x in rows if (x[d] if d else True)]) / max(1, len([x for x in rows if (x[d] if d else True)]))
    funnel = [{"stage": label, "control": round(per_1000(C, f), 1), "treatment": round(per_1000(T, f), 1)}
              for label, f in (("Contacted", "contacted"), ("Delivered", "delivered"), ("Replied", "replied"),
                               ("Positive reply", "positive_reply"), ("Sales-qualified (SQL)", "sql"), ("Opportunity", "opportunity"))]
    diff, lo, hi = impact.bootstrap_diff(T, C, "pipeline_usd", seed=1)
    g = A["guardrails"]
    guard = []
    for name, f, lim in (("Unsubscribe rate", "unsubscribed", g["unsubscribe_rate_max"]), ("Spam complaint rate", "complaint", g["spam_complaint_rate_max"]),
                         ("Hard bounce rate", "hard_bounce", g["hard_bounce_rate_max"])):
        t, c = rate(T, f, "contacted"), rate(C, f, "contacted")
        kt, nt = sum(r[f] for r in T if r["contacted"]), sum(1 for r in T if r["contacted"])
        kc, nc = sum(r[f] for r in C if r["contacted"]), sum(1 for r in C if r["contacted"])
        lo_t, hi_t = _wilson(kt, nt)
        # A single rate is not enough to call a guardrail broken: with few events the 95% interval can include the limit.
        guard.append({"name": name, "control": f"{c:.2%}", "treatment": f"{t:.2%}", "limit": f"{lim:.2%} or less", "status": "ok" if t <= lim else "breach",
                      "limit_pct": round(100 * lim, 3), "events": {"control": kc, "treatment": kt}, "contacted": {"control": nc, "treatment": nt},
                      "ci_pct": [round(100 * lo_t, 3), round(100 * hi_t, 3)],
                      "evidence": "go" if hi_t < lim else ("stop" if lo_t > lim else "hold")})
    vt, vc = sum(r["violation"] for r in T), sum(r["violation"] for r in C)
    guard.append({"name": "Contacted an ineligible account", "control": str(vc), "treatment": str(vt), "limit": "0 in treatment", "status": "ok" if vt == 0 else "breach",
                  "events": {"control": vc, "treatment": vt}, "evidence": "go" if vt == 0 else "stop"})
    pc = sum(r["sql"] for r in C) / len(C)
    power = [{"lift": f"+{l:.0%}", "per_arm": impact.sample_size_two_proportions(pc, pc * (1 + l)),
              "months": round(impact.sample_size_two_proportions(pc, pc * (1 + l)) / 25_000, 1)} for l in (0.10, 0.25, 0.50, 1.00, 2.00)]
    rej = 0
    for k in range(aa_runs):
        aa = impact.simulate_outcomes(seed + 1000 + k, accounts, truth, arms, effect={})
        t_, c_ = [r for r in aa if r["arm"] == "treatment"], [r for r in aa if r["arm"] == "control"]
        rej += impact.exact_rate_p_value(sum(r["sql"] for r in t_), len(t_), sum(r["sql"] for r in c_), len(c_)) < 0.05
    ue = A["unit_economics_usd"]
    cost = lambda rows, extra: (sum(r["sdr_minutes"] for r in rows) / 60 * ue["sdr_cost_per_hour"] + extra) / max(1, sum(r["sql"] for r in rows))
    return {"label": "SIMULATED. Every number comes from the assumptions in funnel_assumptions.json, not from Clara's data. It shows how the impact would be measured, not that it exists.",
            "design": {"unit": A["unit"], "control": A["arms"]["control"], "treatment": A["arms"]["treatment"],
                       "assignment": "Stratified by country, company size and prior contact; 50/50 by a deterministic hash",
                       "analysis": A["design"]["analysis"], "attribution_days": A["design"]["attribution_window_days"], "weeks": A["design"]["duration_weeks"]},
            "primary_metric": "Qualified pipeline (USD accepted by a sales exec) created within 60 days, per 1,000 targeted accounts",
            "leading_metrics": ["Positive-reply rate", "Meetings booked per 1,000 targeted", "Hours to first touch", "Share of eligible accounts reached"],
            "funnel": funnel,
            "pipeline": {"control": round(per_1000(C, "pipeline_usd")), "treatment": round(per_1000(T, "pipeline_usd")), "diff": round(diff), "lo": round(lo), "hi": round(hi),
                         "includes_zero": lo <= 0 <= hi},
            "coverage": {"control": round(rate([r for r in C if r["eligible"]], "contacted"), 2), "treatment": round(rate([r for r in T if r["eligible"]], "contacted"), 2)},
            "reply_rate_assumed": {"control": A["control"]["p_reply"], "treatment": A["treatment"]["p_reply"]},
            "guardrails": guard,
            "cost_per_sql": {"control": round(cost(C, 0)), "treatment": round(cost(T, len(T) * (ue["llm_cost_per_account"] + ue["enrichment_cost_per_account"])))},
            "baseline_sql_rate": round(pc, 5), "power": power, "aa": {"runs": aa_runs, "false_positives": int(rej)},
            "arms": {"control": len(C), "treatment": len(T)}}


# ------------------------------------------------------------------------------------------------------------------
# (requirement, where it is shown on the site as "hash|label", proof in the repository)
MAP = [
    ("build", "1. An event / webhook trigger", "flows|Architecture (diagram)", "orchestrator/ingest.py"),
    ("build", "2. Persistent account and contact state", "account|Decisions > Account trace", "orchestrator/db.py"),
    ("build", "3. Eligibility and next-best-action logic", "automation|Decisions > Automation", "orchestrator/rules.py"),
    ("build", "4. At least one meaningful LLM capability", "live|AI & Safety > Try a reply", "orchestrator/ai/reply.py"),
    ("build", "5. Structured, validated AI output", "run|Live Demo > the moment of truth", "orchestrator/ai/validate.py"),
    ("build", "6. An external action or mock integration", "run|Live Demo > CRM coordination", "orchestrator/mocks.py"),
    ("build", "7. Duplicate / idempotency protection", "run|Live Demo > the same webhook arrives twice", "tests/test_engine.py"),
    ("build", "8. One realistic failure / retry scenario", "run|Live Demo > the email API fails once", "orchestrator/retry.py"),
    ("build", "9. Automated tests for critical logic", "flows|Architecture (this table)", "tests/test_engine.py"),
    ("build", "10. A small AI evaluation suite", "evals|AI & Safety > Evaluation", "data/seed/eval_cases.jsonl"),
    ("demo", "A successful flow", "run|Live Demo > a prospect says they are interested", "data/seed/golden_scenarios.jsonl"),
    ("demo", "A duplicate event", "run|Live Demo > the same webhook arrives twice", "data/seed/golden_scenarios.jsonl"),
    ("demo", "A failure scenario", "run|Live Demo > the email API fails once, and the three CRM cases", "data/seed/golden_scenarios.jsonl"),
    ("demo", "An ambiguous or unsafe AI case", "run|Live Demo > the moment of truth, and the vague reply", "data/seed/llm_recordings.jsonl"),
    ("ai", "Why AI was appropriate for that decision", "live|AI & Safety > Try a reply", "docs/AI.md"),
    ("ai", "What AI may and may not decide", "overview|Command Center > How it works", "docs/AI.md"),
    ("ai", "How its output is validated", "evals|AI & Safety > Evaluation", "orchestrator/ai/validate.py"),
    ("ai", "How ambiguity and low confidence are handled", "run|Live Demo > a reply too vague to act on", "docs/AI.md"),
    ("ai", "What must be true before it runs autonomously", "ops|Operations > Metrics (known risk)", "docs/AI.md"),
    ("ai", "Where AI is deliberately not used", "automation|Decisions > Automation", "docs/DECISION_LOG.md"),
    ("impact", "How incremental qualified pipeline would be measured", "overview|Command Center > Measuring impact", "docs/MEASUREMENT_PLAN.md"),
    ("impact", "The funnel, the experiment, the primary metric and the guardrails", "overview|Command Center > Measuring impact", "docs/MEASUREMENT_PLAN.md"),
    ("prod", "Reliability, security, observability, scale, build vs buy", "ops|Operations > Metrics", "docs/PRODUCTION.md"),
    ("deliver", "A runnable repository with setup instructions", "flows|Architecture", "README.md"),
    ("deliver", "Sample and mock data", "flows|Architecture", "data/seed/mock_api_contracts.json"),
    ("deliver", "AI evaluation results", "evals|AI & Safety > Evaluation", "evals/results/latest-recorded.json"),
    ("deliver", "One architecture diagram", "flows|Architecture (diagram)", "README.md"),
    ("deliver", "A short measurement and experiment plan", "overview|Command Center > Measuring impact", "docs/MEASUREMENT_PLAN.md"),
    ("deliver", "Decision log: what was not built, where AI was not used, the main tradeoff, the biggest production risk", "flows|Architecture", "docs/DECISION_LOG.md"),
]
GROUPS_MAP = {"build": "What to build", "demo": "The demo must include", "ai": "Be prepared to explain (AI)", "impact": "Business impact",
              "prod": "Production thinking", "deliver": "Deliverables"}


def challenge_map_payload() -> dict:
    rows = []
    for g, req, where, proof in MAP:
        view, label = where.split("|")
        rows.append({"group": g, "requirement": req, "view": view, "where": label, "proof": proof})
    return {"groups": GROUPS_MAP, "rows": rows}


# ------------------------------------------------------------------------------------------------------------------
def scoring_compare_payload(world: Path = GENERATED, versions: list[str] | None = None) -> dict:
    """What changing the scoring does, measured by the real engine: every official version runs the same world.

    Per version: ready companies by tier (rules + score on every account), then the whole event stream through the engine
    for outcomes (outreach emails prepared, nurture enrolments, AI calls and their estimated cost). Per pair: who changes group."""
    manifest = json.loads((world / "manifest.json").read_text(encoding="utf-8"))
    raw = json.loads((SEED_DIR / "scoring_policy.json").read_text(encoding="utf-8"))
    ids = versions or [v["id"] for v in raw["versions"]]
    cfgs = {i: scoring.load_config(i) for i in ids}
    policy, now = Policy.load(), parse("2026-10-01T16:00:00Z")
    snap = db.connect()
    db.load_world_dir(snap, world)
    facts = {}
    for f in db.rows(snap, "SELECT * FROM company_facts"):
        facts.setdefault(f["account_id"], []).append(f)
    ready = []
    for a in db.rows(snap, "SELECT * FROM accounts ORDER BY account_id"):
        if decide(snap, a["account_id"], now, policy).action == "contact":
            ready.append(scoring.features(a, facts.get(a["account_id"], []), cfgs[ids[0]]))
    tier = {i: [scoring.score(f, cfgs[i])["tier"] for f in ready] for i in ids}
    cost = json.loads((SEED_DIR / "time_assumptions.json").read_text(encoding="utf-8"))["ai_cost_usd_per_call"]
    runs = {}
    for i in ids:
        conn, events, orch, res = _stream_run(world, i)
        q = lambda sql: conn.execute(sql).fetchone()[0]
        draft, reply = q("SELECT COUNT(*) FROM ai_calls WHERE kind='draft'"), q("SELECT COUNT(*) FROM ai_calls WHERE kind='reply'")
        versions_logged = {json.loads(r[0]).get("version") for r in conn.execute("SELECT detail FROM audit_log WHERE kind='score'")}
        runs[i] = {"ready_by_tier": dict(Counter(tier[i])), "nurture_enrolled": q("SELECT COUNT(*) FROM audit_log WHERE kind='nurture_enrolled'"),
                   "emails_prepared": q("SELECT COUNT(*) FROM audit_log WHERE kind='draft'"), "emails_held_for_approval": len(orch.pending_approvals()), "draft_calls": draft, "reply_calls": reply,
                   "ai_cost_usd": round((draft + reply) * cost, 2), "versions_in_audit": sorted(v for v in versions_logged if v)}
    pairs = []
    for x in range(len(ids)):
        for y in range(x + 1, len(ids)):
            a, b = ids[x], ids[y]
            matrix = Counter(f"{ta}{tb}" for ta, tb in zip(tier[a], tier[b]))
            pairs.append({"from": a, "to": b, "ready": len(ready), "matrix": dict(matrix), "moved": sum(n for k, n in matrix.items() if k[0] != k[1]),
                          "delta": {k: runs[b][k] - runs[a][k] for k in ("nurture_enrolled", "emails_prepared", "draft_calls", "ai_cost_usd")}})
    names = {v["id"]: v["name"] for v in raw["versions"]}
    return {"label": "Computed by running the whole event stream of the 50,000-account world through the real engine once per version (offline fixture model, nothing sent).",
            "dataset": f'synthetic-{manifest["n_accounts"] // 1000}k-seed{manifest["seed"]}',
            "versions": [{"id": i, "name": names[i]} for i in ids], "ready": len(ready), "runs": runs, "pairs": pairs,
            "command": f"python3 -m orchestrator compare-scoring {ids[0]} {ids[-1]}"}


def scoring_compare_markdown(p: dict) -> str:
    out = [f"# Scoring comparison on {p['dataset']}", "", p["label"], "", "| version | Top priority | Standard | Nurture | outreach emails prepared | nurture enrolled | AI calls | est. AI cost |", "|---|---|---|---|---|---|---|---|"]
    for v in p["versions"]:
        r = p["runs"][v["id"]]
        t = r["ready_by_tier"]
        out.append(f"| {v['id']} {v['name']} | {t.get('A', 0):,} | {t.get('B', 0):,} | {t.get('C', 0):,} | {r['emails_prepared']:,} | {r['nurture_enrolled']:,} | {r['draft_calls'] + r['reply_calls']:,} | USD {r['ai_cost_usd']:,.2f} |")
    for q in p["pairs"]:
        d = q["delta"]
        out += ["", f"## {q['from']} to {q['to']}: {q['moved']:,} of {q['ready']:,} ready companies change group", "",
                f"- outreach emails prepared: {d['emails_prepared']:+,}; nurture enrolled: {d['nurture_enrolled']:+,}; draft AI calls: {d['draft_calls']:+,}; est. AI cost: USD {d['ai_cost_usd']:+,.2f}",
                "- who moves (rows: " + q['from'] + ", columns: " + q['to'] + "): " + ", ".join(f"{k[0]}→{k[1]} {n:,}" for k, n in sorted(q["matrix"].items()) if k[0] != k[1])]
    return "\n".join(out) + "\n"
