"""Score and audience check: deterministic, and the checklist never disagrees with the rules engine.

Run:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import unittest

from orchestrator import db, scenario, scoring
from orchestrator.policy import SEED_DIR, Policy
from orchestrator.rules import decide
from orchestrator.timeutil import parse

NOW = parse("2026-10-01T16:00:00Z")
# decisions whose reason is more specific than the first failing check
SPECIFIC = {"ENRICHMENT_EXHAUSTED", "NO_AE_AVAILABLE"}


def _worlds():
    for g in scenario.load_golden():
        o = scenario.build(g)
        yield g["id"], o.conn, [a["account_id"] for a in g["state"]["accounts"]]
    conn = db.connect()
    db.load_world_dir(conn, SEED_DIR / "sample")
    yield "sample", conn, [r["account_id"] for r in db.rows(conn, "SELECT account_id FROM accounts")]


class AudienceCheckMatchesEngine(unittest.TestCase):
    def test_eligible_iff_engine_says_contact_and_first_failure_is_the_reason(self):
        pol, n = Policy.load(), 0
        for name, conn, ids in _worlds():
            for aid in ids:
                checks, d, n = scoring.audience_check(conn, aid, NOW, pol), decide(conn, aid, NOW, pol), n + 1
                self.assertEqual(scoring.verdict(checks) == "eligible", d.action == "contact", (name, aid, d.action))
                first = next((c for c in checks if c["status"] != "pass"), None)
                if first and not SPECIFIC & set(d.reason_codes):
                    self.assertIn(first["code"], d.reason_codes, (name, aid))
        self.assertGreater(n, 500)


def _run_variant(gate: bool, strong: bool):
    """G001 (an eligible account, 120 employees, no facts) with the low-priority track on or off; `strong` adds signals and pain."""
    import copy
    g = copy.deepcopy(next(x for x in scenario.load_golden() if x["id"] == "G001"))
    g["gate"] = gate
    if strong:
        aid = g["state"]["accounts"][0]["account_id"]
        g["state"]["accounts"][0]["international_signal"] = True
        g["state"]["company_facts"] = [
            {"fact_id": f"fct_x{i}", "account_id": aid, "type": t, "text": f"Dorada 1 {t} fact {i}.", "source_name": "Company website",
             "source_url": "https://x.example", "observed_at": "2026-09-01T16:00:00Z", "is_verified": 1, "confidence": 0.9}
            for i, t in enumerate(("expansion", "hiring"))] + [
            {"fact_id": "fct_xp", "account_id": aid, "type": "pain_hypothesis", "text": "Hypothesis: manual reconciliation.",
             "source_name": "x", "source_url": "https://x.example", "observed_at": "2026-09-01T16:00:00Z", "is_verified": 0, "confidence": 0.5}]
    return scenario.run(g)


class Nurture(unittest.TestCase):
    def test_weak_account_goes_to_nurture_without_email_or_model_call(self):
        orch, (r,) = _run_variant(gate=True, strong=False)
        self.assertEqual((r.action, r.final_action, r.final_reason_codes), ("contact", "nurture", ["LOW_PRIORITY"]))
        self.assertEqual(orch.mocks.ledger["send"], {})
        self.assertIsNone(r.email)
        self.assertIn("nurture_enrolled", [a["kind"] for a in orch.audit.trail()])

    def test_strong_account_still_gets_the_email(self):
        orch, (r,) = _run_variant(gate=True, strong=True)
        self.assertEqual((r.action, r.final_action), ("contact", "contact"))
        # the strong account is drafted and held for a person; nothing reaches the mock send before approval
        self.assertEqual((len(orch.pending_approvals()), orch.mocks.ledger["send"]), (1, {}))
        self.assertEqual(r.email["status"], "pending_approval")

    def test_track_is_off_for_scenarios_that_do_not_ask_for_it(self):
        orch, (r,) = _run_variant(gate=False, strong=False)
        self.assertEqual(len(orch.pending_approvals()), 1)         # drafted and held: the weak account is not sent to nurture
        self.assertEqual(orch.mocks.ledger["send"], {})

    def test_audit_records_the_score_version(self):
        orch, _ = _run_variant(gate=True, strong=False)
        sc = next(a for a in orch.audit.trail() if a["kind"] == "score")
        self.assertEqual(sc["detail"]["version"], scoring.load_config()["active_version"])


class Versions(unittest.TestCase):
    def test_active_version_is_defined_and_resolved(self):
        cfg = scoring.load_config()
        self.assertIn(cfg["active_version"], [v["id"] for v in cfg["versions"]])
        self.assertEqual(set(cfg["weights"]), {"size", "pain", "signal_each"})
        self.assertGreater(cfg["tier_a"], cfg["tier_b"])


class PlainLanguage(unittest.TestCase):
    def test_every_reason_code_has_everyday_text(self):
        from orchestrator import plain
        pol = Policy.load()
        seen = set()
        for _, conn, ids in _worlds():
            for aid in ids:
                seen |= set(decide(conn, aid, NOW, pol).reason_codes)
        self.assertFalse(seen - set(plain.CODES), seen - set(plain.CODES))


class Score(unittest.TestCase):
    cfg = scoring.load_config()

    def test_max_and_min(self):
        full = {"size": "ideal", "pain": True, "signals": ["a", "b", "c", "d"]}
        self.assertEqual(scoring.score(full, self.cfg)["score"], 100)       # signals capped at 3
        self.assertEqual(scoring.score({"size": "unknown", "pain": False, "signals": []}, self.cfg)["tier"], "C")

    def test_tiers_and_custom_weights(self):
        f = {"size": "ideal", "pain": True, "signals": []}
        self.assertEqual(scoring.score(f, self.cfg)["tier"], "B")           # 30 + 25 = 55
        self.assertEqual(scoring.score(f, self.cfg, {"size": 50, "pain": 30, "signal_each": 15})["tier"], "A")

    def test_size_bands_have_no_upper_limit(self):
        feat = lambda n: scoring.features({"employee_count": n, "international_signal": False}, [], self.cfg)["size"]
        self.assertEqual([feat(n) for n in (None, 5, 10, 11, 50, 51, 1000, 1001, 25000)],
                         ["unknown", "below", "below", "edge", "edge", "ideal", "ideal", "ideal", "ideal"])

    def test_edge_size_rounds_half_up(self):
        w = {"size": 31, "pain": 0, "signal_each": 0}
        self.assertEqual(scoring.score({"size": "edge", "pain": False, "signals": []}, self.cfg, w)["parts"]["size"], 16)


if __name__ == "__main__":
    unittest.main()
