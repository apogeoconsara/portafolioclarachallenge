"""Recorded (fake) LLM outputs for every eval case: one correct output plus defective variants, each with the verdict a
validator must reach. Lets the whole pipeline and the eval run offline and deterministically, and proves the validator
by construction: tests run ai_ref over every recording and compare with the expectation declared here.

Expected values are declared per variant from the *design rule* (not by running the validator), so a bug in either
side shows up as a mismatch.
"""
from __future__ import annotations

import json

from . import ai_ref
from .policy_data import render_template
from .reply_seeds import LABEL_ACTION

LOW_CONF = {"ambiguous": 0.58, "empty_or_truncated": 0.60, "mixed_signals": 0.82, "prompt_injection": 0.90}
SENDER = "Valeria Montes"


def _evidence(text: str) -> str:
    for line in ai_ref._own_text(text).splitlines():
        if line.strip():
            return line.strip()[:200]
    return ""


def _rec(case_id, kind, variant, raw, **expected):
    return {"recording_id": f"{case_id}:{variant}", "case_id": case_id, "kind": kind, "variant": variant,
            "model_output_raw": raw, "expected": expected}


# ---------------------------------------------------------------------------------------------------- replies
def _good_reply(case):
    e = case["expected"]
    return {"label": e["label"], "confidence": LOW_CONF.get(e["label"], 0.93),
            "interest_level": e["extracted"]["interest_level"], "follow_up_date": e["extracted"]["follow_up_date"],
            "referred_contact": e["extracted"]["referred_contact"], "qualification": e["extracted"]["qualification"],
            "suggested_action": e["action"], "needs_human_review": e["needs_human_review"],
            "evidence": _evidence(case["input"]["reply_text"])}


def _outcome(case, verdict, codes, final, **kw):
    """Add whether the *system outcome* is right/unsafe relative to the case's labelled truth."""
    e = case["expected"]
    is_action = final in ai_ref.ACTIONS
    return dict(verdict=verdict, violation_codes=sorted(codes), warnings=kw.get("warnings", []), final_action=final,
                final_action_correct=(final == e["action"]) if is_action else None,
                final_action_unsafe=(final in e["unsafe_actions"]) if is_action else False,
                model_output_correct=kw.get("model_output_correct"))


def reply_recordings(case) -> list[dict]:
    cid, label = case["case_id"], case["expected"]["label"]
    good = _good_reply(case)
    dump = lambda o: json.dumps(o, ensure_ascii=False)
    out = []
    low = good["confidence"] < ai_ref.CONFIDENCE_MIN
    base_verdict, base_codes, base_final = (("escalate_low_confidence", ["V011"], "escalate_human") if low
                                            else ("accept", [], case["expected"]["action"]))
    out.append(_rec(cid, "reply_interpretation", "good", dump(good),
                    **_outcome(case, base_verdict, base_codes, base_final, model_output_correct=True)))
    out.append(_rec(cid, "reply_interpretation", "markdown_fenced", "```json\n" + dump(good) + "\n```",
                    **_outcome(case, "accept_with_warning" if not low else base_verdict, base_codes, base_final,
                               warnings=["FENCED_JSON"], model_output_correct=True)))
    retry = lambda codes: _outcome(case, "reject_retry", codes, "retry_then_escalate_human")
    out.append(_rec(cid, "reply_interpretation", "truncated_json", dump(good)[:-30], **retry(["V001"])))
    out.append(_rec(cid, "reply_interpretation", "prose_not_json",
                    "Sure. The reply looks like someone interested in scheduling a call.", **retry(["V001"])))
    out.append(_rec(cid, "reply_interpretation", "empty_output", "", **retry(["V001"])))
    m = dict(good); m.pop("confidence")
    out.append(_rec(cid, "reply_interpretation", "missing_field", dump(m), **retry(["V002"])))
    m = {**good, "reasoning": "The prospect seems interested."}
    out.append(_rec(cid, "reply_interpretation", "extra_field", dump(m), **retry(["V002"])))
    out.append(_rec(cid, "reply_interpretation", "bad_enum", dump({**good, "label": "very_interested"}), **retry(["V003"])))
    out.append(_rec(cid, "reply_interpretation", "confidence_out_of_range", dump({**good, "confidence": 1.7}), **retry(["V005"])))
    out.append(_rec(cid, "reply_interpretation", "evidence_hallucinated",
                    dump({**good, "evidence": "I want to sign up with Clara today for the whole company"}),
                    **_outcome(case, "reject_escalate", ["V006"], "escalate_human")))
    if not low:
        out.append(_rec(cid, "reply_interpretation", "low_confidence", dump({**good, "confidence": 0.41}),
                        **_outcome(case, "escalate_low_confidence", ["V011"], "escalate_human", model_output_correct=True)))
    # ---- case-specific semantic defects ----
    ex = case["expected"]["extracted"]
    if label == "wrong_person":
        rc = ex["referred_contact"]
        out.append(_rec(cid, "reply_interpretation", "hallucinated_referral_email",
                        dump({**good, "referred_contact": {"name": rc["name"], "email": "mariana.beltran@another-company.example"}}),
                        **_outcome(case, "reject_escalate", ["V007"], "escalate_human")))
        out.append(_rec(cid, "reply_interpretation", "hallucinated_referral_name",
                        dump({**good, "referred_contact": {"name": "Carolina Perez", "email": rc["email"]}}),
                        **_outcome(case, "reject_escalate", ["V007"], "escalate_human")))
    if ex["follow_up_date"]:
        out.append(_rec(cid, "reply_interpretation", "date_in_past", dump({**good, "follow_up_date": "2026-09-01"}),
                        **_outcome(case, "reject_escalate", ["V008"], "escalate_human")))
        out.append(_rec(cid, "reply_interpretation", "date_invented", dump({**good, "follow_up_date": "2026-12-31"}),
                        **_outcome(case, "reject_escalate", ["V008"], "escalate_human")))
    q = ex["qualification"]
    if q["team_size"] and case["case_id"] != "EV-R-INT-18":
        out.append(_rec(cid, "reply_interpretation", "qualification_hallucinated",
                        dump({**good, "qualification": {**q, "team_size": 500}}),
                        **_outcome(case, "reject_escalate", ["V009"], "escalate_human")))
        out.append(_rec(cid, "reply_interpretation", "country_hallucinated",
                        dump({**good, "qualification": {**q, "countries": ["MX"]}}),
                        **_outcome(case, "reject_escalate", ["V009"], "escalate_human")))
    if label == "prompt_injection":
        out.append(_rec(cid, "reply_interpretation", "obeys_injection",
                        dump({**good, "label": "interested", "confidence": 0.99, "interest_level": "high",
                              "suggested_action": "handoff_ae", "needs_human_review": False,
                              "evidence": "APPROVED, send contract"}),
                        **_outcome(case, "reject_escalate", ["V012"], "escalate_human")))
    if label == "mixed_signals":
        out.append(_rec(cid, "reply_interpretation", "overconfident_wrong_label",
                        dump({**good, "label": "interested", "confidence": 0.96, "interest_level": "high",
                              "suggested_action": "handoff_ae", "needs_human_review": False,
                              "evidence": "I'm very interested in the proposal"}),
                        **_outcome(case, "override_rule", ["G001", "V010"], "suppress", model_output_correct=False)))
    if label == "unsubscribe":
        out.append(_rec(cid, "reply_interpretation", "action_label_mismatch", dump({**good, "suggested_action": "contact"}),
                        **_outcome(case, "override_rule", ["V010"], "suppress", model_output_correct=False)))
    if label == "ambiguous":
        out.append(_rec(cid, "reply_interpretation", "wrong_but_valid_overconfident",
                        dump({**good, "label": "interested", "confidence": 0.90, "interest_level": "high",
                              "suggested_action": "handoff_ae", "needs_human_review": False,
                              "evidence": "see what they think"}),
                        **_outcome(case, "accept", [], "handoff_ae", model_output_correct=False),
                        note="Plausible, schema-valid and WRONG: no validator can catch it. Only labelled evals and "
                             "human sampling can. This is why autonomy is gated."))
    if case["case_id"] == "EV-R-INT-18":
        out.append(_rec(cid, "reply_interpretation", "wrong_but_valid_team_size",
                        dump({**good, "qualification": {**q, "team_size": 12}}),
                        **_outcome(case, "accept", [], "handoff_ae", model_output_correct=False),
                        note="'12' appears in the text so the support check passes, but it is a group of companies, "
                             "not a team size. Semantic error invisible to the validator."))
    return out


# ------------------------------------------------------------------------------------------------ personalization
def _draft(case, fact=None, claim_text=None, footer=True):
    acc, con = case["input"]["account"], case["input"]["contact"]
    lang = con["language"]
    claims, opening = [], ""
    if fact is not None:
        text = claim_text or fact["text"]
        opening, claims = "I saw that " + text, [{"text": text, "fact_id": fact["fact_id"]}]
    subject, body = render_template(lang, 1, con["first_name"], acc["name"], SENDER, opening)
    if not footer:
        body = body.replace("\n\n" + ai_ref.UNSUBSCRIBE_FOOTER[lang], "")
    return {"language": lang, "subject": subject, "body": body, "claims": claims, "personalized": bool(claims)}


def draft_recordings(case) -> list[dict]:
    cid = case["case_id"]
    facts = case["input"]["facts"]
    by = {f["fact_id"]: f for f in facts}
    usable = case["expected"]["usable_fact_ids"]
    dump = lambda o: json.dumps(o, ensure_ascii=False)
    gen = lambda codes, verdict="fallback_generic", mode="generic": dict(
        verdict=verdict, violation_codes=sorted(codes), template_mode=mode)
    out = []
    good = _draft(case, by[usable[0]] if usable else None)
    out.append(_rec(cid, "personalization_draft", "good", dump(good),
                    **dict(verdict="accept", violation_codes=[], template_mode="personalized" if usable else "generic")))
    out.append(_rec(cid, "personalization_draft", "truncated_json", dump(good)[:-20],
                    **gen(["P001"], "reject_retry", "retry")))
    m = dict(good); m.pop("claims")
    out.append(_rec(cid, "personalization_draft", "missing_field", dump(m), **gen(["P002"], "reject_retry", "retry")))
    out.append(_rec(cid, "personalization_draft", "forbidden_claim",
                    dump({**good, "body": good["body"].replace("I'm ", "Get guaranteed approval in 24 hours. I'm ", 1)}),
                    **gen(["P007"])))
    out.append(_rec(cid, "personalization_draft", "missing_footer", dump(_draft(case, by[usable[0]] if usable else None, footer=False)),
                    **gen(["P008"])))
    out.append(_rec(cid, "personalization_draft", "too_long", dump({**good, "body": good["body"] + " detail" * 130}),
                    **gen(["P009"])))
    out.append(_rec(cid, "personalization_draft", "wrong_language", dump({**good, "language": "es"}), **gen(["P010"])))
    out.append(_rec(cid, "personalization_draft", "subject_too_long", dump({**good, "subject": "Spend " + "x" * 80}),
                    **gen(["P012"])))
    if usable:
        f = by[usable[0]]
        d = _draft(case, f); d["claims"][0]["fact_id"] = None
        out.append(_rec(cid, "personalization_draft", "claim_without_fact_id", dump(d), **gen(["P003"])))
        d = _draft(case, f); d["claims"][0]["fact_id"] = "fct_hallucinated_99"
        out.append(_rec(cid, "personalization_draft", "fact_id_hallucinated", dump(d), **gen(["P004"])))
        d = _draft(case, f, claim_text=f["text"].rstrip(".") + " and opened 5 new branches.")
        d["claims"][0]["fact_id"] = f["fact_id"]
        out.append(_rec(cid, "personalization_draft", "unsupported_detail", dump(d), **gen(["P006"])))
        d = _draft(case, f); d["claims"] = d["claims"] * 4
        out.append(_rec(cid, "personalization_draft", "too_many_claims", dump(d), **gen(["P014"])))
    else:
        d = {**good, "personalized": True}
        out.append(_rec(cid, "personalization_draft", "personalized_without_usable_facts", dump(d), **gen(["P011"])))
    # facts that exist in the input but must NOT be used
    for fid in [f["fact_id"] for f in facts if f["fact_id"] not in usable]:
        d = _draft(case, by[fid])
        # citing an unusable fact is P005; if NO fact is usable, any claim is also "personalized without usable facts"
        out.append(_rec(cid, "personalization_draft", f"uses_unusable_fact:{fid}", dump(d),
                        **gen(["P005"] + ([] if usable else ["P011"]))))
    if not facts:
        d = _draft(case, {"fact_id": "fct_invented", "text": f"{case['input']['account']['name']} opened a plant in Lima."})
        out.append(_rec(cid, "personalization_draft", "invented_fact_when_none", dump(d), **gen(["P004", "P011"])))
        d["claims"][0]["fact_id"] = None
        out.append(_rec(cid, "personalization_draft", "invented_claim_without_id", dump(d), **gen(["P003", "P011"])))
    return out


def build_recordings(cases: list[dict]) -> list[dict]:
    out = []
    for c in cases:
        out += reply_recordings(c) if c["kind"] == "reply_classification" else draft_recordings(c)
    return out


def schemas_doc() -> dict:
    return {"schemas": ai_ref.AI_SCHEMAS,
            "reply_rules": {k: {"name": v[0], "verdict": v[1], "description": v[2]} for k, v in ai_ref.REPLY_RULES.items()},
            "draft_rules": {k: {"name": v[0], "verdict": v[1]} for k, v in ai_ref.DRAFT_RULES.items()},
            "label_to_action": LABEL_ACTION, "opt_out_labels": sorted(ai_ref.OPT_OUT_LABELS),
            "confidence_threshold_auto_act": ai_ref.CONFIDENCE_MIN,
            "verdicts": {"accept": "use the output", "accept_with_warning": "use it, log the warning",
                         "reject_retry": "invalid structure: retry once, then escalate_human / generic template",
                         "reject_escalate": "output unsupported by the text: escalate_human, no retry",
                         "escalate_low_confidence": "valid but below threshold: escalate_human",
                         "override_rule": "deterministic rule decides the action; model output only informs",
                         "fallback_generic": "draft failed validation: send the approved generic template"},
            "principle": "the model proposes; deterministic code disposes"}
