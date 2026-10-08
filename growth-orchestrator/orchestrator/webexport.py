"""Regenerate everything the Netlify page shows, from the real engine (python -m orchestrator export-web).

  public/data/runs.json      every golden scenario run through the engine: events, results, audit
                                          trail, mock calls and ledgers (model: offline fixture, labelled as such)
  public/data/stream.json    the 561-delivery sample stream summary
  public/data/evals.json     recorded-suite results (+ the latest live run if one was saved)
  public/data/cases.json     the 18 live eval cases (inputs + expectations) for the live panel
  netlify/functions/_orchestrator_prompts.mjs   prompts, tool schemas, labels and content rules: ONE source (Python)

The page never computes decisions itself; it shows what this code produced. The live panel calls the Netlify function,
which uses the same prompts and a JS port of the validators that is parity-tested against the Python ones.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import hashlib

from . import db, evals, plain, scenario, scoring, showcase
from .ai import prompts
from .engine import AutoApprover
from .ai.fixture import FixtureLLM, reply_output
from .ai.validate import LABEL_ACTION, NEEDS_HUMAN_REVIEW, OPT_OUT_LABELS
from .engine import SENDERS, Orchestrator
from .ai import draft as ai_draft
from .rules import decide
from .policy import SEED_DIR, Policy
from .timeutil import parse

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent
WEB = REPO / "public" / "data"
FN = REPO / "netlify" / "functions"
# Every scenario is run with a harness that approves each held email on the spot, so that what happens AFTER approval
# (retries, windows, duplicates, races) can be checked scenario by scenario. The engine's default has no approver.
HARNESS = AutoApprover("scenario harness (not a person)")
LABEL = ("Recorded run of the real Python engine. The AI steps used the offline fixture (a deterministic stand-in, "
         "not a model); use AI & Safety, Try a reply, to run the real model.")
RUNS_LABEL = LABEL + (" In these scenario runs a test harness (not a person) approves each held email on the spot, so that "
                      "what happens after approval can be shown.")


def _jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def runs() -> dict:
    flows = json.loads((SEED_DIR / "demo_flows.json").read_text(encoding="utf-8"))["flows"]
    out = []
    for g in scenario.load_golden():
        orch, results = scenario.run(g, approver=HARNESS)
        trail = orch.audit.trail()
        out.append({
            "id": g["id"], "title": g["title"], "tags": g["tags"], "notes": g["notes"],
            "mock": {k: v for k, v in (g.get("mock") or {}).items() if v != "ok"},
            "state": {k: v for k, v in g["state"].items() if k in ("accounts", "contacts", "opportunities", "suppression",
                                                                     "outreach_history", "company_facts", "runtime_state")},
            "events": g["events"], "expected": g["expected"],
            "results": [r.to_dict() for r in results],
            "audit": [{"ts": t["ts"], "kind": t["kind"], "event_id": t["event_id"], "detail": t["detail"]} for t in trail],
            "calls": [list(map(str, c)) for c in orch.mocks.calls],
            "ledger": {k: list(v.keys()) for k, v in orch.mocks.ledger.items()},
            "review_queue": db.rows(orch.conn, "SELECT account_id, event_id, reason_codes, status FROM review_queue"),
            "dead_letters": db.rows(orch.conn, "SELECT delivery_id, reason FROM dead_letters"),
            "actions": db.rows(orch.conn, "SELECT idempotency_key, kind, system, status, attempts, send_after FROM actions"),
        })
    return {"label": RUNS_LABEL, "flows": flows, "scenarios": out}


def stream() -> dict:
    sample = SEED_DIR / "sample"
    conn = db.connect()
    db.load_world_dir(conn, sample)
    events = _jsonl(sample / "events.jsonl")
    by_id = {e["event_id"]: e for e in events}
    answers = {by_id[r["event_id"]]["payload"]["body_text"]: reply_output(r["label"], by_id[r["event_id"]]["payload"]["body_text"],
                                                                        r.get("extracted"))
               for r in _jsonl(sample / "truth" / "truth_replies.jsonl") if r["event_id"] in by_id}
    truth = {t["delivery_id"]: t for t in _jsonl(sample / "truth" / "truth_events.jsonl")}
    orch = Orchestrator(conn, llm=FixtureLLM(answers), as_of=parse("2026-10-01T16:00:00Z"))
    res = [(e, orch.process(e)) for e in events]
    agree = sum(1 for e, r in res if r.action == truth[e["delivery_id"]].get("expected_action"))
    return {"label": LABEL, "deliveries": len(res), "handling": dict(Counter(r.handling for _, r in res)),
            "actions": dict(Counter(str(r.action) for _, r in res)),
            "final_actions": dict(Counter(str(r.final_action) for _, r in res)),
            "agreement_with_truth": f"{agree}/{len(res)}",
            "mock_emails": len(orch.mocks.ledger["send"]), "emails_held_for_approval": len(orch.pending_approvals()),
            "review_queue": conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0],
            "dead_letters": conn.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0],
            "ai_calls": dict(Counter(f"{r['kind']}:{r['mode']}" for r in db.rows(conn, "SELECT kind, mode FROM ai_calls")))}


def evals_payload() -> dict:
    rec = evals.run_recorded()
    live = None
    p = evals.RESULTS / "latest-live.json"
    if p.exists():
        live = json.loads(p.read_text(encoding="utf-8"))
    return {"recorded": rec, "live": live}


def scoring_payload() -> dict:
    """The 500 sample accounts with the score inputs, the engine's decision and the audience check (non-passing rows only).
    The page recomputes the score with edited weights; decisions and checks are never recomputed in the browser."""
    sample, cfg, policy, now = SEED_DIR / "sample", scoring.load_config(), Policy.load(), parse("2026-10-01T16:00:00Z")
    conn = db.connect()
    db.load_world_dir(conn, sample)
    facts = {}
    for f in db.rows(conn, "SELECT * FROM company_facts ORDER BY fact_id"):
        facts.setdefault(f["account_id"], []).append(f)
    accounts, labels = [], {}
    templates, footer = ai_draft.load_templates(), json.loads((SEED_DIR / "send_policy.json").read_text(encoding="utf-8"))["content_rules"]["unsubscribe_footer"]["en"]
    now = parse("2026-10-01T16:00:00Z")
    for a in db.rows(conn, "SELECT * FROM accounts ORDER BY account_id"):
        feat = scoring.features(a, facts.get(a["account_id"], []), cfg)
        checks = scoring.audience_check(conn, a["account_id"], now, policy)
        d = decide(conn, a["account_id"], now, policy)
        labels.update({c["id"]: c["label"] for c in checks})
        draft = None
        if d.action == "contact":
            contact = db.one(conn, "SELECT * FROM contacts WHERE contact_id=?", (d.best_contact_id,))
            sender = SENDERS[int(hashlib.sha256(a["account_id"].encode()).hexdigest(), 16) % len(SENDERS)]
            subj, body = ai_draft.render(templates[("en", 1)], contact["first_name"], a["name"], sender, footer)
            usable = [f["text"] for f in ai_draft.usable_facts(facts.get(a["account_id"], []), a, now)]
            draft = {"to": f'{contact["first_name"]} {contact["last_name"]} ({contact["title"]})', "subject": subj, "body": body, "facts": usable}
        accounts.append({"id": a["account_id"], "name": a["name"], "country": a["country"], "industry": a["industry"],
                         "employees": a["employee_count"], "feat": feat, "base": scoring.score(feat, cfg), "draft": draft,
                         "decision": {"action": d.action, "codes": d.reason_codes},
                         "verdict": scoring.verdict(checks),
                         "not_pass": [[c["id"], c["status"], c["code"]] for c in checks if c["status"] != "pass"]})
    return {"label": "Computed by the Python engine on the 500-account sample, state as of 2026-10-01. Weights are illustrative assumptions.",
            "as_of": "2026-10-01T16:00:00Z", "config": cfg, "check_order": list(labels), "check_labels": labels,
            "plain": {"actions": plain.ACTIONS, "codes": plain.CODES, "automation": plain.AUTOMATION},
            "accounts": accounts}


def cases_payload() -> dict:
    return {"cases": evals.cases(), "as_of": "2026-10-01T16:00:00Z"}


def prompts_module() -> str:
    policy = Policy.load()
    templates = [t for t in _jsonl(SEED_DIR / "outreach_templates.jsonl")]
    data = {"default_model": prompts.DEFAULT_MODEL, "reply": prompts.export()["reply"], "draft": prompts.export()["draft"],
            "labels": prompts.LABELS, "actions": prompts.ACTIONS, "interest": prompts.INTEREST, "current": prompts.CURRENT,
            "pains": prompts.PAINS, "budget": prompts.BUDGET, "label_action": LABEL_ACTION,
            "opt_out_labels": sorted(OPT_OUT_LABELS), "needs_human_review": sorted(NEEDS_HUMAN_REVIEW),
            "confidence_min": policy.ai_confidence_min_auto, "content_rules": policy.send["content_rules"],
            "templates": templates}
    return ("// GENERATED by `python -m orchestrator export-web` from growth-orchestrator/orchestrator/ai/prompts.py.\n"
            "// Do not edit by hand: tests fail if this file and the Python source disagree.\n"
            f"export const ORCH = {json.dumps(data, indent=1, ensure_ascii=False)};\n")


def cases_module() -> str:
    return ("// GENERATED by `python -m orchestrator export-web` from data/seed/eval_cases.jsonl. Do not edit by hand.\n"
            f"export const CASES = {json.dumps(evals.cases(), indent=1, ensure_ascii=False)};\n")


def export_all() -> list[Path]:
    WEB.mkdir(parents=True, exist_ok=True)
    written = []
    for name, payload in (("runs.json", runs()), ("stream.json", stream()), ("evals.json", evals_payload()),
                          ("cases.json", cases_payload()), ("scoring.json", scoring_payload()),
                          ("flows.json", showcase.flows_payload()), ("leads.json", showcase.leads_payload()),
                          ("challenge_map.json", showcase.challenge_map_payload())):
        p = WEB / name
        p.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8")
        written.append(p)
    p = FN / "_orchestrator_prompts.mjs"
    p.write_text(prompts_module(), encoding="utf-8")
    written.append(p)
    p = FN / "_orchestrator_cases.mjs"
    p.write_text(cases_module(), encoding="utf-8")
    written.append(p)
    return written


GENERATED = ROOT / "data" / "generated"
EXAMPLES_PER_GROUP = 12


def overview_payload(world: Path = GENERATED) -> dict:
    """All 50,000 accounts: what the rules decide, how the score is distributed, and what that means in hours.
    Weights can be edited on the page because the score only depends on (size, pain, signal count): the payload
    ships exact counts for every such group, plus example accounts for each group."""
    cfg, policy, now = scoring.load_config(), Policy.load(), parse("2026-10-01T16:00:00Z")
    manifest = json.loads((world / "manifest.json").read_text(encoding="utf-8"))
    conn = db.connect()
    db.load_world_dir(conn, world)
    facts = {}
    for f in db.rows(conn, "SELECT * FROM company_facts ORDER BY fact_id"):
        facts.setdefault(f["account_id"], []).append(f)
    actions, reasons, groups, examples = Counter(), {}, {}, {}
    for a in db.rows(conn, "SELECT * FROM accounts ORDER BY account_id"):
        d = decide(conn, a["account_id"], now, policy)
        feat = scoring.features(a, facts.get(a["account_id"], []), cfg)
        key = (feat["size"], feat["pain"], min(len(feat["signals"]), cfg["signal_cap"]))
        actions[d.action] += 1
        reasons.setdefault(d.action, Counter())[d.reason_codes[0]] += 1
        groups.setdefault(key, Counter())[d.action] += 1
        ex = examples.setdefault(key, [])
        if d.action == "contact" and len(ex) < EXAMPLES_PER_GROUP:
            ex.append({"id": a["account_id"], "name": a["name"], "country": a["country"], "industry": a["industry"],
                       "employees": a["employee_count"], "signals": feat["signals"][:3], "n_signals": len(feat["signals"]),
                       "pain": feat["pain_text"], "size": feat["size"]})
    events = Counter()
    for line in (world / "events.jsonl").read_text(encoding="utf-8").splitlines():
        events[json.loads(line)["type"]] += 1
    return {"label": "Computed by the Python engine on all 50,000 synthetic accounts, state as of 2026-10-01. Nothing is sent.",
            "as_of": "2026-10-01T16:00:00Z", "n_accounts": manifest["n_accounts"], "seed": manifest["seed"],
            "dataset": {"id": f'synthetic-{manifest["n_accounts"] // 1000}k-seed{manifest["seed"]}', "determinism_hash": manifest.get("determinism_hash")},
            "events_total": sum(events.values()), "events": dict(events.most_common()),
            "actions": dict(actions), "reasons": {k: dict(v.most_common()) for k, v in reasons.items()},
            "groups": [{"size": k[0], "pain": k[1], "signals": k[2], "counts": dict(v), "examples": examples.get(k, [])}
                       for k, v in sorted(groups.items(), key=str)],
            "config": cfg, "time": json.loads((SEED_DIR / "time_assumptions.json").read_text(encoding="utf-8")),
            "plain": {"actions": plain.ACTIONS, "codes": plain.CODES, "automation": plain.AUTOMATION}}


def export_overview() -> list[Path]:
    out = []
    for name, payload in (("overview.json", overview_payload()), ("operations.json", showcase.operations_payload()),
                          ("approvals.json", showcase.approvals_payload()),
                          ("measurement.json", showcase.measurement_payload()), ("replay.json", showcase.replay_payload()),
                          ("scoring_compare.json", showcase.scoring_compare_payload())):
        p = WEB / name
        p.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8")
        out.append(p)
    return out
