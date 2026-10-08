"""Deterministic OFFLINE stand-in for the model, used by tests and by the recorded demo runs.

It is not a model and is always labelled mode="fixture" in ai_calls and in the web traces. Replies are answered from a
table {reply text -> raw output} (built from the golden scenarios or the llm_recordings); drafts restate the first usable
fact exactly as the prompt asks. The live path (AnthropicLLM) is what the eval suite and the web panel use.
"""
from __future__ import annotations

import json
import re

from .llm import LLMResponse, LLMUnavailable
from .prompts import DRAFT_TOOL, REPLY_TOOL
from .validate import LABEL_ACTION, NEEDS_HUMAN_REVIEW, own_text

EMPTY_Q = {"team_size": None, "current_solution": None, "timeline_months": None, "countries": [], "pain_points": [],
           "budget_signal": None}


def reply_output(label: str, text: str, extracted: dict | None = None, confidence: float = 0.9) -> str:
    """A well-formed interpretation for `label`, with the reply itself as evidence."""
    x = extracted or {}
    text = own_text(text)
    evidence = text.strip().splitlines()[0][:200] if text.strip() else ""
    return json.dumps({
        "label": label, "confidence": confidence, "interest_level": x.get("interest_level", "unclear"),
        "follow_up_date": x.get("follow_up_date"), "referred_contact": x.get("referred_contact"),
        "qualification": x.get("qualification") or dict(EMPTY_Q), "suggested_action": LABEL_ACTION[label],
        "needs_human_review": label in NEEDS_HUMAN_REVIEW, "evidence": evidence})


def draft_output(user_message: str) -> str:
    subject = re.search(r"^Template subject: (.*)$", user_message, re.M).group(1)
    body = user_message.split("Template body:\n", 1)[1].split("\n\nVerified facts:\n", 1)[0]
    facts = re.findall(r"^- (\S+): (.*)$", user_message.split("Verified facts:\n", 1)[1], re.M)
    if not facts:
        return json.dumps({"language": "en", "subject": subject, "body": body.replace("[OPENING]\n\n", ""),
                           "claims": [], "personalized": False})
    fid, text = facts[0]
    return json.dumps({"language": "en", "subject": subject, "body": body.replace("[OPENING]", f"I saw that {text}"),
                       "claims": [{"text": text, "fact_id": fid}], "personalized": True})


class FixtureLLM:
    mode = "fixture"
    model = "fixture"

    def __init__(self, replies: dict | None = None):
        self.replies = {own_text(k).strip(): v for k, v in (replies or {}).items()}   # the prospect's own words -> raw output
        self.calls = []

    def run(self, system, user, tool, max_tokens=700) -> LLMResponse:
        self.calls.append(tool["name"])
        if tool["name"] == DRAFT_TOOL["name"]:
            return LLMResponse(draft_output(user), self.model, self.mode)
        text = user.split("<reply>\n", 1)[1].rsplit("\n</reply>", 1)[0].strip()
        if text not in self.replies:
            raise LLMUnavailable("fixture has no answer for this reply")
        return LLMResponse(self.replies[text], self.model, self.mode)
