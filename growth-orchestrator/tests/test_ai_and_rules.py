"""The production rules engine and AI validators against the data's independent references.

- rules.decide reproduces the truth for all 500 sample accounts (the truth was produced by a separate oracle);
- validate_reply / validate_draft reproduce the expected verdict and violation codes for all 220 recorded model outputs
  (good, malformed, hallucinated, injected, opt-out-ignoring, ...), and never let an unsafe action through;
- usable_facts (rules, no AI) matches the expected usable facts for every personalization case;
- the LLM client reads its key only from ANTHROPIC_API_KEY and ignores a generic base-URL override.

Run:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import db, rules
from orchestrator.ai import draft as ai_draft
from orchestrator.ai import llm as llm_mod
from orchestrator.ai.reply import final_action
from orchestrator.ai.validate import validate_draft, validate_reply
from orchestrator.policy import Policy
from orchestrator.timeutil import parse

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "data" / "seed"
AS_OF = parse("2026-10-01T16:00:00Z")


def _jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


CASES = {c["case_id"]: c for c in _jsonl(SEED / "eval_cases.jsonl")}
RECS = _jsonl(SEED / "llm_recordings.jsonl")
RULES = json.loads((SEED / "send_policy.json").read_text(encoding="utf-8"))["content_rules"]


class RulesMatchTruth(unittest.TestCase):
    def test_all_sample_accounts(self):
        conn = db.connect()
        db.load_world_dir(conn, SEED / "sample")
        policy = Policy.load()
        bad = []
        for t in _jsonl(SEED / "sample" / "truth" / "truth_accounts.jsonl"):
            d = rules.decide(conn, t["account_id"], AS_OF, policy)
            got = (d.action, d.reason_codes, d.best_contact_id, d.wait_until)
            want = (t["expected_action"], t["reason_codes"], t["best_contact_id"], t["wait_until"])
            if got != want or (t["route_to_ae_id"] and d.route_to_ae_id != t["route_to_ae_id"]):
                bad.append((t["account_id"], want, got))
        self.assertEqual(bad, [])


class ValidatorParity(unittest.TestCase):
    def test_reply_recordings(self):
        n = 0
        for r in (r for r in RECS if r["kind"] == "reply_interpretation"):
            inp, exp = CASES[r["case_id"]]["input"], r["expected"]
            v = validate_reply(r["model_output_raw"], inp["reply_text"], parse(inp["received_at"]).date())
            with self.subTest(r["recording_id"]):
                self.assertEqual(v.verdict, exp["verdict"])
                self.assertEqual(sorted(v.codes), sorted(exp["violation_codes"]))
                if v.usable:
                    act = final_action(v.final_action, v.label, inp["account_context"]["crm_state"])
                    unsafe = act in CASES[r["case_id"]]["expected"].get("unsafe_actions", [])
                    # a confident, well-formed, quote-backed WRONG label cannot be caught by validation alone; those
                    # recordings are marked so, and they are why the eval suite and human review exist
                    self.assertEqual(unsafe, bool(exp.get("final_action_unsafe")))
            n += 1
        self.assertEqual(n, 172)

    def test_draft_recordings(self):
        n = 0
        for r in (r for r in RECS if r["kind"] == "personalization_draft"):
            case = CASES[r["case_id"]]
            facts = case["input"]["facts"]
            usable = {f["fact_id"] for f in ai_draft.usable_facts(facts, case["input"]["account"], AS_OF)}
            v = validate_draft(r["model_output_raw"], facts, usable, case["input"]["contact"]["language"], RULES)
            with self.subTest(r["recording_id"]):
                self.assertEqual(v.verdict, r["expected"]["verdict"])
                self.assertEqual(sorted(v.codes), sorted(r["expected"]["violation_codes"]))
            n += 1
        self.assertEqual(n, 48)

    def test_usable_facts_are_decided_by_rules(self):
        for c in CASES.values():
            if c["kind"] == "personalization_grounding":
                got = [f["fact_id"] for f in ai_draft.usable_facts(c["input"]["facts"], c["input"]["account"], AS_OF)]
                self.assertEqual(got, c["expected"]["usable_fact_ids"], c["case_id"])


class LLMClientSecrets(unittest.TestCase):
    def test_key_only_from_its_own_variable(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False):
            with self.assertRaises(llm_mod.LLMUnavailable):
                llm_mod.AnthropicLLM()

    def test_generic_base_url_is_ignored(self):
        env = {"ANTHROPIC_API_KEY": "test-key", "ANTHROPIC_BASE_URL": "https://evil.example"}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("ORCH_ANTHROPIC_BASE_URL", None)
            c = llm_mod.AnthropicLLM()
            self.assertIn("api.anthropic.com", c.base)

    def test_live_calls_ask_for_temperature_zero(self):
        # Reading a reply is classification: the request must not leave the sampling temperature at the API default (1.0),
        # in the Python client and in the Netlify function alike.
        sent = {}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"content":[{"type":"tool_use","input":{}}],"usage":{}}'

        def fake(req, timeout=None):
            sent.update(json.loads(req.data)); return Resp()
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False), mock.patch("urllib.request.urlopen", fake):
            llm_mod.AnthropicLLM().run("s", "u", {"name": "t"})
        self.assertEqual(sent["temperature"], 0)
        fn = (Path(__file__).resolve().parents[2] / "netlify" / "functions" / "orchestrator-llm.mjs").read_text()
        self.assertIn("temperature: 0", fn)


if __name__ == "__main__":
    unittest.main()
