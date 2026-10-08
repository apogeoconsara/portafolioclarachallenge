"""Run a golden scenario (data/seed/golden_scenarios.jsonl) end to end through the orchestrator on a fresh database.

Used by the engine tests, the CLI demo and the web export, so all three exercise exactly the same code path.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import db
from .ai.fixture import FixtureLLM, reply_output
from .engine import Orchestrator
from .mocks import MockSystems
from .policy import SEED_DIR, Policy

SAMPLE_AES = SEED_DIR / "sample" / "aes.jsonl"


def load_golden(seed: Path = SEED_DIR) -> list[dict]:
    return [json.loads(l) for l in (seed / "golden_scenarios.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]


def fixture_for(scenario: dict) -> FixtureLLM:
    """Offline stand-in that answers each reply in the scenario with the label people assigned to it."""
    answers = {}
    for ev, exp in zip(scenario["events"], scenario["expected"]):
        if ev.get("type") == "reply_received" and exp.get("reason_codes"):
            text = ev["payload"]["body_text"].strip()
            answers[text] = reply_output(exp["reason_codes"][0].lower(), text, exp.get("extracted"))
    return FixtureLLM(answers)


def build(scenario: dict, llm=None, policy: Policy | None = None, path=":memory:", approver=None):
    conn = db.connect(path)
    st = scenario["state"]
    db.load_snapshot(conn, {k: v for k, v in st.items() if k not in ("aes", "runtime_state")})
    aes = st.get("aes") or [json.loads(l) for l in SAMPLE_AES.read_text(encoding="utf-8").splitlines() if l.strip()]
    db.insert_rows(conn, "aes", aes)
    rt = st.get("runtime_state")
    if rt:
        conn.execute("INSERT OR REPLACE INTO counters (name, value) VALUES (?,?)", (f"sent:{rt['day']}", rt["sent_today"]))
    if scenario.get("mock_enrichment"):
        me = scenario["mock_enrichment"]
        for a in st["accounts"]:
            db.insert_rows(conn, "mock_enrichment", [{"account_id": a["account_id"], "variant": me["variant"],
                                                     "response": me["response"],
                                                     "expected_action_after_enrichment": me.get("expected_action_after_enrichment")}])
    conn.commit()
    overrides = {a["account_id"]: dict(scenario.get("mock") or {}) for a in st["accounts"]}
    mocks = MockSystems(conn, overrides)
    orch = Orchestrator(conn, policy or Policy.load(), llm if llm is not None else fixture_for(scenario), mocks, approver=approver)
    # golden scenarios test one behaviour each: the low-priority track is off unless the scenario sets "gate": true
    orch.score_cfg = {**orch.score_cfg, "gate_enabled": bool(scenario.get("gate"))}
    return orch


def run(scenario: dict, llm=None, policy: Policy | None = None, approver=None):
    """`approver=None` is the engine's real behaviour: outreach emails stay pending until a person approves them. Tests of what
    happens AFTER approval (retries, windows, duplicates) pass an AutoApprover and say so."""
    orch = build(scenario, llm, policy, approver=approver)
    results = [orch.process(ev) for ev in scenario["events"]]
    return orch, results
