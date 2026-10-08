"""Production validators for model output. Nothing the model returns is used until it passes here.

Verdicts: accept | accept_with_warning | reject_retry (bad structure: retry once) | reject_escalate (not supported by
the text) | escalate_low_confidence | override_rule (a deterministic rule decides) | fallback_generic (draft).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date

from .prompts import ACTIONS, BUDGET, CURRENT, INTEREST, LABELS, PAINS

LABEL_ACTION = {
    "interested": "handoff_ae", "info_request": "escalate_human", "objection": "escalate_human", "not_now": "wait",
    "wrong_person": "enrich", "unsubscribe": "suppress", "out_of_office": "wait", "auto_reply": "no_action",
    "hostile": "suppress", "ambiguous": "escalate_human", "mixed_signals": "suppress",
    "prompt_injection": "escalate_human", "empty_or_truncated": "escalate_human",
}
OPT_OUT_LABELS = {"unsubscribe", "hostile", "mixed_signals"}
NEEDS_HUMAN_REVIEW = {"info_request", "objection", "hostile", "ambiguous", "mixed_signals", "prompt_injection",
                      "empty_or_truncated"}
REPLY_FIELDS = ["label", "confidence", "interest_level", "follow_up_date", "referred_contact", "qualification",
                "suggested_action", "needs_human_review", "evidence"]
QUAL_FIELDS = ["team_size", "current_solution", "timeline_months", "countries", "pain_points", "budget_signal"]
DRAFT_FIELDS = ["language", "subject", "body", "claims", "personalized"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
COUNTRY_WORDS = {"MX": "mexico", "CO": "colombia", "CL": "chile", "BR": "brazil", "AR": "argentina", "PE": "peru"}

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


def own_text(reply_text: str) -> str:
    """The prospect's own words: quoted thread lines ('> ...') are dropped before the model ever sees them."""
    return "\n".join(l for l in (reply_text or "").splitlines() if not l.lstrip().startswith(">"))


def _norm(s) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def _parse(raw):
    txt, warnings = (raw or "").strip(), []
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", txt, re.S)
    if m:
        txt, warnings = m.group(1), ["FENCED_JSON"]
    try:
        return json.loads(txt), warnings
    except (ValueError, TypeError):
        return None, warnings


def _int_or_none(v):
    return v is None or (isinstance(v, int) and not isinstance(v, bool))


@dataclass
class ReplyValidation:
    verdict: str
    codes: list
    warnings: list
    final_action: str          # label-only action, or retry/escalate marker
    output: dict | None = None
    label: str | None = None
    confidence: float | None = None

    @property
    def usable(self) -> bool:
        return self.verdict in ("accept", "accept_with_warning", "override_rule")


def _structure_ok(o) -> list[str]:
    if not isinstance(o, dict) or set(o) != set(REPLY_FIELDS):
        return ["V002"]
    codes = []
    q, rc, fu = o["qualification"], o["referred_contact"], o["follow_up_date"]
    bad = (o["label"] not in LABELS or o["interest_level"] not in INTEREST or o["suggested_action"] not in ACTIONS
           or not isinstance(o["needs_human_review"], bool) or not isinstance(o["evidence"], str) or len(o["evidence"]) > 200
           or (rc is not None and not (isinstance(rc, dict) and set(rc) == {"name", "email"}))
           or not (isinstance(q, dict) and set(q) == set(QUAL_FIELDS)))
    if fu is not None:
        try:
            bad = bad or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(fu)) or not date.fromisoformat(fu)
        except ValueError:
            bad = True
    if not bad:
        bad = (not _int_or_none(q["team_size"]) or q["current_solution"] not in CURRENT + [None]
               or not _int_or_none(q["timeline_months"])
               or not (isinstance(q["countries"], list) and all(isinstance(c, str) and re.fullmatch(r"[A-Z]{2}", c)
                                                                for c in q["countries"]))
               or not (isinstance(q["pain_points"], list) and all(p in PAINS for p in q["pain_points"]))
               or q["budget_signal"] not in BUDGET + [None])
    if bad:
        codes.append("V003")
    c = o["confidence"]
    if isinstance(c, bool) or not isinstance(c, (int, float)) or not 0 <= c <= 1:
        codes.append("V005")
    return codes


def validate_reply(raw: str, reply_text: str, received_date: date, confidence_min: float = 0.75) -> ReplyValidation:
    o, warnings = _parse(raw)
    if o is None:
        return ReplyValidation("reject_retry", ["V001"], warnings, "retry_then_escalate_human")
    codes = _structure_ok(o)
    if codes:
        return ReplyValidation("reject_retry", sorted(set(codes)), warnings, "retry_then_escalate_human", o)
    text = own_text(reply_text)
    low, label = text.lower(), o["label"]
    sem = []
    ev = o["evidence"]
    if ev:
        if _norm(ev) not in _norm(text):
            sem.append("V006")                         # quote not in the reply: the model is making things up
    elif label != "empty_or_truncated":
        sem.append("V006")
    rc = o["referred_contact"]
    if rc and ((rc["email"] and rc["email"].lower() not in low) or (rc["name"] and rc["name"].lower() not in low)):
        sem.append("V007")
    if o["follow_up_date"] is not None:
        d = date.fromisoformat(o["follow_up_date"])
        if d <= received_date or f"{MONTHS[d.month - 1].lower()} {d.day}" not in _norm(text):
            sem.append("V008")
    q = o["qualification"]
    if ((q["team_size"] is not None and not re.search(rf"(?<!\d){q['team_size']}(?!\d)", text))
            or any(COUNTRY_WORDS.get(c, "~") not in low and not re.search(rf"\b{c}\b", text) for c in q["countries"])
            or (q["budget_signal"] and "budget" not in low)
            or (q["timeline_months"] is not None and not re.search(
                r"\d+\s*months?|next (month|quarter|year)|within|semester|quarter|this year", low))):
        sem.append("V009")
    if INJECTION_RE.search(text) and label != "prompt_injection":
        sem.append("V012")                             # the model followed or missed an injection
    if sem:
        return ReplyValidation("reject_escalate", sorted(set(sem)), warnings, "escalate_human", o, label, o["confidence"])
    base = LABEL_ACTION[label]
    if OPT_OUT_RE.search(text) and label not in OPT_OUT_LABELS:     # deterministic guard: opt-out always wins
        return ReplyValidation("override_rule", ["G001"] + (["V010"] if o["suggested_action"] != "suppress" else []),
                               warnings, "suppress", o, label, o["confidence"])
    if o["confidence"] < confidence_min:
        return ReplyValidation("escalate_low_confidence", ["V011"], warnings, "escalate_human", o, label, o["confidence"])
    if o["suggested_action"] != base:                 # the model never picks the action
        return ReplyValidation("override_rule", ["V010"], warnings, base, o, label, o["confidence"])
    return ReplyValidation("accept_with_warning" if warnings else "accept", [], warnings, base, o, label, o["confidence"])


@dataclass
class DraftValidation:
    verdict: str
    codes: list
    template_mode: str
    output: dict | None = None
    warnings: list = field(default_factory=list)


def _words(s):
    return set(re.findall(r"[a-z]{5,}", (s or "").lower()))


def validate_draft(raw: str, facts: list[dict], usable_ids: set, contact_language: str, content_rules: dict) -> DraftValidation:
    o, warnings = _parse(raw)
    if o is None:
        return DraftValidation("reject_retry", ["P001"], "retry", None, warnings)
    ok = isinstance(o, dict) and set(o) == set(DRAFT_FIELDS) and o.get("language") in ("es", "pt", "en") \
        and isinstance(o.get("subject"), str) and isinstance(o.get("body"), str) and isinstance(o.get("personalized"), bool) \
        and isinstance(o.get("claims"), list) \
        and all(isinstance(c, dict) and set(c) == {"text", "fact_id"} and isinstance(c["text"], str) for c in o["claims"])
    if not ok:
        return DraftValidation("reject_retry", ["P002"], "retry", None, warnings)
    by_id = {f["fact_id"]: f for f in facts}
    body, codes = o["body"], []
    if len(o["claims"]) > content_rules["max_claims_per_email"]:
        codes.append("P014")
    for c in o["claims"]:
        fid = c["fact_id"]
        if not fid:
            codes.append("P003"); continue
        if fid not in by_id:
            codes.append("P004"); continue
        if fid not in usable_ids:
            codes.append("P005")
        ft = by_id[fid]["text"]
        cw = _words(c["text"])
        if set(re.findall(r"\d+", c["text"])) - set(re.findall(r"\d+", ft)) or len(cw - _words(ft)) / max(1, len(cw)) > 0.34:
            codes.append("P006")
        if _norm(c["text"]) not in _norm(body):
            codes.append("P013")
    text = o["subject"] + "\n" + body
    if any(re.search(r["regex"], text, re.I) for r in content_rules["forbidden_claims"]):
        codes.append("P007")
    if content_rules["unsubscribe_footer"][contact_language] not in body:
        codes.append("P008")
    if len(body.split()) > content_rules["max_body_words"]:
        codes.append("P009")
    if o["language"] != contact_language:
        codes.append("P010")
    if (o["personalized"] or o["claims"]) and not usable_ids:
        codes.append("P011")
    if len(o["subject"]) > content_rules["max_subject_chars"]:
        codes.append("P012")
    if codes:
        return DraftValidation("fallback_generic", sorted(set(codes)), "generic", o, warnings)
    return DraftValidation("accept", [], "personalized" if o["claims"] else "generic", o, warnings)
