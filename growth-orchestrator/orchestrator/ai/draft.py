"""Grounded personalization. Which facts are usable is decided by rules (no AI); the model may only restate one."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .llm import LLMUnavailable
from .prompts import DRAFT_SYSTEM, DRAFT_TOOL, draft_user_message
from .validate import validate_draft

SEED = Path(__file__).resolve().parents[2] / "data" / "seed"


def load_templates(seed: Path = SEED) -> dict:
    out = {}
    for line in (seed / "outreach_templates.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            t = json.loads(line)
            out[(t["language"], t["step"])] = t
    return out


def usable_facts(facts: list[dict], account: dict, now: datetime) -> list[dict]:
    """Deterministic filter: verified, at most a year old, names THIS company, and does not contradict the CRM size."""
    out = []
    for f in facts:
        if not f["is_verified"]:
            continue
        from ..timeutil import parse
        if now - parse(f["observed_at"]) > timedelta(days=365):
            continue
        if account["name"] not in f["text"]:
            continue
        m = re.search(r"(\d+)\s+employees", f["text"])
        if m and account.get("employee_count") and not (0.5 <= int(m.group(1)) / account["employee_count"] <= 2):
            continue
        out.append(f)
    return out


def render(template: dict, first_name: str, company: str, sender: str, footer: str, opening: str = "") -> tuple[str, str]:
    fmt = dict(first_name=first_name, company=company, sender_name=sender, unsubscribe_footer=footer,
               personalized_opening=(opening + "\n\n") if opening else "")
    return template["subject"].format(**fmt), template["body"].format(**fmt)


@dataclass
class DraftResult:
    subject: str
    body: str
    mode: str                     # personalized | generic
    verdict: str
    codes: list = field(default_factory=list)
    claims: list = field(default_factory=list)
    usable_fact_ids: list = field(default_factory=list)
    attempts: list = field(default_factory=list)
    used_ai: bool = False


def compose(llm, account: dict, contact: dict, facts: list[dict], step: int, sender: str, now: datetime,
            content_rules: dict, templates: dict) -> DraftResult:
    tpl = templates[("en", min(step, 4))]
    footer = content_rules["unsubscribe_footer"]["en"]
    g_subject, g_body = render(tpl, contact["first_name"], account["name"], sender, footer)
    usable = usable_facts(facts, account, now)
    ids = [f["fact_id"] for f in usable]
    generic = lambda verdict, codes, attempts=(), used=False: DraftResult(g_subject, g_body, "generic", verdict, list(codes),
                                                                          [], ids, list(attempts), used)
    if not usable or "{personalized_opening}" not in tpl["body"]:
        return generic("no_usable_facts" if not usable else "step_without_opening", [])
    _, placeholder_body = render(tpl, contact["first_name"], account["name"], sender, footer, "[OPENING]")
    attempts, v = [], None
    for _ in range(2):
        try:
            resp = llm.run(DRAFT_SYSTEM, draft_user_message(g_subject, placeholder_body, usable), DRAFT_TOOL, 900)
        except LLMUnavailable:
            return generic("llm_unavailable", ["LLM_UNAVAILABLE"], attempts)
        v = validate_draft(resp.raw, facts, set(ids), "en", content_rules)
        attempts.append({"raw": resp.raw, "verdict": v.verdict, "codes": v.codes, "model": resp.model, "mode": resp.mode,
                         "latency_ms": resp.latency_ms})
        if v.verdict != "reject_retry":
            break
    if v.verdict != "accept":
        return generic(v.verdict, v.codes, attempts, True)
    o = v.output
    # extra guard: apart from the opening, the approved template must be untouched
    stripped = o["body"]
    for c in o["claims"]:
        stripped = re.sub(r"I saw that " + re.escape(c["text"]) + r"\s*", "", stripped)
    if re.sub(r"\s+", " ", stripped).strip() != re.sub(r"\s+", " ", g_body).strip() or o["subject"] != g_subject:
        return generic("fallback_generic", ["P015_TEMPLATE_MODIFIED"], attempts, True)
    return DraftResult(o["subject"], o["body"], v.template_mode, "accept", [], o["claims"], ids, attempts, True)
