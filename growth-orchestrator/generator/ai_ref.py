"""AI output contracts + REFERENCE validators.

Purpose: (1) define exactly what a model is allowed to return, (2) give every recorded model output
(data/seed/llm_recordings.jsonl) an expected verdict, and (3) let tests prove those expectations are right by running
this independent reference. The production validator can be stricter or differently structured, but it must reproduce
the verdicts in the recordings.

Design rules encoded here:
  * the model proposes; deterministic code disposes: the action always comes from the validated *label* via a fixed map
    (the model's `suggested_action` is advisory and never trusted);
  * every extracted fact must be supported by the reply text; every drafted claim must cite a usable fact_id;
  * opt-out wording is enforced by a deterministic guard no matter what the model says;
  * anything doubtful degrades safely: replies -> escalate_human, drafts -> generic approved template.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime

from .policy_data import SEND_POLICY, UNSUBSCRIBE_FOOTER
from .replies import MONTHS, OPT_OUT_LABELS
from .reply_seeds import LABEL_ACTION, NULL_Q

CONFIDENCE_MIN = SEND_POLICY["ai_autonomy"]["confidence_threshold_auto_act"]
ACTIONS = ["contact", "wait", "enrich", "escalate_human", "handoff_ae", "suppress", "no_action"]
INTEREST = ["high", "medium", "low", "none", "unclear"]
CURRENT = ["bank_cards", "spreadsheets", "other_fintech", "erp_module", "manual_process"]
PAINS = ["reimbursements", "reconciliation", "spend_control", "travel", "multi_currency", "suppliers"]
BUDGET = ["has_budget", "no_budget"]
REPLY_FIELDS = ["label", "confidence", "interest_level", "follow_up_date", "referred_contact", "qualification",
                "suggested_action", "needs_human_review", "evidence"]
QUAL_FIELDS = list(NULL_Q)
DRAFT_FIELDS = ["language", "subject", "body", "claims", "personalized"]

AI_SCHEMAS = {
    "reply_interpretation": {
        "type": "object", "additionalProperties": False, "required": REPLY_FIELDS,
        "properties": {
            "label": {"enum": list(LABEL_ACTION)},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "interest_level": {"enum": INTEREST},
            "follow_up_date": {"type": ["string", "null"], "format": "YYYY-MM-DD",
                               "note": "only when the reply states an explicit date; must be after the reply date"},
            "referred_contact": {"type": ["object", "null"], "required": ["name", "email"],
                                 "note": "only people/emails literally present in the reply"},
            "qualification": {"type": "object", "required": QUAL_FIELDS, "properties": {
                "team_size": {"type": ["integer", "null"]},
                "current_solution": {"enum": CURRENT + [None]},
                "timeline_months": {"type": ["integer", "null"]},
                "countries": {"type": "array", "items": {"type": "string", "pattern": "^[A-Z]{2}$"}},
                "pain_points": {"type": "array", "items": {"enum": PAINS}},
                "budget_signal": {"enum": BUDGET + [None]}}},
            "suggested_action": {"enum": ACTIONS, "note": "advisory only; the system derives the action from `label`"},
            "needs_human_review": {"type": "boolean"},
            "evidence": {"type": "string", "maxLength": 200,
                         "note": "verbatim quote from the reply's own text (not the quoted thread)"},
        }},
    "personalization_draft": {
        "type": "object", "additionalProperties": False, "required": DRAFT_FIELDS,
        "properties": {
            "language": {"enum": ["es", "pt", "en"]},
            "subject": {"type": "string", "maxLength": SEND_POLICY["content_rules"]["max_subject_chars"]},
            "body": {"type": "string"},
            "claims": {"type": "array", "maxItems": SEND_POLICY["content_rules"]["max_claims_per_email"], "items": {
                "type": "object", "additionalProperties": False, "required": ["text", "fact_id"]}},
            "personalized": {"type": "boolean"}}},
}

REPLY_RULES = {
    "V001": ("NOT_JSON", "reject_retry", "empty, truncated or prose output"),
    "V002": ("SCHEMA_MISSING_OR_EXTRA_FIELD", "reject_retry", "required field missing or unknown field present"),
    "V003": ("SCHEMA_BAD_VALUE", "reject_retry", "value outside its enum / type / format"),
    "V005": ("CONFIDENCE_OUT_OF_RANGE", "reject_retry", "confidence not in [0,1]"),
    "V006": ("EVIDENCE_NOT_IN_TEXT", "reject_escalate", "evidence quote is not in the reply's own text"),
    "V007": ("REFERRAL_NOT_IN_TEXT", "reject_escalate", "referred name/email not literally in the reply"),
    "V008": ("DATE_NOT_SUPPORTED", "reject_escalate", "follow_up_date not stated in the reply, or not in the future"),
    "V009": ("QUALIFICATION_NOT_SUPPORTED", "reject_escalate", "extracted team size / country / budget / timeline not in the text"),
    "V010": ("ACTION_LABEL_MISMATCH", "override_rule", "suggested_action disagrees with the label map; label map wins"),
    "V011": ("LOW_CONFIDENCE", "escalate_low_confidence", f"confidence < {CONFIDENCE_MIN}: never auto-act"),
    "V012": ("INJECTION_FOLLOWED", "reject_escalate", "reply contains instructions to the system but label is not prompt_injection"),
    "G001": ("OPT_OUT_GUARD", "override_rule", "opt-out wording in the reply forces suppress, whatever the model says"),
}
DRAFT_RULES = {
    "P001": ("NOT_JSON", "reject_retry"), "P002": ("SCHEMA", "reject_retry"),
    "P003": ("CLAIM_WITHOUT_FACT_ID", "fallback_generic"), "P004": ("FACT_NOT_PROVIDED", "fallback_generic"),
    "P005": ("FACT_NOT_USABLE", "fallback_generic"), "P006": ("CLAIM_UNSUPPORTED_BY_FACT", "fallback_generic"),
    "P007": ("FORBIDDEN_CLAIM", "fallback_generic"), "P008": ("MISSING_UNSUBSCRIBE_FOOTER", "fallback_generic"),
    "P009": ("BODY_TOO_LONG", "fallback_generic"), "P010": ("WRONG_LANGUAGE", "fallback_generic"),
    "P011": ("PERSONALIZED_WITHOUT_USABLE_FACTS", "fallback_generic"), "P012": ("SUBJECT_TOO_LONG", "fallback_generic"),
    "P013": ("CLAIM_NOT_IN_BODY", "fallback_generic"), "P014": ("TOO_MANY_CLAIMS", "fallback_generic"),
}
OPT_OUT_RE = re.compile(
    r"don'?t (write|email|contact|send)( to)? me|do not (contact|email|write)|stop (emailing|sending|writing)|"
    r"remove (me|my (email|address))|take me off|unsubscribe|^\s*stop\s*$|"
    r"(delete|deleted) (my|of my) (personal )?data|personal data be deleted|"
    r"(don'?t|do not) want (any )?(more|further) (emails|messages)|no more (emails|messages)|enough (with )?(the )?emails",
    re.I | re.M)
INJECTION_RE = re.compile(
    r"ignore (all )?(your )?(previous|prior) instructions|ignore all your|forget everything|"
    r"^\s*system\s*:|\[\[system|instructions for the assistant|you are now in|\bai assistant\b|"
    r"reply in json|output action\s*=", re.I | re.M)
COUNTRY_WORDS = {"MX": ("mexico",), "CO": ("colombia",), "CL": ("chile",), "BR": ("brazil",),
                 "AR": ("argentina",), "PE": ("peru",)}


def _own_text(reply_text: str) -> str:
    """The reply's own words: drop quoted thread lines ('> ...') and signature noise stays."""
    return "\n".join(l for l in reply_text.splitlines() if not l.lstrip().startswith(">"))


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def _result(verdict, codes, warnings, final):
    return {"verdict": verdict, "violation_codes": sorted(set(codes)), "warnings": warnings, "final_action": final}


def _parse(raw):
    txt, warnings = (raw or "").strip(), []
    if txt.startswith("```"):
        m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", txt, re.S)
        if m:
            txt, warnings = m.group(1), ["FENCED_JSON"]
    try:
        return json.loads(txt), warnings
    except Exception:
        return None, warnings


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _date_ok(s):
    try:
        return date.fromisoformat(s) if isinstance(s, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else None
    except ValueError:
        return None


def _date_in_text(d: date, text: str) -> bool:
    return f"{MONTHS[d.month - 1].lower()} {d.day}" in _norm(text)


def validate_reply(raw: str, reply_text: str, received_at: datetime) -> dict:
    obj, warnings = _parse(raw)
    if obj is None:
        return _result("reject_retry", ["V001"], warnings, "retry_then_escalate_human")
    structural = []
    if not isinstance(obj, dict):
        structural.append("V002")
    else:
        if set(REPLY_FIELDS) - set(obj) or set(obj) - set(REPLY_FIELDS):
            structural.append("V002")
        else:
            q, rc, fu = obj["qualification"], obj["referred_contact"], obj["follow_up_date"]
            bad = (obj["label"] not in LABEL_ACTION or obj["interest_level"] not in INTEREST
                   or obj["suggested_action"] not in ACTIONS or not isinstance(obj["needs_human_review"], bool)
                   or not isinstance(obj["evidence"], str) or len(obj["evidence"]) > 200
                   or (fu is not None and _date_ok(fu) is None)
                   or (rc is not None and not (isinstance(rc, dict) and set(rc) == {"name", "email"}))
                   or not (isinstance(q, dict) and set(q) == set(QUAL_FIELDS)))
            if not bad:
                bad = (not (q["team_size"] is None or _is_int(q["team_size"]))
                       or q["current_solution"] not in CURRENT + [None]
                       or not (q["timeline_months"] is None or _is_int(q["timeline_months"]))
                       or not (isinstance(q["countries"], list) and all(isinstance(c, str) and re.fullmatch(r"[A-Z]{2}", c) for c in q["countries"]))
                       or not (isinstance(q["pain_points"], list) and all(p in PAINS for p in q["pain_points"]))
                       or q["budget_signal"] not in BUDGET + [None])
            if bad:
                structural.append("V003")
            c = obj["confidence"]
            if isinstance(c, bool) or not isinstance(c, (int, float)) or not 0 <= c <= 1:
                structural.append("V005")
    if structural:
        return _result("reject_retry", structural, warnings, "retry_then_escalate_human")

    own, label = _own_text(reply_text), obj["label"]
    codes = []
    ev = obj["evidence"]
    if ev:
        if _norm(ev) not in _norm(own):
            codes.append("V006")
    elif label != "empty_or_truncated":
        codes.append("V006")
    rc = obj["referred_contact"]
    if rc:
        if rc["email"] and rc["email"].lower() not in own.lower():
            codes.append("V007")
        if rc["name"] and rc["name"].lower() not in own.lower():
            codes.append("V007")
    fu = obj["follow_up_date"]
    if fu is not None:
        d = _date_ok(fu)
        if d <= received_at.date() or not _date_in_text(d, own):
            codes.append("V008")
    q = obj["qualification"]
    low = own.lower()
    if (q["team_size"] is not None and not re.search(rf"(?<!\d){q['team_size']}(?!\d)", own)) \
            or any(not any(w in low for w in COUNTRY_WORDS.get(c, ()) ) and not re.search(rf"\b{c}\b", own)
                   for c in q["countries"]) \
            or (q["budget_signal"] and not re.search(r"budget", low)) \
            or (q["timeline_months"] is not None and not re.search(
                r"\d+\s*months?|next (month|quarter|year)|within|semester|quarter|this year", low)):
        codes.append("V009")
    if INJECTION_RE.search(own) and label != "prompt_injection":
        codes.append("V012")
    if codes:
        return _result("reject_escalate", codes, warnings, "escalate_human")

    label_action = LABEL_ACTION[label]
    if OPT_OUT_RE.search(own) and label not in OPT_OUT_LABELS:
        return _result("override_rule", ["G001"] + (["V010"] if obj["suggested_action"] != "suppress" else []),
                       warnings, "suppress")
    if obj["confidence"] < CONFIDENCE_MIN:
        return _result("escalate_low_confidence", ["V011"], warnings, "escalate_human")
    if obj["suggested_action"] != label_action:
        return _result("override_rule", ["V010"], warnings, label_action)
    return _result("accept_with_warning" if warnings else "accept", [], warnings, label_action)


def _words(s):
    return {w for w in re.findall(r"[a-z]{5,}", s.lower())}


def validate_draft(raw: str, facts: list[dict], usable_ids: set[str], contact_language: str) -> dict:
    obj, warnings = _parse(raw)
    if obj is None:
        return {**_result("reject_retry", ["P001"], warnings, "retry"), "template_mode": "retry"}
    ok = isinstance(obj, dict) and set(obj) == set(DRAFT_FIELDS)
    if ok:
        ok = (obj["language"] in ("es", "pt", "en") and isinstance(obj["subject"], str) and isinstance(obj["body"], str)
              and isinstance(obj["personalized"], bool) and isinstance(obj["claims"], list)
              and all(isinstance(c, dict) and set(c) == {"text", "fact_id"} and isinstance(c["text"], str) for c in obj["claims"]))
    if not ok:
        return {**_result("reject_retry", ["P002"], warnings, "retry"), "template_mode": "retry"}
    rules, codes = SEND_POLICY["content_rules"], []
    by_id = {f["fact_id"]: f for f in facts}
    body = obj["body"]
    if len(obj["claims"]) > rules["max_claims_per_email"]:
        codes.append("P014")
    for c in obj["claims"]:
        fid = c["fact_id"]
        if not fid:
            codes.append("P003")
            continue
        if fid not in by_id:
            codes.append("P004")
            continue
        if fid not in usable_ids:
            codes.append("P005")
        ft = by_id[fid]["text"]
        if set(re.findall(r"\d+", c["text"])) - set(re.findall(r"\d+", ft)) or \
                (len(_words(c["text"]) - _words(ft)) / max(1, len(_words(c["text"]))) > 0.34):
            codes.append("P006")
        if _norm(c["text"]) not in _norm(body):
            codes.append("P013")
    text = obj["subject"] + "\n" + body
    for fc in rules["forbidden_claims"]:
        if re.search(fc["regex"], text, re.I):
            codes.append("P007")
    if UNSUBSCRIBE_FOOTER[contact_language] not in body:
        codes.append("P008")
    if len(body.split()) > rules["max_body_words"]:
        codes.append("P009")
    if obj["language"] != contact_language:
        codes.append("P010")
    if (obj["personalized"] or obj["claims"]) and not usable_ids:
        codes.append("P011")
    if len(obj["subject"]) > rules["max_subject_chars"]:
        codes.append("P012")
    if codes:
        return {**_result("fallback_generic", codes, warnings, "contact"), "template_mode": "generic"}
    return {**_result("accept", [], warnings, "contact"),
            "template_mode": "personalized" if obj["claims"] else "generic"}
