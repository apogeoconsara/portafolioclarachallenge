"""Interpret an inbound reply with the LLM, then let deterministic rules decide the action."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .llm import LLMUnavailable
from .prompts import REPLY_SYSTEM, REPLY_TOOL, reply_user_message
from .validate import NEEDS_HUMAN_REVIEW, OPT_OUT_RE, own_text, validate_reply

EMPTY = {"interest_level": "unclear", "follow_up_date": None, "referred_contact": None,
         "qualification": {"team_size": None, "current_solution": None, "timeline_months": None, "countries": [],
                           "pain_points": [], "budget_signal": None}}


def final_action(label_action: str, label: str | None, crm_state: str) -> str:
    """State-aware override on top of the label map. Opt-out wins everywhere; customers never enter prospecting."""
    if label_action == "suppress":
        return "suppress"
    if crm_state in ("customer", "churned_customer"):
        return label_action if label_action in ("no_action", "wait") else "escalate_human"
    if crm_state in ("active_opportunity", "ae_assigned") and label in ("info_request", "objection"):
        return "handoff_ae"
    return label_action


@dataclass
class ReplyResult:
    action: str
    reason_codes: list
    verdict: str
    label: str | None = None
    confidence: float | None = None
    violation_codes: list = field(default_factory=list)
    extracted: dict = field(default_factory=lambda: dict(EMPTY))
    needs_human_review: bool = True
    attempts: list = field(default_factory=list)      # [{raw, verdict, codes, model, mode, latency_ms, tokens}]
    used_ai: bool = True


def interpret_reply(llm, reply_text: str, received_at: datetime, context: dict, confidence_min: float = 0.75) -> ReplyResult:
    text = own_text(reply_text)
    crm_state = context.get("crm_state", "prospect")
    if not text.strip():                                    # nothing to interpret: no model call, a human looks at it
        return ReplyResult("escalate_human", ["EMPTY_REPLY"], "skipped", used_ai=False)
    attempts, v = [], None
    for _ in range(2):                                      # one retry, only for structurally invalid output
        try:
            resp = llm.run(REPLY_SYSTEM, reply_user_message(text, received_at.date().isoformat(), context), REPLY_TOOL)
        except LLMUnavailable as e:
            if OPT_OUT_RE.search(text):                     # even without AI, an explicit opt-out is honoured
                return ReplyResult("suppress", ["OPT_OUT_GUARD", "LLM_UNAVAILABLE"], "llm_unavailable", attempts=attempts,
                                   used_ai=False)
            return ReplyResult("escalate_human", ["LLM_UNAVAILABLE"], "llm_unavailable", attempts=attempts,
                               violation_codes=[str(e)], used_ai=False)
        v = validate_reply(resp.raw, text, received_at.date(), confidence_min)
        attempts.append({"raw": resp.raw, "verdict": v.verdict, "codes": v.codes, "model": resp.model, "mode": resp.mode,
                         "latency_ms": resp.latency_ms, "input_tokens": resp.input_tokens, "output_tokens": resp.output_tokens})
        if v.verdict != "reject_retry":
            break
    o = v.output or {}
    extracted = {k: o.get(k, EMPTY.get(k)) for k in EMPTY} if v.usable else dict(EMPTY)
    common = dict(verdict=v.verdict, label=v.label, confidence=v.confidence, violation_codes=v.codes,
                  extracted=extracted, attempts=attempts)
    if v.verdict == "reject_retry":
        return ReplyResult("escalate_human", ["AI_INVALID_OUTPUT"], **common)
    if v.verdict == "reject_escalate":
        return ReplyResult("escalate_human", ["AI_UNSUPPORTED_BY_TEXT"], **common)
    if v.verdict == "escalate_low_confidence":
        return ReplyResult("escalate_human", ["AI_LOW_CONFIDENCE", v.label.upper()], **common)
    action = final_action(v.final_action, v.label, crm_state)
    codes = [v.label.upper()] + (["OPT_OUT_GUARD"] if "G001" in v.codes else [])
    # whether the INTERPRETATION needs a person; a state-driven escalation (e.g. a customer) is queued by the engine anyway
    review = v.label in NEEDS_HUMAN_REVIEW or "G001" in v.codes
    return ReplyResult(action, codes, needs_human_review=review, **common)
