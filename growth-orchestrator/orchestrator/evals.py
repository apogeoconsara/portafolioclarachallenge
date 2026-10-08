"""AI eval suite.

Two suites, never mixed up in the results:

  live       the 18 cases (10 core + 4 extended replies, 4 personalization) sent to the REAL model through the production path
             (prompt -> forced tool call -> validator -> rules). Needs ANTHROPIC_API_KEY. This is the model eval.
  recorded   the 220 recorded outputs (good, malformed, hallucinated, injected, overconfident ...) through the
             validators only. No model is called: this measures the safety layer, not the model.

Results are written to evals/results/<suite>-<model>-<timestamp>.json plus a Markdown summary.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .ai import draft as ai_draft
from .ai.prompts import DEFAULT_MODEL
from .ai.reply import final_action, interpret_reply
from .ai.validate import validate_draft, validate_reply
from .policy import SEED_DIR, Policy
from .timeutil import parse

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "evals" / "results"
AS_OF = parse("2026-10-01T16:00:00Z")
QUAL = ["team_size", "current_solution", "timeline_months", "countries", "pain_points", "budget_signal"]


def _jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def cases() -> list[dict]:
    return _jsonl(SEED_DIR / "eval_cases.jsonl")


# ---- live: the model through the production path -----------------------------------------------------------------
def score_reply_case(llm, case: dict, confidence_min: float) -> dict:
    inp, exp = case["input"], case["expected"]
    t0 = time.time()
    res = interpret_reply(llm, inp["reply_text"], parse(inp["received_at"]), inp["account_context"], confidence_min)
    x = res.extracted or {}
    q, qe = x.get("qualification") or {}, exp["extracted"]["qualification"]
    fields = {"interest_level": x.get("interest_level") == exp["extracted"]["interest_level"],
              "follow_up_date": x.get("follow_up_date") == exp["extracted"]["follow_up_date"],
              "referred_contact": (x.get("referred_contact") or None) == (exp["extracted"]["referred_contact"] or None)}
    fields.update({f"qualification.{k}": (sorted(q.get(k) or []) == sorted(qe.get(k) or [])) if isinstance(qe.get(k), list)
                   else q.get(k) == qe.get(k) for k in QUAL})
    safe_escalation = res.action == "escalate_human" and res.action != exp["action"]
    return {
        "case_id": case["case_id"], "kind": "reply", "expected_label": exp["label"], "label": res.label,
        "label_correct": res.label == exp["label"], "expected_action": exp["action"], "action": res.action,
        "action_correct": res.action == exp["action"], "unsafe": res.action in exp.get("unsafe_actions", []),
        "safe_escalation": safe_escalation, "verdict": res.verdict, "violation_codes": res.violation_codes,
        "confidence": res.confidence, "fields_correct": sum(fields.values()), "fields_total": len(fields),
        "field_misses": [k for k, ok in fields.items() if not ok],
        "attempts": len(res.attempts), "latency_ms": sum(a.get("latency_ms", 0) for a in res.attempts) or int((time.time() - t0) * 1000),
        "input_tokens": sum(a.get("input_tokens", 0) for a in res.attempts),
        "output_tokens": sum(a.get("output_tokens", 0) for a in res.attempts),
        "raw_outputs": [a["raw"] for a in res.attempts],
    }


def score_draft_case(llm, case: dict, policy: Policy, templates: dict) -> dict:
    inp, exp = case["input"], case["expected"]
    rules = policy.send["content_rules"]
    t0 = time.time()
    dr = ai_draft.compose(llm, inp["account"], inp["contact"], inp["facts"], 1, "Valeria Montes", AS_OF, rules, templates)
    cited = [c["fact_id"] for c in dr.claims]
    grounded = set(cited) <= set(exp["usable_fact_ids"])
    return {
        "case_id": case["case_id"], "kind": "draft", "expected_usable_fact_ids": exp["usable_fact_ids"],
        "usable_fact_ids": dr.usable_fact_ids, "mode": dr.mode, "verdict": dr.verdict, "codes": dr.codes,
        "cited_fact_ids": cited, "grounded": grounded,
        "generic_when_no_facts": (dr.mode == "generic") if not exp["usable_fact_ids"] else None,
        "model_called": bool(dr.attempts), "personalized_when_possible": (dr.mode == "personalized") if exp["usable_fact_ids"] else None,
        "unsafe": not grounded, "subject": dr.subject, "body": dr.body,
        "latency_ms": sum(a.get("latency_ms", 0) for a in dr.attempts) or int((time.time() - t0) * 1000),
        "raw_outputs": [a["raw"] for a in dr.attempts],
    }


def run_live(llm, policy: Policy | None = None) -> dict:
    policy = policy or Policy.load()
    templates = ai_draft.load_templates()
    out = []
    for c in cases():
        if c["kind"] == "reply_classification":
            out.append(score_reply_case(llm, c, policy.ai_confidence_min_auto))
        else:
            out.append(score_draft_case(llm, c, policy, templates))
    replies = [r for r in out if r["kind"] == "reply"]
    drafts = [r for r in out if r["kind"] == "draft"]
    summary = {
        "reply_cases": len(replies),
        "label_accuracy": _pct(sum(r["label_correct"] for r in replies), len(replies)),
        "action_accuracy": _pct(sum(r["action_correct"] for r in replies), len(replies)),
        "unsafe_actions": sum(r["unsafe"] for r in out),
        "safe_escalations_instead_of_correct_action": sum(r["safe_escalation"] for r in replies),
        "field_accuracy": _pct(sum(r["fields_correct"] for r in replies), sum(r["fields_total"] for r in replies)),
        "validator_verdicts": dict(Counter(r["verdict"] for r in out)),
        "draft_cases": len(drafts),
        "drafts_grounded": f"{sum(r['grounded'] for r in drafts)}/{len(drafts)}",
        "drafts_personalized_when_facts_exist": f"{sum(bool(r['personalized_when_possible']) for r in drafts if r['personalized_when_possible'] is not None)}"
                                                f"/{sum(1 for r in drafts if r['personalized_when_possible'] is not None)}",
        "drafts_generic_when_no_facts": f"{sum(bool(r['generic_when_no_facts']) for r in drafts if r['generic_when_no_facts'] is not None)}"
                                        f"/{sum(1 for r in drafts if r['generic_when_no_facts'] is not None)}",
        "median_latency_ms": sorted(r["latency_ms"] for r in out)[len(out) // 2],
        "input_tokens": sum(r.get("input_tokens", 0) for r in out), "output_tokens": sum(r.get("output_tokens", 0) for r in out),
    }
    return {"suite": "live", "model": getattr(llm, "model", "?"), "mode": getattr(llm, "mode", "?"),
            "ran_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "summary": summary, "cases": out}


# ---- recorded: the validators against 220 recorded outputs ------------------------------------------------------------
def run_recorded(policy: Policy | None = None) -> dict:
    policy = policy or Policy.load()
    rules = policy.send["content_rules"]
    by_id = {c["case_id"]: c for c in cases()}
    out = []
    for r in _jsonl(SEED_DIR / "llm_recordings.jsonl"):
        c, exp = by_id[r["case_id"]], r["expected"]
        if r["kind"] == "reply_interpretation":
            inp = c["input"]
            v = validate_reply(r["model_output_raw"], inp["reply_text"], parse(inp["received_at"]).date(),
                               policy.ai_confidence_min_auto)
            act = final_action(v.final_action, v.label, inp["account_context"]["crm_state"]) if v.usable else "escalate_human"
            unsafe = act in c["expected"].get("unsafe_actions", [])
        else:
            usable = {f["fact_id"] for f in ai_draft.usable_facts(c["input"]["facts"], c["input"]["account"], AS_OF)}
            v = validate_draft(r["model_output_raw"], c["input"]["facts"], usable, c["input"]["contact"]["language"], rules)
            act, unsafe = v.template_mode, False
        out.append({"recording_id": r["recording_id"], "variant": r["variant"], "kind": r["kind"],
                    "verdict": v.verdict, "codes": v.codes, "expected_verdict": exp["verdict"],
                    "expected_codes": exp["violation_codes"], "match": v.verdict == exp["verdict"] and
                    sorted(v.codes) == sorted(exp["violation_codes"]), "final_action": act, "unsafe_after_validation": unsafe})
    summary = {"recordings": len(out), "verdict_and_codes_match": f"{sum(o['match'] for o in out)}/{len(out)}",
               "verdicts": dict(Counter(o["verdict"] for o in out)),
               "unsafe_after_validation": [o["recording_id"] for o in out if o["unsafe_after_validation"]],
               "variants": dict(Counter(o["variant"] for o in out))}
    return {"suite": "recorded", "model": "none (recorded outputs)", "mode": "recorded",
            "ran_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "summary": summary, "cases": out}


def _pct(a, b) -> str:
    return f"{a}/{b} ({(100 * a / b if b else 0):.0f}%)"


def save(result: dict) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = result["ran_at"].replace(":", "").replace("-", "")
    name = f"{result['suite']}-{result['model'].split()[0].replace('/', '_')}-{stamp}"
    p = RESULTS / f"{name}.json"
    p.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    (RESULTS / f"{name}.md").write_text(to_markdown(result), encoding="utf-8")
    (RESULTS / f"latest-{result['suite']}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


def to_markdown(result: dict) -> str:
    s = result["summary"]
    lines = [f"# Eval results: {result['suite']}", "",
             f"- Model: `{result['model']}` (mode: {result['mode']})", f"- Ran at: {result['ran_at']}", ""]
    lines += [f"- **{k.replace('_', ' ')}**: {v}" for k, v in s.items()]
    lines += ["", "| case | kind | expected | got | verdict | ok |", "|---|---|---|---|---|---|"]
    for c in result["cases"]:
        if result["suite"] == "live" and c["kind"] == "reply":
            lines.append(f"| {c['case_id']} | reply | {c['expected_label']} -> {c['expected_action']} | "
                         f"{c['label']} -> {c['action']} | {c['verdict']} | "
                         f"{'yes' if c['action_correct'] else ('UNSAFE' if c['unsafe'] else 'no (safe)')} |")
        elif result["suite"] == "live":
            lines.append(f"| {c['case_id']} | draft | facts {c['expected_usable_fact_ids'] or 'none'} | {c['mode']} "
                         f"cites {c['cited_fact_ids'] or 'none'} | {c['verdict']} | {'yes' if c['grounded'] else 'UNSAFE'} |")
        else:
            lines.append(f"| {c['recording_id']} | {c['kind']} | {c['expected_verdict']} {c['expected_codes']} | "
                         f"{c['verdict']} {c['codes']} | {c['verdict']} | {'yes' if c['match'] else 'no'} |")
    return "\n".join(lines) + "\n"
